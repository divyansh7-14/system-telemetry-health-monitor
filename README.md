# sysmon-agent

Cross-platform Python agent that monitors CPU, RAM, disk, network and processes, checks them against configurable thresholds, and logs OK / WARNING / CRITICAL alerts.

> **Status:** in development — Phase 3 complete: continuous polling of CPU, memory, disk, network and top processes, checked against configurable thresholds with OK / WARNING / CRITICAL alerts in `alerts.log`. Log rotation and background stop arrive in Phase 4. See [docs/PRD.md](docs/PRD.md) and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). Full documentation arrives with v1.0.0.

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/divyansh7-14/system-telemetry-health-monitor.git
cd system-telemetry-health-monitor
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
python -m sysmon --help
python -m sysmon --interval 10          # poll every 10 s until Ctrl+C
python -m sysmon --once                 # one snapshot and exit
python -m sysmon --threshold-config config/config.yaml   # your own limits (YAML or JSON)
```

Output goes to `./logs/`: `host_info.json`, `metrics.jsonl` (one JSON line per cycle, with its status and checks) and `alerts.log` (one line per alert). Edit `config/config.yaml` to change the limits; without `--threshold-config` the same values are built in.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .                          # lint
pytest                                # unit tests (psutil is faked, results are deterministic)
python scripts/e2e_check.py all       # runs the real agent and cross-checks it with OS tools
```

CI runs all three on Windows and Ubuntu with Python 3.10, 3.12 and 3.14.

## License

MIT
