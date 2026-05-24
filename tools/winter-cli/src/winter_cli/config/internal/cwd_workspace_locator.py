from __future__ import annotations

from pathlib import Path

from winter_cli.config.workspace_locator import IWorkspaceLocator

WINTER_DIR = ".winter"


class CwdWorkspaceLocator:
    """Finds the workspace root by walking up from `Path.cwd()` for a `.winter/` directory.

    The only IWorkspaceLocator adapter in production. Confines `Path.cwd()`
    to this file so service code never reaches the filesystem implicitly.
    """

    def find_workspace_root(self) -> Path:
        current = Path.cwd()
        for directory in [current, *current.parents]:
            if (directory / WINTER_DIR).is_dir():
                return directory
        raise RuntimeError(
            f"Could not find workspace root from {current}. Expected to find a {WINTER_DIR}/ directory in a parent."
        )


def _conforms_cwd_workspace_locator(x: CwdWorkspaceLocator) -> IWorkspaceLocator:
    return x
