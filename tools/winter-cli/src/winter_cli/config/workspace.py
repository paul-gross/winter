from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import (
    _DEFAULT_ENV_ALIASES,
    AdoptExtensions,
    DashboardConfig,
    DashboardLayout,
    EnvVarBands,
    FileSizeLintConfig,
    GitIdentity,
    KeybindingsConfig,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    SpaceConfig,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.config.workspace_locator import IWorkspaceLocator
from winter_cli.core.config_file import ConfigError, IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.provision.manifest import ProvisionHandler, ProvisionManifestParser
from winter_cli.modules.service.ext_service_manifest import ExtServiceDef, ExtServiceManifestParser
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


_PROVISION_PARSER = ProvisionManifestParser()
_SERVICE_DEF_PARSER = ExtServiceManifestParser()


def parse_provision(config: WorkspaceConfig, source: str = "project") -> list[ProvisionHandler]:
    """Strictly parse the ``[provision]`` table stored on *config*.

    Runs the ``ProvisionManifestParser`` on the raw table that was stored
    during config load.  Raises ``ConfigError`` on any structural or semantic
    violation.  Call this on demand (e.g. from the provision command or a
    doctor probe) — not at config-load time — so a malformed manifest does
    not break unrelated commands.
    """
    project_names: frozenset[str] = frozenset(repo.name for repo in config.project_repos if repo.name is not None)
    return _PROVISION_PARSER.parse(config.provision_raw or None, source, project_names=project_names)


def parse_service_defs(config: WorkspaceConfig, source: str = "workspace") -> list[ExtServiceDef]:
    """Strictly parse the ``[[service]]`` array stored on *config*.

    Runs the ``ExtServiceManifestParser`` on the raw list stored during config
    load.  Raises ``ConfigError`` on any structural or semantic violation
    (including unknown keys).  Call this on demand — not at config-load time —
    so a malformed entry does not break unrelated commands.
    """
    raw = config.service_defs_raw or None
    return _SERVICE_DEF_PARSER.parse(raw, source)


class WorkspaceConfigService:
    """Loads `.winter/config.toml` (+ optional local overlay) into a WorkspaceConfig.

    Depends on Protocol seams for I/O: `IWorkspaceLocator` for root discovery,
    `IConfigFileReader` for TOML parsing, and `IFilesystemReader` for the
    singleton-detection probes (`product/`, `context/harness/.git`).
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
        if self._fs.exists(workspace_root / "context" / "harness" / ".git"):
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
        _seen_standalone_names: dict[str, str] = {}  # resolved_name -> "name=X" or "url=Y" label
        for entry in merged.get("standalone_repository", []) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            url = entry.get("url")
            if not name and not url:
                continue
            resolved_name = name if name else self._name_from_url(str(url))
            label = f"name={name!r}" if name else f"url={url!r}"
            if resolved_name in _seen_standalone_names:
                first_label = _seen_standalone_names[resolved_name]
                raise ConfigError(
                    f"Duplicate standalone_repository name {resolved_name!r}: "
                    f"entries {first_label} and {label} resolve to the same name. "
                    f"Each [[standalone_repository]] must have a unique name."
                )
            _seen_standalone_names[resolved_name] = label
            path_value = entry.get("path")
            if path_value is not None:
                self._validate_relative_path(path_value, name or url)
            config_dir_value = entry.get("config_dir")
            if config_dir_value is not None:
                self._validate_relative_path(config_dir_value, name or url)
            standalone_repos.append(
                StandaloneRepositoryConfig(
                    name=name,
                    url=url,
                    main_branch=entry.get("main_branch"),
                    ref=entry.get("ref"),
                    path=path_value,
                    prefix=entry.get("prefix"),
                    config_dir=config_dir_value,
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

        skill_prefix_raw = merged.get("prefix")
        skill_prefix = skill_prefix_raw if isinstance(skill_prefix_raw, str) and skill_prefix_raw else None

        if skill_prefix is not None:
            for repo in standalone_repos:
                if repo.prefix == skill_prefix:
                    repo_label = repo.name or str(repo.url)
                    raise ConfigError(
                        f"Workspace `prefix = {skill_prefix!r}` collides with [[standalone_repository]] "
                        f"{repo_label!r}: both write into .claude/skills/ and prune `{skill_prefix}-*` "
                        f"entries. Use a distinct prefix for workspace skills."
                    )

        provision_raw = merged.get("provision")
        if not isinstance(provision_raw, dict):
            provision_raw = {}

        service_defs_raw = merged.get("service")
        if not isinstance(service_defs_raw, list):
            service_defs_raw = []

        file_size_lint = self._parse_file_size_lint(merged.get("core_checks"))

        env_bands = self._parse_env_bands(merged.get("env"))

        space = self._parse_space(merged.get("space"))

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
            skill_prefix=skill_prefix,
            service_orchestrator=(
                merged.get("service_orchestrator") if isinstance(merged.get("service_orchestrator"), str) else None
            ),
            capabilities=capabilities,
            doctor=merged.get("doctor") if isinstance(merged.get("doctor"), str) else None,
            lint=_coerce_str_list(merged.get("lint")),
            file_size_lint=file_size_lint,
            keybindings=keybindings,
            dashboard=dashboard,
            base_port=base_port,
            ports_per_env=ports_per_env,
            env_aliases=env_aliases,
            envs_per_workspace=envs_per_workspace,
            provision_raw=provision_raw,
            service_defs_raw=service_defs_raw,
            env_bands=env_bands,
            space=space,
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

    @staticmethod
    def _parse_file_size_lint(core_checks_raw: object) -> FileSizeLintConfig:
        """Build a FileSizeLintConfig from the ``[core_checks.file_size]`` sub-table.

        Falls back to defaults when the table is absent or any value is missing.
        Non-integer values are silently ignored in favour of the default so a
        stray typo in a threshold does not break unrelated commands.
        """
        if not isinstance(core_checks_raw, dict):
            return FileSizeLintConfig()
        file_size_raw = core_checks_raw.get("file_size")
        if not isinstance(file_size_raw, dict):
            return FileSizeLintConfig()
        kwargs: dict = {}
        injected = file_size_raw.get("injected_bytes")
        if isinstance(injected, int) and not isinstance(injected, bool):
            kwargs["injected_bytes"] = injected
        reference = file_size_raw.get("reference_bytes")
        if isinstance(reference, int) and not isinstance(reference, bool):
            kwargs["reference_bytes"] = reference
        return FileSizeLintConfig(**kwargs)

    @staticmethod
    def _parse_env_bands(env_raw: object) -> EnvVarBands:
        """Build an ``EnvVarBands`` from ``[env.workspace.vars]`` and ``[env.feature.vars]``.

        Absent sub-tables produce empty bands (clean no-op).  Non-scalar values
        (e.g. TOML arrays, tables, or booleans) raise ``ConfigError`` naming the
        band and key.

        A legacy ``[env.vars]`` key (flat ``vars`` directly under ``[env]``) is a
        hard break: raises ``ConfigError`` directing the user to migrate to the new
        band names.
        """
        if not isinstance(env_raw, dict):
            return EnvVarBands()

        if "vars" in env_raw:
            legacy_keys = ", ".join(env_raw["vars"].keys()) if isinstance(env_raw["vars"], dict) else ""
            keys_clause = f" Found keys: {legacy_keys}." if legacy_keys else ""
            raise ConfigError(
                f"Unsupported config key [env.vars] detected.{keys_clause} "
                "Migrate to [env.feature.vars] (feature-env scope) and/or "
                "[env.workspace.vars] (workspace scope)."
            )

        workspace_band = WorkspaceConfigService._parse_env_band(env_raw, "workspace")
        feature_band = WorkspaceConfigService._parse_env_band(env_raw, "feature")
        return EnvVarBands(workspace=workspace_band, feature=feature_band)

    @staticmethod
    def _parse_env_band(env_raw: dict, band: str) -> dict[str, str]:
        """Parse one named band (``workspace`` or ``feature``) from the ``[env]`` table.

        Reads ``env_raw[band]["vars"]``; returns an empty dict when the sub-table or
        the ``vars`` key is absent.  Non-scalar values raise ``ConfigError`` naming the
        band and the offending key.
        """
        band_raw = env_raw.get(band)
        if not isinstance(band_raw, dict):
            return {}
        vars_raw = band_raw.get("vars")
        if not isinstance(vars_raw, dict):
            return {}
        result: dict[str, str] = {}
        for k, v in vars_raw.items():
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ConfigError(
                    f"[env.{band}.vars] key {k!r} has an unsupported value type "
                    f"({type(v).__name__}); only string, integer, and float values are allowed."
                )
            result[str(k)] = str(v)
        return result

    @staticmethod
    def _parse_space(space_raw: object) -> SpaceConfig:
        """Build a ``SpaceConfig`` from the ``[space]`` table.

        Reads scalar ``root`` and the ``[space.kinds]`` sub-table of dynamic,
        untyped artifact-kind → directory overrides. Absent table or keys fall
        back to defaults (``root = ".winter"``, no overrides). Non-string values
        are ignored rather than raising, so a stray typo in one kind does not
        break unrelated commands.
        """
        if not isinstance(space_raw, dict):
            return SpaceConfig()
        kwargs: dict = {}
        root = space_raw.get("root")
        if isinstance(root, str) and root:
            kwargs["root"] = root
        kinds_raw = space_raw.get("kinds")
        if isinstance(kinds_raw, dict):
            kinds = {str(k): v for k, v in kinds_raw.items() if isinstance(v, str) and v}
            if kinds:
                kwargs["kinds"] = kinds
        return SpaceConfig(**kwargs)

    def _read_config(self, path: Path) -> dict:
        if not self._fs.is_file(path):
            return {}
        return self._config_file_reader.load(path)

    @staticmethod
    def _name_from_url(url: str) -> str:
        """Derive a standalone repo name from a clone URL.

        Mirrors ``RepositoryFactory.name_from_url`` so the collision check uses
        the same resolved name that the factory will later materialise on disk.
        """
        stripped = url.rstrip("/")
        cut = max(stripped.rfind("/"), stripped.rfind(":"))
        candidate = stripped[cut + 1 :] if cut != -1 else stripped
        return candidate.removesuffix(".git")

    @staticmethod
    def _validate_relative_path(value: str, label: str | None) -> None:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError(
                f"Invalid path {value!r} for standalone repo {label!r}: "
                f"must be a relative path under the workspace root with no `..` segments."
            )
