"""Exercises the tool functions directly, bypassing the SSE transport, against
a fake `/workspace`, `/manifest.toml` and `/exec.log` monkeypatched to tmp_path.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

import proc
import server


def _lines(log_path):
    return [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l]


async def test_a_clean_run_logs_ok_not_finished(rig):
    result = await server.run_task(repo="demo", name="ok")
    assert result.startswith(server.TASK_DONE_PREFIX)
    records = _lines(rig)
    assert records[-1]["outcome"] == "ok"


async def test_an_unknown_repo_is_denied_not_a_traceback(rig):
    result = await server.run_task(repo="../escape", name="ok")
    assert result.startswith("[denied]")


async def test_git_add_outside_the_repo_is_logged_as_failed(rig):
    await server.git_add(repo="demo", paths=["../../outside"])
    records = _lines(rig)
    assert records[-1]["kind"] == "git"
    assert records[-1]["outcome"] == "failed"


def test_sse_app_constructs_and_keeps_healthz():
    """Nothing else here exercises the transport layer."""
    app = server.mcp.sse_app()
    assert "/healthz" in [getattr(r, "path", None) for r in app.routes]
    assert "/sse" in [getattr(r, "path", None) for r in app.routes]


def test_every_tool_is_still_registered():
    """A silently dropped tool leaves the server running and useless."""
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "run_task", "list_tasks", "task_output",
        "git_status", "git_diff", "git_add", "git_commit", "git_log",
    }



def _run_log(tmp_path, monkeypatch, lines):
    """Point RUN_LOGS at a temp dir holding one log."""
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path)
    (tmp_path / "demo.log").write_text("\n".join(lines) + "\n")
    return "demo"


def test_full_output_is_spilled_and_the_preview_says_where(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "MAX_OUTPUT", 200)
    log = tmp_path / "run.log"
    outcome = asyncio.run(
        proc.run(["sh", "-c", "for i in $(seq 1 500); do echo line $i; done"],
                 cwd=str(tmp_path), timeout_s=30, log_path=log)
    )
    assert outcome.status == "finished"
    # Preview is capped; the file is not.
    assert len(outcome.output) < outcome.total_bytes
    assert outcome.total_lines == 500
    assert log.read_text().splitlines()[-1] == "line 500"
    text = server._with_more("HEADER\n" + outcome.output, outcome, "demo")
    assert text.splitlines()[-1].startswith("...")
    assert "run_id='demo'" in text
    assert "task_output(start_line=1)" in text


def test_a_complete_preview_still_names_its_run_id(tmp_path, monkeypatch):
    log = tmp_path / "run.log"
    outcome = asyncio.run(
        proc.run(["sh", "-c", "echo just one line"], cwd=str(tmp_path), timeout_s=30, log_path=log)
    )
    text = server._with_more("HEADER\nx", outcome, "demo")
    assert "run_id='demo'" in text
    assert "read it with" not in text  # nothing more to read


def test_task_output_reads_a_range(tmp_path, monkeypatch):
    rid = _run_log(tmp_path, monkeypatch, [f"line {i}" for i in range(1, 101)])
    out = asyncio.run(server.task_output(rid, start_line=10, end_line=12))
    assert out == "line 10\nline 11\nline 12"


def test_task_output_searches(tmp_path, monkeypatch):
    rid = _run_log(tmp_path, monkeypatch, ["ok", "FAILED test_a", "ok", "FAILED test_b"])
    out = asyncio.run(server.task_output(rid, pattern="^FAILED"))
    assert "2: FAILED test_a" in out and "4: FAILED test_b" in out
    # Line numbers are the file's, so a range read can follow the search.
    assert "1: ok" not in out


def test_task_output_refuses_a_run_id_that_walks_out(tmp_path, monkeypatch):
    _run_log(tmp_path, monkeypatch, ["x"])
    out = asyncio.run(server.task_output("../../etc/passwd"))
    assert out.startswith("[denied]")
    out = asyncio.run(server.task_output("nope"))
    assert out.startswith("[denied]") and "demo" in out  # names what does exist


def test_task_output_caps_a_huge_range_and_says_how_to_continue(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "MAX_OUTPUT", 200)
    rid = _run_log(tmp_path, monkeypatch, [f"line {i:04d} padding" for i in range(1, 400)])
    out = asyncio.run(server.task_output(rid, start_line=1))
    assert out.splitlines()[-1].startswith("...")
    assert "continue with task_output(" in out


def test_spilled_logs_are_pruned_by_count(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path)
    monkeypatch.setattr(server, "MAX_RUN_LOGS", 3)
    import time as _t
    for i in range(6):
        f = tmp_path / f"run{i}.log"
        f.write_text("x" * 10)
        os.utime(f, (i, i))  # oldest first
    server._prune_run_logs()
    left = sorted(p.stem for p in tmp_path.glob("*.log"))
    assert left == ["run3", "run4", "run5"]  # the three newest


def test_spilled_logs_are_pruned_by_total_size(tmp_path, monkeypatch):
    """Count alone is escapable - twenty 2 GB logs fit under it."""
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path)
    monkeypatch.setattr(server, "MAX_RUN_LOGS", 100)
    monkeypatch.setattr(server, "MAX_RUN_LOG_BYTES", 250)
    for i in range(5):
        f = tmp_path / f"run{i}.log"
        f.write_text("x" * 100)
        os.utime(f, (i, i))
    server._prune_run_logs()
    left = sorted(p.stem for p in tmp_path.glob("*.log"))
    assert left == ["run3", "run4"]  # 200 bytes fits, a third would not


def test_a_path_shaped_repo_argument_is_diagnosed_not_denied_as_absent():
    out = server._deny_repo("src/widget")
    assert "looks like a path" in out
    assert "'widget'" in out  # names the form that would work


def test_an_unknown_repo_still_reads_as_unknown():
    out = server._deny_repo("nope")
    assert "is not a write repo" in out
    assert "looks like a path" not in out


async def test_a_missing_command_is_reported_rather_than_passing_as_ok(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path / "runs")
    monkeypatch.setattr(
        server, "MANIFEST_PATH", _manifest_with(tmp_path, "definitely-not-a-real-binary-xyz")
    )
    result = await server.run_task(repo="demo", name="nope")
    first = result.splitlines()[0]
    assert not first.endswith(": ok")
    assert "definitely-not-a-real-binary-xyz" in result
    assert "not installed" in result


def _manifest_with(tmp_path, binary: str):
    m = tmp_path / "manifest-missing.toml"
    m.write_text(f'[demo]\nnope = {{ argv = ["{binary}"] }}\n', encoding="utf-8")
    return m


def test_task_output_defaults_to_the_most_recent_run(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path)
    # Names chosen so alphabetical and chronological order disagree.
    for name, when, body in [("zzz-old", 100, "old output"), ("aaa-new", 200, "new output")]:
        f = tmp_path / f"{name}.log"
        f.write_text(body)
        os.utime(f, (when, when))
    out = asyncio.run(server.task_output())
    assert out == "new output", "must be newest by mtime, not last by name"


def test_task_output_with_no_runs_at_all_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUN_LOGS", tmp_path)
    out = asyncio.run(server.task_output())
    assert out.startswith("[denied]") and "no task has been run yet" in out
