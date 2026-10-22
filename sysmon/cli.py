"""Command-line interface: argument parsing and validation."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from sysmon import __version__
from sysmon.collectors.host import collect_host_info
from sysmon.collectors.resources import CpuSampler, collect_resources
from sysmon.reporting import format_host_info, format_resources, write_json_atomic

MIN_INTERVAL = 10
MAX_INTERVAL = 30
DEFAULT_INTERVAL = 15
ONCE_SAMPLE_SECONDS = 1.0
EXIT_OUTPUT_DIR = 2


def _interval(value: str) -> int:
    """argparse type: polling interval in whole seconds, 10-30 inclusive."""
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"interval must be a whole number, got {value!r}") from exc
    if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds, got {seconds}"
        )
    return seconds


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a number >= 1, got {number}")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a number > 0, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysmon",
        description=(
            "System Telemetry & Health Monitor: polls CPU, RAM, disk, network and "
            "processes, checks them against configurable thresholds, and logs "
            "OK / WARNING / CRITICAL status."
        ),
    )
    parser.add_argument(
        "--interval",
        type=_interval,
        default=None,
        help=(
            f"polling interval in seconds, {MIN_INTERVAL}-{MAX_INTERVAL} "
            f"(default: {DEFAULT_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--threshold-config",
        metavar="PATH",
        default=None,
        help="path to a .yaml/.yml or .json file with health thresholds",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default="logs",
        help="directory for metrics.jsonl, alerts.log and agent.log (default: ./logs)",
    )
    parser.add_argument(
        "--top-n",
        type=_positive_int,
        default=None,
        help="number of top CPU and memory processes to record (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single collection cycle and exit",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="console and agent.log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--max-log-mb",
        type=_positive_float,
        default=None,
        help="rotate each log file when it reaches this size in MB (default: 5)",
    )
    parser.add_argument(
        "--backup-count",
        type=_positive_int,
        default=None,
        help="number of rotated log files to keep (default: 3)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def prepare_output_dir(path: str) -> Path:
    """Create the output directory and confirm it is writable."""
    out = Path(path).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    probe = out / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")

    try:
        out_dir = prepare_output_dir(args.output_dir)
    except OSError as exc:
        print(
            f"sysmon: cannot write to output directory {args.output_dir!r}: {exc}", file=sys.stderr
        )
        return EXIT_OUTPUT_DIR

    host = collect_host_info()
    write_json_atomic(out_dir / "host_info.json", host.to_dict())

    cpu = CpuSampler()
    cpu.prime()
    time.sleep(ONCE_SAMPLE_SECONDS)  # give the first CPU reading a real window
    resources = collect_resources(cpu)

    print(format_host_info(host))
    print(format_resources(resources))
    print(f"\nHost info written to {out_dir / 'host_info.json'}")

    if not args.once:
        print("Continuous polling is added in Phase 2; ran a single cycle.")
    return 0
