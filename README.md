# sysmon-agent

Cross-platform Python agent that monitors CPU, RAM, disk, network and processes, checks them against configurable thresholds, and logs OK / WARNING / CRITICAL alerts.

> **Status:** in development (Phase 0 — project skeleton). Full documentation arrives with v1.0.0.

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
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

## License

MIT
