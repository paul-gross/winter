from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import (
    AdoptExtensions,
    GitIdentity,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.config.workspace_locator import IWorkspaceLocator
from winter_cli.core.config_file import IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.util import deep_merge

WINTER_DIR = ".winter"
CONFIG_FILE = "config.toml"
LOCAL_CONFIG_FILE = "config.local.toml"


class WorkspaceConfigService:
    """Loads `.winter/config.toml` (+ optional local overlay) into a WorkspaceConfig.

    Depends on Protocol seams for I/O: `IWorkspaceLocator` for root discovery,
    `IConfigFileReader` for TOML parsing, and `IFilesystemReader` for the
    singleton-detection probes (`product/`, `ai/harness/.git`).
    """

    def __init__(
        self,
        workspace_locator: IWorkspaceLocator,
        fs: IFilesystemReader,
        config_file_reader: IConfigFileReader,
    ) -> None:
        self._workspace_locator = workspace_locator
        self._fs = fs
        self._config_file_reader = config_file_reader

    def load(self) -> WorkspaceConfig:
        workspace_root = self._workspace_locator.find_workspace_root()
        raw = self._read_config(workspace_root / WINTER_DIR / CONFIG_FILE)
        overlay = self._read_config(workspace_root / WINTER_DIR / LOCAL_CONFIG_FILE)
        merged = deep_merge(raw, overlay)

        singletons: list[SingletonRepository] = [
            SingletonRepository(name=workspace_root.name, type=SingletonType.workspace),
        ]
        if self._fs.is_dir(workspace_root / "product"):
            singletons.append(SingletonRepository(name="product", type=SingletonType.product))
        if self._fs.exists(workspace_root / "ai" / "harness" / ".git"):
            singletons.append(SingletonRepository(name="harness", type=SingletonType.harness))

        project_repos: list[ProjectRepositoryConfig] = []
        for entry in merged.get("project_repository", []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            url = entry.get("url")
            if not name and not url:
                continue
            project_repos.append(
                ProjectRepositoryConfig(
                    name=name,
                    url=url,
                    main_branch=entry.get("main_branch"),
                    pinned=bool(entry.get("pinned", False)),
                    git_excludes=list(entry.get("git_excludes", []) or []),
                    cmd=list(entry.get("cmd", []) or []),
                )
            )

        standalone_repos: list[StandaloneRepositoryConfig] = []
        for entry in merged.get("standalone_repository", []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            url = entry.get("url")
            if not name and not url:
                continue
            path_value = entry.get("path")
            if path_value is not None:
                self._validate_relative_path(path_value, name or url)
            standalone_repos.append(
                StandaloneRepositoryConfig(
                    name=name,
                    url=url,
                    main_branch=entry.get("main_branch"),
                    path=path_value,
                    prefix=entry.get("prefix"),
                    git_excludes=list(entry.get("git_excludes", []) or []),
                    cmd=list(entry.get("cmd", []) or []),
                )
            )

        user = ((merged.get("git") or {}).get("user")) or {}
        git_identity = (
            GitIdentity(name=user["name"], email=user["email"]) if user.get("name") and user.get("email") else None
        )

        main_branch = merged.get("main_branch") or "main"

        adopt_value = merged.get("adopt_extensions", "winter")
        try:
            adopt_extensions = AdoptExtensions(adopt_value)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid adopt_extensions value: {adopt_value!r}. Must be one of: 'none', 'winter', 'all'."
            ) from exc

        return WorkspaceConfig(
            workspace_root=workspace_root,
            session_prefix=merged.get("session_prefix", "winter"),
            main_branch=main_branch,
            git_excludes=list(merged.get("git_excludes", []) or []),
            git_identity=git_identity,
            adopt_extensions=adopt_extensions,
            singleton_repos=singletons,
            project_repos=project_repos,
            standalone_repos=standalone_repos,
        )

    def _read_config(self, path: Path) -> dict:
        if not self._fs.is_file(path):
            return {}
        return self._config_file_reader.load(path)

    @staticmethod
    def _validate_relative_path(value: str, label: str | None) -> None:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(
                f"Invalid path {value!r} for standalone repo {label!r}: "
                f"must be a relative path under the workspace root with no `..` segments."
            )
