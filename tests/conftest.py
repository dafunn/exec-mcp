"""Shared fixtures."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

# Make the repo-root server.py importable as `server`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    repo = ws / "src" / "demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)

    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        '[demo]\n'
        'ok = { argv = ["python3", "-c", "print(1)"] }\n',
        encoding="utf-8",
    )
    log = tmp_path / "exec.log"
    log.touch()

    monkeypatch.setattr(server, "WORKSPACE", ws)
    monkeypatch.setattr(server, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(server, "LOG_PATH", log)
    monkeypatch.setattr(server, "_log", server.InvocationLog(log))
    return log
