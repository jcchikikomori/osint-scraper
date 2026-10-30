"""Tests for :mod:`osint_scraper.downloader`.

All HTTP traffic is handled by :class:`httpx.MockTransport`; no real network
requests are made.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx

from osint_scraper.config.settings import DownloadSettings
from osint_scraper.downloader import Downloader
from osint_scraper.models import MediaRef, Post

LOGGER = logging.getLogger("test_downloader")
LOGGER.addHandler(logging.NullHandler())


def _settings(**overrides: object) -> DownloadSettings:
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


async def test_downloads_one_image(tmp_path: Path) -> None:
    content = b"fake-image-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image.jpg":
            return httpx.Response(200, content=content, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg")])

    try:
        count = await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    assert count == 1
    assert len(downloader.results) == 1

    result = downloader.results[0]
    assert result.url == "https://example.com/image.jpg"
    assert result.size == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.ok
    assert result.error is None

    expected_dir = tmp_path / "instagram" / "test_user"
    assert result.path.parent == expected_dir
    assert result.path.exists()
    assert result.path.read_bytes() == content
    # Filename uses the UTC timestamp and shortcode.
    assert result.path.name.startswith("2024-01-01_00-00-00_ABC123")


async def test_downloads_two_media_files(tmp_path: Path) -> None:
    first = b"first-image"
    second = b"second-video"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/one.jpg":
            return httpx.Response(200, content=first)
        if request.url.path == "/two.mp4":
            return httpx.Response(200, content=second)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(), LOGGER, client=client)
    post = _post(
        [
            MediaRef(url="https://example.com/one.jpg", kind="image", mime="image/jpeg"),
            MediaRef(url="https://example.com/two.mp4", kind="video", mime="video/mp4"),
        ]
    )

    try:
        count = await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    assert count == 2
    assert len(downloader.results) == 2
    assert all(result.ok for result in downloader.results)
    assert downloader.results[0].path.exists()
    assert downloader.results[1].path.exists()


async def test_404_records_failure_without_raising(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/missing.jpg", kind="image")])

    try:
        count = await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    assert count == 0
    assert len(downloader.results) == 1
    result = downloader.results[0]
    assert not result.ok
    assert result.error is not None
    assert "404" in result.error
    assert not result.path.exists()


async def test_500_retried_then_succeeds(tmp_path: Path) -> None:
    content = b"retried-content"
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(retries=1, delay=0.0, jitter=0.0), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/retry.jpg", kind="image")])

    try:
        count = await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    assert attempts["count"] == 2
    assert count == 1
    assert len(downloader.results) == 1
    result = downloader.results[0]
    assert result.ok
    assert result.path.read_bytes() == content


async def test_min_size_discards_small_file(tmp_path: Path) -> None:
    content = b"tiny"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(min_size=100), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/tiny.jpg", kind="image")])

    try:
        count = await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    assert count == 0
    assert len(downloader.results) == 1
    result = downloader.results[0]
    assert not result.ok
    assert result.error is not None
    assert "min_size" in result.error or "small" in result.error
    assert not result.path.exists()
    assert not list(result.path.parent.glob("*.part"))


async def test_without_resume_an_existing_file_is_not_clobbered(tmp_path: Path) -> None:
    """Without the resume machinery the old non-clobbering behaviour applies."""
    original = b"original"
    new_content = b"new-content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=new_content)

    expected_dir = tmp_path / "instagram" / "test_user"
    expected_dir.mkdir(parents=True)
    existing = expected_dir / "2024-01-01_00-00-00_ABC123.jpg"
    existing.write_bytes(original)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(overwrite=False), LOGGER, client=client, resume=False)
    post = _post([MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg")])

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    result = downloader.results[0]
    assert result.ok
    assert result.path.name != existing.name
    assert "_1" in result.path.name
    assert existing.read_bytes() == original
    assert result.path.read_bytes() == new_content


async def test_resume_uses_a_stable_deterministic_path(tmp_path: Path) -> None:
    """With resume on the path must not change between runs."""
    content = b"image"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    expected_dir = tmp_path / "instagram" / "test_user"
    expected_dir.mkdir(parents=True)
    existing = expected_dir / "2024-01-01_00-00-00_ABC123.jpg"
    existing.write_bytes(content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(overwrite=False), LOGGER, client=client, resume=True)
    post = _post([MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg")])

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    result = downloader.results[0]
    assert result.path == existing
    # No second copy was created.
    assert sorted(p.name for p in expected_dir.iterdir()) == ["2024-01-01_00-00-00_ABC123.jpg"]


async def test_overwrite_true_replaces_existing_file(tmp_path: Path) -> None:
    original = b"original"
    new_content = b"new-content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=new_content)

    expected_dir = tmp_path / "instagram" / "test_user"
    expected_dir.mkdir(parents=True)
    existing = expected_dir / "2024-01-01_00-00-00_ABC123.jpg"
    existing.write_bytes(original)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(overwrite=True), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg")])

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    result = downloader.results[0]
    assert result.ok
    assert result.path == existing
    assert existing.read_bytes() == new_content


async def test_save_metadata_writes_sidecar(tmp_path: Path) -> None:
    content = b"image-with-meta"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(save_metadata=True), LOGGER, client=client)
    post = _post([MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg")])

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    result = downloader.results[0]
    sidecar = result.path.with_suffix(".json")
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["post_id"] == "1234567890"
    assert payload["files"][0]["url"] == result.url
    assert payload["files"][0]["sha256"] == result.sha256
    assert payload["files"][0]["size"] == result.size


async def test_sidecar_is_named_after_the_post(tmp_path: Path) -> None:
    """The sidecar name must not depend on which download finishes first."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The first media item is deliberately the slowest response.
        if request.url.path == "/first.jpg":
            return httpx.Response(200, content=b"slow")
        return httpx.Response(200, content=b"fast")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(save_metadata=True), LOGGER, client=client)
    post = _post(
        [
            MediaRef(url="https://example.com/first.jpg", kind="image", mime="image/jpeg"),
            MediaRef(url="https://example.com/second.jpg", kind="image", mime="image/jpeg"),
        ]
    )

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    files = sorted(path.name for path in tmp_path.rglob("*") if path.is_file())  # noqa: ASYNC240
    assert files == [
        "2024-01-01_00-00-00_ABC123.json",
        "2024-01-01_00-00-00_ABC123_1.jpg",
        "2024-01-01_00-00-00_ABC123_2.jpg",
    ]
    # Results are reported in media order, not completion order.
    assert [result.url for result in downloader.results] == [
        "https://example.com/first.jpg",
        "https://example.com/second.jpg",
    ]


async def test_destination_path_includes_platform_and_target(tmp_path: Path) -> None:
    content = b"data"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    downloader = Downloader(_settings(), LOGGER, client=client)
    post = _post(
        [
            MediaRef(url="https://example.com/image.jpg", kind="image", mime="image/jpeg"),
        ]
    )

    try:
        await downloader.download_post(post, tmp_path)
    finally:
        await client.aclose()

    result = downloader.results[0]
    assert result.path.parts[-3:] == ("instagram", "test_user", result.path.name)
