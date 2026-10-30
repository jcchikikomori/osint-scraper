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
from osint_scraper.crawlers.instagram import (
    SURFACE_HIGHLIGHTS,
    SURFACE_IGTV,
    SURFACE_POSTS,
    SURFACE_STORIES,
    SURFACE_TAGGED,
    InstagramCrawler,
    _normalize_target,
    parse_target,
)
from osint_scraper.exceptions import (
    AuthError,
    ParseError,
    RateLimitError,
    UnsupportedTargetError,
)
from osint_scraper.models import MediaRef, Post, ProfileInfo


@dataclass
class FakeSidecarNode:
    """Minimal instaloader PostSidecarNode stand-in."""

    display_url: str | None = None
    video_url: str | None = None
    is_video: bool = False
    index: int = 0
    node_id: str | None = None

    @property
    def _node(self) -> dict[str, Any]:
        """Return the child's GraphQL node with its own media id."""
        return {"id": self.node_id} if self.node_id is not None else {}


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
    carousel_media: list[dict[str, Any]] | None = None
    _full_metadata_dict: dict[str, Any] | None = None
    edge_sidecar_to_children: list[dict[str, Any]] | None = None

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
        if self.carousel_media is not None:
            node["carousel_media"] = self.carousel_media
        if self.edge_sidecar_to_children is not None:
            node["edge_sidecar_to_children"] = {"edges": self.edge_sidecar_to_children}
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
    tagged_posts: list[FakePost] = field(default_factory=list)
    igtv_posts: list[FakePost] = field(default_factory=list)
    userid: int = 123456789
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

    def get_tagged_posts(self) -> Iterator[FakePost]:
        """Yield the posts the target is tagged in."""
        yield from self.tagged_posts

    def get_igtv_posts(self) -> Iterator[FakePost]:
        """Yield the configured IGTV posts."""
        yield from self.igtv_posts


class FakeLoaderContext:
    """Context object attached to a fake Instaloader instance.

    It answers ``get_iphone_json`` from a per-path response map, so a test can
    state exactly which request it is serving.
    """

    def __init__(self, iphone_responses: dict[str, Any] | None = None) -> None:
        """Store the canned iPhone-API responses and start the call log."""
        self.is_logged_in = False
        self.iphone_support = True
        self.iphone_responses = dict(iphone_responses or {})
        self.iphone_calls: list[tuple[str, dict[str, Any]]] = []

    def get_iphone_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Record the request and return the matching canned response.

        Args:
            path: The iPhone-API path the crawler asked for.
            params: The query parameters the crawler asked for.

        Returns:
            The configured response for ``path``.

        Raises:
            AssertionError: When the path was not preconfigured.
        """
        self.iphone_calls.append((path, dict(params or {})))
        if path not in self.iphone_responses:
            raise AssertionError(f"unexpected iPhone API path: {path}")
        return self.iphone_responses[path]


class FakeInstaloaderClass:
    """Constructor called as ``instaloader.Instaloader()``."""

    instances: ClassVar[list[FakeInstaloaderClass]] = []
    iphone_responses: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.context = FakeLoaderContext(type(self).iphone_responses)
        self.kwargs = kwargs
        self.session: tuple[str, dict[str, str]] | None = None
        FakeInstaloaderClass.instances.append(self)

    def load_session(self, username: str, session_data: dict[str, str]) -> None:
        """Record the session the crawler injected."""
        self.session = (username, session_data)
        self.context.is_logged_in = True


def make_fake_instaloader_module(
    *,
    profile: FakeProfile | None = None,
    raise_on_profile: Exception | None = None,
    iphone_responses: dict[str, Any] | None = None,
) -> types.ModuleType:
    """Build a fake instaloader module for injection into the crawler.

    Args:
        profile: The profile the fake registry returns.
        raise_on_profile: An exception to raise while resolving the profile.
        iphone_responses: Canned responses keyed by iPhone-API path.

    Returns:
        The fake module.
    """
    FakeInstaloaderClass.iphone_responses = dict(iphone_responses or {})

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


class _ThrottledPost:
    """A post whose metadata read hits Instagram's account-level throttle.

    instaloader fetches a post's full metadata when its node is partial, and
    that request is throttled like any other.  The failure has to be
    translated, or it escapes as a raw instaloader exception and the cooldown
    that should stop the *next* run is never registered.
    """

    shortcode = "THROTTLED"
    typename = "GraphImage"
    is_video = False
    video_url = None
    caption = "throttled"
    date_utc = datetime(2024, 1, 1, tzinfo=UTC)
    _node: ClassVar[dict[str, Any]] = {}

    @property
    def owner_username(self) -> str:
        """Fail the way a throttled metadata request does."""
        raise RuntimeError(
            '400 Bad Request - "fail" status, message "feedback_required" '
            "when accessing https://www.instagram.com/graphql/query"
        )


async def test_a_throttle_while_building_a_post_is_translated(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A throttle during post enrichment must stop the run, not crash it."""
    fake_module = make_fake_instaloader_module(
        profile=FakeProfile(posts=[_ThrottledPost()]),  # type: ignore[list-item]
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    with pytest.raises(RateLimitError, match=r"feedback_required|throttled"):
        [post async for post in crawler.crawl("somehandle")]


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


async def test_sidecar_children_carry_index_and_media_id(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """Each carousel child records its position and its own GraphQL media id."""
    sidecar_post = FakePost(
        node_id="333",
        shortcode="CAR333",
        caption="Three children",
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(
                display_url="https://cdn.example.com/child1.jpg",
                is_video=False,
                node_id="9001",
            ),
            FakeSidecarNode(
                video_url="https://cdn.example.com/child2.mp4",
                is_video=True,
                node_id="9002",
            ),
            FakeSidecarNode(
                display_url="https://cdn.example.com/child3.jpg",
                is_video=False,
                node_id="9003",
            ),
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert len(media) == 3
    assert [ref.index for ref in media] == [0, 1, 2]
    assert [ref.media_id for ref in media] == ["9001", "9002", "9003"]
    # The row belongs to the carousel post, not to the children.
    assert results[0].post_id == "333"


async def test_sidecar_children_are_built_from_the_parent_carousel_media(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """The parent's carousel_media carries the children's real identity.

    instaloader's own sidecar objects expose only the URL and the video flag.
    The parent node's ``carousel_media`` list carries each child's media id,
    media type and dimensions, so that is what the crawler reads.
    """
    sidecar_post = FakePost(
        node_id="444",
        shortcode="CAR444",
        typename="GraphSidecar",
        carousel_media=[
            {
                "id": "4016734873234909654_787132",
                "pk": 4016734873234909654,
                "media_type": 1,
                "image_versions2": {
                    "candidates": [
                        {"url": "https://cdn.example.com/c1_small.jpg", "width": 640, "height": 800},
                        {"url": "https://cdn.example.com/c1.jpg", "width": 1080, "height": 1350},
                    ]
                },
            },
            {
                "id": "4016734895371392264_787132",
                "pk": 4016734895371392264,
                "media_type": 2,
                "video_versions": [
                    {"url": "https://cdn.example.com/c2.mp4", "width": 720, "height": 900},
                ],
            },
        ],
        sidecar_nodes=[
            # The thin objects instaloader hands back: URLs only, no identity.
            FakeSidecarNode(display_url="https://cdn.example.com/c1.jpg", is_video=False),
            FakeSidecarNode(video_url="https://cdn.example.com/c2.mp4", is_video=True),
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert len(media) == 2
    assert [ref.index for ref in media] == [0, 1]
    assert [ref.media_id for ref in media] == [
        "4016734873234909654_787132",
        "4016734895371392264_787132",
    ]
    assert [ref.kind for ref in media] == ["image", "video"]
    # The largest candidate wins, and the dimensions come with it.
    assert media[0].url == "https://cdn.example.com/c1.jpg"
    assert (media[0].width, media[0].height) == (1080, 1350)
    assert media[1].url == "https://cdn.example.com/c2.mp4"
    assert (media[1].width, media[1].height) == (720, 900)


async def test_sidecar_without_carousel_media_falls_back_to_the_sidecar_nodes(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A parent with no carousel metadata still yields one ref per child."""
    sidecar_post = FakePost(
        node_id="555",
        shortcode="CAR555",
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(display_url="https://cdn.example.com/f1.jpg", is_video=False),
            FakeSidecarNode(video_url="https://cdn.example.com/f2.mp4", is_video=True),
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert [ref.url for ref in media] == [
        "https://cdn.example.com/f1.jpg",
        "https://cdn.example.com/f2.mp4",
    ]
    assert [ref.kind for ref in media] == ["image", "video"]


async def test_sidecar_children_come_from_the_cached_full_metadata(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """Rich children are read from the metadata instaloader already fetched.

    instaloader fetches a post's full metadata when a carousel contains a
    video.  The crawler reads that cached copy instead of asking for it again,
    so the child's identity is available without an extra request.
    """
    sidecar_post = FakePost(
        node_id="666",
        shortcode="CAR666",
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(video_url="https://cdn.example.com/v1.mp4", is_video=True),
        ],
        _full_metadata_dict={
            "carousel_media": [
                {
                    "id": "4016734873234909654_787132",
                    "media_type": 2,
                    "video_versions": [
                        {"url": "https://cdn.example.com/v1.mp4", "width": 720, "height": 900},
                    ],
                },
            ],
        },
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert len(media) == 1
    assert media[0].media_id == "4016734873234909654_787132"
    assert media[0].kind == "video"
    assert (media[0].width, media[0].height) == (720, 900)


async def test_sidecar_falls_back_to_the_raw_edge_children(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """When instaloader cannot resolve the children, the raw edges are used.

    The raw edges carry the URL and the video flag but no identity, so the
    reference has no media id and no dimensions -- and the run still produces
    one reference per child.
    """

    class BrokenSidecar(FakePost):
        """A post whose sidecar resolution fails."""

        def get_sidecar_nodes(self) -> Iterator[FakeSidecarNode]:
            """Fail the way a throttled request would."""
            raise RuntimeError("feedback_required")

    post = BrokenSidecar(
        node_id="777",
        shortcode="CAR777",
        typename="GraphSidecar",
        edge_sidecar_to_children=[
            {"node": {"display_url": "https://cdn.example.com/e1.jpg", "is_video": False}},
            {"node": {"video_url": "https://cdn.example.com/e2.mp4", "is_video": True}},
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert [ref.url for ref in media] == [
        "https://cdn.example.com/e1.jpg",
        "https://cdn.example.com/e2.mp4",
    ]
    assert [ref.kind for ref in media] == ["image", "video"]
    assert [ref.media_id for ref in media] == [None, None]


async def test_sidecar_child_without_media_id_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A child whose node carries no id still produces a ref with media_id None."""
    sidecar_post = FakePost(
        node_id="444",
        shortcode="CAR444",
        typename="GraphSidecar",
        sidecar_nodes=[
            FakeSidecarNode(display_url="https://cdn.example.com/noid.jpg", is_video=False),
        ],
    )
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=[sidecar_post]))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle")]

    media = results[0].media
    assert len(media) == 1
    assert media[0].index == 0
    assert media[0].media_id is None


async def test_single_image_records_its_own_media_id(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """A single-image post uses its own media id and position zero."""
    fake_module = make_fake_instaloader_module(profile=FakeProfile(posts=_sample_posts()))
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("somehandle", limit=1)]

    media = results[0].media
    assert len(media) == 1
    assert media[0].index == 0
    assert media[0].media_id == "1234567890"


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


def _authenticated_crawler(
    cookies: dict[str, str] | None = None,
) -> InstagramCrawler:
    """Return a crawler wired to a context holding an Instagram session.

    Args:
        cookies: The session cookies to expose. Defaults to a complete set.

    Returns:
        The crawler.
    """
    session = cookies or {
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
    return InstagramCrawler(ctx)


def _story_item(pk: int, *, media_type: int = 1) -> dict[str, Any]:
    """Return an iPhone-API story item.

    Args:
        pk: The item's media id.
        media_type: ``1`` for an image, ``2`` for a video.

    Returns:
        The item.
    """
    item: dict[str, Any] = {
        "pk": pk,
        "code": f"CODE{pk}",
        "taken_at": 1793338154,
        "caption": None,
        "media_type": media_type,
        "expiring_at": 1793424554,
    }
    if media_type == 2:
        item["video_versions"] = [
            {
                "url": f"https://cdn.example.com/{pk}.mp4",
                "width": 720,
                "height": 1280,
                "type": 101,
                "bandwidth": 2_000_000,
            }
        ]
        item["image_versions2"] = {
            "candidates": [{"url": f"https://cdn.example.com/{pk}_poster.jpg", "width": 720, "height": 1280}]
        }
    else:
        item["image_versions2"] = {
            "candidates": [
                {"url": f"https://cdn.example.com/{pk}_small.jpg", "width": 640, "height": 1136},
                {"url": f"https://cdn.example.com/{pk}.jpg", "width": 1080, "height": 1921},
            ]
        }
    return item


@pytest.mark.parametrize(
    ("target", "username", "surface"),
    [
        ("somehandle", "somehandle", SURFACE_POSTS),
        ("https://www.instagram.com/somehandle/", "somehandle", SURFACE_POSTS),
        ("https://www.instagram.com/somehandle/tagged/", "somehandle", SURFACE_TAGGED),
        ("https://www.instagram.com/stories/somehandle/", "somehandle", SURFACE_STORIES),
        ("https://www.instagram.com/somehandle/highlights/", "somehandle", SURFACE_HIGHLIGHTS),
        ("https://www.instagram.com/somehandle/igtv/", "somehandle", SURFACE_IGTV),
    ],
)
def test_parse_target_maps_each_form_to_a_surface(
    target: str,
    username: str,
    surface: str,
) -> None:
    """Every recognised target form names the right account and surface."""
    parsed = parse_target(target)

    assert parsed.username == username
    assert parsed.surface == surface


def test_parse_target_rejects_a_non_instagram_url() -> None:
    """A URL on another host is not an Instagram target."""
    with pytest.raises(UnsupportedTargetError):
        parse_target("https://www.threads.net/@somehandle")


def test_parse_target_rejects_a_post_url() -> None:
    """A post URL is not a profile target."""
    with pytest.raises(UnsupportedTargetError):
        parse_target("https://www.instagram.com/p/ABC123/")


async def test_tagged_surface_yields_the_tagged_posts(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """The tagged surface yields the posts the fake profile reports."""
    tagged = FakePost(
        node_id="777",
        shortcode="TAG777",
        caption="Tagged in",
        date_utc=datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
        image_versions2={
            "candidates": [{"url": "https://cdn.example.com/tagged.jpg", "width": 1080, "height": 1350}]
        },
    )
    fake_module = make_fake_instaloader_module(
        profile=FakeProfile(tagged_posts=[tagged]),
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    results = [post async for post in crawler.crawl("https://www.instagram.com/somehandle/tagged/")]

    assert len(results) == 1
    assert results[0].post_id == "777"
    assert results[0].metadata["surface"] == SURFACE_TAGGED


async def test_stories_surface_yields_one_post_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stories surface converts every item in the reel response."""
    items = [_story_item(101), _story_item(102, media_type=2)]
    responses = {"api/v1/feed/reels_media/?reel_ids=123456789": {"reels": {"123456789": {"items": items}}}}
    fake_module = make_fake_instaloader_module(iphone_responses=responses)
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )
    crawler = _authenticated_crawler()

    results = [post async for post in crawler.crawl("https://www.instagram.com/stories/somehandle/")]

    assert [post.post_id for post in results] == ["101", "102"]
    assert results[0].media[0].kind == "image"
    assert results[1].media[0].kind == "video"
    assert results[0].permalink == "https://www.instagram.com/stories/somehandle/101/"
    assert all(post.metadata["surface"] == SURFACE_STORIES for post in results)
    assert results[0].metadata["expiring_at"] == 1793424554

    context = FakeInstaloaderClass.instances[-1].context
    assert [path for path, _params in context.iphone_calls] == ["api/v1/feed/reels_media/?reel_ids=123456789"]


async def test_highlights_surface_calls_the_tray_then_each_highlight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Highlights fetch the tray, then one reel per highlight."""
    tray_path = "api/v1/highlights/123456789/highlights_tray/"
    first_path = "api/v1/feed/reels_media/?reel_ids=highlight:111"
    second_path = "api/v1/feed/reels_media/?reel_ids=highlight:222"
    responses = {
        tray_path: {
            "tray": [
                {"id": "highlight:111", "title": "One", "media_count": 2},
                {"id": "highlight:222", "title": "Two", "media_count": 1},
            ]
        },
        first_path: {"reels": {"highlight:111": {"items": [_story_item(201), _story_item(202)]}}},
        second_path: {"reels": {"highlight:222": {"items": [_story_item(203, media_type=2)]}}},
    }
    fake_module = make_fake_instaloader_module(iphone_responses=responses)
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )
    crawler = _authenticated_crawler()

    results = [post async for post in crawler.crawl("https://www.instagram.com/somehandle/highlights/")]

    assert [post.post_id for post in results] == ["201", "202", "203"]
    assert all(post.metadata["surface"] == SURFACE_HIGHLIGHTS for post in results)
    assert [post.metadata["highlight_id"] for post in results] == [
        "highlight:111",
        "highlight:111",
        "highlight:222",
    ]

    context = FakeInstaloaderClass.instances[-1].context
    assert [path for path, _params in context.iphone_calls] == [
        tray_path,
        first_path,
        second_path,
    ]


async def test_highlights_without_a_session_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """The iPhone API needs a session; an anonymous crawl says how to get one."""
    fake_module = make_fake_instaloader_module(
        profile=FakeProfile(),
        iphone_responses={
            "api/v1/highlights/123456789/highlights_tray/": {"tray": []},
        },
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    with pytest.raises(AuthError) as exc_info:
        async for _post in crawler.crawl("https://www.instagram.com/somehandle/highlights/"):
            pass  # pragma: no cover - should raise before first yield

    assert "Log in to instagram.com" in str(exc_info.value)
    assert "--cookies" in str(exc_info.value)
    assert FakeInstaloaderClass.instances[-1].context.iphone_calls == []


async def test_limit_bounds_the_stories_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit`` stops the stories surface after that many items."""
    items = [_story_item(301), _story_item(302), _story_item(303)]
    fake_module = make_fake_instaloader_module(
        iphone_responses={
            "api/v1/feed/reels_media/?reel_ids=123456789": {"reels": {"123456789": {"items": items}}}
        }
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )
    crawler = _authenticated_crawler()

    results = [post async for post in crawler.crawl("https://www.instagram.com/stories/somehandle/", limit=1)]

    assert [post.post_id for post in results] == ["301"]


async def test_limit_bounds_the_highlights_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit`` bounds the total yield across every highlight."""
    fake_module = make_fake_instaloader_module(
        iphone_responses={
            "api/v1/highlights/123456789/highlights_tray/": {
                "tray": [{"id": "highlight:111", "title": "One", "media_count": 2}]
            },
            "api/v1/feed/reels_media/?reel_ids=highlight:111": {
                "reels": {"highlight:111": {"items": [_story_item(401), _story_item(402)]}}
            },
        }
    )
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )
    crawler = _authenticated_crawler()

    results = [
        post async for post in crawler.crawl("https://www.instagram.com/somehandle/highlights/", limit=1)
    ]

    assert [post.post_id for post in results] == ["401"]


async def test_igtv_upstream_failure_is_a_parse_error_not_a_key_error(
    monkeypatch: pytest.MonkeyPatch,
    crawler: InstagramCrawler,
) -> None:
    """instaloader's ``KeyError: 'edges'`` is reported as a domain error."""

    class BrokenIgtvProfile(FakeProfile):
        """A profile whose IGTV reader fails the way upstream currently does."""

        def get_igtv_posts(self) -> Iterator[FakePost]:
            """Fail with the upstream ``KeyError`` during iteration."""
            raise KeyError("edges")
            yield  # pragma: no cover - makes this a generator

    fake_module = make_fake_instaloader_module(profile=BrokenIgtvProfile())
    monkeypatch.setattr(
        "osint_scraper.crawlers.instagram._load_instaloader",
        lambda: fake_module,
    )

    with pytest.raises(ParseError) as exc_info:
        async for _post in crawler.crawl("https://www.instagram.com/somehandle/igtv/"):
            pass  # pragma: no cover - should raise before first yield

    message = str(exc_info.value)
    assert "broken upstream" in message
    assert "profile feed" in message
    assert not isinstance(exc_info.value, KeyError)
