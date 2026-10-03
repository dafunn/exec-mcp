"""Manifest parsing, and keeping `phase = "provision"` commands out of
`run_task`: they already ran on a network this container does not have.
"""
from __future__ import annotations

import pytest

from manifest import Command, ManifestError, load_manifest, offline_tasks

GOOD = """
[widget]
configure = { argv = ["cmake", "-S", ".", "-B", "build"], phase = "provision" }
build     = { argv = ["cmake", "--build", "build", "-j4"], timeout_s = 600 }
test      = { argv = ["ctest", "--test-dir", "build", "--output-on-failure"], timeout_s = 300 }
"""


def test_parses_commands_keyed_by_repo_then_task(tmp_path):
    p = tmp_path / "manifest.toml"
    p.write_text(GOOD, encoding="utf-8")
    m = load_manifest(p)
    assert set(m) == {"widget"}
    assert set(m["widget"]) == {"configure", "build", "test"}
    assert m["widget"]["build"].argv == ("cmake", "--build", "build", "-j4")
    assert m["widget"]["build"].timeout_s == 600
    assert m["widget"]["configure"].phase == "provision"
    assert m["widget"]["test"].phase == ""


def test_a_missing_file_is_not_an_error(tmp_path):
    """A repo with no commands is a legitimate state, not a crash."""
    assert load_manifest(tmp_path / "nope.toml") == {}


def test_entries_without_a_timeout_get_the_default(tmp_path):
    p = tmp_path / "manifest.toml"
    p.write_text('[widget]\nlint = { argv = ["ruff", "check", "."] }\n', encoding="utf-8")
    m = load_manifest(p)
    assert m["widget"]["lint"].timeout_s == 600


@pytest.mark.parametrize(
    "body",
    [
        "[widget]\nbuild = 1\n",  # not a table
        '[widget]\nbuild = { argv = [] }\n',  # empty argv
        '[widget]\nbuild = { argv = "x" }\n',  # argv not a list
        '[widget]\nbuild = { argv = ["x"], phase = "bogus" }\n',  # bad phase
        '[widget]\nbuild = { argv = ["x"], timeout_s = 0 }\n',  # non-positive
        '[widget]\nbuild = { argv = ["x"], timeout_s = "soon" }\n',  # not numeric
    ],
)
def test_malformed_entries_are_rejected(tmp_path, body):
    p = tmp_path / "manifest.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_offline_tasks_excludes_the_provision_phase():
    repo_cmds = {
        "configure": Command("configure", ("cmake",), phase="provision"),
        "build": Command("build", ("cmake", "--build", "build")),
    }
    assert set(offline_tasks(repo_cmds)) == {"build"}
