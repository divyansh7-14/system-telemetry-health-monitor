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
| 1 | Host discovery + baseline CPU/RAM/disk | ✅ done — re-reviewed 22 Oct, verified on Windows Server 2025 + Linux, Py 3.10/3.12/3.14 (35 unit tests, 21 e2e checks) |
| 2 | Polling loop, top processes, network, `metrics.jsonl` | ✅ done 22 Oct — verified on Windows + Linux (68 unit tests, 43 e2e checks on Windows) |
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
- [x] `ruff check .` clean, `pytest` 35/35 on Windows + Linux, Py 3.10 + 3.12 + 3.14
- [x] e2e on real Windows (21 checks): OS name/build, RAM, memory %, page file, physical + logical cores, uptime, every fixed drive, C: total/free all match CIM; CPU reads 100% with every core busy; exit codes 2 and 3 correct
- [x] `pip install .` works and the `sysmon` command runs (tested in CI)
- [x] e2e found and fixed a real bug: Windows Server 2025 was reported as "Windows 11" (Py 3.10) / "2025Server" (Py 3.12)
- [x] `python -m sysmon --once` prints host + resources and writes `host_info.json`
- [x] Disk filter skips CD-ROM / squashfs / unreadable drives, logs each once
- [x] Windows 10 / 11 / Server named correctly from build + product type

Re-review 22 Oct (evening) — fixed:
- Exit code 2 was used for both bad flags and an unwritable output dir → output errors now 3 (config 4, lock 5)
- Disk-full / locked `host_info.json` crashed with a traceback → clear message, exit 3
- Ctrl+C during `--once` printed a traceback → clean stop, exit 0
- Windows load average (psutil emulation) is zeros for the first ~5 s → reported as None until warmed up
- Type hint pointed at `psutil._common.sdiskpart`, removed in psutil 7
- `pyproject.toml`: deprecated license table → SPDX string; version defined once (`sysmon.__version__`)

---

## Phase 2 — Continuous polling & process inspection ✅  *(Brief task 2)*

Built:
1. `collectors/processes.py` — `ProcessSampler`: one `process_iter` pass per cycle (psutil caches a
   Process object per PID, so per-process CPU covers the whole interval); CPU normalised to the whole
   machine; PID 0 skipped; AccessDenied / NoSuchProcess never raise; `username()` (slow on Windows)
   only fetched for the top lists; unreadable Windows kernel process names labelled.
2. `collectors/network.py` — `NetworkSampler`: per-interface IPv4/IPv6/MAC/up/speed/bytes; primary
   interface found via the default route (UDP connect, no packets sent); totals and rates counted
   on that interface; rate is None on first cycle, counter reset, or interface change.
3. `agent.py` — `Agent.run()`: monotonic tick grid (drift-free), overrun skips missed ticks,
   0.25 s wait slices, per-collector try/except into `errors`, one JSON line per cycle to
   `metrics.jsonl` (file opened per write, write failures logged once and never fatal),
   Ctrl+C / Ctrl+Break / SIGTERM handlers set a stop event (second Ctrl+C forces exit).
   *Signal handling was pulled forward from Phase 4 so the Phase 2 stop check is meaningful on Windows.*
4. CLI — continuous mode, `--once` = one full snapshot, `--interval` / `--top-n` wired, console
   summary per cycle (ASCII only, never crashes on unusual characters), not-yet-used flags warn.

Tests: 68 unit tests (processes 8, network 13, agent 10, CLI 14, plus Phase 1).

Verify (all passed 22 Oct, CI run on commit `89d5cfa`, 6/6 jobs):
- [x] ruff clean, all tests pass on Windows + Ubuntu, Py 3.10 / 3.12 / 3.14
- [x] e2e: ≥ 4 cycles, consecutive cycle numbers, exactly 10 s apart, no collector errors, each cycle < 30 ms
- [x] e2e: busy-loop PID is #1 by CPU every cycle at 24.9 % (= 100 / 4 cores); 1 GiB memory hog is #1 by memory, RSS within 10 %
- [x] e2e: primary interface, IPv4, MAC, every IPv4 address and per-interface byte counters match `Get-NetRoute` / `Get-NetIPAddress` / `Get-NetAdapter(Statistics)` (Windows) and `ip -j` / `/sys/class/net` (Linux) exactly
- [x] e2e: an 18.9 MB download appears once in the totals and in the rate; rates × time equal the counter change within 0.01 %
- [x] e2e: Ctrl+Break (Windows) / Ctrl+C (Linux) stops within 0.25 s, exit 0, no traceback; SIGINT path also unit-tested on Windows
- [x] e2e: every running process has a name or label
- [x] Agent overhead: ~1 % of one core, 27–34 MB RAM on Windows (NFR-1)

Found and fixed by the Windows/Linux runs:
- Windows kernel process (PID 92, Memory Compression / Registry / Secure System) has an empty name → labelled
- Totals summed all adapters → mirrored traffic counted twice (18.9 MB showed as 38.8 MB) → counted on default-route interface

Full re-review of Phases 0–2 (22 Oct, night) — fixed:
- Laptop sleep: Windows' monotonic clock runs during sleep, so on wake the agent logged "cycle took longer than the interval; skipping 360 ticks". Now detected as a pause, schedule re-anchored, logged honestly.
- Console disappearing (pipe reader exits, terminal closed) raised from `print` and stopped the agent. Console output is now best-effort; stdout is pointed at the null device on failure, monitoring continues.
- A run killed mid-write left a partial last line that the next run appended to. The next run now starts on a fresh line.
- e2e harness: GitHub truncates each annotation at ~4 KB, hiding late lines → failures now reported first and long reports split; a crashing section is now reported as a FAIL instead of killing the report.

New verification (CI run on commit `148e79e`, 6/6 jobs, 55/55 checks on each Windows job, 41/41 on Linux):
- [x] CI's Windows user is an administrator, so a **real standard (non-admin) user** is now created and the agent run as that user: memory measured for 100 % of processes, top-memory list identical to the admin view, CPU of an admin-owned process seen correctly (~25 %), every process named, `sysmon --once` clean
- [x] **Background mode**: `pythonw` launched fully detached (verified `sys.stdout is None`) runs `--once` and continuous polling correctly
- [x] Console closed mid-run (Windows and Linux): agent warns once, keeps writing metrics, exits 0 on Ctrl+Break / Ctrl+C
- [x] Linux CI runs as a normal user (uid 1001), not root

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
2. Signals (handlers already built in Phase 2): `finally:` writes "agent stopped after N cycles", closes handlers, removes the lock.
3. **Graceful stop for a background agent** (found in the Phase 0–2 re-review): an agent started with `pythonw` or Task Scheduler has no console, so Ctrl+C is impossible and killing it skips the clean shutdown. Add `sysmon --stop --output-dir DIR`, which asks the agent owning that directory's lock to stop (stop-request file checked every wait slice), waits for it, and reports the result.
4. Error audit: wrap `main()` so an unexpected exception is logged with traceback to `agent.log` and exits 1; permission errors on disks/processes stay quiet-but-logged-once.

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
- [ ] e2e: a detached `pythonw` agent is stopped with `sysmon --stop`: final log line written, lock removed, exit 0

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
| FR-3 … FR-6 | 2 | ✅ |
| FR-7 … FR-11 | 3 | ☐ |
| FR-12 | 1 (flags) / 2 (`--interval`, `--top-n`, `--once`) / 3 (precedence) | ◐ |
| FR-13 … FR-16 | 4 | ☐ |
| FR-17, FR-18 | 5 | ☐ |
| NFR-1 … NFR-5 | checked in every phase | ◐ |

## Working agreement
- Work directly on `main` in small commits with clear messages; one phase is closed before the next starts.
- Claude runs **every** check: unit tests and e2e locally on Linux, then the same suite on GitHub's real Windows machines through CI, reading the results back before closing a phase. No manual checks are left to the repo owner.
- CI runners are Windows Server 2025, not a laptop; the code paths (psutil, CIM, signals, file locking) are the same. The final `samples/` are produced from a CI Windows run.
- Nothing is marked done on "it should work" — only on a test or an observed run.
