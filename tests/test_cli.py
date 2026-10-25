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
    main(["--once", "--output-dir", str(tmp_path), "--max-log-mb", "1"])
    assert "--max-log-mb is accepted but not used yet" in caplog.text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("thresholds:\n  cpu_max: {warning: 90, critical: 80}\n", "must be lower than critical"),
        ("thresholds: [\n", "invalid YAML at line"),
    ],
)
def test_invalid_config_exits_4_with_one_line(tmp_path, capsys, text, message):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(text, encoding="utf-8")
    assert (
        main(["--once", "--output-dir", str(tmp_path / "out"), "--threshold-config", str(cfg)]) == 4
    )
    err = capsys.readouterr().err.strip()
    assert message in err and len(err.splitlines()) == 1 and "Traceback" not in err
    assert not (tmp_path / "out").exists()  # nothing started


def test_missing_config_file_exits_4(tmp_path, capsys):
    assert main(["--once", "--threshold-config", str(tmp_path / "nope.yaml")]) == 4
    assert "config file not found" in capsys.readouterr().err


def test_no_config_says_built_in(tmp_path, fast, caplog, capsys):
    import logging

    caplog.set_level(logging.INFO)
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    assert "using built-in thresholds" in caplog.text
    assert "== Health rules (built-in defaults) ==" in capsys.readouterr().out


def test_config_file_applied_and_cli_overrides_it(tmp_path, fast, monkeypatch, capsys, caplog):
    from sysmon import cli

    cfg = tmp_path / "c.yaml"
    cfg.write_text("interval: 25\ntop_n: 2\nthresholds:\n  cpu_maxx: 1\n", encoding="utf-8")
    seen = {}

    class Spy(cli.Agent):
        def __init__(self, out_dir, interval, top_n, **kw):
            seen.update(interval=interval, top_n=top_n)
            super().__init__(out_dir, interval, top_n, **kw)

    monkeypatch.setattr(cli, "Agent", Spy)
    out = str(tmp_path / "o")
    assert (
        main(["--once", "--output-dir", out, "--threshold-config", str(cfg), "--top-n", "4"]) == 0
    )
    assert seen == {"interval": 25, "top_n": 4}  # file interval, CLI top-n
    assert "did you mean 'thresholds.cpu_max'" in caplog.text
    assert f"== Health rules ({cfg}) ==" in capsys.readouterr().out


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


def test_once_judges_the_single_reading_immediately(tmp_path, fast, monkeypatch):
    """--once has one cycle, so 'must last 2 cycles' would hide a full disk."""
    import json

    from sysmon import agent as agent_mod
    from sysmon.models import DiskStats

    full = DiskStats("d", "C:\\", "NTFS", 100, 97, 3, 97.0, 3.0)
    monkeypatch.setattr(agent_mod, "collect_disks", lambda warned: [full])
    assert main(["--once", "--output-dir", str(tmp_path)]) == 0
    snap = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    disk = [c for c in snap["checks"] if c["rule"] == "disk_min_free"][0]
    assert disk["status"] == "CRITICAL" and snap["status"] == "CRITICAL"
    assert [a["note"] for a in snap["alerts"] if a["rule"] == "disk_min_free"] == ["OK->CRITICAL"]
