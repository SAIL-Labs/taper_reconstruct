"""
Logging helpers for the taper tools
====================================

Everything the recorder and the reconstruction code do is written to a
rolling application log, plus a per-run log file that sits next to the
artefact it describes:

  - a recording writes ``<video>.log``  (and ``<video>.meta.json``)
  - a reconstruction writes ``<output>.log``

The application log lives in ``~/.taper_tools/logs/taper_tools.log``
(override with the ``TAPER_TOOLS_LOG_DIR`` environment variable) and is
rotated at 2 MB, keeping five backups.  When a reconstruction fails the
traceback is in both the per-run log and the application log, so the user
only has to send one file.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import tempfile
import threading
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

PACKAGE_LOGGER = "taper_tools"
APP_LOG_NAME = "taper_tools.log"

FILE_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-26s  %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
CONSOLE_FORMAT = "%(message)s"

_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 5

_APP_HANDLER_TAG = "_taper_app_file_handler"
_CONSOLE_HANDLER_TAG = "_taper_console_handler"


# ---------------------------------------------------------------------------
# Log locations
# ---------------------------------------------------------------------------
def get_log_dir() -> Path:
    """Return a writable directory for application logs.

    Falls back to the system temp directory if the preferred location
    cannot be created (read-only home, locked-down machine, ...).
    """
    candidates = []
    override = os.environ.get("TAPER_TOOLS_LOG_DIR")
    if override:
        candidates.append(Path(override))
    candidates.append(Path.home() / ".taper_tools" / "logs")
    candidates.append(Path(tempfile.gettempdir()) / "taper_tools_logs")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def app_log_path() -> Path:
    """Path of the rolling application log."""
    return get_log_dir() / APP_LOG_NAME


# ---------------------------------------------------------------------------
# Handler setup
# ---------------------------------------------------------------------------
def _has_tagged_handler(logger, tag):
    return any(getattr(h, tag, False) for h in logger.handlers)


def setup_app_logging(level=logging.INFO, console=True, console_stream=None):
    """Attach the rolling file handler (and optionally a console handler).

    Safe to call more than once — handlers are only added once.

    Returns
    -------
    path : Path or None
        The application log path, or None if no file could be opened.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    path = app_log_path()
    if not _has_tagged_handler(logger, _APP_HANDLER_TAG):
        try:
            handler = RotatingFileHandler(
                path,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            path = None
        else:
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
            setattr(handler, _APP_HANDLER_TAG, True)
            logger.addHandler(handler)

    if console and not _has_tagged_handler(logger, _CONSOLE_HANDLER_TAG):
        stream = console_stream if console_stream is not None else sys.stdout
        if stream is not None:
            handler = logging.StreamHandler(stream)
            handler.setLevel(level)
            handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
            setattr(handler, _CONSOLE_HANDLER_TAG, True)
            logger.addHandler(handler)

    return path


class _ThreadFilter(logging.Filter):
    """Only pass records emitted by one thread."""

    def __init__(self, thread_ident):
        super().__init__()
        self.thread_ident = thread_ident

    def filter(self, record):
        return record.thread == self.thread_ident


@contextmanager
def file_log(path, level=logging.DEBUG, thread_only=False, header=None):
    """Tee package log records into ``path`` for the duration of the block.

    Parameters
    ----------
    path : str or Path or None
        Destination log file.  ``None`` disables the per-run log.
    level : int
        Minimum level captured in this file.
    thread_only : bool
        If True, only records emitted by the calling thread are captured.
        Used so that two concurrent reconstructions do not write into each
        other's log.
    header : str, optional
        First line written to the file.

    Yields
    ------
    path : Path or None
        The log file actually opened (None if it could not be created).
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(logging.DEBUG)
    handler = None

    if path is not None:
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not open log file %s: %s", path, exc)
            handler = None
            path = None

    if handler is not None:
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
        if thread_only:
            handler.addFilter(_ThreadFilter(threading.get_ident()))
        logger.addHandler(handler)

    try:
        if header:
            logger.info("%s", header)
        yield path
    finally:
        if handler is not None:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Environment description
# ---------------------------------------------------------------------------
def _module_version(name):
    try:
        module = __import__(name)
    except Exception:
        return "not installed"
    return getattr(module, "__version__", "unknown")


def describe_environment():
    """Return a dict of version/platform info worth having in every log."""
    try:
        from taper_tools._version import __version__ as taper_version
    except Exception:
        taper_version = "unknown"

    info = {
        "taper_tools": taper_version,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "numpy": _module_version("numpy"),
        "opencv": _module_version("cv2"),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }
    for optional in ("mss", "scipy"):
        info[optional] = _module_version(optional)
    try:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR

        info["Qt"] = QT_VERSION_STR
        info["PyQt6"] = PYQT_VERSION_STR
    except Exception:
        pass
    return info


def log_environment(logger, extra=None):
    """Write the environment block (and any extra key/values) to ``logger``."""
    logger.info("--- environment ---")
    for key, value in describe_environment().items():
        logger.info("  %-12s %s", key, value)
    if extra:
        logger.info("--- context ---")
        for key, value in extra.items():
            logger.info("  %-12s %s", key, value)
    logger.info("-------------------")
