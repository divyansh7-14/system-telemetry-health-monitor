"""Writing results: JSON files and human-readable console output."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sysmon.collectors.host import format_uptime
from sysmon.models import HostInfo, ResourceStats


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename, so a crash never leaves a half file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def format_host_info(h: HostInfo) -> str:
    cores = f"{h.cpu_physical_cores or '?'} physical / {h.cpu_logical_cores or '?'} logical"
    return "\n".join(
        [
            "== Host ==",
            f"  Hostname      : {h.hostname}",
            f"  OS            : {h.os_name} {h.os_release} ({h.os_version})",
            f"  Architecture  : {h.architecture}",
            f"  CPU cores     : {cores}",
            f"  Total RAM     : {human_bytes(h.total_memory_bytes)}",
            f"  Booted        : {h.boot_time}",
            f"  Uptime        : {format_uptime(h.uptime_seconds)}",
        ]
    )


def format_resources(r: ResourceStats) -> str:
    m = r.memory
    lines = [
        "== Resources ==",
        f"  CPU           : {r.cpu.percent:.1f}%  (per core: "
        + ", ".join(f"{p:.0f}%" for p in r.cpu.per_core_percent)
        + ")",
        f"  Memory        : {human_bytes(m.used_bytes)} used of {human_bytes(m.total_bytes)} "
        f"({m.percent:.1f}%), {human_bytes(m.available_bytes)} available",
        f"  Swap          : {m.swap_percent:.1f}% of {human_bytes(m.swap_total_bytes)}",
    ]
    if not r.disks:
        lines.append("  Disks         : none readable")
    for d in r.disks:
        lines.append(
            f"  Disk {d.mountpoint:<9}: {d.used_percent:.1f}% used, "
            f"{human_bytes(d.free_bytes)} free of {human_bytes(d.total_bytes)} "
            f"({d.free_percent:.1f}% free)"
        )
    return "\n".join(lines)
