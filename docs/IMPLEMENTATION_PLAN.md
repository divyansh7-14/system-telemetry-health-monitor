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
| End-to-end checks | `scripts/e2e_check.py` | Runs the real agent and cross-checks it against independent OS tools (PowerShell/CIM on Windows, `/proc`, `df` on Linux) |
| CI | GitHub Actions, Windows + Ubuntu × Py 3.10/3.12 | Runs ruff, pytest and the e2e checks on every push |

## Timeline (today: Thu 22 Oct)

| Phase | Scope | Target |
|---|---|---|
| 0 | Skeleton, packaging, CI | ✅ done |
| 1 | Host discovery + baseline CPU/RAM/disk | ✅ done — verified 22 Oct on Windows Server 2025 + Linux (30 unit tests, 15 e2e checks) |
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
`reporting.py`, full CLI flag set with range validation, `--once`, writable-output-dir check (exit 3).

Verify (all passed 22 Oct):
- [x] `ruff check .` clean, `pytest` 30/30 on Windows + Linux, Py 3.10 + 3.12
- [x] e2e on real Windows: OS name/build, RAM, physical + logical cores, uptime, C: total/free all match CIM; CPU reads 100% with every core busy
- [x] e2e found and fixed a real bug: Windows Server 2025 was reported as "Windows 11" (Py 3.10) / "2025Server" (Py 3.12)
- [x] `python -m sysmon --once` prints host + resources and writes `host_info.json`
- [x] Disk filter skips CD-ROM / squashfs / unreadable drives, logs each once
- [x] Windows 10 / 11 / Server named correctly from build + product type

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
- [ ] e2e: agent runs ≥ 3 cycles on Windows + Linux; `metrics.jsonl` has one valid line per cycle
- [ ] e2e: a known busy-loop PID is #1 by CPU at ~100/cores %; a known memory-hog PID is #1 by memory
- [ ] e2e: IP and MAC match `Get-NetIPAddress` / `Get-NetAdapter` (Windows) and `ip -j addr` (Linux); receive rate rises during a download
- [ ] e2e: interrupting the agent (CTRL_C / SIGINT) stops it within 1 s, exit 0, no traceback

---

## Phase 3 — Health rules & alert engine  *(Brief task 3)*

**Build**
1. `config.py`
   - `load_config(path)`: `.yaml/.yml` → `yaml.safe_load`, `.json` → `json.load`, other → error.
   - Validate: numbers in 0–100, `warning` must be "less severe" than `critical` (below it for `*_max`, above it for `*_min_free`), interval 10–30, unknown keys → warning.
   - Bare number → critical only. Merge: defaults ← file ← CLI flags.
   - `ConfigError` → message on stderr, exit 4.
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
- [ ] e2e: with a test config and a CPU burn, `alerts.log` gets OK→WARNING→CRITICAL then a recovery line after the burn stops
- [ ] e2e: broken configs (typo, `critical < warning`, bad YAML, bad JSON) → one clear sentence and exit 4
- [ ] e2e: no config file → runs on defaults and says so in `agent.log`

---

## Phase 4 — Log management, shutdown, error handling  *(Brief task 4)*

**Build**
1. `logs.py`
   - `RotatingFileHandler(maxBytes=max_log_mb*1MB, backupCount, encoding="utf-8", delay=True)` for `agent.log`, `alerts.log`, `metrics.jsonl` (metrics via a dedicated logger with a bare `%(message)s` formatter).
   - Override `handleError` so a failed rollover (Windows *WinError 32*) or full disk is reported once to stderr/agent.log and the agent keeps running.
   - Single-instance lock: `sysmon.lock` holding the PID, created with `O_EXCL`; stale lock (PID not running) is replaced; live lock → exit 5.
2. Signals: SIGINT always; SIGBREAK on Windows; SIGTERM on Linux. Handler only sets `stop_event`. `finally:` writes "agent stopped after N cycles", closes handlers, removes the lock.
3. Error audit: wrap `main()` so an unexpected exception is logged with traceback to `agent.log` and exits 1; permission errors on disks/processes stay quiet-but-logged-once.

**Tests**
- Rotation: tiny `max_log_mb` → backup files appear, count never exceeds `backup_count + 1`.
- Lock file: second agent → exit 5; stale lock is taken over.
- Shutdown: setting the stop event mid-wait ends the loop and closes files; handler raising during write doesn't crash the loop.
- Exit codes 0/1/2/3/4/5 each covered.

**Verify**
- [ ] ruff + tests + CI green
- [ ] e2e: tiny `--max-log-mb` → `.1 .2 .3` backups appear, never more
- [ ] e2e: CTRL_BREAK_EVENT (Windows) / SIGTERM (Linux) and Ctrl+C all stop cleanly with the final log line; lock file removed
- [ ] e2e: a second agent on the same `--output-dir` exits 5 with a clear message
- [ ] e2e: output folder becomes unwritable mid-run → reported, agent keeps running or exits 3 cleanly

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
- Claude runs **every** check: unit tests and e2e locally on Linux, then the same suite on GitHub's real Windows machines through CI, reading the results back before closing a phase. No manual checks are left to the repo owner.
- CI runners are Windows Server 2025, not a laptop; the code paths (psutil, CIM, signals, file locking) are the same. The final `samples/` are produced from a CI Windows run.
- Nothing is marked done on "it should work" — only on a test or an observed run.
