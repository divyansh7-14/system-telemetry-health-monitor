from collections import namedtuple

import psutil
import pytest

from sysmon.collectors.processes import ProcessSampler

Mem = namedtuple("Mem", "rss vms")
MB = 1024**2


class FakeProc:
    def __init__(self, pid, name, cpu, rss, user="alice", user_error=None):
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": name,
            "cpu_percent": cpu,
            "memory_info": Mem(rss, rss) if rss is not None else None,
            "memory_percent": rss / (16 * 1024 * MB) * 100 if rss is not None else None,
        }
        self._user, self._user_error = user, user_error
        self.username_calls = 0

    def username(self):
        self.username_calls += 1
        if self._user_error:
            raise self._user_error
        return self._user


@pytest.fixture
def procs(monkeypatch):
    table = [
        FakeProc(0, "System Idle Process", 380.0, 8 * 1024),  # must be ignored
        FakeProc(4, "System", 2.0, 1 * MB, user_error=psutil.AccessDenied(4)),
        FakeProc(100, "chrome.exe", 120.0, 900 * MB),
        FakeProc(200, "python.exe", 200.0, 50 * MB),  # 200% on 4 cores -> 50% of machine
        FakeProc(300, "Code.exe", 10.0, 1500 * MB),
        FakeProc(400, "locked.exe", None, None),  # AccessDenied on everything -> ad_value None
        FakeProc(500, "gone.exe", 30.0, 20 * MB, user_error=psutil.NoSuchProcess(500)),
    ]
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None, ad_value=None: iter(table))
    return {p.pid: p for p in table}


def test_ranks_by_cpu_and_normalises_to_whole_machine(procs):
    top = ProcessSampler(top_n=3).sample()
    assert [p.pid for p in top.by_cpu] == [200, 100, 500]
    assert top.by_cpu[0].cpu_percent == 50.0  # 200 / 4 cores
    assert top.by_cpu[1].cpu_percent == 30.0


def test_ranks_by_memory(procs):
    top = ProcessSampler(top_n=2).sample()
    assert [p.pid for p in top.by_memory] == [300, 100]
    assert top.by_memory[0].memory_rss_bytes == 1500 * MB


def test_idle_process_excluded_and_counted_correctly(procs):
    top = ProcessSampler(top_n=10).sample()
    assert 0 not in [p.pid for p in top.by_cpu + top.by_memory]
    assert top.total_processes == 6


def test_access_denied_and_vanished_processes_do_not_crash(procs):
    top = ProcessSampler(top_n=10).sample()
    by_pid = {p.pid: p for p in top.by_cpu}
    assert by_pid[4].username is None  # AccessDenied on username
    assert by_pid[500].username is None  # exited after ranking
    assert by_pid[500].cpu_percent == 7.5  # but its measured numbers are kept
    assert by_pid[400].cpu_percent == 0.0 and by_pid[400].memory_rss_bytes == 0
    assert by_pid[100].username == "alice"


def test_username_only_looked_up_for_top_processes(procs):
    ProcessSampler(top_n=1).sample()
    looked_up = {pid for pid, p in procs.items() if p.username_calls}
    assert looked_up == {200, 300}  # top CPU + top memory only


def test_top_n_validated():
    with pytest.raises(ValueError):
        ProcessSampler(top_n=0)


def test_real_machine_smoke():
    """Runs against the real OS: must not raise and must find this test process."""
    import os
    import time

    sampler = ProcessSampler(top_n=500)
    sampler.prime()
    time.sleep(0.2)
    top = sampler.sample()
    assert top.total_processes > 1
    assert os.getpid() in [p.pid for p in top.by_memory]
    assert all(0.0 <= p.cpu_percent <= 100.0 for p in top.by_cpu)
