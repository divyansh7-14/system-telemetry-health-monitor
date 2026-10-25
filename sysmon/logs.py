"""Append-only output files: metrics.jsonl and alerts.log.

A LineWriter appends whole lines and never stops the agent: a failed write is
logged once, retried on the next call, and its recovery is logged too. If a
previous run was killed in the middle of a write, the first write of this run
starts on a fresh line so the damage stays on its own line.
(Size-based rotation is added in Phase 4.)
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class LineWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._failed = False
        self._checked_tail = False

    def write(self, text: str) -> bool:
        """Append ``text`` (one or more lines ending in newline). Returns success."""
        if not self._checked_tail:
            text = self._newline_if_unterminated() + text
        try:
            # Opened per write: no long-lived handle that would block rotation on Windows.
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            if not self._failed:
                log.error("cannot write %s: %s (will keep trying)", self.path, exc)
            self._failed = True
            return False
        self._checked_tail = True
        if self._failed:
            log.info("writing %s works again", self.path)
        self._failed = False
        return True

    def _newline_if_unterminated(self) -> str:
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() == 0:
                    return ""
                fh.seek(-1, 2)
                return "" if fh.read(1) == b"\n" else "\n"
        except OSError:
            return ""
