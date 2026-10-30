"""Domain exceptions for osint-scraper.

Every error raised by this package derives from :class:`OsintScraperError` so that
callers can catch the whole family with a single ``except`` clause.
"""

from __future__ import annotations


class OsintScraperError(Exception):
    """Base class for all errors raised by osint-scraper."""


class ConfigError(OsintScraperError):
    """Raised when the configuration file is missing, malformed or invalid."""


class TargetError(OsintScraperError):
    """Raised when a target handle/URL cannot be interpreted."""


class AuthError(OsintScraperError):
    """Raised when a platform requires an authenticated session that is unavailable.

    This is the single most common failure mode for Instagram: profile scraping is
    gated behind a logged-in ``sessionid`` cookie. The message is expected to tell
    the operator exactly what to do next.
    """


class RateLimitError(OsintScraperError):
    """Raised when a platform rate-limits or bans the client."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CookieError(OsintScraperError):
    """Raised when browser cookies cannot be located, read or decrypted."""


class ParseError(OsintScraperError):
    """Raised when a platform response cannot be parsed into media records."""


class DownloadError(OsintScraperError):
    """Raised when a media file cannot be downloaded."""


class UnsupportedTargetError(TargetError):
    """Raised when a crawler does not know how to handle the given target."""
