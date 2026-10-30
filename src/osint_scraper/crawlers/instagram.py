"""Instagram crawler using instaloader for public profile posts.

The crawler discovers posts and profile metadata only; it never downloads media.
instaloader is imported lazily so the rest of the package works when it is not
installed.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urlparse

from osint_scraper.crawlers.base import Crawler, register_crawler
from osint_scraper.exceptions import AuthError, RateLimitError, UnsupportedTargetError
from osint_scraper.filepath import extension_for
from osint_scraper.models import MediaKind, MediaRef, Post, ProfileInfo
from osint_scraper.ratelimit import looks_throttled


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
        username = _normalize_target(target)
        instaloader = await asyncio.to_thread(_load_instaloader)
        session = self._instagram_session()
        if session.get("sessionid"):
            self.log.info("using an authenticated Instagram session")
        else:
            self.log.info("no Instagram session cookie available; crawling anonymously")
        try:
            profile = await asyncio.to_thread(_resolve_profile, instaloader, username, session)
        except Exception as exc:
            translated = _translate_instaloader_error(exc)
            if isinstance(translated, RateLimitError):
                self._register_cooldown("instagram.com")
            raise translated from exc
        posts = await asyncio.to_thread(lambda: profile.get_posts())

        count = 0
        while limit is None or count < limit:
            try:
                post = await asyncio.to_thread(_next_post, posts)
            except Exception as exc:
                translated = _translate_instaloader_error(exc)
                if isinstance(translated, RateLimitError):
                    self._register_cooldown("instagram.com")
                raise translated from exc
            if post is _POSTS_SENTINEL:
                break
            yield self._build_post(post, username)
            count += 1

        if count == 0:
            self.log.warning("Profile %s has no posts", username)

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

    def _build_post(self, post: Any, username: str) -> Post:
        """Convert an instaloader post into a :class:`Post`."""
        shortcode = post.shortcode
        post_id = _extract_post_id(post)
        permalink = f"https://www.instagram.com/p/{shortcode}/"
        caption = getattr(post, "caption", None)
        timestamp = _extract_timestamp(post)
        media = self._build_media_list(post)
        metadata = self._build_metadata(post)
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
        """Build media references from instaloader's internal node structure."""
        typename = getattr(post, "typename", None)
        node = getattr(post, "_node", None)
        if typename == "GraphSidecar":
            return self._build_sidecar_media(post)
        if getattr(post, "is_video", False) or getattr(post, "video_url", None):
            return _build_video_media(node)
        return _build_image_media(node)

    def _build_sidecar_media(self, post: Any) -> list[MediaRef]:
        """Yield one MediaRef per child of a sidecar/carousel post."""
        refs: list[MediaRef] = []
        try:
            children = list(post.get_sidecar_nodes())
        except Exception:
            self.log.exception(
                "Failed to read sidecar nodes for post %s",
                getattr(post, "shortcode", "?"),
            )
            return refs
        for child in children:
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
    """Return a bare Instagram username from a handle or URL."""
    target = target.strip()
    if not target:
        raise UnsupportedTargetError("Empty Instagram target")
    lowered = target.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        candidate = _extract_username_from_url(target)
    else:
        candidate = target.lstrip("@")
    if not _USERNAME_RE.match(candidate):
        raise UnsupportedTargetError(f"Invalid Instagram username: {candidate}")
    return candidate


def _extract_username_from_url(target: str) -> str:
    """Extract the username segment from an Instagram profile URL."""
    parsed = urlparse(target)
    host = parsed.netloc.lower().split(":")[0]
    if host not in _INSTAGRAM_HOSTS:
        raise UnsupportedTargetError(f"Not an Instagram URL: {target}")
    path = parsed.path.strip("/")
    if not path:
        raise UnsupportedTargetError(f"Cannot extract username from Instagram URL: {target}")
    parts = path.split("/")
    first = parts[0].lower()
    if first in _NON_PROFILE_SEGMENTS:
        raise UnsupportedTargetError(f"Instagram URL points to a {first}, not a profile: {target}")
    return parts[0]


def _resolve_profile(instaloader: Any, username: str, session: dict[str, str] | None = None) -> Any:
    """Fetch an instaloader profile, translating login walls into AuthError.

    Args:
        instaloader: The ``instaloader`` module.
        username: Profile to resolve.
        session: Cookie dict to authenticate with, when available.

    Returns:
        An ``instaloader`` profile object.
    """
    loader = _build_loader(instaloader, session)
    try:
        return instaloader.Profile.from_username(loader.context, username)
    except Exception as exc:
        if _is_login_error(exc):
            raise AuthError(_AUTH_MESSAGE) from exc
        raise


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


def _build_video_media(node: dict[str, Any] | None) -> list[MediaRef]:
    """Return a single highest-resolution video MediaRef for a video post."""
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
        )
    ]


def _build_image_media(node: dict[str, Any] | None) -> list[MediaRef]:
    """Return a single highest-resolution image MediaRef for an image post."""
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
        )
    ]


def _pick_best_url(node: dict[str, Any] | None, field: str) -> str | None:
    """Select the largest candidate URL from ``video_versions`` or ``image_versions2``."""
    if not isinstance(node, dict):
        return None
    if field == "video_versions":
        candidates = node.get("video_versions") or []
    elif field == "image_versions2":
        candidates = node.get("image_versions2", {}).get("candidates") or []
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
        candidates = node.get("image_versions2", {}).get("candidates") or []
    else:
        return None, None
    if not candidates:
        return None, None
    best = max(candidates, key=lambda item: item.get("width", 0) * item.get("height", 0))
    return best.get("width"), best.get("height")
