"""Browser cookie extraction and export."""

from __future__ import annotations

import asyncio
import configparser
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from osint_scraper.config.settings import DEFAULT_COOKIE_CACHE_TTL, CookieSettings
from osint_scraper.exceptions import CookieError

logger = logging.getLogger("osint_scraper.cookies")

_CHROME_EPOCH_DIFF = 11644473600000000

_STORE_DIR_MODE = 0o700
"""Mode of the cookie store directory and of any directory this module creates."""

_COOKIE_FILE_MODE = 0o600
"""Mode of every cookie file written by this module: owner read/write only."""

DEFAULT_CACHE_TTL_SECONDS: float = DEFAULT_COOKIE_CACHE_TTL
"""How long an extracted cookie cache is reused before the browser is read again.

Fifteen minutes is deliberately short. The cost of a stale cache is a run that
scrapes with a session the browser has already replaced (after a re-login), so
the window has to stay small; the benefit -- not decrypting the whole cookie
database on every invocation -- does not need a long window, because a cache
written a minute ago is already reused by the next run.
"""

_CACHE_FORMAT_VERSION = 1
"""Bumped when the cache layout changes, so old files are never re-used."""

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


def _flatpak_app_id(cookie_db: Path) -> str | None:
    """Return the Flatpak app ID that owns ``cookie_db``, when there is one.

    Flatpak profiles live under ``~/.var/app/<APP_ID>/...``. Parsing the ID out
    of the profile path is more robust than a hardcoded browser table: it is
    already the path the profile was discovered under, and it covers forks and
    renamed app IDs that a table would miss.

    Args:
        cookie_db: Path to a browser's ``Cookies`` database.

    Returns:
        The app ID, or ``None`` when the path is not inside a Flatpak sandbox.
    """
    parts = cookie_db.parts
    for index, part in enumerate(parts):
        if part == "app" and index > 0 and parts[index - 1] == ".var" and index + 1 < len(parts):
            return parts[index + 1]
    return None


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


_V12_KEY_LENGTH = 32
"""Length of the AES-256-GCM key Chromium derives from the portal secret."""

_V12_NONCE_LENGTH = 12
"""AES-GCM nonce length. Chromium stores the nonce before the ciphertext."""

_V12_TAG_LENGTH = 16
"""AES-GCM authentication tag length, appended after the ciphertext."""

_V12_HKDF_SALT = b"fdo_portal_secret_salt"
"""HKDF salt from Chromium's ``SecretPortalKeyProvider``."""

_V12_HKDF_INFO = b"HKDF-SHA-256 AES-256-GCM"
"""HKDF info from Chromium's ``SecretPortalKeyProvider``.

Upstream: ``components/os_crypt/async/browser/secret_portal_key_provider.cc``.
Either constant being wrong yields a key that silently decrypts nothing, so they
are pinned by a test.
"""

_PORTAL_ATTRIBUTE_SERVER = "xdg-desktop-portal"
"""Secret Service ``server`` attribute used by the portal's secret backend."""


def _derive_v12_key(portal_secret: bytes) -> bytes:
    """Derive the ``v12`` AES-256-GCM key from a SecretPortal secret.

    Chromium runs RFC 5869 HKDF-SHA256 over the raw portal secret; the output is
    a 32-byte AES-256 key.

    Args:
        portal_secret: The raw bytes the portal returned for the app.

    Returns:
        The 32-byte key used to decrypt that app's ``v12`` values.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    kdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_V12_KEY_LENGTH,
        salt=_V12_HKDF_SALT,
        info=_V12_HKDF_INFO,
    )
    return kdf.derive(portal_secret)


def _decrypt_v12(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt a ``v12`` payload with AES-256-GCM.

    The payload is ``nonce || ciphertext || tag`` with no associated data. The
    result still carries the 32-byte integrity prefix the caller must strip.

    Args:
        ciphertext: The blob after the three-byte ``b"v12"`` prefix.
        key: A 32-byte key from :func:`_derive_v12_key`.

    Returns:
        The decrypted plaintext.

    Raises:
        ValueError: When the payload is too short to hold a nonce and a tag.
        cryptography.exceptions.InvalidTag: When ``key`` did not encrypt this
            payload.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(ciphertext) < _V12_NONCE_LENGTH + _V12_TAG_LENGTH:
        raise ValueError("v12 payload is too short to hold a nonce and a tag")
    nonce = ciphertext[:_V12_NONCE_LENGTH]
    body = ciphertext[_V12_NONCE_LENGTH:]
    return AESGCM(key).decrypt(nonce, body, None)


def _encrypt_v12(plaintext: bytes, portal_secret: bytes) -> bytes:
    """Encrypt a value using the Chromium ``v12`` scheme (for tests)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive_v12_key(portal_secret)
    nonce = os.urandom(_V12_NONCE_LENGTH)
    payload = b"\x00" * 32 + plaintext
    return b"v12" + nonce + AESGCM(key).encrypt(nonce, payload, None)


class _PortalKeyUnavailableError(Exception):
    """The SecretPortal secret could not be read from the host keyring.

    A private control-flow signal: callers turn it into a cookie warning so a
    missing or locked keyring never aborts an extraction.
    """


@dataclass(frozen=True, slots=True)
class _PortalKeySet:
    """The ``v12`` candidate keys for one profile, or why there are none.

    Attributes:
        keys: Derived AES-256-GCM keys, one per portal secret the keyring holds.
        reason: Human-readable reason ``keys`` is empty, or ``None`` on success.
    """

    keys: tuple[bytes, ...] = ()
    reason: str | None = None


def _portal_secrets_from_keyring(app_id: str) -> tuple[bytes, ...]:
    """Read the SecretPortal secret(s) for ``app_id`` from the host keyring.

    The xdg-desktop-portal Secret backend keeps one binary secret per sandboxed
    application in the host secret service. On KDE that is the KWallet folder
    ``xdg-desktop-portal`` with the app ID as the entry name; GNOME Keyring and
    other backends expose it through the same ``org.freedesktop.secrets``
    attributes. ``secret-tool`` refuses these entries because the secret is
    binary, so the lookup goes through ``secretstorage``, which returns raw
    bytes.

    Locked entries are skipped rather than unlocked: unlocking can pop a desktop
    prompt, which would hang an unattended scrape.

    Args:
        app_id: The Flatpak application ID, e.g. ``"com.google.Chrome"``.

    Returns:
        Every distinct secret stored for ``app_id``, in keyring order. Empty when
        the keyring holds none.

    Raises:
        _PortalKeyUnavailableError: When ``secretstorage`` is not installed, the
            session bus cannot be reached, or the entries cannot be read.
    """
    try:
        import secretstorage
    except ImportError as exc:
        raise _PortalKeyUnavailableError(
            "the optional 'secretstorage' package is not installed; install the "
            "'cookies' extra to read SecretPortal keys"
        ) from exc

    try:
        connection = secretstorage.dbus_init()
    except Exception as exc:
        raise _PortalKeyUnavailableError(f"the session D-Bus is unavailable: {exc}") from exc

    secrets: list[bytes] = []
    locked = 0
    errors: list[str] = []
    try:
        items = secretstorage.search_items(
            connection,
            {"server": _PORTAL_ATTRIBUTE_SERVER, "user": app_id},
        )
        for item in items:
            try:
                if item.is_locked():
                    locked += 1
                    continue
                secret = item.get_secret()
            except Exception as exc:
                errors.append(str(exc))
                continue
            if secret and secret not in secrets:
                secrets.append(secret)
    except Exception as exc:
        raise _PortalKeyUnavailableError(f"could not search the host keyring: {exc}") from exc
    finally:
        try:
            connection.close()
        except Exception as exc:
            logger.debug("could not close the secret service connection: %s", exc)

    if secrets:
        return tuple(secrets)
    if locked:
        raise _PortalKeyUnavailableError(f"the keyring entry for app {app_id!r} is locked")
    if errors:
        raise _PortalKeyUnavailableError(f"could not read the keyring entry for app {app_id!r}: {errors[0]}")
    return ()


class _PortalKeyCache:
    """Read SecretPortal secrets lazily, once per app ID and process.

    The lookup costs a D-Bus round trip to the secret service, so it only runs
    when a ``v12`` value is actually encountered, and both success and failure
    are cached for the lifetime of the process. Caching a failure is deliberate:
    an unavailable keyring would otherwise be re-probed for every ``v12`` cookie
    in the database.
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self._sets: dict[str | None, _PortalKeySet] = {}

    def keys_for(self, app_id: str | None) -> _PortalKeySet:
        """Return the candidate ``v12`` keys for ``app_id``, reading them once.

        Args:
            app_id: The Flatpak application ID, or ``None`` when the profile is
                not inside a Flatpak sandbox.

        Returns:
            The cached key set. ``keys`` is empty when the secret is unavailable,
            and ``reason`` says why.
        """
        if app_id not in self._sets:
            self._sets[app_id] = self._read(app_id)
        return self._sets[app_id]

    def clear(self) -> None:
        """Forget every cached result, so the keyring is read again."""
        self._sets.clear()

    @staticmethod
    def _read(app_id: str | None) -> _PortalKeySet:
        """Read one app's keys, turning every failure into a warning reason."""
        if app_id is None:
            return _PortalKeySet(
                reason=(
                    "the profile is not inside a Flatpak sandbox, so its SecretPortal "
                    "app ID cannot be derived from the path"
                )
            )
        try:
            secrets = _portal_secrets_from_keyring(app_id)
        except _PortalKeyUnavailableError as exc:
            logger.debug("SecretPortal secret for %s is unavailable: %s", app_id, exc)
            return _PortalKeySet(reason=str(exc))
        if not secrets:
            return _PortalKeySet(
                reason=(
                    f"no '{_PORTAL_ATTRIBUTE_SERVER}' secret for app {app_id!r} was found in the host keyring"
                )
            )
        return _PortalKeySet(keys=tuple(_derive_v12_key(secret) for secret in secrets))


_PORTAL_KEY_CACHE = _PortalKeyCache()
"""The process-wide cache the cookie loader shares across profiles."""


def _decrypt_v12_cookie(
    ciphertext: bytes,
    v12_keys: Callable[[], _PortalKeySet] | None,
) -> tuple[str | None, str | None]:
    """Decrypt a ``v12`` payload with lazily fetched portal keys.

    Every candidate is tried and the first one that passes the GCM tag check
    wins, matching how the ``v11`` path copes with a desktop holding several
    keyring entries.

    Args:
        ciphertext: The blob after the three-byte ``b"v12"`` prefix.
        v12_keys: Lazy source of candidate keys, or ``None`` when no lookup is
            available.

    Returns:
        A ``(value, warning)`` tuple, as :func:`_decrypt_chromium_value`.
    """
    if v12_keys is None:
        return None, (
            "v12 (SecretPortal) cookie could not be decrypted: no portal secret lookup is available"
        )
    candidates = v12_keys()
    last_error: Exception | None = None
    for key in candidates.keys:
        try:
            plaintext = _decrypt_v12(ciphertext, key)
            return _strip_integrity_prefix(plaintext).decode("utf-8"), None
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        logger.debug("v12 decryption failed with every candidate portal key: %s", last_error)
    reason = candidates.reason or "the portal secret did not match this cookie"
    return None, f"v12 (SecretPortal) cookie could not be decrypted: {reason}"


def _decrypt_chromium_value(
    encrypted_value: bytes,
    keys_v11: list[bytes],
    v12_keys: Callable[[], _PortalKeySet] | None = None,
) -> tuple[str | None, str | None]:
    """Decrypt a Chromium ``encrypted_value`` blob.

    Args:
        encrypted_value: The raw blob, including its three-byte scheme prefix.
        keys_v11: Candidate AES keys derived from the keyring passwords. Each is
            tried in turn, so the caller does not have to know which secret the
            desktop actually stores.
        v12_keys: Lazy source of ``v12`` keys. It is only called for a ``v12``
            blob, so the keyring round trip is never paid for the other schemes.

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
        return _decrypt_v12_cookie(ciphertext, v12_keys)
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
    """Copy ``original`` to a private temporary file and return the path.

    ``tempfile.mkstemp`` creates the file ``0600`` regardless of the umask, and
    ``copyfile`` (unlike ``copy2``) does not copy the source database's mode
    over it: the temporary copy holds live, decryptable session cookies too, so
    it must not inherit a world-readable mode from the browser's file.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=original.suffix or ".db")
    os.close(fd)
    tmp_path = Path(tmp_name)
    shutil.copyfile(original, tmp_path)
    return tmp_path


def _load_chromium_cookies(cookie_db: Path, keys_v11: list[bytes]) -> tuple[list[_Cookie], list[str]]:
    """Load and decrypt cookies from a Chromium ``Cookies`` SQLite database.

    The ``v12`` key is looked up lazily: the closure below is only called when a
    row actually carries the ``v12`` prefix, so a database with no ``v12``
    cookies never pays for the D-Bus round trip to the secret service.

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
    v12_warning: str | None = None
    other_failures: dict[str, int] = {}
    v12_keys: Callable[[], _PortalKeySet] = partial(_PORTAL_KEY_CACHE.keys_for, _flatpak_app_id(cookie_db))
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
                    decrypted, warn = _decrypt_chromium_value(row["encrypted_value"], keys_v11, v12_keys)
                    if warn:
                        if warn.startswith("v11"):
                            v11_failures += 1
                        elif warn.startswith("v12") or "SecretPortal" in warn:
                            v12_failures += 1
                            v12_warning = warn
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
        detail = v12_warning or "v12 (SecretPortal) cookie could not be decrypted"
        warnings.append(
            f"{v12_failures} cookie(s) could not be decrypted: {detail}. "
            "Launch the browser with the --password-store=basic command-line switch so it "
            "writes v10 cookies instead; deleting the profile is not needed."
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
    """Load cookies from a Netscape ``cookies.txt`` file.

    Only leading whitespace is stripped. A trailing tab is the separator before
    a cookie's value, so stripping it would turn an empty value into a
    six-field line and make the whole cache unreadable. The split is limited to
    seven fields so a value that itself contains a tab survives intact.
    """
    cookies: list[_Cookie] = []
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"could not read cookie file {path}: {exc}")
        return cookies, warnings
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.lstrip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        is_httponly = False
        if line.startswith("#HttpOnly_"):
            is_httponly = True
            line = line[len("#HttpOnly_") :]
        parts = line.split("\t", 6)
        if len(parts) < 7:
            warnings.append(f"{path}:{line_no}: malformed line")
            continue
        domain, _flag, path_str, secure, expiration, name, value = parts
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


def _serialise_netscape(cookies: Sequence[_Cookie]) -> str:
    """Serialise cookies into the Netscape ``cookies.txt`` format.

    This is the single serialiser for both :meth:`CookieStore.export` and the
    extraction cache: one artefact, one format.

    Args:
        cookies: The cookies to write.

    Returns:
        The file content, terminated by a newline.
    """
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in sorted(cookies, key=lambda c: (c.host_key, c.path, c.name)):
        domain = cookie.host_key
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.is_secure else "FALSE"
        prefix = "#HttpOnly_" if cookie.is_httponly else ""
        lines.append(
            f"{prefix}{domain}\t{flag}\t{cookie.path}\t{secure}\t{cookie.expires_utc}"
            f"\t{cookie.name}\t{cookie.value}"
        )
    return "\n".join(lines) + "\n"


def _prepare_store_dir(path: Path) -> None:
    """Create the cookie store directory with owner-only permissions.

    The directory belongs to this subsystem and will hold live session
    cookies, so an existing one is **tightened** to ``0700`` rather than left
    broad or refused: tightening is not destructive (the owner keeps
    read/write/execute) and refusing would break every installation whose
    cache directory was created with the previous, looser behaviour.

    Args:
        path: Directory to create or tighten.

    Raises:
        CookieError: When ``path`` exists but is not a directory, cannot be
            created, or its mode cannot be changed.
    """
    try:
        if path.exists() and not path.is_dir():
            raise CookieError(f"cookie store path is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True, mode=_STORE_DIR_MODE)
        path.chmod(_STORE_DIR_MODE)
    except OSError as exc:
        raise CookieError(f"could not prepare cookie store directory {path}: {exc}") from exc


def _prepare_output_dir(path: Path) -> None:
    """Create a user-supplied output directory without changing an existing one.

    An explicit ``--output`` path may point at ``/tmp`` or at the user's home.
    Chmod-ing those to ``0700`` would break the desktop, so only a directory
    this call actually creates is made private. An existing, broader directory
    is kept as-is with a warning; the file written into it is still ``0600``.

    Args:
        path: Parent directory of an explicitly requested export path.

    Raises:
        CookieError: When ``path`` exists but is not a directory or cannot be
            created.
    """
    try:
        if path.exists():
            if not path.is_dir():
                raise CookieError(f"cookie output path is not a directory: {path}")
            if path.stat().st_mode & 0o077:
                logger.warning(
                    "cookie output directory %s is accessible to group/others; "
                    "the exported file itself is still 0600",
                    path,
                )
            return
        path.mkdir(parents=True, exist_ok=True, mode=_STORE_DIR_MODE)
    except OSError as exc:
        raise CookieError(f"could not prepare cookie output directory {path}: {exc}") from exc


def _write_private_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode ``0600``.

    The content is written to a temporary file that is created ``0600`` by
    ``tempfile.mkstemp`` (which ignores the umask) and then moved into place
    with :meth:`Path.replace`. That way the destination never exists in a
    world-readable state, not even for the duration of the write, and a crash
    mid-write leaves no half-written cookie file behind.

    Args:
        path: Destination file.
        text: Complete file content.

    Raises:
        OSError: When the temporary file cannot be created, written or moved.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, _COOKIE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1  # ownership of the descriptor now belongs to ``handle``
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _selection_digest(settings: CookieSettings) -> str:
    """Return a filesystem-safe digest of the cookie selection.

    The cache must never be served for a different selection: the store can
    hold cookies from many profiles and the same browser can be narrowed with
    ``profile`` and ``source``. Every field that changes what is extracted is
    folded into the digest, so ``--profile A`` and ``--profile B`` cannot read
    each other's cache.

    Args:
        settings: The cookie settings that select the source.

    Returns:
        A 16-character hexadecimal digest, safe as a filename component.
    """
    parts = (
        f"v{_CACHE_FORMAT_VERSION}",
        settings.browser.strip().lower(),
        (settings.source or "any").strip().lower(),
        settings.profile.strip(),
        "flatpak" if settings.flatpak else "system-only",
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


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

    def __init__(
        self,
        settings: CookieSettings,
        *,
        use_cache: bool | None = None,
        cache_ttl: float | None = None,
    ) -> None:
        """Create a store for one cookie selection.

        Args:
            settings: The cookie selection to load.
            use_cache: Whether an existing, fresh extraction cache may be used
                instead of reading the browser again. ``None`` takes the
                setting's value; ``False`` forces a fresh extraction (the
                ``cookies export`` path does this).
            cache_ttl: Override for the cache lifetime. ``None`` takes the
                setting's value.
        """
        self.settings = settings
        self._cookies: list[_Cookie] = []
        self._sources: list[str] = []
        self._warnings: list[str] = []
        self._loaded = False
        self._keys_v11: list[bytes] = []
        self._use_cache = settings.use_cache if use_cache is None else use_cache
        self._cache_ttl = settings.cache_ttl if cache_ttl is None else cache_ttl

    async def load(self, *, use_cache: bool | None = None) -> None:
        """Resolve the configured cookie source and load all cookies.

        With no profile configured, **every** profile of the selected browser is
        used and their cookies are merged. The session usually lives in one
        profile while the other cookies are scattered across the rest, and
        merging beats guessing. On a conflict the value from the profile with
        the stronger session wins.

        A cache written for the same selection by an earlier run is used when it
        is fresh, unexpired and readable; otherwise the browser is read again and
        the cache is rewritten.

        Args:
            use_cache: Overrides the store's ``use_cache`` setting for this call.
                ``False`` forces a fresh extraction; ``None`` keeps the setting.
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

        cache_enabled = self._use_cache if use_cache is None else use_cache
        if cache_enabled:
            cached = await asyncio.to_thread(self._read_cache)
            if cached is not None:
                self._cookies = cached
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
                await asyncio.to_thread(self._write_cache)
            return

        await self._load_all_profiles(profiles)
        await asyncio.to_thread(self._write_cache)

    def _cache_path(self) -> Path:
        """Return the cache file path for this store's selection.

        Returns:
            ``<store_dir>/cache-<browser>-<digest>.txt``. The digest covers the
            browser, source, profile and Flatpak setting, so two selections never
            share a file.
        """
        store_dir = Path(self.settings.store_dir).expanduser()
        digest = _selection_digest(self.settings)
        return store_dir / f"cache-{self.settings.browser or 'cookies'}-{digest}.txt"

    def _read_cache(self) -> list[_Cookie] | None:
        """Load the selection's cached cookies when they are usable.

        The cache is rejected, and the caller falls back to extraction, when it
        is missing, older than the TTL, unreadable, malformed, empty or holds
        nothing but expired cookies. Expired cookies are dropped rather than
        served: the Netscape expiry is authoritative once a cache exists.

        Returns:
            The usable cookies, or ``None`` when the cache must not be used.
        """
        path = self._cache_path()
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            message = f"could not inspect cookie cache {path}: {exc}"
            logger.warning(message)
            self._warnings.append(message)
            return None

        age = time.time() - stat.st_mtime
        if age > self._cache_ttl:
            logger.info(
                "cookie cache %s is %.0fs old (ttl %.0fs); extracting again",
                path,
                age,
                self._cache_ttl,
            )
            return None

        cookies, warnings = _load_netscape_cookies(path)
        if warnings:
            message = f"ignoring corrupt cookie cache {path}: {'; '.join(warnings)}"
            logger.warning(message)
            self._warnings.append(message)
            return None

        now = time.time()
        unexpired = [c for c in cookies if c.expires_utc == 0 or c.expires_utc > now]
        dropped = len(cookies) - len(unexpired)
        if dropped:
            logger.info("dropped %d expired cookie(s) from cache %s", dropped, path)
        if not unexpired:
            logger.info("cookie cache %s holds no usable cookies; extracting again", path)
            return None

        logger.info("reusing %d cookie(s) from cache %s", len(unexpired), path)
        self._sources.append(f"cache: {path}")
        return unexpired

    def _write_cache(self) -> None:
        """Write the extracted cookies to the selection's cache file.

        The cache is an optimisation, so a failure to write it is recorded as a
        warning rather than raised: the extraction itself succeeded.
        """
        if not self._cookies:
            return
        path = self._cache_path()
        try:
            _prepare_store_dir(path.parent)
            _write_private_file(path, _serialise_netscape(self._cookies))
        except (OSError, CookieError) as exc:
            message = f"could not write cookie cache {path}: {exc}"
            logger.warning(message)
            self._warnings.append(message)

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

        Warnings from the extraction are added to the store, so a profile loaded
        as part of a merge still reports cookies that could not be decrypted.

        Args:
            profile: The profile to inspect.

        Returns:
            The decrypted cookies, or ``None`` when the profile cannot be read.
        """
        warnings: list[str] = []
        try:
            if profile.browser == "firefox":
                cookies, warnings = await asyncio.to_thread(_load_firefox_cookies, profile.cookie_db)
            else:
                passwords = await asyncio.to_thread(_get_secret_tool_passwords, profile.product)
                keys = [_derive_key(password) for password in passwords]
                cookies, warnings = await asyncio.to_thread(_load_chromium_cookies, profile.cookie_db, keys)
        except OSError as exc:
            logger.warning("could not read cookies in %s: %s", profile.cookie_db, exc)
            return None
        self._warnings.extend(warnings)
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

        The export always reflects a fresh extraction when the store has not
        been loaded yet: a cached extraction is bypassed so ``cookies export``
        cannot hand back a session the browser has already replaced. The file is
        written atomically with mode ``0600``.

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
                asyncio.run(self.load(use_cache=False))
            else:
                raise CookieError("cookie store not loaded; await load() before export")

        if path is None:
            store_dir = Path(self.settings.store_dir).expanduser()
            _prepare_store_dir(store_dir)
            path = store_dir / f"{self.settings.browser or 'cookies'}.txt"
        else:
            path = Path(path)
            _prepare_output_dir(path.parent)

        _write_private_file(path, _serialise_netscape(self._cookies))

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
