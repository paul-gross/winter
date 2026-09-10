from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from winter_cli.modules.workspace.internal import subprocess_command_entry_runner
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.subprocess_command_entry_runner import (
    SubprocessCommandEntryRunner,
)
from winter_cli.modules.workspace.models import RepoError

_ROOT = Path("/fake/workspace")


@pytest.fixture
def error_factory() -> RepoErrorFactory:
    return RepoErrorFactory()


@pytest.fixture
def runner(error_factory: RepoErrorFactory) -> SubprocessCommandEntryRunner:
    return SubprocessCommandEntryRunner(workspace_root=_ROOT, error_factory=error_factory)


def test_run_tokenizes_via_shlex_split_by_default(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="secret\n", stderr="")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    output = runner.run("vals get ref+vault://db", shell=False, env={"DB": "x"}, description="entry 'DB_PASSWORD'")

    args, kwargs = fake_subprocess.run.call_args
    assert args[0] == ["vals", "get", "ref+vault://db"]
    assert kwargs["cwd"] == str(_ROOT)
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["env"]["DB"] == "x"
    # The accumulated scope is merged over this process's own environment, not
    # a replacement of it — PATH (or any other inherited var) survives.
    assert "PATH" in kwargs["env"] or len(kwargs["env"]) > 1
    assert output == "secret\n"


def test_run_passes_the_raw_string_through_a_shell_when_shell_true(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    runner.run("echo hi | tr a-z A-Z", shell=True, env={}, description="entry 'X'")

    args, kwargs = fake_subprocess.run.call_args
    assert args[0] == "echo hi | tr a-z A-Z"
    assert kwargs["shell"] is True


def test_run_merges_scope_over_the_adapters_own_environment(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    monkeypatch.setenv("EXISTING_VAR", "already-here")
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    runner.run("echo hi", shell=False, env={"DB_PASSWORD": "hunter2"}, description="entry 'X'")

    env = fake_subprocess.run.call_args.kwargs["env"]
    assert env["EXISTING_VAR"] == "already-here"
    assert env["DB_PASSWORD"] == "hunter2"


def test_run_raises_repo_error_naming_entry_command_and_exit_code_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(
        args=["vals", "get", "ref+vault://db"], returncode=1, stdout="", stderr="no such secret"
    )
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run("vals get ref+vault://db", shell=False, env={}, description="env.feature.vars key 'DB_PASSWORD'")

    err = excinfo.value
    assert "env.feature.vars key 'DB_PASSWORD'" in str(err)
    assert "vals get ref+vault://db" in str(err)
    assert err.exit_code == 1
    assert "no such secret" in str(err)
    # Names the program it actually ran, not "git".
    assert "$ vals get ref+vault://db" in str(err)


def test_run_raises_repo_error_not_a_bare_value_error_on_unbalanced_quotes(
    runner: SubprocessCommandEntryRunner,
) -> None:
    """`shlex.split` runs inside its own guarded region, so a quoting error is a `RepoError`.

    A bare `ValueError` here would be caught by `provision_scope_env`'s
    `except ValueError` and silently degrade to no env injected at all — the
    opposite of this seam's fail-loud contract. Real `shlex.split`, no mock.
    """
    with pytest.raises(RepoError) as excinfo:
        runner.run('vals get "unbalanced', shell=False, env={}, description="env.feature.vars key 'DB_PASSWORD'")

    message = str(excinfo.value)
    assert "env.feature.vars key 'DB_PASSWORD'" in message
    assert 'vals get "unbalanced' in message


def test_run_names_the_declared_command_not_the_rendered_one_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    """A rendered command may carry a value substituted in from another entry's
    output; the failure message and the RepoError's structured fields must name the
    entry's declared (unrendered) command instead, or they leak that value to stderr
    and the error log.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(
        args=["vault", "kv", "get", "-field=pw", "hunter2-token"], returncode=1, stdout="", stderr="denied"
    )
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run(
            "vault kv get -field=pw hunter2-token",
            shell=False,
            env={},
            description="env.feature.vars key 'PW'",
            declared_command="vault kv get -field=pw ${TOKEN}",
        )

    rendered = str(excinfo.value)
    assert "hunter2-token" not in rendered
    assert "vault kv get -field=pw ${TOKEN}" in rendered
    assert excinfo.value.program == "vault"
    assert excinfo.value.subcommand == "kv"


def test_run_raises_repo_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    fake_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd="slow-cmd", timeout=30.0)
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run("slow-cmd", shell=False, env={}, description="env.feature.vars key 'SLOW'")

    assert "timed out" in str(excinfo.value)
    assert "env.feature.vars key 'SLOW'" in str(excinfo.value)


def test_run_raises_repo_error_on_oserror(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    fake_subprocess = MagicMock()
    # The except clauses check the raised exception against `subprocess.TimeoutExpired`
    # before `OSError`, so it must resolve to a real exception class even though
    # this test raises the other one.
    fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    fake_subprocess.run.side_effect = OSError("no such file or directory")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run("does-not-exist", shell=False, env={}, description="env.feature.vars key 'X'")

    assert "no such file or directory" in str(excinfo.value)


def test_timeout_repo_error_stderr_does_not_leak_the_rendered_command(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    """`str(TimeoutExpired)` embeds `exc.cmd` — the *rendered* command, which may carry
    a value substituted in from another entry's output. Every failure mode's message
    already names the *declared* command; the `RepoError.stderr` field must not
    separately leak the rendered one back out.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    fake_subprocess.run.side_effect = subprocess.TimeoutExpired(
        cmd="secret-tool --pw=hunter2-SECRET; sleep 5", timeout=0.3
    )
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run(
            "secret-tool --pw=hunter2-SECRET; sleep 5",
            shell=True,
            env={},
            description="env.feature.vars key 'MIGRATE'",
            declared_command="secret-tool --pw=${PW}; sleep 5",
        )

    err = excinfo.value
    assert "hunter2-SECRET" not in err.stderr
    assert "hunter2-SECRET" not in str(err)
    assert "secret-tool --pw=${PW}; sleep 5" in str(err)


def test_oserror_repo_error_stderr_does_not_leak_the_rendered_program_name(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    """`str(FileNotFoundError)` embeds the rendered, resolved program name (its
    `filename` attribute) alongside `strerror` — a lesser instance of the same leak
    as the timeout arm above. `strerror` alone carries no path.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    fake_subprocess.run.side_effect = FileNotFoundError(2, "No such file or directory", "hunter2-rendered-program")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    with pytest.raises(RepoError) as excinfo:
        runner.run(
            "hunter2-rendered-program --go",
            shell=False,
            env={},
            description="entry 'X'",
            declared_command="${PROGRAM} --go",
        )

    err = excinfo.value
    assert "hunter2-rendered-program" not in err.stderr
    assert "hunter2-rendered-program" not in str(err)


def test_run_passes_devnull_stdin_so_a_prompting_child_cannot_hang_on_the_parents_terminal(
    monkeypatch: pytest.MonkeyPatch, runner: SubprocessCommandEntryRunner
) -> None:
    """`capture_output=True` hides any prompt a child writes; without `stdin=DEVNULL` an
    interactive tool reading stdin (e.g. an unlocked secrets CLI) could block on the
    parent's own terminal instead of getting immediate EOF from a closed pipe.
    """
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess_command_entry_runner, "subprocess", fake_subprocess)

    runner.run("op read secret", shell=False, env={}, description="entry 'X'")

    assert fake_subprocess.run.call_args.kwargs["stdin"] == fake_subprocess.DEVNULL


def test_empty_rendered_command_raises_repo_error_not_a_bare_exception(
    runner: SubprocessCommandEntryRunner,
) -> None:
    """An entry like ``command = "${X}"`` with ``X = ""`` renders to an empty string.
    `shlex.split` tokenizes that to `[]`, and `subprocess.run([])` raises a bare
    exception unless this seam rejects the empty token list explicitly through the
    `RepoError` path first. Real `shlex.split`/no mock, like the unbalanced-quotes case.
    """
    with pytest.raises(RepoError) as excinfo:
        runner.run("", shell=False, env={}, description="env.feature.vars key 'EMPTY'")

    assert "env.feature.vars key 'EMPTY'" in str(excinfo.value)


def test_undecodable_stdout_is_not_misattributed_to_tokenizing() -> None:
    """Stdout that is not valid UTF-8 raises `UnicodeDecodeError` — a `ValueError`
    subclass — from `subprocess.run`'s own text decoding, which happens *after* the
    command already tokenized and ran. It must be attributed to decoding, not folded
    into the tokenizing failure path just because both raise `ValueError` subclasses.

    Real `subprocess.run`, no mock — a mocked one cannot itself raise a decode error.
    """
    real_runner = SubprocessCommandEntryRunner(workspace_root=Path.cwd(), error_factory=RepoErrorFactory())

    with pytest.raises(RepoError) as excinfo:
        real_runner.run(
            """python3 -c "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')" """,
            shell=False,
            env={},
            description="env.feature.vars key 'BIN'",
        )

    message = str(excinfo.value)
    assert "could not be tokenized" not in message
    assert "could not be decoded" in message


def test_run_real_subprocess_captures_stdout() -> None:
    """Real subprocess, no mock — pins the adapter against the real `subprocess.run` signature."""
    real_runner = SubprocessCommandEntryRunner(workspace_root=Path.cwd(), error_factory=RepoErrorFactory())

    output = real_runner.run("echo hello-from-real-subprocess", shell=False, env={}, description="entry 'X'")

    assert output.strip() == "hello-from-real-subprocess"


class TestRealSubprocessBehavior:
    """The adapter driven against real processes, with `subprocess` unmocked.

    A mocked `subprocess` can only assert that the adapter passes the arguments
    the test already believes it passes; it cannot show that the resulting
    process behaves as the adapter's execution contract requires. These tests run
    real commands and read the observable result instead: where the child's cwd
    landed, what its environment held, and whether a shell interpreted the
    command line.
    """

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        (tmp_path / "marker.txt").write_text("i am the workspace root\n")
        return tmp_path

    @pytest.fixture
    def real_runner(self, root: Path) -> SubprocessCommandEntryRunner:
        return SubprocessCommandEntryRunner(workspace_root=root, error_factory=RepoErrorFactory())

    def test_child_cwd_is_the_workspace_root(self, real_runner: SubprocessCommandEntryRunner, root: Path) -> None:
        assert real_runner.run("pwd", shell=False, env={}, description="d").strip() == str(root)
        # A relative path in the command resolves against that root.
        assert "workspace root" in real_runner.run("cat marker.txt", shell=False, env={}, description="d")

    def test_scope_reaches_the_child_as_an_ordinary_env_var(self, real_runner: SubprocessCommandEntryRunner) -> None:
        out = real_runner.run("printenv DB_TIER", shell=False, env={"DB_TIER": "qa"}, description="d")
        assert out.strip() == "qa"

    def test_scope_overrides_the_parent_environment_but_the_rest_survives(
        self, real_runner: SubprocessCommandEntryRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLLIDING_VAR", "from-parent")
        monkeypatch.setenv("PARENT_ONLY_VAR", "from-parent")
        out = real_runner.run(
            "printenv COLLIDING_VAR PARENT_ONLY_VAR", shell=False, env={"COLLIDING_VAR": "from-scope"}, description="d"
        )
        assert out.split() == ["from-scope", "from-parent"]

    def test_shlex_split_keeps_a_quoted_argument_containing_spaces_whole(
        self, real_runner: SubprocessCommandEntryRunner
    ) -> None:
        """Naive `.split()` would hand the child three arguments where the operator wrote one."""
        out = real_runner.run(
            """python3 -c 'import sys; print(len(sys.argv) - 1); print(sys.argv[1])' 'a b c'""",
            shell=False,
            env={},
            description="d",
        )
        assert out.splitlines() == ["1", "a b c"]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo one ; echo two", "one ; echo two"),
            ("echo $(id -u)", "$(id -u)"),
            ("echo $DB_TIER", "$DB_TIER"),
            ("echo a && echo b", "a && echo b"),
        ],
    )
    def test_shell_metacharacters_are_not_interpreted_without_the_opt_in(
        self, real_runner: SubprocessCommandEntryRunner, command: str, expected: str
    ) -> None:
        """No shell by default: metacharacters reach the program as literal argument text."""
        out = real_runner.run(command, shell=False, env={"DB_TIER": "qa"}, description="d")
        assert out.strip() == expected

    def test_shell_true_really_gets_a_shell(self, real_runner: SubprocessCommandEntryRunner) -> None:
        assert real_runner.run("echo one ; echo two", shell=True, env={}, description="d").split() == ["one", "two"]
        out = real_runner.run("echo $DB_TIER", shell=True, env={"DB_TIER": "qa"}, description="d")
        assert out.strip() == "qa"

    def test_non_zero_exit_of_a_real_process_names_entry_command_and_exit_code(
        self, real_runner: SubprocessCommandEntryRunner
    ) -> None:
        with pytest.raises(RepoError) as excinfo:
            real_runner.run(
                """python3 -c 'import sys; sys.stderr.write("no such secret\\n"); sys.exit(7)'""",
                shell=False,
                env={},
                description="env.feature.vars key 'DB_PASSWORD'",
            )
        rendered = str(excinfo.value)
        assert "env.feature.vars key 'DB_PASSWORD'" in rendered
        assert "python3" in rendered
        assert "exit 7" in rendered
        assert excinfo.value.exit_code == 7
        assert "no such secret" in rendered

    def test_a_missing_program_surfaces_as_repo_error_not_a_raw_oserror(
        self, real_runner: SubprocessCommandEntryRunner
    ) -> None:
        with pytest.raises(RepoError) as excinfo:
            real_runner.run("winter-no-such-program-xyz --go", shell=False, env={}, description="entry 'X'")
        assert "winter-no-such-program-xyz" in str(excinfo.value)
        assert "entry 'X'" in str(excinfo.value)

    def test_a_hanging_command_really_times_out_and_names_entry_command_and_exit_code(
        self, real_runner: SubprocessCommandEntryRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real hanging process, so the timeout is observed rather than simulated.

        The budget is shortened for the test's own runtime; that the adapter
        passes `_TIMEOUT_S` to `subprocess.run` at all is what makes shortening
        it work, and the declared value is pinned separately below.
        """
        monkeypatch.setattr(subprocess_command_entry_runner, "_TIMEOUT_S", 1.0)
        with pytest.raises(RepoError) as excinfo:
            real_runner.run("sleep 30", shell=False, env={}, description="env.feature.vars key 'HANGS'")
        rendered = str(excinfo.value)
        assert "env.feature.vars key 'HANGS'" in rendered
        assert "sleep 30" in rendered
        assert "timed out" in rendered
        # A timeout names an exit code like the other two failure modes: the
        # child really is SIGKILLed by the timing-out `subprocess.run`.
        assert excinfo.value.exit_code == subprocess_command_entry_runner._TIMEOUT_EXIT_CODE
        assert "killed by signal 9" in rendered

    def test_declared_timeout_budget_is_thirty_seconds(self) -> None:
        assert subprocess_command_entry_runner._TIMEOUT_S == 30.0

    def test_a_child_reading_stdin_gets_immediate_eof_not_the_parents_terminal(
        self, real_runner: SubprocessCommandEntryRunner
    ) -> None:
        """A real process reading stdin sees immediate EOF rather than blocking — the
        observable effect of `stdin=DEVNULL`, confirmed against a real child."""
        out = real_runner.run("cat", shell=False, env={}, description="d")
        assert out == ""
