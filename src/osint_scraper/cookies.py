"""Browser cookie extraction and export."""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from osint_scraper.config.settings import CookieSettings
from osint_scraper.exceptions import CookieError

logger = logging.getLogger("osint_scraper.cookies")

_CHROME_EPOCH_DIFF = 11644473600000000

_BROWSER_CONFIG: dict[str, dict[str, str]] = {
    "brave": {
        "system": "~/.config/BraveSoftware/Brave-Browser",
        "flatpak": "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
        "product": "Brave",
    },
    "chrome": {
        "system": "~/.config/google-chrome",
        "flatpak": "~/.var/app/com.google.Chrome/config/google-chrome",
        "product": "Chrome",
    },
    "chromium": {
        "system": "~/.config/chromium",
        "flatpak": "~/.var/app/org.chromium.Chromium/config/chromium",
        "product": "Chromium",
    },
    "edge": {
        "system": "~/.config/microsoft-edge",
        "flatpak": "~/.var/app/com.microsoft.Edge/config/microsoft-edge",
        "product": "Microsoft Edge",
    },
}

_FIREFOX_ROOTS: list[tuple[str, str]] = [
    ("system", "~/.mozilla/firefox"),
    ("xdg", "$XDG_CONFIG_HOME/mozilla/firefox"),
]

_SESSION_COOKIES: dict[str, set[str]] = {
    "instagram.com": {"sessionid"},
    "threads.net": {"sessionid"},
    "threads.com": {"sessionid"},
    "facebook.com": {"c_user", "xs"},
}
"""Cookies that indicate an authenticated session, per domain.

A profile that holds one of these is worth preferring over a profile that merely
holds more anonymous cookies, because only an authenticated session can reach
gated content.

``csrftoken`` is deliberately **not** in the Threads sets: it is present on any
anonymous visit, and counting it would make an anonymous profile look
authenticated.
"""


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """A discovered browser profile that may contain cookies.

    Attributes:
        browser: Browser identifier (e.g. ``"brave"``).
        source: ``"system"`` or ``"flatpak"``.
        profile_name: Profile directory name, e.g. ``"Default"``.
        cookie_db: Path to the SQLite cookie database.
        product: Product name used for keyring secret lookups.
    """

    browser: str
    source: str
    profile_name: str
    cookie_db: Path
    product: str

    @property
    def spec(self) -> str:
        """Return the source-qualified identifier, e.g. ``"flatpak:Profile 1"``.

        A profile name alone is ambiguous: ``Default`` exists in both the system
        and the Flatpak installation of most browsers.
        """
        return f"{self.source}:{self.profile_name}"


@dataclass(frozen=True, slots=True)
class ProfileReport:
    """What a discovered profile turned out to hold.

    Attributes:
        profile: The profile itself.
        cookie_count: Number of cookies it holds.
        session_domains: Domains for which it holds an authenticated session.
    """

    profile: BrowserProfile
    cookie_count: int = 0
    session_domains: tuple[str, ...] = ()

    @property
    def spec(self) -> str:
        """The source-qualified identifier of the profile."""
        return self.profile.spec

    @property
    def authenticated(self) -> bool:
        """Whether the profile holds an authenticated session for any domain."""
        return bool(self.session_domains)


@dataclass(frozen=True, slots=True)
class _Cookie:
    """Internal cookie representation."""

    host_key: str
    name: str
    value: str
    path: str
    expires_utc: int
    is_secure: bool
    is_httponly: bool


def _expand(path: str) -> Path:
    """Expand a path that may contain ``~`` or environment variables."""
    if path.startswith("$"):
        # $XDG_CONFIG_HOME/...
        var, rest = path.split("/", 1)
        value = os.environ.get(var[1:])
        if not value:
            return Path(path)
        return Path(value) / rest
    return Path(path).expanduser()


def _profile_sort_key(name: str) -> tuple[int, str]:
    """Sort ``Default`` first, then alphabetically."""
    return (0 if name == "Default" else 1, name)


def _chromium_profiles(base: Path) -> list[str]:
    """Return Chromium profile directory names under ``base`` that contain Cookies."""
    if not base.is_dir():
        return []
    return sorted(
        (entry.name for entry in base.iterdir() if entry.is_dir() and (entry / "Cookies").is_file()),
        key=_profile_sort_key,
    )


def _firefox_profiles(base: Path) -> list[tuple[str, str, Path]]:
    """Return Firefox profiles under ``base`` as ``(name, dir_name, cookie_db)``.

    Reads ``profiles.ini`` when present and falls back to scanning for
    ``cookies.sqlite``.
    """
    if not base.is_dir():
        return []
    profiles: list[tuple[str, str, Path]] = []
    seen_dirs: set[str] = set()
    profiles_ini = base / "profiles.ini"
    if profiles_ini.is_file():
        config = configparser.ConfigParser()
        try:
            config.read(profiles_ini, encoding="utf-8")
        except configparser.Error as exc:
            logger.warning("could not parse %s: %s", profiles_ini, exc)
            config = configparser.ConfigParser()
        for section in config.sections():
            if not section.startswith("Profile"):
                continue
            try:
                name = config.get(section, "Name", fallback="")
                path_str = config.get(section, "Path", fallback="")
                is_relative = config.getboolean(section, "IsRelative", fallback=True)
                if not path_str:
                    continue
                profile_dir = base / path_str if is_relative else Path(path_str)
                cookie_db = profile_dir / "cookies.sqlite"
                if cookie_db.is_file():
                    profiles.append((name or profile_dir.name, profile_dir.name, cookie_db))
                    seen_dirs.add(profile_dir.name)
            except (configparser.Error, ValueError) as exc:
                logger.warning("skipping malformed Firefox profile section %s: %s", section, exc)
                continue
    for entry in base.iterdir():
        if entry.is_dir() and entry.name not in seen_dirs:
            cookie_db = entry / "cookies.sqlite"
            if cookie_db.is_file():
                profiles.append((entry.name, entry.name, cookie_db))

    def sort_key(item: tuple[str, str, Path]) -> tuple[int, str]:
        name = item[0]
        return (0 if "default" in name.lower() else 1, name)

    return sorted(profiles, key=sort_key)


def discover_browsers(
    *,
    include_flatpak: bool = True,
    browsers: Sequence[str] | None = None,
) -> list[BrowserProfile]:
    """Discover browser profiles with accessible cookie databases.

    Args:
        include_flatpak: Whether to look inside Flatpak sandboxes.
        browsers: Optional browser names to restrict discovery to.

    Returns:
        A list of profiles ordered Brave first, then Chrome, Chromium, Edge and
        Firefox. Within a browser, system profiles precede Flatpak profiles, and
        ``Default`` comes before other profile names.
    """
    profiles: list[BrowserProfile] = []
    requested = set(browsers) if browsers else None
    for browser in ("brave", "chrome", "chromium", "edge"):
        if requested is not None and browser not in requested:
            continue
        cfg = _BROWSER_CONFIG[browser]
        product = cfg["product"]
        for source in ("system", "flatpak"):
            if source == "flatpak" and not include_flatpak:
                continue
            base = _expand(cfg[source])
            for profile_name in _chromium_profiles(base):
                cookie_db = base / profile_name / "Cookies"
                profiles.append(BrowserProfile(browser, source, profile_name, cookie_db, product))
    if requested is None or "firefox" in requested:
        for source, root_template in _FIREFOX_ROOTS:
            base = _expand(root_template)
            for name, _dir_name, cookie_db in _firefox_profiles(base):
                profiles.append(BrowserProfile("firefox", source, name, cookie_db, "Firefox"))
    return profiles


def _derive_key(password: bytes) -> bytes:
    """Derive the Chromium AES key from a password."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    )
    return kdf.derive(password)


def _pad_pkcs7(data: bytes) -> bytes:
    """Apply PKCS#7 padding to ``data`` for AES block size."""
    from cryptography.hazmat.primitives import padding

    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()


def _decrypt_aes_cbc(ciphertext: bytes, key: bytes, iv: bytes = b" " * 16) -> bytes:
    """Decrypt AES-128-CBC ciphertext and remove PKCS#7 padding."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _strip_integrity_prefix(plaintext: bytes) -> bytes:
    """Strip the 32-byte integrity prefix from decrypted Chromium cookies."""
    if len(plaintext) < 32:
        return plaintext
    return plaintext[32:]


def _encrypt_v10(plaintext: bytes) -> bytes:
    """Encrypt a value using the Chromium v10 scheme (for tests)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _derive_key(b"peanuts")
    payload = b"\x00" * 32 + plaintext
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    ciphertext = encryptor.update(_pad_pkcs7(payload)) + encryptor.finalize()
    return b"v10" + ciphertext


def _encrypt_v11(plaintext: bytes, password: bytes) -> bytes:
    """Encrypt a value using the Chromium v11 scheme (for tests)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _derive_key(password)
    payload = b"\x00" * 32 + plaintext
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    ciphertext = encryptor.update(_pad_pkcs7(payload)) + encryptor.finalize()
    return b"v11" + ciphertext


def _decrypt_chromium_value(
    encrypted_value: bytes,
    keys_v11: list[bytes],
) -> tuple[str | None, str | None]:
    """Decrypt a Chromium ``encrypted_value`` blob.

    Args:
        encrypted_value: The raw blob, including its three-byte scheme prefix.
        keys_v11: Candidate AES keys derived from the keyring passwords. Each is
            tried in turn, so the caller does not have to know which secret the
            desktop actually stores.

    Returns:
        A ``(value, warning)`` tuple. ``value`` is ``None`` when decryption
        fails; ``warning`` is ``None`` on success.
    """
    if not encrypted_value:
        return "", None
    prefix = encrypted_value[:3]
    ciphertext = encrypted_value[3:]
    if prefix == b"v10":
        try:
            key = _derive_key(b"peanuts")
            plaintext = _decrypt_aes_cbc(ciphertext, key)
            return _strip_integrity_prefix(plaintext).decode("utf-8"), None
        except Exception as exc:
            logger.exception("failed to decrypt v10 cookie")
            return None, f"v10 decryption failed: {exc}"
    if prefix == b"v11":
        attempts: list[bytes] = list(keys_v11)
        attempts.append(_derive_key(b""))
        last_error: Exception | None = None
        for key in attempts:
            try:
                plaintext = _decrypt_aes_cbc(ciphertext, key)
                return _strip_integrity_prefix(plaintext).decode("utf-8"), None
            except Exception as exc:
                last_error = exc
                continue
        msg = f"v11 decryption failed: {last_error}" if last_error else "v11 decryption failed"
        return None, msg
    if prefix == b"v12":
        return (
            None,
            "v12 (Flatpak SecretPortal) cookies cannot be decrypted by third-party tools",
        )
    return None, f"unsupported encrypted_value prefix {prefix!r}"


def _get_secret_tool_passwords(product: str) -> list[bytes]:
    """Retrieve candidate Chromium OS crypt passwords from the keyring.

    Several layouts are in the wild and a desktop may hold more than one entry
    for the same key, so every candidate is collected and the caller verifies
    which one actually decrypts:

    * the KWallet native layout (``<Product> Keys`` / ``<Product> Safe Storage``)
      with and without the ``org.qt.keychain`` schema -- on KDE the two entries
      can hold *different* secrets, and only one of them is the live key;
    * the Secret Service layout (``chrome_libsecret_os_crypt_password_v2``);
    * the Secret Service layout scoped to the product name.

    Returns:
        The candidate passwords, most likely first. Empty when no secret was found.
    """
    lookups = [
        [
            "secret-tool",
            "lookup",
            "server",
            f"{product} Keys",
            "user",
            f"{product} Safe Storage",
            "xdg:schema",
            "org.qt.keychain",
        ],
        [
            "secret-tool",
            "lookup",
            "server",
            f"{product} Keys",
            "user",
            f"{product} Safe Storage",
        ],
        [
            "secret-tool",
            "lookup",
            "xdg:schema",
            "chrome_libsecret_os_crypt_password_v2",
            "application",
            product,
        ],
        [
            "secret-tool",
            "lookup",
            "xdg:schema",
            "chrome_libsecret_os_crypt_password_v2",
        ],
    ]
    candidates: list[bytes] = []
    for cmd in lookups:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except FileNotFoundError:
            logger.debug("secret-tool not found, cannot retrieve keyring password")
            return candidates
        except subprocess.TimeoutExpired:
            logger.warning("secret-tool lookup timed out")
            continue
        except OSError as exc:
            logger.warning("secret-tool lookup failed: %s", exc)
            continue
        if result.returncode != 0 or not result.stdout:
            continue
        secret = result.stdout.strip().encode("utf-8")
        if secret and secret not in candidates:
            candidates.append(secret)
    return candidates


def _chrome_expires_to_unix(expires_utc: int) -> int:
    """Convert a Chromium ``expires_utc`` value to a Unix timestamp."""
    if expires_utc == 0:
        return 0
    return max(0, (expires_utc - _CHROME_EPOCH_DIFF) // 1000000)


def _copy_to_temp(original: Path) -> Path:
    """Copy ``original`` to a temporary file and return the path."""
    fd, tmp_name = tempfile.mkstemp(suffix=original.suffix or ".db")
    os.close(fd)
    tmp_path = Path(tmp_name)
    shutil.copy2(original, tmp_path)
    return tmp_path


def _load_chromium_cookies(cookie_db: Path, keys_v11: list[bytes]) -> tuple[list[_Cookie], list[str]]:
    """Load and decrypt cookies from a Chromium ``Cookies`` SQLite database.

    Args:
        cookie_db: Path to the browser's ``Cookies`` database.
        keys_v11: Candidate AES keys for the ``v11`` scheme. Each is tried in
            turn so that a desktop holding several keyring entries still works.

    Returns:
        A ``(cookies, warnings)`` tuple.
    """
    tmp_path = _copy_to_temp(cookie_db)
    cookies: list[_Cookie] = []
    warnings: list[str] = []
    v11_failures = 0
    v12_failures = 0
    other_failures: dict[str, int] = {}
    try:
        with sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value, path, "
                "expires_utc, is_secure, is_httponly, has_expires "
                "FROM cookies"
            )
            for row in rows:
                value = row["value"]
                if not value:
                    decrypted, warn = _decrypt_chromium_value(row["encrypted_value"], keys_v11)
                    if warn:
                        if warn.startswith("v11"):
                            v11_failures += 1
                        elif warn.startswith("v12") or "SecretPortal" in warn:
                            v12_failures += 1
                        else:
                            other_failures[warn] = other_failures.get(warn, 0) + 1
                        continue
                    value = decrypted or ""
                expires = 0 if not row["has_expires"] else _chrome_expires_to_unix(row["expires_utc"])
                cookies.append(
                    _Cookie(
                        host_key=row["host_key"],
                        name=row["name"],
                        value=value,
                        path=row["path"],
                        expires_utc=expires,
                        is_secure=bool(row["is_secure"]),
                        is_httponly=bool(row["is_httponly"]),
                    )
                )
    finally:
        tmp_path.unlink(missing_ok=True)
    if v11_failures:
        warnings.append(
            f"{v11_failures} v11 cookie(s) could not be decrypted (keyring secret may be unavailable)"
        )
    if v12_failures:
        warnings.append(
            f"{v12_failures} v12 (Flatpak SecretPortal) cookie(s) cannot be decrypted by third-party tools"
        )
    for msg, count in other_failures.items():
        warnings.append(f"{count} cookie(s) could not be decrypted: {msg}")
    return cookies, warnings


def _load_firefox_cookies(cookie_db: Path) -> tuple[list[_Cookie], list[str]]:
    """Load plaintext cookies from a Firefox ``cookies.sqlite`` database."""
    tmp_path = _copy_to_temp(cookie_db)
    cookies: list[_Cookie] = []
    warnings: list[str] = []
    try:
        with sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies"
                )
            except sqlite3.OperationalError as exc:
                warnings.append(f"could not read Firefox cookies: {exc}")
                return cookies, warnings
            for row in rows:
                cookies.append(
                    _Cookie(
                        host_key=row["host"],
                        name=row["name"],
                        value=row["value"],
                        path=row["path"],
                        expires_utc=row["expiry"],
                        is_secure=bool(row["isSecure"]),
                        is_httponly=bool(row["isHttpOnly"]),
                    )
                )
    finally:
        tmp_path.unlink(missing_ok=True)
    return cookies, warnings


def _load_netscape_cookies(path: Path) -> tuple[list[_Cookie], list[str]]:
    """Load cookies from a Netscape ``cookies.txt`` file."""
    cookies: list[_Cookie] = []
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"could not read cookie file {path}: {exc}")
        return cookies, warnings
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        is_httponly = False
        if line.startswith("#HttpOnly_"):
            is_httponly = True
            line = line[len("#HttpOnly_") :]
        parts = line.split("\t")
        if len(parts) < 7:
            warnings.append(f"{path}:{line_no}: malformed line")
            continue
        domain, _flag, path_str, secure, expiration, name, value = parts[:7]
        try:
            expires_utc = int(expiration)
        except ValueError:
            warnings.append(f"{path}:{line_no}: invalid expiration {expiration!r}")
            continue
        cookies.append(
            _Cookie(
                host_key=domain,
                name=name,
                value=value,
                path=path_str,
                expires_utc=expires_utc,
                is_secure=secure.upper() == "TRUE",
                is_httponly=is_httponly,
            )
        )
    return cookies, warnings


def _match_domain(host_key: str, domain: str) -> bool:
    """Return whether ``host_key`` matches ``domain`` (handles leading dot)."""
    if host_key.startswith("."):
        canonical = host_key[1:]
        if domain == canonical:
            return True
        return domain.endswith("." + canonical)
    return host_key == domain


class CookieStore:
    """Load browser cookies and expose them to crawlers.

    Implements the :class:`~osint_scraper.crawlers.base.CookieProvider`
    protocol.
    """

    def __init__(self, settings: CookieSettings) -> None:
        self.settings = settings
        self._cookies: list[_Cookie] = []
        self._sources: list[str] = []
        self._warnings: list[str] = []
        self._loaded = False
        self._keys_v11: list[bytes] = []

    async def load(self) -> None:
        """Resolve the configured cookie source and load all cookies.

        With no profile configured, **every** profile of the selected browser is
        used and their cookies are merged. The session usually lives in one
        profile while the other cookies are scattered across the rest, and
        merging beats guessing. On a conflict the value from the profile with
        the stronger session wins.
        """
        self._cookies = []
        self._sources = []
        self._warnings = []
        self._reports: list[ProfileReport] = []
        self._loaded = True

        if self.settings.file:
            file_path = await asyncio.to_thread(Path(self.settings.file).expanduser)
            self._cookies, warnings = await asyncio.to_thread(_load_netscape_cookies, file_path)
            self._warnings.extend(warnings)
            if self._cookies:
                self._sources.append(f"file: {file_path}")
            return

        if not self.settings.browser:
            self._warnings.append("browser cookie extraction disabled (no browser configured)")
            return

        profiles = discover_browsers(
            include_flatpak=self.settings.flatpak,
            browsers=[self.settings.browser],
        )
        profiles = self._filter_by_source(profiles)
        if not profiles:
            self._warnings.append(f"no {self.settings.browser} cookie database found")
            return

        if self.settings.profile:
            selected = self._match_profile(profiles)
            if selected is not None:
                await self._load_profile(selected)
            return

        await self._load_all_profiles(profiles)

    def _filter_by_source(self, profiles: list[BrowserProfile]) -> list[BrowserProfile]:
        """Drop the profiles that do not belong to the configured source.

        Args:
            profiles: Discovered profiles.

        Returns:
            The profiles matching ``settings.source``.
        """
        source = (self.settings.source or "any").strip().lower()
        if source in ("", "any"):
            return profiles
        return [p for p in profiles if p.source == source]

    async def _load_all_profiles(self, profiles: list[BrowserProfile]) -> None:
        """Load and merge the cookies of every given profile.

        Profiles are ranked by how many authenticated sessions they hold, then
        by cookie count, and the strongest profile is merged first so that it
        wins any conflict.

        Args:
            profiles: Profiles to load.
        """
        loaded: list[tuple[BrowserProfile, list[_Cookie]]] = []
        for profile in profiles:
            cookies = await self._probe_profile(profile)
            if cookies is None:
                continue
            loaded.append((profile, cookies))

        ranked = sorted(
            loaded,
            key=lambda item: (self._session_score(item[1]), len(item[1])),
            reverse=True,
        )

        merged: dict[tuple[str, str, str], _Cookie] = {}
        for profile, cookies in ranked:
            for cookie in cookies:
                merged.setdefault((cookie.host_key, cookie.path, cookie.name), cookie)
            self._sources.append(
                f"{profile.browser} {profile.source} {profile.profile_name}: {profile.cookie_db}"
            )
        self._cookies = list(merged.values())

    def _session_score(self, cookies: list[_Cookie]) -> int:
        """Return how many domains the cookies hold an authenticated session for.

        Args:
            cookies: The cookies to inspect.

        Returns:
            The number of domains with an authenticated session.
        """
        names = {c.name for c in cookies}
        return sum(1 for domain in self._session_domains() if names & _SESSION_COOKIES[domain])

    def _session_domains(self) -> list[str]:
        """Return the configured domains that have a session-cookie definition."""
        return [d for d in self.settings.domains if d in _SESSION_COOKIES]

    def _match_profile(self, profiles: list[BrowserProfile]) -> BrowserProfile | None:
        """Resolve the configured profile spec against the available profiles.

        The spec is either a bare directory name (``"Profile 1"``) or a
        source-qualified one (``"flatpak:Profile 1"``). A bare name that
        matches more than one profile is an error rather than a coin toss:
        picking the wrong one silently crawls anonymously.

        Args:
            profiles: Profiles that survived the browser and source filters.

        Returns:
            The selected profile.

        Raises:
            CookieError: When the spec matches nothing or matches ambiguously.
        """
        spec = self.settings.profile.strip()
        wanted_source: str | None = None
        wanted_name = spec
        if ":" in spec:
            wanted_source, wanted_name = spec.split(":", 1)
            wanted_source = wanted_source.strip().lower()
            wanted_name = wanted_name.strip()
        elif self.settings.source and self.settings.source != "any":
            wanted_source = self.settings.source.strip().lower()

        matches = [
            p
            for p in profiles
            if p.profile_name == wanted_name and (wanted_source is None or p.source == wanted_source)
        ]
        if not matches:
            available = ", ".join(sorted(p.spec for p in profiles)) or "none"
            raise CookieError(
                f"profile {spec!r} not found for {self.settings.browser}; available: {available}"
            )
        if len(matches) > 1:
            raise CookieError(
                f"profile {wanted_name!r} is ambiguous for {self.settings.browser} "
                f"(it exists in {', '.join(sorted(m.source for m in matches))}). "
                f"Qualify it: {', '.join(sorted(m.spec for m in matches))}"
            )
        return matches[0]

    async def _probe_profile(self, profile: BrowserProfile) -> list[_Cookie] | None:
        """Read a profile's cookies and record a report for it.

        Args:
            profile: The profile to inspect.

        Returns:
            The decrypted cookies, or ``None`` when the profile cannot be read.
        """
        try:
            if profile.browser == "firefox":
                cookies, _ = await asyncio.to_thread(_load_firefox_cookies, profile.cookie_db)
            else:
                passwords = await asyncio.to_thread(_get_secret_tool_passwords, profile.product)
                keys = [_derive_key(password) for password in passwords]
                cookies, _ = await asyncio.to_thread(_load_chromium_cookies, profile.cookie_db, keys)
        except OSError as exc:
            logger.warning("could not read cookies in %s: %s", profile.cookie_db, exc)
            return None
        names = {c.name for c in cookies}
        session_domains = tuple(
            domain for domain in self._session_domains() if names & _SESSION_COOKIES[domain]
        )
        self._reports.append(
            ProfileReport(
                profile=profile,
                cookie_count=len(cookies),
                session_domains=session_domains,
            )
        )
        return cookies

    async def _load_profile(self, profile: BrowserProfile) -> None:
        if profile.browser == "firefox":
            cookies, warnings = await asyncio.to_thread(_load_firefox_cookies, profile.cookie_db)
        else:
            passwords = await asyncio.to_thread(_get_secret_tool_passwords, profile.product)
            self._keys_v11 = [_derive_key(password) for password in passwords]
            cookies, warnings = await asyncio.to_thread(
                _load_chromium_cookies, profile.cookie_db, self._keys_v11
            )
        self._cookies = cookies
        self._warnings.extend(warnings)
        self._sources.append(
            f"{profile.browser} {profile.source} {profile.profile_name}: {profile.cookie_db}"
        )

    @property
    def reports(self) -> list[ProfileReport]:
        """What each discovered profile turned out to hold.

        Only populated once :meth:`load` has run. Ordered by profile
        discovery order, not by strength.
        """
        return list(self._reports)

    def cookies_for(self, domain: str) -> dict[str, str]:
        """Return cookies whose ``host_key`` matches ``domain``."""
        return {c.name: c.value for c in self._cookies if _match_domain(c.host_key, domain)}

    def cookie_header(self, domain: str) -> str:
        """Return a ``Cookie`` header value for ``domain``."""
        return "; ".join(f"{name}={value}" for name, value in self.cookies_for(domain).items())

    def as_httpx_cookies(self) -> httpx.Cookies:
        """Return an ``httpx.Cookies`` object scoped per-domain."""
        cookies = httpx.Cookies()
        for c in self._cookies:
            cookies.set(c.name, c.value, domain=c.host_key, path=c.path)
        return cookies

    def export(self, path: Path | None = None) -> dict[str, Any]:
        """Write a Netscape ``cookies.txt`` and return a report.

        Args:
            path: Destination path. Defaults to
                ``<store_dir>/<browser>.txt``.

        Returns:
            A dictionary with ``path``, ``cookies``, ``authenticated``,
            ``sources``, ``warnings`` and ``domains`` keys.
        """
        if not self._loaded:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self.load())
            else:
                raise CookieError("cookie store not loaded; await load() before export")

        if path is None:
            store_dir = Path(self.settings.store_dir).expanduser()
            store_dir.mkdir(parents=True, exist_ok=True)
            path = store_dir / f"{self.settings.browser or 'cookies'}.txt"
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["# Netscape HTTP Cookie File"]
        for c in sorted(self._cookies, key=lambda x: (x.host_key, x.path, x.name)):
            domain = c.host_key
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if c.is_secure else "FALSE"
            expiration = str(c.expires_utc)
            prefix = "#HttpOnly_" if c.is_httponly else ""
            lines.append(f"{prefix}{domain}\t{flag}\t{c.path}\t{secure}\t{expiration}\t{c.name}\t{c.value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        domains_report: dict[str, int] = {}
        for domain in self.settings.domains:
            count = len(self.cookies_for(domain))
            if count:
                domains_report[domain] = count

        return {
            "path": str(path),
            "cookies": len(self._cookies),
            "authenticated": self._detect_authenticated(domains_report),
            "sources": self.sources,
            "warnings": self.warnings,
            "domains": domains_report,
        }

    def _detect_authenticated(self, domains_report: dict[str, int]) -> bool | None:
        """Detect whether a logged-in session is present for configured domains."""
        if not domains_report:
            return None
        for domain in self.settings.domains:
            if domain not in domains_report:
                continue
            cookies = self.cookies_for(domain)
            required = _SESSION_COOKIES.get(domain)
            if required is None:
                continue
            if domain == "facebook.com":
                if required.issubset(cookies):
                    return True
            elif required & cookies.keys():
                return True
        return False

    @property
    def sources(self) -> list[str]:
        """Human-readable descriptions of loaded cookie sources."""
        return list(self._sources)

    @property
    def warnings(self) -> list[str]:
        """Non-fatal issues encountered while loading cookies."""
        return list(self._warnings)
