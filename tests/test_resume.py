"""Tests for the resume, file-check and hash-dedupe subsystem.

The downloader keeps a real SQLite history (:class:`osint_scraper.database.Database`)
so that a second run can recognise work it has already done:

* resume keyed on the stable media path (a rotated CDN signature still matches);
* file checks that re-download a file which changed or disappeared on disk;
* SHA-256 deduplication so identical content is only stored once;
* partial downloads continued with a ``Range`` request.

Every test uses a fresh ``tmp_path``, a real :class:`Database` and an
:class:`httpx.MockTransport` whose handler counts calls, so "no request was
made" is an assertion rather than an assumption.  No real network traffic is
ever generated.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from osint_scraper.config.settings import DownloadSettings
from osint_scraper.database import Database
from osint_scraper.downloader import Downloader
from osint_scraper.models import MediaRef, Post
from osint_scraper.ratelimit import CooldownStore, Pacer

LOGGER = logging.getLogger("test_resume")
LOGGER.addHandler(logging.NullHandler())

_POST_STEM = "2024-01-01_00-00-00_ABC123"
"""Deterministic filename stem produced by the ``_post`` helper's timestamp/shortcode."""


def _settings(**overrides: object) -> DownloadSettings:
    """Return download settings suitable for a test.

    Args:
        **overrides: Values that replace the test defaults.

    Returns:
        The settings object.
    """
    defaults = {
        "directory": "./osint-output",
        "concurrency": 2,
        "delay": 0.0,
        "jitter": 0.0,
        "retries": 1,
        "timeout": 10.0,
        "overwrite": False,
        "save_metadata": False,
        "min_size": 0,
    }
    defaults.update(overrides)
    return DownloadSettings(**defaults)  # type: ignore[arg-type]


def _post(media: list[MediaRef], **overrides: object) -> Post:
    """Return a deterministic post carrying ``media``.

    Args:
        media: The media references attached to the post.
        **overrides: Post fields that replace the test defaults.

    Returns:
        The post object.
    """
    kwargs = {
        "platform": "instagram",
        "target": "test_user",
        "post_id": "1234567890",
        "shortcode": "ABC123",
        "timestamp": 1704067200,
        "media": media,
    }
    kwargs.update(overrides)
    return Post(**kwargs)  # type: ignore[arg-type]


def _database(tmp_path: Path) -> Database:
    """Open a fresh history database inside ``tmp_path``.

    Args:
        tmp_path: Test-scoped temporary directory.

    Returns:
        A connected database. The caller owns closing it.
    """
    db = Database(tmp_path / "history.db")
    db.connect()
    return db


def _pacer(tmp_path: Path) -> Pacer:
    """Return a pacer with private cooldown state and no delays.

    Args:
        tmp_path: Test-scoped temporary directory.

    Returns:
        A pacer that never sleeps and never reads the user's real cooldown file.
    """
    store = CooldownStore(path=tmp_path / "cooldown.json")
    return Pacer(store, base_delay=0.0, jitter=0.0)


def _downloader(
    settings: DownloadSettings,
    client: httpx.AsyncClient,
    history: Database,
    tmp_path: Path,
    *,
    resume: bool = True,
    verify: bool = True,
    dedupe: bool = True,
) -> Downloader:
    """Build a downloader wired to the test client, database and pacer.

    Args:
        settings: Download settings.
        client: Mock-transport HTTP client.
        history: Real history database.
        tmp_path: Test-scoped temporary directory for pacer state.
        resume: Whether existing media may be skipped.
        verify: Whether existing files are re-hashed.
        dedupe: Whether identical content is stored only once.

    Returns:
        A configured downloader.
    """
    return Downloader(
        settings,
        LOGGER,
        client=client,
        pacer=_pacer(tmp_path),
        history=history,
        resume=resume,
        verify=verify,
        dedupe=dedupe,
    )


def _dest_dir(tmp_path: Path) -> Path:
    """Return the directory the downloader writes this post's media to."""
    return tmp_path / "instagram" / "test_user"


def _target(tmp_path: Path, name: str = f"{_POST_STEM}.jpg") -> Path:
    """Return the deterministic path of a single-media post."""
    return _dest_dir(tmp_path) / name


def _image(url: str) -> MediaRef:
    """Return a JPEG media reference for ``url``."""
    return MediaRef(url=url, kind="image", mime="image/jpeg")


def _part_path(target: Path) -> Path:
    """Return the partial-download path next to ``target``."""
    return target.with_name(target.name + ".part")


async def test_resume_skips_media_that_was_already_downloaded(tmp_path: Path) -> None:
    """A second run over the same post must not touch the network."""
    content = b"already-downloaded-image"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content, headers={"content-type": "image/jpeg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg?token=1")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(post, tmp_path) == 1
        assert len(calls) == 1
        assert first.results[0].skipped is False

        second = _downloader(_settings(), client, db, tmp_path)
        available = await second.download_post(post, tmp_path)
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert available == 1
    assert result.ok is True
    assert result.skipped is True
    assert second.stats["skipped"] == 1
    assert second.stats["downloaded"] == 0
    # The whole point: no HTTP request was made by the second run.
    assert len(calls) == 1
    assert result.path == _target(tmp_path)
    assert result.path.read_bytes() == content


async def test_rotated_cdn_token_still_matches(tmp_path: Path) -> None:
    """A different query string with the same path must still resume."""
    content = b"cdn-image"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    first_post = _post([_image("https://cdn.example.com/v/abc.jpg?token=1")])
    second_post = _post([_image("https://cdn.example.com/v/abc.jpg?token=2")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(first_post, tmp_path) == 1
        assert len(calls) == 1

        second = _downloader(_settings(), client, db, tmp_path)
        assert await second.download_post(second_post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert result.url == "https://cdn.example.com/v/abc.jpg?token=2"
    assert result.skipped is True
    assert result.ok is True
    assert len(calls) == 1
    assert result.path == _target(tmp_path)
    assert result.path.read_bytes() == content


async def test_changed_file_is_downloaded_again(tmp_path: Path) -> None:
    """A file whose digest no longer matches the history must be re-fetched."""
    original = b"original-image-bytes"
    tampered = b"x" * len(original)
    updated = b"updated-image-bytes"
    served = {"content": original}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=served["content"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(post, tmp_path) == 1
        target = first.results[0].path
        # Same size, different bytes: only the digest check can catch this.
        target.write_bytes(tampered)
        served["content"] = updated

        second = _downloader(_settings(), client, db, tmp_path)
        assert await second.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert len(calls) == 2
    assert result.ok is True
    assert result.skipped is False
    assert result.sha256 == hashlib.sha256(updated).hexdigest()
    assert target.read_bytes() == updated


async def test_deleted_file_is_downloaded_again(tmp_path: Path) -> None:
    """A history row without its file must not prevent a fresh download."""
    content = b"deleted-then-restored"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(post, tmp_path) == 1
        target = first.results[0].path
        target.unlink()
        assert not target.exists()
        assert db.lookup_media("instagram", "/image.jpg") is not None

        second = _downloader(_settings(), client, db, tmp_path)
        assert await second.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert len(calls) == 2
    assert result.ok is True
    assert result.skipped is False
    assert target.read_bytes() == content


async def test_duplicate_content_is_not_stored_twice(tmp_path: Path) -> None:
    """Two media keys serving identical bytes must leave one file behind."""
    content = b"shared-image"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post(
        [
            _image("https://example.com/first.jpg"),
            _image("https://example.com/second.jpg"),
        ]
    )
    try:
        # concurrency=1 makes the recording order deterministic: the first
        # media is fully recorded before the second starts its dedupe check.
        downloader = _downloader(_settings(concurrency=1), client, db, tmp_path)
        assert await downloader.download_post(post, tmp_path) == 2
    finally:
        await client.aclose()
        db.close()

    first, second = downloader.results
    first_path = _dest_dir(tmp_path) / f"{_POST_STEM}_1.jpg"
    second_path = _dest_dir(tmp_path) / f"{_POST_STEM}_2.jpg"
    assert len(calls) == 2
    assert first.path == first_path
    assert first_path.exists()
    assert first_path.read_bytes() == content
    assert second.duplicate is True
    assert second.skipped is True
    assert second.ok is True
    assert second.path == first_path
    assert not second_path.exists()
    assert sorted(p.name for p in _dest_dir(tmp_path).iterdir()) == [first_path.name]
    assert downloader.stats["duplicates"] == 1
    # The duplicate was fetched and then discarded, so it counts as skipped
    # rather than downloaded: the counters partition the media items.
    assert downloader.stats["skipped"] == 1
    assert downloader.stats["downloaded"] == 1
    assert downloader.stats["failed"] == 0


async def test_partial_download_resumes_with_range_header(tmp_path: Path) -> None:
    """An existing ``.part`` file must be continued with a range request."""
    content = b"0123456789abcdef"
    prefix_length = 7
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    _part_path(target).write_bytes(content[:prefix_length])
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        ranges.append(requested)
        if requested is not None:
            start = int(requested.removeprefix("bytes=").split("-", 1)[0])
            return httpx.Response(
                206,
                content=content[start:],
                headers={"content-range": f"bytes {start}-{len(content) - 1}/{len(content)}"},
            )
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        downloader = _downloader(_settings(), client, db, tmp_path)
        assert await downloader.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = downloader.results[0]
    assert ranges == [f"bytes={prefix_length}-"]
    assert result.ok is True
    assert result.resumed is True
    assert downloader.stats["resumed"] == 1
    assert target.read_bytes() == content
    assert not _part_path(target).exists()


async def test_server_ignoring_the_range_restarts_cleanly(tmp_path: Path) -> None:
    """A ``200`` reply to a range request must replace, not append to, the part."""
    content = b"full-image-content"
    prefix_length = 5
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    _part_path(target).write_bytes(content[:prefix_length])
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range"))
        # Ignore the range entirely and serve the whole file.
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        downloader = _downloader(_settings(), client, db, tmp_path)
        assert await downloader.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = downloader.results[0]
    assert ranges == [f"bytes={prefix_length}-"]
    assert result.ok is True
    assert result.resumed is False
    assert target.read_bytes() == content
    assert target.read_bytes() != content[:prefix_length] + content
    assert not _part_path(target).exists()


async def test_oversized_partial_is_discarded_after_416(tmp_path: Path) -> None:
    """A ``.part`` larger than the media must be thrown away and refetched."""
    content = b"real-content"
    oversized = content + b"stale-extra-bytes"
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    _part_path(target).write_bytes(oversized)
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        ranges.append(requested)
        if requested is not None:
            return httpx.Response(416)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        downloader = _downloader(_settings(), client, db, tmp_path)
        assert await downloader.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = downloader.results[0]
    assert ranges == [f"bytes={len(oversized)}-", None]
    assert result.ok is True
    assert result.resumed is False
    assert target.read_bytes() == content
    assert not _part_path(target).exists()


async def test_resume_false_always_downloads(tmp_path: Path) -> None:
    """Disabling resume must fetch the media even when the file is present."""
    content = b"always-refetch"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(post, tmp_path) == 1
        assert len(calls) == 1
        original = first.results[0].path
        assert original.exists()

        second = _downloader(_settings(), client, db, tmp_path, resume=False, dedupe=False)
        assert await second.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert len(calls) == 2
    assert result.ok is True
    assert result.skipped is False
    assert result.path != original
    assert result.path.read_bytes() == content
    assert original.read_bytes() == content


async def test_verify_false_trusts_a_matching_size(tmp_path: Path) -> None:
    """Without verification a same-size file is skipped without being hashed."""
    content = b"trusted-content"
    tampered = b"y" * len(content)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db = _database(tmp_path)
    post = _post([_image("https://example.com/image.jpg")])
    try:
        first = _downloader(_settings(), client, db, tmp_path)
        assert await first.download_post(post, tmp_path) == 1
        target = first.results[0].path
        # Same size, different bytes. verify=False must trust it blindly.
        target.write_bytes(tampered)

        second = _downloader(_settings(), client, db, tmp_path, verify=False)
        assert await second.download_post(post, tmp_path) == 1
    finally:
        await client.aclose()
        db.close()

    result = second.results[0]
    assert len(calls) == 1
    assert result.ok is True
    assert result.skipped is True
    assert result.reason == "already downloaded"
    assert target.read_bytes() == tampered
