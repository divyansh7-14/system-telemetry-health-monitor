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


def test_main_returns_zero(capsys):
    assert main(["--once"]) == 0
    assert "arguments parsed" in capsys.readouterr().out
