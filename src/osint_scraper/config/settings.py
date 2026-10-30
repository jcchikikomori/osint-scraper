"""Configuration model.

Settings are layered: built-in defaults, then an optional YAML file, then
command-line overrides. The file is looked up in this order:

1. ``$OSINT_SCRAPER_CONFIG``
2. ``./osint-scraper.yaml`` (or ``.yml``)
3. ``$XDG_CONFIG_HOME/osint-scraper/config.yaml``
4. ``~/.config/osint-scraper/config.yaml``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from osint_scraper.exceptions import ConfigError

CONFIG_FILENAMES = ("osint-scraper.yaml", "osint-scraper.yml")
"""Config file names looked for in the current working directory."""

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""Desktop user agent used for HTTP requests."""

DEFAULT_COOKIE_CACHE_TTL = 900.0
"""Seconds a cached cookie extraction stays usable.

A cookie's *value* rarely changes, but the set of cookies in a browser does,
and a stale cache would silently serve an old session. The window is kept
short: the cost of a wrong cache is a failed or mis-scoped run, and the cost
of a short TTL is only re-reading the browser's cookie database.
"""


@dataclass(slots=True)
class CookieSettings:
    """How browser cookies are sourced.

    Attributes:
        browser: Preferred browser name, e.g. ``"brave"``. Empty disables
            browser extraction.
        profile: Which profile to use. Either a bare directory name
            (``"Profile 1"``) or a source-qualified spec (``"flatpak:Profile 1"``).
            Empty means "pick automatically".
        source: Which installation to look in: ``"system"``, ``"flatpak"`` or
            ``"any"``. Narrowing this disambiguates profiles that share a name
            across both.
        flatpak: Whether to look inside Flatpak sandboxes
            (``~/.var/app/<id>/config``) at all.
        domains: Domains cookies are exported for.
        file: Path of an existing Netscape ``cookies.txt`` to use instead.
        store_dir: Directory where exported cookie files are written.
        use_cache: Whether a cached extraction may be reused instead of reading
            the browser again. Turn it off to force a fresh read.
        cache_ttl: How long a cached extraction stays usable, in seconds.
    """

    browser: str = "brave"
    profile: str = ""
    source: str = "any"
    flatpak: bool = True
    domains: list[str] = field(
        default_factory=lambda: [
            "instagram.com",
            "threads.net",
            "threads.com",
            "facebook.com",
        ]
    )
    file: str = ""
    store_dir: str = "~/.cache/osint-scraper/cookies"
    use_cache: bool = True
    cache_ttl: float = DEFAULT_COOKIE_CACHE_TTL


@dataclass(slots=True)
class DownloadSettings:
    """Download behaviour.

    Attributes:
        directory: Root output directory for downloaded media.
        concurrency: Maximum number of simultaneous downloads.
        delay: Minimum delay in seconds between consecutive requests to the same
            host. Combined with jitter this keeps request cadence irregular.
        jitter: Maximum extra random delay added on top of ``delay``.
        retries: Number of retries per failed download.
        timeout: Per-request timeout in seconds.
        overwrite: Whether existing files may be overwritten.
        save_metadata: Whether to write a per-post metadata sidecar file.
        min_size: Skip media files smaller than this many bytes.
    """

    directory: str = "./osint-output"
    concurrency: int = 4
    delay: float = 1.8
    jitter: float = 2.7
    retries: int = 3
    timeout: float = 60.0
    overwrite: bool = False
    save_metadata: bool = True
    min_size: int = 1024


@dataclass(slots=True)
class CrawlSettings:
    """Crawl behaviour.

    Attributes:
        limit: Maximum number of posts per target. ``0`` means unlimited.
        include_text_only: Whether to keep posts that carry no media. A post
            with no media is only a caption; keeping those is useful for a
            text-first account and pure noise for a media-first one.
        platforms: Platforms to crawl when a target is given without a platform.
        user_agent: User agent used for HTTP requests.
        max_posts_per_target: Hard ceiling applied after ``limit``.
        use_graphql: Whether Threads may use the GraphQL transport, which
            reaches a profile's whole timeline instead of the handful of posts
            the server-rendered page carries.  The server-rendered page stays
            as the fallback, so turning this off degrades the result rather
            than failing the run.
        max_pages: How many GraphQL pages to walk per target.  Each page holds
            a few posts, so this is what bounds a run against a very long
            timeline.
    """

    limit: int = 0
    include_text_only: bool = True
    platforms: list[str] = field(default_factory=lambda: ["instagram", "threads"])
    user_agent: str = DEFAULT_USER_AGENT
    max_posts_per_target: int = 5000
    use_graphql: bool = True
    max_pages: int = 100


@dataclass(slots=True)
class RateLimitSettings:
    """Cooldown and pacing behaviour.

    Attributes:
        cooldown_minutes: How long a host is left alone after it throttles us.
            The cooldown is persisted, so it also applies to the *next* run.
        file: Where the cooldown state is stored.
        enabled: Whether cooldowns are enforced at all.
    """

    cooldown_minutes: float = 60.0
    file: str = "~/.cache/osint-scraper/cooldown.json"
    enabled: bool = True


@dataclass(slots=True)
class HistorySettings:
    """Resume, file-check and hash-deduplication behaviour.

    The history database is what makes a re-run cheap.  It records where every
    media file was written, what its SHA-256 is, and which platform media it
    came from, so a second run can recognise work it has already done instead
    of downloading it again.

    Attributes:
        database: Path to the SQLite history database.
        resume: Skip media that is already present.  This is the resume switch:
            with it off, every run re-downloads everything.
        verify: Re-hash a file that is already on disk and compare the digest
            with the recorded one.  A file whose digest no longer matches is
            re-downloaded, because the platform changed the media.
        dedupe: Skip media whose content is already in the library under a
            different name.  The media is then recorded as pointing at the
            copy that already exists instead of being stored twice.
        prune: Drop history rows whose file has been deleted from disk when the
            run starts, so the history does not grow stale forever.
    """

    database: str = "~/.cache/osint-scraper/history.db"
    resume: bool = True
    verify: bool = True
    dedupe: bool = True
    prune: bool = True


@dataclass(slots=True)
class Settings:
    """Top level application settings.

    Attributes:
        crawl: Crawl behaviour.
        download: Download behaviour.
        cookies: Cookie sourcing.
        ratelimit: Cooldown and pacing behaviour.
        history: Resume and file-check behaviour.
        log_level: Logging verbosity.
        manifest_name: Name of the manifest file written next to the media.
    """

    crawl: CrawlSettings = field(default_factory=CrawlSettings)
    download: DownloadSettings = field(default_factory=DownloadSettings)
    cookies: CookieSettings = field(default_factory=CookieSettings)
    ratelimit: RateLimitSettings = field(default_factory=RateLimitSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    log_level: str = "INFO"
    manifest_name: str = "manifest.jsonl"


def _coerce(value: Any, annotation: Any, path: str) -> Any:
    """Coerce a YAML value to the type declared by a dataclass field.

    Args:
        value: Raw value from the config file.
        annotation: Target annotation of the field.
        path: Dotted path of the field, used in error messages.

    Returns:
        The coerced value.

    Raises:
        ConfigError: When the value cannot be coerced.
    """
    if annotation in (int, float, str, bool):
        try:
            return annotation(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: expected {annotation.__name__}, got {value!r}") from exc
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list, got {type(value).__name__}")
        return list(value)
    return value


def _apply(section: Any, dataclass_type: type, path: str) -> Any:
    """Build a dataclass instance from a mapping, validating known keys.

    Args:
        section: Mapping of field names to values, or ``None``.
        dataclass_type: Dataclass to instantiate.
        path: Prefix used in error messages.

    Returns:
        An instance of ``dataclass_type``.
    """
    if section is None:
        return dataclass_type()
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(section).__name__}")
    known = {f.name: f for f in fields(dataclass_type)}
    unknown = set(section) - set(known)
    if unknown:
        raise ConfigError(f"{path}: unknown option(s): {', '.join(sorted(unknown))}")
    kwargs = {name: _coerce(value, known[name].type, f"{path}.{name}") for name, value in section.items()}
    return dataclass_type(**kwargs)


def find_config_file(start: Path | None = None) -> Path | None:
    """Locate the configuration file, if one exists.

    Args:
        start: Directory to search first. Defaults to the current directory.

    Returns:
        The path of the config file, or ``None`` when no file was found.
    """
    env_path = os.environ.get("OSINT_SCRAPER_CONFIG")
    if env_path:
        candidate = Path(env_path).expanduser()
        return candidate if candidate.is_file() else None
    base = start or Path.cwd()
    for name in CONFIG_FILENAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    xdg = os.environ.get("XDG_CONFIG_HOME")
    roots = [Path(xdg) / "osint-scraper"] if xdg else []
    roots.append(Path.home() / ".config" / "osint-scraper")
    for root in roots:
        candidate = root / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from a YAML file, falling back to defaults.

    Args:
        path: Explicit config file. When ``None`` the file is auto-discovered.

    Returns:
        The resolved :class:`Settings`.

    Raises:
        ConfigError: When the file cannot be parsed or contains invalid options.
    """
    resolved = path or find_config_file()
    if resolved is None:
        return Settings()
    if not resolved.is_file():
        raise ConfigError(f"config file not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{resolved}: top level must be a mapping")
    settings = Settings(
        crawl=_apply(raw.get("crawl"), CrawlSettings, "crawl"),
        download=_apply(raw.get("download"), DownloadSettings, "download"),
        cookies=_apply(raw.get("cookies"), CookieSettings, "cookies"),
        ratelimit=_apply(raw.get("ratelimit"), RateLimitSettings, "ratelimit"),
        history=_apply(raw.get("history"), HistorySettings, "history"),
        log_level=_coerce(raw.get("log_level", "INFO"), str, "log_level"),
        manifest_name=_coerce(raw.get("manifest_name", "manifest.jsonl"), str, "manifest_name"),
    )
    return settings
