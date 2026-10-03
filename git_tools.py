"""The git tool set: defined in code, independent of the manifest.

`git_add`'s `paths` is the one untrusted argument. `git add` already refuses
to leave its worktree; `resolve_path` is defence in depth.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


class GitToolError(RuntimeError):
    """Model-facing: the message is returned as the tool result, not raised
    to the transport."""


class PathOutsideRepo(GitToolError):
    pass


def resolve_path(repo_root: Path, entry: str) -> Path:
    """Confine `entry` under `repo_root`, or raise `PathOutsideRepo`."""
    if not entry or entry.startswith("/"):
        raise PathOutsideRepo(f"{entry!r} must be a path relative to the repo root.")
    root = repo_root.resolve()
    target = (root / entry).resolve()
    if target != root and root not in target.parents:
        raise PathOutsideRepo(f"{entry!r} resolves outside the repo.")
    return target


async def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo_root), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def git_status(repo_root: Path) -> str:
    code, out = await _git(repo_root, "status", "--short")
    if code != 0:
        raise GitToolError(f"git status failed (exit {code}): {out or '(no output)'}")
    return out or "(clean)"


async def git_diff(repo_root: Path, staged: bool = False) -> str:
    args = ("diff", "--staged") if staged else ("diff",)
    code, out = await _git(repo_root, *args)
    if code != 0:
        raise GitToolError(f"git diff failed (exit {code}): {out or '(no output)'}")
    return out or "(no changes)"


async def git_add(repo_root: Path, paths: list[str]) -> str:
    if not paths:
        raise GitToolError("no paths given; nothing to stage.")
    for p in paths:
        resolve_path(repo_root, p)  # raises PathOutsideRepo on a bad entry
    code, out = await _git(repo_root, "add", "--", *paths)
    if code != 0:
        raise GitToolError(f"git add failed (exit {code}): {out or '(no output)'}")
    return f"staged: {', '.join(paths)}"


async def git_commit(repo_root: Path, message: str) -> str:
    if not message.strip():
        raise GitToolError("a commit message is required.")
    code, out = await _git(repo_root, "commit", "-m", message)
    if code != 0:
        raise GitToolError(f"git commit failed (exit {code}): {out or '(no output)'}")
    return out or "committed."


async def git_log(repo_root: Path, n: int = 10) -> str:
    code, out = await _git(repo_root, "log", f"-n{max(1, int(n))}", "--oneline")
    if code != 0:
        raise GitToolError(f"git log failed (exit {code}): {out or '(no output)'}")
    return out or "(no commits)"
