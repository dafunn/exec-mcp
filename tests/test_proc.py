"""Subprocess supervision: who did the killing decides the outcome, output is
never discarded on timeout, and the whole process group dies, not just the
direct child.
"""
from __future__ import annotations

import os
import signal
import sys

import pytest

from proc import MAX_OUTPUT, run


async def test_a_clean_exit_reports_finished(tmp_path):
    outcome = await run([sys.executable, "-c", "print('hi')"], cwd=str(tmp_path), timeout_s=5)
    assert outcome.status == "finished"
    assert outcome.returncode == 0
    assert "hi" in outcome.output


async def test_a_nonzero_exit_reports_failed_not_killed(tmp_path):
    outcome = await run(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=str(tmp_path), timeout_s=5
    )
    assert outcome.status == "failed"
    assert outcome.returncode == 3


async def test_a_stalled_process_is_reported_as_timeout_with_output_kept(tmp_path):
    outcome = await run(
        [sys.executable, "-c", "print('before stall', flush=True); import time; time.sleep(30)"],
        cwd=str(tmp_path),
        timeout_s=0.3,
    )
    assert outcome.status == "timeout"
    # The output captured before the stall must not be discarded.
    assert "before stall" in outcome.output


async def test_a_signalled_process_reports_killed_with_the_signal_named(tmp_path):
    outcome = await run(
        [sys.executable, "-c", f"import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        cwd=str(tmp_path),
        timeout_s=5,
    )
    assert outcome.status == "killed"
    assert outcome.signal_name == "SIGKILL"


def _is_dead(pid: int) -> bool:
    """Reaped, or a zombie waiting to be.

    `os.kill(pid, 0)` cannot tell a zombie from a running process, and a
    grandchild reparented to a PID 1 that does not reap sits as one
    indefinitely. /proc state holds under either arrangement.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # `comm` can contain spaces and parens; the state follows the last
            # ")" by definition of the format.
            return fh.read().rsplit(") ", 1)[1].split()[0] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


async def test_the_whole_process_group_is_killed_on_timeout(tmp_path):
    """cmake --build spawns ninja, which spawns compilers - a child of the
    child must not survive as an orphan."""
    marker = tmp_path / "child_pid"
    script = (
        "import os, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', "
        f"'open(\"{marker}\", \"w\").write(str(__import__(\"os\").getpid())); "
        f"import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    await run([sys.executable, "-c", script], cwd=str(tmp_path), timeout_s=0.5)
    # Give the grandchild a moment to actually die after the group signal.
    import asyncio

    for _ in range(20):
        if marker.exists():
            break
        await asyncio.sleep(0.1)
    assert marker.exists(), "the child process never started"
    child_pid = int(marker.read_text())
    # Poll past GRACE_S: a process that ignores SIGTERM is only SIGKILLed
    # after that, and a shorter budget here would give up first.
    for _ in range(40):
        if _is_dead(child_pid):
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail("the grandchild survived the process-group kill")


async def test_output_beyond_the_cap_keeps_head_and_tail(tmp_path):
    script = (
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * (1 << 20))\n"
        "sys.stdout.write('TAIL')\n"
    )
    outcome = await run([sys.executable, "-c", script], cwd=str(tmp_path), timeout_s=5)
    assert outcome.status == "finished"
    assert outcome.output.startswith("HEAD")
    assert outcome.output.endswith("TAIL")
    assert len(outcome.output) < 3 * MAX_OUTPUT


async def test_a_missing_binary_is_an_outcome_not_an_exception(tmp_path):
    outcome = await run(["definitely-not-a-real-binary-xyz"], cwd=str(tmp_path), timeout_s=5)
    assert outcome.status == "missing"
    assert "definitely-not-a-real-binary-xyz" in outcome.output
    assert outcome.returncode is None


async def test_a_spill_that_cannot_be_set_up_says_so(tmp_path):
    """The blocker is a directory under a regular file, not a permission
    denial: ENOTDIR binds regardless of privilege, so this holds as root."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file")
    outcome = await run(
        ["sh", "-c", "echo hello"], cwd=str(tmp_path), timeout_s=10,
        log_path=blocker / "sub" / "run.log",
    )
    assert outcome.status == "finished"          # the build still passes
    assert outcome.log_path is None              # nothing points at a missing file
    assert "was NOT kept" in outcome.output      # and the caller is told
    assert "hello" in outcome.output             # the preview survives


async def test_a_working_spill_says_nothing_extra(tmp_path):
    outcome = await run(["sh", "-c", "echo hello"], cwd=str(tmp_path),
                        timeout_s=10, log_path=tmp_path / "runs" / "r.log")
    assert "was NOT kept" not in outcome.output
    assert outcome.log_path is not None
