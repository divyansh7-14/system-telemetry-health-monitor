"""Command-line interface: argument parsing and validation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sysmon import __version__

MIN_INTERVAL = 10
MAX_INTERVAL = 30
DEFAULT_INTERVAL = 15


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Phase 0: CLI only. The agent loop is wired in later phases.
    print(f"sysmon {__version__}: arguments parsed ({vars(args)}). Agent not implemented yet.")
    return 0
