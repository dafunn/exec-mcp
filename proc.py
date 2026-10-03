"""Subprocess supervision: spawn, cap output, enforce a timeout, and tell a
timeout apart from a kill by something else.

Output accumulates as it streams rather than being discarded on timeout, and
the whole process group is signalled, not just the direct child.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

# Preview size on the wire, not a context-window cap: the full output is on
# disk and the harness clips well below this. Read at call time, not bound as
# a default argument, so an env or test override takes effect.
DEFAULT_MAX_OUTPUT = 16 * 1024
MAX_OUTPUT = DEFAULT_MAX_OUTPUT


def max_output() -> int:
    raw = os.environ.get("EXEC_MAX_OUTPUT")
    if raw and raw.strip().isdigit():
        return int(raw)
    return MAX_OUTPUT
GRACE_S = 5.0  # SIGTERM's head start before SIGKILL


@dataclass(frozen=True)
class Outcome:
    status: str  # "finished" | "failed" | "timeout" | "killed" | "missing"
    returncode: int | None
    signal_name: str | None
    output: str
    duration_s: float
    # What `output` is a preview of; the rest is on disk at `log_path`.
    total_bytes: int = 0
    total_lines: int = 0
    log_path: str | None = None


class _RingCapture:
    """Head and tail, not head alone: the first error and the summary line
    are both worth having."""

    def __init__(self, cap: int | None = None):
        self._cap = max_output() if cap is None else cap
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._newlines = 0
        self._ends_with_newline = True

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def total_lines(self) -> int:
        """`splitlines()` semantics, matching the harness: a trailing newline
        does not open a further line."""
        return self._newlines + (0 if self._ends_with_newline else 1)

    def feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        self._newlines += chunk.count(b"\n")
        self._ends_with_newline = chunk.endswith(b"\n")
        if len(self._head) < self._cap:
            take = self._cap - len(self._head)
            self._head += chunk[:take]
        self._tail += chunk
        if len(self._tail) > self._cap:
            del self._tail[: len(self._tail) - self._cap]

    def render(self) -> str:
        if self._total <= self._cap:
            return self._head.decode("utf-8", errors="replace")
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        omitted = max(self._total - len(self._head) - len(self._tail), 0)
        return f"{head}\n[... {omitted} bytes omitted ...]\n{tail}"


async def run(
    argv: list[str], cwd: str, timeout_s: float, log_path: Path | None = None
) -> Outcome:
    """Run `argv` in its own process group, capped by `timeout_s` and
    `MAX_OUTPUT`. Never raises for a process-level failure; that is an outcome.

    `MAX_OUTPUT` bounds what is returned, not what is kept: with `log_path`
    every byte is spilled to disk as it arrives. Spilling is best-effort, and
    a failed spill leaves `log_path` unset on the outcome.
    """
    start = time.monotonic()
    capture = _RingCapture()
    sink = None
    spill_error = ""
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = log_path.open("wb")
        except OSError as exc:
            # Best-effort, but not silent: a directory that cannot be created
            # is a deployment fault and is named in the result.
            sink = None
            spill_error = f"{log_path.parent}: {exc.strerror or exc}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # its own process group - see module docstring
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        # A missing binary is an outcome, not an exception: escaped, it reads
        # as a bare "Error executing tool" naming neither command nor reason.
        if sink is not None:
            sink.close()
        return Outcome(
            "missing", None, None,
            f"{argv[0]}: {exc.strerror or exc}", time.monotonic() - start,
        )

    async def _pump() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            capture.feed(chunk)
            if sink is not None:
                try:
                    sink.write(chunk)
                except OSError:
                    pass

    pump_task = asyncio.ensure_future(_pump())
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(pump_task), timeout=timeout_s)
        await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        await _signal_group(proc, pump_task)
    finally:
        if not pump_task.done():
            pump_task.cancel()

    duration = time.monotonic() - start
    output = capture.render()
    if sink is not None:
        try:
            sink.close()
        except OSError:
            pass
    spilled = str(log_path) if sink is not None and log_path is not None else None
    if spill_error:
        output += f"\n... full output was NOT kept ({spill_error}); only this preview exists"
    counts = {
        "total_bytes": capture.total_bytes,
        "total_lines": capture.total_lines,
        "log_path": spilled,
    }

    # The timeout flag settles who did the killing before returncode is read.
    if timed_out:
        return Outcome("timeout", proc.returncode, None, output, duration, **counts)
    if proc.returncode is not None and proc.returncode < 0:
        sig = signal.Signals(-proc.returncode)
        return Outcome("killed", proc.returncode, sig.name, output, duration, **counts)
    if proc.returncode == 0:
        return Outcome("finished", 0, None, output, duration, **counts)
    return Outcome("failed", proc.returncode, None, output, duration, **counts)


async def _signal_group(proc, pump_task) -> None:
    """SIGTERM first with a short grace period, then SIGKILL, so a test
    runner can flush its report."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=GRACE_S)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    try:
        await asyncio.wait_for(pump_task, timeout=1.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
