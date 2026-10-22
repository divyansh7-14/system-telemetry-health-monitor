"""Top CPU and memory consumers (brief task 2).

How per-process CPU is measured
-------------------------------
``Process.cpu_percent()`` reports usage since the previous call *on the same
Process object*; the first call always returns 0.0. ``psutil.process_iter()``
keeps one cached Process object per PID (and drops it if the PID is reused), so
calling it once per cycle measures every process over the whole polling
interval. A process seen for the first time reads 0.0 for that one cycle.

psutil reports per-process CPU where one fully used core = 100 %, so on a
4-core machine the maximum is 400 %. We divide by the logical core count so the
value is a share of the whole machine, the same as Task Manager shows.
"""

from __future__ import annotations

import logging

import psutil

from sysmon.models import ProcessInfo, TopProcesses

log = logging.getLogger(__name__)

# Cheap attributes read for every process. username() is slow on Windows, so it
# is only fetched for the processes that make the top lists.
_ATTRS = ["pid", "name", "cpu_percent", "memory_info", "memory_percent"]
# PID 0 is "System Idle Process" on Windows: its "CPU" is idle time, not load.
_SKIP_PIDS = {0}
# Windows kernel-managed processes started by "System" (PID 4), such as Memory
# Compression, Registry and Secure System, do not expose their name to user-mode
# APIs, so psutil returns "". Label them instead of showing a blank.
_WINDOWS_SYSTEM_PID = 4
PROTECTED_LABEL = "[protected system process]"
UNKNOWN_LABEL = "[unknown]"


class ProcessSampler:
    def __init__(self, top_n: int = 5) -> None:
        if top_n < 1:
            raise ValueError("top_n must be >= 1")
        self.top_n = top_n
        self._cores = psutil.cpu_count(logical=True) or 1

    def prime(self) -> None:
        """Start every process's CPU counter so the next sample has a real window."""
        for _ in psutil.process_iter(["cpu_percent"], ad_value=None):
            pass

    def sample(self) -> TopProcesses:
        rows: list[tuple[psutil.Process, dict]] = []
        for proc in psutil.process_iter(_ATTRS, ad_value=None):
            info = proc.info
            if info.get("pid") in _SKIP_PIDS:
                continue
            rows.append((proc, info))

        def cpu_key(row: tuple[psutil.Process, dict]) -> tuple[float, int]:
            return (-(row[1].get("cpu_percent") or 0.0), row[1]["pid"])

        def mem_key(row: tuple[psutil.Process, dict]) -> tuple[int, int]:
            mi = row[1].get("memory_info")
            return (-(mi.rss if mi else 0), row[1]["pid"])

        by_cpu = [self._to_info(r) for r in sorted(rows, key=cpu_key)[: self.top_n]]
        by_mem = [self._to_info(r) for r in sorted(rows, key=mem_key)[: self.top_n]]
        return TopProcesses(by_cpu=by_cpu, by_memory=by_mem, total_processes=len(rows))

    def _to_info(self, row: tuple[psutil.Process, dict]) -> ProcessInfo:
        proc, info = row
        mi = info.get("memory_info")
        try:
            username = proc.username()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            username = None
        except psutil.NoSuchProcess:
            username = None  # exited after it was ranked; keep the numbers we measured
        except OSError as exc:  # rare Windows lookup failures (e.g. deleted account SID)
            log.debug("username lookup failed for pid %s: %s", info["pid"], exc)
            username = None
        return ProcessInfo(
            pid=info["pid"],
            name=info.get("name") or _label_unnamed(proc),
            username=username,
            cpu_percent=round((info.get("cpu_percent") or 0.0) / self._cores, 1),
            memory_rss_bytes=mi.rss if mi else 0,
            memory_percent=round(info.get("memory_percent") or 0.0, 2),
        )


def _label_unnamed(proc: psutil.Process) -> str:
    try:
        parent = proc.ppid()
    except psutil.Error:
        return UNKNOWN_LABEL
    return PROTECTED_LABEL if parent == _WINDOWS_SYSTEM_PID else UNKNOWN_LABEL
