"""IEnvFileSourcer — Protocol for sourcing per-scope winter env files.

The sourcer shell-evaluates the env file for a given scope and returns the
variables the file defines.  Shell evaluation is required because env files
may carry arithmetic expressions (e.g. ``WTS_DB_PORT=$((WINTER_PORT_BASE+12))``),
and those must be evaluated exactly as the providers do today (via ``source``
or ``. <file>``).

Contract
--------
- **File selection**:
    - A concrete env name (e.g. ``"alpha"``) → ``<ws_root>/<env>/.winter.env``
    - The literal string ``"workspace"`` → ``<ws_root>/.winter.workspace.env``
- **Absent file** → empty dict (not an error).
- **Return value** → only the variables defined (or derived via arithmetic) by
  the env file itself.  The caller's ambient environment is NOT included in the
  returned dict.  This "no-leak" contract is enforced by sourcing into a minimal
  shell environment and emitting only the variables set by the file.
- **Shell failure** (non-zero exit from the sourcing subprocess) → raises
  ``EnvFileSourcerError`` so callers can handle it specifically without
  importing subprocess types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class EnvFileSourcerError(Exception):
    """Raised when the env-file sourcing subprocess exits non-zero.

    Callers catch this specifically to distinguish a shell failure (e.g. syntax
    error in the env file) from an absent file (empty dict, no exception).
    """

    def __init__(self, message: str, *, exit_code: int, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class IEnvFileSourcer(Protocol):
    """Shell-source the per-scope winter env file and return its variables.

    The returned dict contains ONLY the variables that the env file defines
    (or derives via shell arithmetic).  The caller's ambient environment is
    never included — variables that the file does not set are absent from the
    result.

    Absent file → empty dict (not an error).
    Shell failure → ``EnvFileSourcerError`` with the exit code and stderr.
    """

    def source(self, scope: str, ws_root: Path) -> dict[str, str]: ...
