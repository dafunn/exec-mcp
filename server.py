"""exec-mcp: runs a task's named build/test commands and commits the result.

Holds no credentials, has no route off the task network, and is the only
place in a build run that spawns a process.

Env contract: MCP_TRANSPORT, MCP_HOST, MCP_PORT, and a bare /healthz. The
workspace root, manifest, and log are fixed in-container paths.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.responses import PlainTextResponse

import git_tools
import proc
from log import InvocationLog
from manifest import ManifestError, load_manifest, offline_tasks
from prefixes import TASK_DONE_PREFIX, TASK_KILLED_PREFIX, TASK_TIMEOUT_PREFIX

WORKSPACE = Path("/workspace")
MANIFEST_PATH = Path("/manifest.toml")
LOG_PATH = Path("/exec.log")

# Full command output, spilled as it streams so a capped preview is not a loss.
# Not under /workspace: a log inside a checkout would show up in `git status`.
# Overridable because the default is only writable if the launcher mounts it -
# the container runs unprivileged and `/` is root-owned.
RUN_LOGS = Path(os.environ.get("EXEC_RUN_LOGS") or "/exec-runs")

# Bounded by count and total size; either alone is escapable.
MAX_RUN_LOGS = int(os.environ.get("EXEC_MAX_RUN_LOGS", "20"))
MAX_RUN_LOG_BYTES = int(os.environ.get("EXEC_MAX_RUN_LOG_BYTES", str(512 * 1024 * 1024)))

MAX_MATCHES = 200

# One invocation at a time, run_task and the git tools alike.
_lock = asyncio.Lock()
_log = InvocationLog(LOG_PATH)

# mcp 2.x: host/port belong to the transport runner, not the constructor.
# `main` passes them, so a stdio run does not have to carry them.
mcp = MCPServer("exec-mcp")


def _deny(why: str) -> str:
    """A refusal: what was refused, why, and what to do instead."""
    return f"[denied] {why}"


def _deny_repo(repo: str) -> str:
    """Name the actual mistake: a path-shaped argument, or an unknown repo."""
    try:
        known = sorted(p.name for p in (WORKSPACE / "src").iterdir() if p.is_dir())
    except OSError:
        known = []
    available = ", ".join(known) if known else "(none checked out)"
    if "/" in repo:
        return _deny(
            f"{repo!r} looks like a path. This takes the repo's name on its "
            f"own, not a path to it - try {repo.rstrip('/').rsplit('/', 1)[-1]!r}. "
            f"Available: {available}."
        )
    return _deny(
        f"{repo!r} is not a write repo in this task's workspace. "
        f"Available: {available}."
    )


def _repo_root(repo: str) -> Path | None:
    """Resolve `repo` to its write checkout, or None. There is no `/refs`
    mount here, so a read-mode repo is simply not found."""
    if not repo or "/" in repo or repo in (".", ".."):
        return None
    root = WORKSPACE / "src" / repo
    return root if root.is_dir() else None


def _manifest_for(repo: str) -> dict[str, "object"]:
    try:
        manifest = load_manifest(MANIFEST_PATH)
    except ManifestError:
        # A malformed manifest is provisioning's bug; treated as nothing declared.
        return {}
    return offline_tasks(manifest.get(repo, {}))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    return PlainTextResponse("ok")


@mcp.tool()
async def list_tasks(repo: str) -> str:
    """List the task names available for `repo`, and what each runs.

    Args:
        repo: the checkout's basename - the same name `run_task` and the git
            tools take.
    """
    if _repo_root(repo) is None:
        return _deny_repo(repo)
    tasks = _manifest_for(repo)
    if not tasks:
        return f"no tasks declared for {repo!r}."
    return "\n".join(
        f"{name}: {' '.join(c.argv)} (timeout {c.timeout_s}s)"
        for name, c in tasks.items()
    )


def _prune_run_logs() -> None:
    """Drop the oldest spilled logs until both bounds hold.

    Newest first, so the run that just finished is never the one deleted.
    Best-effort, like the write: tidying up must not fail a passing build.
    """
    try:
        logs = sorted(RUN_LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    kept = 0
    for index, log in enumerate(logs):
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if index < MAX_RUN_LOGS and kept + size <= MAX_RUN_LOG_BYTES:
            kept += size
            continue
        try:
            log.unlink()
        except OSError:
            pass


def _with_more(text: str, outcome: "proc.Outcome", run_id: str) -> str:
    """Append the run id and, when output was withheld, where the rest is.

    The id is always named so a short result can still be re-read. Both forms
    open with "..." so a harness that caps this text keeps the line as a
    continuation note.
    """
    if outcome.log_path is None:
        return text  # nothing was kept; naming an id would promise a file
    if outcome.total_bytes > len(outcome.output.encode()):
        return (
            f"{text}\n... full output is {outcome.total_lines} lines "
            f"({outcome.total_bytes} bytes), run_id={run_id!r}; read it with "
            f"task_output(start_line=1) or search it with task_output(pattern='...')."
        )
    return f"{text}\n... all output shown above; kept as run_id={run_id!r}."


@mcp.tool()
async def run_task(repo: str, name: str) -> str:
    """Run one named build or test command for `repo` and return its output.

    No parameters beyond the name: whole suite or nothing, one command per
    call. See `list_tasks` for what is available.

    Args:
        repo: the checkout's basename.
        name: one of the names `list_tasks` returned.
    """
    root = _repo_root(repo)
    if root is None:
        return _deny_repo(repo)
    tasks = _manifest_for(repo)
    cmd = tasks.get(name)
    if cmd is None:
        available = ", ".join(sorted(tasks)) or "(none declared)"
        return _deny(f"{name!r} is not a task for {repo!r}. Available: {available}.")

    run_id = f"{repo}-{name}-{int(time.time() * 1000)}"
    async with _lock:
        outcome = await proc.run(
            list(cmd.argv),
            cwd=str(root),
            timeout_s=cmd.timeout_s,
            log_path=RUN_LOGS / f"{run_id}.log",
        )
    _prune_run_logs()

    # The log's vocabulary is ok/failed/timeout/killed; only the model needs
    # finished-clean and finished-nonzero told apart.
    log_outcome = "ok" if outcome.status == "finished" else outcome.status
    _log.task(
        repo, name, list(cmd.argv), log_outcome, outcome.returncode,
        outcome.duration_s, len(outcome.output.encode("utf-8")),
    )

    # First line is a wire header, "{PREFIX} {name}: {short}", that the driver
    # parses; the rest is for the model.
    if outcome.status == "timeout":
        short = f"timed out at {cmd.timeout_s}s"
        return _with_more(
            f"{TASK_TIMEOUT_PREFIX} {name}: {short}\n"
            f"stopped after {cmd.timeout_s}s (timeout_s for {name!r})\n{outcome.output}",
            outcome, run_id,
        )
    if outcome.status == "killed":
        short = f"killed by {outcome.signal_name}"
        return _with_more(
            f"{TASK_KILLED_PREFIX} {name}: {short}\n"
            f"killed by {outcome.signal_name} ({outcome.returncode})\n{outcome.output}",
            outcome, run_id,
        )
    if outcome.status == "missing":
        # Done prefix like any non-zero result; the short says what is wrong.
        return _with_more(
            f"{TASK_DONE_PREFIX} {name}: {outcome.output}\n"
            f"the command for {name!r} is not installed in this image; "
            f"it cannot be run here and no retry will change that",
            outcome, run_id,
        )
    if outcome.status == "failed":
        return _with_more(
            f"{TASK_DONE_PREFIX} {name}: exit {outcome.returncode}\n{outcome.output}",
            outcome, run_id,
        )
    return _with_more(
        f"{TASK_DONE_PREFIX} {name}: ok\n{outcome.output or '(no output)'}", outcome, run_id
    )


# Builds a path, so validated rather than trusted.
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _latest_run() -> str:
    """The most recently written log, by mtime. Run ids sort chronologically
    only within one task, so name order will not do."""
    try:
        logs = sorted(RUN_LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return ""
    return logs[-1].stem if logs else ""


def _known_runs() -> list[str]:
    try:
        return sorted(p.stem for p in RUN_LOGS.glob("*.log"))
    except OSError:
        return []


@mcp.tool()
async def task_output(
    run_id: str = "", start_line: int = 0, end_line: int = 0, pattern: str = ""
) -> str:
    """Read or search the full output of an earlier `run_task`.

    `run_task` returns a preview; the whole log is kept on disk. Give a line
    range to read part of it, or a pattern to search it.

    Args:
        run_id: the id named in the run_task result. Omit it for the most
            recent run.
        start_line: 1-based first line to read. Ignored when `pattern` is set.
        end_line: last line to read; 0 means to the end.
        pattern: a regex; when given, returns matching lines with their numbers.
    """
    if not run_id:
        run_id = _latest_run()
        if not run_id:
            return _deny("no task has been run yet, so there is no output to read.")
    if not _RUN_ID.match(run_id):
        return _deny(f"{run_id!r} is not a run id. Known runs: {', '.join(_known_runs()) or '(none)'}.")
    log = RUN_LOGS / f"{run_id}.log"
    if not log.is_file():
        return _deny(f"no output kept for {run_id!r}. Known runs: {', '.join(_known_runs()) or '(none)'}.")

    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError as exc:
        return _deny(f"could not read the log for {run_id!r}: {exc}")

    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return _deny(f"{pattern!r} is not a valid regex: {exc}")
        hits = [(n, ln) for n, ln in enumerate(lines, 1) if rx.search(ln)]
        if not hits:
            return f"no lines match {pattern!r} in {run_id} ({len(lines)} lines)."
        shown = hits[:MAX_MATCHES]
        body = "\n".join(f"{n:>6}: {ln}" for n, ln in shown)
        if len(hits) > len(shown):
            body += (
                f"\n... {len(hits)} lines match, {len(shown)} shown "
                f"(FS-style cap); narrow the pattern or read a range."
            )
        return body

    start = max(start_line, 1) - 1
    end = end_line if end_line else len(lines)
    if start >= len(lines):
        return f"(no lines; {run_id} has {len(lines)})"
    chunk = lines[start:end]
    body = "\n".join(chunk)
    if len(body.encode()) > proc.MAX_OUTPUT:
        kept = []
        size = 0
        for ln in chunk:
            size += len(ln.encode()) + 1
            if size > proc.MAX_OUTPUT:
                break
            kept.append(ln)
        body = "\n".join(kept)
        nxt = start + len(kept) + 1
        body += (
            f"\n... lines {start + 1}-{start + len(kept)} of {len(lines)}; "
            f"continue with task_output(run_id={run_id!r}, start_line={nxt})."
        )
    return body


def _git_repo(repo: str) -> Path | str:
    """A repo root, or a ready-to-return denial string - callers check
    `isinstance(result, str)`."""
    root = _repo_root(repo)
    if root is None:
        return _deny_repo(repo)
    return root


async def _run_git(repo: str, op: str, summary: str, coro) -> str:
    start = time.monotonic()
    try:
        result = await coro
    except git_tools.GitToolError as e:
        _log.git(repo, op, summary, "failed", time.monotonic() - start)
        return _deny(str(e))
    _log.git(repo, op, summary, "ok", time.monotonic() - start)
    return result


@mcp.tool()
async def git_status(repo: str) -> str:
    """Show `git status --short` for `repo`."""
    root = _git_repo(repo)
    if isinstance(root, str):
        return root
    async with _lock:
        return await _run_git(repo, "status", "", git_tools.git_status(root))


@mcp.tool()
async def git_diff(repo: str, staged: bool = False) -> str:
    """Show a diff for `repo` - unstaged by default, staged on request."""
    root = _git_repo(repo)
    if isinstance(root, str):
        return root
    async with _lock:
        return await _run_git(
            repo, "diff", f"staged={staged}", git_tools.git_diff(root, staged=staged)
        )


@mcp.tool()
async def git_add(repo: str, paths: list[str]) -> str:
    """Stage `paths` (relative to `repo`'s root) for commit.

    Args:
        repo: the checkout's basename.
        paths: relative to the repo root; each is confined under it.
    """
    root = _git_repo(repo)
    if isinstance(root, str):
        return root
    async with _lock:
        return await _run_git(
            repo, "add", ", ".join(paths), git_tools.git_add(root, paths)
        )


@mcp.tool()
async def git_commit(repo: str, message: str) -> str:
    """Commit staged changes in `repo` with `message`."""
    root = _git_repo(repo)
    if isinstance(root, str):
        return root
    async with _lock:
        return await _run_git(
            repo, "commit", message, git_tools.git_commit(root, message)
        )


@mcp.tool()
async def git_log(repo: str, n: int = 10) -> str:
    """Show the last `n` commits in `repo`, oneline."""
    root = _git_repo(repo)
    if isinstance(root, str):
        return root
    async with _lock:
        return await _run_git(repo, "log", f"n={n}", git_tools.git_log(root, n=n))


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "sse")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(
        transport=transport,
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "3000")),
    )


if __name__ == "__main__":
    main()
