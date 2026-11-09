import pytest
from PyQt6.QtWidgets import QMessageBox

from taper_tools import update_ui
from taper_tools.update_ui import UpdateController
from taper_tools.updates import Updater

from test_updates import FakeManager


@pytest.fixture
def boxes(monkeypatch):
    """Record message boxes instead of showing them."""
    shown = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a, **k: shown.append(("info", a[2]))
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: shown.append(("warning", a[2]))
    )
    return shown


def controller(manager, before_restart=lambda: True, factory=None):
    notes = []
    c = UpdateController(
        Updater(factory or (lambda: manager)),
        lambda title, msg: notes.append((title, msg)),
        before_restart,
        threaded=False,
    )
    return c, notes


def test_startup_check_is_silent_when_up_to_date(qapp, boxes):
    c, notes = controller(FakeManager())
    c.check()
    assert not boxes and not notes and c.pending is None
    assert not c.restart_action.isVisible()


def test_startup_check_downloads_and_offers_restart(qapp, boxes):
    m = FakeManager("0.6.0")
    c, notes = controller(m)
    c.check()
    assert m.downloaded is not None
    assert c.pending.version == "0.6.0"
    assert c.restart_action.isVisible() and "0.6.0" in c.restart_action.text()
    assert notes and notes[0][0] == "Update Ready"
    assert not boxes


def test_restart_action_restarts(qapp, boxes):
    m = FakeManager("0.6.0")
    c, _ = controller(m)
    c.check()
    c.restart_action.trigger()
    assert m.applied is c.pending.info


def test_restart_is_cancelled_by_before_restart(qapp, boxes):
    m = FakeManager("0.6.0")
    c, _ = controller(m, before_restart=lambda: False)
    c.check()
    assert c.restart_now() is False
    assert m.applied is None and c.pending is not None


def test_manual_check_restart_now_and_later(qapp, boxes, monkeypatch):
    answers = []
    monkeypatch.setattr(update_ui, "ask_restart", lambda v: answers.pop(0))

    m = FakeManager("0.6.0")
    c, notes = controller(m)
    answers.append(False)  # Later
    c.check(manual=True)
    assert m.applied is None and not notes
    assert c.restart_action.isVisible()

    answers.append(True)  # a second manual check offers it again: Restart Now
    c.check(manual=True)
    assert m.applied is c.pending.info


def test_manual_check_reports_every_outcome(qapp, boxes):
    controller(FakeManager())[0].check(manual=True)
    assert boxes[-1][0] == "info" and "latest version" in boxes[-1][1]

    controller(FakeManager(fail="offline"))[0].check(manual=True)
    assert boxes[-1][0] == "warning" and "offline" in boxes[-1][1]

    controller(None, factory=Updater._velopack_manager)[0].check(manual=True)
    assert boxes[-1][0] == "info" and "installed app" in boxes[-1][1]


def test_startup_check_failure_is_silent(qapp, boxes):
    c, notes = controller(FakeManager(fail="offline"))
    c.check()
    assert not boxes and not notes and not c.busy


def test_manual_check_during_running_check_is_reported(qapp, boxes):
    c, notes = controller(FakeManager())
    c.busy = True  # a start-up check is running
    c.check(manual=True)
    assert c.report and not boxes
    c._ready.emit(None, False)  # the running check finishes
    assert boxes and "latest version" in boxes[-1][1]
    assert not c.report and not c.busy
