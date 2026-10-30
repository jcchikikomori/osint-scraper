"""Tests for :mod:`osint_scraper.ratelimit`.

The pacer is what keeps a session from being flagged, so its behaviour is
pinned down here: cooldowns persist, they survive a reload, the pacing
multiplier grows on repeated throttles and relaxes again afterwards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from osint_scraper.ratelimit import (
    DEFAULT_COOLDOWN_MINUTES,
    MAX_MULTIPLIER,
    CooldownStore,
    HostState,
    Pacer,
    host_of,
    looks_throttled,
)


def test_default_cooldown_is_in_minutes_not_seconds(tmp_path: Path) -> None:
    """The default cooldown is 60 minutes, not 60 seconds.

    The constant is named in minutes, so using it as a seconds count made the
    cooldown sixty times shorter than it claimed -- and an account-level
    throttle needs minutes, not seconds.
    """
    store = CooldownStore(path=tmp_path / "cooldown.json")
    applied = store.register_throttle("instagram.com")

    assert applied == DEFAULT_COOLDOWN_MINUTES * 60
    remaining = store.cooldown_remaining("instagram.com")
    assert DEFAULT_COOLDOWN_MINUTES * 60 - 5 < remaining <= DEFAULT_COOLDOWN_MINUTES * 60


def test_pacer_uses_the_configured_cooldown_length(tmp_path: Path) -> None:
    """The pacer's ``cooldown_minutes`` decides how long a host is left alone."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    pacer = Pacer(store, cooldown_minutes=15)

    applied = pacer.on_throttled("https://www.instagram.com/somehandle/")

    assert applied == 900
    assert pacer.cooldown_remaining("instagram.com") > 0


def test_pacer_can_disable_cooldowns(tmp_path: Path) -> None:
    """With cooldowns disabled a throttle is logged but nothing is recorded."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    pacer = Pacer(store, enabled=False)

    applied = pacer.on_throttled("https://www.instagram.com/somehandle/")

    assert applied == 0
    assert pacer.cooldown_remaining("instagram.com") == 0
    assert store.cooldown_remaining("instagram.com") == 0


def test_pacer_ignores_an_existing_cooldown_when_disabled(tmp_path: Path) -> None:
    """A cooldown recorded earlier is not honoured while the switch is off."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    store.register_throttle("instagram.com", seconds=600)

    pacer = Pacer(store, enabled=False)

    assert pacer.cooldown_remaining("instagram.com") == 0


def test_host_of_extracts_the_hostname() -> None:
    """``host_of`` returns the normalised hostname of a URL."""
    assert host_of("https://www.instagram.com/somehandle/") == "instagram.com"
    assert host_of("https://www.instagram.com/somehandle/") == host_of("https://instagram.com/somehandle/")
    assert host_of("not a url") == "unknown"


def test_looks_throttled_detects_the_common_markers() -> None:
    """Instagram's throttle markers are recognised."""
    assert looks_throttled('{"message":"feedback_required","spam":true}')
    assert looks_throttled("Try Again Later")
    assert looks_throttled("HTTP 429 Too Many Requests")
    assert not looks_throttled("all good")


def test_register_throttle_starts_a_cooldown(tmp_path: Path) -> None:
    """A throttle puts the host into a cooldown."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    store.register_throttle("instagram.com", seconds=120)

    assert store.in_cooldown("instagram.com")
    assert 0 < store.cooldown_remaining("instagram.com") <= 120


def test_register_success_relaxes_the_pacing(tmp_path: Path) -> None:
    """The pacing multiplier decays back to the baseline after success."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    store.register_throttle("instagram.com", seconds=60)
    assert store.state_for("instagram.com").multiplier > 1.0

    store.register_success("instagram.com")
    store.register_success("instagram.com")
    state = store.state_for("instagram.com")

    assert state.strikes == 0
    assert state.multiplier == 1.0
    assert not store.in_cooldown("instagram.com")


def test_repeated_throttles_grow_the_multiplier(tmp_path: Path) -> None:
    """Each consecutive throttle doubles the pacing delay, up to a ceiling."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    for _ in range(10):
        store.register_throttle("instagram.com", seconds=60)

    assert store.state_for("instagram.com").multiplier == MAX_MULTIPLIER
    assert store.state_for("instagram.com").strikes == 10


def test_state_survives_a_reload(tmp_path: Path) -> None:
    """The cooldown must apply to the next invocation, not just this one."""
    path = tmp_path / "cooldown.json"
    store = CooldownStore(path=path)
    store.register_throttle("instagram.com", seconds=600)
    store.save()

    assert path.exists()
    reloaded = CooldownStore.load(path)

    assert reloaded.in_cooldown("instagram.com")
    assert reloaded.state_for("instagram.com").multiplier == store.state_for("instagram.com").multiplier


def test_corrupt_state_file_is_ignored(tmp_path: Path) -> None:
    """A corrupt state file must never stop a run."""
    path = tmp_path / "cooldown.json"
    path.write_text("{not json", encoding="utf-8")

    store = CooldownStore.load(path)

    assert store.hosts == {}


def test_missing_state_file_yields_an_empty_store(tmp_path: Path) -> None:
    """A missing state file is not an error."""
    store = CooldownStore.load(tmp_path / "absent.json")

    assert store.hosts == {}
    assert not store.in_cooldown("instagram.com")


def test_pacer_delay_scales_with_the_multiplier(tmp_path: Path) -> None:
    """A throttled host is paced more slowly than a healthy one."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    pacer = Pacer(store, base_delay=2.0, jitter=0.0)

    assert pacer.delay_for("instagram.com") == 2.0

    store.register_throttle("instagram.com", seconds=60)
    assert pacer.delay_for("instagram.com") == 4.0


def test_pacer_refuses_a_host_in_cooldown(tmp_path: Path) -> None:
    """Requesting a cooling host raises instead of hammering it."""
    from osint_scraper.exceptions import RateLimitError

    store = CooldownStore(path=tmp_path / "cooldown.json")
    pacer = Pacer(store, base_delay=0.0, jitter=0.0)
    store.register_throttle("instagram.com", seconds=60)

    with pytest.raises(RateLimitError) as exc_info:
        import asyncio

        asyncio.run(pacer.wait("https://www.instagram.com/somehandle/"))

    assert "cooldown" in str(exc_info.value)


def test_clear_removes_the_state(tmp_path: Path) -> None:
    """``clear`` wipes the cooldown for one host or for all of them."""
    store = CooldownStore(path=tmp_path / "cooldown.json")
    store.register_throttle("instagram.com", seconds=60)
    store.register_throttle("threads.net", seconds=60)

    store.clear("instagram.com")
    assert not store.in_cooldown("instagram.com")
    assert store.in_cooldown("threads.net")

    store.clear()
    assert store.hosts == {}


def test_saved_file_is_valid_json(tmp_path: Path) -> None:
    """The persisted state is plain JSON."""
    path = tmp_path / "cooldown.json"
    store = CooldownStore(path=path)
    store.hosts["instagram.com"] = HostState(cooldown_until=time.time() + 60, multiplier=2.0, strikes=1)
    store.save()

    data = json.loads(path.read_text(encoding="utf-8"))

    assert "instagram.com" in data["hosts"]
    assert data["hosts"]["instagram.com"]["multiplier"] == 2.0
