from types import SimpleNamespace

import pytest

from taper_tools._version import __version__
from taper_tools.updates import UpdateError, Updater


class FakeManager:
    def __init__(self, target=None, fail=None):
        self.target, self.fail = target, fail
        self.downloaded = self.applied = None

    def get_current_version(self):
        return "0.5.0"

    def check_for_updates(self):
        if self.fail:
            raise RuntimeError(self.fail)
        if self.target is None:
            return None
        return SimpleNamespace(
            TargetFullRelease=SimpleNamespace(Version=self.target, NotesMarkdown="notes")
        )

    def download_updates(self, info):
        self.downloaded = info

    def apply_updates_and_restart(self, info):
        self.applied = info


def test_dev_checkout_has_no_updates():
    u = Updater()  # not frozen: no velopack manager
    assert not u.available()
    assert u.current_version() == __version__
    with pytest.raises(UpdateError, match="installed app"):
        u.check()


def test_check_download_and_restart():
    m = FakeManager("0.6.0")
    u = Updater(lambda: m)
    assert u.available() and u.current_version() == "0.5.0"
    up = u.check()
    assert up.version == "0.6.0" and up.notes == "notes"
    u.download(up)
    assert m.downloaded is up.info
    u.restart_into(up)
    assert m.applied is up.info


def test_no_update_and_network_errors():
    assert Updater(lambda: FakeManager()).check() is None
    with pytest.raises(UpdateError, match="Could not check"):
        Updater(lambda: FakeManager(fail="offline")).check()


def test_manager_that_cannot_start_is_reported():
    def boom():
        raise RuntimeError("not installed")

    u = Updater(boom)
    assert not u.available()
    with pytest.raises(UpdateError, match="not installed"):
        u.check()


def test_download_and_install_errors_become_update_error():
    class Broken(FakeManager):
        def download_updates(self, info):
            raise OSError("disk full")

        def apply_updates_and_restart(self, info):
            raise OSError("locked")

    u = Updater(lambda: Broken("0.6.0"))
    up = u.check()
    with pytest.raises(UpdateError, match="download"):
        u.download(up)
    with pytest.raises(UpdateError, match="install"):
        u.restart_into(up)


def test_install_hooks_are_a_no_op_when_not_frozen(monkeypatch):
    import sys

    from taper_tools import updates

    monkeypatch.delattr(sys, "frozen", raising=False)
    updates.run_install_hooks()  # must not import velopack or exit
