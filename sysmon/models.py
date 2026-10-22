"""Plain data containers shared by collectors, rules and reporters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class HostInfo:
    """Static facts about the machine, collected once at start-up."""

    hostname: str
    os_name: str
    os_release: str
    os_version: str
    platform: str
    architecture: str
    python_version: str
    cpu_physical_cores: int | None
    cpu_logical_cores: int | None
    total_memory_bytes: int
    boot_time: str  # ISO 8601, local time with offset
    uptime_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CpuStats:
    percent: float
    per_core_percent: list[float]
    load_average: list[float] | None = None  # 1, 5, 15 min; None where unsupported


@dataclass(frozen=True)
class MemoryStats:
    total_bytes: int
    used_bytes: int
    available_bytes: int
    percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float


@dataclass(frozen=True)
class DiskStats:
    device: str
    mountpoint: str
    fstype: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    free_percent: float


@dataclass(frozen=True)
class ResourceStats:
    cpu: CpuStats
    memory: MemoryStats
    disks: list[DiskStats] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
