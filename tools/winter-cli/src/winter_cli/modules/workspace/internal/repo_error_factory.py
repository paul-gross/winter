from __future__ import annotations

import logging
from pathlib import Path

import git

from winter_cli.modules.workspace.models import RepoError


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
        stderr = stderr_raw.strip()
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
