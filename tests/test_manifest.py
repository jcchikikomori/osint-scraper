"""Tests for the JSONL manifest writer.

The manifest has three row kinds. A post with media produces one ``"post"``
row followed by one ``"media"`` row per media object; the ``"file"`` rows are
written later by the CLI once the downloader has fetched the object. These
tests exercise the writer directly and never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from osint_scraper.manifest import ManifestWriter
from osint_scraper.models import MediaRef, Post


def _carousel_post() -> Post:
    """Return a three-child carousel post with fully described media."""
    return Post(
        platform="instagram",
        target="somehandle",
        post_id="4011374380604567319",
        shortcode="DerQ775",
        permalink="https://www.instagram.com/p/DerQ775/",
        caption="Three children",
        timestamp=1700000000,
        media=[
            MediaRef(
                url="https://cdn.example.com/child_1.jpg",
                kind="image",
                width=1080,
                height=1350,
                mime="image/jpeg",
                extension=".jpg",
                index=0,
                media_id="111",
            ),
            MediaRef(
                url="https://cdn.example.com/child_2.mp4",
                kind="video",
                width=720,
                height=1280,
                mime="video/mp4",
                extension=".mp4",
                index=1,
                media_id="222",
            ),
            MediaRef(
                url="https://cdn.example.com/child_3.jpg",
                kind="image",
                width=640,
                height=800,
                mime="image/jpeg",
                extension=".jpg",
                index=2,
                media_id="333",
            ),
        ],
    )


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    """Parse every JSON line written to ``path``."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_carousel_writes_post_then_media_rows(tmp_path: Path) -> None:
    """A post with three refs emits one post row and three media rows in order."""
    path = tmp_path / "manifest.jsonl"
    post = _carousel_post()

    with ManifestWriter(path) as writer:
        writer.write_post(post)

    entries = _read_manifest(path)
    assert [entry["kind"] for entry in entries] == ["post", "media", "media", "media"]
    assert [entry["metadata"]["index"] for entry in entries[1:]] == [0, 1, 2]


def test_media_rows_carry_the_media_identity(tmp_path: Path) -> None:
    """A media row describes the object, not the file that may be downloaded."""
    path = tmp_path / "manifest.jsonl"
    post = _carousel_post()

    with ManifestWriter(path) as writer:
        writer.write_post(post)

    entries = _read_manifest(path)
    first, second, third = entries[1:]
    for entry in (first, second, third):
        assert entry["kind"] == "media"
        assert entry["platform"] == "instagram"
        assert entry["target"] == "somehandle"
        assert entry["post_id"] == post.post_id
        assert entry["metadata"]["shortcode"] == "DerQ775"
        assert entry["timestamp"] == post.timestamp

    assert first["url"] == "https://cdn.example.com/child_1.jpg"
    assert first["metadata"] == {
        "index": 0,
        "media_id": "111",
        "kind": "image",
        "width": 1080,
        "height": 1350,
        "mime": "image/jpeg",
        "extension": ".jpg",
        "shortcode": "DerQ775",
    }
    assert second["metadata"]["kind"] == "video"
    assert second["metadata"]["media_id"] == "222"
    assert second["metadata"]["width"] == 720
    assert second["metadata"]["mime"] == "video/mp4"
    assert third["metadata"]["index"] == 2
    assert third["metadata"]["media_id"] == "333"


def test_media_rows_leave_file_fields_empty(tmp_path: Path) -> None:
    """The path, digest and size belong to the file row written after download."""
    path = tmp_path / "manifest.jsonl"

    with ManifestWriter(path) as writer:
        writer.write_post(_carousel_post())

    for entry in _read_manifest(path)[1:]:
        assert entry["path"] is None
        assert entry["sha256"] is None
        assert entry["size"] is None


def test_text_only_post_writes_no_media_rows(tmp_path: Path) -> None:
    """A post without media produces the post row and nothing else."""
    path = tmp_path / "manifest.jsonl"
    post = Post(
        platform="threads",
        target="somehandle",
        post_id="999",
        shortcode="TextOnly",
        caption="Just words",
        timestamp=1700000100,
    )

    with ManifestWriter(path) as writer:
        writer.write_post(post)

    entries = _read_manifest(path)
    assert len(entries) == 1
    assert entries[0]["kind"] == "post"
    assert entries[0]["metadata"]["media_count"] == 0


def test_manifest_on_disk_is_valid_jsonl(tmp_path: Path) -> None:
    """Every written line parses and carries the documented top-level keys."""
    path = tmp_path / "manifest.jsonl"

    with ManifestWriter(path) as writer:
        writer.write_post(_carousel_post())

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 4
    expected_keys = {
        "kind",
        "platform",
        "target",
        "post_id",
        "url",
        "path",
        "sha256",
        "size",
        "timestamp",
        "metadata",
    }
    for line in raw_lines:
        assert set(json.loads(line)) == expected_keys
