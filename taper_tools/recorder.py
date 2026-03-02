"""
Taper Scan Recorder
====================

A system tray application for recording screen regions during photonic
lantern taper diameter scans, with integrated taper image reconstruction.

Features:
  - System tray icon with quick-access menu
  - Interactive screen region selection (rubber band)
  - Records a fixed screen region to .mp4
  - Configurable frame rate and scan parameters
  - One-click taper reconstruction from recorded video
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
import time
import threading
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


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "TaperScans"),
    "fps": 15,
    "scan_speed": 1000.0,
    "skip": 1,
    "tolerance": 0.3,
    "axis": 1,
    "compress": 10.0,
    "auto_reconstruct": False,
    "region": None,  # [x, y, w, h]
}


def load_settings():
    settings_path = Path.home() / ".taper_recorder.json"
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                saved = json.load(f)
            merged = {**DEFAULT_SETTINGS, **saved}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    settings_path = Path.home() / ".taper_recorder.json"
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)


# ---------------------------------------------------------------------------
# Region selection overlay
# ---------------------------------------------------------------------------
class RegionSelector(QWidget):
    """Full-screen transparent overlay for rubber-band region selection."""

    region_selected = pyqtSignal(QRect)

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


# ---------------------------------------------------------------------------
# Screen recorder (runs in a thread)
# ---------------------------------------------------------------------------
class RecorderSignals(QObject):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    frame_count = pyqtSignal(int)


class ScreenRecorder:
    """Records a screen region to an MP4 file using mss + opencv."""

    def __init__(self, region, output_path, fps=15):
        """
        Parameters
        ----------
        region : dict
            {"left": x, "top": y, "width": w, "height": h} in screen coords.
        output_path : str
            Path for the output .mp4 file.
        fps : int
            Target frame rate.
        """
        self.region = region
        self.output_path = output_path
        self.fps = fps
        self.running = False
        self.signals = RecorderSignals()
        self._thread = None
        self._frame_count = 0

    def start(self):
        self.running = True
        self._frame_count = 0
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def frame_count(self):
        return self._frame_count

    def _record_loop(self):
        writer = None
        try:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            frame_interval = 1.0 / self.fps

            with mss.mss() as sct:
                # Grab first frame to determine actual dimensions
                # (may differ from region dict due to DPI or edge clipping)
                img = sct.grab(self.region)
                frame = np.array(img)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                h, w = frame.shape[:2]

                writer = cv2.VideoWriter(
                    self.output_path, fourcc, self.fps, (w, h)
                )
                if not writer.isOpened():
                    self.signals.error.emit(
                        f"Failed to open video writer ({w}x{h}, mp4v).\n"
                        "Check codec support."
                    )
                    return

                writer.write(frame)
                self._frame_count += 1

                while self.running:
                    t0 = time.perf_counter()
                    img = sct.grab(self.region)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    writer.write(frame)
                    self._frame_count += 1

                    elapsed = time.perf_counter() - t0
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)

            self.signals.finished.emit(self.output_path)
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            if writer is not None:
                writer.release()


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

        layout.addWidget(rec_group)

        # Reconstruction settings
        recon_group = QGroupBox("Reconstruction")
        recon_form = QFormLayout(recon_group)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(1, 100000)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setSuffix(" µm/s")
        self.speed_spin.setValue(self.settings["scan_speed"])
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
        }


# ---------------------------------------------------------------------------
# Main tray application
# ---------------------------------------------------------------------------
class TaperRecorderApp(QObject):
    _recon_message = pyqtSignal(str, str, QSystemTrayIcon.MessageIcon, int)

    def __init__(self):
        super().__init__()
        self._recon_message.connect(self._show_tray_message)
        self.settings = load_settings()
        self.recorder = None
        self.recording = False
        self.current_video_path = None
        self.region_selector = None

        self._setup_tray()
        self._update_status_timer = QTimer()
        self._update_status_timer.timeout.connect(self._update_recording_status)

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

        menu.addSeparator()

        self.record_action = QAction("Start Recording", self)
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
        initial = None
        r = self.settings.get("region")
        if r:
            initial = QRect(r[0], r[1], r[2], r[3])

        self.region_selector = RegionSelector(initial)
        self.region_selector.region_selected.connect(self._on_region_selected)
        self.region_selector.show()
        self.region_selector.raise_()
        self.region_selector.activateWindow()

    def _on_region_selected(self, rect):
        self.settings["region"] = [rect.x(), rect.y(), rect.width(), rect.height()]
        save_settings(self.settings)
        self._update_region_label()
        self.tray.showMessage(
            "Region Selected",
            f"{rect.width()} × {rect.height()} at ({rect.x()}, {rect.y()})",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    # --- Recording ---
    def toggle_recording(self):
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        r = self.settings.get("region")
        if not r:
            self.tray.showMessage(
                "No Region",
                "Please select a screen region first.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        # Prompt for output folder (starts at last-used directory)
        folder = QFileDialog.getExistingDirectory(
            None,
            "Save recording to...",
            self.settings["output_dir"],
        )
        if not folder:
            return  # user cancelled
        out_dir = Path(folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Remember for next time
        self.settings["output_dir"] = str(out_dir)
        save_settings(self.settings)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        video_path = str(out_dir / f"taper_scan_{timestamp}.mp4")

        region_dict = self._region_for_mss(r)

        self.recorder = ScreenRecorder(
            region=region_dict,
            output_path=video_path,
            fps=self.settings["fps"],
        )
        self.recorder.signals.finished.connect(self._on_recording_finished)
        self.recorder.signals.error.connect(self._on_recording_error)
        self.recorder.start()

        self.recording = True
        self.current_video_path = video_path
        self.record_action.setText("Stop Recording")
        self.select_action.setEnabled(False)
        self.tray.setIcon(self._create_icon(recording=True))
        self.tray.setToolTip("Taper Scan Recorder — RECORDING")
        self.status_action.setVisible(True)
        self._update_status_timer.start(500)
        self._record_start_time = time.time()

        self.tray.showMessage(
            "Recording Started",
            f"Saving to {video_path}",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def _stop_recording(self):
        if self.recorder:
            self.recorder.stop()
        self.recording = False
        self.record_action.setText("Start Recording")
        self.select_action.setEnabled(True)
        self.tray.setIcon(self._create_icon(recording=False))
        self.tray.setToolTip("Taper Scan Recorder")
        self._update_status_timer.stop()
        self.status_action.setVisible(False)

    def _update_recording_status(self):
        if self.recorder:
            elapsed = time.time() - self._record_start_time
            mins, secs = divmod(int(elapsed), 60)
            n = self.recorder.frame_count
            self.status_action.setText(f"  {mins:02d}:{secs:02d}  |  {n} frames")

    def _on_recording_finished(self, path):
        self.recon_action.setEnabled(True)
        n = self.recorder.frame_count if self.recorder else 0
        self.tray.showMessage(
            "Recording Complete",
            f"{n} frames saved to {Path(path).name}",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )
        if self.settings.get("auto_reconstruct"):
            self._run_reconstruction(path)

    def _on_recording_error(self, msg):
        self.recording = False
        self.record_action.setText("Start Recording")
        self.select_action.setEnabled(True)
        self.tray.setIcon(self._create_icon(recording=False))
        self._update_status_timer.stop()
        self.tray.showMessage(
            "Recording Error",
            msg,
            QSystemTrayIcon.MessageIcon.Critical,
            5000,
        )

    # --- Reconstruction ---
    def reconstruct_last(self):
        if self.current_video_path and Path(self.current_video_path).exists():
            self._run_reconstruction(self.current_video_path)

    def reconstruct_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            None,
            "Select scan video",
            self.settings["output_dir"],
            "Video files (*.mp4 *.avi *.mkv *.mov);;All files (*)",
        )
        if path:
            self._run_reconstruction(path)

    def _run_reconstruction(self, video_path):
        """Run taper reconstruction in a background thread."""
        from taper_tools.reconstruct import reconstruct_taper

        out_path = str(Path(video_path).with_suffix(".reconstructed.png"))
        s = self.settings

        self.tray.showMessage(
            "Reconstructing...",
            f"Processing {Path(video_path).name}",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

        def _worker():
            try:
                result, shifts = reconstruct_taper(
                    video_path=video_path,
                    mode="auto",
                    scan_speed=s["scan_speed"],
                    axis=s["axis"],
                    skip=s["skip"],
                    tolerance=s["tolerance"],
                )
                # Apply compression
                compress = s.get("compress", 1.0)
                if compress and compress > 1:
                    h, w = result.shape[:2]
                    if s["axis"] == 1:
                        new_w = max(1, int(round(w / compress)))
                        result = cv2.resize(
                            result, (new_w, h), interpolation=cv2.INTER_AREA
                        )
                    else:
                        new_h = max(1, int(round(h / compress)))
                        result = cv2.resize(
                            result, (w, new_h), interpolation=cv2.INTER_AREA
                        )

                cv2.imwrite(out_path, result)
                self._recon_message.emit(
                    "Reconstruction Complete",
                    f"Saved: {Path(out_path).name}\n"
                    f"Size: {result.shape[1]} × {result.shape[0]} px",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            except Exception as e:
                self._recon_message.emit(
                    "Reconstruction Failed",
                    str(e),
                    QSystemTrayIcon.MessageIcon.Critical,
                    5000,
                )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    # --- Settings ---
    def show_settings(self):
        dlg = SettingsDialog(self.settings)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings = dlg.get_settings()
            save_settings(self.settings)

    def _open_output_dir(self):
        out_dir = Path(self.settings["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        # Cross-platform open
        import subprocess

        if sys.platform == "win32":
            os.startfile(str(out_dir))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(out_dir)])
        else:
            subprocess.run(["xdg-open", str(out_dir)])

    def _quit(self):
        if self.recording:
            self._stop_recording()
        save_settings(self.settings)
        QApplication.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Taper Scan Recorder")

    # Check system tray is available
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "System Tray",
            "System tray not available on this system.",
        )
        sys.exit(1)

    # First launch: prompt for default output folder
    settings_path = Path.home() / ".taper_recorder.json"
    if not settings_path.exists():
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
