"""
Taper Scan Recorder
====================

A system tray application for recording screen regions during photonic
lantern taper diameter scans, with integrated taper image reconstruction.

Features:
  - System tray icon with quick-access menu
  - Interactive screen region selection (rubber band)
  - Records a fixed screen region to .mp4
  - Prompts for the scan speed and writes it into the file name
  - Live on-screen overlay outlining the captured region, with a preview
    of what is actually being captured (drawn outside the region, so it
    never lands in the recording)
  - Configurable frame rate and scan parameters
  - One-click taper reconstruction from recorded video
  - Writes a log and a metadata sidecar for every recording and every
    reconstruction, so a failed run can be diagnosed afterwards
  - Remembers last region and settings between sessions

Dependencies:
    pip install PyQt6 opencv-python-headless numpy mss tqdm

Usage:
    python taper_recorder.py

Author: Claude (for Chris @ SAIL)
"""

import sys
import os
import json
import logging
import time
import threading
import traceback
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np
import mss
from PyQt6.QtCore import (
    Qt,
    QTimer,
    QRect,
    QPoint,
    QSize,
    pyqtSignal,
    QObject,
    QSettings,
)
from PyQt6.QtGui import (
    QIcon,
    QAction,
    QPainter,
    QColor,
    QPen,
    QFont,
    QFontMetrics,
    QImage,
    QPixmap,
    QCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QPushButton,
    QFileDialog,
    QGroupBox,
    QCheckBox,
    QMessageBox,
    QProgressDialog,
    QTextEdit,
)

from taper_tools import sidecar
from taper_tools.logging_utils import (
    app_log_path,
    file_log,
    get_log_dir,
    log_environment,
    setup_app_logging,
)

# Explicit name (not __name__): PyInstaller runs this module as __main__,
# and records would then bypass the package handlers.
log = logging.getLogger("taper_tools.recorder")


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "TaperScans"),
    "fps": 15,
    "scan_speed": 1000.0,
    "scan_label": "",
    "skip": 1,
    "tolerance": 0.3,
    "axis": 1,
    "compress": 10.0,
    "auto_reconstruct": False,
    "show_overlay": True,
    "overlay_preview": True,
    "region": None,  # [x, y, w, h]
}

SETTINGS_PATH = Path.home() / ".taper_recorder.json"


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH) as f:
                saved = json.load(f)
            merged = {**DEFAULT_SETTINGS, **saved}
            return merged
        except Exception as exc:
            log.warning("Could not read %s (%s); using defaults", SETTINGS_PATH, exc)
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as exc:
        log.error("Could not save settings to %s: %s", SETTINGS_PATH, exc)


# ---------------------------------------------------------------------------
# Region selection overlay
# ---------------------------------------------------------------------------
class RegionSelector(QWidget):
    """Full-screen transparent overlay for rubber-band region selection."""

    region_selected = pyqtSignal(QRect)
    closed = pyqtSignal()

    def __init__(self, initial_rect=None):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

        # Cover all screens — use setGeometry + show() (not showFullScreen,
        # which overrides geometry to a single monitor on Windows)
        screens = QApplication.screens()
        combined = QRect()
        for s in screens:
            combined = combined.united(s.geometry())
        self.setGeometry(combined)

        self.origin = None
        self.current = None
        self.selection = initial_rect
        self.is_drawing = False

    def paintEvent(self, event):
        painter = QPainter(self)
        # Semi-transparent dark overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        rect = self._get_rect()
        if rect and rect.isValid():
            # Clear the selected region (make it transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )

            # Draw border
            pen = QPen(QColor(0, 180, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect)

            # Draw dimensions label
            label = f"{rect.width()} x {rect.height()}"
            painter.setPen(QColor(255, 255, 255))
            font = QFont("monospace", 12)
            painter.setFont(font)
            painter.drawText(rect.x(), rect.y() - 8, label)

    def _get_rect(self):
        if self.selection:
            return self.selection
        if self.origin and self.current:
            return QRect(self.origin, self.current).normalized()
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.pos()
            self.current = event.pos()
            self.selection = None
            self.is_drawing = True
            self.update()

    def mouseMoveEvent(self, event):
        if self.is_drawing:
            self.current = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_drawing:
            self.is_drawing = False
            rect = QRect(self.origin, event.pos()).normalized()
            if rect.width() > 10 and rect.height() > 10:
                self.selection = rect
                self.region_selected.emit(rect)
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Live capture overlay
#
# Everything the overlay draws sits *outside* the captured region, so the
# recording never contains the overlay itself.
# ---------------------------------------------------------------------------
OVERLAY_GAP = 3  # clear pixels between the region edge and the outline
OVERLAY_BORDER = 3  # outline thickness
OVERLAY_MARGIN = OVERLAY_GAP + OVERLAY_BORDER
PANEL_GAP = 8  # gap between the outline and the status panel

RECORDING_COLOUR = QColor(235, 60, 60)
PREVIEW_COLOUR = QColor(0, 180, 255)

THUMB_MAX_W = 260
THUMB_MAX_H = 200


class ClickThroughWindow(QWidget):
    """Frameless, always-on-top window that ignores mouse input."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


class RegionOutline(ClickThroughWindow):
    """Draws a ring just outside the captured region."""

    def __init__(self):
        super().__init__()
        self._colour = PREVIEW_COLOUR

    def set_colour(self, colour):
        if colour != self._colour:
            self._colour = colour
            self.update()

    def set_region(self, region):
        self.setGeometry(
            region.adjusted(
                -OVERLAY_MARGIN, -OVERLAY_MARGIN, OVERLAY_MARGIN, OVERLAY_MARGIN
            )
        )
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(self._colour, OVERLAY_BORDER)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Stroke centred so it occupies [GAP, GAP + BORDER] from the window
        # edge — i.e. it stops short of the captured region.
        inset = OVERLAY_MARGIN - OVERLAY_BORDER / 2.0
        painter.drawRect(
            int(inset),
            int(inset),
            int(self.width() - 2 * inset),
            int(self.height() - 2 * inset),
        )


class StatusPanel(ClickThroughWindow):
    """Status readout and live thumbnail, placed clear of the region."""

    PAD = 10

    def __init__(self):
        super().__init__()
        self._colour = PREVIEW_COLOUR
        self._title = ""
        self._lines = []
        self._thumb = QPixmap()
        self._title_font = QFont("Arial", 11, QFont.Weight.Bold)
        self._line_font = QFont("Arial", 9)

    def set_colour(self, colour):
        self._colour = colour

    def set_content(self, title, lines):
        self._title = title
        self._lines = list(lines)

    def set_thumbnail(self, pixmap):
        self._thumb = pixmap if pixmap is not None else QPixmap()

    def content_size(self):
        fm_title = QFontMetrics(self._title_font)
        fm_line = QFontMetrics(self._line_font)

        width = fm_title.horizontalAdvance(self._title) + 20  # room for the dot
        height = fm_title.height()
        for line in self._lines:
            width = max(width, fm_line.horizontalAdvance(line))
            height += fm_line.height() + 2
        if not self._thumb.isNull():
            width = max(width, self._thumb.width())
            height += self._thumb.height() + 8
        return QSize(width + 2 * self.PAD, height + 2 * self.PAD)

    def place(self, region):
        """Position the panel outside ``region``.  False if it does not fit."""
        size = self.content_size()
        self.resize(size)

        screen = QApplication.screenAt(region.center()) or QApplication.primaryScreen()
        available = screen.geometry()
        outline = region.adjusted(
            -OVERLAY_MARGIN, -OVERLAY_MARGIN, OVERLAY_MARGIN, OVERLAY_MARGIN
        )
        offset = OVERLAY_MARGIN + PANEL_GAP

        candidates = [
            QPoint(region.left(), region.bottom() + offset),
            QPoint(region.left(), region.top() - offset - size.height()),
            QPoint(region.right() + offset, region.top()),
            QPoint(region.left() - offset - size.width(), region.top()),
            QPoint(available.left() + PANEL_GAP, available.top() + PANEL_GAP),
            QPoint(
                available.right() - PANEL_GAP - size.width(),
                available.top() + PANEL_GAP,
            ),
            QPoint(
                available.left() + PANEL_GAP,
                available.bottom() - PANEL_GAP - size.height(),
            ),
            QPoint(
                available.right() - PANEL_GAP - size.width(),
                available.bottom() - PANEL_GAP - size.height(),
            ),
        ]
        for point in candidates:
            rect = QRect(point, size)
            if available.contains(rect) and not rect.intersects(outline):
                self.move(point)
                return True
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        body = self.rect().adjusted(0, 0, -1, -1)
        painter.setBrush(QColor(0, 0, 0, 200))
        painter.setPen(QPen(self._colour, 1))
        painter.drawRoundedRect(body, 6, 6)

        x = self.PAD
        y = self.PAD

        # Title line, prefixed with a status dot
        fm_title = QFontMetrics(self._title_font)
        dot = 9
        painter.setBrush(self._colour)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(
            x, y + (fm_title.height() - dot) // 2, dot, dot
        )
        painter.setFont(self._title_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            x + dot + 7, y + fm_title.ascent(), self._title
        )
        y += fm_title.height()

        # Detail lines
        painter.setFont(self._line_font)
        fm_line = QFontMetrics(self._line_font)
        painter.setPen(QColor(215, 215, 215))
        for line in self._lines:
            y += fm_line.height() + 2
            painter.drawText(x, y - fm_line.descent(), line)

        # Live thumbnail
        if not self._thumb.isNull():
            y += 8
            painter.drawPixmap(x, y, self._thumb)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(120, 120, 120), 1))
            painter.drawRect(
                x, y, self._thumb.width() - 1, self._thumb.height() - 1
            )


class CaptureOverlay(QObject):
    """Outline + status panel shown over the captured region."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outline = RegionOutline()
        self._panel = StatusPanel()
        self._region = None
        self._show_panel = True
        self._panel_fits = True

    @staticmethod
    def _to_pixmap(frame):
        """BGR ndarray -> QPixmap thumbnail (None if there is no frame)."""
        if frame is None or getattr(frame, "size", 0) == 0:
            return None
        h, w = frame.shape[:2]
        scale = min(1.0, THUMB_MAX_W / float(w), THUMB_MAX_H / float(h))
        if scale < 1.0:
            frame = cv2.resize(
                frame,
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        frame = np.ascontiguousarray(frame)
        h, w = frame.shape[:2]
        image = QImage(
            frame.data, w, h, frame.strides[0], QImage.Format.Format_BGR888
        ).copy()
        return QPixmap.fromImage(image)

    def show(self, region, recording=False, show_panel=True):
        self._region = QRect(region)
        self._show_panel = show_panel
        colour = RECORDING_COLOUR if recording else PREVIEW_COLOUR

        self._outline.set_colour(colour)
        self._outline.set_region(self._region)
        self._outline.show()
        self._outline.raise_()

        self._panel.set_colour(colour)
        if show_panel:
            self._panel_fits = self._panel.place(self._region)
            if self._panel_fits:
                self._panel.show()
                self._panel.raise_()
            else:
                log.warning(
                    "No room on screen for the overlay status panel outside the "
                    "captured region; showing the outline only."
                )
                self._panel.hide()
        else:
            self._panel.hide()

    def update_status(self, title, lines, frame=None):
        if not self._show_panel or self._region is None:
            return
        self._panel.set_content(title, lines)
        self._panel.set_thumbnail(self._to_pixmap(frame))
        self._panel_fits = self._panel.place(self._region)
        if self._panel_fits:
            if not self._panel.isVisible():
                self._panel.show()
            self._panel.update()
            self._panel.raise_()
        else:
            self._panel.hide()

    def set_recording(self, recording):
        colour = RECORDING_COLOUR if recording else PREVIEW_COLOUR
        self._outline.set_colour(colour)
        self._panel.set_colour(colour)
        self._panel.update()

    def hide(self):
        self._outline.hide()
        self._panel.hide()

    @property
    def visible(self):
        return self._outline.isVisible()


# ---------------------------------------------------------------------------
# Screen recorder (runs in a thread)
# ---------------------------------------------------------------------------
class RecorderSignals(QObject):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    frame_count = pyqtSignal(int)


class ScreenRecorder:
    """Records a screen region to an MP4 file using mss + opencv."""

    PREVIEW_INTERVAL = 0.2  # seconds between preview frame updates
    PROGRESS_INTERVAL = 10.0  # seconds between log progress lines

    def __init__(
        self,
        region,
        output_path,
        fps=15,
        metadata=None,
        log_path=None,
        capture_preview=True,
    ):
        """
        Parameters
        ----------
        region : dict
            {"left": x, "top": y, "width": w, "height": h} in screen coords.
        output_path : str
            Path for the output .mp4 file.
        fps : int
            Target frame rate.
        metadata : dict, optional
            Extra values (scan speed, label, settings, ...) written to the
            ``.meta.json`` sidecar alongside the video.
        log_path : str or Path, optional
            Per-recording log file. Defaults to ``<video>.log``.
        capture_preview : bool
            Keep the most recent frame available for the overlay preview.
        """
        self.region = region
        self.output_path = output_path
        self.fps = fps
        self.metadata = dict(metadata or {})
        self.log_path = (
            Path(log_path)
            if log_path is not None
            else sidecar.recording_log_path(output_path)
        )
        self.capture_preview = capture_preview
        self.running = False
        self.signals = RecorderSignals()
        self._thread = None
        self._frame_count = 0
        self._preview_frame = None
        self._preview_lock = threading.Lock()
        self._stats = {}
        self._start_perf = None

    def start(self):
        self.running = True
        self._frame_count = 0
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                log.error("Capture thread did not stop within 10 s")

    @property
    def frame_count(self):
        return self._frame_count

    @property
    def stats(self):
        return dict(self._stats)

    @property
    def elapsed(self):
        if self._start_perf is None:
            return 0.0
        return time.perf_counter() - self._start_perf

    def latest_preview(self):
        """Most recent captured frame (BGR), or None."""
        with self._preview_lock:
            return self._preview_frame

    def _record_loop(self):
        with file_log(
            self.log_path, thread_only=True, header="=== taper recorder capture ==="
        ):
            log_environment(
                log,
                {
                    "video": self.output_path,
                    "region": self.region,
                    "target fps": self.fps,
                    **{
                        f"meta:{k}": v
                        for k, v in self.metadata.items()
                        if not isinstance(v, (dict, list))
                    },
                },
            )
            try:
                self._capture()
            except Exception as exc:
                log.error("Recording failed:\n%s", traceback.format_exc())
                self.signals.error.emit(str(exc))

    def _capture(self):
        writer = None
        frame_times = []
        grab_times = []
        n_slow = 0
        started_at = datetime.now()
        frame_interval = 1.0 / self.fps
        self._start_perf = time.perf_counter()
        next_progress = self.PROGRESS_INTERVAL
        last_preview = -1.0

        try:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")

            with mss.mss() as sct:
                # Probe frame, for the actual dimensions (these may differ
                # from the region dict due to DPI or edge clipping).  It is
                # not written: opening the writer takes long enough that
                # including it would skew the first frame interval, and the
                # measured frame rate is what the reconstruction relies on.
                img = sct.grab(self.region)
                frame = np.array(img)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                h, w = frame.shape[:2]
                log.info("Capture frame size: %dx%d", w, h)

                writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
                if not writer.isOpened():
                    message = (
                        f"Failed to open video writer ({w}x{h}, mp4v).\n"
                        "Check codec support."
                    )
                    log.error(message)
                    self.signals.error.emit(message)
                    return

                # Every written frame goes through the same grab/write/sleep
                # cycle, so the timestamps measure the real capture cadence.
                self._start_perf = time.perf_counter()

                while self.running:
                    t0 = time.perf_counter()
                    img = sct.grab(self.region)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    writer.write(frame)
                    self._frame_count += 1

                    stamp = t0 - self._start_perf
                    frame_times.append(stamp)
                    grab = time.perf_counter() - t0
                    grab_times.append(grab)
                    if grab > frame_interval:
                        n_slow += 1

                    if (
                        self.capture_preview
                        and stamp - last_preview >= self.PREVIEW_INTERVAL
                    ):
                        self._store_preview(frame)
                        last_preview = stamp

                    if stamp >= next_progress:
                        log.info(
                            "  %.0f s: %d frames (%.2f fps average)",
                            stamp,
                            self._frame_count,
                            self._frame_count / stamp if stamp else 0.0,
                        )
                        next_progress += self.PROGRESS_INTERVAL

                    sleep_time = frame_interval - (time.perf_counter() - t0)
                    if sleep_time > 0:
                        time.sleep(sleep_time)

            # Close the file before announcing it: whoever handles
            # `finished` (auto-reconstruct, for one) opens it straight away.
            writer.release()
            writer = None

            self._finalise(started_at, frame_times, grab_times, n_slow, (w, h))
            self.signals.finished.emit(self.output_path)
        finally:
            if writer is not None:
                writer.release()

    def _store_preview(self, frame):
        if not self.capture_preview:
            return
        with self._preview_lock:
            self._preview_frame = frame

    def _finalise(self, started_at, frame_times, grab_times, n_slow, frame_size):
        """Log capture statistics and write the metadata sidecar."""
        finished_at = datetime.now()
        n = len(frame_times)
        duration = frame_times[-1] - frame_times[0] if n > 1 else 0.0
        effective_fps = (n - 1) / duration if duration > 0 else float(self.fps)
        if n < 2:
            log.warning(
                "Only %d frame(s) were captured — the recording is too short to "
                "reconstruct.",
                n,
            )

        stats = {
            "frame_count": n,
            "duration_s": round(duration, 3),
            "effective_fps": round(effective_fps, 4),
            "target_fps": self.fps,
            "slow_frames": n_slow,
            "mean_grab_ms": round(float(np.mean(grab_times) * 1000), 2)
            if grab_times
            else None,
            "max_grab_ms": round(float(np.max(grab_times) * 1000), 2)
            if grab_times
            else None,
        }
        self._stats = stats

        try:
            size_mb = Path(self.output_path).stat().st_size / 1e6
        except OSError:
            size_mb = None

        log.info("--- capture summary ---")
        for key, value in stats.items():
            log.info("  %-14s %s", key, value)
        if size_mb is not None:
            log.info("  %-14s %.1f MB", "video_size", size_mb)
        log.info("-----------------------")

        if n_slow and n:
            fraction = 100.0 * n_slow / n
            if fraction > 10:
                log.warning(
                    "%.1f%% of frames took longer than the frame interval to "
                    "capture; the recording ran at %.2f fps instead of %d. "
                    "Reconstruction will use the measured rate.",
                    fraction,
                    effective_fps,
                    self.fps,
                )

        metadata = {
            "tool": "taper-recorder",
            "video": Path(self.output_path).name,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "capture_region": self.region,
            "frame_size": list(frame_size),
            "log": Path(self.log_path).name if self.log_path else None,
            **self.metadata,
            **stats,
        }
        # Frame timestamps let a suspicious reconstruction be checked against
        # the real capture cadence.  Thin them out for very long scans.
        step = max(1, n // 20000)
        metadata["frame_times_s"] = [round(t, 4) for t in frame_times[::step]]
        metadata["frame_times_step"] = step
        sidecar.write_metadata(self.output_path, metadata)


# ---------------------------------------------------------------------------
# Recording setup dialog
# ---------------------------------------------------------------------------
class RecordingSetupDialog(QDialog):
    """Asks for the scan speed (and where to save) before recording."""

    def __init__(self, settings, region, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Start Taper Scan Recording")
        # A tray app has no main window to parent to, so keep the prompt on
        # top rather than let it open behind the scan software.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.settings = dict(settings)
        self.region = region
        self._timestamp = None  # fixed when the user presses Start
        self._build_ui()
        self.setMinimumWidth(460)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        scan_group = QGroupBox("Scan")
        scan_form = QFormLayout(scan_group)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 100000.0)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSuffix(" µm/s")
        self.speed_spin.setValue(float(self.settings.get("scan_speed", 1000.0)))
        self.speed_spin.setToolTip(
            "Stage speed for this scan. It goes in the file name and the "
            "metadata sidecar, and sets the pixel scale during reconstruction."
        )
        self.speed_spin.valueChanged.connect(self._update_preview)
        scan_form.addRow("Scan speed:", self.speed_spin)

        self.label_edit = QLineEdit(self.settings.get("scan_label", ""))
        self.label_edit.setPlaceholderText("optional — e.g. lantern7_run2")
        self.label_edit.textChanged.connect(self._update_preview)
        scan_form.addRow("Sample label:", self.label_edit)

        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["Horizontal (left-right)", "Vertical (top-bottom)"])
        self.axis_combo.setCurrentIndex(0 if self.settings.get("axis", 1) == 1 else 1)
        self.axis_combo.setToolTip(
            "Direction the taper moves across the capture region. Stored with "
            "the recording and used to align the frames."
        )
        scan_form.addRow("Scan axis:", self.axis_combo)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(int(self.settings.get("fps", 15)))
        scan_form.addRow("Frame rate:", self.fps_spin)

        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText(
            "optional — objective, fibre ID, anything worth keeping with the scan"
        )
        scan_form.addRow("Notes:", self.notes_edit)

        layout.addWidget(scan_group)

        out_group = QGroupBox("Output")
        out_form = QFormLayout(out_group)

        self.dir_edit = QLineEdit(self.settings.get("output_dir", ""))
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse_btn)
        out_form.addRow("Folder:", dir_row)

        self.name_label = QLabel()
        self.name_label.setWordWrap(True)
        self.name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        out_form.addRow("File name:", self.name_label)

        layout.addWidget(out_group)

        overlay_group = QGroupBox("On-screen overlay")
        overlay_form = QVBoxLayout(overlay_group)
        self.overlay_cb = QCheckBox("Outline the captured region while recording")
        self.overlay_cb.setChecked(bool(self.settings.get("show_overlay", True)))
        overlay_form.addWidget(self.overlay_cb)
        self.overlay_preview_cb = QCheckBox(
            "Show a live preview of the captured frames"
        )
        self.overlay_preview_cb.setChecked(
            bool(self.settings.get("overlay_preview", True))
        )
        overlay_form.addWidget(self.overlay_preview_cb)
        note = QLabel(
            "The overlay is drawn outside the captured region, so it does not "
            "appear in the recording."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        overlay_form.addWidget(note)
        layout.addWidget(overlay_group)

        if self.region:
            region_label = QLabel(
                f"Region: {self.region[2]} × {self.region[3]} px at "
                f"({self.region[0]}, {self.region[1]})"
            )
            region_label.setStyleSheet("color: gray;")
            layout.addWidget(region_label)

        btn_row = QHBoxLayout()
        start_btn = QPushButton("Start Recording")
        start_btn.setDefault(True)
        start_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(start_btn)
        layout.addLayout(btn_row)

        self._update_preview()

    def _browse_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self, "Save recording to...", self.dir_edit.text()
        )
        if directory:
            self.dir_edit.setText(directory)

    def _update_preview(self):
        self.name_label.setText(self.filename())

    def filename(self):
        """File name for the recording.

        While the dialog is open this previews the name as it would be
        right now; after Start is pressed the timestamp is fixed, so the
        name written to disk is the one the recording actually began with.
        """
        return sidecar.build_video_filename(
            self.speed_spin.value(), self.label_edit.text(), self._timestamp
        )

    def accept(self):
        directory = self.dir_edit.text().strip()
        if not directory:
            QMessageBox.warning(self, "Output folder", "Choose an output folder.")
            return
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Output folder", f"Cannot use {directory}:\n{exc}"
            )
            return
        self._timestamp = datetime.now()
        super().accept()

    def values(self):
        return {
            "output_dir": self.dir_edit.text().strip(),
            "scan_speed": self.speed_spin.value(),
            "scan_label": self.label_edit.text().strip(),
            "axis": 1 if self.axis_combo.currentIndex() == 0 else 0,
            "fps": self.fps_spin.value(),
            "notes": self.notes_edit.text().strip(),
            "show_overlay": self.overlay_cb.isChecked(),
            "overlay_preview": self.overlay_preview_cb.isChecked(),
        }


# ---------------------------------------------------------------------------
# Reconstruction confirmation dialog
# ---------------------------------------------------------------------------
class ReconstructionDialog(QDialog):
    """Shows the scan facts a reconstruction will use, and where each came from.

    Prefilled from the recording's sidecar or file name; anything that had
    to fall back to a Settings default is flagged so a video recorded
    elsewhere is never reconstructed at a guessed speed without the user
    seeing it.
    """

    def __init__(self, video_path, defaults, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reconstruct Taper Scan")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.video_path = Path(video_path)
        self.defaults = dict(defaults)
        self.settings = dict(settings)
        self._build_ui()
        self.setMinimumWidth(500)

    @staticmethod
    def _source_label(text, warn=False):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            "color: #c0392b; font-weight: bold;" if warn else "color: gray;"
        )
        return label

    def _build_ui(self):
        d = self.defaults
        layout = QVBoxLayout(self)

        file_label = QLabel(f"<b>{self.video_path.name}</b>")
        file_label.setWordWrap(True)
        file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(file_label)
        if d.get("label"):
            layout.addWidget(self._source_label(f"Sample label: {d['label']}"))

        form = QFormLayout()

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 100000.0)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSuffix(" µm/s")
        self.speed_spin.setValue(float(d["scan_speed"]))
        form.addRow("Scan speed:", self.speed_spin)
        form.addRow("", self._source_label(d["speed_source"], d["speed_warn"]))

        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["Horizontal (left-right)", "Vertical (top-bottom)"])
        self.axis_combo.setCurrentIndex(0 if d["axis"] == 1 else 1)
        form.addRow("Scan axis:", self.axis_combo)
        form.addRow("", self._source_label(d["axis_source"], d["axis_warn"]))

        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0.1, 1000.0)
        self.fps_spin.setDecimals(3)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setValue(float(d["fps"]))
        form.addRow("Frame rate:", self.fps_spin)
        form.addRow("", self._source_label(d["fps_source"], d["fps_warn"]))

        layout.addLayout(form)

        s = self.settings
        skip = s.get("skip", 1)
        compress = s.get("compress", 1.0)
        processing = QLabel(
            f"Processing: {'every frame' if skip == 1 else f'every {skip}th frame'}"
            f" · outlier tolerance {s.get('tolerance', 0.3):.2f}"
            f" · {'no compression' if not compress or compress <= 1 else f'{compress:g}× compression'}"
            "  (change in Settings)"
        )
        processing.setWordWrap(True)
        processing.setStyleSheet("color: gray;")
        layout.addWidget(processing)

        btn_row = QHBoxLayout()
        go_btn = QPushButton("Reconstruct")
        go_btn.setDefault(True)
        go_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(go_btn)
        layout.addLayout(btn_row)

    def values(self):
        return {
            "scan_speed": self.speed_spin.value(),
            "axis": 1 if self.axis_combo.currentIndex() == 0 else 0,
            "fps": self.fps_spin.value(),
        }


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Taper Recorder - Settings")
        self.settings = dict(settings)
        self._build_ui()
        self.setMinimumWidth(400)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Recording settings
        rec_group = QGroupBox("Recording")
        rec_form = QFormLayout(rec_group)

        self.dir_edit = QLineEdit(self.settings["output_dir"])
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse_btn)
        rec_form.addRow("Output directory:", dir_row)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(self.settings["fps"])
        rec_form.addRow("Frame rate (fps):", self.fps_spin)

        self.overlay_cb = QCheckBox("Outline the captured region while recording")
        self.overlay_cb.setChecked(self.settings.get("show_overlay", True))
        rec_form.addRow(self.overlay_cb)

        self.overlay_preview_cb = QCheckBox("Include a live capture preview")
        self.overlay_preview_cb.setChecked(self.settings.get("overlay_preview", True))
        rec_form.addRow(self.overlay_preview_cb)

        layout.addWidget(rec_group)

        # Reconstruction settings
        recon_group = QGroupBox("Reconstruction")
        recon_form = QFormLayout(recon_group)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(1, 100000)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSuffix(" µm/s")
        self.speed_spin.setValue(self.settings["scan_speed"])
        self.speed_spin.setToolTip(
            "Default scan speed. Each recording asks for its own speed, which "
            "is stored with the video and used when reconstructing it."
        )
        recon_form.addRow("Scan speed:", self.speed_spin)

        self.skip_spin = QSpinBox()
        self.skip_spin.setRange(1, 100)
        self.skip_spin.setValue(self.settings["skip"])
        recon_form.addRow("Frame skip:", self.skip_spin)

        self.tol_spin = QDoubleSpinBox()
        self.tol_spin.setRange(0.01, 1.0)
        self.tol_spin.setDecimals(2)
        self.tol_spin.setSingleStep(0.05)
        self.tol_spin.setValue(self.settings["tolerance"])
        recon_form.addRow("Outlier tolerance:", self.tol_spin)

        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["Horizontal (left-right)", "Vertical (top-bottom)"])
        self.axis_combo.setCurrentIndex(0 if self.settings["axis"] == 1 else 1)
        self.axis_combo.setToolTip(
            "Default scan axis. Each recording asks for its own, which is "
            "stored with the video."
        )
        recon_form.addRow("Scan axis:", self.axis_combo)

        self.compress_spin = QDoubleSpinBox()
        self.compress_spin.setRange(1, 100)
        self.compress_spin.setDecimals(1)
        self.compress_spin.setValue(self.settings.get("compress", 10.0))
        self.compress_spin.setSpecialValueText("None")
        recon_form.addRow("Compress factor:", self.compress_spin)

        self.auto_recon_cb = QCheckBox("Automatically reconstruct after recording")
        self.auto_recon_cb.setChecked(self.settings["auto_reconstruct"])
        recon_form.addRow(self.auto_recon_cb)

        layout.addWidget(recon_group)

        # Buttons
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory")
        if d:
            self.dir_edit.setText(d)

    def get_settings(self):
        return {
            **self.settings,
            "output_dir": self.dir_edit.text(),
            "fps": self.fps_spin.value(),
            "scan_speed": self.speed_spin.value(),
            "skip": self.skip_spin.value(),
            "tolerance": self.tol_spin.value(),
            "axis": 1 if self.axis_combo.currentIndex() == 0 else 0,
            "compress": self.compress_spin.value(),
            "auto_reconstruct": self.auto_recon_cb.isChecked(),
            "show_overlay": self.overlay_cb.isChecked(),
            "overlay_preview": self.overlay_preview_cb.isChecked(),
        }


# ---------------------------------------------------------------------------
# Main tray application
# ---------------------------------------------------------------------------
class TaperRecorderApp(QObject):
    _recon_message = pyqtSignal(str, str, QSystemTrayIcon.MessageIcon, int)
    _recon_failed = pyqtSignal(str, str, str)

    def __init__(self):
        super().__init__()
        self._recon_message.connect(self._show_tray_message)
        self._recon_failed.connect(self._show_reconstruction_failure)
        self.settings = load_settings()
        self.recorder = None
        self.recording = False
        self.current_video_path = None
        self.current_scan_speed = None
        self.region_selector = None
        self.last_log_path = app_log_path()
        self._record_start_time = None
        self._sct = None

        self.overlay = CaptureOverlay(self)

        self._setup_tray()
        self._update_status_timer = QTimer(self)
        self._update_status_timer.timeout.connect(self._update_recording_status)
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._update_idle_overlay)

        log.info("Recorder ready (settings: %s)", SETTINGS_PATH)

    def _create_icon(self, recording=False):
        """Generate a simple coloured icon (no external file needed)."""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Circle
        colour = QColor(220, 50, 50) if recording else QColor(80, 150, 220)
        painter.setBrush(colour)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, size - 8, size - 8)

        # "T" for taper
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Arial", 28, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "T")

        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self._create_icon(), parent=self)
        self.tray.setToolTip("Taper Scan Recorder")
        self.tray.activated.connect(self._on_tray_activated)

        menu = QMenu()

        self.select_action = QAction("Select Region...", self)
        self.select_action.triggered.connect(self.select_region)
        menu.addAction(self.select_action)

        self.region_label = QAction("No region selected", self)
        self.region_label.setEnabled(False)
        menu.addAction(self.region_label)
        self._update_region_label()

        self.overlay_action = QAction("Show Region Overlay", self)
        self.overlay_action.setCheckable(True)
        self.overlay_action.setToolTip(
            "Outline the captured region on screen so you can confirm the "
            "scan is in the right spot before recording."
        )
        self.overlay_action.toggled.connect(self._toggle_idle_overlay)
        menu.addAction(self.overlay_action)

        menu.addSeparator()

        self.record_action = QAction("Start Recording...", self)
        self.record_action.triggered.connect(self.toggle_recording)
        menu.addAction(self.record_action)

        self.status_action = QAction("", self)
        self.status_action.setEnabled(False)
        self.status_action.setVisible(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        self.recon_action = QAction("Reconstruct Last Recording...", self)
        self.recon_action.triggered.connect(self.reconstruct_last)
        self.recon_action.setEnabled(False)
        menu.addAction(self.recon_action)

        recon_file_action = QAction("Reconstruct from File...", self)
        recon_file_action.triggered.connect(self.reconstruct_from_file)
        menu.addAction(recon_file_action)

        menu.addSeparator()

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.show_settings)
        menu.addAction(settings_action)

        open_dir_action = QAction("Open Output Folder", self)
        open_dir_action.triggered.connect(self._open_output_dir)
        menu.addAction(open_dir_action)

        logs_menu = menu.addMenu("Logs")
        view_log_action = QAction("View Last Log...", self)
        view_log_action.triggered.connect(self._view_last_log)
        logs_menu.addAction(view_log_action)
        open_logs_action = QAction("Open Log Folder", self)
        open_logs_action.triggered.connect(self._open_log_dir)
        logs_menu.addAction(open_logs_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.show()

    def _update_region_label(self):
        r = self.settings.get("region")
        if r:
            self.region_label.setText(f"Region: {r[0]},{r[1]} — {r[2]}×{r[3]}")
        else:
            self.region_label.setText("No region selected")

    def _show_tray_message(self, title, msg, icon, timeout):
        self.tray.showMessage(title, msg, icon, timeout)

    @staticmethod
    def _region_for_mss(region_list):
        """Convert stored [x, y, w, h] logical coords to mss physical-pixel dict.

        On Windows, Qt6 enables per-monitor DPI awareness, so Qt logical
        coordinates differ from physical screen pixels.  mss always captures
        using physical pixels on Windows.  On macOS/Linux, mss coordinates
        are logical and the DPR is handled internally, so no scaling needed.
        """
        x, y, w, h = region_list
        if sys.platform == "win32":
            screen = QApplication.screenAt(QPoint(x + w // 2, y + h // 2))
            if screen is None:
                screen = QApplication.primaryScreen()
            dpr = screen.devicePixelRatio()
            x, y, w, h = (int(v * dpr) for v in (x, y, w, h))
        return {"left": x, "top": y, "width": w, "height": h}

    @staticmethod
    def _device_pixel_ratio(region_list):
        x, y, w, h = region_list
        screen = QApplication.screenAt(QPoint(x + w // 2, y + h // 2))
        if screen is None:
            screen = QApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen else 1.0

    def _region_rect(self):
        r = self.settings.get("region")
        return QRect(r[0], r[1], r[2], r[3]) if r else None

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.recording:
                self.toggle_recording()
            elif self.settings.get("region"):
                self.toggle_recording()
            else:
                self.select_region()

    # --- Region selection ---
    def select_region(self):
        initial = self._region_rect()
        # The overlay would sit on top of the selector, so hide it for the
        # duration and put it back if the selection is cancelled.
        was_previewing = self.overlay_action.isChecked()
        if was_previewing:
            self.overlay_action.setChecked(False)

        def _restore_overlay():
            if was_previewing and not self.overlay_action.isChecked():
                self.overlay_action.setChecked(True)

        self.region_selector = RegionSelector(initial)
        self.region_selector.region_selected.connect(self._on_region_selected)
        self.region_selector.closed.connect(_restore_overlay)
        self.region_selector.show()
        self.region_selector.raise_()
        self.region_selector.activateWindow()

    def _on_region_selected(self, rect):
        self.settings["region"] = [rect.x(), rect.y(), rect.width(), rect.height()]
        save_settings(self.settings)
        self._update_region_label()
        log.info(
            "Region selected: %d×%d at (%d, %d)",
            rect.width(),
            rect.height(),
            rect.x(),
            rect.y(),
        )
        self.tray.showMessage(
            "Region Selected",
            f"{rect.width()} × {rect.height()} at ({rect.x()}, {rect.y()})",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
        # Show the outline straight away so the region can be checked.
        self.overlay_action.setChecked(True)

    # --- Overlay ---
    def _toggle_idle_overlay(self, checked):
        if self.recording:
            # Mid-scan the overlay is the recording one; let it be shown or
            # dismissed, but leave the idle preview grabber alone.
            if checked:
                self.overlay.show(self._region_rect(), recording=True, show_panel=True)
                self._update_recording_status()
            else:
                self.overlay.hide()
            return
        if checked:
            region = self._region_rect()
            if region is None:
                self.overlay_action.setChecked(False)
                self.tray.showMessage(
                    "No Region",
                    "Select a screen region first.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    3000,
                )
                return
            self.overlay.show(region, recording=False, show_panel=True)
            self._update_idle_overlay()
            self._preview_timer.start(250)
        else:
            self._preview_timer.stop()
            self._close_grabber()
            self.overlay.hide()

    def _grab_region(self):
        """Grab the region once for the idle preview (GUI thread)."""
        region = self.settings.get("region")
        if not region:
            return None
        try:
            if self._sct is None:
                self._sct = mss.mss()
            img = self._sct.grab(self._region_for_mss(region))
            return cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
        except Exception as exc:
            log.warning("Overlay preview grab failed: %s", exc)
            self._close_grabber()
            return None

    def _close_grabber(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None

    def _update_idle_overlay(self):
        if self.recording or not self.overlay_action.isChecked():
            return
        region = self.settings.get("region")
        if not region:
            return
        frame = (
            self._grab_region()
            if self.settings.get("overlay_preview", True)
            else None
        )
        lines = [
            f"{region[2]} × {region[3]} px at ({region[0]}, {region[1]})",
            f"{self.settings.get('scan_speed', 0):g} µm/s · "
            f"{self.settings.get('fps', 0)} fps (last used)",
        ]
        self.overlay.update_status("Region preview", lines, frame)

    # --- Recording ---
    def toggle_recording(self):
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        region = self.settings.get("region")
        if not region:
            log.warning("Start requested with no region selected")
            self.tray.showMessage(
                "No Region",
                "Please select a screen region first.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        dialog = RecordingSetupDialog(self.settings, region)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            log.info("Recording cancelled at the setup dialog")
            return

        values = dialog.values()
        notes = values.pop("notes")  # per-scan, belongs in the sidecar only
        self.settings.update(values)
        save_settings(self.settings)

        out_dir = Path(values["output_dir"])
        video_path = str(out_dir / dialog.filename())
        scan_speed = values["scan_speed"]
        fps = values["fps"]

        region_dict = self._region_for_mss(region)
        metadata = {
            "scan_speed_um_s": scan_speed,
            "label": values["scan_label"],
            "notes": notes,
            "axis": values["axis"],
            "skip": self.settings.get("skip", 1),
            "tolerance": self.settings.get("tolerance", 0.3),
            "compress": self.settings.get("compress", 1.0),
            "region_logical": list(region),
            "device_pixel_ratio": self._device_pixel_ratio(region),
        }

        log.info(
            "Starting recording: %s (%.1f µm/s, %d fps)", video_path, scan_speed, fps
        )

        self.recorder = ScreenRecorder(
            region=region_dict,
            output_path=video_path,
            fps=fps,
            metadata=metadata,
            capture_preview=bool(values["overlay_preview"]),
        )
        self.recorder.signals.finished.connect(self._on_recording_finished)
        self.recorder.signals.error.connect(self._on_recording_error)
        self.recorder.start()

        self.recording = True
        self.current_video_path = video_path
        self.current_scan_speed = scan_speed
        self.last_log_path = self.recorder.log_path
        self.record_action.setText("Stop Recording")
        self.select_action.setEnabled(False)
        self.tray.setIcon(self._create_icon(recording=True))
        self.tray.setToolTip(
            f"Taper Scan Recorder — RECORDING ({scan_speed:g} µm/s)"
        )
        self.status_action.setVisible(True)
        self._record_start_time = time.time()

        # Swap the idle preview for the recording overlay
        self._preview_timer.stop()
        self._close_grabber()
        if values["show_overlay"]:
            self.overlay.show(
                self._region_rect(),
                recording=True,
                show_panel=True,
            )
        else:
            self.overlay.hide()
        # Keep the menu's check mark truthful without re-triggering the slot
        self.overlay_action.blockSignals(True)
        self.overlay_action.setChecked(bool(values["show_overlay"]))
        self.overlay_action.blockSignals(False)
        self._update_status_timer.start(500)
        self._update_recording_status()

        self.tray.showMessage(
            "Recording Started",
            f"{scan_speed:g} µm/s → {Path(video_path).name}",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def _stop_recording(self):
        log.info("Stopping recording")
        if self.recorder:
            self.recorder.stop()
        self.recording = False
        self.record_action.setText("Start Recording...")
        self.select_action.setEnabled(True)
        self.tray.setIcon(self._create_icon(recording=False))
        self.tray.setToolTip("Taper Scan Recorder")
        self._update_status_timer.stop()
        self.status_action.setVisible(False)

        # Back to the idle overlay if it was left switched on
        if self.overlay_action.isChecked():
            self._toggle_idle_overlay(True)
        else:
            self.overlay.hide()

    def _update_recording_status(self):
        if not self.recorder:
            return
        elapsed = time.time() - self._record_start_time
        mins, secs = divmod(int(elapsed), 60)
        n = self.recorder.frame_count
        rate = n / elapsed if elapsed > 0 else 0.0
        self.status_action.setText(f"  {mins:02d}:{secs:02d}  |  {n} frames")

        if self.overlay.visible:
            lines = [
                f"{n} frames · {rate:.1f} fps",
                f"{self.current_scan_speed:g} µm/s"
                if self.current_scan_speed
                else "",
                Path(self.current_video_path).name
                if self.current_video_path
                else "",
            ]
            self.overlay.update_status(
                f"REC  {mins:02d}:{secs:02d}",
                [line for line in lines if line],
                self.recorder.latest_preview(),
            )

    def _on_recording_finished(self, path):
        self.recon_action.setEnabled(True)
        stats = self.recorder.stats if self.recorder else {}
        n = stats.get("frame_count", self.recorder.frame_count if self.recorder else 0)
        effective = stats.get("effective_fps")
        log.info("Recording finished: %s (%s frames)", path, n)
        detail = f"{n} frames saved to {Path(path).name}"
        if effective:
            detail += f"\nMeasured {effective:.2f} fps"
        self.tray.showMessage(
            "Recording Complete",
            detail,
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )
        if self.settings.get("auto_reconstruct"):
            # Values were typed into the setup dialog moments ago; no need
            # to confirm them again.
            self._run_reconstruction(path, confirm=False)

    def _on_recording_error(self, msg):
        log.error("Recording error: %s", msg)
        self.recording = False
        self.record_action.setText("Start Recording...")
        self.select_action.setEnabled(True)
        self.tray.setIcon(self._create_icon(recording=False))
        self._update_status_timer.stop()
        self.status_action.setVisible(False)
        self.overlay.hide()
        self.tray.showMessage(
            "Recording Error",
            f"{msg}\nLog: {self.last_log_path}",
            QSystemTrayIcon.MessageIcon.Critical,
            5000,
        )

    # --- Reconstruction ---
    def reconstruct_last(self):
        if self.current_video_path and Path(self.current_video_path).exists():
            self._run_reconstruction(self.current_video_path)
        else:
            self.tray.showMessage(
                "Nothing to Reconstruct",
                "No recording from this session was found.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )

    def reconstruct_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            None,
            "Select scan video",
            self.settings["output_dir"],
            "Video files (*.mp4 *.avi *.mkv *.mov);;All files (*)",
        )
        if path:
            self._run_reconstruction(path)

    def _reconstruction_defaults(self, video_path):
        """Scan facts for a reconstruction, each tagged with its provenance."""
        from taper_tools.reconstruct import get_video_info

        params = sidecar.scan_parameters(video_path)
        s = self.settings
        try:
            container_fps = float(get_video_info(video_path)[0])
        except Exception as exc:
            log.warning("Could not read %s: %s", Path(video_path).name, exc)
            container_fps = 0.0
        if not np.isfinite(container_fps):
            container_fps = 0.0

        d = {"label": params.get("label"), "container_fps": container_fps}

        if "scan_speed" in params:
            d["scan_speed"] = params["scan_speed"]
            source = params.get("source", "")
            d["speed_source"] = (
                "From the file name"
                if source == "file name"
                else f"From the recording ({Path(source).name})"
            )
            d["speed_warn"] = False
        else:
            d["scan_speed"] = float(s["scan_speed"])
            d["speed_source"] = (
                "Settings default — this video has no recorded scan speed. "
                "Check it before continuing."
            )
            d["speed_warn"] = True

        if "axis" in params:
            d["axis"] = params["axis"]
            d["axis_source"] = "From the recording"
            d["axis_warn"] = False
        else:
            d["axis"] = int(s["axis"])
            d["axis_source"] = "Settings default — not recorded with this video"
            d["axis_warn"] = True

        if "fps" in params:
            d["fps"] = params["fps"]
            d["fps_source"] = (
                f"Measured during recording (container says {container_fps:.2f})"
                if container_fps > 0
                else "Measured during recording"
            )
            d["fps_warn"] = False
        elif container_fps > 0:
            d["fps"] = container_fps
            d["fps_source"] = "From the video container"
            d["fps_warn"] = False
        else:
            d["fps"] = float(s["fps"])
            d["fps_source"] = (
                "The container reports no frame rate — enter the rate the "
                "scan was captured at"
            )
            d["fps_warn"] = True
        return d

    def _run_reconstruction(self, video_path, confirm=True):
        """Run taper reconstruction in a background thread.

        With ``confirm`` the scan facts are shown for checking first;
        automatic reconstruction straight after a recording skips that.
        """
        from taper_tools.reconstruct import compress_along_axis, reconstruct_taper

        out_path = str(Path(video_path).with_suffix(".reconstructed.png"))
        recon_log_path = Path(video_path).with_suffix(".reconstruct.log")
        s = dict(self.settings)

        defaults = self._reconstruction_defaults(video_path)
        sources = {
            "scan_speed": defaults["speed_source"],
            "axis": defaults["axis_source"],
            "fps": defaults["fps_source"],
        }
        if confirm:
            dialog = ReconstructionDialog(video_path, defaults, s)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                log.info("Reconstruction of %s cancelled", Path(video_path).name)
                return
            chosen = dialog.values()
            for key in sources:
                if abs(float(chosen[key]) - float(defaults[key])) > 1e-9:
                    sources[key] = f"Entered by user (was {defaults[key]})"
        else:
            chosen = {k: defaults[k] for k in ("scan_speed", "axis", "fps")}
            if defaults["speed_warn"]:
                log.warning(
                    "No recorded scan speed for %s; using the settings value "
                    "of %.1f µm/s",
                    Path(video_path).name,
                    chosen["scan_speed"],
                )

        self.last_log_path = recon_log_path
        scan_speed = float(chosen["scan_speed"])
        axis = int(chosen["axis"])
        # Only override the container rate when it actually differs
        fps_override = (
            float(chosen["fps"])
            if abs(float(chosen["fps"]) - defaults["container_fps"]) > 1e-3
            else None
        )

        self.tray.showMessage(
            "Reconstructing...",
            f"{Path(video_path).name} at {scan_speed:g} µm/s",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

        def _worker():
            with file_log(
                recon_log_path,
                thread_only=True,
                header="=== taper reconstruction ===",
            ):
                log_environment(
                    log,
                    {
                        "video": video_path,
                        "output": out_path,
                        "scan speed": scan_speed,
                        "speed source": sources["scan_speed"],
                        "axis": axis,
                        "axis source": sources["axis"],
                        "fps override": fps_override,
                        "fps source": sources["fps"],
                        "skip": s["skip"],
                        "tolerance": s["tolerance"],
                        "compress": s.get("compress"),
                    },
                )
                try:
                    result, shifts = reconstruct_taper(
                        video_path=video_path,
                        mode="auto",
                        scan_speed=scan_speed,
                        axis=axis,
                        skip=s["skip"],
                        tolerance=s["tolerance"],
                        fps_override=fps_override,
                    )
                    # Apply compression
                    compress = s.get("compress", 1.0)
                    if compress and compress > 1:
                        result = compress_along_axis(result, compress, axis)
                        log.info(
                            "Compressed %.1fx along the scan axis -> %d x %d px",
                            compress,
                            result.shape[1],
                            result.shape[0],
                        )

                    if not cv2.imwrite(out_path, result):
                        raise OSError(f"Could not write image: {out_path}")
                    log.info("Saved reconstruction: %s", out_path)

                    self._recon_message.emit(
                        "Reconstruction Complete",
                        f"Saved: {Path(out_path).name}\n"
                        f"Size: {result.shape[1]} × {result.shape[0]} px",
                        QSystemTrayIcon.MessageIcon.Information,
                        5000,
                    )
                except Exception as exc:
                    detail = traceback.format_exc()
                    log.error("Reconstruction failed:\n%s", detail)
                    self._recon_failed.emit(
                        str(exc), detail, str(recon_log_path)
                    )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _show_reconstruction_failure(self, message, detail, log_path):
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Reconstruction Failed")
        box.setText(message)
        box.setInformativeText(
            f"A log of the run was written to:\n{log_path}\n\n"
            f"Application log: {app_log_path()}"
        )
        box.setDetailedText(detail)
        open_btn = box.addButton("Open Log", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_path(log_path)

    # --- Settings ---
    def show_settings(self):
        dlg = SettingsDialog(self.settings)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings = dlg.get_settings()
            save_settings(self.settings)
            log.info("Settings updated")
            if self.overlay_action.isChecked() and not self.recording:
                self._toggle_idle_overlay(True)

    # --- Files and logs ---
    @staticmethod
    def _open_path(path):
        import subprocess

        path = str(path)
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as exc:
            log.error("Could not open %s: %s", path, exc)

    def _open_output_dir(self):
        out_dir = Path(self.settings["output_dir"])
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Could not create %s: %s", out_dir, exc)
            return
        self._open_path(out_dir)

    def _open_log_dir(self):
        self._open_path(get_log_dir())

    def _view_last_log(self):
        path = Path(self.last_log_path) if self.last_log_path else app_log_path()
        if not path.exists():
            path = app_log_path()
        if not path.exists():
            self.tray.showMessage(
                "No Log Yet",
                "Nothing has been logged in this session.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("Could not read log %s: %s", path, exc)
            return

        dlg = QDialog()
        dlg.setWindowTitle(f"Log — {path.name}")
        dlg.resize(900, 600)
        layout = QVBoxLayout(dlg)
        header = QLabel(str(path))
        header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(header)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("monospace", 9))
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(text)
        view.moveCursor(view.textCursor().MoveOperation.End)
        layout.addWidget(view)
        btn_row = QHBoxLayout()
        folder_btn = QPushButton("Open Folder")
        folder_btn.clicked.connect(lambda: self._open_path(path.parent))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(folder_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        dlg.exec()

    def _quit(self):
        log.info("Quitting")
        if self.recording:
            self._stop_recording()
        self._preview_timer.stop()
        self._close_grabber()
        self.overlay.hide()
        save_settings(self.settings)
        QApplication.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _install_excepthook():
    """Make sure an unhandled GUI exception reaches the log."""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        log.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def main():
    log_path = setup_app_logging(console=True)
    _install_excepthook()
    log.info("=== taper recorder starting ===")
    log_environment(log, {"log file": log_path})

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Taper Scan Recorder")

    # Check system tray is available
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("System tray not available")
        QMessageBox.critical(
            None,
            "System Tray",
            "System tray not available on this system.",
        )
        sys.exit(1)

    # First launch: prompt for default output folder
    if not SETTINGS_PATH.exists():
        folder = QFileDialog.getExistingDirectory(
            None,
            "Choose default folder for taper scan recordings",
            str(Path.home()),
        )
        if folder:
            settings = load_settings()
            settings["output_dir"] = folder
            save_settings(settings)

    recorder_app = TaperRecorderApp()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
