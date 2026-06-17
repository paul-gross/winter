from __future__ import annotations

import sys
from pathlib import Path

from winter_cli.modules.lint.models import LintScope

# Env var names handed to every contributed lint script so it can see what
# slice of the workspace to lint. Kept here so the workspace and extension
# services emit an identical contract.
SCOPE_KIND_VAR = "WINTER_LINT_SCOPE"
SCOPE_PATHS_VAR = "WINTER_LINT_PATHS"

# Path to the winter CLI that launched this run, handed to every lint script so
# it can call back into the same binary (e.g. `$WINTER_CLI graph --json`) rather
# than re-deriving workspace-wide data itself. A check that needs it but finds
# it unset should fail loudly — there is no degraded mode.
WINTER_CLI_VAR = "WINTER_CLI"


def resolve_winter_cli_path() -> str:
    """Absolute path to the winter CLI entry point invoked for this process.

    Uses `argv[0]` — the exact executable that launched this run — so a lint
    script calls back into the same CLI that dispatched it, regardless of which
    copy is on PATH. Run with cwd at the workspace root, that callback discovers
    the same workspace.
    """
    return str(Path(sys.argv[0]).resolve())


def lint_scope_env(scope: LintScope) -> dict[str, str]:
    """The `WINTER_LINT_*` env vars describing `scope` to a lint script.

    `WINTER_LINT_SCOPE` is the scope kind (`all` / `repo` / `env` / `changed`);
    `WINTER_LINT_PATHS` is the newline-delimited absolute paths in scope (repo
    or env directories, the workspace root, or the individual changed files).

    Note on empty `WINTER_LINT_PATHS`: a ``--changed`` run with no changed
    files is short-circuited by the handler before reaching here, so lint
    scripts are never invoked with an empty path list. Scripts must not rely
    on this as a guarantee for non-changed scopes (e.g. ``--all`` always
    passes at least the workspace root even when no repos are configured).
    """
    return {
        SCOPE_KIND_VAR: scope.kind.value,
        SCOPE_PATHS_VAR: "\n".join(str(p) for p in scope.paths),
    }
