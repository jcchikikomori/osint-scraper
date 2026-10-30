"""Unit tests for the Threads crawler.

All network I/O is mocked with ``httpx.MockTransport`` so the tests run
offline against a realistic HTML fixture that mirrors Threads' current
SvelteKit/Comet envelope.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from osint_scraper.config.settings import Settings
from osint_scraper.crawlers.base import CrawlContext
from osint_scraper.crawlers.threads import ThreadsCrawler, normalize_target, parse_target
from osint_scraper.exceptions import ParseError, UnsupportedTargetError
from osint_scraper.models import ProfileInfo


def _make_posts_envelope() -> dict[str, object]:
    """Return the envelope that carries the fake Threads post objects."""
    user = {
        "username": "testuser",
        "pk": "user_1",
        "full_name": "Test User",
        "biography": "A test biography",
        "follower_count": 1000,
        "is_verified": True,
        "profile_pic_url": "https://example.com/pic.jpg",
    }

    post_image = {
        "pk": 123,
        "code": "AbCdEfG",
        "caption": {"text": "Hello world"},
        "taken_at": 1700000000,
        "like_count": 42,
        "text_post_app_info": {"direct_reply_count": 5},
        "image_versions2": {
            "candidates": [
                {"url": "https://cdn.example.com/img_small.jpg", "width": 320, "height": 480},
                {"url": "https://cdn.example.com/img_large.jpg", "width": 1080, "height": 1920},
            ]
        },
        "user": user,
    }

    post_carousel = {
        "pk": "124",
        "code": "Carousel1",
        "caption": {"text": "Swipe through"},
        "taken_at": 1700000100,
        "like_count": 7,
        "text_post_app_info": {"direct_reply_count": 2},
        "carousel_media": [
            {
                "pk": "carousel_child_1",
                "image_versions2": {
                    "candidates": [
                        {"url": "https://cdn.example.com/carousel_1.jpg", "width": 800, "height": 800}
                    ]
                },
            },
            {
                "pk": "carousel_child_2",
                "image_versions2": {
                    "candidates": [
                        {"url": "https://cdn.example.com/carousel_2.jpg", "width": 800, "height": 800}
                    ]
                },
            },
        ],
        "user": user,
    }

    post_video = {
        "pk": 125,
        "code": "VideoOne",
        "caption": {"text": "Watch this"},
        "taken_at": 1700000200,
        "like_count": 99,
        "text_post_app_info": {"direct_reply_count": 11},
        "image_versions2": {
            "candidates": [{"url": "https://cdn.example.com/poster.jpg", "width": 1080, "height": 1920}]
        },
        "video_versions": [
            {
                "url": "https://cdn.example.com/video_low.mp4",
                "width": 480,
                "height": 854,
                "bandwidth": 500_000,
            },
            {
                "url": "https://cdn.example.com/video_high.mp4",
                "width": 1080,
                "height": 1920,
                "bandwidth": 2_000_000,
            },
        ],
        "user": user,
    }

    quoted_user = {
        "username": "otheruser",
        "pk": "user_2",
    }
    post_quoted = {
        "pk": 126,
        "code": "Quoted1",
        "caption": {"text": "Original quoted text"},
        "taken_at": 1700000300,
        "like_count": 12,
        "text_post_app_info": {"direct_reply_count": 1},
        "image_versions2": {
            "candidates": [{"url": "https://cdn.example.com/quoted.jpg", "width": 600, "height": 600}]
        },
        "user": quoted_user,
    }

    post_quoting = {
        "pk": 127,
        "code": "Quoting1",
        "caption": {"text": "Look at this"},
        "taken_at": 1700000400,
        "like_count": 3,
        "text_post_app_info": {
            "direct_reply_count": 0,
            "share_info": {"quoted_post": post_quoted},
        },
        "user": user,
    }

    envelope = {
        "require": [
            [
                "ScheduledServerJS",
                "handle",
                None,
                [
                    {
                        "__bbox": {
                            "require": [
                                [
                                    "RelayPrefetchedStreamCache",
                                    "next",
                                    [],
                                    [
                                        "adp_BarcelonaProfileThreadsTabDirectQueryRelayPreloader_XXX",
                                        {
                                            "data": {
                                                "media": {
                                                    "edges": [
                                                        {"node": post_image},
                                                        {"node": post_carousel},
                                                        {"node": post_video},
                                                        {"node": post_quoting},
                                                        {"node": post_quoted},
                                                    ]
                                                },
                                                "user": user,
                                            }
                                        },
                                    ],
                                ]
                            ]
                        }
                    }
                ],
            ]
        ]
    }

    return envelope


def _make_fixture() -> str:
    """Return an HTML document embedding fake Threads post objects."""
    script_block = f'<script type="application/json">{json.dumps(_make_posts_envelope())}</script>'
    invalid_block = '<script type="application/json">this is not json</script>'

    return f"""<!DOCTYPE html>
<html>
<head><title>testuser (@testuser) on Threads</title></head>
<body>
{script_block}
{invalid_block}
</body>
</html>
"""


def _make_context(transport: httpx.MockTransport, *, use_graphql: bool = False) -> CrawlContext:
    """Build a crawl context backed by the supplied mock transport."""
    settings = Settings()
    settings.crawl.use_graphql = use_graphql
    settings.crawl.max_pages = 5
    return CrawlContext(
        settings=settings,
        http=httpx.AsyncClient(transport=transport),
        cookies=_DummyCookieProvider(),
        logger=logging.getLogger("test.threads"),
        output_dir=Path("/tmp"),
        user_agent="test-ua",
    )


class _DummyCookieProvider:
    """Minimal cookie provider for test contexts."""

    def cookies_for(self, _domain: str) -> dict[str, str]:
        return {}

    def cookie_header(self, _domain: str) -> str:
        return ""


def _make_private_fixture() -> str:
    """Return a page for a private profile: the user object, but no posts."""
    user = {
        "username": "privateuser",
        "pk": "user_2",
        "full_name": "Private User",
        "follower_count": 12,
        "text_post_app_is_private": True,
    }
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"user": user}]]]}
    script = f'<script type="application/json">{json.dumps(payload)}</script>'
    return f"<html><body>{script}</body></html>"


def _make_null_user_fixture() -> str:
    """Return a page whose quoted post carries ``"user": null``.

    Threads returns a null author for posts whose account was deleted, and the
    key is present rather than missing -- so ``dict.get("user", {})`` hands back
    ``None`` and every later ``.get()`` on it explodes.
    """
    user = {
        "username": "nulluser",
        "pk": "user_9",
        "full_name": "Null User",
        "follower_count": 5,
    }
    post_quoting = {
        "pk": 200,
        "code": "QuoteNullUser",
        "caption": {"text": "Quoting a deleted account"},
        "taken_at": 1700000500,
        "like_count": 1,
        "text_post_app_info": {
            "direct_reply_count": 0,
            "share_info": {
                "quoted_post": {
                    "pk": 201,
                    "code": "DeletedPost",
                    "caption": {"text": "gone"},
                    "taken_at": 1700000400,
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn.example.com/deleted.jpg", "width": 640, "height": 640}
                        ]
                    },
                    "user": None,
                }
            },
        },
        "user": user,
    }
    payload = {
        "require": [
            [
                "ScheduledServerJS",
                "handle",
                None,
                [{"media": {"edges": [{"node": post_quoting}]}}, {"user": user}],
            ]
        ]
    }
    script = f'<script type="application/json">{json.dumps(payload)}</script>'
    return f"<html><body>{script}</body></html>"


def _make_quoted_carousel_fixture() -> str:
    """Return a page whose parent image quotes a two-child carousel.

    The quoting parent already owns one media object, so attaching the quoted
    carousel must renumber the combined list rather than leave two ``index 0``
    refs behind.
    """
    user = {
        "username": "mixeduser",
        "pk": "user_7",
        "full_name": "Mixed User",
        "follower_count": 7,
    }
    quoted = {
        "pk": 301,
        "code": "QuotedCarousel",
        "caption": {"text": "two children"},
        "taken_at": 1700000600,
        "carousel_media": [
            {
                "pk": "30101",
                "image_versions2": {
                    "candidates": [
                        {"url": "https://cdn.example.com/quoted_c_1.jpg", "width": 600, "height": 600}
                    ]
                },
            },
            {
                "pk": "30102",
                "video_versions": [
                    {
                        "url": "https://cdn.example.com/quoted_c_2.mp4",
                        "width": 640,
                        "height": 360,
                        "bandwidth": 1_000,
                    }
                ],
            },
        ],
        "user": {"username": "otheruser", "pk": "user_8"},
    }
    parent = {
        "pk": 300,
        "code": "MixedQuote",
        "caption": {"text": "Quoting a carousel"},
        "taken_at": 1700000700,
        "image_versions2": {
            "candidates": [{"url": "https://cdn.example.com/own.jpg", "width": 1080, "height": 1080}]
        },
        "text_post_app_info": {
            "direct_reply_count": 0,
            "share_info": {"quoted_post": quoted},
        },
        "user": user,
    }
    payload = {
        "require": [
            [
                "ScheduledServerJS",
                "handle",
                None,
                [{"media": {"edges": [{"node": parent}]}}, {"user": user}],
            ]
        ]
    }
    script = f'<script type="application/json">{json.dumps(payload)}</script>'
    return f"<html><body>{script}</body></html>"


def _make_post_page_fixture() -> str:
    """Return a post page: the requested post plus the surrounding conversation.

    A real Threads post page carries the post *and* the replies around it, so
    the crawler has to pick the right one out rather than take the first.
    """
    user = {
        "username": "testuser",
        "pk": "user_1",
        "full_name": "Test User",
        "follower_count": 1000,
    }
    other = {"username": "otheruser", "pk": "user_2"}

    target_post = {
        "pk": 125,
        "code": "VideoOne",
        "caption": {"text": "Watch this"},
        "taken_at": 1700000200,
        "like_count": 99,
        "text_post_app_info": {"direct_reply_count": 11},
        "image_versions2": {
            "candidates": [{"url": "https://cdn.example.com/poster.jpg", "width": 1080, "height": 1920}]
        },
        "video_versions": [
            {
                "url": "https://cdn.example.com/video_low.mp4",
                "width": 480,
                "height": 854,
                "bandwidth": 500_000,
            }
        ],
        "user": user,
    }
    reply = {
        "pk": 900,
        "code": "SomeReply",
        "caption": {"text": "nice one"},
        "taken_at": 1700000300,
        "image_versions2": {
            "candidates": [{"url": "https://cdn.example.com/reply.jpg", "width": 640, "height": 640}]
        },
        "user": other,
    }

    payload = {
        "require": [
            [
                "ScheduledServerJS",
                "handle",
                None,
                [{"media": {"edges": [{"node": reply}, {"node": target_post}]}}, {"user": user}],
            ]
        ]
    }
    script = f'<script type="application/json">{json.dumps(payload)}</script>'
    return f"<html><body>{script}</body></html>"


def _make_graphql_session_page() -> str:
    """Return a profile page carrying the preload manifest and the posts.

    This is the shape the real page has: the server tells the client which
    persisted query it used, so the query id never has to be hardcoded, and
    the posts are server-rendered alongside it.
    """
    preloader = {
        "preloaderID": "adp_BarcelonaProfileThreadsTabDirectQueryRelayPreloader_ABC123",
        "queryID": "28967344372926951",
        "variables": {
            "allow_page_info_for_lox_user": True,
            "first": 4,
            "userID": "63055343223",
        },
        "queryName": "BarcelonaProfileThreadsTabDirectQuery",
    }
    manifest = json.dumps({"expectedPreloaders": [preloader]}, separators=(",", ":"))
    return (
        "<html><body>"
        f'<script type="application/json">{manifest}</script>'
        f'<script type="application/json">{json.dumps(_make_posts_envelope())}</script>'
        '"LSD",[],{"token":"AbCdEfToken"}'
        "</body></html>"
    )


def _make_graphql_post(code: str, pk: int, caption: str) -> dict[str, object]:
    """Return a post dict shaped like the one GraphQL returns."""
    return {
        "pk": pk,
        "code": code,
        "caption": {"text": caption},
        "taken_at": 1700000000 + pk,
        "like_count": 5,
        "text_post_app_info": {"direct_reply_count": 2},
        "image_versions2": {
            "candidates": [{"url": f"https://cdn.example.com/{code}.jpg", "width": 1080, "height": 1920}]
        },
        "user": {"username": "testuser", "pk": "user_1"},
    }


def _make_graphql_response(
    posts: list[dict[str, object]],
    *,
    end_cursor: str | None = None,
    has_next_page: bool = False,
) -> dict[str, object]:
    """Return a GraphQL response envelope."""
    return {
        "data": {
            "mediaData": {
                "edges": [{"node": {"thread_items": [{"post": post}]}} for post in posts],
                "page_info": {"end_cursor": end_cursor, "has_next_page": has_next_page},
            }
        }
    }


def _make_graphql_handler() -> Any:
    """Return a handler that serves a two-page GraphQL timeline."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/graphql":
            body = dict(parse_qsl(request.content.decode()))
            variables = json.loads(body["variables"])
            if variables.get("after") == "CURSOR2":
                return httpx.Response(
                    200,
                    json=_make_graphql_response(
                        [_make_graphql_post("Second", 302, "second page")],
                        end_cursor=None,
                        has_next_page=False,
                    ),
                )
            return httpx.Response(
                200,
                json=_make_graphql_response(
                    [_make_graphql_post("First", 301, "first page")],
                    end_cursor="CURSOR2",
                    has_next_page=True,
                ),
            )
        return httpx.Response(200, text=_make_graphql_session_page())

    return handler


def _handler(request: httpx.Request) -> httpx.Response:
    """Return the fixture HTML for Threads profile pages."""
    if request.url.path == "/@testuser":
        return httpx.Response(200, text=_make_fixture())
    if request.url.path == "/@privateuser":
        return httpx.Response(200, text=_make_private_fixture())
    if request.url.path == "/@nulluser":
        return httpx.Response(200, text=_make_null_user_fixture())
    if request.url.path == "/@mixeduser":
        return httpx.Response(200, text=_make_quoted_carousel_fixture())
    if request.url.path == "/@testuser/post/VideoOne":
        return httpx.Response(200, text=_make_post_page_fixture())
    if request.url.path == "/@testuser/post/GhostPost":
        # The page loads, but the post it was asked for is not in it.
        return httpx.Response(200, text=_make_post_page_fixture())
    if request.url.path == "/share/SHARE1":
        return httpx.Response(
            302,
            headers={"location": "https://www.threads.net/@testuser/post/VideoOne"},
        )
    if request.url.path == "/share/BROKEN":
        return httpx.Response(302, headers={"location": "https://www.threads.net/@testuser"})
    if request.url.path == "/@emptyuser":
        return httpx.Response(
            200,
            text='<script type="application/json">{"empty": true}</script>',
        )
    return httpx.Response(404, text="Not found")


def test_normalize_target() -> None:
    """Target normalisation accepts handles, profile URLs, and post URLs."""
    assert normalize_target("testuser") == "testuser"
    assert normalize_target("@testuser") == "testuser"
    assert normalize_target("https://www.threads.net/@testuser") == "testuser"
    assert normalize_target("https://www.threads.com/@testuser") == "testuser"
    assert normalize_target("https://www.threads.net/@testuser/post/AbCdEfG/") == "testuser"


def test_parse_target_recognises_every_form() -> None:
    """A target is a profile, a post, or a link that has to be resolved."""
    profile = parse_target("https://www.threads.net/@testuser")
    assert profile.username == "testuser"
    assert profile.post_code is None
    assert profile.share_url is None
    assert not profile.is_single_post

    post = parse_target("https://www.threads.net/@testuser/post/AbCdEfG/")
    assert post.username == "testuser"
    assert post.post_code == "AbCdEfG"
    assert post.share_url is None
    assert post.is_single_post

    share = parse_target("https://www.threads.com/share/D-VhmZm13/")
    assert share.post_code == "D-VhmZm13"
    assert share.share_url == "https://www.threads.com/share/D-VhmZm13/"
    assert share.is_single_post


def test_normalize_target_refuses_a_share_link() -> None:
    """A share link's author is only known after the redirect."""
    with pytest.raises(UnsupportedTargetError, match="share link"):
        normalize_target("https://www.threads.com/share/D-VhmZm13/")


async def test_post_url_yields_only_that_post() -> None:
    """A post URL must yield that one post, not the conversation around it."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("https://www.threads.net/@testuser/post/VideoOne")]

    assert len(posts) == 1
    assert posts[0].shortcode == "VideoOne"
    assert posts[0].media[0].url == "https://cdn.example.com/video_low.mp4"
    # A single post is not truncated.
    assert "truncated" not in posts[0].metadata


async def test_share_link_resolves_to_the_post() -> None:
    """A share link redirects to the canonical post, and that post is scraped."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("https://www.threads.net/share/SHARE1")]

    assert len(posts) == 1
    assert posts[0].shortcode == "VideoOne"
    assert posts[0].target == "testuser"


async def test_share_link_that_does_not_resolve_is_reported() -> None:
    """A share link that redirects to a profile is an error, not silence."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)

        with pytest.raises(ParseError, match="did not resolve to a post"):
            [post async for post in crawler.crawl("https://www.threads.net/share/BROKEN")]


async def test_graphql_transport_reports_the_page_ceiling() -> None:
    """A timeline cut short by the page budget must say so.

    The paging loop used to run out of pages silently, and the result was
    reported as a complete timeline. A caller cannot tell a short history from
    a whole one unless the crawler says which it is.
    """
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        if request.url.path == "/api/graphql":
            pages += 1
            # Always claim there is more, with a cursor that keeps moving.
            return httpx.Response(
                200,
                json=_make_graphql_response(
                    [_make_graphql_post(f"Post{pages}", 300 + pages, f"page {pages}")],
                    end_cursor=f"CURSOR{pages}",
                    has_next_page=True,
                ),
            )
        return httpx.Response(200, text=_make_graphql_session_page())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        ctx.settings.crawl.max_pages = 3
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert pages == 3
    assert len(posts) == 3
    assert all(post.metadata.get("truncated") is True for post in posts)
    reason = posts[0].metadata.get("truncation_reason", "")
    assert "ceiling" in reason


async def test_graphql_transport_does_not_report_truncation_when_complete() -> None:
    """A timeline that really ended is not reported as cut short."""
    transport = httpx.MockTransport(_make_graphql_handler())
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert all("truncated" not in post.metadata for post in posts)


async def test_graphql_transport_pages_the_whole_timeline() -> None:
    """The GraphQL transport must walk past the first page."""
    transport = httpx.MockTransport(_make_graphql_handler())
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert [post.shortcode for post in posts] == ["First", "Second"]
    # A paginated timeline is not truncated.
    assert all("truncated" not in post.metadata for post in posts)


async def test_graphql_transport_sends_the_discovered_doc_id() -> None:
    """The query id has to come from the page, not from a constant."""
    bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/graphql":
            bodies.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(200, json=_make_graphql_response([]))
        return httpx.Response(200, text=_make_graphql_session_page())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        _ = [post async for post in crawler.crawl("testuser")]

    assert bodies
    body = bodies[0]
    assert body["doc_id"] == "28967344372926951"
    assert body["lsd"] == "AbCdEfToken"
    assert body["fb_api_req_friendly_name"] == "BarcelonaProfileThreadsTabDirectQuery"
    variables = json.loads(body["variables"])
    assert variables["userID"] == "63055343223"
    assert variables["first"] == 4
    assert "after" not in variables


async def test_graphql_transport_passes_the_cursor() -> None:
    """The second page must carry the cursor of the first."""
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/graphql":
            body = dict(parse_qsl(request.content.decode()))
            variables = json.loads(body["variables"])
            cursors.append(variables.get("after"))
            if variables.get("after") == "CURSOR2":
                return httpx.Response(200, json=_make_graphql_response([]))
            return httpx.Response(
                200,
                json=_make_graphql_response(
                    [_make_graphql_post("First", 301, "first")],
                    end_cursor="CURSOR2",
                    has_next_page=True,
                ),
            )
        return httpx.Response(200, text=_make_graphql_session_page())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        _ = [post async for post in crawler.crawl("testuser")]

    assert cursors == [None, "CURSOR2"]


async def test_graphql_failure_falls_back_to_the_profile_page() -> None:
    """A broken query must degrade the result, not fail the run."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/graphql":
            return httpx.Response(500)
        return httpx.Response(200, text=_make_graphql_session_page())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport, use_graphql=True)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert len(posts) == 4
    assert "/api/graphql" in calls
    # The fallback is the truncated server-rendered page.
    assert all(post.metadata.get("truncated") is True for post in posts)


async def test_missing_post_is_reported() -> None:
    """A post that is not in the payload must be named as missing."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)

        with pytest.raises(ParseError, match="was not in the payload"):
            [post async for post in crawler.crawl("https://www.threads.net/@testuser/post/GhostPost")]


async def test_crawl_extracts_expected_posts() -> None:
    """The crawler extracts the expected top-level posts from the fixture."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert len(posts) == 4
    codes = [post.shortcode for post in posts]
    assert codes == ["AbCdEfG", "Carousel1", "VideoOne", "Quoting1"]


async def test_post_field_mapping() -> None:
    """Post fields map correctly from the raw Threads payload."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = {post.shortcode: post async for post in crawler.crawl("testuser")}

    image = posts["AbCdEfG"]
    assert image.post_id == "123"
    assert image.permalink == "https://www.threads.net/@testuser/post/AbCdEfG/"
    assert image.caption == "Hello world"
    assert image.timestamp == 1700000000
    assert image.metadata["like_count"] == 42
    assert image.metadata["reply_count"] == 5


async def test_video_post_emits_only_video() -> None:
    """A video post yields exactly one MediaRef of kind video."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = {post.shortcode: post async for post in crawler.crawl("testuser")}

    video = posts["VideoOne"]
    assert len(video.media) == 1
    assert video.media[0].kind == "video"
    assert video.media[0].url == "https://cdn.example.com/video_high.mp4"
    assert video.media[0].width == 1080
    assert video.media[0].height == 1920


async def test_carousel_yields_multiple_media_refs() -> None:
    """A carousel post yields one MediaRef per child image."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = {post.shortcode: post async for post in crawler.crawl("testuser")}

    carousel = posts["Carousel1"]
    assert len(carousel.media) == 2
    assert all(ref.kind == "image" for ref in carousel.media)
    assert [ref.index for ref in carousel.media] == [0, 1]
    assert [ref.media_id for ref in carousel.media] == ["carousel_child_1", "carousel_child_2"]


async def test_quoted_post_attached_to_parent() -> None:
    """A quoted post is not emitted top-level; its media attaches to the parent."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = {post.shortcode: post async for post in crawler.crawl("testuser")}

    assert "Quoted1" not in posts
    quoting = posts["Quoting1"]
    quoted_refs = [ref for ref in quoting.media if ref.url == "https://cdn.example.com/quoted.jpg"]
    assert quoted_refs
    assert quoted_refs[0].media_id == "126"
    assert [ref.index for ref in quoting.media] == list(range(len(quoting.media)))
    assert quoting.metadata["quoted"] == {
        "post_id": "126",
        "username": "otheruser",
        "permalink": "https://www.threads.net/@otheruser/post/Quoted1/",
    }


async def test_quoted_carousel_keeps_contiguous_indices_and_child_ids() -> None:
    """Attaching a quoted carousel renumbers the combined media list."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("mixeduser")]

    assert len(posts) == 1
    media = posts[0].media
    assert [ref.url for ref in media] == [
        "https://cdn.example.com/own.jpg",
        "https://cdn.example.com/quoted_c_1.jpg",
        "https://cdn.example.com/quoted_c_2.mp4",
    ]
    assert [ref.index for ref in media] == [0, 1, 2]
    # The parent owns the first object; the quoted carousel children keep their
    # own ids so the manifest can say whose media it is.
    assert [ref.media_id for ref in media] == ["300", "30101", "30102"]


async def test_quoted_post_with_null_user_does_not_crash() -> None:
    """A quoted post whose author was deleted must not raise.

    Threads sends ``"user": null`` -- the key is present, the value is not --
    so a naive ``get("user", {})`` returns ``None`` and the next ``.get()``
    on it raises ``AttributeError``.
    """
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = {post.shortcode: post async for post in crawler.crawl("nulluser")}

    quoting = posts["QuoteNullUser"]
    assert any(ref.url == "https://cdn.example.com/deleted.jpg" for ref in quoting.media)
    assert quoting.metadata["quoted"]["post_id"] == "201"
    assert quoting.metadata["quoted"]["username"] is None


async def test_invalid_script_block_skipped() -> None:
    """The walker tolerates script blocks that are not valid JSON."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert len(posts) == 4


async def test_no_posts_raises_parse_error() -> None:
    """A response with no matching posts raises ParseError."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)

        with pytest.raises(ParseError, match="no post objects were found"):
            [post async for post in crawler.crawl("emptyuser")]


async def test_private_profile_is_reported_as_private() -> None:
    """A private profile must be named as such, not blamed on the user agent."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)

        with pytest.raises(ParseError, match="private Threads profile"):
            [post async for post in crawler.crawl("privateuser")]


async def test_profile_parses_user() -> None:
    """The profile method extracts profile metadata from the same payload."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        profile = await crawler.profile("testuser")

    assert isinstance(profile, ProfileInfo)
    assert profile.platform == "threads"
    assert profile.target == "testuser"
    assert profile.display_name == "Test User"
    assert profile.follower_count == 1000
    assert profile.biography == "A test biography"
    assert profile.post_count is None
    assert profile.metadata["is_verified"] is True
    assert profile.metadata["profile_pic_url"] == "https://example.com/pic.jpg"


async def test_truncation_metadata_set() -> None:
    """Each emitted post carries metadata explaining the platform ceiling."""
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ctx = _make_context(transport)
        ctx.http = client
        crawler = ThreadsCrawler(ctx)
        posts = [post async for post in crawler.crawl("testuser")]

    assert all(post.metadata.get("truncated") is True for post in posts)
    assert all("10-20" in post.metadata.get("truncation_reason", "") for post in posts)


def test_invalid_target_raises() -> None:
    """Invalid targets raise UnsupportedTargetError."""
    with pytest.raises(UnsupportedTargetError):
        normalize_target("not a valid target!")
