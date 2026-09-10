from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import git

from winter_cli.modules.workspace.models import RepoError

if TYPE_CHECKING:
    import subprocess

_GITPYTHON_STREAM_RE = re.compile(r"^\s*std(?:out|err):\s*'(.*)'\s*$", re.DOTALL)


def unwrap_gitpython_stream(raw: str) -> str:
    """The raw text out of GitPython's decorated `GitCommandError.stdout`/`.stderr`.

    `CommandError.__init__` stores each stream as `"\\n  stdout: '<text>'"` (or
    `stderr:`) rather than as the text itself, so a caller reading `.stderr`
    directly gets that label wrapped around the real message — doubling up
    when `RepoError.__str__` prepends its own `stderr:` label. Returns `raw`
    unchanged when it carries no decoration, so this is safe on an
    already-plain string.
    """
    match = _GITPYTHON_STREAM_RE.match(raw)
    return match.group(1) if match else raw


def _split_for_display(command: str) -> tuple[str, str | None, tuple[str, ...]]:
    """Best-effort split of a declared (unrendered) command line for display only.

    Never raises: a *declared_command* the operator wrote may not be valid
    shell/`shlex` syntax on its own terms (it can still contain unresolved
    ``${...}`` tokens), and this is display formatting, not execution — a
    malformed line should still render as *something* readable rather than
    blow up the error path that is reporting a failure.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return command, None, ()
    program = tokens[0]
    subcommand = tokens[1] if len(tokens) > 1 else None
    cmd_args = tuple(tokens[2:]) if len(tokens) > 2 else ()
    return program, subcommand, cmd_args


class RepoErrorFactory:
    """Builds structured `RepoError` instances from GitPython exceptions.

    Owns the translation from `git.GitCommandError` into the winter-defined
    error type plus the structured-context logging at the wrap site. Injected
    into every repository class so they can raise without importing the
    conversion helper directly — keeps the boundary between GitPython and
    winter's exception hierarchy testable and swappable.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def from_exception(
        self,
        exc: Exception,
        message: str,
        *,
        cwd: Path | str,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> RepoError:
        """Wrap a non-`GitCommandError` GitPython exception and log it.

        `from_git` reads `command` / `status` / `stderr` off a failed git
        invocation; the constructor-level failures (`NoSuchPathError`,
        `InvalidGitRepositoryError`) carry none of those because no git
        process ever ran. They still need to reach callers as `RepoError`
        rather than as a raw GitPython traceback, so they are wrapped here
        with the exception text as `stderr` by default.

        `stderr`, when given, overrides `str(exc)` as the `RepoError.stderr`
        field. A caller whose *exc* stringifies to something that can carry a
        secret or another entry's rendered command line (e.g.
        `subprocess.TimeoutExpired.__str__` embeds `exc.cmd`) passes its own
        sanitized text here instead, so that value never reaches stderr or
        the error log this method also writes.

        `exit_code` defaults to `None` — the shape every such
        no-process-ever-ran failure keeps. A caller that *did* start a process
        and then lost it (a timeout, whose child the timing-out `run` kills)
        passes the status that killing produced, so the failure still renders
        an exit line.
        """
        cwd_str = str(cwd)
        err = RepoError(message, cwd=cwd_str, exit_code=exit_code, stderr=stderr if stderr is not None else str(exc))
        self._logger.error("%s — %s (cwd=%s exit=%s)", message, type(exc).__name__, cwd_str, exit_code)
        return err

    def from_git(
        self,
        exc: git.GitCommandError,
        message: str,
        *,
        cwd: Path | str,
    ) -> RepoError:
        """Wrap `exc` into a structured `RepoError` and log it.

        Extracts subcommand / args / exit code / stderr from `exc` so the
        dashboard's Log tab and the CLI's error messages can render a
        readable `$ git <subcommand> <args>` line alongside the underlying
        stderr. Logs at ERROR with the structured context before returning,
        so the boundary that transforms the exception is also the place it
        gets recorded — no catch-log-rethrow ladder at higher layers.
        """
        command = list(exc.command) if exc.command else []
        subcommand = command[1] if len(command) > 1 else None
        cmd_args = tuple(str(a) for a in command[2:]) if len(command) > 2 else ()
        stderr_raw = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr = unwrap_gitpython_stream(stderr_raw).strip()
        cwd_str = str(cwd)
        err = RepoError(
            message,
            subcommand=subcommand,
            cmd_args=cmd_args,
            cwd=cwd_str,
            exit_code=getattr(exc, "status", None),
            stderr=stderr,
        )
        self._logger.error(
            "%s — git %s %s (cwd=%s exit=%s) %s",
            message,
            subcommand or "",
            " ".join(cmd_args),
            cwd_str,
            err.exit_code,
            stderr,
        )
        return err

    def from_subprocess(
        self,
        completed: subprocess.CompletedProcess[str],
        message: str,
        *,
        cwd: Path | str,
        declared_command: str | None = None,
    ) -> RepoError:
        """Wrap a failed `subprocess.CompletedProcess` into a structured `RepoError` and log it.

        The canonical shape for a non-git adapter wrapping raw `subprocess`
        (`winter-context:/architecture/subprocess.md`). Without
        *declared_command*, `completed.args` is either the `list[str]` passed
        to `subprocess.run` (the default, non-shell form) or the raw command
        string (`shell=True`): the former decomposes into `program` /
        `subcommand` / `cmd_args` the same way `from_git` decomposes a git
        command line; the latter has no subcommand to split out, so
        `RepoError.__str__` renders no `$ ...` line for it — the caller's
        `message` is expected to already name the command it ran.

        *declared_command*, when given, overrides `completed.args` as the
        source for `program` / `subcommand` / `cmd_args`. A caller whose
        `completed.args` may carry a value substituted in from another
        entry's output (a command-band entry's rendered command line) passes
        the entry's declared, unrendered form here instead, so the structured
        fields — and the `$ ...` line and log record built from them — never
        render a value some other command produced.
        """
        if declared_command is not None:
            program, subcommand, cmd_args = _split_for_display(declared_command)
        else:
            args = completed.args
            if isinstance(args, str):
                program, subcommand, cmd_args = args, None, ()
            else:
                tokens = [str(a) for a in args]
                program = tokens[0] if tokens else ""
                subcommand = tokens[1] if len(tokens) > 1 else None
                cmd_args = tuple(tokens[2:]) if len(tokens) > 2 else ()
        stderr = (completed.stderr or "").strip()
        cwd_str = str(cwd)
        err = RepoError(
            message,
            program=program,
            subcommand=subcommand,
            cmd_args=cmd_args,
            cwd=cwd_str,
            exit_code=completed.returncode,
            stderr=stderr,
        )
        self._logger.error(
            "%s — %s %s %s (cwd=%s exit=%s) %s",
            message,
            program,
            subcommand or "",
            " ".join(cmd_args),
            cwd_str,
            err.exit_code,
            stderr,
        )
        return err
