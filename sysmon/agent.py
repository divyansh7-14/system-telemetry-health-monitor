"""The polling loop (brief task 2): collect a snapshot every interval and record it.

Design notes
------------
* Drift-free schedule: ticks sit on a fixed grid (start + k * interval) measured
  with a monotonic clock, so slow cycles do not push later ones back and a clock
  change does not break the schedule. If a cycle overruns, missed ticks are
  skipped rather than run back-to-back.
* Interruptible wait: the loop sleeps in short slices on a threading.Event, so a
  stop request (Ctrl+C, Ctrl+Break, SIGTERM, or a test) takes effect within
  ~0.25 s on every OS.
* Fault isolation: each collector runs in its own try/except. A failing collector
  is recorded in the snapshot's ``errors`` list and the others still run. A broken
  console (closed pipe, no console under pythonw) never stops data collection.
* Sleep/resume: on Windows the monotonic clock keeps running while a laptop
  sleeps. If the agent wakes far behind schedule, the schedule is re-anchored
  and the gap is logged as a pause, not blamed on a slow cycle.
"""

from __future__ import annotations

import json
import logging
import math
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sysmon.collectors.network import NetworkSampler
from sysmon.collectors.processes import ProcessSampler
from sysmon.collectors.resources import CpuSampler, collect_disks, collect_memory
from sysmon.models import Snapshot

log = logging.getLogger(__name__)

WAIT_SLICE = 0.25  # seconds; upper bound on how long a stop request can go unnoticed
WARMUP_SECONDS = 1.0  # first cycle measures CPU over this window instead of a full interval
METRICS_FILE = "metrics.jsonl"


class Agent:
    def __init__(
        self,
        out_dir: Path,
        interval: float,
        top_n: int = 5,
        *,
        on_snapshot: Callable[[Snapshot], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.metrics_path = out_dir / METRICS_FILE
        self.interval = interval
        self.on_snapshot = on_snapshot
        self.clock = clock
        self.stop_event = stop_event or threading.Event()
        self.cycles = 0
        self.cpu = CpuSampler()
        self.processes = ProcessSampler(top_n)
        self.network = NetworkSampler(clock=clock)
        self._disk_warned: set[str] = set()
        self._write_failed = False
        self._display_failed = False
        self._checked_tail = False

    # -------------------------------------------------------------- lifecycle
    def stop(self) -> None:
        self.stop_event.set()

    def prime(self) -> None:
        """Start the 'since last call' counters (system CPU, per-process CPU, network)."""
        for name, fn in (
            ("cpu", self.cpu.prime),
            ("processes", self.processes.prime),
            ("network", self.network.sample),
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - priming must never stop the agent
                log.warning("could not prime %s collector: %s", name, exc)

    def run(self, max_cycles: int | None = None) -> int:
        """Poll until stopped (or ``max_cycles`` reached). Returns cycles completed."""
        self.prime()
        if self._wait(self.clock() + WARMUP_SECONDS):
            return self.cycles
        next_tick = self.clock()
        while True:
            late = self.clock() - next_tick
            if late > self.interval:
                log.info(
                    "resumed %.0fs behind schedule (system sleep or paused process); "
                    "schedule re-aligned",
                    late,
                )
                next_tick = self.clock()
            self.run_cycle()
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            next_tick += self.interval
            now = self.clock()
            if now >= next_tick:
                missed = math.floor((now - next_tick) / self.interval) + 1
                log.warning(
                    "cycle %d took longer than the %gs interval; skipping %d tick(s)",
                    self.cycles,
                    self.interval,
                    missed,
                )
                next_tick += missed * self.interval
            if self._wait(next_tick):
                break
        return self.cycles

    def _wait(self, until: float) -> bool:
        """Sleep until ``until`` (monotonic). Returns True if a stop was requested."""
        while True:
            remaining = until - self.clock()
            if remaining <= 0:
                return self.stop_event.is_set()
            if self.stop_event.wait(min(remaining, WAIT_SLICE)):
                return True

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> Snapshot:
        self.cycles += 1
        started = time.perf_counter()
        errors: list[str] = []

        def safe(name: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - one collector must not stop the rest
                log.error(
                    "collector %s failed: %s", name, exc, exc_info=log.isEnabledFor(logging.DEBUG)
                )
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None

        cpu = safe("cpu", self.cpu.sample)
        memory = safe("memory", collect_memory)
        disks = safe("disks", lambda: collect_disks(self._disk_warned))
        network = safe("network", self.network.sample)
        top = safe("processes", self.processes.sample)

        snap = Snapshot(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            cycle=self.cycles,
            duration_ms=round((time.perf_counter() - started) * 1000),
            cpu=cpu,
            memory=memory,
            disks=disks,
            network=network,
            top_processes=top,
            errors=errors,
        )
        self._write(snap)
        if self.on_snapshot:
            try:
                self.on_snapshot(snap)
            except Exception as exc:  # noqa: BLE001 - display is optional, monitoring is not
                if not self._display_failed:
                    log.warning("console output failed (%s); still writing metrics", exc)
                self._display_failed = True
        return snap

    def _write(self, snap: Snapshot) -> None:
        """Append one JSON line. A failed write is reported, never fatal."""
        line = json.dumps(snap.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        if not self._checked_tail:
            # A previous run killed mid-write leaves a partial last line; start on a
            # fresh line so that damage never spreads to this run's first record.
            line = self._newline_if_unterminated() + line
        try:
            # Opened per write: no long-lived handle that would block rotation on Windows.
            with self.metrics_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            if not self._write_failed:
                log.error("cannot write %s: %s (will keep trying)", self.metrics_path, exc)
            self._write_failed = True
        else:
            self._checked_tail = True
            if self._write_failed:
                log.info("writing %s works again", self.metrics_path)
            self._write_failed = False

    def _newline_if_unterminated(self) -> str:
        try:
            with self.metrics_path.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() == 0:
                    return ""
                fh.seek(-1, 2)
                return "" if fh.read(1) == b"\n" else "\n"
        except OSError:
            return ""


# ------------------------------------------------------------------ signals
def install_stop_handlers(agent: Agent) -> Callable[[], None]:
    """Route Ctrl+C, Ctrl+Break (Windows) and SIGTERM to a graceful stop.

    A second Ctrl+C while stopping raises KeyboardInterrupt as an escape hatch.
    Returns a function that restores the previous handlers.
    """
    names = ["SIGINT", "SIGTERM", "SIGBREAK"]
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        if agent.stop_event.is_set() and signum == signal.SIGINT:
            raise KeyboardInterrupt
        log.info("received %s, stopping after the current step", signal.Signals(signum).name)
        agent.stop()

    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not main thread, or not supported here
            continue

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass

    return restore
