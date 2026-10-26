import json
import os

import psutil
import pytest

from sysmon.instance import AlreadyRunning, InstanceLock, read_owner, request_stop


def fake_lock(folder, pid, create_time):
    (folder / "sysmon.lock").write_text(
        json.dumps({"pid": pid, "create_time": create_time, "started": "earlier"}), encoding="utf-8"
    )


def test_acquire_and_release(tmp_path):
    lock = InstanceLock(tmp_path)
    lock.acquire()
    owner = read_owner(tmp_path)
    assert owner is not None and owner.pid == os.getpid()
    lock.release()
    assert not (tmp_path / "sysmon.lock").exists()


def test_live_other_process_blocks(tmp_path):
    parent = psutil.Process(os.getppid())
    fake_lock(tmp_path, parent.pid, parent.create_time())
    with pytest.raises(AlreadyRunning) as exc:
        InstanceLock(tmp_path).acquire()
    assert exc.value.pid == parent.pid


def test_dead_process_lock_is_taken_over(tmp_path):
    fake_lock(tmp_path, 999_999_9, 1.0)  # no such PID
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert lock.took_over is not None and lock.took_over.pid == 9999999
    assert read_owner(tmp_path).pid == os.getpid()


def test_reused_pid_lock_is_taken_over(tmp_path):
    """The PID is alive but belongs to another process started at a different time."""
    parent = psutil.Process(os.getppid())
    fake_lock(tmp_path, parent.pid, parent.create_time() - 3600)
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert lock.took_over.pid == parent.pid


def test_corrupt_lock_is_taken_over(tmp_path):
    (tmp_path / "sysmon.lock").write_text("garbage{", encoding="utf-8")
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert read_owner(tmp_path).pid == os.getpid()


def test_leftover_stop_request_is_cleared_on_start(tmp_path):
    (tmp_path / "sysmon.stop").write_text("old", encoding="utf-8")
    lock = InstanceLock(tmp_path)
    lock.acquire()
    assert not lock.stop_requested()


def test_release_does_not_delete_someone_elses_lock(tmp_path):
    lock = InstanceLock(tmp_path)
    lock.acquire()
    fake_lock(tmp_path, 4242, 1.0)  # replaced meanwhile
    lock.release()
    assert (tmp_path / "sysmon.lock").exists()


def test_request_stop_not_running(tmp_path):
    assert request_stop(tmp_path, timeout=1)[0] == "not-running"
    assert not (tmp_path / "sysmon.stop").exists()


def test_stop_check_ends_the_agent_loop(tmp_path, monkeypatch):
    """The agent polls stop_check every wait slice and stops with reason 'sysmon --stop'."""
    import threading
    import time

    from sysmon import agent as agent_mod
    from sysmon.agent import Agent

    monkeypatch.setattr(agent_mod, "WARMUP_SECONDS", 0.0)
    lock = InstanceLock(tmp_path)
    lock.acquire()
    a = Agent(tmp_path, interval=30, top_n=2, stop_check=lock.stop_requested)
    threading.Timer(0.6, lambda: (tmp_path / "sysmon.stop").write_text("stop")).start()
    t0 = time.monotonic()
    assert a.run() == 1
    assert time.monotonic() - t0 < 2.0 and a.stop_reason == "sysmon --stop"
    lock.release()
    assert not (tmp_path / "sysmon.stop").exists()
