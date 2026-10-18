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
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".meta.json"
LOG_SUFFIX = ".log"
SCHEMA_VERSION = 1

FILENAME_PREFIX = "taper_scan"
PROCESS_PREFIX = "taper_process"
_SPEED_RE = re.compile(r"_(\d+(?:p\d+)?)umps(?:_|\.|$)", re.IGNORECASE)

# Recordings whose device folder does not exist yet (a taper still being
# drawn) wait here, under the output folder, until they are filed.
HOLDING_DIRNAME = "unfiled"

# What a recording is of: a diameter scan to be stitched into one image,
# or the taper being drawn, kept as an ordinary video.
KIND_SCAN = "scan"
KIND_PROCESS = "process"


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


def build_process_filename(label=None, timestamp=None, extension=".mp4"):
    """Build ``taper_process_<timestamp>[_<label>].mp4`` for an in-progress recording."""
    if timestamp is None:
        timestamp = datetime.now()
    if isinstance(timestamp, datetime):
        timestamp = timestamp.strftime("%Y%m%d_%H%M%S")
    parts = [PROCESS_PREFIX, timestamp]
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
        ``label``, ``kind``, plus ``source`` saying where the scan speed
        came from.
    """
    params = {}
    meta = read_metadata(video_path)
    if meta:
        speed = meta.get("scan_speed_um_s")
        if speed:
            params["scan_speed"] = float(speed)
            params["source"] = str(sidecar_path(video_path))
        fps = meta.get("effective_fps")
        if fps:
            params["fps"] = float(fps)
        if meta.get("axis") is not None:
            params["axis"] = int(meta["axis"])
        if meta.get("label"):
            params["label"] = meta["label"]
        if meta.get("kind"):
            params["kind"] = meta["kind"]

    # A sidecar without a speed (hand-edited, older schema) must not hide
    # the speed written into the file name.
    if "scan_speed" not in params:
        speed = parse_speed_from_name(video_path)
        if speed:
            params["scan_speed"] = speed
            params["source"] = "file name"
        elif meta:
            log.warning(
                "%s has no scan speed and the file name does not encode one",
                sidecar_path(video_path).name,
            )
    return params


# ---------------------------------------------------------------------------
# Filing recordings into a device folder
# ---------------------------------------------------------------------------
VIDEO_SUFFIXES = (".mp4", ".avi", ".mkv", ".mov")


def holding_dir(output_dir):
    """The holding folder for recordings not yet filed."""
    return Path(output_dir) / HOLDING_DIRNAME


def companion_files(video_path):
    """Every file that belongs with a video: the sidecar, logs, reconstruction.

    They all share the video's stem (``scan.mp4`` -> ``scan.meta.json``,
    ``scan.log``, ``scan.reconstruct.log``, ``scan.reconstructed.png``), so
    moving a recording means moving this whole set.
    """
    video_path = Path(video_path)
    if not video_path.parent.exists():
        return []
    return sorted(
        f for f in video_path.parent.glob(video_path.stem + ".*") if f.is_file()
    )


def held_recordings(output_dir):
    """Videos waiting in the holding folder, newest first."""
    folder = holding_dir(output_dir)
    if not folder.is_dir():
        return []
    videos = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_SUFFIXES
    ]
    return sorted(videos, key=lambda f: f.stat().st_mtime, reverse=True)


def move_recording(video_path, dest_dir):
    """Move a video and its companions into ``dest_dir``.

    Refuses rather than overwrites if any destination already exists.

    Returns
    -------
    new_video_path : Path
    """
    video_path = Path(video_path)
    dest_dir = Path(dest_dir)
    files = companion_files(video_path)
    if video_path not in files:
        raise FileNotFoundError(f"Recording not found: {video_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    clashes = [f.name for f in files if (dest_dir / f.name).exists()]
    if clashes:
        raise FileExistsError(
            f"Already in {dest_dir}: {', '.join(clashes)}"
        )
    for f in files:
        shutil.move(str(f), str(dest_dir / f.name))
        log.info("Filed %s -> %s", f.name, dest_dir)
    return dest_dir / video_path.name
