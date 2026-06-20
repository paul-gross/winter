from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import (
    _DEFAULT_ENV_ALIASES,
    AdoptExtensions,
    DashboardConfig,
    DashboardLayout,
    GitIdentity,
    KeybindingsConfig,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.config.workspace_locator import IWorkspaceLocator
from winter_cli.core.config_file import ConfigError, IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.util import deep_merge

WINTER_DIR = ".winter"
CONFIG_FILE = "config.toml"
LOCAL_CONFIG_FILE = "config.local.toml"


def _coerce_str_list(value: object) -> list[str]:
    """Coerce a TOML `str | list[str]` field into a clean list of non-empty strings.

    A bare string becomes a one-element list (back-compat with the single-path
    `lint = "..."` form); a list keeps its string entries; anything else is empty.
    """
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _coerce_int(value: object, default: int) -> int:
    """Coerce a TOML scalar into an int, falling back to *default*.

    ``bool`` is rejected even though it is an ``int`` subclass, so a stray
    ``base_port = true`` does not silently become ``1``.
    """
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


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

        keybindings = self._parse_keybindings(merged.get("keybindings"))
        dashboard = self._parse_dashboard(merged.get("tui"))

        # ── capabilities: each slot accepts str | list[str] ─────────────────────
        # Parse the [capabilities] table, normalizing each slot value to a deduped
        # ordered list. A bare string becomes a one-element list; a list is used
        # as-is (skipping non-string and empty-string entries).
        caps_raw = merged.get("capabilities")
        capabilities: dict[str, list[str]] = {}
        if isinstance(caps_raw, dict):
            for k, v in caps_raw.items():
                slot_key = str(k)
                normalized = _coerce_str_list(v)
                # Deduplicate preserving order.
                _seen_cap: set[str] = set()
                _deduped_cap: list[str] = []
                for _s in normalized:
                    if _s not in _seen_cap:
                        _seen_cap.add(_s)
                        _deduped_cap.append(_s)
                if _deduped_cap:
                    capabilities[slot_key] = _deduped_cap

        # Legacy back-compat: service_orchestrator (singular) folds into
        # capabilities["service"] when no explicit capabilities.service is set.
        if (
            "service" not in capabilities
            and isinstance(merged.get("service_orchestrator"), str)
            and merged["service_orchestrator"]
        ):
            capabilities["service"] = [merged["service_orchestrator"]]

        main_branch = merged.get("main_branch") or "main"

        adopt_value = merged.get("adopt_extensions", "winter")
        try:
            adopt_extensions = AdoptExtensions(adopt_value)
        except ValueError as exc:
            raise ConfigError(
                f"Invalid adopt_extensions value: {adopt_value!r}. Must be one of: 'none', 'winter', 'all'."
            ) from exc

        base_port = _coerce_int(merged.get("base_port", 4000), 4000)
        ports_per_env = _coerce_int(merged.get("ports_per_env", 20), 20)

        env_aliases = _coerce_str_list(merged.get("env_aliases", list(_DEFAULT_ENV_ALIASES)))

        envs_per_workspace = _coerce_int(merged.get("envs_per_workspace", 48), 48)

        if envs_per_workspace < len(env_aliases) + 2:
            raise ConfigError(
                f"Invalid config: envs_per_workspace ({envs_per_workspace}) must be >= "
                f"len(env_aliases) + 2 ({len(env_aliases) + 2}). "
                f"Either reduce env_aliases (currently {len(env_aliases)} entries) or increase envs_per_workspace."
            )

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
            service_orchestrator=(
                merged.get("service_orchestrator") if isinstance(merged.get("service_orchestrator"), str) else None
            ),
            capabilities=capabilities,
            doctor=merged.get("doctor") if isinstance(merged.get("doctor"), str) else None,
            lint=_coerce_str_list(merged.get("lint")),
            keybindings=keybindings,
            dashboard=dashboard,
            base_port=base_port,
            ports_per_env=ports_per_env,
            env_aliases=env_aliases,
            envs_per_workspace=envs_per_workspace,
        )

    @staticmethod
    def _parse_keybindings(raw: object) -> KeybindingsConfig:
        """Build a KeybindingsConfig from the `[keybindings]` table.

        Action-id -> key-spec entries live in the `[keybindings.bindings]`
        sub-table so dotted ids (`workspace.sync`) stay flat string keys rather
        than splitting into nested TOML tables. `leader` and `timeoutlen` are
        scalar siblings.
        """
        if not isinstance(raw, dict):
            return KeybindingsConfig()
        bindings_raw = raw.get("bindings")
        bindings = {str(k): str(v) for k, v in bindings_raw.items()} if isinstance(bindings_raw, dict) else {}
        kwargs: dict = {"bindings": bindings}
        if isinstance(raw.get("leader"), str):
            kwargs["leader"] = raw["leader"]
        if isinstance(raw.get("timeoutlen"), int) and not isinstance(raw.get("timeoutlen"), bool):
            kwargs["timeoutlen"] = raw["timeoutlen"]
        return KeybindingsConfig(**kwargs)

    @staticmethod
    def _parse_dashboard(tui_raw: object) -> DashboardConfig:
        """Build a DashboardConfig from the `[tui.dashboard]` sub-table.

        Reads the nested `[tui.dashboard]` table: first the `[tui]` table, then
        its `dashboard` sub-table. An unknown or invalid `layout` value raises
        ConfigError listing the valid choices.
        """
        if not isinstance(tui_raw, dict):
            return DashboardConfig()
        dashboard_raw = tui_raw.get("dashboard")
        if not isinstance(dashboard_raw, dict):
            return DashboardConfig()
        layout_value = dashboard_raw.get("layout", "auto")
        try:
            layout = DashboardLayout(layout_value)
        except ValueError as exc:
            valid = ", ".join(repr(m.value) for m in DashboardLayout)
            raise ConfigError(
                f"Invalid tui.dashboard.layout value: {layout_value!r}. Must be one of: {valid}."
            ) from exc
        return DashboardConfig(layout=layout)

    def _read_config(self, path: Path) -> dict:
        if not self._fs.is_file(path):
            return {}
        return self._config_file_reader.load(path)

    @staticmethod
    def _validate_relative_path(value: str, label: str | None) -> None:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError(
                f"Invalid path {value!r} for standalone repo {label!r}: "
                f"must be a relative path under the workspace root with no `..` segments."
            )
