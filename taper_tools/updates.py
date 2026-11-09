"""Installing and updating the recorder with Velopack (no Qt here; update_ui.py drives it).

The Windows release is a Velopack package: ``TaperRecorder-win-Setup.exe`` installs it per
user under ``%LOCALAPPDATA%\\TaperRecorder`` (Start menu shortcut, Add/Remove Programs, no
admin rights) and the installed app updates itself from this repository's GitHub releases.
The repository is public, so no access token is needed.

Everything here degrades quietly: in a development checkout, a copy that wasn't installed,
or offline, ``check()`` raises ``UpdateError`` and the app carries on.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable

from taper_tools._version import __version__

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/SAIL-Labs/taper_reconstruct"


class UpdateError(Exception):
    """An update check, download or install could not be done."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# Arguments Velopack starts the exe with during install, update and uninstall.
HOOK_ARGS = ("--veloapp-install", "--veloapp-updated", "--veloapp-obsolete",
             "--veloapp-uninstall")


def is_install_hook(argv) -> bool:
    """True when Velopack started this process to run an install/update/uninstall hook."""
    return any(a in HOOK_ARGS for a in argv)


def run_install_hooks() -> None:
    """Velopack's start-up hook. Must run first thing in ``main``: during install,
    update and uninstall Velopack starts the exe with special arguments and this handles
    them (and exits). With auto-apply on, an update downloaded earlier and left for
    "later" is installed here. A no-op when not frozen or when velopack can't load."""
    if not is_frozen():
        return
    try:
        import velopack

        velopack.App().set_auto_apply_on_startup(True).run()
    except Exception:  # never stop the app from starting
        log.exception("velopack start-up hook failed")


@dataclass
class Update:
    version: str
    info: Any  # velopack.UpdateInfo
    notes: str = ""


class Updater:
    """Checks for, downloads and installs updates. ``factory`` makes the velopack
    UpdateManager (tests pass a fake)."""

    def __init__(self, factory: Callable[[], Any] | None = None):
        self._factory = factory or self._velopack_manager
        self._mgr = None
        self._error: str | None = None

    @staticmethod
    def _velopack_manager():
        if not is_frozen():
            raise UpdateError("Updates are only available in the installed app.")
        import velopack

        return velopack.UpdateManager(velopack.GithubSource(REPO_URL, None, False))

    def _manager(self):
        if self._mgr is None and self._error is None:
            try:
                self._mgr = self._factory()
            except UpdateError as e:
                self._error = str(e)
            except Exception as e:  # velopack: "not installed", ...
                log.info("updates unavailable: %s", e)
                self._error = (
                    "Updates are only available in the installed app "
                    f"(install it with TaperRecorder-win-Setup.exe). ({e})"
                )
        if self._mgr is None:
            raise UpdateError(self._error)
        return self._mgr

    def available(self) -> bool:
        try:
            self._manager()
            return True
        except UpdateError:
            return False

    def current_version(self) -> str:
        try:
            return str(self._manager().get_current_version())
        except Exception:
            return __version__

    def check(self) -> Update | None:
        """The newest release if it is newer than this one, else None. Blocking (network)."""
        mgr = self._manager()
        try:
            info = mgr.check_for_updates()
        except Exception as e:
            raise UpdateError(f"Could not check for updates: {e}") from e
        if info is None:
            return None
        target = info.TargetFullRelease
        notes = str(getattr(target, "NotesMarkdown", "") or "")
        return Update(str(target.Version), info, notes)

    def download(self, update: Update) -> None:
        """Download ``update`` (blocking). Once downloaded it is installed the next time
        the app starts, or at once with ``restart_into``."""
        try:
            self._manager().download_updates(update.info)
        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(f"Could not download the update: {e}") from e

    def restart_into(self, update: Update) -> None:
        """Install the downloaded ``update`` and restart the app (exits this process)."""
        try:
            self._manager().apply_updates_and_restart(update.info)
        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(f"Could not install the update: {e}") from e
