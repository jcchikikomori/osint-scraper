"""Logging setup.

A single ``rich``-backed handler is installed on the root logger. Libraries that
do their own logging (``instaloader``, ``gallery_dl``) are quieted down to
``WARNING`` so that the tool's own output stays readable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

NOISY_LOGGERS = (
    "instaloader",
    "gallery_dl",
    "yt_dlp",
    "httpx",
    "httpcore",
    "urllib3",
)


def setup_logging(level: str = "INFO", *, quiet_libraries: bool = True) -> logging.Logger:
    """Configure and return the application logger.

    Args:
        level: Log level name, e.g. ``"DEBUG"`` or ``"INFO"``.
        quiet_libraries: Whether to reduce third-party loggers to ``WARNING``.

    Returns:
        The configured ``osint_scraper`` logger.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(handler)

    if quiet_libraries:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    logger = logging.getLogger("osint_scraper")
    logger.setLevel(numeric)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger.

    Args:
        name: Optional child name appended to the ``osint_scraper`` namespace.

    Returns:
        The requested logger.
    """
    base = "osint_scraper"
    return logging.getLogger(f"{base}.{name}" if name else base)


def log_extra(**fields: Any) -> str:
    """Format extra fields for a log message.

    Args:
        **fields: Arbitrary key/value pairs to append.

    Returns:
        A ``key=value`` formatted string, empty when no fields were given.
    """
    if not fields:
        return ""
    return " " + " ".join(f"{key}={value!r}" for key, value in fields.items())
