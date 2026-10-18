"""
Recording sidecar metadata
===========================

Every recording made by the taper recorder is written with two companion
files:

  ``taper_scan_20260822_143000_1000umps_lantern7.mp4``   the video
  ``taper_scan_20260822_143000_1000umps_lantern7.meta.json``   this metadata
  ``taper_scan_20260822_143000_1000umps_lantern7.log``   the capture log

The scan speed is in the file name so a video is never orphaned from the
one number the reconstruction cannot work without.  The metadata file
carries the same value in machine-readable form, along with the frame
rate actually achieved during capture, which is what the reconstruction
should use to convert pixel shifts into microns.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".meta.json"
LOG_SUFFIX = ".log"
SCHEMA_VERSION = 1

FILENAME_PREFIX = "taper_scan"
_SPEED_RE = re.compile(r"_(\d+(?:p\d+)?)umps(?:_|\.|$)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------
def slugify(text, max_length=40):
    """Reduce free text to something safe for a file name."""
    if not text:
        return ""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text).strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return slug[:max_length]


def format_speed(speed):
    """Format a scan speed for a file name: 1000 -> '1000', 12.5 -> '12p5'."""
    speed = float(speed)
    if speed.is_integer():
        return str(int(speed))
    return f"{speed:g}".replace(".", "p")


def build_video_filename(scan_speed, label=None, timestamp=None, extension=".mp4"):
    """Build ``taper_scan_<timestamp>_<speed>umps[_<label>].mp4``."""
    if timestamp is None:
        timestamp = datetime.now()
    if isinstance(timestamp, datetime):
        timestamp = timestamp.strftime("%Y%m%d_%H%M%S")

    parts = [FILENAME_PREFIX, timestamp, f"{format_speed(scan_speed)}umps"]
    slug = slugify(label)
    if slug:
        parts.append(slug)
    return "_".join(parts) + extension


def parse_speed_from_name(path):
    """Recover the scan speed (um/s) from a file name, or None."""
    match = _SPEED_RE.search(Path(path).name)
    if not match:
        return None
    try:
        return float(match.group(1).replace("p", "."))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sidecar read/write
# ---------------------------------------------------------------------------
def sidecar_path(video_path):
    """Metadata path for a video: ``scan.mp4`` -> ``scan.meta.json``."""
    return Path(video_path).with_suffix(SIDECAR_SUFFIX)


def recording_log_path(video_path):
    """Capture log path for a video: ``scan.mp4`` -> ``scan.log``."""
    return Path(video_path).with_suffix(LOG_SUFFIX)


def write_metadata(video_path, metadata):
    """Write the sidecar next to ``video_path``.  Never raises."""
    path = sidecar_path(video_path)
    payload = {"schema": SCHEMA_VERSION, **metadata}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        log.warning("Could not write metadata sidecar %s: %s", path, exc)
        return None
    log.info("Wrote metadata sidecar: %s", path)
    return path


def read_metadata(video_path):
    """Read the sidecar for ``video_path``, or None if absent/unreadable."""
    path = sidecar_path(video_path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Could not read metadata sidecar %s: %s", path, exc)
        return None


def scan_parameters(video_path):
    """Best-effort scan parameters for a video.

    Prefers the sidecar, falls back to the speed encoded in the file name.

    Returns
    -------
    params : dict
        Keys present only when known: ``scan_speed``, ``fps``, ``axis``,
        ``label``, plus ``source`` describing where they came from.
    """
    params = {}
    meta = read_metadata(video_path)
    if meta:
        speed = meta.get("scan_speed_um_s")
        if speed:
            params["scan_speed"] = float(speed)
        fps = meta.get("effective_fps")
        if fps:
            params["fps"] = float(fps)
        if meta.get("axis") is not None:
            params["axis"] = int(meta["axis"])
        if meta.get("label"):
            params["label"] = meta["label"]
        params["source"] = str(sidecar_path(video_path))
        return params

    speed = parse_speed_from_name(video_path)
    if speed:
        params["scan_speed"] = speed
        params["source"] = "file name"
    return params
