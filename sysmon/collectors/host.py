"""Host discovery: static OS and hardware facts (brief task 1)."""

from __future__ import annotations

import platform
import socket
import sys
import time
from datetime import datetime

import psutil

from sysmon.models import HostInfo


def _windows_name(release: str) -> str:
    """Windows 11 still reports release '10' on older Pythons; use the build number."""
    try:
        build = sys.getwindowsversion().build  # type: ignore[attr-defined]
    except AttributeError:
        return release
    return "11" if release == "10" and build >= 22000 else release


def collect_host_info(now: float | None = None) -> HostInfo:
    """Return static host details. ``now`` is injectable for tests."""
    now = time.time() if now is None else now
    system = platform.system() or "Unknown"
    release = platform.release()
    if system == "Windows":
        release = _windows_name(release)

    boot = psutil.boot_time()
    return HostInfo(
        hostname=socket.gethostname(),
        os_name=system,
        os_release=release,
        os_version=platform.version(),
        platform=platform.platform(),
        architecture=platform.machine() or "unknown",
        python_version=platform.python_version(),
        cpu_physical_cores=psutil.cpu_count(logical=False),
        cpu_logical_cores=psutil.cpu_count(logical=True),
        total_memory_bytes=psutil.virtual_memory().total,
        boot_time=datetime.fromtimestamp(boot).astimezone().isoformat(timespec="seconds"),
        uptime_seconds=max(0, int(now - boot)),
    )


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    parts = [f"{days}d"] if days else []
    parts.append(f"{hours}h {minutes}m")
    return " ".join(parts)
