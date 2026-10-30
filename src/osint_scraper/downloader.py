"""Async media downloader for discovered posts.

The :class:`Downloader` is intentionally dumb: it receives :class:`Post` objects
from a crawler and writes their attached media to disk.  Discovery, cookies and
parsing live in other modules.

Three behaviours make a re-run cheap instead of wasteful, and all three are
borrowed from ``cyberdrop-dl``:

**Resume.**  A media URL on Instagram or Threads carries a short-lived CDN
signature, so the same image arrives under a different full URL on every run.
The history is therefore keyed on the URL *path*, and a media whose file is
still on disk is recognised and skipped without a single request.

**File check by hash.**  Before trusting an existing file, its SHA-256 is
compared with the recorded digest.  A mismatch means the platform changed the
media, so the file is downloaded again.

**Partial resume.**  An interrupted download leaves a ``.part`` file behind.
The next run asks the server for the remainder with a ``Range`` header instead
of starting over.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from osint_scraper.config.settings import DownloadSettings
from osint_scraper.database import Database, media_key
from osint_scraper.exceptions import DownloadError
from osint_scraper.filepath import extension_for, sanitize_filename, unique_path
from osint_scraper.models import MediaRef, Post
from osint_scraper.ratelimit import Pacer

_HASH_CHUNK = 1024 * 1024


@dataclass(slots=True)
class DownloadResult:
    """Outcome of a single media download attempt.

    Attributes:
        url: The URL that was requested.
        path: The local path where the file was written (or intended to be written).
        size: Number of bytes written. ``0`` when the download failed.
        sha256: Hex digest of the written bytes. Empty string when the download failed.
        kind: Media kind advertised by the crawler.
        ok: Whether the file is present and usable. A skipped file counts as
            ok: the media *is* on disk, it was simply not fetched again.
        error: Human-readable failure reason when ``ok`` is ``False``.
        skipped: Whether the file was already present and was not downloaded again.
        resumed: Whether the download continued a partial file instead of starting over.
        duplicate: Whether the content was already in the library under a different name.
        reason: Why the file was skipped.
    """

    url: str
    path: Path
    size: int
    sha256: str
    kind: str
    ok: bool
    error: str | None = None
    skipped: bool = False
    resumed: bool = False
    duplicate: bool = False
    reason: str | None = None


class _RetryableDownloadError(Exception):
    """Internal signal that a request should be retried."""


class _ThrottledDownloadError(Exception):
    """Internal signal that the host asked us to slow down."""


class _FatalDownloadError(Exception):
    """Internal signal that a request failed and should not be retried."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Downloader:
    """Download media referenced by :class:`Post` objects.

    The downloader owns its own HTTP client unless one is provided.  It is polite,
    concurrent and retries transient failures.  Individual media failures are
    recorded as :class:`DownloadResult` entries rather than raised.
    """

    def __init__(
        self,
        settings: DownloadSettings,
        logger: logging.Logger,
        client: httpx.AsyncClient | None = None,
        pacer: Pacer | None = None,
        history: Database | None = None,
        *,
        resume: bool = True,
        verify: bool = True,
        dedupe: bool = True,
    ) -> None:
        """Create a downloader.

        Args:
            settings: Download behaviour configuration.
            logger: Logger for progress and warnings.
            client: Optional shared HTTP client.  When omitted the downloader
                creates and owns one.
            pacer: Shared request pacer.  When omitted one is created from the
                configured delay and jitter.
            history: Optional history database.  Without it nothing is ever
                skipped and every run re-downloads everything.
            resume: Whether media already on disk may be skipped.
            verify: Whether an existing file is re-hashed and compared with the
                recorded digest before it is trusted.
            dedupe: Whether media whose content is already in the library under
                another name is stored a second time.
        """
        self.settings = settings
        self.logger = logger
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._own_client = client is None
        self._pacer = pacer or Pacer(base_delay=settings.delay, jitter=settings.jitter)
        self._results: list[DownloadResult] = []
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._history = history
        self._resume = resume
        self._verify = verify
        self._dedupe = dedupe
        self._stats: dict[str, int] = {
            "downloaded": 0,
            "skipped": 0,
            "duplicates": 0,
            "resumed": 0,
            "failed": 0,
        }

    @property
    def results(self) -> list[DownloadResult]:
        """Return the results for the most recent ``download_post`` call."""
        return list(self._results)

    @property
    def stats(self) -> dict[str, int]:
        """Return the counters for the whole run.

        ``downloaded``, ``skipped`` and ``failed`` partition the media items:
        a media item is either newly stored, already present, or failed.
        ``duplicates`` counts the items that were skipped because their
        content already existed under another name, and ``resumed`` counts the
        downloads that continued a partial file.
        """
        return dict(self._stats)

    async def aclose(self) -> None:
        """Close the internal HTTP client if the downloader created it."""
        if self._own_client and not self._client.is_closed:
            await self._client.aclose()

    async def download_post(self, post: Post, root: Path) -> int:
        """Download every media item attached to ``post``.

        Args:
            post: The post whose media should be fetched.
            root: Root output directory.  Files are written to
                ``root/<platform>/<sanitised_target>/``.

        Returns:
            Number of media files available on disk, whether they were
            downloaded now or were already there.
        """
        self._validate_post(post)
        self._results.clear()

        dest_dir = root / post.platform / sanitize_filename(post.target)
        dest_dir.mkdir(parents=True, exist_ok=True)

        media = [ref for ref in post.media if ref.url]
        if not media:
            self.logger.info(
                "%s/%s has no downloadable media",
                post.platform,
                post.target,
            )
            return 0

        base_stem = _base_stem(post)
        total = len(media)

        # Resolve every target path before anything is written, so that the names do
        # not depend on which download finishes first.
        paths = [
            self._resolve_target_path(ref, index, total, base_stem, dest_dir)
            for index, ref in enumerate(media)
        ]

        ordered: list[DownloadResult | None] = [None] * total

        async def _download_one(index: int, ref: MediaRef) -> None:
            async with self._semaphore:
                ordered[index] = await self._handle_media(post, ref, index, total, paths[index])

        await asyncio.gather(*(_download_one(idx, ref) for idx, ref in enumerate(media)))
        # Keep the results in media order, not completion order, so that the manifest
        # and the metadata sidecar are identical between runs.
        self._results = [result for result in ordered if result is not None]

        available = sum(1 for result in self._results if result.ok)

        if self.settings.save_metadata and self._results:
            self._write_metadata_sidecar(post, dest_dir, base_stem)

        self.logger.info(
            "%s/%s: %d/%d media files available",
            post.platform,
            post.target,
            available,
            total,
        )
        return available

    @staticmethod
    def _validate_post(post: Post) -> None:
        if not isinstance(post, Post):
            raise DownloadError("expected a Post instance")
        if not post.platform or not post.target or not post.post_id:
            raise DownloadError("post is missing platform, target or post_id")

    async def _handle_media(
        self,
        post: Post,
        ref: MediaRef,
        index: int,
        total: int,
        target_path: Path,
    ) -> DownloadResult:
        """Skip, download or dedupe a single media item.

        Args:
            post: The post the media belongs to.
            ref: The media reference.
            index: Zero-based position of the media within the post.
            total: Number of media items in the post.
            target_path: Where the file should end up.

        Returns:
            The outcome of the media item.
        """
        url = ref.url
        key = media_key(url)

        existing = await self._check_existing(post.platform, key, url, target_path, ref)
        if existing is not None:
            self._record(post, key, url, existing)
            return existing

        result = await self._download_media(ref, index, total, target_path)

        if result.ok and self._dedupe and self._history is not None and result.sha256:
            duplicate = self._dedupe_result(result)
            if duplicate is not None:
                result = duplicate

        if result.ok:
            self._record(post, key, url, result)
        return result

    async def _check_existing(
        self,
        platform: str,
        key: str,
        url: str,
        target_path: Path,
        ref: MediaRef,
    ) -> DownloadResult | None:
        """Return a result when the media is already on disk.

        The history is consulted first: a recorded media whose file is still
        there is the cheapest possible answer.  When there is no record the
        deterministic path is checked instead, and the file's digest decides
        whether it is a copy we already have.

        Args:
            platform: Platform the media belongs to.
            key: Stable media key.
            url: The media URL.
            target_path: Where the file would be written.
            ref: The media reference.

        Returns:
            A skipped result, or ``None`` when the media must be downloaded.
        """
        if self._history is None or not self._resume or self.settings.overwrite:
            return None

        record = self._history.lookup_media(platform, key)
        candidate = Path(record.path) if record is not None else target_path
        actual_size = await asyncio.to_thread(_file_size, candidate)
        if actual_size is None:
            # Either never downloaded, or the user deleted it.
            return None

        if record is not None and record.size is not None and record.size != actual_size:
            self.logger.info(
                "%s: %s changed on disk; downloading it again",
                platform,
                candidate.name,
            )
            return None

        digest: str | None = None
        if self._verify:
            digest = await asyncio.to_thread(_file_digest, candidate)
            if record is not None and record.sha256 and digest != record.sha256:
                self.logger.info(
                    "%s: %s does not match its recorded digest; downloading it again",
                    platform,
                    candidate.name,
                )
                return None

        if digest is not None:
            known = self._history.hash_path(digest)
            if known is not None:
                known_path = Path(known)
                known_size = await asyncio.to_thread(_file_size, known_path)
                if known_size is not None and known_path != candidate:
                    # The same content already lives somewhere else in the library.
                    self.logger.info(
                        "%s: identical content already present at %s",
                        platform,
                        known_path,
                    )
                    self._stats["skipped"] += 1
                    self._stats["duplicates"] += 1
                    return DownloadResult(
                        url=url,
                        path=known_path,
                        size=known_size,
                        sha256=digest,
                        kind=ref.kind,
                        ok=True,
                        skipped=True,
                        duplicate=True,
                        reason="identical content already present",
                    )

        self.logger.info(
            "%s: %s already downloaded, skipping",
            platform,
            candidate.name,
        )
        self._stats["skipped"] += 1
        return DownloadResult(
            url=url,
            path=candidate,
            size=actual_size,
            sha256=digest or (record.sha256 if record is not None else "") or "",
            kind=ref.kind,
            ok=True,
            skipped=True,
            reason="already downloaded",
        )

    def _dedupe_result(self, result: DownloadResult) -> DownloadResult | None:
        """Discard a freshly downloaded file whose content is already stored.

        Args:
            result: The result of a successful download.

        Returns:
            A duplicate result, or ``None`` when the file is unique.
        """
        if self._history is None or not result.sha256:
            return None
        known = self._history.hash_path(result.sha256)
        if known is None:
            return None
        known_path = Path(known)
        if not known_path.exists() or known_path == result.path:
            return None
        result.path.unlink(missing_ok=True)
        self.logger.info(
            "identical content already present at %s; not storing it twice",
            known_path,
        )
        # The bytes were fetched, but nothing new was stored.  Count it as
        # skipped rather than downloaded so that the counters partition the
        # media: downloaded + skipped + failed == the number of media items.
        self._stats["downloaded"] -= 1
        self._stats["skipped"] += 1
        self._stats["duplicates"] += 1
        return DownloadResult(
            url=result.url,
            path=known_path,
            size=result.size,
            sha256=result.sha256,
            kind=result.kind,
            ok=True,
            skipped=True,
            duplicate=True,
            reason="identical content already present",
        )

    def _record(self, post: Post, key: str, url: str, result: DownloadResult) -> None:
        """Write the outcome of a media item into the history.

        Args:
            post: The post the media belongs to.
            key: Stable media key.
            url: The media URL.
            result: The outcome to record.
        """
        if self._history is None:
            return
        self._history.record_media(
            platform=post.platform,
            media_key=key,
            url=url,
            path=str(result.path),
            sha256=result.sha256 or None,
            size=result.size or None,
            completed=result.ok,
        )
        if result.ok and result.sha256 and result.size:
            self._history.record_hash(
                sha256=result.sha256,
                size=result.size,
                path=str(result.path),
                platform=post.platform,
            )

    async def _download_media(
        self,
        ref: MediaRef,
        index: int,
        total: int,
        target_path: Path,
    ) -> DownloadResult:
        url = ref.url

        for attempt in range(self.settings.retries + 1):
            if attempt > 0:
                backoff = min(2 ** (attempt - 1), 30)
                await asyncio.sleep(backoff)

            try:
                await self._apply_politeness(url)
                size, sha256, resumed = await self._stream_to_disk(url, target_path)

                if size < self.settings.min_size:
                    await asyncio.to_thread(target_path.unlink, missing_ok=True)
                    error = f"file too small ({size} < {self.settings.min_size} bytes)"
                    self.logger.warning("%s: %s", url, error)
                    self._stats["failed"] += 1
                    return DownloadResult(
                        url=url,
                        path=target_path,
                        size=0,
                        sha256="",
                        kind=ref.kind,
                        ok=False,
                        error=error,
                    )

                if resumed:
                    self._stats["resumed"] += 1
                self._stats["downloaded"] += 1
                self.logger.info(
                    "%s/%s: media %d/%d downloaded (%d bytes%s)",
                    ref.kind,
                    target_path.name,
                    index + 1,
                    total,
                    size,
                    ", resumed" if resumed else "",
                )
                return DownloadResult(
                    url=url,
                    path=target_path,
                    size=size,
                    sha256=sha256,
                    kind=ref.kind,
                    ok=True,
                    resumed=resumed,
                )

            except _FatalDownloadError as exc:
                self.logger.warning("%s: %s", url, exc.message)
                self._stats["failed"] += 1
                return DownloadResult(
                    url=url,
                    path=target_path,
                    size=0,
                    sha256="",
                    kind=ref.kind,
                    ok=False,
                    error=exc.message,
                )
            except _ThrottledDownloadError:
                cooldown = self._pacer.on_throttled(url)
                error = f"throttled (HTTP 429); cooling down for {cooldown:.0f} s"
                self.logger.warning("%s: %s", url, error)
                self._stats["failed"] += 1
                return DownloadResult(
                    url=url,
                    path=target_path,
                    size=0,
                    sha256="",
                    kind=ref.kind,
                    ok=False,
                    error=error,
                )
            except _RetryableDownloadError as exc:
                self.logger.debug("%s: retryable failure on attempt %d: %s", url, attempt + 1, exc)
                if attempt == self.settings.retries:
                    error = f"failed after {self.settings.retries} retries"
                    self.logger.warning("%s: %s", url, error)
                    self._stats["failed"] += 1
                    return DownloadResult(
                        url=url,
                        path=target_path,
                        size=0,
                        sha256="",
                        kind=ref.kind,
                        ok=False,
                        error=error,
                    )
            except httpx.HTTPError as exc:
                self.logger.debug("%s: HTTP error on attempt %d: %s", url, attempt + 1, exc)
                if attempt == self.settings.retries:
                    error = f"network error after {self.settings.retries} retries: {exc}"
                    self.logger.warning("%s: %s", url, error)
                    self._stats["failed"] += 1
                    return DownloadResult(
                        url=url,
                        path=target_path,
                        size=0,
                        sha256="",
                        kind=ref.kind,
                        ok=False,
                        error=error,
                    )
            except OSError as exc:
                self.logger.warning("%s: OS error: %s", url, exc)
                self._stats["failed"] += 1
                return DownloadResult(
                    url=url,
                    path=target_path,
                    size=0,
                    sha256="",
                    kind=ref.kind,
                    ok=False,
                    error=f"file-system error: {exc}",
                )

        # Unreachable, but keeps mypy happy when retries == 0 and all attempts fail.
        self._stats["failed"] += 1
        return DownloadResult(
            url=url,
            path=target_path,
            size=0,
            sha256="",
            kind=ref.kind,
            ok=False,
            error="download failed",
        )

    def _resolve_target_path(
        self,
        ref: MediaRef,
        index: int,
        total: int,
        base_stem: str,
        dest_dir: Path,
    ) -> Path:
        """Return the path a media item should be written to.

        With the resume machinery in play the path has to be *stable*: the same
        media must land on the same name on every run, otherwise the history
        can never find it again.  Without it the old non-clobbering behaviour
        applies and a numeric suffix is appended to avoid overwriting.

        Args:
            ref: The media reference.
            index: Zero-based position of the media within the post.
            total: Number of media items in the post.
            base_stem: Filename stem shared by the post's media.
            dest_dir: Directory the media is written to.

        Returns:
            The path to write to.
        """
        stem = base_stem
        if total > 1:
            stem = f"{base_stem}_{index + 1}"
        stem = sanitize_filename(stem)
        extension = ref.extension or extension_for(ref.mime, ref.url)
        deterministic = dest_dir / f"{stem}{extension}"

        if self._resume or self.settings.overwrite:
            return deterministic
        return unique_path(dest_dir, stem, extension)

    async def _apply_politeness(self, url: str) -> None:
        """Wait until the host may be contacted again.

        Args:
            url: The URL about to be requested.

        Raises:
            RateLimitError: When the host is in a cooldown.
        """
        await self._pacer.wait(url)

    async def _stream_to_disk(self, url: str, target_path: Path) -> tuple[int, str, bool]:
        """Fetch ``url`` into ``target_path``.

        An existing ``.part`` file is continued with a ``Range`` header instead
        of being thrown away, so an interrupted download is picked up where it
        stopped rather than starting over.

        Args:
            url: The URL to fetch.
            target_path: Where the finished file belongs.

        Returns:
            The size of the finished file, its SHA-256 digest, and whether the
            download resumed a partial file.

        Raises:
            _ThrottledDownloadError: When the host answered with HTTP 429.
            _RetryableDownloadError: When the host answered with a 5xx status.
            _FatalDownloadError: When the host answered with a 4xx status.
        """
        temp_path = target_path.with_name(target_path.name + ".part")

        # Two attempts at most: the second one is only needed when the server
        # rejects the range and the partial file has to go.
        for attempt in range(2):
            resume_from = 0
            if temp_path.exists():
                resume_from = temp_path.stat().st_size
                if resume_from == 0:
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                    resume_from = 0

            headers: dict[str, str] = {}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"

            hasher = hashlib.sha256()
            if resume_from:
                # The bytes already on disk are part of the file, so they are
                # part of the digest too.
                await asyncio.to_thread(_feed_digest, hasher, temp_path)

            async with self._client.stream(
                "GET",
                url,
                headers=headers,
                follow_redirects=True,
                timeout=self.settings.timeout,
            ) as response:
                if response.status_code == 416 and attempt == 0:
                    # The partial file is longer than the file itself.  Throw
                    # it away and ask for the whole thing.
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                    continue
                if response.status_code == 429:
                    raise _ThrottledDownloadError("HTTP 429")
                if response.status_code >= 500:
                    raise _RetryableDownloadError(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise _FatalDownloadError(f"HTTP {response.status_code}")

                resumed = resume_from > 0 and response.status_code == 206
                if resume_from and not resumed:
                    # The server ignored the range and sent the whole file.
                    hasher = hashlib.sha256()
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                    resume_from = 0

                mode = "ab" if resumed else "wb"
                size = resume_from
                with temp_path.open(mode) as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)

            await asyncio.to_thread(temp_path.replace, target_path)
            return size, hasher.hexdigest(), resumed

        raise _FatalDownloadError("the server refused the range request")

    def _write_metadata_sidecar(self, post: Post, dest_dir: Path, base_stem: str) -> None:
        """Write the post's metadata next to its media.

        Args:
            post: The post the sidecar describes.
            dest_dir: Directory holding the post's media.
            base_stem: Filename stem shared by the post's media.
        """
        sidecar_path = dest_dir / f"{sanitize_filename(base_stem)}.json"
        files = [
            {
                "url": result.url,
                "path": str(result.path),
                "sha256": result.sha256,
                "size": result.size,
                "kind": result.kind,
                "skipped": result.skipped,
                "duplicate": result.duplicate,
            }
            for result in self._results
        ]
        payload: dict[str, Any] = {
            **post.to_dict(),
            "files": files,
        }
        sidecar_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _file_digest(path: Path) -> str:
    """Return the SHA-256 digest of the file at ``path``."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _file_size(path: Path) -> int | None:
    """Return the size of the file at ``path``, or ``None`` when it is missing."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _feed_digest(hasher: Any, path: Path) -> None:
    """Feed the contents of ``path`` into ``hasher``."""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            hasher.update(chunk)


def _base_stem(post: Post) -> str:
    """Return the deterministic filename stem for ``post``'s media."""
    if post.timestamp is not None and post.shortcode:
        published = _utc_timestamp_to_str(post.timestamp)
        return f"{published}_{post.shortcode}"
    if post.timestamp is not None:
        return _utc_timestamp_to_str(post.timestamp)
    return post.post_id


def _utc_timestamp_to_str(timestamp: int) -> str:
    """Format a Unix timestamp as ``YYYY-MM-DD_HH-MM-SS`` in UTC."""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
