"""Persistent history database for downloaded files and discovered posts.

The database is a plain SQLite file managed through the stdlib :mod:`sqlite3`
module.  Writes are committed immediately so an interrupted run keeps the
history it has already recorded.

Two indexes live here, and they answer two different questions:

``media``
    *Where did this media end up?*  Keyed on the platform plus a **stable** media
    key -- the URL path without its query string.  Instagram and Threads serve
    media from a CDN whose URL carries a short-lived signature, so the same
    image arrives under a different full URL on every run.  Keying on the full
    URL would never match and a resume would download everything again.

``hashes``
    *Do we already have this content?*  Keyed on the SHA-256 digest.  This is
    what catches the same image posted by two different accounts, and what
    confirms that a file still on disk is the file we recorded.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    media_key    TEXT NOT NULL,
    url          TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT,
    size         INTEGER,
    completed    INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE (platform, media_key)
);
CREATE TABLE IF NOT EXISTS hashes (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    path       TEXT NOT NULL,
    platform   TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    target   TEXT NOT NULL,
    post_id  TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    UNIQUE (platform, post_id)
);
CREATE INDEX IF NOT EXISTS idx_media_platform_key ON media (platform, media_key);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts (platform);
"""


def media_key(url: str) -> str:
    """Return the stable identity of the media behind ``url``.

    Instagram and Threads serve media from a CDN whose URL carries a
    short-lived signature -- the same image is served under a different query
    string on every run, and sometimes from a different CDN host.  Keying the
    history on the full URL would therefore never match, and a resume would
    download everything again.

    The path without its query string identifies the media itself and stays
    stable across runs.

    Args:
        url: The media URL.

    Returns:
        A key that is stable for the same media across runs.
    """
    parsed = urlsplit(url)
    path = parsed.path
    if not path:
        return parsed.netloc
    if len(path) > 1:
        path = path.rstrip("/")
    return path


@dataclass(frozen=True, slots=True)
class MediaRecord:
    """A media item the history knows about.

    Attributes:
        platform: Platform the media came from.
        media_key: Stable identity of the media, see :func:`media_key`.
        url: The URL the media was last downloaded from.
        path: Where the file was written.
        sha256: Digest of the downloaded bytes, when known.
        size: Size of the downloaded file in bytes, when known.
        completed: Whether the download finished.
        created_at: Unix timestamp of the first sighting.
    """

    platform: str
    media_key: str
    url: str
    path: str
    sha256: str | None
    size: int | None
    completed: bool
    created_at: int


class Database:
    """SQLite-backed history store.

    Args:
        path: Filesystem path for the SQLite database.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        """Return the database file path."""
        return self._path

    def connect(self) -> None:
        """Open the database, enable WAL and create the schema if necessary."""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # The connection is opened on whichever thread first touches the
        # database -- pruning runs in a worker thread -- and closed on the
        # main thread.  Every statement is executed synchronously, so there is
        # never more than one statement in flight.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._connection().commit()

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._conn is None:
            return
        try:
            self._conn.close()
        finally:
            self._conn = None

    # -- media index ----------------------------------------------------

    def lookup_media(self, platform: str, key: str) -> MediaRecord | None:
        """Return the history record for one media item, if it is known.

        Args:
            platform: Platform the media belongs to.
            key: Stable media key, see :func:`media_key`.

        Returns:
            The record, or ``None`` when the media is not in the history.
        """
        self._ensure_connected()
        with closing(self._connection().cursor()) as cursor:
            cursor.execute(
                """
                SELECT platform, media_key, url, path, sha256, size, completed, created_at
                FROM media WHERE platform = ? AND media_key = ?
                """,
                (platform, key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return MediaRecord(
            platform=row[0],
            media_key=row[1],
            url=row[2],
            path=row[3],
            sha256=row[4],
            size=row[5],
            completed=bool(row[6]),
            created_at=row[7],
        )

    def record_media(
        self,
        *,
        platform: str,
        media_key: str,
        url: str,
        path: str,
        sha256: str | None = None,
        size: int | None = None,
        completed: bool = True,
    ) -> None:
        """Record a media item, updating any existing row for the same key.

        Args:
            platform: Platform the media belongs to.
            media_key: Stable media key, see :func:`media_key`.
            url: The URL the media was downloaded from.
            path: Where the file was written.
            sha256: Digest of the downloaded bytes, when known.
            size: Size of the downloaded file in bytes, when known.
            completed: Whether the download finished.
        """
        self._ensure_connected()
        now = int(time.time())
        with closing(self._connection().cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO media (
                    platform, media_key, url, path, sha256, size,
                    completed, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, media_key) DO UPDATE SET
                    url=excluded.url,
                    path=excluded.path,
                    sha256=excluded.sha256,
                    size=excluded.size,
                    completed=excluded.completed,
                    completed_at=excluded.completed_at
                """,
                (
                    platform,
                    media_key,
                    url,
                    path,
                    sha256,
                    size,
                    1 if completed else 0,
                    now,
                    now if completed else None,
                ),
            )
            self._connection().commit()

    # -- content index --------------------------------------------------

    def hash_path(self, sha256: str) -> str | None:
        """Return where a given content digest is already stored.

        Args:
            sha256: The content digest.

        Returns:
            The path of the known copy, or ``None`` when the digest is unknown.
        """
        self._ensure_connected()
        with closing(self._connection().cursor()) as cursor:
            cursor.execute("SELECT path FROM hashes WHERE sha256 = ?", (sha256,))
            row = cursor.fetchone()
        return row[0] if row else None

    def record_hash(
        self,
        *,
        sha256: str,
        size: int,
        path: str,
        platform: str,
    ) -> None:
        """Record where a piece of content is stored.

        Args:
            sha256: The content digest.
            size: Size of the file in bytes.
            path: Where the file lives.
            platform: Platform the content came from.
        """
        self._ensure_connected()
        now = int(time.time())
        with closing(self._connection().cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO hashes (sha256, size, path, platform, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    size=excluded.size,
                    path=excluded.path
                """,
                (sha256, size, path, platform, now),
            )
            self._connection().commit()

    # -- posts ----------------------------------------------------------

    def has_post(self, platform: str, post_id: str) -> bool:
        """Return whether a post has been seen before."""
        self._ensure_connected()
        with closing(self._connection().cursor()) as cursor:
            cursor.execute(
                "SELECT 1 FROM posts WHERE platform = ? AND post_id = ?",
                (platform, post_id),
            )
            return cursor.fetchone() is not None

    def record_post(
        self,
        *,
        platform: str,
        target: str,
        post_id: str,
    ) -> None:
        """Record that a post was seen.

        The ``(platform, post_id)`` pair is the uniqueness key, so duplicate
        inserts are ignored.
        """
        self._ensure_connected()
        now = int(time.time())
        with closing(self._connection().cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO posts (platform, target, post_id, first_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, post_id) DO UPDATE SET
                    target=excluded.target
                """,
                (platform, target, post_id, now),
            )
            self._connection().commit()

    # -- maintenance ----------------------------------------------------

    def prune_missing(self) -> int:
        """Drop rows whose file is no longer on disk.

        A file the user deleted must not keep the history alive: the next run
        has to be able to download it again.  The stale rows are removed so the
        resume check sees a missing file rather than a phantom.

        Returns:
            The number of media rows that were removed.
        """
        self._ensure_connected()
        with closing(self._connection().cursor()) as cursor:
            cursor.execute("SELECT id, path FROM media")
            stale = [row[0] for row in cursor.fetchall() if not Path(row[1]).exists()]
            if stale:
                cursor.executemany("DELETE FROM media WHERE id = ?", [(pid,) for pid in stale])
            cursor.execute("SELECT sha256, path FROM hashes")
            stale_hashes = [row[0] for row in cursor.fetchall() if not Path(row[1]).exists()]
            if stale_hashes:
                cursor.executemany(
                    "DELETE FROM hashes WHERE sha256 = ?",
                    [(digest,) for digest in stale_hashes],
                )
            self._connection().commit()
        return len(stale)

    def stats(self) -> dict[str, int]:
        """Return a small summary of what the history holds.

        Returns:
            A mapping of table name to row count.
        """
        self._ensure_connected()
        counts: dict[str, int] = {}
        with closing(self._connection().cursor()) as cursor:
            for table in ("media", "hashes", "posts"):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                row = cursor.fetchone()
                counts[table] = row[0] if row else 0
        return counts

    # -- internals ------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._conn is None:
            self.connect()

    def _connection(self) -> sqlite3.Connection:
        """Return the open connection, opening it if necessary.

        Returns:
            The live SQLite connection.

        Raises:
            RuntimeError: If the connection cannot be opened.
        """
        self._ensure_connected()
        if self._conn is None:  # pragma: no cover - connect() always assigns
            raise RuntimeError(f"could not open the database at {self._path}")
        return self._conn
