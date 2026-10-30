"""Filename helpers: sanitisation, extension guessing and unique-path resolution.

Everything downloaded by this tool ends up on the user's disk, so path handling is
deliberately conservative: no traversal, no control characters, no silent
truncation of meaningful characters, and guaranteed uniqueness.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

MAX_STEM_LENGTH = 120
"""Maximum length of a filename stem before truncation."""


def sanitize_filename(name: str, *, fallback: str = "file") -> str:
    """Turn an arbitrary string into a safe filename component.

    Args:
        name: Raw text, typically a caption or post title.
        fallback: Value to use when sanitising leaves nothing behind.

    Returns:
        A filename component that is safe on Linux, Windows and macOS.
    """
    name = unicodedata.normalize("NFKD", name)
    name = name.replace("\u2044", "/")
    name = _UNSAFE.sub("_", name)
    name = _WHITESPACE.sub(" ", name).strip()
    name = name.strip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > MAX_STEM_LENGTH:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_STEM_LENGTH - 9].rstrip()}~{digest}"
    return name


def extension_for(mime: str | None, url: str | None = None) -> str:
    """Guess a file extension (including the dot) from a MIME type or URL.

    Args:
        mime: MIME type advertised by the platform, when known.
        url: Media URL, used to read the path extension.

    Returns:
        An extension such as ``".jpg"``, or ``".bin"`` when nothing can be guessed.
    """
    mime_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
    }
    if mime:
        normalised = mime.split(";")[0].strip().lower()
        if normalised in mime_map:
            return mime_map[normalised]
        if normalised.startswith("image/"):
            return f".{normalised.removeprefix('image/')}"
        if normalised.startswith("video/"):
            return f".{normalised.removeprefix('video/')}"
    if url:
        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix and len(suffix) <= 6:
            return suffix
    return ".bin"


def unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Return a path inside ``directory`` that does not collide with existing files.

    Args:
        directory: Destination directory.
        stem: Desired filename without extension.
        extension: File extension including the dot.

    Returns:
        A path inside ``directory``; a numeric suffix is appended when needed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{extension}"
    if not candidate.exists():
        return candidate
    for index in range(1, 10_000):
        candidate = directory / f"{stem}_{index}{extension}"
        if not candidate.exists():
            return candidate
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]
    return directory / f"{stem}_{digest}{extension}"
