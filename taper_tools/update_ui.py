"""Update checks from the tray: a quiet check at start-up and "Check for Updates...".

``UpdateController(updater, notify, before_restart)``. The check and download run on a
worker thread; results come back as Qt signals.

The recorder has no main window, so the bottom-of-window "Restart now / Later" bar becomes:

* after the quiet start-up check, a tray message plus a "Restart to Install vX.Y.Z" menu
  item (``restart_action``), which stays until the user picks it or quits;
* after a manual check, a dialog with "Restart Now" and "Later".

"Later" needs nothing: the downloaded update is installed the next time the app starts.
``before_restart()`` must return True first (it refuses while a recording or a
reconstruction is running, and saves settings). The start-up check says nothing unless an
update is ready; a manual check reports every outcome.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMessageBox

from taper_tools.updates import Update, UpdateError, Updater

log = logging.getLogger(__name__)

APP_TITLE = "Taper Recorder"


def ask_restart(version: str) -> bool:
    """Ask "Restart Now" or "Later"; True for Restart Now."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Update Ready")
    box.setText(f"Version {version} has been downloaded.")
    box.setInformativeText(
        "Restart now to use it, or it is installed the next time the app starts."
    )
    restart = box.addButton("Restart Now", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(restart)
    box.exec()
    return box.clickedButton() is restart


class UpdateController(QObject):
    _ready = pyqtSignal(object, bool)  # (Update | None, manual)
    _failed = pyqtSignal(str, bool)  # (message, manual)

    def __init__(
        self,
        updater: Updater,
        notify: Callable[[str, str], None],
        before_restart: Callable[[], bool],
        parent: QObject | None = None,
        threaded: bool = True,
    ):
        super().__init__(parent)
        self.updater = updater
        self.notify = notify
        self.before_restart = before_restart
        self.threaded = threaded
        self.busy = False
        self.report = False  # a manual check came in while a check was running
        self.pending: Update | None = None

        self.check_action = QAction("Check for Updates...", self)
        self.check_action.triggered.connect(lambda: self.check(manual=True))
        self.restart_action = QAction("", self)
        self.restart_action.setVisible(False)
        self.restart_action.triggered.connect(self.restart_now)

        self._ready.connect(self._on_ready)
        self._failed.connect(self._on_failed)

    # ---- checking ---------------------------------------------------------------

    def check(self, manual: bool = False) -> None:
        """Look for an update and download it. ``manual`` reports every outcome."""
        if self.pending is not None:
            if manual:
                self._offer_restart(self.pending)
            return
        if self.busy:
            # the running check reports its outcome to this manual check
            self.report = self.report or manual
            return
        if not self.updater.available():
            if manual:
                try:
                    self.updater.check()
                except UpdateError as e:
                    QMessageBox.information(None, "Check for Updates", str(e))
            return
        self.busy = True
        if self.threaded:
            threading.Thread(target=self._work, args=(manual,), daemon=True).start()
        else:
            self._work(manual)

    def _work(self, manual: bool) -> None:
        try:
            up = self.updater.check()
            if up is not None:
                self.updater.download(up)
            self._ready.emit(up, manual)
        except UpdateError as e:
            self._failed.emit(str(e), manual)
        except Exception as e:  # never let the worker die silently
            log.exception("update check failed")
            self._failed.emit(f"Update check failed ({type(e).__name__}): {e}", manual)

    def _on_ready(self, up: Update | None, manual: bool) -> None:
        self.busy = False
        manual, self.report = manual or self.report, False
        if up is None:
            log.info("update check: up to date")
            if manual:
                QMessageBox.information(
                    None,
                    "Check for Updates",
                    f"{APP_TITLE} {self.updater.current_version()} is the latest version.",
                )
            return
        log.info("update %s downloaded", up.version)
        self.pending = up
        self.restart_action.setText(f"Restart to Install v{up.version}")
        self.restart_action.setVisible(True)
        if manual:
            self._offer_restart(up)
        else:
            self.notify(
                "Update Ready",
                f"Version {up.version} has been downloaded. Choose "
                f"“Restart to Install v{up.version}” from the tray menu, "
                "or it is installed the next time the app starts.",
            )

    def _on_failed(self, msg: str, manual: bool) -> None:
        self.busy = False
        manual, self.report = manual or self.report, False
        log.info("update check: %s", msg)
        if manual:
            QMessageBox.warning(None, "Check for Updates", msg)

    def _offer_restart(self, up: Update) -> None:
        if ask_restart(up.version):
            self.restart_now()

    # ---- installing ---------------------------------------------------------------

    def restart_now(self) -> bool:
        """Install the downloaded update and restart (after ``before_restart``)."""
        if self.pending is None or not self.before_restart():
            return False
        try:
            self.updater.restart_into(self.pending)  # exits the process on success
        except UpdateError as e:
            QMessageBox.warning(None, "Update", str(e))
            return False
        return True
