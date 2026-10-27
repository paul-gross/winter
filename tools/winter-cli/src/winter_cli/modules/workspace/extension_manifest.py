from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from winter_cli.config.models import ExtensionLoad
from winter_cli.core.config_file import ConfigError, ConfigFileReadError, IConfigFileReader
from winter_cli.modules.provision.manifest import ProvisionHandler, ProvisionManifestParser
from winter_cli.modules.service.ext_service_manifest import ExtServiceDef, ExtServiceManifestParser
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

EXT_MANIFEST = "winter-ext.toml"
DEFAULT_SKILLS_DIRS = ("skills", ".claude/skills")
DEFAULT_AGENTS_DIRS = ("agents", ".claude/agents")
# Applies to both standalones and project-repo extensions (ExtensionAgentsMdService
# tries all three for either kind). For a standalone this widens eligibility versus
# the pre-winter#160 index.md-only check: a standalone with a root AGENTS.md or
# context/index.md but no index.md is now eagerly `@`-imported where it previously
# wasn't. Accepted consequence, not scoped to the project-repo routing-row path —
# grows the always-on `@`-import graph's `injected_bytes` budget only for a
# standalone that ships one of the fallback paths but not `index.md`.
DEFAULT_ENTRY_POINT_PATHS = ("index.md", "AGENTS.md", "context/index.md")

HOOK_ON_ENV_INIT = "on_env_init"
HOOK_ON_ENV_DESTROY = "on_env_destroy"
HOOK_ON_WORKSPACE_RECONCILE = "on_workspace_reconcile"

EXTENSION_BLOCK_NAME = "winter-extensions"
EXTENSION_INDEX_FILENAME = "index.md"


class ExcludesDeclarationError(RepoError):
    """A malformed `[excludes]` declaration; unlike other manifest errors it fails the init run."""


def prefixes_overlap(a: str, b: str) -> bool:
    """Whether two extension prefixes can name the same projected `<prefix>-*` artifact.

    Equal prefixes collide, and so does one that extends the other at a dash
    (`wf` and `wf-extra`): `wf-*` also matches `wf-extra-skill`.
    """
    return a == b or a.startswith(f"{b}-") or b.startswith(f"{a}-")


def _coerce_load(value: object) -> ExtensionLoad | None:
    """Coerce the manifest `load` field into an `ExtensionLoad`.

    Absent or empty leaves the field undeclared (`None`). Any other value is
    something the author wrote deliberately, so a typo raises rather than
    silently falling back to a default the author didn't ask for — a silent
    fallback to `eager` would inject context they meant to make lazy.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            declared = ExtensionLoad(value)
        except ValueError:
            pass
        else:
            if declared is not ExtensionLoad.none:
                return declared
    # `none` is workspace authority (a repo entry's `load`), never a manifest value.
    accepted = ", ".join(f'"{member.value}"' for member in ExtensionLoad if member is not ExtensionLoad.none)
    raise ConfigError(f"`load` must be one of {accepted}; got {value!r}")


# Mirrors the single-name grammar `winter space` accepts for a kind.
_SPACE_KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FORBIDDEN_PATTERN_CHARS = ("\n", "\r", "\x00")


def _coerce_exclude_path(entry: object) -> str:
    """Validate one `[excludes] paths` entry and return it as an anchored Git pattern.

    A single leading `/` is a Git root anchor, not a filesystem path; entries
    without it are anchored when emitted. Anything that could escape the
    workspace root or change a pattern's meaning (negation, `..`, home/drive/UNC
    forms, newlines) raises rather than being silently dropped.
    """
    if not isinstance(entry, str) or not entry:
        raise ConfigError(f"`[excludes] paths` entries must be non-empty strings; got {entry!r}")
    if any(ch in entry for ch in _FORBIDDEN_PATTERN_CHARS):
        raise ConfigError(f"`[excludes] paths` entry {entry!r} contains a newline, carriage return, or NUL")
    if entry.startswith("!"):
        raise ConfigError(f"`[excludes] paths` entry {entry!r} is a negated pattern; negation is not allowed")
    if entry.startswith("#"):
        raise ConfigError(f"`[excludes] paths` entry {entry!r} starts with '#', which Git reads as a comment")
    if entry.startswith(("~", "\\")) or re.match(r"^[A-Za-z]:", entry) or entry.startswith("//"):
        raise ConfigError(
            f"`[excludes] paths` entry {entry!r} is a home, drive, or UNC path, not a workspace-root pattern"
        )
    body = entry[1:] if entry.startswith("/") else entry
    segments = body.split("/")
    if ".." in segments:
        raise ConfigError(f"`[excludes] paths` entry {entry!r} contains a '..' segment")
    if all(seg in ("", ".") for seg in segments):
        raise ConfigError(f"`[excludes] paths` entry {entry!r} names only the workspace root")
    return "/" + body


def _coerce_excludes(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Coerce the manifest `[excludes]` table into `(paths, space_kinds)`.

    Both fields are optional lists of non-empty strings. Any malformed table,
    field type, or entry raises so a declaration is never partially accepted.
    """
    if value is None:
        return (), ()
    if not isinstance(value, dict):
        raise ConfigError(f"`[excludes]` must be a table; got {type(value).__name__}")
    unknown = sorted(set(value) - {"paths", "space_kinds"})
    if unknown:
        raise ConfigError(f"`[excludes]` has unknown field(s): {', '.join(unknown)}")
    result: dict[str, list[object]] = {}
    for key in ("paths", "space_kinds"):
        raw = value.get(key, [])
        if not isinstance(raw, list):
            raise ConfigError(f"`[excludes] {key}` must be a list of strings; got {type(raw).__name__}")
        result[key] = raw
    paths = tuple(dict.fromkeys(_coerce_exclude_path(e) for e in result["paths"]))
    kinds: list[str] = []
    for kind in result["space_kinds"]:
        if not isinstance(kind, str) or not _SPACE_KIND.match(kind):
            raise ConfigError(
                f"`[excludes] space_kinds` entry {kind!r} is invalid — a kind is a single name "
                f"(letters, digits, '.', '_', '-'), not a path"
            )
        if kind not in kinds:
            kinds.append(kind)
    return paths, tuple(kinds)


# Workspaces commit a stable `# Winter Extensions` section in AGENTS.md (or
# CLAUDE.md for backward-compat) that imports `@AGENTS.winter.md`; this CLI
# only writes the imported file. It is gitignored so init runs don't dirty
# the workspace.
AGENTS_WINTER_FILENAME = "AGENTS.winter.md"
# Retained for migration-cleanup deletion of stale files written by older
# versions of winter that also generated a paired CLAUDE.winter.md shim.
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

    `lint` also accepts a **table** form, `[lint] scripts = [...]`, which means
    the same thing. It exists because `[lint.ignore]` — the suppression rules
    read by `LintIgnoreService`, not by this loader — makes `lint` a table, and
    TOML cannot spell both `lint = [...]` and `[lint.ignore]` in one file. A
    repo that contributes checks *and* declares ignores writes the table form
    for both.

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

    `provision` is the tuple of `ProvisionHandler` objects parsed from the
    `[provision]` table in `winter-ext.toml`. The source label on each handler
    is the extension prefix. Empty by default; raises `RepoError` on malformed
    entries (caught and reported at each call site like other manifest errors).

    `service_defs` is the tuple of `ExtServiceDef` objects parsed from the
    `[[service]]` array in `winter-ext.toml`.  Each entry declares a bare service
    that winter-cli aggregates across all extensions and hands to the bound
    orchestrator(s) via the ``WINTER_SERVICE_MANIFEST`` env var.  Unknown keys
    REJECT at parse time.  Empty by default; raises `RepoError` on malformed
    entries (caught and reported at each call site like other manifest errors).

    `description` is a one-line, human-readable summary of the extension, used
    to source the text of a lazily-loaded extension's routing row in
    `AGENTS.winter.md` (`ExtensionAgentsMdService`). An eager `@`-import needs
    no description and drops it. `None` when absent; the row then renders with
    the entry-point path alone.

    `load` declares how the entry point reaches an agent — `eager` `@`-imports
    it into every session, `lazy` renders a link the agent follows only when the
    row's subject is in scope. `None` when undeclared, which leaves the choice
    to `ExtensionAgentsMdService`'s per-repo-kind default (standalones eager,
    project repos lazy). Raises `RepoError` on an unrecognized value.

    `exclude_paths` and `exclude_space_kinds` come from the `[excludes]` table:
    workspace-root Git patterns (already normalized to a leading-`/` anchor) and
    winter-space kind names whose directories the workspace exclude file should
    cover. Empty by default; raises `RepoError` on a malformed table or entry.
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
    provision: tuple[ProvisionHandler, ...] = ()
    description: str | None = None
    load: ExtensionLoad | None = None
    service_defs: tuple[ExtServiceDef, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    exclude_space_kinds: tuple[str, ...] = ()

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


class IExtensionManifestLoader(Protocol):
    """Protocol seam for loading an extension manifest from a repo path."""

    def load(self, repo: StandaloneRepository, manifest_path: Path | None) -> ExtensionManifest: ...


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

        lint_raw = data.get("lint")
        # Table form (`[lint] scripts = [...]`) coexists with `[lint.ignore]`;
        # the scalar/list form is the original spelling. `ignore` is read by
        # `LintIgnoreService` straight off the manifest, not resolved here —
        # its globs mean nothing without the repo root they resolve against.
        lint = _coerce_str_tuple(lint_raw.get("scripts") if isinstance(lint_raw, dict) else lint_raw)

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

        try:
            provision = tuple(ProvisionManifestParser().parse(data.get("provision"), source=prefix))
        except ConfigError as exc:
            raise RepoError(f"reading {EXT_MANIFEST} — {exc}") from exc

        try:
            service_defs = tuple(ExtServiceManifestParser().parse(data.get("service"), source=prefix))
        except ConfigError as exc:
            raise RepoError(f"reading {EXT_MANIFEST} — {exc}") from exc

        description_raw = data.get("description")
        description = description_raw if isinstance(description_raw, str) and description_raw else None

        try:
            load = _coerce_load(data.get("load"))
        except ConfigError as exc:
            raise RepoError(f"reading {EXT_MANIFEST} — {exc}") from exc

        try:
            exclude_paths, exclude_space_kinds = _coerce_excludes(data.get("excludes"))
        except ConfigError as exc:
            raise ExcludesDeclarationError(f"reading {EXT_MANIFEST} — {exc}") from exc

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
            provision=provision,
            service_defs=service_defs,
            description=description,
            load=load,
            exclude_paths=exclude_paths,
            exclude_space_kinds=exclude_space_kinds,
        )


def _conforms_extension_manifest_loader(x: ExtensionManifestLoader) -> IExtensionManifestLoader:
    return x
