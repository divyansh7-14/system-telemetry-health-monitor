# Implementation Plan — `sysmon-agent`

Requirements live in [PRD.md](PRD.md). We build one phase at a time. A phase is **done** only when
every box in its "Verify" list is ticked; then we commit, push, confirm CI is green, and only then
start the next phase.

## Tech stack (decided)

| Need | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Allowed by the brief; fastest to build well in 6 days |
| OS metrics | `psutil` ≥ 5.9 | De-facto cross-platform library for CPU, memory, disk, network and processes; works on Windows without admin |
| Host facts | stdlib `platform`, `socket` | No extra dependency |
| Config | `PyYAML` (`safe_load`) + stdlib `json` | Brief asks for `config.json` or `.yaml` — we support both |
| CLI | stdlib `argparse` | No dependency, built-in `--help` |
| Logging & rotation | stdlib `logging` + `RotatingFileHandler` | Size-based rotation the brief asks for |
| Scheduling / shutdown | `time.monotonic`, `threading.Event`, `signal` | Drift-free, interruptible sleep |
| Tests / lint | `pytest`, `ruff` | Already configured |
| CI | GitHub Actions, Windows + Ubuntu × Py 3.10/3.12 | Already configured |

## Timeline (today: Thu 22 Oct)

| Phase | Scope | Target |
|---|---|---|
| 0 | Skeleton, packaging, CI | ✅ done |
| 1 | Host discovery + baseline CPU/RAM/disk | ✅ done — verified 22 Oct (ruff clean, 24 tests pass, live run OK) |
| 2 | Polling loop, top processes, network, `metrics.jsonl` | Thu 22 – Fri 23 Oct |
| 3 | Config loader, rules engine, statuses, `alerts.log` | Sat 24 Oct |
| 4 | Log rotation, graceful shutdown, error handling, lock file | Sun 25 Oct |
| 5 | Windows demo, `samples/`, full README, `v1.0.0` tag | Mon 26 – Tue 27 Oct |
| — | Buffer / review | Wed 28 Oct (deadline) |

---

## Phase 0 — Skeleton ✅
Package layout, `pyproject.toml`, requirements, ruff/pytest config, CI matrix, MIT licence, stub README.

## Phase 1 — Host discovery & baseline stats ✅  *(Brief task 1)*
Built: `collectors/host.py`, `collectors/resources.py` (with `CpuSampler` priming), `models.py`,
`reporting.py`, full CLI flag set with range validation, `--once`, writable-output-dir check (exit 2).

Verify (all passed 22 Oct):
- [x] `ruff check .` clean, `pytest` 24/24
- [x] `python -m sysmon --once` prints host + resources and writes `host_info.json`
- [x] Disk filter skips CD-ROM / squashfs / unreadable drives, logs each once
- [x] Windows 11 detected from build number

---

## Phase 2 — Continuous polling & process inspection  *(Brief task 2)*

**Build**
1. `collectors/processes.py`
   - `ProcessSampler` using `psutil.process_iter(["pid","name","username","memory_info","memory_percent"], ad_value=None)`; psutil caches `Process` objects between calls so `cpu_percent()` measures the whole interval.
   - First cycle primes every process; values from the priming call are discarded.
   - Normalise CPU by logical core count; skip pid 0 / "System Idle Process"; `username=None` when access is denied.
   - Return `top_by_cpu` and `top_by_memory` (N each).
2. `collectors/network.py`
   - `net_if_addrs()` → per interface IPv4, IPv6, MAC (`psutil.AF_LINK`), `net_if_stats()` → up/down.
   - `NetworkSampler` keeps the previous `net_io_counters()` + timestamp to compute `sent_bps` / `recv_bps`; negative delta (counter reset) → rate 0.
   - Loopback and down interfaces kept but flagged, so IP/MAC of the real adapter is easy to find.
3. `models.py`: `ProcessInfo`, `InterfaceInfo`, `NetworkStats`, `Snapshot`.
4. `agent.py`: `Agent.run()` loop
   - `next_tick = monotonic() + interval`; `stop_event.wait(timeout=next_tick - monotonic())`.
   - If a cycle overruns, log a warning and schedule from now (no burst of catch-up cycles).
   - Each collector wrapped in try/except → error string into `snapshot.errors`, other collectors still run.
   - Appends one JSON line per cycle to `metrics.jsonl` (plain file append in this phase; rotation added in Phase 4) and prints a 3-line console summary.
5. `cli.py`: without `--once`, start the loop; Ctrl+C stops it cleanly (full signal work in Phase 4).

**Tests**
- Fake `process_iter` with a vanishing process, an access-denied process, pid 0 → only valid ones ranked, correct order, N respected.
- CPU normalisation (200 % on 2 cores → 100 %); first cycle priming.
- Network rate maths, counter reset, MAC extraction from a fake `AF_LINK` entry.
- Loop: injected fake clock + collectors, `max_cycles=3` → 3 JSON lines, valid JSON, cycle numbers 1–3; a raising collector → `errors` populated, loop continues.

**Verify**
- [ ] ruff clean, all tests pass locally and on CI (Windows + Ubuntu)
- [ ] `python -m sysmon --interval 10` runs ≥ 3 cycles on Windows; `metrics.jsonl` has one valid line per cycle
- [ ] Top processes look like Task Manager (start a busy loop → it appears at the top with ~100/cores %)
- [ ] IP and MAC match `ipconfig /all`; bytes rate rises while downloading something
- [ ] Ctrl+C stops within 1 s, no traceback

---

## Phase 3 — Health rules & alert engine  *(Brief task 3)*

**Build**
1. `config.py`
   - `load_config(path)`: `.yaml/.yml` → `yaml.safe_load`, `.json` → `json.load`, other → error.
   - Validate: numbers in 0–100, `warning` must be "less severe" than `critical` (below it for `*_max`, above it for `*_min_free`), interval 10–30, unknown keys → warning.
   - Bare number → critical only. Merge: defaults ← file ← CLI flags.
   - `ConfigError` → message on stderr, exit 3.
2. `config/config.yaml` with the defaults from PRD §7, plus `config/config.example.json`.
3. `rules.py`
   - `evaluate(snapshot, thresholds) -> list[Check]` — pure function; `disk_min_free` makes one check per disk, process rules one check per offending process.
   - `AlertState` keyed by `(rule, target)`: consecutive counter, current status, cycles in status → applies `consecutive`, `recovery_margin`, `repeat_every`, and returns the events to log.
   - Overall status = worst check.
4. Alert writer → `alerts.log` in the PRD §7 format; status added to `metrics.jsonl` and console (`[OK]`, `[WARNING]`, `[CRITICAL]`).

**Tests**
- Config: valid YAML and JSON give identical results; each kind of invalid file → `ConfigError` with a helpful message; CLI overrides file.
- Rules: boundary values (exactly 85 vs 85.1), direction of `disk_min_free`, worst-of status.
- Anti-flapping table test: sequence 80, 90, 84, 90, 90, 70 with consecutive=2 → exact expected event list.
- Repeat line after `repeat_every` cycles; recovery only below `limit - margin`.

**Verify**
- [ ] ruff + tests + CI green
- [ ] Run with `--threshold-config config/config.yaml`; with a low test config (`cpu_max: 5`) WARNING/CRITICAL lines appear in `alerts.log` within 2 cycles; when the load stops, an OK recovery line follows
- [ ] Broken config (typo, `critical < warning`, bad YAML) → one clear sentence and exit code 3
- [ ] No config file → runs on defaults and says so in `agent.log`

---

## Phase 4 — Log management, shutdown, error handling  *(Brief task 4)*

**Build**
1. `logs.py`
   - `RotatingFileHandler(maxBytes=max_log_mb*1MB, backupCount, encoding="utf-8", delay=True)` for `agent.log`, `alerts.log`, `metrics.jsonl` (metrics via a dedicated logger with a bare `%(message)s` formatter).
   - Override `handleError` so a failed rollover (Windows *WinError 32*) or full disk is reported once to stderr/agent.log and the agent keeps running.
   - Single-instance lock: `sysmon.lock` holding the PID, created with `O_EXCL`; stale lock (PID not running) is replaced; live lock → exit 4.
2. Signals: SIGINT always; SIGBREAK on Windows; SIGTERM on Linux. Handler only sets `stop_event`. `finally:` writes "agent stopped after N cycles", closes handlers, removes the lock.
3. Error audit: wrap `main()` so an unexpected exception is logged with traceback to `agent.log` and exits 1; permission errors on disks/processes stay quiet-but-logged-once.

**Tests**
- Rotation: tiny `max_log_mb` → backup files appear, count never exceeds `backup_count + 1`.
- Lock file: second agent → exit 4; stale lock is taken over.
- Shutdown: setting the stop event mid-wait ends the loop and closes files; handler raising during write doesn't crash the loop.
- Exit codes 0/1/2/3/4 each covered.

**Verify**
- [ ] ruff + tests + CI green
- [ ] Windows: `--max-log-mb 0.05` for 5 minutes → `.1 .2 .3` backups, never more
- [ ] Ctrl+C and Ctrl+Break both stop cleanly with the final log line; lock file removed
- [ ] Starting a second agent on the same `--output-dir` refuses with a clear message
- [ ] Read-only output folder → exit 2 with a clear message

---

## Phase 5 — Demo, samples, documentation, release

**Build**
1. Windows demo run (15–20 min) with a demo config that triggers alerts: CPU load script for WARNING→CRITICAL→OK; copy outputs into `samples/`.
2. `scripts/cpu_burn.py` — small helper to create load for the demo (stops on its own after N seconds).
3. README: overview, features mapped to the brief, install, quick start (Windows PowerShell + Linux), all flags, config reference, output formats with real excerpts, running in background (`pythonw` / Task Scheduler), architecture diagram, testing, limitations, licence.
4. Bump version to 1.0.0, tag `v1.0.0`, GitHub release notes.

**Verify (final acceptance)**
- [ ] Fresh clone on Windows → follow README word for word → it runs
- [ ] Every PRD requirement ticked in the table below
- [ ] `samples/` contains host_info.json, metrics.jsonl, alerts.log (WARNING + CRITICAL + recovery), agent.log, console output
- [ ] CI green, tag pushed, repo public, regular commit history

## Requirement tracker

| ID | Phase | Status |
|---|---|---|
| FR-1, FR-2 | 1 | ✅ |
| FR-3 … FR-6 | 2 | ☐ |
| FR-7 … FR-11 | 3 | ☐ |
| FR-12 | 1 (flags) / 3 (precedence) | ◐ |
| FR-13 … FR-16 | 4 | ☐ |
| FR-17, FR-18 | 5 | ☐ |
| NFR-1 … NFR-5 | checked in every phase | ◐ |

## Working agreement
- Work directly on `main` in small commits with clear messages; one phase is closed before the next starts.
- Claude builds and tests in a Linux sandbox; CI covers Windows. The **Windows manual checks are done by the repo owner** on the Windows laptop and reported back before the phase is closed.
- Nothing is marked done on "it should work" — only on a test or an observed run.
