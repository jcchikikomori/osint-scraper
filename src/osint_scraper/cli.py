"""Command line interface.

Subcommands:

``scrape``
    Crawl one or more targets and download their media.
``cookies``
    Inspect and export browser cookies.
``doctor``
    Check the local environment (Python version, available tools, browsers).
``platforms``
    List the registered crawlers.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from osint_scraper import __version__
from osint_scraper.config.settings import Settings, load_settings
from osint_scraper.crawlers.base import CrawlContext, get_crawler, load_crawlers
from osint_scraper.exceptions import OsintScraperError, UnsupportedTargetError
from osint_scraper.logs import setup_logging

if TYPE_CHECKING:  # pragma: no cover - typing only
    from osint_scraper.ratelimit import Pacer

app = typer.Typer(
    name="osint-scraper",
    help="Scrape media from social media profiles.",
    no_args_is_help=True,
    add_completion=False,
)
cookies_app = typer.Typer(help="Inspect and export browser cookies.", no_args_is_help=True)
app.add_typer(cookies_app, name="cookies")

_settings_cache: Settings | None = None
"""Settings resolved by the root callback, reused by the subcommands."""


def _version_callback(value: bool) -> None:
    """Print the version and exit."""
    if value:
        typer.echo(f"osint-scraper {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to the configuration file."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only log errors.")] = False,
) -> None:
    """Configure logging and load the settings file."""
    level = "DEBUG" if verbose else "ERROR" if quiet else "INFO"
    setup_logging(level)
    settings = load_settings(config)
    if verbose or quiet:
        settings.log_level = level
    global _settings_cache
    _settings_cache = settings


def _settings() -> Settings:
    """Return the settings loaded by the root callback."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = Settings()
    return _settings_cache


@app.command()
def scrape(
    target: Annotated[
        list[str] | None,
        typer.Argument(help="Target handles or profile URLs. Omit to use --platform with --target-file."),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", "-p", help="Platform to crawl. Repeatable."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", help="Maximum number of posts per target."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output directory for downloaded media."),
    ] = None,
    cookies: Annotated[
        bool,
        typer.Option("--cookies/--no-cookies", help="Use browser cookies for authenticated access."),
    ] = True,
    browser: Annotated[
        str | None,
        typer.Option(
            "--browser", "-b", help="Browser to take cookies from (brave, chrome, chromium, firefox, edge)."
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Which browser profile to use, e.g. 'Profile 1' or 'flatpak:Profile 1'. "
            "Omit to use every profile of the browser.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Restrict to one installation: system, flatpak or any."),
    ] = None,
    download: Annotated[
        bool,
        typer.Option("--download/--no-download", help="Download the discovered media."),
    ] = True,
    manifest: Annotated[
        bool,
        typer.Option("--manifest/--no-manifest", help="Write a manifest next to the media."),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Discover media but do not download anything."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--no-resume",
            help="Skip media that was already downloaded. On by default.",
        ),
    ] = True,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Re-hash files already on disk and compare them with the recorded digest.",
        ),
    ] = True,
    dedupe: Annotated[
        bool,
        typer.Option(
            "--dedupe/--no-dedupe",
            help="Do not store the same content twice under different names.",
        ),
    ] = True,
    history_db: Annotated[
        Path | None,
        typer.Option("--history", help="Path to the history database."),
    ] = None,
) -> None:
    """Crawl the given targets and download their media."""
    settings = _settings()
    if browser is not None:
        settings.cookies.browser = browser
    if profile is not None:
        settings.cookies.profile = profile
    if source is not None:
        settings.cookies.source = source
    if output is not None:
        settings.download.directory = str(output)
    if limit is not None:
        settings.crawl.limit = limit
    if history_db is not None:
        settings.history.database = str(history_db)
    settings.history.resume = resume
    settings.history.verify = verify
    settings.history.dedupe = dedupe

    platforms = list(platform or settings.crawl.platforms)
    targets = list(target or [])
    if not targets:
        raise typer.BadParameter("at least one target is required")

    try:
        result = asyncio.run(
            _run_scrape(
                settings,
                platforms,
                targets,
                use_cookies=cookies,
                do_download=download,
                dry_run=dry_run,
                write_manifest=manifest,
            )
        )
    except OsintScraperError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


def _enforce_cooldowns(pacer: Pacer, platforms: list[str]) -> None:
    """Refuse to start while a platform is cooling down.

    Retrying through a throttle is what turns a rate limit into a spam flag, so
    the run stops here instead of hammering the host.

    Args:
        pacer: The shared pacer holding the cooldown state.
        platforms: Platforms the run is about to crawl.

    Raises:
        RateLimitError: When any relevant host is still in a cooldown.
    """
    from osint_scraper.exceptions import RateLimitError

    hosts = {"instagram": "instagram.com", "threads": "threads.net"}
    waiting = []
    for platform in platforms:
        host = hosts.get(platform)
        if host is None:
            continue
        remaining = pacer.cooldown_remaining(host)
        if remaining > 0:
            waiting.append(f"{host} for another {remaining / 60:.0f} min")
    if waiting:
        raise RateLimitError(
            "cooling down after being throttled: "
            + ", ".join(waiting)
            + ". Retrying now is what causes the flag -- wait it out. "
            "Use `osint-scraper cooldown --clear` to override deliberately."
        )


def _prepare_output_root(directory: str) -> Path:
    """Resolve and create the output directory.

    Args:
        directory: Configured output directory.

    Returns:
        The resolved directory, created if it did not exist.
    """
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _expand_history_path(location: str) -> Path:
    """Resolve the history database path.

    Args:
        location: Configured database location.

    Returns:
        The resolved path.
    """
    return Path(location).expanduser()


async def _run_scrape(
    settings: Settings,
    platforms: list[str],
    targets: list[str],
    *,
    use_cookies: bool,
    do_download: bool,
    dry_run: bool,
    write_manifest: bool,
) -> dict[str, Any]:
    """Run the crawl for every platform/target combination.

    Args:
        settings: Resolved settings.
        platforms: Platforms to crawl.
        targets: Target handles.
        use_cookies: Whether to source browser cookies.
        do_download: Whether to download the discovered media.
        dry_run: When true, discovery only.
        write_manifest: Whether to write the JSONL manifest.

    Returns:
        A summary mapping suitable for printing as JSON.
    """
    import httpx

    from osint_scraper.cookies import CookieStore
    from osint_scraper.database import Database
    from osint_scraper.downloader import Downloader
    from osint_scraper.manifest import ManifestWriter
    from osint_scraper.ratelimit import CooldownStore, Pacer

    load_crawlers()
    root = await asyncio.to_thread(_prepare_output_root, settings.download.directory)
    logger = setup_logging(settings.log_level)

    store = CooldownStore.load(settings.ratelimit.file)
    pacer = Pacer(store, base_delay=settings.download.delay, jitter=settings.download.jitter)

    cookie_store = CookieStore(settings.cookies)
    if use_cookies:
        await cookie_store.load()

    _enforce_cooldowns(pacer, platforms)

    database = Database(await asyncio.to_thread(_expand_history_path, settings.history.database))
    if settings.history.prune:
        removed = await asyncio.to_thread(database.prune_missing)
        if removed:
            logger.info("pruned %d stale history entr(ies)", removed)

    timeout = httpx.Timeout(settings.download.timeout)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.crawl.user_agent},
    ) as client:
        # No cookie jar on the shared client.  The Threads crawler sets the
        # Cookie header itself, per request, because some Threads requests
        # must be anonymous and others must carry the session -- and the
        # server-rendered page and the GraphQL timeline want opposite things.
        # A jar on the client would override that choice.
        downloader = Downloader(
            settings.download,
            logger=logger,
            pacer=pacer,
            history=database,
            resume=settings.history.resume,
            verify=settings.history.verify,
            dedupe=settings.history.dedupe,
        )
        manifest_writer = ManifestWriter(root / settings.manifest_name) if write_manifest else None
        summary: dict[str, Any] = {"output": str(root), "targets": [], "skipped": []}
        try:
            for plat in platforms:
                crawler_cls = get_crawler(plat)
                for tgt in targets:
                    ctx = CrawlContext(
                        settings=settings,
                        http=client,
                        cookies=cookie_store,
                        logger=logger,
                        output_dir=root,
                        user_agent=settings.crawl.user_agent,
                        pacer=pacer,
                    )
                    crawler = crawler_cls(ctx)
                    found = 0
                    downloaded = 0
                    try:
                        async for post in crawler.crawl(tgt, limit=settings.crawl.limit or None):
                            found += 1
                            database.record_post(
                                platform=post.platform,
                                target=post.target,
                                post_id=post.post_id,
                            )
                            if manifest_writer is not None:
                                manifest_writer.write_post(post)
                            if not post.has_media:
                                continue
                            if not do_download or dry_run:
                                continue
                            saved = await downloader.download_post(post, root)
                            downloaded += saved
                            if manifest_writer is not None:
                                for result in downloader.results:
                                    manifest_writer.write_file(
                                        post,
                                        url=result.url,
                                        path=result.path,
                                        sha256=result.sha256,
                                        size=result.size,
                                        root=root,
                                    )
                    except UnsupportedTargetError as exc:
                        # The target simply is not this platform's business.  A
                        # Threads link handed to the Instagram crawler must not
                        # kill the run -- the next platform may well own it.
                        logger.info("%s: %s", plat, exc)
                        summary["skipped"].append({"platform": plat, "target": tgt, "reason": str(exc)})
                        continue
                    summary["targets"].append(
                        {
                            "platform": plat,
                            "target": tgt,
                            "posts": found,
                            "downloaded": downloaded,
                        }
                    )
            if not summary["targets"] and summary["skipped"]:
                raise UnsupportedTargetError(
                    "no platform accepted the target: "
                    + "; ".join(f"{item['platform']}: {item['reason']}" for item in summary["skipped"])
                )
            summary["downloads"] = downloader.stats
        finally:
            store.save()
            if manifest_writer is not None:
                summary["manifest"] = manifest_writer.summary()
                manifest_writer.close()
            await downloader.aclose()
            database.close()
    return summary


@cookies_app.command("list")
def cookies_list(
    browser: Annotated[
        str | None,
        typer.Option("--browser", "-b", help="Restrict the listing to one browser."),
    ] = None,
    include_flatpak: Annotated[
        bool,
        typer.Option("--flatpak/--no-flatpak", help="Include Flatpak sandboxed browser profiles."),
    ] = True,
    probe: Annotated[
        bool,
        typer.Option("--probe/--no-probe", help="Decrypt each profile to count cookies and detect sessions."),
    ] = True,
) -> None:
    """List the browser profiles that can be used as a cookie source."""
    from osint_scraper.cookies import CookieStore, discover_browsers

    profiles = discover_browsers(include_flatpak=include_flatpak)
    if browser:
        profiles = [p for p in profiles if p.browser == browser]

    reports: dict[str, object] = {}
    if probe and profiles:
        settings = _settings()
        settings.cookies.browser = browser or settings.cookies.browser
        settings.cookies.profile = ""
        settings.cookies.source = "any"
        store = CookieStore(settings.cookies)
        try:
            asyncio.run(store.load())
        except OsintScraperError as exc:
            typer.secho(f"warning: {exc}", fg=typer.colors.YELLOW, err=True)
        else:
            for report in store.reports:
                reports[report.spec] = {
                    "cookie_count": report.cookie_count,
                    "session_domains": list(report.session_domains),
                }

    rows: list[dict[str, object]] = []
    for profile in profiles:
        row: dict[str, object] = {
            "browser": profile.browser,
            "source": profile.source,
            "profile": profile.profile_name,
            "spec": profile.spec,
            "cookie_db": str(profile.cookie_db),
            "exists": profile.cookie_db.exists(),
        }
        probe_info = reports.get(profile.spec)
        if isinstance(probe_info, dict):
            row.update(probe_info)
        rows.append(row)
    typer.echo(json.dumps(rows, indent=2))


@cookies_app.command("export")
def cookies_export(
    browser: Annotated[str, typer.Option("--browser", "-b", help="Browser to export from.")] = "brave",
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Which profile to export, e.g. 'Profile 1' or 'flatpak:Profile 1'. "
            "Omit to merge every profile.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Restrict to one installation: system, flatpak or any."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination cookies.txt path."),
    ] = None,
    include_flatpak: Annotated[
        bool,
        typer.Option("--flatpak/--no-flatpak", help="Include Flatpak sandboxed browser profiles."),
    ] = True,
    check: Annotated[
        bool,
        typer.Option("--check/--no-check", help="Report whether an authenticated session was found."),
    ] = True,
) -> None:
    """Export browser cookies into a Netscape cookies.txt file."""
    from osint_scraper.cookies import CookieStore

    settings = _settings()
    settings.cookies.browser = browser
    if profile:
        settings.cookies.profile = profile
    if source:
        settings.cookies.source = source
    settings.cookies.flatpak = include_flatpak

    store = CookieStore(settings.cookies)
    try:
        report = store.export(output)
    except OsintScraperError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    if check:
        status = report.get("authenticated")
        if status is False:
            typer.secho(
                "warning: no authenticated session found for the requested domains; "
                "log in to the site in the browser and export again.",
                fg=typer.colors.YELLOW,
                err=True,
            )


@app.command()
def cooldown(
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Clear the stored cooldown state."),
    ] = False,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Only affect this host. Defaults to every host."),
    ] = None,
) -> None:
    """Show, or clear, the stored cooldown state."""
    from osint_scraper.ratelimit import CooldownStore

    settings = _settings()
    store = CooldownStore.load(settings.ratelimit.file)
    if clear:
        store.clear(host)
        store.save()
        typer.echo(f"cleared the cooldown for {host or 'all hosts'}")
        return

    rows = []
    for name, state in sorted(store.hosts.items()):
        remaining = store.cooldown_remaining(name)
        rows.append(
            {
                "host": name,
                "cooldown_remaining_s": round(remaining, 1),
                "in_cooldown": remaining > 0,
                "pacing_multiplier": state.multiplier,
                "strikes": state.strikes,
            }
        )
    typer.echo(
        json.dumps(
            {
                "file": str(store.path),
                "exists": store.path.exists(),
                "hosts": rows,
            },
            indent=2,
        )
    )


@app.command()
def history(
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            help="Drop the entries whose file has been deleted from disk.",
        ),
    ] = False,
    database: Annotated[
        Path | None,
        typer.Option("--history", help="Path to the history database."),
    ] = None,
) -> None:
    """Show the download history, or prune the entries whose file is gone."""
    from osint_scraper.database import Database

    settings = _settings()
    path = Path(database or settings.history.database).expanduser()
    store = Database(path)
    try:
        store.connect()
        removed = store.prune_missing() if prune else 0
        report: dict[str, Any] = {
            "database": str(path),
            "exists": path.exists(),
            "pruned": removed,
            **store.stats(),
        }
    finally:
        store.close()
    typer.echo(json.dumps(report, indent=2))


@app.command()
def doctor() -> None:
    """Report on the local environment."""
    from osint_scraper.cookies import discover_browsers
    from osint_scraper.ratelimit import CooldownStore

    settings = _settings()
    store = CooldownStore.load(settings.ratelimit.file)
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "version": __version__,
        "platforms": sorted(load_crawlers()),
        "browsers": [],
        "cooldowns": {
            "file": str(store.path),
            "hosts": {
                name: {
                    "remaining_s": round(store.cooldown_remaining(name), 1),
                    "multiplier": state.multiplier,
                    "strikes": state.strikes,
                }
                for name, state in sorted(store.hosts.items())
            },
        },
        "tools": {},
    }
    for profile in discover_browsers():
        report["browsers"].append(
            {
                "browser": profile.browser,
                "source": profile.source,
                "profile": profile.profile_name,
                "cookie_db": str(profile.cookie_db),
                "exists": profile.cookie_db.exists(),
            }
        )
    for tool in ("instaloader", "gallery-dl", "yt-dlp", "ffmpeg", "secret-tool"):
        found = any((Path(p) / tool).exists() for p in sys.path if p) or _which(tool)
        report["tools"][tool] = bool(found)
    typer.echo(json.dumps(report, indent=2))


@app.command()
def platforms() -> None:
    """List the registered crawlers."""
    rows = []
    for name, cls in sorted(load_crawlers().items()):
        rows.append(
            {
                "platform": name,
                "display_name": cls.DISPLAY_NAME or name,
                "requires_auth": cls.REQUIRES_AUTH,
                "supports_limit": cls.SUPPORTS_LIMIT,
            }
        )
    typer.echo(json.dumps(rows, indent=2))


def _which(program: str) -> bool:
    """Return whether ``program`` is on PATH.

    Args:
        program: Executable name.

    Returns:
        ``True`` when the executable was found.
    """
    import shutil

    return shutil.which(program) is not None


def main() -> None:
    """Entry point for the ``osint-scraper`` console script."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
