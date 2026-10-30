"""Instagram crawler using instaloader for public profile posts.

The crawler discovers posts and profile metadata only; it never downloads media.
instaloader is imported lazily so the rest of the package works when it is not
installed.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from osint_scraper.crawlers.base import Crawler, register_crawler
from osint_scraper.crawlers.instagram_api import (
    SURFACE_HIGHLIGHTS,
    SURFACE_IGTV,
    SURFACE_POSTS,
    SURFACE_STORIES,
    SURFACE_TAGGED,
    build_post,
    highlight_reel_id,
    highlight_tray,
    highlights_tray_path,
    reels_items,
    reels_media_path,
    stories_path,
)
from osint_scraper.exceptions import (
    AuthError,
    ParseError,
    RateLimitError,
    UnsupportedTargetError,
)
from osint_scraper.filepath import extension_for
from osint_scraper.models import MediaKind, MediaRef, Post, ProfileInfo, stamp_indices
from osint_scraper.ratelimit import looks_throttled

__all__ = [
    "SURFACE_HIGHLIGHTS",
    "SURFACE_IGTV",
    "SURFACE_POSTS",
    "SURFACE_STORIES",
    "SURFACE_TAGGED",
    "InstagramCrawler",
    "InstagramTarget",
    "parse_target",
]


def _load_instaloader() -> Any:
    """Return the instaloader module, raising a clear error if absent."""
    try:
        import instaloader
    except ImportError as exc:
        msg = "Instagram crawling requires instaloader. Install it with: pip install instaloader"
        raise ImportError(msg) from exc
    return instaloader


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._\-]+$")
_NON_PROFILE_SEGMENTS = frozenset(
    {"p", "reel", "reels", "stories", "tv", "explore", "accounts", "direct", "about"}
)
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am"})
"""Hosts an Instagram target URL may live on.

A URL on any other host is not an Instagram target at all.  Without this the
first path segment of a foreign URL would be taken as a username -- a Threads
share link would turn into the Instagram user "share".
"""
_AUTH_MESSAGE = (
    "Instagram requires an authenticated session for this target. "
    "Log in to instagram.com in your browser and re-run with --cookies (browser: brave)."
)
_POSTS_SENTINEL = object()
_INSTAGRAM_API_URL = "https://www.instagram.com/"
"""The host the iPhone API is reached through, used for pacing."""

_SURFACE_BY_SEGMENT = {
    "tagged": SURFACE_TAGGED,
    "highlights": SURFACE_HIGHLIGHTS,
    "igtv": SURFACE_IGTV,
    "tv": SURFACE_IGTV,
}
"""The profile sub-paths that select a surface."""


@dataclass(frozen=True, slots=True)
class InstagramTarget:
    """A parsed Instagram target: the account and the surface to scrape.

    Attributes:
        username: The account handle.
        surface: One of the ``SURFACE_*`` constants. A bare handle or a profile
            URL selects :data:`SURFACE_POSTS`.
    """

    username: str
    surface: str = SURFACE_POSTS


def parse_target(target: str) -> InstagramTarget:
    """Parse an Instagram handle or URL into an account and a surface.

    The URL selects the surface, the way ``yt-dlp`` selects by URL. Recognised
    profile sub-paths are ``tagged``, ``highlights``, ``igtv``/``tv`` and the
    ``stories/<user>`` form (which puts the handle in the second segment).

    Args:
        target: A bare handle, a profile URL, or a surface URL.

    Returns:
        The parsed target.

    Raises:
        UnsupportedTargetError: When ``target`` is empty, malformed, on a
            non-Instagram host, or points at something that is not a profile.
    """
    stripped = target.strip()
    if not stripped:
        raise UnsupportedTargetError("Empty Instagram target")
    lowered = stripped.lower()
    if lowered.startswith(("http://", "https://")):
        username, surface = _parse_url(stripped)
    else:
        username = stripped.lstrip("@")
        surface = SURFACE_POSTS
    if not _USERNAME_RE.match(username):
        raise UnsupportedTargetError(f"Invalid Instagram username: {username}")
    return InstagramTarget(username=username, surface=surface)


def _parse_url(target: str) -> tuple[str, str]:
    """Return the ``(username, surface)`` a target URL names.

    Args:
        target: An absolute Instagram URL.

    Returns:
        The handle and the surface.

    Raises:
        UnsupportedTargetError: When the URL is on another host, carries no
            path, or points at a post/route rather than a profile.
    """
    parsed = urlparse(target)
    host = parsed.netloc.lower().split(":")[0]
    if host not in _INSTAGRAM_HOSTS:
        raise UnsupportedTargetError(f"Not an Instagram URL: {target}")
    path = parsed.path.strip("/")
    if not path:
        raise UnsupportedTargetError(f"Cannot extract username from Instagram URL: {target}")
    parts = [part for part in path.split("/") if part]
    first = parts[0].lower()

    # ``stories/<user>/`` puts the handle in the second segment, so it has to be
    # recognised before the generic non-profile rejection below.
    if first == "stories":
        if len(parts) < 2 or parts[1].lower() in _NON_PROFILE_SEGMENTS:
            raise UnsupportedTargetError(f"Cannot extract username from Instagram URL: {target}")
        return parts[1], SURFACE_STORIES
    if first in _NON_PROFILE_SEGMENTS:
        raise UnsupportedTargetError(f"Instagram URL points to a {first}, not a profile: {target}")

    username = parts[0]
    if len(parts) >= 2:
        surface = _SURFACE_BY_SEGMENT.get(parts[1].lower())
        if surface is not None:
            return username, surface
    return username, SURFACE_POSTS


@register_crawler
class InstagramCrawler(Crawler):
    """Discover public Instagram profile posts via instaloader."""

    PLATFORM = "instagram"
    DISPLAY_NAME = "Instagram"
    REQUIRES_AUTH = False
    SUPPORTS_LIMIT = True

    async def crawl(self, target: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        """Yield posts for ``target`` as they are discovered.

        Args:
            target: Instagram username or profile URL.
            limit: Maximum number of posts to yield, or ``None`` for no limit.

        Yields:
            :class:`~osint_scraper.models.Post` objects, including text-only posts.
        """
        parsed = parse_target(target)
        username = parsed.username
        instaloader = await asyncio.to_thread(_load_instaloader)
        session = self._instagram_session()
        if session.get("sessionid"):
            self.log.info("using an authenticated Instagram session")
        else:
            self.log.info("no Instagram session cookie available; crawling anonymously")
        try:
            loader, profile = await asyncio.to_thread(_load_profile, instaloader, username, session)
        except Exception as exc:
            translated = _translate_instaloader_error(exc)
            if isinstance(translated, RateLimitError):
                self._register_cooldown("instagram.com")
            raise translated from exc

        if parsed.surface == SURFACE_POSTS:
            posts = await self._fetch_accessor(lambda: profile.get_posts())
            async for post in self._iter_profile_posts(posts, username, limit):
                yield post
            return

        if parsed.surface == SURFACE_TAGGED:
            tagged = await self._fetch_accessor(lambda: profile.get_tagged_posts())
            async for post in self._iter_profile_posts(tagged, username, limit, surface=SURFACE_TAGGED):
                yield post
            return

        if parsed.surface == SURFACE_IGTV:
            collected = await self._collect_igtv(profile, username)
        elif parsed.surface == SURFACE_STORIES:
            self._require_iphone_session(loader)
            collected = await self._collect_stories(loader.context, profile, username, limit)
        else:
            self._require_iphone_session(loader)
            collected = await self._collect_highlights(loader.context, profile, username, limit)

        count = 0
        for post in collected:
            if limit is not None and count >= limit:
                break
            yield post
            count += 1
        if count == 0:
            self.log.warning("Profile %s has no %s items", username, parsed.surface)

    async def _fetch_accessor(self, factory: Callable[[], Any]) -> Any:
        """Call an instaloader accessor, translating platform errors.

        Some of instaloader's accessors fetch **eagerly** -- ``get_posts()``
        and ``get_tagged_posts()`` both read the profile's metadata before
        returning an iterator -- so the request happens here rather than
        during iteration.  It has to be translated here too, or a throttle
        escapes as a raw instaloader exception instead of registering the
        cooldown that stops the next run.

        Args:
            factory: The accessor to call.

        Returns:
            Whatever the accessor returned.

        Raises:
            RateLimitError: When Instagram throttled the request.
            AuthError: When the session was rejected.
        """
        try:
            return await asyncio.to_thread(factory)
        except Exception as exc:
            translated = _translate_instaloader_error(exc)
            if isinstance(translated, RateLimitError):
                self._register_cooldown("instagram.com")
            raise translated from exc

    async def _iter_profile_posts(
        self,
        posts: Iterator[Any],
        username: str,
        limit: int | None,
        *,
        surface: str | None = None,
    ) -> AsyncIterator[Post]:
        """Yield instaloader posts lazily, translating platform errors.

        Args:
            posts: The instaloader post iterator.
            username: The account being crawled.
            limit: Maximum number of posts to yield, or ``None`` for no limit.
            surface: The surface to record in the metadata, when not ``posts``.

        Yields:
            The converted posts.
        """
        count = 0
        while limit is None or count < limit:
            try:
                post = await asyncio.to_thread(_next_post, posts)
                if post is _POSTS_SENTINEL:
                    break
                # Building the post can itself reach the network: instaloader
                # fetches a post's full metadata when its node is partial, and
                # that request is throttled like any other.  It has to happen
                # inside the guard, or a throttle escapes as a raw instaloader
                # exception instead of registering the cooldown.
                built = self._build_post(post, username, surface=surface)
            except Exception as exc:
                translated = _translate_instaloader_error(exc)
                if isinstance(translated, RateLimitError):
                    self._register_cooldown("instagram.com")
                raise translated from exc
            yield built
            count += 1

        if count == 0:
            self.log.warning("Profile %s has no posts", username)

    async def _collect_stories(
        self,
        context: Any,
        profile: Any,
        username: str,
        limit: int | None,
    ) -> list[Post]:
        """Fetch the account's current stories over the iPhone API.

        Args:
            context: The instaloader context the request goes through.
            profile: The resolved instaloader profile.
            username: The account handle.
            limit: Stop converting after this many items, or ``None``.

        Returns:
            One post per story item.
        """
        user_id = _profile_user_id(profile)
        payload = await self._iphone_json(context, stories_path(user_id))
        posts: list[Post] = []
        for item in reels_items(payload, user_id):
            if limit is not None and len(posts) >= limit:
                break
            posts.append(build_post(item, username, surface=SURFACE_STORIES))
        return posts

    async def _collect_highlights(
        self,
        context: Any,
        profile: Any,
        username: str,
        limit: int | None,
    ) -> list[Post]:
        """Fetch the account's highlights and every item they contain.

        The tray costs one request and each highlight one more. They are issued
        one at a time -- the account-level throttle does not allow a burst.
        Once ``limit`` items have been collected no further highlight is
        fetched.

        Args:
            context: The instaloader context the requests go through.
            profile: The resolved instaloader profile.
            username: The account handle.
            limit: Stop after this many items, or ``None``.

        Returns:
            One post per item across every fetched highlight.
        """
        user_id = _profile_user_id(profile)
        tray_payload = await self._iphone_json(context, highlights_tray_path(user_id))

        posts: list[Post] = []
        for entry in highlight_tray(tray_payload):
            if limit is not None and len(posts) >= limit:
                break
            highlight_id = entry.get("id")
            if not isinstance(highlight_id, str) or not highlight_id:
                continue
            reel_id = highlight_reel_id(highlight_id)
            payload = await self._iphone_json(context, reels_media_path(reel_id))
            title = entry.get("title")
            for item in reels_items(payload, reel_id):
                if limit is not None and len(posts) >= limit:
                    break
                post = build_post(item, username, surface=SURFACE_HIGHLIGHTS)
                post.metadata["highlight_id"] = highlight_id
                if isinstance(title, str):
                    post.metadata["highlight_title"] = title
                posts.append(post)
        return posts

    async def _collect_igtv(self, profile: Any, username: str) -> list[Post]:
        """Fetch the account's IGTV videos.

        IGTV also appears in the regular profile feed. The surface is kept so
        the break is reported plainly rather than surfacing as an empty result.

        Args:
            profile: The resolved instaloader profile.
            username: The account handle.

        Returns:
            One post per IGTV video.

        Raises:
            ParseError: When instaloader's IGTV reader fails upstream, which it
                currently does with ``KeyError: 'edges'``.
        """
        try:
            items = await asyncio.to_thread(_list_igtv_posts, profile)
        except KeyError as exc:
            raise ParseError(
                "Instagram's IGTV surface is broken upstream: "
                "instaloader 4.16 raises KeyError('edges') inside "
                "profile.get_igtv_posts(). It is not an empty account and retrying "
                "will not help. IGTV videos also appear in the regular profile feed; "
                "scrape the profile URL instead."
            ) from exc
        except Exception as exc:
            translated = _translate_instaloader_error(exc)
            if isinstance(translated, RateLimitError):
                self._register_cooldown("instagram.com")
            raise translated from exc
        return [self._build_post(item, username, surface=SURFACE_IGTV) for item in items]

    async def _iphone_json(self, context: Any, path: str) -> Any:
        """Call ``get_iphone_json``, paced and with errors translated.

        Args:
            context: The instaloader context.
            path: The iPhone-API path to request.

        Returns:
            The decoded response.

        Raises:
            RateLimitError: When Instagram throttled the request.
            AuthError: When the session was rejected.
        """
        await self._apply_politeness()
        try:
            return await asyncio.to_thread(context.get_iphone_json, path, {})
        except Exception as exc:
            translated = _translate_instaloader_error(exc)
            if isinstance(translated, RateLimitError):
                self._register_cooldown("instagram.com")
            raise translated from exc

    async def _apply_politeness(self) -> None:
        """Wait until Instagram may be contacted again.

        Raises:
            RateLimitError: When the host is in a cooldown.
        """
        pacer = self.ctx.pacer
        if pacer is None:
            return
        await pacer.wait(_INSTAGRAM_API_URL)

    def _require_iphone_session(self, loader: Any) -> None:
        """Raise :class:`AuthError` when the iPhone API cannot be used.

        ``get_iphone_json`` needs both an authenticated session -- it
        synthesises the bearer token from ``ds_user_id`` and ``sessionid`` --
        and instaloader's iPhone support.

        Args:
            loader: The configured instaloader instance.

        Raises:
            AuthError: When either requirement is unmet.
        """
        context = getattr(loader, "context", None)
        if not getattr(context, "iphone_support", False):
            raise AuthError(_AUTH_MESSAGE)
        if not getattr(context, "is_logged_in", False):
            raise AuthError(_AUTH_MESSAGE)

    async def profile(self, target: str) -> ProfileInfo:
        """Fetch profile-level metadata for ``target``.

        Args:
            target: Instagram username or profile URL.

        Returns:
            :class:`~osint_scraper.models.ProfileInfo` with available metadata.
        """
        username = _normalize_target(target)
        instaloader = await asyncio.to_thread(_load_instaloader)
        session = self._instagram_session()
        profile = await asyncio.to_thread(_resolve_profile, instaloader, username, session)
        return self._build_profile_info(profile, username)

    def _register_cooldown(self, host: str) -> None:
        """Start a cooldown for ``host`` on the shared pacer, when there is one.

        Args:
            host: The host that throttled us.
        """
        pacer = self.ctx.pacer
        if pacer is None:
            return
        pacer.on_throttled(f"https://{host}/")

    def _instagram_session(self) -> dict[str, str]:
        """Return the Instagram cookies available to this crawl.

        Returns:
            The cookie dict for ``instagram.com``, empty when no cookie provider
            is configured or the domain holds no cookies.
        """
        provider = self.ctx.cookies
        if provider is None:
            return {}
        try:
            return dict(provider.cookies_for("instagram.com"))
        except Exception:
            self.log.debug("could not read Instagram cookies", exc_info=True)
            return {}

    def _build_post(self, post: Any, username: str, *, surface: str | None = None) -> Post:
        """Convert an instaloader post into a :class:`Post`.

        Args:
            post: The instaloader post.
            username: The account the post belongs to.
            surface: The surface to record in the metadata. ``None`` leaves the
                metadata exactly as the feed path has always produced it.

        Returns:
            The converted post.
        """
        shortcode = post.shortcode
        post_id = _extract_post_id(post)
        permalink = f"https://www.instagram.com/p/{shortcode}/"
        caption = getattr(post, "caption", None)
        timestamp = _extract_timestamp(post)
        media = self._build_media_list(post)
        metadata = self._build_metadata(post)
        if surface is not None:
            metadata["surface"] = surface
        return Post(
            platform=self.PLATFORM,
            target=username,
            post_id=post_id,
            shortcode=shortcode,
            permalink=permalink,
            caption=caption,
            timestamp=timestamp,
            media=media,
            metadata=metadata,
        )

    def _build_media_list(self, post: Any) -> list[MediaRef]:
        """Build media references from instaloader's internal node structure.

        Every ref is stamped with its zero-based position, and with the media
        id the platform uses for the object when one is available.

        Args:
            post: The instaloader post.

        Returns:
            The post's media references in the order the post presents them.
        """
        typename = getattr(post, "typename", None)
        node = getattr(post, "_node", None)
        if typename == "GraphSidecar":
            refs = self._build_sidecar_media(post)
        elif getattr(post, "is_video", False) or getattr(post, "video_url", None):
            refs = _build_video_media(node, media_id=_extract_post_id(post))
        else:
            refs = _build_image_media(node, media_id=_extract_post_id(post))
        return stamp_indices(refs)

    def _build_sidecar_media(self, post: Any) -> list[MediaRef]:
        """Return one MediaRef per child of a sidecar/carousel post.

        A carousel child's full metadata -- its own media id, its media type
        and its dimensions -- lives in the parent's ``carousel_media`` list.
        instaloader's own sidecar objects do not carry it: they expose only
        ``display_url``, ``video_url`` and ``is_video``.

        The parent carries that list only for **anonymous** reads. An
        authenticated profile feed returns thin children instead. instaloader
        fetches the post's full metadata when a carousel contains a video --
        a fetch it makes anyway -- so the richer children are read from the
        metadata it already has rather than requested again. No extra request
        is made here.

        Args:
            post: The instaloader sidecar post.

        Returns:
            One reference per child that exposes a usable URL.
        """
        refs: list[MediaRef] = []
        try:
            sidecar_nodes = list(post.get_sidecar_nodes())
        except Exception:
            self.log.exception(
                "Failed to read sidecar nodes for post %s",
                getattr(post, "shortcode", "?"),
            )
            sidecar_nodes = []

        children = _sidecar_child_nodes(post)
        for child in children:
            ref = _build_sidecar_child_media(child)
            if ref is not None:
                refs.append(ref)
        if refs:
            return refs

        # Fall back to the thin children: the URL and the video flag only.
        for child in sidecar_nodes:
            is_video = getattr(child, "is_video", False)
            url = getattr(child, "video_url", None) if is_video else getattr(child, "display_url", None)
            if not url:
                continue
            kind: MediaKind = "video" if is_video else "image"
            mime = "video/mp4" if is_video else "image/jpeg"
            refs.append(
                MediaRef(
                    url=url,
                    kind=kind,
                    mime=mime,
                    extension=extension_for(mime, url),
                    media_id=_sidecar_media_id(child),
                )
            )
        if refs:
            return refs

        # instaloader could not resolve the children -- use the raw edges,
        # which carry the same URL and video flag.
        for node in _sidecar_edge_nodes(post):
            is_video = bool(node.get("is_video"))
            url = node.get("video_url") if is_video else node.get("display_url")
            if not url:
                continue
            mime = "video/mp4" if is_video else "image/jpeg"
            refs.append(
                MediaRef(
                    url=url,
                    kind="video" if is_video else "image",
                    mime=mime,
                    extension=extension_for(mime, url),
                )
            )
        return refs

    def _build_metadata(self, post: Any) -> dict[str, Any]:
        """Collect optional engagement and ownership metadata.

        The engagement counts are read straight out of the GraphQL node that
        instaloader already fetched. Reading ``post.likes`` or ``post.comments``
        instead would trigger an *extra* metadata request per post -- one that
        fails outright on some private-account responses, and that would double
        the request rate for every post.

        Args:
            post: The instaloader post.

        Returns:
            A metadata mapping, with only the keys that have a value.
        """
        metadata: dict[str, Any] = {}
        node = getattr(post, "_node", None)
        if isinstance(node, dict):
            likes = _first_int(node, "edge_media_preview_like.count", "fb_like_count")
            if likes is not None:
                metadata["like_count"] = likes
            comments = _first_int(node, "comment_count", "fb_comment_count")
            if comments is not None:
                metadata["comment_count"] = comments
        typename = getattr(post, "typename", None)
        if typename is not None:
            metadata["typename"] = typename
        owner = getattr(post, "owner_username", None)
        if owner is not None:
            metadata["owner_username"] = owner
        is_video = getattr(post, "is_video", None)
        if is_video is not None:
            metadata["is_video"] = bool(is_video)
        return metadata

    def _build_profile_info(self, profile: Any, username: str) -> ProfileInfo:
        """Convert an instaloader profile into a :class:`ProfileInfo`."""
        metadata: dict[str, Any] = {}
        is_verified = getattr(profile, "is_verified", None)
        if is_verified is not None:
            metadata["is_verified"] = bool(is_verified)
        external_url = getattr(profile, "external_url", None)
        if external_url is not None:
            metadata["external_url"] = external_url
        business_category = getattr(profile, "business_category_name", None)
        if business_category is not None:
            metadata["business_category"] = business_category
        return ProfileInfo(
            platform=self.PLATFORM,
            target=username,
            display_name=getattr(profile, "full_name", None) or None,
            follower_count=getattr(profile, "followers", None),
            post_count=getattr(profile, "mediacount", None),
            biography=getattr(profile, "biography", None),
            metadata=metadata,
        )


def _normalize_target(target: str) -> str:
    """Return a bare Instagram username from a handle or URL.

    Args:
        target: A handle, profile URL, or surface URL.

    Returns:
        The bare username.

    Raises:
        UnsupportedTargetError: When ``target`` cannot be interpreted as an
            Instagram profile.
    """
    return parse_target(target).username


def _load_profile(
    instaloader: Any,
    username: str,
    session: dict[str, str] | None = None,
) -> tuple[Any, Any]:
    """Build a loader and resolve a profile, translating login walls.

    The loader is returned alongside the profile because the iPhone-API
    surfaces need its context to issue their requests.

    Args:
        instaloader: The ``instaloader`` module.
        username: Profile to resolve.
        session: Cookie dict to authenticate with, when available.

    Returns:
        The loader and the profile.

    Raises:
        AuthError: When Instagram answers with a login wall.
    """
    loader = _build_loader(instaloader, session)
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except Exception as exc:
        if _is_login_error(exc):
            raise AuthError(_AUTH_MESSAGE) from exc
        raise
    return loader, profile


def _resolve_profile(instaloader: Any, username: str, session: dict[str, str] | None = None) -> Any:
    """Fetch an instaloader profile, translating login walls into AuthError.

    Args:
        instaloader: The ``instaloader`` module.
        username: Profile to resolve.
        session: Cookie dict to authenticate with, when available.

    Returns:
        An ``instaloader`` profile object.

    Raises:
        AuthError: When Instagram answers with a login wall.
    """
    _loader, profile = _load_profile(instaloader, username, session)
    return profile


def _profile_user_id(profile: Any) -> str:
    """Return the account's numeric id as a string.

    Args:
        profile: The resolved instaloader profile.

    Returns:
        The numeric user id.

    Raises:
        ParseError: When the profile carries no usable id.
    """
    user_id = getattr(profile, "userid", None)
    if isinstance(user_id, str) and user_id:
        return user_id
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return str(user_id)
    raise ParseError("Instagram returned a profile without a numeric user id")


def _list_igtv_posts(profile: Any) -> list[Any]:
    """Drain ``profile.get_igtv_posts()``, forcing any deferred error.

    instaloader's reader is a generator, so the upstream ``KeyError`` surfaces
    during iteration rather than at call time. Materialising the list here is
    what keeps that error inside the caller's ``try``.

    Args:
        profile: The resolved instaloader profile.

    Returns:
        The IGTV posts.
    """
    return list(profile.get_igtv_posts())


def _build_loader(instaloader: Any, session: dict[str, str] | None) -> Any:
    """Build an instaloader instance, authenticated when a session is available.

    instaloader talks to Instagram over its own ``requests`` session, so the
    browser cookies have to be handed to it directly rather than through the
    shared HTTP client.

    Args:
        instaloader: The ``instaloader`` module.
        session: Cookie dict for ``instagram.com``, or ``None`` to stay anonymous.

    Returns:
        A configured ``instaloader.Instaloader`` instance.
    """
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )
    if not session:
        return loader
    if not session.get("sessionid"):
        return loader
    if not session.get("csrftoken"):
        # instaloader requires a CSRF token to build the session headers.
        return loader
    loader.load_session(session.get("ds_user_id") or "session", dict(session))
    return loader


def _is_login_error(exc: Exception) -> bool:
    """Return whether an instaloader exception indicates a login wall."""
    class_name = type(exc).__name__
    if class_name in {
        "QueryReturnedNotFoundException",
        "LoginRequiredException",
        "QueryReturnedNotFoundError",
        "LoginRequiredError",
    }:
        return True
    message = str(exc).lower()
    return any(term in message for term in ("login", "authenticated", "session", "sessionid"))


def _first_int(node: dict[str, Any], *paths: str) -> int | None:
    """Return the first integer found at any of ``paths``.

    Args:
        node: The GraphQL node.
        *paths: Dotted paths to try, in order of preference.

    Returns:
        The first integer value found, or ``None``.
    """
    for path in paths:
        current: Any = node
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if isinstance(current, int):
            return current
    return None


def _is_throttle_error(exc: Exception) -> bool:
    """Return whether an instaloader exception indicates throttling.

    Instagram answers a burst of requests with ``feedback_required`` and
    ``spam: true``, or with a 429. Both mean the same thing: stop, and wait.

    Args:
        exc: The exception raised by instaloader.

    Returns:
        ``True`` when the exception carries a throttle marker.
    """
    message = str(exc).lower()
    if any(marker in message for marker in ("429", "too many requests", "rate limit")):
        return True
    return looks_throttled(message)


def _translate_instaloader_error(exc: Exception) -> Exception:
    """Turn instaloader login-wall and throttle errors into domain exceptions."""
    if _is_throttle_error(exc):
        return RateLimitError(
            "Instagram throttled this account (feedback_required / spam flag). "
            "This is a signal to stop, not to retry: the cooldown is enforced by "
            "the pacer and survives across runs. Try again after the cooldown, "
            "and keep the request rate low."
        )
    if _is_login_error(exc):
        return AuthError(_AUTH_MESSAGE)
    return exc


def _next_post(posts: Iterator[Any]) -> Any:
    """Return the next post, using a sentinel to avoid raising StopIteration across threads."""
    try:
        return next(posts)
    except StopIteration:
        return _POSTS_SENTINEL


def _extract_post_id(post: Any) -> str:
    """Return the Instagram media id when exposed, otherwise the shortcode."""
    node = getattr(post, "_node", None)
    if isinstance(node, dict):
        node_id = node.get("id")
        if node_id:
            return str(node_id)
    shortcode: str = post.shortcode
    return shortcode


def _extract_timestamp(post: Any) -> int | None:
    """Return the UTC publication timestamp when available."""
    date_utc = getattr(post, "date_utc", None)
    if date_utc is None:
        return None
    return int(date_utc.timestamp())


def _sidecar_child_nodes(post: Any) -> list[dict[str, Any]]:
    """Return the carousel's child nodes, with their full metadata.

    The children live in the parent's ``carousel_media`` list.  When instaloader
    has already fetched the post's full metadata -- it does so for a carousel
    that contains a video -- the richer list is read from there.

    The cached metadata is read **directly** rather than through instaloader's
    ``_full_metadata`` property: that property fetches the metadata on first
    access, and this crawler must never make a request of its own.

    Args:
        post: The instaloader sidecar post.

    Returns:
        The child nodes, or an empty list when neither source carries them.
    """
    for source in (getattr(post, "_node", None), getattr(post, "_full_metadata_dict", None)):
        if not isinstance(source, dict):
            continue
        children = source.get("carousel_media")
        if not isinstance(children, list):
            continue
        nodes = [child for child in children if isinstance(child, dict)]
        if nodes:
            return nodes
    return []


def _sidecar_edge_nodes(post: Any) -> list[dict[str, Any]]:
    """Return the raw sidecar edge children of a post.

    These carry the media URL and the video flag, but no media id and no
    dimensions.  They are the last resort when instaloader could not resolve
    the children itself.

    Args:
        post: The instaloader sidecar post.

    Returns:
        The child nodes, or an empty list when the post carries none.
    """
    node = getattr(post, "_node", None)
    if not isinstance(node, dict):
        return []
    edges = node.get("edge_sidecar_to_children")
    if not isinstance(edges, dict):
        return []
    children: list[dict[str, Any]] = []
    for edge in edges.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        child = edge.get("node")
        if isinstance(child, dict):
            children.append(child)
    return children


def _build_sidecar_child_media(child: dict[str, Any]) -> MediaRef | None:
    """Build one MediaRef from a carousel child's own metadata.

    Args:
        child: One child node from the parent's ``carousel_media`` list.

    Returns:
        The reference, or ``None`` when the child exposes no usable URL.
    """
    is_video = child.get("media_type") == 2 or bool(child.get("is_video"))
    if is_video:
        url = _pick_best_url(child, "video_versions")
        width, height = _best_dimensions(child, "video_versions")
        mime = "video/mp4"
    else:
        url = _pick_best_url(child, "image_versions2")
        width, height = _best_dimensions(child, "image_versions2")
        mime = "image/jpeg"
    if not url:
        return None
    media_id = child.get("id")
    if not isinstance(media_id, str) or not media_id:
        pk = child.get("pk")
        media_id = str(pk) if isinstance(pk, int) and not isinstance(pk, bool) else None
    return MediaRef(
        url=url,
        kind="video" if is_video else "image",
        width=width,
        height=height,
        mime=mime,
        extension=extension_for(mime, url),
        media_id=media_id,
    )


def _sidecar_media_id(child: Any) -> str | None:
    """Return the sidecar child's own media id when it exposes one.

    The child is an instaloader post-like object whose GraphQL node is reachable
    as ``child._node``. The node's ``"id"`` is used only when it is a non-empty
    string; the child's ``pk`` is the fallback. A missing id is never an error.

    Args:
        child: One sidecar node yielded by ``post.get_sidecar_nodes()``.

    Returns:
        The child's media id, or ``None`` when it has none.
    """
    node = getattr(child, "_node", None)
    if isinstance(node, dict):
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            return node_id
    pk = getattr(child, "pk", None)
    if isinstance(pk, str) and pk:
        return pk
    if isinstance(pk, int) and not isinstance(pk, bool):
        return str(pk)
    return None


def _build_video_media(node: dict[str, Any] | None, *, media_id: str | None = None) -> list[MediaRef]:
    """Return a single highest-resolution video MediaRef for a video post.

    Args:
        node: The post's GraphQL node.
        media_id: The platform's id for the media object, when known.

    Returns:
        A one-element list, or an empty list when the node carries no video URL.
    """
    url = _pick_best_url(node, "video_versions")
    if not url and isinstance(node, dict):
        url = node.get("video_url") or node.get("display_url")
    if not url:
        return []
    width, height = _best_dimensions(node, "video_versions")
    mime = "video/mp4"
    return [
        MediaRef(
            url=url,
            kind="video",
            width=width,
            height=height,
            mime=mime,
            extension=extension_for(mime, url),
            media_id=media_id,
        )
    ]


def _build_image_media(node: dict[str, Any] | None, *, media_id: str | None = None) -> list[MediaRef]:
    """Return a single highest-resolution image MediaRef for an image post.

    Args:
        node: The post's GraphQL node.
        media_id: The platform's id for the media object, when known.

    Returns:
        A one-element list, or an empty list when the node carries no image URL.
    """
    url = _pick_best_url(node, "image_versions2")
    if not url and isinstance(node, dict):
        url = node.get("display_url")
    if not url:
        return []
    width, height = _best_dimensions(node, "image_versions2")
    mime = "image/jpeg"
    return [
        MediaRef(
            url=url,
            kind="image",
            width=width,
            height=height,
            mime=mime,
            extension=extension_for(mime, url),
            media_id=media_id,
        )
    ]


def _pick_best_url(node: dict[str, Any] | None, field: str) -> str | None:
    """Select the largest candidate URL from ``video_versions`` or ``image_versions2``."""
    if not isinstance(node, dict):
        return None
    if field == "video_versions":
        candidates = node.get("video_versions") or []
    elif field == "image_versions2":
        candidates = (node.get("image_versions2") or {}).get("candidates") or []
    else:
        return None
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item.get("width", 0) * item.get("height", 0))
    url: str | None = best.get("url")
    return url


def _best_dimensions(node: dict[str, Any] | None, field: str) -> tuple[int | None, int | None]:
    """Return the width/height of the largest candidate, when available."""
    if not isinstance(node, dict):
        return None, None
    if field == "video_versions":
        candidates = node.get("video_versions") or []
    elif field == "image_versions2":
        candidates = (node.get("image_versions2") or {}).get("candidates") or []
    else:
        return None, None
    if not candidates:
        return None, None
    best = max(candidates, key=lambda item: item.get("width", 0) * item.get("height", 0))
    return best.get("width"), best.get("height")
