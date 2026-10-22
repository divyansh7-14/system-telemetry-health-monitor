import platform

from sysmon.collectors import host
from sysmon.collectors.host import collect_host_info, format_uptime


def test_host_info_fields(fake_psutil):
    info = collect_host_info(now=1_000_000.0 + 90_061)  # 1d 1h 1m 1s after boot
    assert info.uptime_seconds == 90_061
    assert info.cpu_physical_cores == 4
    assert info.cpu_logical_cores == 8
    assert info.total_memory_bytes == 16 * 1024**3
    assert info.os_name
    assert info.architecture
    assert "T" in info.boot_time  # ISO 8601


def test_uptime_never_negative(fake_psutil):
    assert collect_host_info(now=0).uptime_seconds == 0


def test_windows_11_detected_from_build(fake_psutil, monkeypatch):
    class V:
        build = 22631

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "release", lambda: "10")
    monkeypatch.setattr(host.sys, "getwindowsversion", lambda: V, raising=False)
    assert collect_host_info().os_release == "11"


def test_format_uptime():
    assert format_uptime(59) == "0h 0m"
    assert format_uptime(3_660) == "1h 1m"
    assert format_uptime(90_061) == "1d 1h 1m"
