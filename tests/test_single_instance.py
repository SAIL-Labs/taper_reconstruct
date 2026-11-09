import time
import uuid

import pytest

from taper_tools.single_instance import SingleInstance
from taper_tools.updates import is_install_hook


@pytest.fixture
def key():
    return f"taper-test-{uuid.uuid4().hex[:8]}"


def wait_for(qapp, cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    return cond()


def test_second_copy_is_refused_until_the_first_quits(qapp, tmp_path, key):
    first = SingleInstance(key, tmp_path)
    assert first.acquire()
    second = SingleInstance(key, tmp_path)
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_second_launch_tells_the_running_copy(qapp, tmp_path, key):
    first = SingleInstance(key, tmp_path)
    assert first.acquire()
    seen = []
    first.listen(lambda: seen.append(True))

    second = SingleInstance(key, tmp_path)
    assert not second.acquire()
    assert second.notify_running()
    assert wait_for(qapp, lambda: seen == [True])
    first.release()


def test_notify_without_a_listener_reports_failure(qapp, tmp_path, key):
    first = SingleInstance(key, tmp_path)
    assert first.acquire()  # holds the lock but never listens (hung / still starting)
    second = SingleInstance(key, tmp_path)
    assert not second.acquire()
    assert not second.notify_running(timeout_ms=200)
    first.release()


def test_unwritable_lock_folder_does_not_block_start(qapp, tmp_path, key):
    blocker = tmp_path / "file"
    blocker.write_text("")  # a file where the lock folder should be
    assert SingleInstance(key, blocker / "sub").acquire()


def test_velopack_hook_arguments_are_recognised():
    assert is_install_hook(["--veloapp-install", "0.5.0"])
    assert is_install_hook(["--veloapp-uninstall", "0.5.0"])
    assert not is_install_hook([])
    assert not is_install_hook(["--no-update-check"])
