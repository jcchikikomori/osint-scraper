"""Threads (threads.net) crawler.

Threads is a Meta "Comet" application. Its server-side rendering gates the
actual post data on the User-Agent: a desktop browser UA receives an empty
shell, while the Threads mobile-app UA receives HTML that embeds the full
post objects inside ``<script type="application/json">`` blocks.

This crawler parses every JSON script block, recursively walks the parsed
object graph, and collects every dict that matches the Threads post signature.
That strategy is intentionally robust against envelope changes.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from osint_scraper.crawlers.base import CrawlContext, Crawler, register_crawler
from osint_scraper.crawlers.threads_api import (
    GRAPHQL_ENDPOINT,
    GRAPHQL_POST_UNAVAILABLE,
    GRAPHQL_PRIVATE_PROFILE,
    THREADS_USER_AGENTS,
    ThreadsGraphQLError,
    ThreadsGraphQLSession,
    fetch_graphql_page,
    open_graphql_session,
    open_graphql_session_from_html,
    page_headers,
    post_url,
    profile_url,
)
from osint_scraper.exceptions import AuthError, ParseError, RateLimitError, UnsupportedTargetError
from osint_scraper.models import MediaRef, Post, ProfileInfo, stamp_indices

__all__ = [
    "THREADS_USER_AGENTS",
    "ThreadsCrawler",
    "ThreadsTarget",
    "normalize_target",
    "parse_target",
]

_PROFILE_URL_RE = re.compile(r"^https?://(?:www\.)?threads\.(?:net|com)/@(?P<username>[A-Za-z0-9_.]+)/?.*$")
_POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?threads\.(?:net|com)/@(?P<username>[A-Za-z0-9_.]+)/post/(?P<code>[A-Za-z0-9_-]+)/?.*$"
)
_SHARE_URL_RE = re.compile(r"^https?://(?:www\.)?threads\.(?:net|com)/share/(?P<code>[A-Za-z0-9_-]+)/?.*$")
_SHORT_URL_RE = re.compile(r"^https?://(?:www\.)?threads\.(?:net|com)/t/(?P<code>[A-Za-z0-9_-]+)/?.*$")
_HANDLE_RE = re.compile(r"^@?(?P<username>[A-Za-z0-9_.]+)$")
_SCRIPT_JSON_RE = re.compile(
    r'<script\s+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


_SSR_TRUNCATION = "Threads unauthenticated profile pages return roughly the 10-20 most recent posts"
"""Why a server-rendered profile result is incomplete."""


@dataclass(frozen=True, slots=True)
class ThreadsTarget:
    """A parsed Threads target.

    A target is either a profile (scrape the posts it shows) or a single post
    (scrape that one post).  A share link is a third case: it names a post
    only indirectly, and the author and the post code are revealed by the
    redirect it issues.

    Attributes:
        username: The account handle, empty when it is only known after a
            redirect.
        post_code: The short code of a single post, or ``None`` for a profile.
        share_url: The share or short link to resolve, or ``None``.
    """

    username: str
    post_code: str | None = None
    share_url: str | None = None

    @property
    def is_single_post(self) -> bool:
        """Whether the target names one post rather than a profile."""
        return self.post_code is not None or self.share_url is not None


def parse_target(target: str) -> ThreadsTarget:
    """Parse a Threads target into a handle, a post, or a share link.

    Args:
        target: A handle, profile URL, post URL, share link or short link.

    Returns:
        The parsed target.

    Raises:
        UnsupportedTargetError: When ``target`` cannot be interpreted.
    """
    target = target.strip()
    for pattern in (_SHARE_URL_RE, _SHORT_URL_RE):
        match = pattern.match(target)
        if match:
            return ThreadsTarget(username="", post_code=match.group("code"), share_url=target)

    match = _POST_URL_RE.match(target)
    if match:
        return ThreadsTarget(username=match.group("username"), post_code=match.group("code"))

    match = _PROFILE_URL_RE.match(target)
    if match:
        return ThreadsTarget(username=match.group("username"))

    match = _HANDLE_RE.match(target)
    if match:
        return ThreadsTarget(username=match.group("username"))

    raise UnsupportedTargetError(f"Threads target {target!r} is not a recognised handle or URL")


def normalize_target(target: str) -> str:
    """Return the bare username for a Threads profile or post URL/handle.

    Args:
        target: A handle, profile URL, or single-post URL.

    Returns:
        The bare username without a leading ``@``.

    Raises:
        UnsupportedTargetError: When ``target`` cannot be interpreted as a
            Threads profile or post, or is a share link whose author is only
            known after the redirect.
    """
    parsed = parse_target(target)
    if not parsed.username:
        raise UnsupportedTargetError(
            f"Threads target {target!r} is a share link; its author is only known "
            "after the redirect. Scrape it with `scrape`, not with `profile`."
        )
    return parsed.username


def _parse_post_url(url: str) -> tuple[str, str] | None:
    """Return the ``(username, code)`` of a canonical Threads post URL.

    Args:
        url: The URL to inspect.

    Returns:
        The handle and the post code, or ``None`` when the URL is not a post
        URL.
    """
    match = _POST_URL_RE.match(url)
    if match:
        return match.group("username"), match.group("code")
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a dict, otherwise an empty dict.

    The Threads payload routinely carries ``null`` for an object it otherwise
    always provides, e.g. ``"user": null`` on a quoted post whose author was
    deleted. ``dict.get(key, {})`` does not guard against that -- it only
    guards against a *missing* key -- so every lookup goes through here.
    """
    return value if isinstance(value, dict) else {}


def _is_post_dict(obj: Any) -> bool:
    """Return whether ``obj`` looks like a Threads post object."""
    if not isinstance(obj, dict):
        return False
    has_pk = "pk" in obj
    has_code = "code" in obj
    has_content = any(
        key in obj for key in ("caption", "image_versions2", "video_versions", "text_post_app_info")
    )
    return has_pk and has_code and has_content


def _walk_json(obj: Any, posts: list[dict[str, Any]], seen_pks: set[int | str]) -> None:
    """Recursively walk ``obj`` and append every post dict to ``posts``."""
    if isinstance(obj, dict):
        if _is_post_dict(obj):
            pk = obj["pk"]
            if pk not in seen_pks:
                seen_pks.add(pk)
                posts.append(obj)
        for value in obj.values():
            _walk_json(value, posts, seen_pks)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, posts, seen_pks)


def _find_profile_user(obj: Any, username: str) -> dict[str, Any] | None:
    """Recursively search ``obj`` for the profile user dict.

    The match is on the handle plus at least one profile-level field, so that a
    post's embedded author object is not mistaken for the profile itself. The
    field list is deliberately broad: a private profile's object carries
    ``text_post_app_is_private`` but may omit ``biography``.
    """
    profile_fields = ("follower_count", "text_post_app_is_private", "text_app_biography")
    if isinstance(obj, dict):
        if obj.get("username") == username and any(field in obj for field in profile_fields):
            return obj
        for value in obj.values():
            found = _find_profile_user(value, username)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_profile_user(item, username)
            if found is not None:
                return found
    return None


def _caption_from_raw(raw: dict[str, Any]) -> str | None:
    """Extract caption text from a Threads post dict."""
    caption = raw.get("caption")
    if isinstance(caption, dict):
        return caption.get("text")
    if isinstance(caption, str):
        return caption
    return None


def _normalize_url(url: str) -> str:
    """Return ``url`` with escaped slashes decoded."""
    return url.replace("\\/", "/")


def _media_id_from_raw(raw: dict[str, Any]) -> str | None:
    """Return the post's own ``pk`` as a media id, when it carries one.

    Args:
        raw: A Threads post dict.

    Returns:
        The ``pk`` as a string, or ``None`` when it is missing.
    """
    return _string_id(raw.get("pk"))


def _string_id(value: Any) -> str | None:
    """Return ``value`` as a non-empty id string, or ``None``.

    Args:
        value: A platform identifier, usually an ``int`` or a ``str``.

    Returns:
        The identifier as a string when it is usable, otherwise ``None``.
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _best_image(candidates: list[Any], *, media_id: str | None = None) -> MediaRef | None:
    """Pick the highest-resolution image candidate.

    Args:
        candidates: The ``image_versions2.candidates`` list.
        media_id: The platform's id for the media object, when known.

    Returns:
        The best candidate as a :class:`MediaRef`, or ``None`` when there is no
        usable candidate.
    """
    best: dict[str, Any] | None = None
    best_size = -1
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        width = candidate.get("width") or 0
        height = candidate.get("height") or 0
        size = width * height
        if size > best_size:
            best_size = size
            best = candidate
    if not best:
        return None
    return MediaRef(
        url=_normalize_url(best["url"]),
        kind="image",
        width=best.get("width"),
        height=best.get("height"),
        mime="image/jpeg",
        extension=".jpg",
        media_id=media_id,
    )


def _best_video(versions: list[Any], *, media_id: str | None = None) -> MediaRef | None:
    """Pick the highest-quality video version.

    Args:
        versions: The ``video_versions`` list.
        media_id: The platform's id for the media object, when known.

    Returns:
        The best version as a :class:`MediaRef`, or ``None`` when there is no
        usable version.
    """
    best: dict[str, Any] | None = None
    best_size = -1
    best_bandwidth = -1
    for version in versions:
        if not isinstance(version, dict):
            continue
        width = version.get("width") or 0
        height = version.get("height") or 0
        size = width * height
        bandwidth = version.get("bandwidth") or 0
        if (size, bandwidth) > (best_size, best_bandwidth):
            best_size = size
            best_bandwidth = bandwidth
            best = version
    if not best:
        return None
    return MediaRef(
        url=_normalize_url(best["url"]),
        kind="video",
        width=best.get("width"),
        height=best.get("height"),
        mime="video/mp4",
        extension=".mp4",
        media_id=media_id,
    )


def _media_from_post(raw: dict[str, Any]) -> list[MediaRef]:
    """Return media refs for a single Threads post dict.

    Every ref carries its zero-based position and, where the payload provides
    one, the platform's own id for that media object.

    Args:
        raw: A Threads post dict.

    Returns:
        The post's media references in the order the post presents them.
    """
    media: list[MediaRef] = []
    post_media_id = _media_id_from_raw(raw)

    # A video post with a poster frame is still a video post.
    video_versions = raw.get("video_versions")
    if isinstance(video_versions, list) and video_versions:
        video = _best_video(video_versions, media_id=post_media_id)
        if video is not None:
            media.append(video)
            return stamp_indices(media)

    image_versions2 = raw.get("image_versions2")
    if isinstance(image_versions2, dict):
        candidates = image_versions2.get("candidates")
        if isinstance(candidates, list) and candidates:
            image = _best_image(candidates, media_id=post_media_id)
            if image is not None:
                media.append(image)

    carousel_media = raw.get("carousel_media")
    if isinstance(carousel_media, list):
        for child in carousel_media:
            if not isinstance(child, dict):
                continue
            child_media_id = _string_id(child.get("pk"))
            child_video = child.get("video_versions")
            if isinstance(child_video, list) and child_video:
                video = _best_video(child_video, media_id=child_media_id)
                if video is not None:
                    media.append(video)
                    continue
            child_images = child.get("image_versions2")
            if isinstance(child_images, dict):
                candidates = child_images.get("candidates")
                if isinstance(candidates, list) and candidates:
                    image = _best_image(candidates, media_id=child_media_id)
                    if image is not None:
                        media.append(image)

    return stamp_indices(media)


def _permalink_from_raw(raw: dict[str, Any], fallback_username: str) -> str:
    """Return the best permalink available for a post dict."""
    for key in ("permalink", "url", "link"):
        value = raw.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return _normalize_url(value)
    code = raw.get("code")
    username = _as_dict(raw.get("user")).get("username") or fallback_username
    return f"https://www.threads.net/@{username}/post/{code}/"


def _build_post(raw: dict[str, Any], username: str) -> Post:
    """Map a Threads post dict to a :class:`Post`."""
    post_id = str(raw["pk"])
    shortcode = raw.get("code")
    permalink = _permalink_from_raw(raw, username)
    caption = _caption_from_raw(raw)
    timestamp = raw.get("taken_at")
    media = _media_from_post(raw)

    text_post_app_info = _as_dict(raw.get("text_post_app_info"))
    share_info = _as_dict(text_post_app_info.get("share_info"))
    quoted_raw = share_info.get("quoted_post")
    reply_to_author = text_post_app_info.get("reply_to_author")
    author = _as_dict(raw.get("user"))

    metadata: dict[str, Any] = {
        "like_count": raw.get("like_count"),
        "reply_count": text_post_app_info.get("direct_reply_count"),
        "is_quote_post": bool(quoted_raw),
        "author_username": author.get("username"),
        "author_id": author.get("pk"),
    }
    if "quote_count" in text_post_app_info:
        metadata["quote_count"] = text_post_app_info["quote_count"]
    if "view_count" in raw:
        metadata["view_count"] = raw["view_count"]

    quoted_post_id: str | None = None
    if isinstance(quoted_raw, dict) and "pk" in quoted_raw:
        quoted_post_id = str(quoted_raw["pk"])

    parent_post_id: str | None = None
    if isinstance(reply_to_author, dict) and "pk" in reply_to_author:
        parent_post_id = str(reply_to_author["pk"])
    elif isinstance(reply_to_author, (int, str)):
        parent_post_id = str(reply_to_author)

    return Post(
        platform="threads",
        target=username,
        post_id=post_id,
        shortcode=shortcode,
        permalink=permalink,
        caption=caption,
        timestamp=timestamp if isinstance(timestamp, int) else None,
        media=media,
        metadata=metadata,
        quoted_post_id=quoted_post_id,
        parent_post_id=parent_post_id,
    )


def _attach_quoted(
    parent: Post,
    quoted_raw: dict[str, Any],
    seen: set[str],
    *,
    is_direct: bool = True,
) -> None:
    """Attach a quoted post's media to ``parent`` and record metadata.

    Args:
        parent: The post that is receiving the quoted media.
        quoted_raw: The raw quoted post dict from ``share_info.quoted_post``.
        seen: Pks that have already been attached, to prevent infinite recursion
            when a quoted post itself quotes another post.
        is_direct: Whether this is the immediate quoted post of ``parent``.
    """
    pk = str(quoted_raw.get("pk"))
    if not pk or pk in seen:
        return
    seen.add(pk)

    parent.media.extend(_media_from_post(quoted_raw))

    if is_direct:
        author = _as_dict(quoted_raw.get("user"))
        parent.metadata["quoted"] = {
            "post_id": pk,
            "username": author.get("username"),
            "permalink": _permalink_from_raw(quoted_raw, parent.target),
        }

    nested = _as_dict(_as_dict(quoted_raw.get("text_post_app_info")).get("share_info")).get("quoted_post")
    if isinstance(nested, dict):
        _attach_quoted(parent, nested, seen, is_direct=False)

    # Attaching extends the parent's list, so the positions stamped by
    # ``_media_from_post`` no longer describe the combined list. Re-stamp after
    # every attach -- including the recursive ones above -- so the final list is
    # contiguous 0..n-1 with no gaps or duplicates.
    parent.media[:] = stamp_indices(parent.media)


@register_crawler
class ThreadsCrawler(Crawler):
    """Discover public Threads posts and profile metadata."""

    PLATFORM: ClassVar[str] = "threads"
    DISPLAY_NAME: ClassVar[str] = "Threads"
    REQUIRES_AUTH: ClassVar[bool] = False
    SUPPORTS_LIMIT: ClassVar[bool] = True

    MAX_EMPTY_PAYLOAD_RETRIES: ClassVar[int] = 3
    """How many times to re-fetch when Threads returns a payload with no posts."""

    RETRY_BACKOFF_SECONDS: ClassVar[float] = 2.5
    """Base backoff between those retries, multiplied by the attempt number."""

    def __init__(self, ctx: CrawlContext) -> None:
        """Store the crawl context and derive a scoped logger."""
        super().__init__(ctx)

    async def crawl(
        self,
        target: str,
        *,
        limit: int | None = None,
    ) -> AsyncIterator[Post]:
        """Yield public Threads posts for ``target``.

        A profile target yields the posts the profile page shows.  A post
        target -- a post URL, a share link or a short link -- yields exactly
        that one post.

        Args:
            target: Threads handle, profile URL, post URL, or share link.
            limit: Maximum number of posts to yield.

        Yields:
            :class:`~osint_scraper.models.Post` objects.
        """
        parsed = parse_target(target)

        truncation_reason: str | None = None
        if parsed.share_url is not None:
            username, entries = await self._fetch_shared_post(parsed.share_url)
        elif parsed.post_code is not None:
            username, entries = await self._fetch_single_post(parsed.username, parsed.post_code)
        else:
            username = parsed.username
            entries, truncation_reason = await self._fetch_profile_posts(username, limit)

        # Attach quoted-post media to the quoting parent. Quoted posts from
        # other accounts are not emitted as top-level posts.
        for post, raw in entries:
            quoted_raw = _as_dict(_as_dict(raw.get("text_post_app_info")).get("share_info")).get(
                "quoted_post"
            )
            if isinstance(quoted_raw, dict):
                _attach_quoted(post, quoted_raw, set())

        top_level = [post for post, _raw in entries]
        if truncation_reason is None:
            self.log.info("Threads returned %d post(s) for @%s", len(top_level), username)
        else:
            self.log.info(
                "Threads returned %d post(s) for @%s (incomplete: %s)",
                len(top_level),
                username,
                truncation_reason,
            )

        for yielded, post in enumerate(top_level, start=1):
            if truncation_reason is not None:
                post.metadata["truncated"] = True
                post.metadata["truncation_reason"] = truncation_reason
            if limit is not None and yielded > limit:
                self.log.debug("Reached limit of %d post(s)", limit)
                break
            yield post

    async def _fetch_profile_posts(
        self, username: str, limit: int | None = None
    ) -> tuple[list[tuple[Post, dict[str, Any]]], str | None]:
        """Fetch the posts of a profile.

        The GraphQL transport reaches the whole timeline; the server-rendered
        page only carries the most recent handful.  The GraphQL transport is
        tried first and the page is the fallback, so a bundle rotation that
        breaks the query degrades the result instead of failing the run.

        Args:
            username: The Threads handle.
            limit: Stop once this many posts have been collected.

        Returns:
            The posts authored by ``username``, each paired with its raw dict,
            and the reason the result is incomplete -- ``None`` when it is
            complete.
        """
        if self.ctx.settings.crawl.use_graphql:
            # The GraphQL timeline query must be *anonymous*.  A logged-in
            # request is answered with the personal, non-paginating variant of
            # the query -- one page and no cursor -- and the session cookie
            # makes the endpoint answer with HTML instead of JSON.  So the page
            # that carries the query manifest is fetched without the browser
            # session, and the query goes out without it too.
            html, _response_url = await self._fetch_page(profile_url(username), use_cookies=False)
            try:
                session = open_graphql_session_from_html(html)
            except ThreadsGraphQLError as exc:
                self.log.warning(
                    "the Threads GraphQL transport failed (%s); falling back to the profile page",
                    exc,
                )
                return await self._fetch_ssr_posts(username), _SSR_TRUNCATION
            try:
                entries, hit_ceiling = await self._fetch_graphql_posts(username, session, limit)
            except ThreadsGraphQLError as exc:
                self.log.warning(
                    "the Threads GraphQL transport failed (%s); falling back to the profile page",
                    exc,
                )
            except httpx.HTTPError as exc:
                self.log.warning(
                    "the Threads GraphQL transport failed (%s); falling back to the profile page",
                    exc,
                )
            else:
                if entries:
                    if hit_ceiling:
                        return entries, (
                            f"the run stopped at the {self.ctx.settings.crawl.max_pages}-page "
                            "ceiling, so the timeline is incomplete"
                        )
                    return entries, None
                self.log.warning(
                    "the Threads GraphQL transport returned no posts for @%s; "
                    "falling back to the profile page",
                    username,
                )
            return await self._fetch_ssr_posts(username), _SSR_TRUNCATION
        return await self._fetch_ssr_posts(username), _SSR_TRUNCATION

    async def _fetch_graphql_posts(
        self, username: str, session: ThreadsGraphQLSession, limit: int | None = None
    ) -> tuple[list[tuple[Post, dict[str, Any]]], bool]:
        """Page a profile's whole timeline over the GraphQL transport.

        The cursor chain is strictly sequential -- each page names the next --
        so the requests cannot be parallelised.

        Args:
            username: The Threads handle.
            session: The GraphQL session to page with.
            limit: Stop once this many posts have been collected.

        Returns:
            The posts authored by ``username``, each paired with its raw dict,
            and whether the page budget ran out before the timeline did.

        Raises:
            ParseError: When the profile is private.
            ThreadsGraphQLError: When the transport fails for any other reason.
        """
        max_pages = max(1, self.ctx.settings.crawl.max_pages)
        seen: set[str] = set()
        entries: list[tuple[Post, dict[str, Any]]] = []
        cursor: str | None = None
        refreshed = False
        hit_ceiling = False

        for page_number in range(1, max_pages + 1):
            await self._apply_politeness()
            try:
                posts, info = await fetch_graphql_page(
                    self.ctx.http,
                    session,
                    after=cursor,
                )
            except ThreadsGraphQLError as exc:
                if exc.code == GRAPHQL_POST_UNAVAILABLE:
                    # The server walked into a post it cannot resolve.  That is
                    # the end of the timeline, not a failure.
                    self.log.debug("reached the end of the timeline of @%s", username)
                    break
                if exc.code == GRAPHQL_PRIVATE_PROFILE:
                    raise ParseError(
                        f"@{username} is a private Threads profile. Its posts are "
                        "not readable, even with an authenticated session."
                    ) from exc
                if exc.summary == "NotJSON" and not refreshed:
                    # The session token expired.  Mine a fresh one and retry the
                    # same cursor once.
                    refreshed = True
                    self.log.info("the Threads session token expired; renewing it")
                    session = await open_graphql_session(self.ctx.http, username)
                    continue
                raise

            fresh = [raw for raw in posts if str(raw["pk"]) not in seen]
            seen.update(str(raw["pk"]) for raw in fresh)
            for raw in fresh:
                if _as_dict(raw.get("user")).get("username") == username:
                    entries.append((_build_post(raw, username), raw))
                    if limit is not None and len(entries) >= limit:
                        break

            self.log.info(
                "Threads page %d for @%s: %d new post(s), %d so far",
                page_number,
                username,
                len(fresh),
                len(entries),
            )

            if limit is not None and len(entries) >= limit:
                self.log.debug("Reached limit of %d post(s)", limit)
                break

            if not info.get("has_next_page"):
                break
            cursor = info.get("end_cursor")
            if not cursor or not fresh:
                break
        else:
            # The loop ran out of pages without the server saying there was no
            # more.  The timeline was cut short, and the caller has to say so
            # rather than report an incomplete result as a complete one.
            hit_ceiling = True
            self.log.warning(
                "stopped at the %d-page ceiling for @%s; the timeline is incomplete",
                max_pages,
                username,
            )

        return entries, hit_ceiling

    async def _fetch_ssr_posts(
        self, username: str, html: str | None = None
    ) -> list[tuple[Post, dict[str, Any]]]:
        """Return the posts the server-rendered profile page carries.

        Args:
            username: The Threads handle.
            html: The page HTML, when the caller already has it.

        Returns:
            The posts authored by ``username``, each paired with its raw dict.

        Raises:
            ParseError: When the page carried no posts at all.
        """
        if html is None:
            html, _response_url = await self._fetch_page(profile_url(username))
        posts = self._parse_posts(html)

        # A private profile never renders its posts, so there is nothing to retry.
        if not posts and self._is_private_profile(html, username):
            raise self._empty_payload_error(username, html, 0)

        # Threads intermittently serves an empty shell even with the right user agent
        # and headers -- the response is smaller and carries no post objects at all.
        # A retry gets the full payload, so retry before giving up.
        attempts = 0
        while not posts and attempts < self.MAX_EMPTY_PAYLOAD_RETRIES:
            attempts += 1
            self.log.warning(
                "Threads returned a payload with no posts (attempt %d/%d), retrying",
                attempts,
                self.MAX_EMPTY_PAYLOAD_RETRIES,
            )
            await asyncio.sleep(self.RETRY_BACKOFF_SECONDS * attempts)
            html, _response_url = await self._fetch_page(
                profile_url(username), ua_offset=attempts % len(THREADS_USER_AGENTS)
            )
            posts = self._parse_posts(html)

        if not posts:
            raise self._empty_payload_error(username, html, attempts)

        return [
            (_build_post(raw, username), raw)
            for raw in posts
            if _as_dict(raw.get("user")).get("username") == username
        ]

    async def _fetch_single_post(
        self, username: str, code: str
    ) -> tuple[str, list[tuple[Post, dict[str, Any]]]]:
        """Fetch one post by its short code.

        The post page carries the post itself plus the surrounding conversation,
        so the post is picked out by its code rather than taken as the first one.

        Args:
            username: The handle of the account that wrote the post.
            code: The short code of the post.

        Returns:
            The handle and the post paired with its raw dict.

        Raises:
            ParseError: When the page does not carry the post.
        """
        html, _response_url = await self._fetch_page(post_url(username, code))
        raw = self._select_post(self._parse_posts(html), code)
        if raw is None:
            raise self._missing_post_error(username, code, html)
        return username, [(_build_post(raw, username), raw)]

    async def _fetch_shared_post(self, share_url: str) -> tuple[str, list[tuple[Post, dict[str, Any]]]]:
        """Resolve a share link and return the post it points at.

        A share link redirects to the canonical post URL, which is where both
        the author and the short code come from.

        Args:
            share_url: The share or short link.

        Returns:
            The handle and the post paired with its raw dict.

        Raises:
            ParseError: When the link does not resolve to a post.
        """
        html, final_url = await self._fetch_page(share_url)
        canonical = _parse_post_url(final_url)
        if canonical is None:
            raise ParseError(
                f"the share link {share_url!r} did not resolve to a post (final URL: {final_url})"
            )
        username, code = canonical
        raw = self._select_post(self._parse_posts(html), code)
        if raw is None:
            raise self._missing_post_error(username, code, html)
        return username, [(_build_post(raw, username), raw)]

    @staticmethod
    def _select_post(posts: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
        """Return the post with the given short code.

        Args:
            posts: The post dicts found in the payload.
            code: The short code to look for.

        Returns:
            The post dict, or ``None`` when it is not in the payload.
        """
        for raw in posts:
            if raw.get("code") == code:
                return raw
        return None

    async def _apply_politeness(self) -> None:
        """Wait until Threads may be contacted again.

        Raises:
            RateLimitError: When the host is in a cooldown.
        """
        pacer = self.ctx.pacer
        if pacer is None:
            return
        await pacer.wait(GRAPHQL_ENDPOINT)

    async def profile(self, target: str) -> ProfileInfo:
        """Fetch profile metadata for ``target``.

        Args:
            target: Threads handle, profile URL, or single-post URL.

        Returns:
            :class:`~osint_scraper.models.ProfileInfo`.
        """
        username = normalize_target(target)
        html, _response_url = await self._fetch_page(profile_url(username))
        blocks = _SCRIPT_JSON_RE.findall(html)

        parsed: list[Any] = []
        for block in blocks:
            try:
                parsed.append(json.loads(block))
            except json.JSONDecodeError:
                continue

        user: dict[str, Any] | None = None
        for obj in parsed:
            user = _find_profile_user(obj, username)
            if user is not None:
                break

        if user is None:
            raise ParseError(f"Could not locate profile user object for @{username}")

        return ProfileInfo(
            platform="threads",
            target=username,
            display_name=user.get("full_name"),
            follower_count=user.get("follower_count"),
            biography=user.get("biography"),
            metadata={
                "is_verified": user.get("is_verified"),
                "profile_pic_url": user.get("profile_pic_url"),
            },
        )

    async def _fetch_page(self, url: str, *, ua_offset: int = 0, use_cookies: bool = True) -> tuple[str, str]:
        """Fetch a Threads page and return (html, final_url).

        Args:
            url: The page to fetch.
            ua_offset: Index of the first user agent to try. Callers rotate this
                so that a retry lands on a different user agent -- the empty-shell
                response appears to depend on which variant is presented, so
                repeating the same one can simply repeat the failure.
            use_cookies: Whether to send the browser session. The GraphQL
                timeline query has to be anonymous: a logged-in request is
                answered with a different, non-paginating query.

        Returns:
            The page HTML and the final URL.  A share link is followed to the
            canonical post URL, so the final URL is not the one that was asked
            for.
        """
        headers = page_headers(THREADS_USER_AGENTS[ua_offset])
        cookie_header = self._cookie_header() if use_cookies else ""
        if cookie_header:
            headers["Cookie"] = cookie_header

        last_error: Exception | None = None
        agents = THREADS_USER_AGENTS[ua_offset:] + THREADS_USER_AGENTS[:ua_offset]
        for user_agent in agents:
            request_headers = {**headers, "User-Agent": user_agent}
            try:
                response = await self.ctx.http.get(url, headers=request_headers, follow_redirects=True)
            except httpx.HTTPError as exc:
                last_error = exc
                self.log.debug("HTTP error with UA %r: %s", user_agent, exc)
                continue

            if response.status_code == 404:
                raise UnsupportedTargetError(f"Threads page not found: {url}")
            if response.status_code == 429:
                cooldown = self._register_cooldown("threads.net")
                raise RateLimitError(
                    f"Threads throttled us (HTTP 429); cooling down for {cooldown:.0f} s. "
                    "Retrying now is what causes the flag -- wait it out."
                )
            if response.status_code == 403:
                raise AuthError(
                    "Threads returned a 403 block. The request may require an authenticated session."
                )
            response.raise_for_status()

            # A redirect to a login route means the session was rejected. The response body
            # itself is never sniffed for the words "login" or "session": the page embeds the
            # Meta "Comet" bundle, which contains those strings in ordinary route and component
            # names, so a body sniff fires on every healthy response.
            final_path = response.url.path.lower()
            if "/accounts/" in final_path or "/login" in final_path:
                raise AuthError(
                    "Threads redirected to a login page. "
                    "Provide an authenticated session or try a different network."
                )
            return response.text, str(response.url)

        raise ParseError(f"Failed to fetch the Threads page {url}: {last_error}") from last_error

    def _cookie_header(self) -> str:
        """Build the ``Cookie`` header for a Threads request.

        The authenticated Threads session lives on ``.threads.com`` while the
        site is served from ``threads.net``. The cookie store scopes cookies by
        domain, so the ``threads.net`` request would otherwise carry no session
        at all. Both domains are collected here and sent together.

        Returns:
            A ``Cookie`` header value, empty when there are no cookies.
        """
        provider = self.ctx.cookies
        if provider is None:
            return ""
        merged: dict[str, str] = {}
        for domain in ("threads.net", "threads.com"):
            try:
                merged.update(provider.cookies_for(domain))
            except Exception:
                self.log.debug("could not read %s cookies", domain, exc_info=True)
        if not merged:
            return ""
        return "; ".join(f"{name}={value}" for name, value in merged.items())

    def _register_cooldown(self, host: str) -> float:
        """Start a cooldown for ``host`` on the shared pacer, when there is one.

        Args:
            host: The host that throttled us.

        Returns:
            The cooldown length in seconds.
        """
        pacer = self.ctx.pacer
        if pacer is None:
            return 0.0
        return pacer.on_throttled(f"https://{host}/")

    def _empty_payload_error(self, username: str, html: str, attempts: int) -> ParseError:
        """Build the error for a profile page that carried no posts.

        A private Threads profile is the common cause and it is not an error at
        all -- Threads simply does not server-render a private profile's posts.
        Say so, rather than blaming the user agent.

        Args:
            username: The requested handle.
            html: The page HTML that carried no posts.
            attempts: How many retries were made.

        Returns:
            The error to raise.
        """
        blocks = _SCRIPT_JSON_RE.findall(html)
        user = self._profile_user(html, username)
        if user is not None and user.get("text_post_app_is_private") is True:
            return ParseError(
                f"@{username} is a private Threads profile. Threads does not "
                "server-render private profiles, so their posts are not in the "
                "public page. Reading them needs the GraphQL transport "
                "(tracked in docs/ROADMAP.md as M2) or the official Threads API."
            )
        return ParseError(
            f"Threads returned {len(html)} bytes and {len(blocks)} "
            f"application/json script block(s), but no post objects were found "
            f"after {attempts} retr(ies). The mobile User-Agent may have been "
            "rejected, or the response may be a login wall."
        )

    def _missing_post_error(self, username: str, code: str, html: str) -> ParseError:
        """Build the error for a post that the payload did not carry.

        A post page that does not contain the post it was asked for is either a
        deleted post, a private account, or a link that does not point at a
        post at all.  Say which, rather than reporting an empty payload.

        Args:
            username: The handle the post was expected to belong to.
            code: The short code of the post.
            html: The page HTML that was fetched.

        Returns:
            The error to raise.
        """
        if self._is_private_profile(html, username):
            return ParseError(
                f"@{username} is a private Threads profile. Threads does not "
                "server-render private profiles, so their posts are not in the "
                "public page."
            )
        return ParseError(
            f"The Threads post {code!r} of @{username} was not in the payload "
            f"({len(html)} bytes). The post may have been deleted, or it may "
            "belong to a private account."
        )

    def _is_private_profile(self, html: str, username: str) -> bool:
        """Return whether the profile is private.

        Args:
            html: The page HTML.
            username: The requested handle.

        Returns:
            ``True`` when the profile's user object marks it private.
        """
        user = self._profile_user(html, username)
        return user is not None and user.get("text_post_app_is_private") is True

    def _profile_user(self, html: str, username: str) -> dict[str, Any] | None:
        """Return the profile's user object from the page payload.

        Args:
            html: The page HTML.
            username: The requested handle.

        Returns:
            The user object, or ``None`` when it is not in the payload.
        """
        for block in _SCRIPT_JSON_RE.findall(html):
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            user = _find_profile_user(obj, username)
            if user is not None:
                return user
        return None

    def _parse_posts(self, html: str) -> list[dict[str, Any]]:
        """Parse ``html`` and return the list of unique post dicts found."""
        blocks = _SCRIPT_JSON_RE.findall(html)
        self.log.debug("Found %d application/json script block(s)", len(blocks))

        posts: list[dict[str, Any]] = []
        seen_pks: set[int | str] = set()
        for index, block in enumerate(blocks):
            try:
                obj = json.loads(block)
            except json.JSONDecodeError as exc:
                self.log.debug("Skipping non-JSON script block #%d: %s", index, exc)
                continue
            _walk_json(obj, posts, seen_pks)

        return posts
