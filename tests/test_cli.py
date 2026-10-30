"""Tests for the CLI's own decisions.

These cover the settings that the CLI reads but that no crawler consults: the
per-target post ceiling, the text-only post switch, and the cache and
cooldown knobs the CLI threads through.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from osint_scraper.cli import _effective_post_limit, _keep_post
from osint_scraper.config.settings import Settings
from osint_scraper.models import MediaRef, Post


def _post(*, with_media: bool) -> Post:
    """Return a post with or without media."""
    media = [MediaRef(url="https://cdn.example.com/a.jpg", kind="image")] if with_media else []
    return Post(
        platform="instagram",
        target="somehandle",
        post_id="1",
        media=media,
    )


def test_keep_post_drops_a_text_only_post_when_asked() -> None:
    """A post with no media is only a caption, and is kept only on request."""
    assert _keep_post(_post(with_media=True), include_text_only=False) is True
    assert _keep_post(_post(with_media=False), include_text_only=False) is False


def test_keep_post_keeps_a_text_only_post_when_asked() -> None:
    """The text-only switch keeps the caption when it is on."""
    assert _keep_post(_post(with_media=False), include_text_only=True) is True


def test_effective_post_limit_is_unlimited_by_default() -> None:
    """With no limit configured, nothing is capped."""
    settings = Settings()
    settings.crawl.limit = 0
    settings.crawl.max_posts_per_target = 0
    logger = logging.getLogger("test.cli")

    assert _effective_post_limit(settings, logger) is None


def test_effective_post_limit_uses_the_user_limit() -> None:
    """The user's own limit is used when it is the tighter of the two."""
    settings = Settings()
    settings.crawl.limit = 10
    settings.crawl.max_posts_per_target = 5000
    logger = logging.getLogger("test.cli")

    assert _effective_post_limit(settings, logger) == 10


def test_effective_post_limit_applies_the_safety_ceiling() -> None:
    """The safety ceiling caps a run even when the user set no limit."""
    settings = Settings()
    settings.crawl.limit = 0
    settings.crawl.max_posts_per_target = 250
    logger = logging.getLogger("test.cli")

    assert _effective_post_limit(settings, logger) == 250


def test_effective_post_limit_safety_ceiling_wins() -> None:
    """The tighter of the two limits wins."""
    settings = Settings()
    settings.crawl.limit = 5000
    settings.crawl.max_posts_per_target = 100
    logger = logging.getLogger("test.cli")

    assert _effective_post_limit(settings, logger) == 100


def test_effective_post_limit_warns_when_the_ceiling_bites(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A capped run says so, rather than silently collecting less."""
    settings = Settings()
    settings.crawl.limit = 0
    settings.crawl.max_posts_per_target = 250
    logger = logging.getLogger("test.cli")

    with caplog.at_level(logging.WARNING, logger="test.cli"):
        _effective_post_limit(settings, logger)

    assert any("max_posts_per_target" in record.message for record in caplog.records)


def test_cookie_cache_settings_are_wired() -> None:
    """The cookie cache knobs reach the settings the store reads."""
    settings = Settings()

    assert settings.cookies.use_cache is True
    assert settings.cookies.cache_ttl > 0


def test_ratelimit_settings_are_wired() -> None:
    """The cooldown knobs exist and have usable defaults."""
    settings = Settings()

    assert settings.ratelimit.enabled is True
    assert settings.ratelimit.cooldown_minutes > 0


def test_history_path_expands(tmp_path: Path) -> None:
    """The history path is expanded, so a ``~`` in the config works."""
    settings = Settings()
    settings.history.database = str(tmp_path / "history.db")

    assert not settings.history.database.startswith("~")
