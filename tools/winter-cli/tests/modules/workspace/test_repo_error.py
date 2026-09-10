from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import git

from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory, unwrap_gitpython_stream
from winter_cli.modules.workspace.models import RepoError


def test_unwrap_gitpython_stream_extracts_decorated_multiline_stream():
    """Real git stderr is multi-line and newline-terminated; the regex needs DOTALL to match it."""
    decorated = "\n  stderr: 'remote: Permission denied\nfatal: Could not read from remote repository.\n'"
    assert (
        unwrap_gitpython_stream(decorated)
        == "remote: Permission denied\nfatal: Could not read from remote repository.\n"
    )


def test_unwrap_gitpython_stream_passes_through_plain_string():
    assert unwrap_gitpython_stream("already plain, no decoration") == "already plain, no decoration"


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
        stderr="connection closed",
    )
    err = factory.from_git(exc, message="fetch failed for X", cwd="/tmp/r")
    assert isinstance(err, RepoError)
    assert err.subcommand == "fetch"
    assert err.cmd_args == ("origin",)
    assert err.cwd == "/tmp/r"
    assert err.exit_code == 128
    assert err.stderr == "connection closed"
    assert err.message == "fetch failed for X"


def test_factory_from_git_unwraps_gitpython_stderr_label():
    """GitPython decorates `.stderr` as `"\\n  stderr: '<text>'"`; RepoError.__str__ adds its
    own `stderr:` label, so an unwrapped value would render doubled as `stderr: stderr: '...'`.
    """
    factory = RepoErrorFactory()
    exc = git.GitCommandError(
        command=["git", "push", "origin"],
        status=1,
        stderr="rejected: non-fast-forward",
    )
    err = factory.from_git(exc, message="push failed", cwd="/tmp/r")
    assert err.stderr == "rejected: non-fast-forward"
    assert str(err).count("stderr:") == 1


def test_repo_error_str_renders_legible_message_for_negative_exit_code():
    """A signal-killed git op (exit=-9) must render a legible hint, not a bare negative number."""
    err = RepoError(
        "git push timed out",
        subcommand="push",
        cmd_args=("origin",),
        cwd="/tmp/repo",
        exit_code=-9,
        stderr="",
    )
    rendered = str(err)
    assert "killed by signal 9" in rendered
    # The raw exit code is still present for traceability.
    assert "-9" in rendered


def test_repo_error_program_defaults_to_git():
    """`program` is a new field, defaulted so every pre-existing `RepoError(...)` call

    (none of which ever passed it) renders exactly as before — this is the
    regression the RepoError.__str__ generalization must not break.
    """
    err = RepoError("fetch failed", subcommand="fetch", cmd_args=("origin",))
    assert err.program == "git"
    assert "$ git fetch origin" in str(err)


def test_repo_error_non_git_program_renders_in_place_of_git():
    err = RepoError("command failed", program="vals", subcommand="get", cmd_args=("ref+vault://db",))
    rendered = str(err)
    assert "$ vals get ref+vault://db" in rendered
    assert "$ git" not in rendered


def test_factory_from_subprocess_extracts_fields_for_a_multi_token_command():
    factory = RepoErrorFactory()
    completed = subprocess.CompletedProcess(
        args=["vals", "get", "ref+vault://db"],
        returncode=1,
        stdout="",
        stderr="no such secret",
    )
    err = factory.from_subprocess(completed, "env command entry 'DB_PASSWORD' failed", cwd="/ws")
    assert isinstance(err, RepoError)
    assert err.program == "vals"
    assert err.subcommand == "get"
    assert err.cmd_args == ("ref+vault://db",)
    assert err.cwd == "/ws"
    assert err.exit_code == 1
    assert err.stderr == "no such secret"
    assert "$ vals get ref+vault://db" in str(err)


def test_factory_from_subprocess_handles_a_single_token_command():
    factory = RepoErrorFactory()
    completed = subprocess.CompletedProcess(args=["true"], returncode=1, stdout="", stderr="")
    err = factory.from_subprocess(completed, "command failed", cwd="/ws")
    assert err.program == "true"
    assert err.subcommand is None
    assert err.cmd_args == ()


def test_factory_from_subprocess_handles_a_shell_string_command():
    """`shell=True` invocations pass a raw string as `completed.args`, not a list."""
    factory = RepoErrorFactory()
    completed = subprocess.CompletedProcess(args="run-migration --pw=hunter2", returncode=2, stdout="", stderr="boom")
    err = factory.from_subprocess(completed, "entry 'MIGRATE' failed", cwd="/ws")
    assert err.program == "run-migration --pw=hunter2"
    assert err.subcommand is None
    assert err.cmd_args == ()
    # No `$ ...` line is rendered without a subcommand to split out — the
    # caller's message is expected to already name the command it ran.
    assert "  $ " not in str(err)
    assert err.exit_code == 2
    assert err.stderr == "boom"


def test_factory_from_subprocess_strips_trailing_whitespace_from_stderr():
    factory = RepoErrorFactory()
    completed = MagicMock(args=["cmd"], returncode=1, stdout="", stderr="boom\n\n")
    err = factory.from_subprocess(completed, "failed", cwd="/ws")
    assert err.stderr == "boom"


def test_every_git_wrap_site_still_renders_git_as_the_program():
    """`RepoError.__str__` renders `self.program`; `program` is keyword-only and defaults
    to `"git"`, so no pre-existing call site — none of which passes it — can render anything
    else. Pins that across the field shapes git failures actually take.
    """
    factory = RepoErrorFactory()
    for command in (["git", "fetch", "origin"], ["git", "status"], ["git"], ["git", "clean", "-f", "-d"]):
        exc = git.GitCommandError(command=command, status=128, stderr="nope")
        err = factory.from_git(exc, message="op failed", cwd="/tmp/r")
        assert err.program == "git"
        if err.subcommand:
            assert str(err).splitlines()[1] == "  $ " + " ".join(command)
    # A `RepoError` raised directly, the way most of the codebase raises it.
    assert str(RepoError("boom")) == "boom"
    assert "$ git fetch origin" in str(RepoError("f", subcommand="fetch", cmd_args=("origin",)))


def test_from_exception_defaults_to_no_exit_code():
    """The no-process-ever-ran shape every GitPython constructor failure keeps."""
    err = RepoErrorFactory().from_exception(ValueError("bad path"), "wrap", cwd="/tmp/r")
    assert err.exit_code is None
    assert "exit" not in str(err)


def test_from_exception_renders_an_exit_code_when_one_is_supplied():
    """A timed-out command's child is killed, so its failure can still name a status."""
    err = RepoErrorFactory().from_exception(TimeoutError("slow"), "timed out", cwd="/ws", exit_code=-9)
    assert err.exit_code == -9
    assert "killed by signal 9" in str(err)
