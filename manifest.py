"""Load the command manifest provisioning writes to `$TASK_DIR/manifest.toml`.
Read-only here; keyed by repo (the checkout basename) then task name.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 600


class ManifestError(ValueError):
    """The manifest file exists but is malformed - a provisioning bug. A repo
    with no entry is "no tasks", not this."""


@dataclass(frozen=True)
class Command:
    name: str
    argv: tuple[str, ...]
    phase: str = ""  # "" (exec, offline) | "provision"
    timeout_s: int = DEFAULT_TIMEOUT_S


def load_manifest(path: Path) -> dict[str, dict[str, Command]]:
    """Parse the manifest into `{repo_name: {task_name: Command}}`. A missing
    file comes back as `{}`."""
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ManifestError(f"could not read manifest at {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"manifest at {path} is not valid TOML: {e}") from e

    out: dict[str, dict[str, Command]] = {}
    for repo_name, tasks in raw.items():
        if not isinstance(tasks, dict):
            raise ManifestError(f"[{repo_name}] is not a table")
        repo_cmds: dict[str, Command] = {}
        for task_name, entry in tasks.items():
            repo_cmds[task_name] = _command(repo_name, task_name, entry)
        out[repo_name] = repo_cmds
    return out


def _command(repo_name: str, task_name: str, entry: object) -> Command:
    where = f"[{repo_name}].{task_name}"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} is not a table")
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(a, str) and a for a in argv
    ):
        raise ManifestError(f"{where} needs a non-empty argv of strings")
    phase = str(entry.get("phase", "")).strip()
    if phase not in ("", "provision"):
        raise ManifestError(f"{where} has phase {phase!r}; expected \"provision\" or unset")
    try:
        timeout_s = int(entry.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        raise ManifestError(f"{where} has a non-numeric timeout_s") from None
    if timeout_s <= 0:
        raise ManifestError(f"{where} needs a positive timeout_s")
    return Command(name=task_name, argv=tuple(argv), phase=phase, timeout_s=timeout_s)


def offline_tasks(repo_commands: dict[str, Command]) -> dict[str, Command]:
    """Everything but `phase = "provision"`, which already ran on a network
    this container does not have."""
    return {n: c for n, c in repo_commands.items() if c.phase != "provision"}
