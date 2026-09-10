"""`subprocess` adapter for `ICommandEntryRunner`. All `subprocess` usage for
command-band entries is confined here.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from winter_cli.modules.workspace.command_entry_runner import ICommandEntryRunner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory

_TIMEOUT_S = 30.0
"""Wall-clock budget for one command-band entry.

A module-level constant rather than a config knob: an `EnvCommandEntry`'s
field set is closed, and a per-entry timeout is not in it.
"""


_TIMEOUT_EXIT_CODE = -signal.SIGKILL
"""Exit status a timed-out entry reports.

`subprocess.run` kills the child with `SIGKILL` when its timeout elapses, so
the process really does end with that status — `subprocess` just raises
`TimeoutExpired` instead of returning it. Reporting it keeps a timeout naming
an exit code, as the two other command-failure modes do, and
`RepoError.__str__` renders a negative status as `killed by signal N`.
"""


def _sanitized_timeout_cause(exc: subprocess.TimeoutExpired) -> str:
    """A `TimeoutExpired`'s own message without the rendered command it embeds.

    `str(exc)` interpolates `exc.cmd` — the *rendered* command line actually
    executed, which may carry a value substituted in from another entry's
    output (e.g. a secret). The caller's own message already names the
    entry's *declared* command, so this reports only the budget that elapsed.
    """
    return f"timed out after {exc.timeout}s"


def _sanitized_oserror_cause(exc: OSError) -> str:
    """An `OSError`'s own message without a rendered value it may embed.

    `str(exc)` on a real `FileNotFoundError` interpolates `exc.filename` —
    here, the resolved (rendered) program name — alongside `strerror`.
    `strerror` alone is the OS-level message with no path in it.
    """
    return exc.strerror or str(exc)


class SubprocessCommandEntryRunner:
    """`subprocess` implementation of `ICommandEntryRunner`.

    Owns cwd (fixed at the workspace root for every entry), merging the
    caller's accumulated scope over this process's own environment (so the
    child can still resolve the program on `PATH`), `shlex.split` tokenizing
    by default, the `shell = true` opt-in, and the fixed execution timeout.
    Wraps a non-zero exit via the injected `RepoErrorFactory`; tokenizing,
    execution, and decoding are each wrapped separately so a failure in one
    is never attributed to another — an empty or malformed rendered command,
    a timeout, a decode failure, and an `OSError` (e.g. program not found)
    each raise `RepoError` naming the failure that actually happened, rather
    than one escaping as a bare exception or being misattributed to a
    different stage.

    Stdin is `DEVNULL`: `capture_output=True` hides any prompt a child might
    write, and a tool that prompts by reading *stdin* (an unlock prompt, a
    passphrase read) gets immediate EOF from the closed pipe rather than
    hanging on the parent's terminal — though EOF can just as easily let the
    tool continue and resolve the entry to an empty value at exit 0 as it can
    fail the tool outright; closed stdin does not reliably fail either way. A
    tool that instead opens `/dev/tty` directly (as some secrets-manager
    prompts do) bypasses the closed stdin entirely: with no controlling
    terminal, that open fails immediately (`ENXIO`) rather than hanging, but
    under `shell = true` a failed `/dev/tty` redirect can let the shell
    continue anyway, again landing on an empty or truncated value at exit 0
    instead of failing the command. Neither prompt style reliably hangs or
    fails; the risk this leaves is a silently empty or truncated resolved
    value, not an invisible hang.

    Every message this adapter raises names *declared_command*
    (`ICommandEntryRunner.run`'s unrendered form), never the rendered
    *command* it actually executes — the latter may carry another entry's
    command-derived value substituted in, and echoing it back on failure
    would leak that value to stderr and the error log. The same holds for the
    underlying exception's own text: a `TimeoutExpired` or `OSError` can
    itself embed the rendered command or program name, so this adapter builds
    its `RepoError.stderr` from a sanitized cause rather than passing the
    exception's `str()` straight through.
    """

    def __init__(self, workspace_root: Path, error_factory: RepoErrorFactory) -> None:
        self._workspace_root = workspace_root
        self._errors = error_factory

    def run(
        self,
        command: str,
        *,
        shell: bool,
        env: Mapping[str, str],
        description: str,
        declared_command: str | None = None,
    ) -> str:
        display_command = declared_command if declared_command is not None else command
        child_env = os.environ.copy()
        child_env.update(env)

        cmd: list[str] | str = command
        if not shell:
            try:
                cmd = shlex.split(command)
            except ValueError as exc:
                raise self._errors.from_exception(
                    exc,
                    f"{description}: command `{display_command}` could not be tokenized",
                    cwd=self._workspace_root,
                ) from exc
            if not cmd:
                raise self._errors.from_exception(
                    ValueError("command rendered to no tokens"),
                    f"{description}: command `{display_command}` rendered to an empty command line",
                    cwd=self._workspace_root,
                    stderr="",
                )

        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self._workspace_root),
                env=child_env,
                shell=shell,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise self._errors.from_exception(
                exc,
                f"{description}: command `{display_command}` timed out after {_TIMEOUT_S}s",
                cwd=self._workspace_root,
                exit_code=_TIMEOUT_EXIT_CODE,
                stderr=_sanitized_timeout_cause(exc),
            ) from exc
        except UnicodeDecodeError as exc:
            raise self._errors.from_exception(
                exc,
                f"{description}: command `{display_command}` output could not be decoded as text",
                cwd=self._workspace_root,
            ) from exc
        except OSError as exc:
            raise self._errors.from_exception(
                exc,
                f"{description}: command `{display_command}` could not be run",
                cwd=self._workspace_root,
                stderr=_sanitized_oserror_cause(exc),
            ) from exc
        if completed.returncode != 0:
            raise self._errors.from_subprocess(
                completed,
                f"{description}: command `{display_command}` failed",
                cwd=self._workspace_root,
                declared_command=display_command,
            )
        return completed.stdout


def _conforms_subprocess_command_entry_runner(x: SubprocessCommandEntryRunner) -> ICommandEntryRunner:
    return x
