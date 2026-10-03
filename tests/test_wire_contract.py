"""Producer side of the wire contract: `run_task`'s first line, which the
driver parses. Pins the grammar, not just the constants, since `exit 1`
drifting to `exited 1` would keep every other test green.
"""
from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest

import proc
import server

CONTRACT = Path(server.__file__).resolve().parent / "wire_contract.toml"

# The contract's `short` templates are formatted with these, so a renamed
# field fails here rather than drifting.
VALUES = {"returncode": 3, "timeout_s": 45, "signal_name": "SIGKILL",
          "output": "systemd-analyze: No such file or directory"}


def _cases():
    return tomllib.loads(CONTRACT.read_text(encoding="utf-8"))["case"]


def _outcome(status: str) -> proc.Outcome:
    return proc.Outcome(
        status=status,
        returncode=VALUES["returncode"] if status == "failed" else None,
        signal_name=VALUES["signal_name"] if status == "killed" else None,
        output=VALUES["output"] if status == "missing" else "some output",
        duration_s=1.0,
    )


def test_the_contract_covers_every_outcome_status():
    covered = {c["status"] for c in _cases()}
    assert covered == {"finished", "failed", "timeout", "killed", "missing"}


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["status"])
def test_each_status_renders_the_header_the_contract_declares(case, rig, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path / "runs")
    expected_short = case["short"].format(**VALUES)
    expected = f"{case['prefix']} demo-task: {expected_short}"

    async def fake_run(argv, cwd, timeout_s, log_path=None):
        return _outcome(case["status"])

    monkeypatch.setattr(proc, "run", fake_run)
    monkeypatch.setattr(
        server, "MANIFEST_PATH", _manifest(tmp_path, timeout_s=VALUES["timeout_s"])
    )
    result = asyncio.run(server.run_task(repo="demo", name="demo-task"))
    assert result.splitlines()[0] == expected


def _manifest(tmp_path, timeout_s: int):
    m = tmp_path / "wire-manifest.toml"
    m.write_text(
        f'[demo]\ndemo-task = {{ argv = ["true"], timeout_s = {timeout_s} }}\n',
        encoding="utf-8",
    )
    return m
