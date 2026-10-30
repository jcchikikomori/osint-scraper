"""Platform crawlers.

Importing this package registers every built-in crawler. Third-party crawlers can
join the registry by calling
:func:`osint_scraper.crawlers.base.register_crawler`.
"""

from __future__ import annotations

from osint_scraper.crawlers.base import (
    CRAWLERS,
    CookieProvider,
    CrawlContext,
    Crawler,
    get_crawler,
    load_crawlers,
    register_crawler,
)

__all__ = [
    "CRAWLERS",
    "CookieProvider",
    "CrawlContext",
    "Crawler",
    "get_crawler",
    "load_crawlers",
    "register_crawler",
]
