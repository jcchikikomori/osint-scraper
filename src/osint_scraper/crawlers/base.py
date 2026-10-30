"""Base classes and shared contracts for platform crawlers.

A crawler is responsible for **discovery only**: turning a target handle into a
stream of :class:`~osint_scraper.models.Post` objects. It never writes files and
never downloads media — that is the :mod:`osint_scraper.downloader` job. Keeping
the split sharp is what makes both layers independently testable.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from osint_scraper.exceptions import UnsupportedTargetError
from osint_scraper.models import Post, ProfileInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

    from osint_scraper.config.settings import Settings
    from osint_scraper.ratelimit import Pacer


@runtime_checkable
class CookieProvider(Protocol):
    """Supplies HTTP cookies for a given domain.

    Implementations may read from a browser profile, a Netscape ``cookies.txt``
    file, or an in-memory dictionary.
    """

    def cookies_for(self, domain: str) -> dict[str, str]:
        """Return a mapping of cookie name to value applicable to ``domain``."""
        ...

    def cookie_header(self, domain: str) -> str:
        """Return a ``Cookie`` header value for ``domain``."""
        ...


@dataclass(slots=True)
class CrawlContext:
    """Everything a crawler needs that is not target-specific.

    Attributes:
        settings: Resolved application settings.
        http: Async HTTP client pre-configured with the shared headers and cookie
            hook. Crawlers may issue requests through it directly.
        cookies: Cookie provider used when a crawler needs cookies outside of the
            shared HTTP client (for example when shelling out to a tool). May be
            ``None`` when the crawl runs without cookies.
        pacer: Shared request pacer. Crawlers use it to space out their own
            requests and to register a cooldown when a platform pushes back.
            May be ``None``.
        logger: Logger scoped to the crawl.
        output_dir: Root directory the caller intends to write results into.
        user_agent: User agent string crawlers should present.
    """

    settings: Settings
    http: httpx.AsyncClient
    cookies: CookieProvider | None
    logger: logging.Logger
    output_dir: Path
    user_agent: str
    pacer: Pacer | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Crawler(ABC):
    """Base class for platform crawlers.

    Subclasses must set :attr:`PLATFORM` and implement :meth:`crawl`.
    """

    PLATFORM: ClassVar[str] = ""
    """Stable platform identifier, e.g. ``"instagram"``."""

    DISPLAY_NAME: ClassVar[str] = ""
    """Human readable platform name used in output paths and logs."""

    REQUIRES_AUTH: ClassVar[bool] = False
    """Whether the platform refuses unauthenticated profile access.

    When true, the crawler must raise
    :class:`~osint_scraper.exceptions.AuthError` with actionable remediation
    instructions if the session cookie is missing.
    """

    SUPPORTS_LIMIT: ClassVar[bool] = True
    """Whether the crawler honours the ``limit`` argument of :meth:`crawl`."""

    def __init__(self, ctx: CrawlContext) -> None:
        """Store the crawl context and derive a scoped logger.

        Args:
            ctx: Shared crawl context.
        """
        self.ctx = ctx
        self.log = ctx.logger.getChild(self.PLATFORM or type(self).__name__)

    @abstractmethod
    def crawl(self, target: str, *, limit: int | None = None) -> AsyncIterator[Post]:
        """Yield posts for ``target`` as they are discovered.

        Implementations are async generators: declare them with ``async def`` and
        ``yield`` inside, but the base signature is a plain ``def`` returning an
        :class:`~collections.abc.AsyncIterator`, because calling an async
        generator function does not return a coroutine.

        Args:
            target: Platform handle or profile URL.
            limit: Maximum number of posts to yield, or ``None`` for no limit.

        Yields:
            :class:`~osint_scraper.models.Post` objects. Posts without media are
            still yielded; the downloader filters them out.
        """
        raise NotImplementedError

    async def profile(self, target: str) -> ProfileInfo:
        """Fetch profile level metadata for ``target``.

        Args:
            target: Platform handle or profile URL.

        Returns:
            :class:`~osint_scraper.models.ProfileInfo` with whatever the platform
            exposes. Implementations that cannot fetch it should raise
            :class:`~osint_scraper.exceptions.UnsupportedTargetError`.
        """
        raise UnsupportedTargetError(f"{self.PLATFORM} does not expose profile metadata")

    async def aclose(self) -> None:
        """Release any resources held by the crawler."""
        return None

    @classmethod
    def supports_target(cls, target: str) -> bool:
        """Return whether ``target`` looks like something this crawler handles.

        Args:
            target: Raw user input.

        Returns:
            ``True`` when the target is plausibly valid for this platform.
        """
        return bool(target.strip())


CRAWLERS: dict[str, type[Crawler]] = {}
"""Registry of crawler classes keyed by :attr:`Crawler.PLATFORM`."""


def register_crawler(cls: type[Crawler]) -> type[Crawler]:
    """Register a crawler class in :data:`CRAWLERS`.

    Args:
        cls: The crawler class to register.

    Returns:
        ``cls`` unchanged, so the decorator can be applied directly.
    """
    if not cls.PLATFORM:
        raise ValueError(f"{cls.__name__} must define a non-empty PLATFORM")
    CRAWLERS[cls.PLATFORM] = cls
    return cls


def get_crawler(platform: str) -> type[Crawler]:
    """Look up a crawler class by platform name.

    Args:
        platform: Platform identifier.

    Returns:
        The registered crawler class.

    Raises:
        UnsupportedTargetError: When no crawler is registered for ``platform``.
    """
    try:
        return CRAWLERS[platform]
    except KeyError as exc:
        known = ", ".join(sorted(CRAWLERS)) or "none"
        raise UnsupportedTargetError(f"unknown platform {platform!r}; known platforms: {known}") from exc


def load_crawlers() -> dict[str, type[Crawler]]:
    """Import the built-in crawler modules so that they register themselves.

    Returns:
        The populated :data:`CRAWLERS` mapping.
    """
    from osint_scraper.crawlers import instagram, threads  # noqa: F401  (import for side effects)

    return CRAWLERS
