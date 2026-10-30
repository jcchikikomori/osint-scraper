"""Manifest writing.

Every run produces a ``manifest.jsonl`` next to the downloaded media. Each line
is one of three kinds of record:

* ``"post"`` — one per crawled post, whether or not it carried media.
* ``"media"`` — one per media object discovered on a post, recording the
  object's identity and position before any download happens.
* ``"file"`` — one per downloaded file, carrying the path, digest and size.

The manifest is the audit trail of a collection run — it records what was found,
where it came from and when.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from osint_scraper.models import MediaRef, Post


@dataclass(slots=True)
class ManifestEntry:
    """A single line of the manifest.

    Attributes:
        kind: ``"post"`` for a crawled post, ``"media"`` for a media object
            discovered on a post, or ``"file"`` for a downloaded file.
        platform: Platform the entry came from.
        target: Target handle the entry came from.
        post_id: Post identifier, when applicable.
        url: Source URL of the media, for file entries.
        path: Path of the downloaded file relative to the output root.
        sha256: SHA-256 digest of the downloaded file.
        size: Size of the downloaded file in bytes.
        timestamp: Unix timestamp of the post, when known.
        metadata: Extra platform metadata.
    """

    kind: str
    platform: str
    target: str
    post_id: str | None = None
    url: str | None = None
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    timestamp: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the entry."""
        return {
            "kind": self.kind,
            "platform": self.platform,
            "target": self.target,
            "post_id": self.post_id,
            "url": self.url,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class ManifestWriter:
    """Append-only JSONL manifest writer.

    The writer is intentionally append-only and flushed after every record, so a
    run that is interrupted still leaves a valid, complete-so-far manifest.
    """

    def __init__(self, path: Path) -> None:
        """Open the manifest for writing.

        Args:
            path: Path of the ``manifest.jsonl`` file.
        """
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._count = 0

    def __enter__(self) -> ManifestWriter:
        """Enter the context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying file handle."""
        self.close()

    def write(self, entry: ManifestEntry) -> None:
        """Append a single entry.

        Args:
            entry: The entry to append.
        """
        self._handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._handle.flush()
        self._count += 1

    def write_post(self, post: Post, *, saved: int = 0) -> None:
        """Append an entry describing a crawled post.

        Args:
            post: The post that was crawled.
            saved: Number of media files downloaded for the post.
        """
        self.write(
            ManifestEntry(
                kind="post",
                platform=post.platform,
                target=post.target,
                post_id=post.post_id,
                url=post.permalink,
                timestamp=post.timestamp,
                metadata={
                    "shortcode": post.shortcode,
                    "caption": post.caption,
                    "media_count": len(post.media),
                    "saved": saved,
                    "quoted_post_id": post.quoted_post_id,
                    "parent_post_id": post.parent_post_id,
                    **post.metadata,
                },
            )
        )
        for ref in post.media:
            self.write_media(post, ref)

    def write_media(self, post: Post, ref: MediaRef) -> None:
        """Append an entry describing one media object attached to ``post``.

        The row records discovery only: the ``"file"`` row written after a
        download carries the path, digest and size.

        Args:
            post: The post that owns the media object.
            ref: The media reference to record.
        """
        self.write(
            ManifestEntry(
                kind="media",
                platform=post.platform,
                target=post.target,
                post_id=post.post_id,
                url=ref.url,
                timestamp=post.timestamp,
                metadata={
                    "index": ref.index,
                    "media_id": ref.media_id,
                    "kind": ref.kind,
                    "width": ref.width,
                    "height": ref.height,
                    "mime": ref.mime,
                    "extension": ref.extension,
                    "shortcode": post.shortcode,
                },
            )
        )

    def write_file(
        self,
        post: Post,
        *,
        url: str,
        path: Path,
        sha256: str | None = None,
        size: int | None = None,
        root: Path | None = None,
    ) -> None:
        """Append an entry describing a downloaded file.

        Args:
            post: The post the file belongs to.
            url: Source URL of the file.
            path: Path the file was written to.
            sha256: SHA-256 digest of the file, when computed.
            size: Size of the file in bytes, when known.
            root: Root the recorded path is made relative to.
        """
        relative = path
        if root is not None:
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = path
        self.write(
            ManifestEntry(
                kind="file",
                platform=post.platform,
                target=post.target,
                post_id=post.post_id,
                url=url,
                path=str(relative),
                sha256=sha256,
                size=size,
                timestamp=post.timestamp,
                metadata={"shortcode": post.shortcode},
            )
        )

    @property
    def count(self) -> int:
        """Number of entries written so far."""
        return self._count

    def close(self) -> None:
        """Close the manifest file."""
        if not self._handle.closed:
            self._handle.close()

    def summary(self) -> dict[str, Any]:
        """Return a small summary of the run suitable for printing.

        Returns:
            A mapping with the manifest path and entry count.
        """
        return {
            "manifest": str(self.path),
            "entries": self._count,
            "generated_at": int(time.time()),
        }
