from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

EXT_MANIFEST = "winter-ext.toml"
DEFAULT_SKILLS_DIRS = ("skills", ".claude/skills")
DEFAULT_AGENTS_DIRS = ("agents", ".claude/agents")

HOOK_ON_ENV_INIT = "on_env_init"
HOOK_ON_ENV_DESTROY = "on_env_destroy"
HOOK_ON_WORKSPACE_RECONCILE = "on_workspace_reconcile"

CLAUDEMD_BLOCK_NAME = "winter-extensions"
CLAUDEMD_INDEX_FILENAME = "index.md"

# Workspaces commit a stable `# Winter Extensions` section in CLAUDE.md that
# imports `@CLAUDE.winter.md`; this CLI only writes the imported file. The
# file is gitignored so init runs don't dirty the workspace.
CLAUDEMD_WINTER_FILENAME = "CLAUDE.winter.md"


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a manifest `str | list[str]` field into a tuple of non-empty strings.

    A bare string becomes a one-element tuple (back-compat with the single-path
    `lint = "..."` form); a list keeps its string entries; anything else is empty.
    """
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


@dataclass(frozen=True)
class ExtensionManifest:
    """Resolved extension settings for a single standalone repo.

    `prefix` is the final symlink prefix after applying overrides:
    workspace config `prefix` > manifest `prefix` > manifest `name` > repo dir name.

    `skills_dirs` and `agents_dirs` are ordered candidate paths; processing uses
    the first one that exists. The defaults try the winter convention
    (top-level `skills/`/`agents/`) and then the Claude Code convention
    (`.claude/skills/`/`.claude/agents/`), so vanilla Claude Code repos can be
    adopted as extensions without modification.

    `hooks` maps hook names (e.g. `on_env_init`, `on_workspace_reconcile`) to
    executable script paths relative to the extension's repo root. Hooks let an
    extension contribute setup steps that don't fit the symlink-skills/agents
    model — for example, dropping additional files into a worktree or running
    provisioning commands.

    `doctor` is the relative path of an executable probe script invoked by
    `winter doctor`. The script emits one NDJSON event per check on stdout;
    a non-zero exit is treated as a single fail with stderr as the message.

    `lint` is the tuple of relative paths of executable lint scripts invoked by
    `winter lint`. Same NDJSON contract as `doctor`, with optional `file`/`line`
    fields per finding and the scope passed in via `WINTER_LINT_*` env vars. The
    manifest accepts a single path or a list; a bare string is coerced to a
    one-element tuple. Empty by default.

    `provides` maps capability slot names to entrypoint paths relative to the extension
    repo root (e.g. `{"service": "workflow/service"}`). It is the general successor to
    `orchestrate_services`; use `capability_entrypoint` to resolve a slot, which bridges
    both forms transparently.

    `orchestrate_services` is the deprecated, slot-specific predecessor of `provides.service`.
    New extensions should declare `[provides]` instead; `capability_entrypoint` shims the
    two so extensions that predate `[provides]` continue to resolve without change.

    `requires` is the module's declared dependency list — the other modules this
    one references and therefore needs when shipped standalone. Each entry is a
    module name (the `<context>` half of a `<context>:/path` reference, e.g.
    `winter-product`). It is the data the `winter graph` command aggregates and
    the extractability lint check validates references against. Empty by default.

    `implements` maps capability slot names to the spec version string this
    extension implements (e.g. `{"service": "v1"}`). Declared via `[implements]`
    in `winter-ext.toml`. Absent or empty means the extension predates the field
    and is treated as compatible (opt-in / lenient-when-absent). Use
    `implemented_version(slot)` to look up the version for a specific slot.
    """

    prefix: str
    skills_dirs: tuple[str, ...]
    agents_dirs: tuple[str, ...]
    hooks: dict[str, str] = field(default_factory=dict)
    doctor: str | None = None
    lint: tuple[str, ...] = ()
    orchestrate_services: str | None = None
    requires: tuple[str, ...] = ()
    provides: dict[str, str] = field(default_factory=dict)
    implements: dict[str, str] = field(default_factory=dict)

    def capability_entrypoint(self, slot: str) -> str | None:
        """Resolve the entrypoint for a capability slot.

        Reads `provides.<slot>` first; for the `service` slot, falls back to the
        deprecated `orchestrate_services` key so extensions that predate `[provides]`
        keep resolving. Returns None when neither is declared.
        """
        if self.provides.get(slot):
            return self.provides[slot]
        if slot == "service":
            return self.orchestrate_services
        return None

    def implemented_version(self, slot: str) -> str | None:
        """Return the spec version this extension declares it implements for `slot`.

        Returns None when `[implements]` is absent or the slot is not listed —
        indicating the extension predates the `implements` field; treated as
        compatible by the version-compat check.
        """
        return self.implements.get(slot)


class ExtensionManifestLoader:
    """Reads `winter-ext.toml` and produces a resolved `ExtensionManifest`.

    A single loader is shared by the symlink, hook, and exclude services so
    the prefix resolution and field-shape interpretation stay in one place.

    Error-handling shape: raises `RepoError` on a malformed manifest. Callers
    catch at their own wrap site and route the failure through the reporter.
    """

    def __init__(self, config_file_reader: IConfigFileReader) -> None:
        self._config_file_reader = config_file_reader

    def load(
        self,
        repo: StandaloneRepository,
        manifest_path: Path | None,
    ) -> ExtensionManifest:
        data: dict = {}
        if manifest_path is not None:
            try:
                data = self._config_file_reader.load(manifest_path)
            except ConfigFileReadError as exc:
                raise RepoError(f"reading {EXT_MANIFEST} — {exc}") from exc

        # Prefix resolution: workspace override > manifest prefix > manifest name > repo dir name.
        prefix = repo.prefix or data.get("prefix") or data.get("name") or repo.name

        # Manifest can declare an explicit dir; otherwise fall back to the
        # default search list which covers both winter and Claude Code conventions.
        skills_dirs = (data["skills_dir"],) if "skills_dir" in data else DEFAULT_SKILLS_DIRS
        agents_dirs = (data["agents_dir"],) if "agents_dir" in data else DEFAULT_AGENTS_DIRS

        hooks_raw = data.get("hooks") or {}
        hooks = {k: str(v) for k, v in hooks_raw.items() if isinstance(v, str)}

        doctor_raw = data.get("doctor")
        doctor = doctor_raw if isinstance(doctor_raw, str) and doctor_raw else None

        lint = _coerce_str_tuple(data.get("lint"))

        orchestrate_services_raw = data.get("orchestrate_services")
        orchestrate_services = (
            orchestrate_services_raw if isinstance(orchestrate_services_raw, str) and orchestrate_services_raw else None
        )

        requires_raw = data.get("requires")
        requires = tuple(r for r in requires_raw if isinstance(r, str) and r) if isinstance(requires_raw, list) else ()

        provides_raw = data.get("provides")
        provides = (
            {k: str(v) for k, v in provides_raw.items() if isinstance(v, str) and v}
            if isinstance(provides_raw, dict)
            else {}
        )

        implements_raw = data.get("implements")
        implements = (
            {k: str(v) for k, v in implements_raw.items() if isinstance(k, str) and isinstance(v, str) and v}
            if isinstance(implements_raw, dict)
            else {}
        )

        return ExtensionManifest(
            prefix=prefix,
            skills_dirs=skills_dirs,
            agents_dirs=agents_dirs,
            hooks=hooks,
            doctor=doctor,
            lint=lint,
            orchestrate_services=orchestrate_services,
            requires=requires,
            provides=provides,
            implements=implements,
        )
