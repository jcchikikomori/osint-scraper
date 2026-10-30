"""The Instagram iPhone-API transport: stories, highlights and their items.

Instagram's public profile page carries posts, but it does not carry the
account's stories or highlights. Those are served by the iPhone API, which
instaloader wraps as ``InstaloaderContext.get_iphone_json``. The endpoint
requires an authenticated session: the bearer token is synthesised from the
``ds_user_id`` and ``sessionid`` cookies.

This module holds the wire-level details -- the request paths, the response
envelopes and the conversion of a story/highlight item into a
:class:`~osint_scraper.models.Post`. The crawler in
:mod:`osint_scraper.crawlers.instagram` does the target parsing and the
dispatch, mirroring the split between ``threads`` and ``threads_api``.
"""

from __future__ import annotations

from typing import Any

from osint_scraper.filepath import extension_for
from osint_scraper.models import MediaRef, Post, stamp_indices

SURFACE_POSTS = "posts"
"""The profile's own feed."""

SURFACE_TAGGED = "tagged"
"""Posts the target is tagged in -- target-scoped, not self-scoped."""

SURFACE_STORIES = "stories"
"""The account's current stories."""

SURFACE_HIGHLIGHTS = "highlights"
"""The account's saved highlights, a container of story items."""

SURFACE_IGTV = "igtv"
"""IGTV videos. Broken upstream in instaloader 4.16."""

VIDEO_VERSION_TYPE = 101
"""The ``video_versions`` entry that is the actual video.

The other entries (102, 103) are alternate representations; on a story item
they share the same URL. When 101 is absent the highest-bandwidth entry is
used instead.
"""

HIGHLIGHT_ID_PREFIX = "highlight:"
"""The prefix Instagram gives a highlight's reel id."""

_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2


def reels_media_path(reel_id: str) -> str:
    """Return the iPhone-API path that serves one reel's items.

    Args:
        reel_id: A numeric user id for stories, or ``"highlight:<id>"`` for a
            highlight.

    Returns:
        The path to pass to ``get_iphone_json``.
    """
    return f"api/v1/feed/reels_media/?reel_ids={reel_id}"


def stories_path(user_id: str) -> str:
    """Return the iPhone-API path that serves an account's stories.

    Args:
        user_id: The account's numeric user id.

    Returns:
        The path to pass to ``get_iphone_json``.
    """
    return reels_media_path(user_id)


def highlights_tray_path(user_id: str) -> str:
    """Return the iPhone-API path that lists an account's highlights.

    Args:
        user_id: The account's numeric user id.

    Returns:
        The path to pass to ``get_iphone_json``.
    """
    return f"api/v1/highlights/{user_id}/highlights_tray/"


def highlight_reel_id(highlight_id: str) -> str:
    """Return the full reel id of a highlight from a tray entry's ``id``.

    A tray entry's ``id`` already has the ``"highlight:<numeric>"`` form, but
    the numeric part alone is accepted too.

    Args:
        highlight_id: A tray entry's ``id``, e.g. ``"highlight:1794"``.

    Returns:
        The reel id, e.g. ``"highlight:1794"``.
    """
    numeric = highlight_id.split(":", 1)[-1]
    return f"{HIGHLIGHT_ID_PREFIX}{numeric}"


def reels_items(payload: Any, reel_id: str) -> list[dict[str, Any]]:
    """Return the items of one reel from a ``reels_media`` response.

    The response is keyed on the full reel id -- a story response uses the
    numeric user id, a highlight response the ``"highlight:<id>"`` string.

    Args:
        payload: The decoded ``get_iphone_json`` response.
        reel_id: The key the requested reel is stored under.

    Returns:
        The reel's items, or an empty list when the response carries none.
    """
    if not isinstance(payload, dict):
        return []
    reels = payload.get("reels")
    if not isinstance(reels, dict):
        return []
    reel = reels.get(reel_id)
    if not isinstance(reel, dict):
        return []
    items = reel.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def highlight_tray(payload: Any) -> list[dict[str, Any]]:
    """Return the highlight entries from a highlights-tray response.

    The tray's own ``items`` list is empty on this endpoint; the items of each
    highlight are fetched separately.

    Args:
        payload: The decoded ``highlights_tray`` response.

    Returns:
        The tray entries, or an empty list when the response carries none.
    """
    if not isinstance(payload, dict):
        return []
    tray = payload.get("tray")
    if not isinstance(tray, list):
        return []
    return [entry for entry in tray if isinstance(entry, dict)]


def media_from_item(item: dict[str, Any]) -> list[MediaRef]:
    """Build the media reference of one story/highlight item.

    A video item's ``image_versions2`` is its poster frame, not a second media
    object -- the same rule the regular-post path follows. Only the video is
    emitted, exactly one reference per item.

    Args:
        item: One item from a ``reels_media`` response.

    Returns:
        A one-element list, or an empty list when the item carries no usable
        media.
    """
    media_id = _string_id(item.get("pk"))
    media_type = item.get("media_type")

    if media_type == _MEDIA_TYPE_VIDEO:
        ref = _video_ref(item, media_id=media_id)
    elif media_type == _MEDIA_TYPE_IMAGE:
        ref = _image_ref(item, media_id=media_id)
    elif item.get("video_versions"):
        # The type is missing or unexpected; trust the payload.
        ref = _video_ref(item, media_id=media_id)
    else:
        ref = _image_ref(item, media_id=media_id)

    if ref is None:
        return []
    return stamp_indices([ref])


def build_post(item: dict[str, Any], username: str, *, surface: str) -> Post:
    """Convert one story/highlight item into a :class:`Post`.

    Args:
        item: One item from a ``reels_media`` response.
        username: The account the item belongs to.
        surface: The surface the item came from, recorded in the metadata.

    Returns:
        The post, with its media reference stamped with its position.
    """
    pk = str(item["pk"])
    metadata: dict[str, Any] = {"surface": surface}
    expiring_at = item.get("expiring_at")
    if isinstance(expiring_at, int):
        metadata["expiring_at"] = expiring_at
    media_type = item.get("media_type")
    if isinstance(media_type, int):
        metadata["media_type"] = media_type

    return Post(
        platform="instagram",
        target=username,
        post_id=pk,
        shortcode=_string_or_none(item.get("code")),
        permalink=f"https://www.instagram.com/stories/{username}/{pk}/",
        caption=_caption(item),
        timestamp=_int_or_none(item.get("taken_at")),
        media=media_from_item(item),
        metadata=metadata,
    )


def _video_ref(item: dict[str, Any], *, media_id: str | None) -> MediaRef | None:
    """Return the item's video reference.

    Args:
        item: One story/highlight item.
        media_id: The item's own ``pk`` as a string, when known.

    Returns:
        The reference, or ``None`` when no usable video version is present.
    """
    versions = item.get("video_versions")
    if not isinstance(versions, list):
        return None
    best = _select_video_version(versions)
    if best is None:
        return None
    url = best["url"]
    return MediaRef(
        url=url,
        kind="video",
        width=_int_or_none(best.get("width")),
        height=_int_or_none(best.get("height")),
        mime="video/mp4",
        extension=extension_for("video/mp4", url),
        media_id=media_id,
    )


def _image_ref(item: dict[str, Any], *, media_id: str | None) -> MediaRef | None:
    """Return the item's largest image reference.

    Args:
        item: One story/highlight item.
        media_id: The item's own ``pk`` as a string, when known.

    Returns:
        The reference, or ``None`` when no usable image candidate is present.
    """
    image_versions = item.get("image_versions2")
    if not isinstance(image_versions, dict):
        return None
    candidates = image_versions.get("candidates")
    if not isinstance(candidates, list):
        return None
    best = _select_image_candidate(candidates)
    if best is None:
        return None
    url = best["url"]
    return MediaRef(
        url=url,
        kind="image",
        width=_int_or_none(best.get("width")),
        height=_int_or_none(best.get("height")),
        mime="image/jpeg",
        extension=extension_for("image/jpeg", url),
        media_id=media_id,
    )


def _select_video_version(versions: list[Any]) -> dict[str, Any] | None:
    """Pick the video version to download.

    ``type == 101`` is the video; the other entries are alternates that share
    its URL. When no 101 is present, the highest-bandwidth entry is the best
    available proxy for quality.

    Args:
        versions: The item's ``video_versions`` list.

    Returns:
        The chosen version, or ``None`` when none carries a URL.
    """
    playable = [entry for entry in versions if isinstance(entry, dict) and entry.get("url")]
    if not playable:
        return None
    typed = [entry for entry in playable if entry.get("type") == VIDEO_VERSION_TYPE]
    pool = typed or playable
    return max(pool, key=lambda entry: _bandwidth(entry))


def _select_image_candidate(candidates: list[Any]) -> dict[str, Any] | None:
    """Pick the largest image candidate by pixel count.

    Args:
        candidates: The item's ``image_versions2.candidates`` list.

    Returns:
        The chosen candidate, or ``None`` when none carries a URL.
    """
    usable = [entry for entry in candidates if isinstance(entry, dict) and entry.get("url")]
    if not usable:
        return None
    return max(usable, key=lambda entry: _pixels(entry))


def _bandwidth(entry: dict[str, Any]) -> int:
    """Return an entry's bandwidth as an integer, defaulting to zero."""
    return _int_or_none(entry.get("bandwidth")) or 0


def _pixels(entry: dict[str, Any]) -> int:
    """Return an entry's pixel count, defaulting to zero."""
    width = _int_or_none(entry.get("width")) or 0
    height = _int_or_none(entry.get("height")) or 0
    return width * height


def _int_or_none(value: Any) -> int | None:
    """Return ``value`` when it is an int (and not a bool), otherwise ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _string_or_none(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty string, otherwise ``None``."""
    if isinstance(value, str) and value:
        return value
    return None


def _string_id(value: Any) -> str | None:
    """Return a platform id as a string, or ``None`` when it is unusable."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _caption(item: dict[str, Any]) -> str | None:
    """Return the item's caption text, tolerating the dict envelope.

    Args:
        item: One story/highlight item.

    Returns:
        The caption text, or ``None`` when the item has none.
    """
    caption = item.get("caption")
    if isinstance(caption, str):
        return caption
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str):
            return text
    return None
