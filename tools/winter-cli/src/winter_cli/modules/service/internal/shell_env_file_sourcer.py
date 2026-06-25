"""Shell-based implementation of IEnvFileSourcer.

Sources the per-scope winter env file in a minimal bash environment and
returns the variables the file defines.  Arithmetic in the env file is
evaluated by the shell before the variables are captured (e.g.
``WTS_DB_PORT=$((WINTER_PORT_BASE+12))`` is expanded rather than returned
verbatim).  This matches the sourcing semantics of both winter-service-docker
(which uses ``set -a; . "$FILE"; set +a; exec docker compose ...``) and
winter-service-tmux (which sources the same file for each service pane).

No-ambient-leak contract
------------------------
The subprocess starts with an intentionally empty environment (``env={}``)
so only the variables set by the sourcing script appear in the printed output.
The caller's ``os.environ`` is *not* passed as a base, so variables the file
doesn't define can never leak into the returned dict.  A small set of
bash-internal variables that bash sets even in an empty environment (``PWD``,
``SHLVL``, ``_``) are stripped from the output before returning.

Newline-safe parsing
--------------------
Values may contain embedded newlines (e.g. multi-line PEM keys).  The
``env`` command line-splits output, which silently truncates such values.
Instead the script uses ``env -0`` (NUL-delimited output) so each
``KEY=VALUE`` record is terminated by ``\\x00`` and may span multiple lines
intact.  This achieves byte-parity with how providers source the same file
(``set -a; . "$FILE"``), where a multi-line assignment is fully preserved.

OSError resilience
------------------
If ``bash`` is absent or unreadable, ``subprocess.run`` raises ``OSError``
before the process starts.  That exception is caught here and re-raised as
``EnvFileSourcerError`` so ``run_matrix``'s per-scope degradation contract
applies uniformly — no raw ``OSError`` escapes to crash the whole status run.

File selection (mirrors ``docker_orchestrator.env_context.resolve_env_file``):
    scope == "workspace"  →  ``<ws_root>/.winter.workspace.env``
    any other scope       →  ``<ws_root>/<scope>/.winter.env``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from winter_cli.modules.service.env_file_sourcer import EnvFileSourcerError, IEnvFileSourcer
from winter_cli.modules.service.scope import WORKSPACE_SCOPE

_ENV_FILE = ".winter.env"
_WORKSPACE_ENV_FILE = ".winter.workspace.env"

# Variables that bash sets in every session even when started with an empty
# environment.  These are never from the env file and must be excluded from
# the returned dict so they don't accidentally override provider-env vars.
_BASH_INTERNALS: frozenset[str] = frozenset({"PWD", "SHLVL", "_"})

# Shell script: source the file with allexport and errexit on, then emit the
# environment NUL-delimited via ``env -0`` so values containing newlines are
# preserved intact.  ``set -ea`` combines allexport (every assignment is
# automatically exported) and errexit (abort on first error, including
# source-command parse errors).  The env-file path arrives as ``$1`` (the
# script's first positional parameter) so no shell-quoting of the path is
# needed and the invocation is safe against paths with spaces.
_SOURCE_SCRIPT = 'set -ea; . "$1"; set +a; env -0'


def _env_file_path(scope: str, ws_root: Path) -> Path:
    if scope == WORKSPACE_SCOPE:
        return ws_root / _WORKSPACE_ENV_FILE
    return ws_root / scope / _ENV_FILE


def _parse_env_output(text: str) -> dict[str, str]:
    """Parse NUL-delimited ``KEY=VALUE`` records from the ``env -0`` output.

    Each record is terminated by ``\\x00`` and may contain embedded newlines
    in the value portion.  Records that do not contain ``=`` are skipped.
    Known bash-internal variables (``PWD``, ``SHLVL``, ``_``) are excluded
    since they are set by bash itself, not by the env file.
    """
    result: dict[str, str] = {}
    for record in text.split("\x00"):
        if not record or "=" not in record:
            continue
        key, _, value = record.partition("=")
        if key in _BASH_INTERNALS:
            continue
        result[key] = value
    return result


class ShellEnvFileSourcer:
    """Shell-sources the per-scope winter env file and returns its variables.

    All ``subprocess`` usage is confined to this class.  Tests may substitute
    a fake ``IEnvFileSourcer`` at the Protocol seam without needing to mock
    subprocess.
    """

    def source(self, scope: str, ws_root: Path) -> dict[str, str]:
        """Return the variables defined by the scope's winter env file.

        Returns an empty dict when the file is absent.  Raises
        ``EnvFileSourcerError`` when the sourcing subprocess exits non-zero
        (e.g. syntax error in the env file) or when an ``OSError`` is raised
        (e.g. ``bash`` not found or unreadable env file path), preserving
        the original exception as the cause.
        """
        env_file = _env_file_path(scope, ws_root)
        if not env_file.exists():
            return {}

        try:
            completed = subprocess.run(
                ["bash", "-c", _SOURCE_SCRIPT, "bash", str(env_file)],
                env={},
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise EnvFileSourcerError(
                f"env-file sourcing failed for scope {scope!r}: {exc}",
                exit_code=1,
                stderr=str(exc),
            ) from exc

        if completed.returncode != 0:
            raise EnvFileSourcerError(
                f"env-file sourcing failed for scope {scope!r} (exit {completed.returncode}): {env_file}",
                exit_code=completed.returncode,
                stderr=completed.stderr or "",
            )
        return _parse_env_output(completed.stdout or "")


def _conforms_shell_env_file_sourcer(x: ShellEnvFileSourcer) -> IEnvFileSourcer:
    return x
