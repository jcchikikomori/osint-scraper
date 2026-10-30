"""Unit tests for the Instagram iPhone-API transport.

These tests exercise the pure conversion functions with fixture dicts that
mirror the item shape Instagram returns. No network is involved.
"""

from __future__ import annotations

from typing import Any

from osint_scraper.crawlers.instagram_api import (
    SURFACE_HIGHLIGHTS,
    SURFACE_STORIES,
    build_post,
    highlight_reel_id,
    highlight_tray,
    highlights_tray_path,
    media_from_item,
    reels_items,
    reels_media_path,
    stories_path,
)

_IMAGE_URL = "https://cdn.example.com/image_1080.jpg?stp=dst-jpg"
_SMALL_IMAGE_URL = "https://cdn.example.com/image_750.jpg?stp=dst-jpg&p750x750"
_VIDEO_URL = "https://cdn.example.com/video.mp4?bytestart=0"


def _image_item() -> dict[str, Any]:
    """Return a story item shaped like a verified image item."""
    return {
        "pk": 4019134249743017444,
        "code": "DfG1U5MieXk00pbuOlrdDYhH_ki692e1LLrNqo0",
        "taken_at": 1793338154,
        "caption": None,
        "media_type": 1,
        "expiring_at": 1793424554,
        "device_timestamp": 1793338155337242,
        "image_versions2": {
            "candidates": [
                {"url": _SMALL_IMAGE_URL, "width": 750, "height": 1334, "type": None},
                {"url": _IMAGE_URL, "width": 1080, "height": 1921, "type": None},
            ]
        },
    }


def _video_item(*, with_type_101: bool = True) -> dict[str, Any]:
    """Return a story item shaped like a verified video item.

    Args:
        with_type_101: Whether the ``video_versions`` list carries the entry
            with ``type == 101``.

    Returns:
        The fixture item.
    """
    versions: list[dict[str, Any]] = []
    if with_type_101:
        versions.append(
            {"url": _VIDEO_URL, "width": 720, "height": 1280, "type": 101, "bandwidth": 1_000_000}
        )
    versions.extend(
        [
            {"url": "https://cdn.example.com/alt_high.mp4", "width": 720, "height": 1280, "type": 102},
            {"url": "https://cdn.example.com/alt_low.mp4", "width": 720, "height": 1280, "type": 103},
        ]
    )
    return {
        "pk": 4019134249743017444,
        "code": "DfG1U5MieXk00pbuOlrdDYhH_ki692e1LLrNqo0",
        "taken_at": 1793338154,
        "caption": None,
        "media_type": 2,
        "expiring_at": 1793424554,
        "image_versions2": {
            "candidates": [
                {"url": "https://cdn.example.com/poster.jpg", "width": 1080, "height": 1921},
            ]
        },
        "video_versions": versions,
    }


def test_image_item_picks_the_largest_candidate() -> None:
    """An image item produces one image ref with the largest candidate's size."""
    media = media_from_item(_image_item())

    assert len(media) == 1
    ref = media[0]
    assert ref.kind == "image"
    assert ref.url == _IMAGE_URL
    assert (ref.width, ref.height) == (1080, 1921)
    assert ref.mime == "image/jpeg"
    assert ref.extension == ".jpg"
    assert ref.index == 0


def test_video_item_emits_the_video_not_the_poster() -> None:
    """A video item produces one video ref with the type-101 version."""
    media = media_from_item(_video_item())

    assert len(media) == 1
    ref = media[0]
    assert ref.kind == "video"
    assert ref.url == _VIDEO_URL
    assert (ref.width, ref.height) == (720, 1280)
    assert ref.mime == "video/mp4"
    assert ref.extension == ".mp4"
    assert all(media_ref.url != "https://cdn.example.com/poster.jpg" for media_ref in media)


def test_video_item_without_type_101_falls_back_to_highest_bandwidth() -> None:
    """No type-101 entry means the highest-bandwidth version wins."""
    item = _video_item(with_type_101=False)
    versions = item["video_versions"]
    assert isinstance(versions, list)
    versions[0]["bandwidth"] = 2_000_000
    versions[1]["bandwidth"] = 500_000

    media = media_from_item(item)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].url == "https://cdn.example.com/alt_high.mp4"


def test_item_without_media_versions_has_no_ref_and_does_not_raise() -> None:
    """An item with neither image nor video versions yields no ref."""
    item = {"pk": 1, "code": "abc", "media_type": 1}

    assert media_from_item(item) == []


def test_build_post_maps_the_story_fields() -> None:
    """The item-to-Post conversion carries the identity and the surface."""
    post = build_post(_video_item(), "somehandle", surface=SURFACE_STORIES)

    assert post.platform == "instagram"
    assert post.target == "somehandle"
    assert post.post_id == "4019134249743017444"
    assert post.shortcode == "DfG1U5MieXk00pbuOlrdDYhH_ki692e1LLrNqo0"
    assert post.timestamp == 1793338154
    assert post.caption is None
    assert post.permalink == ("https://www.instagram.com/stories/somehandle/4019134249743017444/")
    assert post.metadata["surface"] == SURFACE_STORIES
    assert post.metadata["expiring_at"] == 1793424554
    assert len(post.media) == 1
    assert post.media[0].kind == "video"


def test_build_post_records_the_highlight_surface() -> None:
    """A highlight item is labelled with its own surface."""
    post = build_post(_image_item(), "somehandle", surface=SURFACE_HIGHLIGHTS)

    assert post.metadata["surface"] == SURFACE_HIGHLIGHTS


def test_reel_paths_and_envelopes() -> None:
    """The request paths and response envelopes match Instagram's shape."""
    assert stories_path("123") == "api/v1/feed/reels_media/?reel_ids=123"
    assert highlights_tray_path("123") == "api/v1/highlights/123/highlights_tray/"
    assert highlight_reel_id("highlight:17945569111203835") == "highlight:17945569111203835"
    assert highlight_reel_id("17945569111203835") == "highlight:17945569111203835"

    tray_payload = {"tray": [{"id": "highlight:1", "title": "One", "media_count": 2}]}
    entries = highlight_tray(tray_payload)
    assert entries == tray_payload["tray"]

    reel_payload = {"reels": {"highlight:1": {"items": [{"pk": 1}, {"pk": 2}]}}}
    assert reels_items(reel_payload, "highlight:1") == [{"pk": 1}, {"pk": 2}]
    assert reels_items(reel_payload, "highlight:missing") == []
    assert reels_media_path("highlight:1") == "api/v1/feed/reels_media/?reel_ids=highlight:1"
