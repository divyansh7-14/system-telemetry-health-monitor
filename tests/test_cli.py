import pytest

from sysmon.cli import build_parser, main


def test_defaults():
    args = build_parser().parse_args([])
    assert args.interval is None
    assert args.output_dir == "logs"
    assert args.once is False
    assert args.log_level == "INFO"


@pytest.mark.parametrize("value", ["10", "15", "30"])
def test_interval_accepts_valid_range(value):
    assert build_parser().parse_args(["--interval", value]).interval == int(value)


@pytest.mark.parametrize("value", ["9", "31", "0", "-5", "abc", "12.5"])
def test_interval_rejects_out_of_range(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--interval", value])


def test_top_n_rejects_zero():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--top-n", "0"])


def test_all_flags_parse():
    args = build_parser().parse_args(
        [
            "--interval",
            "20",
            "--threshold-config",
            "config/config.yaml",
            "--output-dir",
            "out",
            "--top-n",
            "3",
            "--once",
            "--log-level",
            "DEBUG",
            "--max-log-mb",
            "0.5",
            "--backup-count",
            "2",
        ]
    )
    assert (args.interval, args.top_n, args.max_log_mb, args.backup_count) == (20, 3, 0.5, 2)
    assert args.threshold_config == "config/config.yaml"


@pytest.fixture
def fast(monkeypatch):
    from sysmon import agent

    monkeypatch.setattr(agent, "WARMUP_SECONDS", 0.0)


def test_once_writes_host_info_and_one_snapshot(tmp_path, fast, capsys):
    import json

    assert main(["--once", "--output-dir", str(tmp_path), "--top-n", "3"]) == 0
    data = json.loads((tmp_path / "host_info.json").read_text(encoding="utf-8"))
    assert {"hostname", "os_name", "architecture", "uptime_seconds"} <= data.keys()
    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    snap = json.loads(lines[0])
    assert snap["cycle"] == 1 and snap["errors"] == []
    assert len(snap["top_processes"]["by_cpu"]) == 3
    out = capsys.readouterr().out
    assert "== Host ==" in out and "== Network ==" in out and "[#1 " in out


def test_unused_flags_warn(tmp_path, fast, caplog):
    main(["--once", "--output-dir", str(tmp_path), "--threshold-config", "x.yaml"])
    assert "--threshold-config is accepted but not used yet" in caplog.text


def test_unwritable_output_dir_exits_3(tmp_path, capsys):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    assert main(["--once", "--output-dir", str(blocker / "sub")]) == 3
    assert "cannot write to output directory" in capsys.readouterr().err


def test_bad_flag_is_usage_error_2_not_output_error(capsys):
    """argparse exits 2; that code must not also mean 'output dir not writable'."""
    with pytest.raises(SystemExit) as exc:
        main(["--interval", "5"])
    assert exc.value.code == 2


def test_host_info_write_failure_exits_3_without_traceback(tmp_path, monkeypatch, capsys):
    from sysmon import cli

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_json_atomic", disk_full)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 3
    assert "No space left on device" in capsys.readouterr().err


def test_ctrl_c_stops_cleanly(tmp_path, monkeypatch, capsys):
    from sysmon.agent import Agent

    def interrupt(self, max_cycles=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(Agent, "run", interrupt)
    assert main(["--output-dir", str(tmp_path)]) == 0
    assert "interrupted" in capsys.readouterr().err


def test_continuous_mode_stops_on_signal(tmp_path, fast, monkeypatch, capsys):
    """Without --once the agent loops until a stop signal arrives."""
    import signal
    import threading

    from sysmon import cli
    from sysmon.agent import Agent

    real_run_cycle = Agent.run_cycle

    def run_cycle_then_signal(self):
        snap = real_run_cycle(self)
        if self.cycles == 2:
            threading.Timer(0.05, signal.raise_signal, (signal.SIGINT,)).start()
        return snap

    monkeypatch.setattr(Agent, "run_cycle", run_cycle_then_signal)

    class QuickAgent(Agent):  # the CLI only allows 10-30 s; keep the test fast
        def __init__(self, out_dir, interval, *a, **k):
            super().__init__(out_dir, 0.2, *a, **k)

    monkeypatch.setattr(cli, "Agent", QuickAgent)
    assert main(["--output-dir", str(tmp_path), "--interval", "10"]) == 0
    out = capsys.readouterr().out
    assert "stopped after 2 cycle(s)" in out


def test_no_console_pythonw_style(tmp_path, fast, monkeypatch):
    """pythonw / Task Scheduler: sys.stdout and sys.stderr are None."""
    import json
    import sys

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))["cycle"] == 1


def test_console_that_breaks_is_silenced_once(tmp_path, fast, monkeypatch, caplog):
    import io
    import sys

    class Broken(io.StringIO):
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self):
            raise io.UnsupportedOperation("no fd")

    monkeypatch.setattr(sys, "stdout", Broken())
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert caplog.text.count("console output is unavailable") == 1
    assert (tmp_path / "metrics.jsonl").exists()
