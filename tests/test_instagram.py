"""Tests for the Instagram crawler.

These tests never hit the network. They monkeypatch the instaloader module
lookup so the crawler operates against lightweight fakes.
"""

from __future__ import annotations

import logging
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from osint_scraper.config.settings import Settings
from osint_scraper.crawlers.base import CrawlContext
from osint_scraper.crawlers.instagram import InstagramCrawler, _normalize_target
from osint_scraper.exceptions import AuthError, UnsupportedTargetError
from osint_scraper.models import MediaRef, Post, ProfileInfo


@dataclass
class FakeSidecarNode:
    """Minimal instaloader PostSidecarNode stand-in."""

    display_url: str | None = None
    video_url: str | None = None
    is_video: bool = False
    index: int = 0


@dataclass
class FakePost:
    """Minimal instaloader 4.16 Post stand-in."""

    node_id: str
    shortcode: str
    caption: str | None = None
    date_utc: datetime | None = None
    typename: str = "GraphImage"
    is_video: bool = False
    video_url: str | None = None
    image_versions2: dict[str, Any] | None = None
    video_versions: list[dict[str, Any]] | None = None
    display_url: str | None = None
    likes: int | None = None
    comments: int | None = None
    owner_username: str | None = "fakeuser"
    sidecar_nodes: list[FakeSidecarNode] = field(default_factory=list)

    @property
    def _node(self) -> dict[str, Any]:
        """Return the internal GraphQL node dict."""
        node: dict[str, Any] = {
            "id": self.node_id,
            "__typename": self.typename,
            "is_video": self.is_video,
        }
        if self.image_versions2 is not None:
            node["image_versions2"] = self.image_versions2
        if self.video_versions is not None:
            node["video_versions"] = self.video_versions
        if self.display_url is not None:
            node["display_url"] = self.display_url
        if self.video_url is not None:
            node["video_url"] = self.video_url
        if self.likes is not None:
            node["edge_media_preview_like"] = {"count": self.likes}
        if self.comments is not None:
            node["comment_count"] = self.comments
        return node

    @property
    def url(self) -> str:
        """Return the canonical post URL."""
        return f"https://www.instagram.com/p/{self.shortcode}/"

    def get_sidecar_nodes(self) -> Iterator[FakeSidecarNode]:
        """Yield sidecar child nodes as instaloader would."""
        yield from self.sidecar_nodes


@dataclass
class FakeProfile:
    """Minimal instaloader Profile stand-in."""

    posts: list[FakePost] = field(default_factory=list)
    full_name: str | None = "Fake Name"
    followers: int | None = 100
    mediacount: int | None = 5
    biography: str | None = "Fake bio"
    is_verified: bool = False
    external_url: str | None = None
    business_category_name: str | None = None

    def get_posts(self) -> Iterator[FakePost]:
        """Yield the configured posts."""
        yield from self.posts


class FakeLoaderContext:
    """Context object attached to a fake Instaloader instance."""


class FakeInstaloaderClass:
    """Constructor called as ``instaloader.Instaloader()``."""

    instances: ClassVar[list[FakeInstaloaderClass]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.context = FakeLoaderContext()
        self.kwargs = kwargs
        self.session: tuple[str, dict[str, str]] | None = None
        FakeInstaloaderClass.instances.append(self)

    def load_session(self, username: str, session_data: dict[str, str]) -> None:
        """Record the session the crawler injected."""
        self.session = (username, session_data)


def make_fake_instaloader_module(
    *,
    profile: FakeProfile | None = None,
    raise_on_profile: Exception | None = None,
) -> types.ModuleType:
    """Build a fake instaloader module for injection into the crawler."""

    class FakeExceptions:
        class QueryReturnedNotFoundError(Exception):
            pass

        class LoginRequiredError(Exception):
            pass

    class FakeProfileClass:
        @staticmethod
        def from_username(context: Any, username: str) -> FakeProfile:
            if raise_on_profile is not None:
                raise raise_on_profile
            return profile or FakeProfile()

    return types.SimpleNamespace(
        Instaloader=FakeInstaloaderClass,
        Profile=FakeProfileClass,
        exceptions=FakeExceptions,
    )


class FakeCookieProvider:
    """Cookie provider returning a fixed mapping."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self._cookies = cookies or {}

    def cookies_for(self, domain: str) -> dict[str, str]:
        """Return the configured cookies."""
        return dict(self._cookies)

    def cookie_header(self, domain: str) -> str:
        """Return a ``Cookie`` header value."""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())


@pytest.fixture
def crawl_context() -> Iterator[CrawlContext]:
    """Provide a minimal crawl context for tests."""
    http = httpx.AsyncClient()
    ctx = CrawlContext(
        settings=Settings(),
        http=http,
        cookies=FakeCookieProvider(),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        output_dir=Path("/tmp"),
        user_agent="test",
    )
    yield ctx
    # httpx.AsyncClient.aclose is async; rely on garbage collection in tests.


@pytest.fixture
def crawler(crawl_context: CrawlContext) -> InstagramCrawler:
    """Return an InstagramCrawler wired to a test context."""
    return InstagramCrawler(crawl_context)


def _sample_posts() -> list[FakePost]:
    """Return two representative fake posts (image and video)."""
    return [
        FakePost(
            node_id="1234567890",
            shortcode="ABC123",
            caption="First post",
            date_utc=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            typename="GraphImage",
            is_video=False,
            image_versions2={
                "candidates": [
                    {"url": "https://cdn.example.com/photo1_1080.jpg", "width": 1080, "height": 1350},
                    {"url": "https://cdn.example.com/photo1_640.jpg", "width": 640, "height": 800},
                ]
            },
            display_url="https://cdn.example.com/photo1_display.jpg",
            likes=42,
            comments=7,
        ),
        FakePost(
            node_id="0987654321",
            shortcode="XYZ789",
            caption="Second post",
            date_utc=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            typename="GraphVideo",
            is_video=True,
            video_url="https://cdn.example.com/video1_1080.mp4",
            video_versions=[
                {"url": "https://cdn.example.com/video1_720.mp4", "width": 720, "height": 1280},
                {"url": "https://cdn.example.com/video1_480.mp4", "width": 480, "height": 854},
            ],
            image_versions2={
                "candidates": [
                    {"url": "https://cdn.example.com/video1_poster.jpg", "width": 1080, "height": 1920},
                ]
            },
            display_url="https://cdn.example.com/video1_display.jpg",
        ),
    ]


async def test_crawl_yields_posts(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """``crawl`` produces Post objects with expected fields and media refs."""
    posts = _sample_posts()
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=posts))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    assert len(results) == 2
    first, second = results
    assert isinstance(first, Post)
    assert first.platform == "instagram"
    assert first.target == "somehandle"
    assert first.post_id == "1234567890"
    assert first.shortcode == "ABC123"
    assert first.permalink == "https://www.instagram.com/p/ABC123/"
    assert first.caption == "First post"
    assert first.timestamp == int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
    assert len(first.media) == 1
    media = first.media[0]
    assert isinstance(media, MediaRef)
    assert media.url == "https://cdn.example.com/photo1_1080.jpg"
    assert media.width == 1080
    assert media.height == 1350
    assert media.kind == "image"
    assert media.mime == "image/jpeg"
    assert media.extension == ".jpg"
    assert first.metadata.get("like_count") == 42
    assert first.metadata.get("comment_count") == 7
    assert first.metadata.get("typename") == "GraphImage"

    assert second.post_id == "0987654321"
    assert second.shortcode == "XYZ789"
    assert len(second.media) == 1
    assert second.media[0].url == "https://cdn.example.com/video1_720.mp4"
    assert second.media[0].width == 720
    assert second.media[0].height == 1280
    assert second.media[0].kind == "video"
    assert second.media[0].mime == "video/mp4"
    assert second.metadata.get("is_video") is True
    assert second.metadata.get("typename") == "GraphVideo"


async def test_video_post_yields_single_video_not_poster(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A video post emits exactly one video MediaRef; the poster frame is skipped."""
    video_post = FakePost(
        node_id="111",
        shortcode="VID111",
        caption="Video post",
        typename="GraphVideo",
        is_video=True,
        video_url="https://cdn.example.com/video_main.mp4",
        video_versions=[
            {"url": "https://cdn.example.com/video_main.mp4", "width": 1080, "height": 1920},
        ],
        image_versions2={
            "candidates": [
                {"url": "https://cdn.example.com/poster.jpg", "width": 1080, "height": 1920},
            ]
        },
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[video_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    assert len(results) == 1
    media = results[0].media
    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].url == "https://cdn.example.com/video_main.mp4"
    assert media[0].mime == "video/mp4"
    assert all(ref.kind != "image" for ref in media)


async def test_sidecar_post_yields_one_ref_per_child(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A sidecar post emits one MediaRef per child with the correct kind."""
    sidecar_post = FakePost(
        node_id="222",
        shortcode="CAR222",
        caption="Carousel post",
        typename="GraphSidecar",
        is_video=False,
        sidecar_nodes=[
            FakeSidecarNode(
                display_url="https://cdn.example.com/child1.jpg",
                is_video=False,
                index=0,
            ),
            FakeSidecarNode(
                video_url="https://cdn.example.com/child2.mp4",
                is_video=True,
                index=1,
            ),
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    assert len(results) == 1
    media = results[0].media
    assert len(media) == 2
    assert media[0].kind == "image"
    assert media[0].url == "https://cdn.example.com/child1.jpg"
    assert media[1].kind == "video"
    assert media[1].url == "https://cdn.example.com/child2.mp4"


async def test_crawl_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """``limit`` stops the crawl early without fetching extra posts."""
    posts = _sample_posts()
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=posts))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle", limit=1)]

    assert len(results) == 1
    assert results[0].shortcode == "ABC123"


async def test_crawl_no_posts_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile with zero posts yields nothing and logs a warning."""
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    with caplog.at_level(logging.WARNING):
        results = [post async for post in crawler.crawl("somehandle")]

    assert results == []
    assert "has no posts" in caplog.text


async def test_crawl_login_wall_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A login-wall exception is surfaced as an actionable AuthError."""
    fake_module = make_fake_instaloader_module(profile=FakeProfile())
    exception_class = fake_module.exceptions.QueryReturnedNotFoundError
    fake_module_with_error = make_fake_instaloader_module(
        profile=FakeProfile(),
        raise_on_profile=exception_class("user not found"),
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module_with_error,
    )

    with pytest.raises(AuthError) as exc_info:
        async for _post in crawler.crawl("somehandle"):
            pass  # pragma: no cover - should raise before first yield

    assert "Log in to instagram.com" in str(exc_info.value)
    assert "--cookies" in str(exc_info.value)


async def test_profile_maps_fields(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """``profile`` returns a ProfileInfo populated from the fake profile."""
    profile = FakeProfile(
        full_name="National Geographic",
        followers=1_000_000,
        mediacount=42,
        biography="Explore the world",
        is_verified=True,
        external_url="https://www.somehandle.com",
        business_category_name="Media",
    )
    fake_module = make_fake_instaloader_module(profile=profile)
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    info = await crawler.profile("somehandle")

    assert isinstance(info, ProfileInfo)
    assert info.platform == "instagram"
    assert info.target == "somehandle"
    assert info.display_name == "National Geographic"
    assert info.follower_count == 1_000_000
    assert info.post_count == 42
    assert info.biography == "Explore the world"
    assert info.metadata.get("is_verified") is True
    assert info.metadata.get("external_url") == "https://www.somehandle.com"
    assert info.metadata.get("business_category") == "Media"


async def test_target_normalisation(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """Bare usernames and profile URLs both normalise to the handle."""
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=_sample_posts()))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    for target in ("somehandle", "@somehandle", "https://www.instagram.com/somehandle/"):
        results = [post async for post in crawler.crawl(target)]
        assert results, f"target {target!r} should resolve"
        assert results[0].target == "somehandle"


async def test_invalid_target_raises(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """Non-handle targets raise UnsupportedTargetError."""
    fake_module = make_fake_instaloader_module(profile=FakeProfile())
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    invalid_targets = [
        "",
        "https://www.instagram.com/p/ABC123/",
        "not a valid user!",
    ]
    for target in invalid_targets:
        with pytest.raises(UnsupportedTargetError):
            async for _post in crawler.crawl(target):
                pass  # pragma: no cover - should raise before first yield


def test_normalize_target_bare_and_url() -> None:
    """``_normalize_target`` extracts usernames from varied inputs."""
    assert _normalize_target("somehandle") == "somehandle"
    assert _normalize_target("@somehandle") == "somehandle"
    assert _normalize_target("https://www.instagram.com/somehandle/") == "somehandle"
    assert _normalize_target("http://instagram.com/somehandle") == "somehandle"


def test_normalize_target_rejects_invalid() -> None:
    """``_normalize_target`` rejects post URLs and malformed handles."""
    with pytest.raises(UnsupportedTargetError):
        _normalize_target("")
    with pytest.raises(UnsupportedTargetError):
        _normalize_target("https://www.instagram.com/p/ABC123/")
    with pytest.raises(UnsupportedTargetError):
        _normalize_target("bad name!")


async def test_authenticated_session_is_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser session must be handed to instaloader, not just to the HTTP client."""
    FakeInstaloaderClass.instances.clear()
    fake = make_fake_instaloader_module(profile=FakeProfile())
    monkeypatch.setattr("osint_scraper.crawlers.instagram._load_instaloader", lambda: fake)

    session = {
        "sessionid": "sess-value",
        "csrftoken": "csrf-value",
        "ds_user_id": "123456789",
    }
    ctx = CrawlContext(
        settings=Settings(),
        http=httpx.AsyncClient(),
        cookies=FakeCookieProvider(session),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        output_dir=Path("/tmp"),
        user_agent="test",
    )
    crawler = InstagramCrawler(ctx)

    async for _post in crawler.crawl("somehandle"):
        pass

    assert FakeInstaloaderClass.instances, "no instaloader instance was created"
    injected = FakeInstaloaderClass.instances[-1].session
    assert injected is not None, "the session was not handed to instaloader"
    username, data = injected
    assert username == "123456789"
    assert data["sessionid"] == "sess-value"


async def test_anonymous_crawl_injects_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without cookies the crawler must stay anonymous."""
    FakeInstaloaderClass.instances.clear()
    fake = make_fake_instaloader_module(profile=FakeProfile())
    monkeypatch.setattr("osint_scraper.crawlers.instagram._load_instaloader", lambda: fake)

    ctx = CrawlContext(
        settings=Settings(),
        http=httpx.AsyncClient(),
        cookies=FakeCookieProvider(),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        output_dir=Path("/tmp"),
        user_agent="test",
    )
    crawler = InstagramCrawler(ctx)

    async for _post in crawler.crawl("somehandle"):
        pass

    assert FakeInstaloaderClass.instances
    assert FakeInstaloaderClass.instances[-1].session is None


async def test_session_without_csrf_token_is_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """instaloader needs a CSRF token; without one the session must be skipped."""
    FakeInstaloaderClass.instances.clear()
    fake = make_fake_instaloader_module(profile=FakeProfile())
    monkeypatch.setattr("osint_scraper.crawlers.instagram._load_instaloader", lambda: fake)

    ctx = CrawlContext(
        settings=Settings(),
        http=httpx.AsyncClient(),
        cookies=FakeCookieProvider({"sessionid": "sess-value"}),  # type: ignore[arg-type]
        logger=logging.getLogger("test"),
        output_dir=Path("/tmp"),
        user_agent="test",
    )
    crawler = InstagramCrawler(ctx)

    async for _post in crawler.crawl("somehandle"):
        pass

    assert FakeInstaloaderClass.instances
    assert FakeInstaloaderClass.instances[-1].session is None
