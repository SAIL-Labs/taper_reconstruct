"""
Taper Image Reconstruction from Diameter Scan Video
=====================================================

Reconstructs a full image of a photonic lantern taper from a screen-captured
diameter scan video. The scan moves the taper through the camera FOV at a
known (approximately constant) speed along one axis.

Three alignment modes:
  - 'auto' (default): uses phase correlation to measure raw shifts, then
    derives the pixel scale from the known scan speed and the dominant
    shift peak. Outlier shifts are replaced with the expected value.
  - 'correlation': uses phase correlation only (no outlier replacement).
  - 'constant': uses the known scan speed, frame rate, and pixel scale
    to compute a fixed shift per frame (faster, but assumes constant speed).

Videos recorded with the taper recorder carry a ``.meta.json`` sidecar
holding the scan speed and the frame rate actually achieved during
capture; both are picked up automatically, so ``--speed`` is only needed
for videos from elsewhere.

Every run writes a log (``<output>.log`` by default) with the full set of
inputs, the alignment diagnostics and, if the run fails, the traceback.

Usage:
    taper-reconstruct video.mp4 --speed 1000 --output taper.png
    taper-reconstruct video.mp4 --mode constant --speed 1000 --pixel_scale 1.5

Author: Claude (for Chris @ SAIL)
"""

import argparse
import logging
import sys
import traceback
from pathlib import Path

import cv2
import numpy as np
from tqdm import tqdm

from taper_tools import sidecar
from taper_tools.logging_utils import (
    app_log_path,
    file_log,
    log_environment,
    setup_app_logging,
)

# Explicit name (not __name__) so the package handlers still catch these
# records when the module is run as a script or from a frozen build.
log = logging.getLogger("taper_tools.reconstruct")


def _progress(iterable, total, desc):
    """tqdm when we have a real terminal, plain iteration otherwise.

    A windowed executable has no usable stderr, so the progress bar is
    skipped there; the log gets percentage updates instead.
    """
    stderr = getattr(sys, "stderr", None)
    try:
        interactive = stderr is not None and stderr.isatty()
    except Exception:
        interactive = False
    if not interactive:
        return iterable
    return tqdm(iterable, total=total, desc=desc)


def extract_frames(video_path, roi=None, skip=1):
    """
    Generator that yields frames from a video file.

    Parameters
    ----------
    video_path : str or Path
        Path to the video file.
    roi : tuple of (y1, y2, x1, x2), optional
        Region of interest to crop from each frame. Useful if the video
        includes UI elements (e.g. diameter readout, controls) that should
        be excluded from the stitching.
    skip : int
        Only yield every `skip`-th frame (1 = every frame).

    Yields
    ------
    frame : np.ndarray
        Grayscale frame (or cropped region).
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % skip == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if roi is not None:
                y1, y2, x1, x2 = roi
                gray = gray[y1:y2, x1:x2]
            yield gray
        idx += 1
    cap.release()


def estimate_shift_correlation(prev, curr, axis=1):
    """
    Estimate the sub-pixel shift between two frames using phase correlation.

    Parameters
    ----------
    prev, curr : np.ndarray
        Consecutive grayscale frames.
    axis : int
        0 for vertical scan, 1 for horizontal scan.

    Returns
    -------
    shift : float
        Estimated shift in pixels along the scan axis.
    """
    # Phase correlation gives (x_shift, y_shift)
    shift, response = cv2.phaseCorrelate(
        prev.astype(np.float64), curr.astype(np.float64)
    )
    # shift is (dx, dy)
    if axis == 1:
        return shift[0]
    else:
        return shift[1]


def find_peak_shift(shifts):
    """
    Find the dominant shift value using robust statistics.

    Tries scipy KDE first for a smooth peak estimate, falls back to
    numpy histogram, then median.

    Parameters
    ----------
    shifts : array-like
        Raw per-frame shift values.

    Returns
    -------
    peak : float
        The dominant shift value.
    """
    abs_shifts = np.abs(shifts)
    # Remove near-zero shifts (stationary frames / failed correlations)
    abs_shifts = abs_shifts[abs_shifts > 0.5]
    if len(abs_shifts) < 5:
        log.warning(
            "Only %d frames with a measurable shift; falling back to the median. "
            "The scan may not have been moving, or the region may be static.",
            len(abs_shifts),
        )
        return float(np.median(np.abs(shifts)))

    try:
        from scipy.stats import gaussian_kde

        kde = gaussian_kde(abs_shifts)
        x = np.linspace(abs_shifts.min(), abs_shifts.max(), 500)
        peak = x[np.argmax(kde(x))]
        log.debug("Peak shift from scipy KDE over %d values", len(abs_shifts))
    except ImportError:
        # Fallback: histogram peak
        counts, edges = np.histogram(abs_shifts, bins="auto")
        peak_bin = np.argmax(counts)
        peak = (edges[peak_bin] + edges[peak_bin + 1]) / 2.0
        log.debug("scipy unavailable; peak shift from histogram")

    return float(peak)


def get_video_info(video_path):
    """Return (fps, frame_count, width, height) for a video."""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return fps, n_frames, w, h


def reconstruct_taper(
    video_path,
    mode="auto",
    scan_speed=None,
    pixel_scale=None,
    axis=1,
    roi=None,
    skip=1,
    blend=True,
    tolerance=0.3,
    fps_override=None,
):
    """
    Reconstruct the full taper image from a scan video.

    Parameters
    ----------
    video_path : str or Path
        Path to the video.
    mode : str
        'auto', 'correlation', or 'constant'.
    scan_speed : float, optional
        Scan speed in um/s (required for 'auto' and 'constant' modes).
    pixel_scale : float, optional
        um per pixel (required for 'constant' mode).
    axis : int
        Scan axis: 1 = horizontal (left-right), 0 = vertical (top-bottom).
    roi : tuple, optional
        (y1, y2, x1, x2) crop region.
    skip : int
        Frame skip factor.
    blend : bool
        If True, use linear blending in overlap regions.
    tolerance : float
        For 'auto' mode: fractional deviation from expected shift beyond
        which a frame's shift is replaced (default 0.3 = 30%).
    fps_override : float, optional
        Frame rate to use instead of the one stored in the container.
        The recorder measures the rate it actually achieved and saves it
        in the sidecar; if the capture ran slower than requested, the
        container frame rate would bias the derived pixel scale.

    Returns
    -------
    result : np.ndarray
        Reconstructed taper image.
    shifts : list of float
        Per-frame shifts in pixels.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    log.info("--- reconstruction inputs ---")
    log.info("  video        %s (%.1f MB)", video_path, video_path.stat().st_size / 1e6)
    log.info("  mode         %s", mode)
    log.info("  scan speed   %s um/s", scan_speed)
    log.info("  pixel scale  %s um/px", pixel_scale)
    log.info("  axis         %d (%s)", axis, "horizontal" if axis == 1 else "vertical")
    log.info("  roi          %s", roi)
    log.info("  skip         %d", skip)
    log.info("  blend        %s", blend)
    log.info("  tolerance    %s", tolerance)
    log.info("  fps override %s", fps_override)
    log.info("-----------------------------")

    fps, n_frames, vid_w, vid_h = get_video_info(video_path)
    log.info("Video: %dx%d, %.1f fps, %d frames", vid_w, vid_h, fps, n_frames)

    if fps_override:
        log.info(
            "Using measured capture rate %.4f fps instead of the container's %.4f fps",
            float(fps_override),
            fps,
        )
        fps = float(fps_override)

    if not fps or not np.isfinite(fps) or fps <= 0:
        raise ValueError(
            f"Video reports an unusable frame rate ({fps}). "
            "Pass an explicit frame rate to continue."
        )
    if n_frames < 2:
        raise ValueError(
            f"Video has {n_frames} frame(s); at least 2 are needed to stitch a taper."
        )

    n_used = n_frames // skip
    log.info("Using every %d-th frame -> ~%d frames", skip, n_used)

    # Validate mode requirements
    if mode == "constant":
        if scan_speed is None or pixel_scale is None:
            raise ValueError(
                "scan_speed and pixel_scale are required for 'constant' mode"
            )
        px_per_sec = scan_speed / pixel_scale
        constant_shift = px_per_sec / (fps / skip)
        log.info("Constant shift: %.2f px/frame", constant_shift)
    elif mode == "auto":
        if scan_speed is None:
            raise ValueError("scan_speed is required for 'auto' mode")

    # First pass: collect shifts
    log.info("Pass 1: estimating shifts...")
    shifts = []
    prev_frame = None
    frame_gen = extract_frames(video_path, roi=roi, skip=skip)
    report_every = max(1, n_used // 10)

    for i, frame in enumerate(_progress(frame_gen, n_used, "Shifts")):
        if prev_frame is not None:
            if mode == "constant":
                shifts.append(constant_shift)
            else:
                # Both 'correlation' and 'auto' use phase correlation
                s = estimate_shift_correlation(prev_frame, frame, axis=axis)
                shifts.append(s)
        prev_frame = frame
        if i and i % report_every == 0:
            log.debug("  shifts: frame %d/%d", i, n_used)

    if not shifts:
        raise ValueError(
            "No frame pairs were read from the video — it may be truncated or "
            "the skip factor may exceed the frame count."
        )
    log.info("Measured %d inter-frame shifts", len(shifts))

    # Auto mode: find dominant shift, derive pixel scale, replace outliers
    if mode == "auto":
        raw_shifts = np.array(shifts)
        # Determine scan sign from cumulative sum
        sign = np.sign(np.sum(raw_shifts)) or 1.0
        peak_shift = find_peak_shift(raw_shifts) * sign
        abs_peak = abs(peak_shift)

        if abs_peak < 1e-6:
            raise ValueError(
                "No consistent frame-to-frame motion was detected. Check that "
                "the captured region covers the moving taper image and that the "
                "scan axis setting matches the direction of travel."
            )

        # Derive pixel scale: speed [um/s] / (shift [px/frame] * fps/skip [frame/s])
        derived_pixel_scale = scan_speed / (abs_peak * fps / skip)
        log.info("--- Auto mode diagnostics ---")
        log.info("Peak shift: %.3f px/frame", peak_shift)
        log.info("Derived pixel scale: %.4f um/px", derived_pixel_scale)
        log.info(
            "Raw shift stats: median %.3f, mean %.3f, std %.3f, min %.3f, max %.3f",
            float(np.median(raw_shifts)),
            float(np.mean(raw_shifts)),
            float(np.std(raw_shifts)),
            float(np.min(raw_shifts)),
            float(np.max(raw_shifts)),
        )

        # Replace outliers
        n_replaced = 0
        for j in range(len(shifts)):
            deviation = abs(shifts[j] - peak_shift) / abs_peak
            if deviation > tolerance:
                shifts[j] = peak_shift
                n_replaced += 1

        frac = n_replaced / len(shifts) * 100
        log.info("Replaced %d/%d shifts (%.1f%%)", n_replaced, len(shifts), frac)
        log.info("Shift range (after): [%.3f, %.3f]", min(shifts), max(shifts))
        log.info("-----------------------------")
        if frac > 50:
            log.warning(
                "More than half the shifts were replaced (%.1f%%). The scan speed, "
                "frame rate or scan axis may be wrong, or the scan may have "
                "stalled part-way through.",
                frac,
            )

    # Compute cumulative positions
    cum_shifts = np.cumsum(shifts)
    cum_shifts = np.insert(cum_shifts, 0, 0.0)  # first frame at position 0

    # Handle negative shifts (scan could go either direction)
    if cum_shifts[-1] < 0:
        log.info("Detected reverse scan direction, flipping.")
        cum_shifts = -cum_shifts

    # Frames are placed at whole pixels; shift the whole run so the
    # earliest frame starts at zero (a 'correlation' run can wander
    # backwards, which would otherwise index the canvas from the end).
    positions = np.round(cum_shifts).astype(int)
    positions -= positions.min()
    total_travel = int(positions.max())

    mean_shift = np.mean(np.abs(shifts))
    std_shift = np.std(shifts)
    log.info("Mean shift: %.2f px/frame (std: %.2f)", mean_shift, std_shift)
    log.info("Total travel: %d px", total_travel)

    # Work out the output image size
    # Get a sample frame to know dimensions
    sample = next(extract_frames(video_path, roi=roi, skip=skip))
    fh, fw = sample.shape[:2]

    if axis == 1:
        out_w = total_travel + fw
        out_h = fh
    else:
        out_w = fw
        out_h = total_travel + fh

    megapixels = out_w * out_h / 1e6
    log.info("Output image: %d x %d px (%.1f MP)", out_w, out_h, megapixels)
    if megapixels > 400:
        log.warning(
            "The output image is very large (%.0f MP). If this runs out of memory, "
            "raise the frame skip or lower the scan speed used for alignment.",
            megapixels,
        )

    # Second pass: composite
    log.info("Pass 2: compositing...")
    result = np.zeros((out_h, out_w), dtype=np.float64)
    weight = np.zeros((out_h, out_w), dtype=np.float64)

    # Create a per-frame blending mask (ramp at edges for smooth blending)
    if blend:
        if axis == 1:
            ramp = np.ones(fw, dtype=np.float64)
            blend_width = min(fw // 4, 50)
            # Endpoints excluded: a weight of exactly zero would leave the
            # outermost column of the composite uncovered.
            edge = np.linspace(0, 1, blend_width + 2)[1:-1]
            ramp[:blend_width] = edge
            ramp[-blend_width:] = edge[::-1]
            mask = np.tile(ramp, (fh, 1))
        else:
            ramp = np.ones(fh, dtype=np.float64)
            blend_width = min(fh // 4, 50)
            edge = np.linspace(0, 1, blend_width + 2)[1:-1]
            ramp[:blend_width] = edge
            ramp[-blend_width:] = edge[::-1]
            mask = np.tile(ramp[:, None], (1, fw))
    else:
        mask = np.ones((fh, fw), dtype=np.float64)

    frame_gen = extract_frames(video_path, roi=roi, skip=skip)
    for i, frame in enumerate(_progress(frame_gen, n_used, "Composite")):
        # Nearest-pixel placement; sub-pixel accuracy comes from the
        # blending of many overlapping frames
        pos = int(positions[i])
        if axis == 1:
            x0 = pos
            x1 = x0 + fw
            if x1 > out_w:
                # Trim if we overshoot slightly
                trim = x1 - out_w
                result[:, x0:out_w] += (
                    frame[:, : fw - trim].astype(np.float64) * mask[:, : fw - trim]
                )
                weight[:, x0:out_w] += mask[:, : fw - trim]
            else:
                result[:, x0:x1] += frame.astype(np.float64) * mask
                weight[:, x0:x1] += mask
        else:
            y0 = pos
            y1 = y0 + fh
            if y1 > out_h:
                trim = y1 - out_h
                result[y0:out_h, :] += (
                    frame[: fh - trim, :].astype(np.float64) * mask[: fh - trim, :]
                )
                weight[y0:out_h, :] += mask[: fh - trim, :]
            else:
                result[y0:y1, :] += frame.astype(np.float64) * mask
                weight[y0:y1, :] += mask
        if i and i % report_every == 0:
            log.debug("  composite: frame %d/%d", i, n_used)

    # Normalise
    n_empty = int(np.count_nonzero(weight == 0))
    if n_empty:
        log.warning(
            "%d output pixels (%.2f%%) were never covered by a frame — there is a "
            "gap in the scan.",
            n_empty,
            100.0 * n_empty / weight.size,
        )
    weight[weight == 0] = 1.0
    result = (result / weight).astype(np.uint8)
    log.info("Composite complete")

    return result, shifts


def compress_along_axis(image, factor, axis=1):
    """Shrink ``image`` by ``factor`` along the scan axis."""
    h, w = image.shape[:2]
    if axis == 1:
        new_w = max(1, int(round(w / factor)))
        return cv2.resize(image, (new_w, h), interpolation=cv2.INTER_AREA)
    new_h = max(1, int(round(h / factor)))
    return cv2.resize(image, (w, new_h), interpolation=cv2.INTER_AREA)


def main():
    parser = argparse.ArgumentParser(
        description="Reconstruct a photonic lantern taper image from a scan video."
    )
    parser.add_argument("video", help="Path to the scan video file")
    parser.add_argument(
        "-o",
        "--output",
        default="taper_reconstructed.png",
        help="Output image path (default: taper_reconstructed.png)",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "correlation", "constant"],
        default="auto",
        help="Alignment mode (default: auto)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="Scan speed in um/s (required for 'auto' and 'constant' modes; "
        "read from the recording sidecar or file name when omitted)",
    )
    parser.add_argument(
        "--pixel_scale",
        type=float,
        default=None,
        help="Pixel scale in um/pixel (for 'constant' mode)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.3,
        help="Fractional shift deviation threshold for outlier replacement in 'auto' mode (default: 0.3)",
    )
    parser.add_argument(
        "--axis",
        type=int,
        choices=[0, 1],
        default=None,
        help="Scan axis: 1=horizontal, 0=vertical (default: the axis stored in "
        "the recording sidecar, else 1=horizontal)",
    )
    parser.add_argument(
        "--roi",
        type=int,
        nargs=4,
        metavar=("Y1", "Y2", "X1", "X2"),
        default=None,
        help="ROI crop: y1 y2 x1 x2 (to exclude UI elements)",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=1,
        help="Use every N-th frame (default: 1, i.e. all frames)",
    )
    parser.add_argument(
        "--no-blend",
        action="store_true",
        help="Disable linear blending (use simple averaging)",
    )
    parser.add_argument(
        "--compress",
        type=float,
        default=None,
        help="Compress the scan axis by this factor (e.g. 10 = 10:1 shrink along scan axis)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Frame rate to use instead of the container's (defaults to the "
        "rate measured during recording, when a sidecar is present)",
    )
    parser.add_argument(
        "--ignore-sidecar",
        action="store_true",
        help="Ignore the recording's .meta.json sidecar",
    )
    parser.add_argument(
        "--save-shifts",
        action="store_true",
        help="Save per-frame shifts to a .npy file",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Log file path (default: <output>.log)",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Do not write a per-run log file",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print debug-level detail to the console",
    )

    args = parser.parse_args()

    setup_app_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    log_path = None
    if not args.no_log:
        log_path = Path(args.log) if args.log else Path(args.output).with_suffix(".log")

    scan_speed = args.speed
    fps_override = args.fps
    axis = args.axis
    params = {}
    if not args.ignore_sidecar:
        params = sidecar.scan_parameters(args.video)

    with file_log(log_path, header="=== taper-reconstruct ===") as active_log:
        log_environment(log, {"command": " ".join(sys.argv)})

        if params:
            log.info("Recording metadata found (%s)", params.get("source"))
            if scan_speed is None and "scan_speed" in params:
                scan_speed = params["scan_speed"]
                log.info("Using scan speed %.3f um/s from the recording", scan_speed)
            if fps_override is None and "fps" in params:
                fps_override = params["fps"]
            if axis is None and "axis" in params:
                axis = params["axis"]
                log.info("Using scan axis %d from the recording", axis)

        if axis is None:
            axis = 1

        try:
            result, shifts = reconstruct_taper(
                video_path=args.video,
                mode=args.mode,
                scan_speed=scan_speed,
                pixel_scale=args.pixel_scale,
                axis=axis,
                roi=tuple(args.roi) if args.roi else None,
                skip=args.skip,
                blend=not args.no_blend,
                tolerance=args.tolerance,
                fps_override=fps_override,
            )

            if args.compress is not None and args.compress > 1:
                result = compress_along_axis(result, args.compress, axis)
                log.info(
                    "Compressed %.1fx along scan axis -> %d x %d px",
                    args.compress,
                    result.shape[1],
                    result.shape[0],
                )

            if not cv2.imwrite(args.output, result):
                raise OSError(f"Could not write output image: {args.output}")
            log.info("Saved: %s", args.output)

            if args.save_shifts:
                shifts_path = Path(args.output).with_suffix(".shifts.npy")
                np.save(shifts_path, np.array(shifts))
                log.info("Saved shifts: %s", shifts_path)

        except Exception:
            log.error("Reconstruction failed:\n%s", traceback.format_exc())
            log.error("Application log: %s", app_log_path())
            if active_log:
                log.error("Run log: %s", active_log)
            sys.exit(1)

        if active_log:
            log.info("Log written to %s", active_log)


if __name__ == "__main__":
    main()
