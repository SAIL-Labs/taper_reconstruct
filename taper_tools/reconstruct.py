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

Usage:
    python taper_reconstruct.py video.mp4 --speed 1000 --output taper.png
    python taper_reconstruct.py video.mp4 --mode constant --speed 1000 --pixel_scale 1.5

Author: Claude (for Chris @ SAIL)
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from tqdm import tqdm


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
        return float(np.median(np.abs(shifts)))

    try:
        from scipy.stats import gaussian_kde

        kde = gaussian_kde(abs_shifts)
        x = np.linspace(abs_shifts.min(), abs_shifts.max(), 500)
        peak = x[np.argmax(kde(x))]
    except ImportError:
        # Fallback: histogram peak
        counts, edges = np.histogram(abs_shifts, bins="auto")
        peak_bin = np.argmax(counts)
        peak = (edges[peak_bin] + edges[peak_bin + 1]) / 2.0

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

    Returns
    -------
    result : np.ndarray
        Reconstructed taper image.
    shifts : list of float
        Per-frame shifts in pixels.
    """
    fps, n_frames, vid_w, vid_h = get_video_info(video_path)
    n_used = n_frames // skip
    print(f"Video: {vid_w}x{vid_h}, {fps:.1f} fps, {n_frames} frames")
    print(f"Using every {skip}-th frame -> ~{n_used} frames")

    # Validate mode requirements
    if mode == "constant":
        if scan_speed is None or pixel_scale is None:
            raise ValueError(
                "scan_speed and pixel_scale are required for 'constant' mode"
            )
        px_per_sec = scan_speed / pixel_scale
        constant_shift = px_per_sec / (fps / skip)
        print(f"Constant shift: {constant_shift:.2f} px/frame")
    elif mode == "auto":
        if scan_speed is None:
            raise ValueError("scan_speed is required for 'auto' mode")

    # First pass: collect shifts
    print("Pass 1: estimating shifts...")
    shifts = []
    prev_frame = None
    frame_gen = extract_frames(video_path, roi=roi, skip=skip)

    for i, frame in enumerate(tqdm(frame_gen, total=n_used, desc="Shifts")):
        if prev_frame is not None:
            if mode == "constant":
                shifts.append(constant_shift)
            else:
                # Both 'correlation' and 'auto' use phase correlation
                s = estimate_shift_correlation(prev_frame, frame, axis=axis)
                shifts.append(s)
        prev_frame = frame

    # Auto mode: find dominant shift, derive pixel scale, replace outliers
    if mode == "auto":
        raw_shifts = np.array(shifts)
        # Determine scan sign from cumulative sum
        sign = np.sign(np.sum(raw_shifts)) or 1.0
        peak_shift = find_peak_shift(raw_shifts) * sign
        abs_peak = abs(peak_shift)

        # Derive pixel scale: speed [um/s] / (shift [px/frame] * fps/skip [frame/s])
        derived_pixel_scale = scan_speed / (abs_peak * fps / skip)
        print(f"\n--- Auto mode diagnostics ---")
        print(f"Peak shift: {peak_shift:.3f} px/frame")
        print(f"Derived pixel scale: {derived_pixel_scale:.4f} um/px")

        # Replace outliers
        n_replaced = 0
        for j in range(len(shifts)):
            deviation = abs(shifts[j] - peak_shift) / abs_peak
            if deviation > tolerance:
                shifts[j] = peak_shift
                n_replaced += 1

        frac = n_replaced / len(shifts) * 100
        print(f"Replaced {n_replaced}/{len(shifts)} shifts ({frac:.1f}%)")
        print(f"Shift range (after): [{min(shifts):.3f}, {max(shifts):.3f}]")
        print(f"-----------------------------\n")

    # Compute cumulative positions
    cum_shifts = np.cumsum(shifts)
    cum_shifts = np.insert(cum_shifts, 0, 0.0)  # first frame at position 0

    # Handle negative shifts (scan could go either direction)
    if cum_shifts[-1] < 0:
        print("Detected reverse scan direction, flipping.")
        cum_shifts = -cum_shifts

    # Print some diagnostics
    total_travel = cum_shifts[-1]
    mean_shift = np.mean(np.abs(shifts))
    std_shift = np.std(shifts)
    print(f"Mean shift: {mean_shift:.2f} px/frame (std: {std_shift:.2f})")
    print(f"Total travel: {total_travel:.0f} px")

    # Work out the output image size
    # Get a sample frame to know dimensions
    sample = next(extract_frames(video_path, roi=roi, skip=skip))
    fh, fw = sample.shape[:2]

    if axis == 1:
        out_w = int(np.ceil(total_travel)) + fw
        out_h = fh
    else:
        out_w = fw
        out_h = int(np.ceil(total_travel)) + fh

    print(f"Output image: {out_w} x {out_h} px")

    # Second pass: composite
    print("Pass 2: compositing...")
    result = np.zeros((out_h, out_w), dtype=np.float64)
    weight = np.zeros((out_h, out_w), dtype=np.float64)

    # Create a per-frame blending mask (ramp at edges for smooth blending)
    if blend:
        if axis == 1:
            ramp = np.ones(fw, dtype=np.float64)
            blend_width = min(fw // 4, 50)
            ramp[:blend_width] = np.linspace(0, 1, blend_width)
            ramp[-blend_width:] = np.linspace(1, 0, blend_width)
            mask = np.tile(ramp, (fh, 1))
        else:
            ramp = np.ones(fh, dtype=np.float64)
            blend_width = min(fh // 4, 50)
            ramp[:blend_width] = np.linspace(0, 1, blend_width)
            ramp[-blend_width:] = np.linspace(1, 0, blend_width)
            mask = np.tile(ramp[:, None], (1, fw))
    else:
        mask = np.ones((fh, fw), dtype=np.float64)

    frame_gen = extract_frames(video_path, roi=roi, skip=skip)
    for i, frame in enumerate(tqdm(frame_gen, total=n_used, desc="Composite")):
        pos = cum_shifts[i]
        # Integer position (we do nearest-pixel placement; sub-pixel
        # accuracy comes from the blending of many overlapping frames)
        if axis == 1:
            x0 = int(round(pos))
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
            y0 = int(round(pos))
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

    # Normalise
    weight[weight == 0] = 1.0
    result = (result / weight).astype(np.uint8)

    return result, shifts


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
        help="Scan speed in um/s (required for 'auto' and 'constant' modes)",
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
        default=1,
        help="Scan axis: 1=horizontal, 0=vertical (default: 1)",
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
        "--save-shifts",
        action="store_true",
        help="Save per-frame shifts to a .npy file",
    )

    args = parser.parse_args()

    result, shifts = reconstruct_taper(
        video_path=args.video,
        mode=args.mode,
        scan_speed=args.speed,
        pixel_scale=args.pixel_scale,
        axis=args.axis,
        roi=tuple(args.roi) if args.roi else None,
        skip=args.skip,
        blend=not args.no_blend,
        tolerance=args.tolerance,
    )

    if args.compress is not None and args.compress > 1:
        h, w = result.shape[:2]
        if args.axis == 1:
            new_w = max(1, int(round(w / args.compress)))
            result = cv2.resize(result, (new_w, h), interpolation=cv2.INTER_AREA)
        else:
            new_h = max(1, int(round(h / args.compress)))
            result = cv2.resize(result, (w, new_h), interpolation=cv2.INTER_AREA)
        print(f"Compressed {args.compress:.1f}x along scan axis -> {result.shape[1]} x {result.shape[0]} px")

    cv2.imwrite(args.output, result)
    print(f"Saved: {args.output}")

    if args.save_shifts:
        shifts_path = Path(args.output).with_suffix(".shifts.npy")
        np.save(shifts_path, np.array(shifts))
        print(f"Saved shifts: {shifts_path}")


if __name__ == "__main__":
    main()
