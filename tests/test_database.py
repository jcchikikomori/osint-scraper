"""Tests for :mod:`osint_scraper.database`."""

from __future__ import annotations

from pathlib import Path

from osint_scraper.database import Database, media_key


def test_media_key_ignores_the_query_string() -> None:
    """A CDN signature must not change the identity of the media."""
    first = "https://scontent.cdninstagram.com/v/t51.29350-15/abc.jpg?stp=dst-jpg&_nc_ht=abc"
    second = "https://scontent.cdninstagram.com/v/t51.29350-15/abc.jpg?stp=dst-jpg&_nc_ht=xyz&oe=99"

    assert media_key(first) == media_key(second)
    assert media_key(first) == "/v/t51.29350-15/abc.jpg"


def test_media_key_distinguishes_different_media() -> None:
    """Two different files must not share a key."""
    first = "https://cdn.example.com/a/one.jpg?token=1"
    second = "https://cdn.example.com/a/two.jpg?token=1"

    assert media_key(first) != media_key(second)


def test_media_key_survives_a_trailing_slash() -> None:
    """A trailing slash must not create a second key for the same media."""
    assert media_key("https://cdn.example.com/a/one.jpg/") == media_key("https://cdn.example.com/a/one.jpg")


def test_lookup_media_before_and_after_record(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        assert db.lookup_media("instagram", "/photo.jpg") is None
        db.record_media(
            platform="instagram",
            media_key="/photo.jpg",
            url="https://example.com/photo.jpg",
            path="/tmp/photo.jpg",
            sha256="abc",
            size=123,
        )
        record = db.lookup_media("instagram", "/photo.jpg")
        assert record is not None
        assert record.path == "/tmp/photo.jpg"
        assert record.sha256 == "abc"
        assert record.size == 123
        assert record.completed
    finally:
        db.close()


def test_record_media_is_idempotent(tmp_path: Path) -> None:
    """Recording the same media twice updates the row instead of adding one."""
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        db.record_media(
            platform="instagram",
            media_key="/photo.jpg",
            url="https://example.com/photo.jpg",
            path="/tmp/photo.jpg",
            sha256="abc",
            size=123,
        )
        db.record_media(
            platform="instagram",
            media_key="/photo.jpg",
            url="https://example.com/photo.jpg?token=2",
            path="/tmp/photo_v2.jpg",
            sha256="def",
            size=456,
        )

        conn = db._conn
        assert conn is not None
        row = conn.execute(
            "SELECT path, sha256, size FROM media WHERE platform = ? AND media_key = ?",
            ("instagram", "/photo.jpg"),
        ).fetchone()
        assert row == ("/tmp/photo_v2.jpg", "def", 456)

        count = conn.execute(
            "SELECT COUNT(*) FROM media WHERE platform = ? AND media_key = ?",
            ("instagram", "/photo.jpg"),
        ).fetchone()[0]
        assert count == 1
    finally:
        db.close()


def test_media_is_scoped_per_platform(tmp_path: Path) -> None:
    """The same key on two platforms is two different media."""
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        db.record_media(
            platform="instagram",
            media_key="/photo.jpg",
            url="https://example.com/photo.jpg",
            path="/tmp/ig.jpg",
        )
        assert db.lookup_media("threads", "/photo.jpg") is None
        assert db.lookup_media("instagram", "/photo.jpg") is not None
    finally:
        db.close()


def test_hash_path_lookup(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        assert db.hash_path("deadbeef") is None
        db.record_hash(
            sha256="deadbeef",
            size=99,
            path="/tmp/photo.jpg",
            platform="instagram",
        )
        assert db.hash_path("deadbeef") == "/tmp/photo.jpg"
    finally:
        db.close()


def test_record_post_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        db.record_post(platform="instagram", target="test_user", post_id="123")
        db.record_post(platform="instagram", target="test_user", post_id="123")

        conn = db._conn
        assert conn is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE platform = ? AND post_id = ?",
            ("instagram", "123"),
        ).fetchone()[0]
        assert count == 1
        assert db.has_post("instagram", "123")
        assert not db.has_post("threads", "123")
    finally:
        db.close()


def test_prune_missing_drops_rows_whose_file_is_gone(tmp_path: Path) -> None:
    """A file the user deleted must not keep the history alive."""
    present = tmp_path / "present.jpg"
    present.write_bytes(b"x")

    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        db.record_media(
            platform="instagram",
            media_key="/present.jpg",
            url="https://example.com/present.jpg",
            path=str(present),
        )
        db.record_media(
            platform="instagram",
            media_key="/gone.jpg",
            url="https://example.com/gone.jpg",
            path=str(tmp_path / "gone.jpg"),
        )
        db.record_hash(
            sha256="deadbeef",
            size=1,
            path=str(tmp_path / "gone.jpg"),
            platform="instagram",
        )

        removed = db.prune_missing()

        assert removed == 1
        assert db.lookup_media("instagram", "/present.jpg") is not None
        assert db.lookup_media("instagram", "/gone.jpg") is None
        assert db.hash_path("deadbeef") is None
    finally:
        db.close()


def test_database_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    db = Database(path)
    db.connect()
    try:
        db.record_media(
            platform="threads",
            media_key="/video.mp4",
            url="https://example.com/video.mp4",
            path="/tmp/video.mp4",
            sha256="deadbeef",
            size=1024,
        )
        db.record_post(platform="threads", target="another_user", post_id="456")
    finally:
        db.close()

    assert path.exists()

    db2 = Database(path)
    db2.connect()
    try:
        record = db2.lookup_media("threads", "/video.mp4")
        assert record is not None
        assert record.sha256 == "deadbeef"
        conn = db2._conn
        assert conn is not None
        row = conn.execute(
            "SELECT post_id FROM posts WHERE platform = ? AND post_id = ?",
            ("threads", "456"),
        ).fetchone()
        assert row is not None
        assert row[0] == "456"
    finally:
        db2.close()


def test_stats_counts_every_table(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    db.connect()
    try:
        db.record_media(
            platform="instagram",
            media_key="/a.jpg",
            url="https://example.com/a.jpg",
            path="/tmp/a.jpg",
        )
        db.record_hash(sha256="aa", size=1, path="/tmp/a.jpg", platform="instagram")
        db.record_post(platform="instagram", target="u", post_id="1")

        stats = db.stats()
        assert stats == {"media": 1, "hashes": 1, "posts": 1}
    finally:
        db.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    db.connect()
    db.close()
    db.close()  # should not raise
    assert db._conn is None
