"""Tests for the browser cookie subsystem.

All tests use synthetic data in temporary directories and never touch the
user's real browser profiles.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osint_scraper.config.settings import CookieSettings
from osint_scraper.cookies import (
    _PORTAL_KEY_CACHE,
    BrowserProfile,
    CookieStore,
    _copy_to_temp,
    _decrypt_chromium_value,
    _derive_key,
    _derive_v12_key,
    _encrypt_v10,
    _encrypt_v11,
    _encrypt_v12,
    _flatpak_app_id,
    _load_netscape_cookies,
    _match_domain,
    _portal_secrets_from_keyring,
    _PortalKeySet,
    _PortalKeyUnavailableError,
    discover_browsers,
)
from osint_scraper.exceptions import CookieError


def _make_chromium_db(path: Path, rows: list[dict[str, Any]]) -> None:
    """Create a synthetic Chromium ``Cookies`` SQLite database at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                path TEXT,
                expires_utc INTEGER,
                is_secure INTEGER,
                is_httponly INTEGER,
                last_access_utc INTEGER,
                has_expires INTEGER,
                is_persistent INTEGER,
                source_scheme INTEGER,
                source_port INTEGER,
                creation_utc INTEGER,
                last_update_utc INTEGER
            )
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["host_key"],
                    row["name"],
                    row.get("value", ""),
                    row.get("encrypted_value", b""),
                    row.get("path", "/"),
                    row.get("expires_utc", 0),
                    int(row.get("is_secure", False)),
                    int(row.get("is_httponly", False)),
                    row.get("last_access_utc", 0),
                    int(row.get("has_expires", False)),
                    int(row.get("is_persistent", False)),
                    row.get("source_scheme", 0),
                    row.get("source_port", 0),
                    row.get("creation_utc", 0),
                    row.get("last_update_utc", 0),
                ),
            )


def _row(
    host_key: str,
    name: str,
    value: str = "",
    encrypted_value: bytes = b"",
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "host_key": host_key,
        "name": name,
        "value": value,
        "encrypted_value": encrypted_value,
        **kwargs,
    }


def _settings_for(profile_root: Path, **kwargs: Any) -> CookieSettings:
    defaults = {
        "browser": "brave",
        "profile": "",
        "flatpak": False,
        "domains": ["instagram.com", "threads.net", "threads.com", "facebook.com"],
        "file": "",
        "store_dir": str(profile_root / "store"),
    }
    defaults.update(kwargs)
    return CookieSettings(**defaults)


@pytest.fixture(autouse=True)
def _reset_portal_key_cache() -> Iterator[None]:
    """Keep the process-wide v12 key cache from leaking between tests."""
    _PORTAL_KEY_CACHE.clear()
    yield
    _PORTAL_KEY_CACHE.clear()


class _FakeSecretItem:
    """Minimal stand-in for ``secretstorage.Item``."""

    def __init__(self, secret: bytes, *, locked: bool = False) -> None:
        """Store the raw secret and whether the entry is locked."""
        self._secret = secret
        self._locked = locked

    def is_locked(self) -> bool:
        """Return whether the entry is locked."""
        return self._locked

    def get_secret(self) -> bytes:
        """Return the raw secret bytes."""
        return self._secret


def _fake_secretstorage(
    items: list[_FakeSecretItem],
    *,
    seen: list[dict[str, str]] | None = None,
) -> SimpleNamespace:
    """Build a ``secretstorage`` stand-in whose search returns ``items``."""

    def _search(_connection: object, attributes: dict[str, str]) -> list[_FakeSecretItem]:
        if seen is not None:
            seen.append(attributes)
        return items

    connection = SimpleNamespace(close=lambda: None)
    return SimpleNamespace(dbus_init=lambda: connection, search_items=_search)


def test_v10_decryption_round_trip() -> None:
    plaintext = "super-secret-session-id"
    encrypted = _encrypt_v10(plaintext.encode())
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[])
    assert warning is None
    assert decrypted == plaintext


def test_v10_strips_integrity_prefix() -> None:
    """The 32-byte integrity prefix must be stripped, not returned as value."""
    encrypted = _encrypt_v10(b"realvalue")
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[])
    assert warning is None
    assert decrypted == "realvalue"
    assert not decrypted.startswith("\x00")


def test_v11_decryption_with_keyring_password() -> None:
    password = b"my-kwallet-password"
    plaintext = "v11-session"
    encrypted = _encrypt_v11(plaintext.encode(), password)
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[_derive_key(password)])
    assert warning is None
    assert decrypted == plaintext


def test_v11_tries_every_candidate_key() -> None:
    """The first candidate may be wrong; a later one must still work."""
    password = b"the-real-password"
    encrypted = _encrypt_v11(b"value", password)
    wrong = _derive_key(b"the-wrong-password")
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[wrong, _derive_key(password)])
    assert warning is None
    assert decrypted == "value"


def test_v11_empty_password_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    plaintext = "legacy-empty-password"
    encrypted = _encrypt_v11(plaintext.encode(), b"")
    # Ensure keyring lookup returns nothing so the empty-password fallback runs.
    monkeypatch.setattr("osint_scraper.cookies._get_secret_tool_passwords", lambda _product: [])
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[])
    assert warning is None
    assert decrypted == plaintext


def test_v12_detected_as_undecryptable() -> None:
    encrypted = b"v12" + b"flatpak-secret-portal-blob"
    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[_derive_key(b"anything")])
    assert decrypted is None
    assert "SecretPortal" in warning or "v12" in warning


def test_v12_decryption_round_trip() -> None:
    """A synthesised v12 blob decrypts to its plaintext value."""
    portal_secret = bytes(range(64))
    plaintext = "v12-session-id"
    encrypted = _encrypt_v12(plaintext.encode(), portal_secret)

    def _keys() -> _PortalKeySet:
        return _PortalKeySet(keys=(_derive_v12_key(portal_secret),))

    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[], v12_keys=_keys)
    assert warning is None
    assert decrypted == plaintext


def test_v12_strips_the_integrity_prefix() -> None:
    """The 32-byte integrity prefix is stripped exactly as for v10/v11."""
    portal_secret = bytes(range(64))
    encrypted = _encrypt_v12(b"realvalue", portal_secret)

    def _keys() -> _PortalKeySet:
        return _PortalKeySet(keys=(_derive_v12_key(portal_secret),))

    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[], v12_keys=_keys)
    assert warning is None
    assert decrypted == "realvalue"
    assert not decrypted.startswith("\x00")


def test_v12_hkdf_matches_the_reference_vector() -> None:
    """Pin the HKDF salt, info and length against Chromium's parameters.

    The expected key was derived independently with an RFC 5869 implementation,
    so a drift in either constant or in the output length fails here rather than
    silently producing undecryptable cookies.
    """
    secret = bytes(range(64))
    expected = "39ba5df998f8f1c032fac27b67d7359f8feda53482637745b35e4778295d32e7"
    assert _derive_v12_key(secret).hex() == expected


def test_v12_wrong_key_is_skipped_for_the_next_candidate() -> None:
    """A wrong portal secret fails the GCM tag and the next candidate is tried."""
    portal_secret = b"the-real-portal-secret"
    encrypted = _encrypt_v12(b"realvalue", portal_secret)

    def _keys() -> _PortalKeySet:
        return _PortalKeySet(
            keys=(
                _derive_v12_key(b"the-wrong-portal-secret"),
                _derive_v12_key(portal_secret),
            )
        )

    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[], v12_keys=_keys)
    assert warning is None
    assert decrypted == "realvalue"


def test_v12_all_wrong_keys_report_a_warning() -> None:
    """When every candidate fails, the value is reported, never guessed at."""
    encrypted = _encrypt_v12(b"value", b"right")

    def _keys() -> _PortalKeySet:
        return _PortalKeySet(keys=(_derive_v12_key(b"wrong"),))

    decrypted, warning = _decrypt_chromium_value(encrypted, keys_v11=[], v12_keys=_keys)
    assert decrypted is None
    assert warning is not None
    assert warning.startswith("v12")


def test_v12_without_secretstorage_degrades_to_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing optional dependency is reported, never raised."""
    monkeypatch.setitem(sys.modules, "secretstorage", None)
    key_set = _PORTAL_KEY_CACHE.keys_for("com.example.Browser")
    assert key_set.keys == ()
    assert key_set.reason is not None
    assert "secretstorage" in key_set.reason

    def _keys() -> _PortalKeySet:
        return key_set

    decrypted, warning = _decrypt_chromium_value(
        _encrypt_v12(b"value", b"secret"), keys_v11=[], v12_keys=_keys
    )
    assert decrypted is None
    assert warning is not None
    assert "secretstorage" in warning


def test_v12_missing_portal_secret_degrades_to_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent portal secret names the app and does not raise."""
    monkeypatch.setattr("osint_scraper.cookies._portal_secrets_from_keyring", lambda _app_id: ())
    key_set = _PORTAL_KEY_CACHE.keys_for("com.example.Browser")
    assert key_set.keys == ()
    assert key_set.reason is not None
    assert "com.example.Browser" in key_set.reason


def test_v12_keyring_failure_degrades_to_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A D-Bus failure becomes a warning, not an exception."""

    def _fail(_app_id: str) -> tuple[bytes, ...]:
        raise _PortalKeyUnavailableError("the session D-Bus is unavailable: boom")

    monkeypatch.setattr("osint_scraper.cookies._portal_secrets_from_keyring", _fail)
    key_set = _PORTAL_KEY_CACHE.keys_for("com.example.Browser")
    assert key_set.keys == ()
    assert key_set.reason is not None
    assert "D-Bus" in key_set.reason


def test_portal_secret_lookup_uses_the_portal_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyring is searched by the attributes the portal writes."""
    secret = b"\x07" * 64
    seen: list[dict[str, str]] = []
    monkeypatch.setitem(
        sys.modules,
        "secretstorage",
        _fake_secretstorage([_FakeSecretItem(secret)], seen=seen),
    )

    assert _portal_secrets_from_keyring("com.example.Browser") == (secret,)
    assert seen == [{"server": "xdg-desktop-portal", "user": "com.example.Browser"}]


def test_portal_secret_lookup_skips_locked_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked entry is skipped rather than unlocked, so no prompt can hang."""
    secret = b"\x07" * 64
    monkeypatch.setitem(
        sys.modules,
        "secretstorage",
        _fake_secretstorage([_FakeSecretItem(b"locked", locked=True), _FakeSecretItem(secret)]),
    )

    assert _portal_secrets_from_keyring("com.example.Browser") == (secret,)


def test_portal_secret_lookup_reports_a_locked_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyring with only locked entries explains why nothing was read."""
    monkeypatch.setitem(
        sys.modules,
        "secretstorage",
        _fake_secretstorage([_FakeSecretItem(b"locked", locked=True)]),
    )

    with pytest.raises(_PortalKeyUnavailableError, match="locked"):
        _portal_secrets_from_keyring("com.example.Browser")


def test_flatpak_app_id_comes_from_the_profile_path() -> None:
    """The app ID is parsed out of the path, not looked up in a browser table."""
    flatpak = Path("/home/deck/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies")
    assert _flatpak_app_id(flatpak) == "com.brave.Browser"
    assert (
        _flatpak_app_id(Path("/home/deck/.var/app/org.chromium.Chromium/config/chromium/Default/Cookies"))
        == "org.chromium.Chromium"
    )
    assert _flatpak_app_id(Path("/home/deck/.config/google-chrome/Default/Cookies")) is None


async def test_plaintext_value_column_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [_row(".instagram.com", "plain", value="plain-value", encrypted_value=b"")],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"plain": "plain-value"}


async def test_load_decrypts_v10_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(
                ".instagram.com",
                "sessionid",
                encrypted_value=_encrypt_v10(b"abc123"),
                has_expires=True,
                expires_utc=13376208000000000,  # far future
            )
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "abc123"}


async def test_load_decrypts_v11_cookie_with_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    password = b"test-password"
    monkeypatch.setattr("osint_scraper.cookies._get_secret_tool_passwords", lambda _product: [password])
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [_row(".threads.net", "sessionid", encrypted_value=_encrypt_v11(b"t11", password))],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("threads.net") == {"sessionid": "t11"}


async def test_v12_cookie_reported_as_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "ok", encrypted_value=_encrypt_v10(b"fine")),
            _row(".instagram.com", "bad", encrypted_value=b"v12" + b"x"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"ok": "fine"}
    assert any("v12" in warning for warning in store.warnings)


async def test_flatpak_v12_cookie_decrypts_with_the_portal_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real v12 row decrypts, and the portal secret is read only once."""
    monkeypatch.setenv("HOME", str(tmp_path))
    portal_secret = bytes(range(64))
    looked_up: list[str] = []

    def _secrets(app_id: str) -> tuple[bytes, ...]:
        looked_up.append(app_id)
        return (portal_secret,)

    monkeypatch.setattr("osint_scraper.cookies._portal_secrets_from_keyring", _secrets)
    monkeypatch.setattr("osint_scraper.cookies._get_secret_tool_passwords", lambda _product: [])
    db_path = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(
                ".instagram.com",
                "sessionid",
                encrypted_value=_encrypt_v12(b"first", portal_secret),
            ),
            _row(
                ".threads.net",
                "sessionid",
                encrypted_value=_encrypt_v12(b"second", portal_secret),
            ),
        ],
    )

    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="flatpak:Default")
    store = CookieStore(settings)
    await store.load()

    assert store.cookies_for("instagram.com") == {"sessionid": "first"}
    assert store.cookies_for("threads.net") == {"sessionid": "second"}
    assert looked_up == ["com.brave.Browser"]


async def test_flatpak_v12_without_a_portal_secret_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing portal secret is a warning; the other cookies still load."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("osint_scraper.cookies._portal_secrets_from_keyring", lambda _app_id: ())
    monkeypatch.setattr("osint_scraper.cookies._get_secret_tool_passwords", lambda _product: [])
    db_path = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "ok", encrypted_value=_encrypt_v10(b"fine")),
            _row(".instagram.com", "bad", encrypted_value=_encrypt_v12(b"lost", b"secret")),
        ],
    )

    # No explicit profile: the merge path must surface the warning too.
    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="")
    store = CookieStore(settings)
    await store.load()

    assert store.cookies_for("instagram.com") == {"ok": "fine"}
    assert any("v12" in warning and "com.brave.Browser" in warning for warning in store.warnings)


async def test_no_keyring_lookup_without_a_v12_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v10/v11-only database never pays for the D-Bus round trip."""
    monkeypatch.setenv("HOME", str(tmp_path))
    password = b"test-password"
    lookups: list[str] = []

    def _must_not_be_called(app_id: str) -> tuple[bytes, ...]:
        lookups.append(app_id)
        raise AssertionError("the portal secret must not be read without a v12 cookie")

    monkeypatch.setattr("osint_scraper.cookies._portal_secrets_from_keyring", _must_not_be_called)
    monkeypatch.setattr("osint_scraper.cookies._get_secret_tool_passwords", lambda _product: [password])
    db_path = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "v10", encrypted_value=_encrypt_v10(b"ten")),
            _row(".instagram.com", "v11", encrypted_value=_encrypt_v11(b"eleven", password)),
        ],
    )

    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="flatpak:Default")
    store = CookieStore(settings)
    await store.load()

    assert store.cookies_for("instagram.com") == {"v10": "ten", "v11": "eleven"}
    assert lookups == []


def test_discover_browsers_brave_first_and_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / ".config/BraveSoftware/Brave-Browser"
    (base / "Profile 1" / "Cookies").parent.mkdir(parents=True)
    (base / "Profile 1" / "Cookies").write_text("")
    (base / "Default" / "Cookies").parent.mkdir(parents=True)
    (base / "Default" / "Cookies").write_text("")
    (tmp_path / ".config/google-chrome/Default/Cookies").parent.mkdir(parents=True)
    (tmp_path / ".config/google-chrome/Default/Cookies").write_text("")

    profiles = discover_browsers(include_flatpak=False)
    browsers = [p.browser for p in profiles]
    assert browsers == ["brave", "brave", "chrome"]
    assert profiles[0].profile_name == "Default"
    assert profiles[0].source == "system"
    assert profiles[1].profile_name == "Profile 1"
    assert profiles[2].browser == "chrome"


def test_discover_browsers_flatpak_preferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    system = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    system.parent.mkdir(parents=True)
    system.write_text("")
    flatpak = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    flatpak.parent.mkdir(parents=True)
    flatpak.write_text("")

    profiles = discover_browsers(include_flatpak=True)
    assert len(profiles) == 2
    assert profiles[0].source == "system"


def test_profile_spec_is_source_qualified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile name alone is ambiguous, so the spec carries the source."""
    monkeypatch.setenv("HOME", str(tmp_path))
    system = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    system.parent.mkdir(parents=True)
    system.write_text("")

    profiles = discover_browsers(include_flatpak=True)

    assert profiles[0].spec == "system:Default"


async def test_default_load_merges_every_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no profile configured, every profile is used and merged."""
    monkeypatch.setenv("HOME", str(tmp_path))
    default_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        default_db,
        [_row(".instagram.com", "datr", value="anonymous")],
    )
    profile_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Profile 1/Cookies"
    _make_chromium_db(
        profile_db,
        [_row(".instagram.com", "sessionid", encrypted_value=_encrypt_v10(b"logged-in"))],
    )

    settings = _settings_for(tmp_path, browser="brave", profile="")
    store = CookieStore(settings)
    await store.load()

    assert len(store.sources) == 2
    assert store.cookies_for("instagram.com")["sessionid"] == "logged-in"


async def test_explicit_profile_narrows_to_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming a profile loads only that profile."""
    monkeypatch.setenv("HOME", str(tmp_path))
    default_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(default_db, [_row(".instagram.com", "datr", value="anonymous")])
    profile_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Profile 1/Cookies"
    _make_chromium_db(
        profile_db,
        [_row(".instagram.com", "sessionid", encrypted_value=_encrypt_v10(b"logged-in"))],
    )

    settings = _settings_for(tmp_path, browser="brave", profile="Profile 1")
    store = CookieStore(settings)
    await store.load()

    assert len(store.sources) == 1
    assert "sessionid" in store.cookies_for("instagram.com")


async def test_ambiguous_profile_name_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name that exists in both installations must be qualified."""
    monkeypatch.setenv("HOME", str(tmp_path))
    system_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(system_db, [_row(".instagram.com", "datr", value="system")])
    flatpak_db = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(flatpak_db, [_row(".instagram.com", "datr", value="flatpak")])

    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="Default")
    store = CookieStore(settings)

    with pytest.raises(CookieError, match="ambiguous"):
        await store.load()


async def test_qualified_profile_selects_the_flatpak_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``flatpak:Default`` picks the Flatpak copy of an ambiguous name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    system_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(system_db, [_row(".instagram.com", "datr", value="system")])
    flatpak_db = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(flatpak_db, [_row(".instagram.com", "datr", value="flatpak")])

    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="flatpak:Default")
    store = CookieStore(settings)
    await store.load()

    assert store.cookies_for("instagram.com")["datr"] == "flatpak"


async def test_unknown_profile_lists_the_available_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong profile name must say what is available."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "datr", value="x")])

    settings = _settings_for(tmp_path, browser="brave", profile="Nope")
    store = CookieStore(settings)

    with pytest.raises(CookieError, match="system:Default"):
        await store.load()


async def test_source_setting_narrows_the_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``source`` restricts the run to one installation."""
    monkeypatch.setenv("HOME", str(tmp_path))
    system_db = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(system_db, [_row(".instagram.com", "datr", value="system")])
    flatpak_db = tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(flatpak_db, [_row(".instagram.com", "datr", value="flatpak")])

    settings = _settings_for(tmp_path, browser="brave", flatpak=True, profile="")
    settings.source = "system"
    store = CookieStore(settings)
    await store.load()

    assert len(store.sources) == 1
    assert store.cookies_for("instagram.com")["datr"] == "system"


async def test_reports_expose_the_session_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-profile report says which domains have a session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Profile 1/Cookies"
    _make_chromium_db(
        db_path,
        [_row(".instagram.com", "sessionid", encrypted_value=_encrypt_v10(b"logged-in"))],
    )

    settings = _settings_for(tmp_path, browser="brave", profile="")
    store = CookieStore(settings)
    await store.load()

    reports = store.reports
    assert len(reports) == 1
    assert reports[0].spec == "system:Profile 1"
    assert reports[0].authenticated
    assert "instagram.com" in reports[0].session_domains


def test_discover_browsers_filters_browser_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies").parent.mkdir(parents=True)
    (tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies").write_text("")
    (tmp_path / ".config/google-chrome/Default/Cookies").parent.mkdir(parents=True)
    (tmp_path / ".config/google-chrome/Default/Cookies").write_text("")

    profiles = discover_browsers(include_flatpak=False, browsers=["chrome"])
    assert [p.browser for p in profiles] == ["chrome"]


def test_match_domain_leading_dot() -> None:
    assert _match_domain(".instagram.com", "instagram.com")
    assert _match_domain(".instagram.com", "www.instagram.com")
    assert _match_domain(".instagram.com", "i.instagram.com")
    assert not _match_domain(".instagram.com", "evilinstagram.com")
    assert _match_domain("instagram.com", "instagram.com")
    assert not _match_domain("instagram.com", "www.instagram.com")


async def test_cookies_for_domain_matching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "sub", value="a"),
            _row("instagram.com", "exact", value="b"),
            _row(".threads.net", "t", value="c"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sub": "a", "exact": "b"}
    assert store.cookies_for("www.instagram.com") == {"sub": "a"}
    assert store.cookies_for("threads.net") == {"t": "c"}


async def test_cookie_header_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "a", value="1"),
            _row(".instagram.com", "b", value="2"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    header = store.cookie_header("instagram.com")
    assert header == "a=1; b=2"


async def test_as_httpx_cookies_scoped_per_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "ig", value="x"),
            _row(".threads.net", "th", value="y"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    httpx_cookies = store.as_httpx_cookies()
    # httpx stores domain cookies with the leading dot preserved.
    assert httpx_cookies.get("ig", domain=".instagram.com") == "x"
    assert httpx_cookies.get("th", domain=".threads.net") == "y"
    assert httpx_cookies.get("ig", domain=".threads.net") is None


async def test_export_writes_netscape_file_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(
                ".instagram.com",
                "sessionid",
                value="abc",
                is_secure=True,
                is_httponly=True,
                has_expires=True,
                expires_utc=13376208000000000,
            ),
            _row("instagram.com", "nosub", value="def"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    output = tmp_path / "out.txt"
    report = store.export(output)
    assert report["path"] == str(output)
    assert report["cookies"] == 2

    text = output.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE" in text
    assert ".instagram.com\tTRUE\t/\tTRUE" in text
    assert "instagram.com\tFALSE\t/\tFALSE" in text

    cookies, warnings = _load_netscape_cookies(output)
    assert not warnings
    assert len(cookies) == 2
    by_name = {c.name: c for c in cookies}
    assert by_name["sessionid"].is_httponly
    assert by_name["sessionid"].is_secure
    assert by_name["sessionid"].host_key == ".instagram.com"
    assert by_name["nosub"].host_key == "instagram.com"


async def test_netscape_round_trip_preserves_empty_and_tabbed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty value keeps its trailing separator and a tab survives in a value."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(
                ".forum.winehq.org",
                "whqforum_k",
                value="",
                is_secure=True,
                is_httponly=True,
                has_expires=True,
                expires_utc=13376208000000000,
            ),
            _row(".instagram.com", "tabbed", value="left\tright"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    output = tmp_path / "out.txt"
    store.export(output)

    text = output.read_text(encoding="utf-8")
    assert "\twhqforum_k\t\n" in text
    assert "\ttabbed\tleft\tright\n" in text

    cookies, warnings = _load_netscape_cookies(output)
    assert not warnings
    by_name = {c.name: c for c in cookies}
    assert by_name["whqforum_k"].value == ""
    assert by_name["whqforum_k"].is_httponly
    assert by_name["tabbed"].value == "left\tright"


async def test_export_authenticated_false_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [
            _row(".instagram.com", "csrftoken", value="x"),
            _row(".instagram.com", "datr", value="y"),
        ],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    report = store.export(tmp_path / "out.txt")
    assert report["authenticated"] is False
    assert report["domains"]["instagram.com"] == 2


async def test_export_authenticated_true_with_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(
        db_path,
        [_row(".instagram.com", "sessionid", value="x")],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()
    report = store.export(tmp_path / "out.txt")
    assert report["authenticated"] is True


async def test_missing_profile_raises_cookie_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "x", value="y")])
    settings = _settings_for(tmp_path, browser="brave", profile="Profile 99")
    store = CookieStore(settings)
    with pytest.raises(CookieError) as exc_info:
        await store.load()
    message = str(exc_info.value)
    assert "Profile 99" in message
    assert "Default" in message


async def test_load_file_netscape(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
        ".threads.net\tTRUE\t/\tFALSE\t0\tcsrftoken\tdef\n",
        encoding="utf-8",
    )
    settings = CookieSettings(
        browser="",
        profile="",
        flatpak=False,
        file=str(cookie_file),
        store_dir=str(tmp_path / "store"),
    )
    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "abc"}
    assert store.cookies_for("threads.net") == {"csrftoken": "def"}
    assert store.sources == [f"file: {cookie_file}"]


def test_browser_profile_dataclass() -> None:
    profile = BrowserProfile("brave", "flatpak", "Default", Path("/x/Cookies"), "Brave")
    assert profile.browser == "brave"


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(path.stat().st_mode)


def test_cookie_db_temp_copy_is_owner_only(tmp_path: Path) -> None:
    """The temporary copy of a browser cookie database must not be world-readable."""
    source = tmp_path / "Cookies"
    source.write_text("synthetic database", encoding="utf-8")
    source.chmod(0o644)

    copy = _copy_to_temp(source)
    try:
        assert _mode(copy) == 0o600
        assert copy.read_text(encoding="utf-8") == "synthetic database"
    finally:
        copy.unlink()


async def test_export_file_is_owner_only_and_store_dir_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exported cookies are 0600 and the default store directory is 0700."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="live")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()

    report = store.export()
    exported = Path(report["path"])
    assert _mode(exported) == 0o600
    assert _mode(exported.parent) == 0o700

    # The extraction cache holds the same live values and gets the same mode.
    caches = list(exported.parent.glob("cache-*.txt"))
    assert caches, "expected the extraction cache next to the export"
    assert all(_mode(cache) == 0o600 for cache in caches)


async def test_explicit_export_file_is_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file written outside the store is still 0600."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="live")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    store = CookieStore(settings)
    await store.load()

    output = tmp_path / "explicit" / "cookies.txt"
    store.export(output)
    assert _mode(output) == 0o600
    assert _mode(output.parent) == 0o700


async def test_second_load_reuses_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh cache is served without touching the browser again."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="cached")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    await CookieStore(settings).load()

    def _must_not_extract(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the cache should have been reused")

    monkeypatch.setattr("osint_scraper.cookies.discover_browsers", _must_not_extract)

    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "cached"}
    assert store.sources[0].startswith("cache: ")


async def test_cache_is_not_served_for_a_different_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache written for one profile must never be handed to another."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_chromium_db(
        tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        [_row(".instagram.com", "sessionid", value="anonymous")],
    )
    _make_chromium_db(
        tmp_path / ".config/BraveSoftware/Brave-Browser/Profile 1/Cookies",
        [_row(".instagram.com", "sessionid", value="logged-in")],
    )

    default_settings = _settings_for(tmp_path, browser="brave", profile="Default")
    profile_settings = _settings_for(tmp_path, browser="brave", profile="Profile 1")
    assert CookieStore(default_settings)._cache_path() != CookieStore(profile_settings)._cache_path()

    await CookieStore(default_settings).load()
    store = CookieStore(profile_settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "logged-in"}


async def test_expired_cached_cookie_is_not_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expiry in the Netscape cache is honoured; expired cookies are dropped."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_chromium_db(
        tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        [_row(".instagram.com", "sessionid", value="from-browser")],
    )
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    cache_path = CookieStore(settings)._cache_path()
    cache_path.parent.mkdir(parents=True)
    now = int(time.time())
    cache_path.write_text(
        "# Netscape HTTP Cookie File\n"
        f".instagram.com\tTRUE\t/\tTRUE\t{now - 60}\texpired\told\n"
        f".instagram.com\tTRUE\t/\tTRUE\t{now + 3600}\tvalid\tfresh\n"
        ".instagram.com\tTRUE\t/\tFALSE\t0\tsessionid\tsession\n",
        encoding="utf-8",
    )

    def _must_not_extract(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the cache was usable and should have been served")

    monkeypatch.setattr("osint_scraper.cookies.discover_browsers", _must_not_extract)

    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"valid": "fresh", "sessionid": "session"}


async def test_cache_rejected_after_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache older than the TTL is ignored and the browser is read again."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="first")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    await CookieStore(settings).load()

    cache_path = CookieStore(settings)._cache_path()
    stale = time.time() - 3600
    os.utime(cache_path, (stale, stale))

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cookies SET value = ? WHERE name = ?", ("second", "sessionid"))

    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "second"}


async def test_corrupt_cache_falls_back_to_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed cache is reported and ignored, not fatal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="from-browser")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    cache_path = CookieStore(settings)._cache_path()
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("this is not a cookies file\n", encoding="utf-8")

    store = CookieStore(settings)
    await store.load()
    assert store.cookies_for("instagram.com") == {"sessionid": "from-browser"}
    assert any("corrupt" in warning and "cache" in warning for warning in store.warnings)


async def test_bypass_cache_forces_a_fresh_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``use_cache=False`` and ``export`` never serve a stale cache."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
    _make_chromium_db(db_path, [_row(".instagram.com", "sessionid", value="first")])
    settings = _settings_for(tmp_path, browser="brave", profile="Default")
    await CookieStore(settings).load()

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cookies SET value = ? WHERE name = ?", ("second", "sessionid"))

    bypass = CookieStore(settings, use_cache=False)
    await bypass.load()
    assert bypass.cookies_for("instagram.com") == {"sessionid": "second"}

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cookies SET value = ? WHERE name = ?", ("third", "sessionid"))

    # ``export`` on an unloaded store must also bypass the cache. It is sync and
    # starts its own event loop, so run it off this test's loop.
    output = tmp_path / "fresh.txt"
    await asyncio.to_thread(CookieStore(settings).export, output)
    assert "third" in output.read_text(encoding="utf-8")
