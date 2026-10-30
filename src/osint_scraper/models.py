"""Core data models shared by every crawler, downloader and manifest writer.

These dataclasses are the contract between the crawler layer (which discovers
media) and the download layer (which fetches it). Crawlers never write files and
the downloader never performs discovery.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

MediaKind = Literal["image", "video", "audio", "unknown"]
"""Kind of media a :class:`MediaRef` points at."""


@dataclass(frozen=True, slots=True)
class MediaRef:
    """A single downloadable media object attached to a post.

    Attributes:
        url: Absolute URL of the media. May be a CDN URL with an expiring signature.
        kind: Broad media category. Drives extension guessing and the downloader.
        width: Pixel width when known, otherwise ``None``.
        height: Pixel height when known, otherwise ``None``.
        mime: MIME type when the platform advertises one.
        extension: File extension including the dot, when known (e.g. ``".mp4"``).
        index: Zero-based position of this media object within the owning
            post's media list, when known.
        media_id: The platform's own identifier for this media object, when it
            has one (for example an Instagram media id or a Threads child
            ``pk``).
    """

    url: str
    kind: MediaKind = "unknown"
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    extension: str | None = None
    index: int | None = None
    media_id: str | None = None

    @property
    def size(self) -> int | None:
        """Pixel count, used for picking the highest quality candidate."""
        if self.width and self.height:
            return self.width * self.height
        return None


def stamp_indices(refs: list[MediaRef]) -> list[MediaRef]:
    """Return refs with their position in the list recorded on each.

    ``MediaRef`` is frozen, so the index cannot be set in place. The returned
    list is new; the input list is left untouched.

    Args:
        refs: Media references in the order the owning post presents them.

    Returns:
        A new list where each reference carries its zero-based ``index``.
    """
    return [dataclasses.replace(ref, index=position) for position, ref in enumerate(refs)]


@dataclass(slots=True)
class Post:
    """A single post (Instagram post/reel/story, Threads post) and its media.

    Attributes:
        platform: Platform identifier, e.g. ``"instagram"`` or ``"threads"``.
        target: Handle or profile the post belongs to.
        post_id: Stable platform identifier (Instagram media id, Threads ``pk``).
        shortcode: Short code used in permalinks, when the platform has one.
        permalink: Canonical public URL of the post.
        caption: Post text/caption, when present.
        timestamp: Unix timestamp of publication, when known.
        media: Media objects attached to the post, each stamped with its
            position and platform media id when known. Empty for text-only
            posts.
        metadata: Platform-specific extras (engagement counts, quoted post ids, ...).
        quoted_post_id: Id of a quoted post, when the post quotes another.
        parent_post_id: Id of the post being replied to, when the post is a reply.
    """

    platform: str
    target: str
    post_id: str
    shortcode: str | None = None
    permalink: str | None = None
    caption: str | None = None
    timestamp: int | None = None
    media: list[MediaRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    quoted_post_id: str | None = None
    parent_post_id: str | None = None

    @property
    def has_media(self) -> bool:
        """Whether the post carries at least one downloadable media object."""
        return bool(self.media)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the post."""
        return dataclasses.asdict(self)


@dataclass(slots=True)
class ProfileInfo:
    """Metadata about a scraped profile.

    Attributes:
        platform: Platform identifier.
        target: Handle that was requested.
        display_name: Human readable profile name.
        follower_count: Follower count when the platform exposes it.
        post_count: Total post count when the platform exposes it.
        biography: Profile bio text.
        metadata: Platform-specific extras.
    """

    platform: str
    target: str
    display_name: str | None = None
    follower_count: int | None = None
    post_count: int | None = None
    biography: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the profile."""
        return dataclasses.asdict(self)


@dataclass(slots=True)
class CrawlStats:
    """Counters describing what a crawl found.

    Attributes:
        posts_seen: Total posts encountered, including duplicates and text-only posts.
        posts_with_media: Posts that carried at least one media object.
        media_found: Total media objects found.
        truncated: Whether the crawl stopped early because of a limit or a
            platform-imposed pagination ceiling.
        notes: Human readable remarks about the crawl (truncation reason, warnings).
    """

    posts_seen: int = 0
    posts_with_media: int = 0
    media_found: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
