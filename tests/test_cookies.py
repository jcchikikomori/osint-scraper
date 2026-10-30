"""Tests for the browser cookie subsystem.

All tests use synthetic data in temporary directories and never touch the
user's real browser profiles.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from osint_scraper.config.settings import CookieSettings
from osint_scraper.cookies import (
    BrowserProfile,
    CookieStore,
    _decrypt_chromium_value,
    _derive_key,
    _encrypt_v10,
    _encrypt_v11,
    _load_netscape_cookies,
    _match_domain,
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
