"""One JSON object per line, appended to a file the launcher bind-mounts in.
Every tool is logged, git included, so one file shows everything the agent
did through this server.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class InvocationLog:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def _write(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True)
        with self._lock, open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def task(
        self,
        repo: str,
        name: str,
        argv: list[str],
        outcome: str,
        returncode: int | None,
        duration_s: float,
        output_bytes: int,
    ) -> None:
        self._write({
            "ts": time.time(),
            "repo": repo,
            "kind": "task",
            "name": name,
            "argv": argv,
            "outcome": outcome,
            "returncode": returncode,
            "duration_s": round(duration_s, 3),
            "output_bytes": output_bytes,
        })

    def git(self, repo: str, op: str, summary: str, outcome: str, duration_s: float) -> None:
        self._write({
            "ts": time.time(),
            "repo": repo,
            "kind": "git",
            "op": op,
            # Capped: this is a log line, not the diff itself.
            "summary": summary[:500],
            "outcome": outcome,
            "duration_s": round(duration_s, 3),
        })
