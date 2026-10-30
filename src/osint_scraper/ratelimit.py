"""Adaptive pacing and cooldown handling.

Scraping a platform politely is not a matter of sleeping a fixed amount between
requests. A fixed interval is itself a signature. What keeps a session healthy
is:

* **jitter** -- the gap between requests varies;
* **adaptivity** -- the gap grows when the platform pushes back and relaxes
  again once it stops;
* **a cooldown that survives the process** -- a run that gets throttled must not
  be immediately retried, not even by the next invocation.

This module provides the shared pacing state and its on-disk persistence. The
state lives in a small JSON file so that ``osint-scraper`` invocations minutes
apart still respect the cooldown that the previous one triggered.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_FILE = "~/.cache/osint-scraper/cooldown.json"
"""Where the cooldown state is persisted."""

DEFAULT_COOLDOWN_MINUTES = 60.0
"""Default cooldown applied when a platform signals throttling, in minutes."""

MIN_MULTIPLIER = 1.0
"""The pacing multiplier never drops below the configured baseline."""

MAX_MULTIPLIER = 16.0
"""The pacing multiplier never grows beyond this, no matter how often we are pushed back."""

THROTTLE_MARKERS = (
    "feedback_required",
    "rate_limit",
    "rate limit",
    "too many requests",
    "try again later",
    "spam",
    "checkpoint_required",
    "login_required",
)
"""Substrings that mark a platform response as a throttle rather than an error."""


def host_of(url: str) -> str:
    """Return the normalised host of ``url``.

    The host is lowercased and a leading ``www.`` is removed, so that
    ``www.instagram.com`` and ``instagram.com`` share one cooldown. Treating
    them as different hosts would let a caller slip past its own cooldown by
    switching between the two forms.

    Args:
        url: An absolute URL.

    Returns:
        The normalised hostname, or ``"unknown"`` when the URL cannot be parsed.
    """
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    return host.removeprefix("www.") or "unknown"


@dataclass(slots=True)
class HostState:
    """Pacing state for a single host.

    Attributes:
        cooldown_until: Epoch timestamp until which the host must not be touched.
        multiplier: Current scaling factor applied to the configured delay.
        strikes: Number of consecutive throttle responses.
    """

    cooldown_until: float = 0.0
    multiplier: float = MIN_MULTIPLIER
    strikes: int = 0


@dataclass(slots=True)
class CooldownStore:
    """Persistent cooldown and pacing state, keyed by host.

    Attributes:
        path: File the state is persisted to.
        hosts: Per-host state.
    """

    path: Path
    hosts: dict[str, HostState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> CooldownStore:
        """Load the state from disk, or start empty when there is none.

        Args:
            path: State file path. Defaults to :data:`DEFAULT_COOLDOWN_FILE`.

        Returns:
            The loaded store. A missing or unreadable file yields an empty store
            rather than an error -- a corrupt state file must never stop a run.
        """
        resolved = Path(path or DEFAULT_COOLDOWN_FILE).expanduser()
        store = cls(path=resolved)
        if not resolved.is_file():
            return store
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ignoring unreadable cooldown file %s: %s", resolved, exc)
            return store
        for host, values in (raw.get("hosts") or {}).items():
            if not isinstance(values, dict):
                continue
            store.hosts[host] = HostState(
                cooldown_until=float(values.get("cooldown_until", 0.0)),
                multiplier=max(MIN_MULTIPLIER, float(values.get("multiplier", MIN_MULTIPLIER))),
                strikes=int(values.get("strikes", 0)),
            )
        return store

    def save(self) -> None:
        """Persist the state to disk.

        Failures are logged rather than raised: losing the cooldown state must
        never abort a run that is otherwise fine.
        """
        payload = {
            "hosts": {
                host: {
                    "cooldown_until": state.cooldown_until,
                    "multiplier": state.multiplier,
                    "strikes": state.strikes,
                }
                for host, state in self.hosts.items()
                if state.cooldown_until > 0 or state.strikes > 0 or state.multiplier > MIN_MULTIPLIER
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not persist the cooldown state: %s", exc)

    def state_for(self, host: str) -> HostState:
        """Return the state for ``host``, creating it when needed.

        Args:
            host: Hostname.

        Returns:
            The host's state.
        """
        return self.hosts.setdefault(host, HostState())

    def cooldown_remaining(self, host: str) -> float:
        """Return how long ``host`` must not be touched, in seconds.

        Args:
            host: Hostname.

        Returns:
            The remaining cooldown in seconds; ``0.0`` when there is none.
        """
        state = self.hosts.get(host)
        if state is None:
            return 0.0
        return max(0.0, state.cooldown_until - time.time())

    def in_cooldown(self, host: str) -> bool:
        """Return whether ``host`` is currently in a cooldown.

        Args:
            host: Hostname.

        Returns:
            ``True`` when the host must not be contacted yet.
        """
        return self.cooldown_remaining(host) > 0

    def register_throttle(self, host: str, *, seconds: float | None = None) -> float:
        """Record a throttle response and start a cooldown.

        The pacing multiplier grows on every consecutive throttle, so a host
        that keeps pushing back is contacted ever more slowly.

        Args:
            host: Hostname.
            seconds: Cooldown length. Defaults to the configured default.

        Returns:
            The cooldown that was applied, in seconds.
        """
        state = self.state_for(host)
        state.strikes += 1
        state.multiplier = min(MAX_MULTIPLIER, max(MIN_MULTIPLIER, state.multiplier * 2))
        cooldown = seconds if seconds is not None else DEFAULT_COOLDOWN_MINUTES * 60
        state.cooldown_until = max(state.cooldown_until, time.time() + cooldown)
        logger.warning(
            "%s throttled us; pausing for %.0f s (pacing multiplier now %.1fx)",
            host,
            cooldown,
            state.multiplier,
        )
        return cooldown

    def register_success(self, host: str) -> None:
        """Record a successful exchange, relaxing the pacing a little.

        Args:
            host: Hostname.
        """
        state = self.hosts.get(host)
        if state is None:
            return
        if state.strikes:
            state.strikes -= 1
        if state.strikes <= 0:
            state.strikes = 0
            state.multiplier = max(MIN_MULTIPLIER, state.multiplier / 2)
            if state.multiplier <= MIN_MULTIPLIER:
                state.multiplier = MIN_MULTIPLIER
                state.cooldown_until = 0.0

    def clear(self, host: str | None = None) -> None:
        """Forget the cooldown state.

        Args:
            host: Host to clear. ``None`` clears everything.
        """
        if host is None:
            self.hosts.clear()
        else:
            self.hosts.pop(host, None)


class Pacer:
    """Adaptive per-host request pacing.

    Attributes:
        store: Persistent cooldown state.
        base_delay: Minimum delay between requests to the same host.
        jitter: Extra random delay added on top of ``base_delay``.
        cooldown_minutes: How long a host is left alone after it throttles us.
        enabled: Whether cooldowns are enforced. When false a throttle is
            logged but no cooldown is started, and an existing one is ignored.
    """

    def __init__(
        self,
        store: CooldownStore | None = None,
        *,
        base_delay: float = 1.8,
        jitter: float = 2.7,
        cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES,
        enabled: bool = True,
    ) -> None:
        """Create a pacer.

        Args:
            store: Persistent state. A new, empty store is created when omitted.
            base_delay: Minimum delay between requests to the same host.
            jitter: Extra random delay added on top of ``base_delay``.
            cooldown_minutes: How long a host is left alone after it throttles
                us. Fractional minutes are allowed.
            enabled: Whether cooldowns are enforced at all.
        """
        self.store = store or CooldownStore.load()
        self.base_delay = base_delay
        self.jitter = jitter
        self.cooldown_minutes = cooldown_minutes
        self.enabled = enabled
        self._last_request: dict[str, float] = {}

    def delay_for(self, host: str) -> float:
        """Return the delay currently applied to ``host``.

        Args:
            host: Hostname.

        Returns:
            The delay in seconds, including the adaptive multiplier.
        """
        state = self.store.hosts.get(host)
        multiplier = state.multiplier if state else MIN_MULTIPLIER
        base = self.base_delay * multiplier
        return random.uniform(base, base + self.jitter * multiplier)

    def cooldown_remaining(self, host: str) -> float:
        """Return how long ``host`` must not be touched, in seconds.

        Args:
            host: Hostname.

        Returns:
            The remaining cooldown in seconds, or ``0.0`` when cooldowns are
            disabled.
        """
        if not self.enabled:
            return 0.0
        return self.store.cooldown_remaining(host)

    async def wait(self, url: str) -> None:
        """Wait until ``url`` may be requested, then mark the request as sent.

        Args:
            url: The URL about to be requested.

        Raises:
            RateLimitError: When the host is in a cooldown.
        """
        host = host_of(url)
        remaining = self.cooldown_remaining(host)
        if remaining > 0:
            from osint_scraper.exceptions import RateLimitError

            raise RateLimitError(
                f"{host} is in a cooldown for another {remaining:.0f} s. "
                "Wait it out rather than retrying -- retrying now is what causes the flag."
            )
        last = self._last_request.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            sleep_for = self.delay_for(host) - elapsed
            if sleep_for > 0:
                await _sleep(sleep_for)
        self._last_request[host] = time.monotonic()

    def on_throttled(self, url: str, *, seconds: float | None = None) -> float:
        """Record a throttle response for the host of ``url``.

        Args:
            url: The URL that was throttled.
            seconds: Cooldown length, in seconds. Defaults to the pacer's
                configured ``cooldown_minutes``.

        Returns:
            The cooldown that was applied, in seconds. Zero when cooldowns are
            disabled.
        """
        host = host_of(url)
        if not self.enabled:
            logger.warning(
                "%s throttled us, but cooldowns are disabled; continuing anyway",
                host,
            )
            return 0.0
        if seconds is None:
            seconds = self.cooldown_minutes * 60
        return self.store.register_throttle(host, seconds=seconds)

    def on_success(self, url: str) -> None:
        """Record a successful exchange with the host of ``url``.

        Args:
            url: The URL that succeeded.
        """
        self.store.register_success(host_of(url))


async def _sleep(seconds: float) -> None:
    """Sleep asynchronously.

    Args:
        seconds: How long to sleep.
    """
    import asyncio

    await asyncio.sleep(seconds)


def looks_throttled(body: str) -> bool:
    """Return whether a response body indicates throttling.

    Args:
        body: The response body.

    Returns:
        ``True`` when the body carries a throttle marker.
    """
    lowered = body.lower()
    return any(marker in lowered for marker in THROTTLE_MARKERS)
