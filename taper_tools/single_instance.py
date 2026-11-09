"""Keep to one running recorder per user.

A lock file decides which copy runs; the running copy also listens on a local socket so a
second launch can tell it "someone tried to start me" (the recorder lives in the tray and
is otherwise easy to miss) and then exit.

* ``QLockFile`` gives the exclusivity. Its stale detection is by process ID, so a copy
  that crashed doesn't block the next start. The age-based staleness is turned off: a lock
  held for days by a running copy is not stale.
* ``QLocalServer`` / ``QLocalSocket`` carry the "activate" message. On Windows several
  copies can listen on the same pipe name, so the socket alone can't be the lock.

The lock is per user (it lives in the user's temp folder and the socket name includes the
user name), so other users on a shared PC each get their own recorder.
"""
from __future__ import annotations

import getpass
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QLockFile
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

ACTIVATE = b"activate\n"


def _user_tag() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return hashlib.sha1(user.encode("utf-8", "replace")).hexdigest()[:12]


class SingleInstance:
    def __init__(self, key: str = "taper-recorder", directory: Path | None = None):
        self.key = key
        self.server_name = f"{key}-{_user_tag()}"
        self.lock_path = Path(directory or tempfile.gettempdir()) / f"{key}.lock"
        self._lock = QLockFile(str(self.lock_path))
        self._lock.setStaleLockTime(0)  # stale only if the holder's process is gone
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """True if this is the only running copy (it now holds the lock)."""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if self._lock.tryLock(100):
            return True
        err = self._lock.error()
        if err != QLockFile.LockError.LockFailedError:
            # Can't create the lock file at all (read-only temp, ...): don't stop the
            # user from working because of it.
            log.warning("single-instance lock unavailable (%s); not enforcing", err)
            return True
        return False

    def notify_running(self, timeout_ms: int = 1000) -> bool:
        """Tell the running copy that another launch happened. False if it didn't answer."""
        sock = QLocalSocket()
        sock.connectToServer(self.server_name)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(ACTIVATE)
        ok = sock.waitForBytesWritten(timeout_ms)
        sock.disconnectFromServer()
        return ok

    def listen(self, on_activate: Callable[[], None]) -> None:
        """Call ``on_activate`` whenever another launch reports in. Needs a QApplication."""
        QLocalServer.removeServer(self.server_name)  # a crashed copy's socket (Unix)
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.server_name):
            log.warning("single-instance listener failed: %s", server.errorString())
            return

        def accept():
            while server.hasPendingConnections():
                conn = server.nextPendingConnection()

                def read(conn=conn):
                    if ACTIVATE.strip() in bytes(conn.readAll()):
                        on_activate()
                    conn.disconnectFromServer()

                conn.readyRead.connect(read)
                conn.disconnected.connect(conn.deleteLater)
                if conn.bytesAvailable():
                    read()

        server.newConnection.connect(accept)
        self._server = server

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._lock.unlock()
