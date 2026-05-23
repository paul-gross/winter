from __future__ import annotations

import git

from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError


def test_repo_error_str_includes_structured_fields():
    err = RepoError(
        "fetch failed",
        subcommand="fetch",
        cmd_args=("origin",),
        cwd="/tmp/repo",
        exit_code=128,
        stderr="Could not read from remote repository.",
    )
    rendered = str(err)
    assert "fetch failed" in rendered
    assert "git fetch origin" in rendered
    assert "/tmp/repo" in rendered
    assert "128" in rendered
    assert "Could not read from remote repository." in rendered


def test_repo_error_str_minimal_when_only_message():
    assert str(RepoError("boom")) == "boom"


def test_factory_from_git_extracts_fields():
    factory = RepoErrorFactory()
    exc = git.GitCommandError(
        command=["git", "fetch", "origin"],
        status=128,
        stderr="stderr: 'connection closed'",
    )
    err = factory.from_git(exc, message="fetch failed for X", cwd="/tmp/r")
    assert isinstance(err, RepoError)
    assert err.subcommand == "fetch"
    assert err.cmd_args == ("origin",)
    assert err.cwd == "/tmp/r"
    assert err.exit_code == 128
    assert "connection closed" in err.stderr
    assert err.message == "fetch failed for X"
