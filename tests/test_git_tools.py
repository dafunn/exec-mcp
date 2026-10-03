"""Path confinement: `git_add`'s `paths` is the one untrusted argument."""
from __future__ import annotations

import subprocess

import pytest

from git_tools import (
    GitToolError,
    PathOutsideRepo,
    git_add,
    git_commit,
    git_log,
    git_status,
    resolve_path,
)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


def test_resolve_path_accepts_a_relative_path_inside_the_repo(repo):
    (repo / "a.py").write_text("x", encoding="utf-8")
    assert resolve_path(repo, "a.py") == (repo / "a.py").resolve()


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "a/../../outside"])
def test_resolve_path_refuses_absolute_and_climbing_paths(repo, bad):
    with pytest.raises(PathOutsideRepo):
        resolve_path(repo, bad)


async def test_status_and_add_and_commit_round_trip(repo):
    (repo / "a.py").write_text("hello\n", encoding="utf-8")
    assert "a.py" in await git_status(repo)
    result = await git_add(repo, ["a.py"])
    assert "a.py" in result
    out = await git_commit(repo, "add a.py")
    assert out
    log = await git_log(repo, n=5)
    assert "add a.py" in log


async def test_add_refuses_a_path_outside_the_repo(repo):
    with pytest.raises(PathOutsideRepo):
        await git_add(repo, ["../elsewhere"])


async def test_commit_refuses_an_empty_message(repo):
    with pytest.raises(GitToolError):
        await git_commit(repo, "   ")


async def test_add_refuses_when_nothing_named(repo):
    with pytest.raises(GitToolError):
        await git_add(repo, [])
