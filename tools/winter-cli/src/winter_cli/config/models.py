from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from winter_cli.modules.workspace.agent_transform.models import AgentFormat


class SingletonType(enum.Enum):
    workspace = "workspace"
    """The workspace repo itself — contains .winter/, context/, workflow scripts."""

    product = "product"
    """The product branch worktree — orphan branch for plans and TODOs."""

    harness = "harness"
    """The agentic harness repo — cross-repo documentation at context/harness/."""


class AdoptExtensions(enum.Enum):
    none = "none"
    """Never symlink skills/agents from standalone repos."""

    winter = "winter"
    """Symlink skills/agents only when the standalone repo declares winter-ext.toml."""

    all = "all"
    """Symlink skills/agents from any standalone repo, with or without winter-ext.toml."""


class SkillInstall(enum.Enum):
    """How a code-agent vendor wants extension skills materialized into its skills dir.

    Symlink vendors discover skills through relative directory symlinks; copy
    vendors need real directories (their skill globber does not traverse
    symlinked directories).
    """

    symlink = "symlink"
    copy = "copy"


class CodeAgentVendor(enum.Enum):
    """A code-agent tool that winter projects extension skills and agents into.

    Each member carries the workspace-relative directory its skills live in
    (`skills_subpath`), the `SkillInstall` strategy it requires
    (`skill_install`), the workspace-relative directory its agent copies live
    in (`agents_subpath`), the `AgentFormat` used to render agent files
    (`agent_format`), and the canonical `vendor_label` string used as the
    override-block key in canonical agent frontmatter (e.g. ``claude:``,
    ``codex:``, ``opencode:``).  Strategy selection is data-driven off the
    vendor — adding a vendor is a data change, not a new control-flow branch.

    ``vendor_label`` is the single source of truth for the vendor-name strings
    that appear in ``MODEL_TIER_IDS`` keys and in agent frontmatter override
    blocks.  The canonical parser derives its ``_VENDOR_LABELS`` set from this
    attribute so the two never drift.
    """

    ClaudeCode = (
        "claude-code",
        ".claude/skills",
        SkillInstall.symlink,
        ".claude/agents",
        AgentFormat.claude_md,
        "claude",
    )
    Codex = ("codex", ".codex/skills", SkillInstall.symlink, ".codex/agents", AgentFormat.codex_toml, "codex")
    OpenCode = (
        "opencode",
        ".opencode/skill",
        SkillInstall.copy,
        ".opencode/agent",
        AgentFormat.opencode_md,
        "opencode",
    )

    skills_subpath: str
    skill_install: SkillInstall
    agents_subpath: str
    agent_format: AgentFormat
    vendor_label: str

    def __init__(
        self,
        label: str,
        skills_subpath: str,
        skill_install: SkillInstall,
        agents_subpath: str,
        agent_format: AgentFormat,
        vendor_label: str,
    ) -> None:
        self._value_ = label
        self.skills_subpath = skills_subpath
        self.skill_install = skill_install
        self.agents_subpath = agents_subpath
        self.agent_format = agent_format
        self.vendor_label = vendor_label


class DashboardLayout(enum.Enum):
    auto = "auto"
    """Default. 1 repo → list; repos > envs → repos-as-rows; else → repos-as-columns."""

    repos_as_columns = "repos-as-columns"
    """Transpose of repos-as-rows: envs are rows, repos are columns."""

    repos_as_rows = "repos-as-rows"
    """Repos are rows, envs are columns (current behavior)."""

    list = "list"
    """One row per (env, repo) with columns env/project/remote/git-status/service-status;
    multi-repo groups under each env elide env/service on repeat rows (both env-scoped),
    while remote is per-repo (each worktree's own upstream) and shows on every row."""

    @staticmethod
    def resolve_auto(n_repos: int, n_envs: int) -> DashboardLayout:
        """Resolve ``auto`` to a concrete layout given the workspace shape.

        Single source of truth for the ``auto`` heuristic, shared by the
        dashboard TUI grid (`FeatureWorktreesGrid`) and `winter ws status
        --json` so the interactive and scripted surfaces cannot drift.
        """
        if n_repos == 1:
            return DashboardLayout.list
        if n_repos > n_envs:
            return DashboardLayout.repos_as_rows
        return DashboardLayout.repos_as_columns

    def resolve(self, n_repos: int, n_envs: int) -> DashboardLayout:
        """Resolve this configured layout to the concrete one for the given shape.

        The full resolution policy, shared by the dashboard TUI grid and
        `winter ws status --json` so the formula *and* its guards live in one
        place. A concrete layout returns itself unchanged. ``auto`` resolves via
        `resolve_auto`, except an empty workspace (no envs) falls back to
        `repos_as_rows` — the grid has no env axis to lay out without at least
        one env. Both surfaces must feed `n_repos`/`n_envs` derived the same way
        (the per-env worktree count and the env count) so they cannot diverge.
        """
        if self is not DashboardLayout.auto:
            return self
        if n_envs == 0:
            return DashboardLayout.repos_as_rows
        return DashboardLayout.resolve_auto(n_repos, n_envs)


class GitIdentity(BaseModel):
    """Git author identity applied to every repo winter-cli manages."""

    name: str
    email: str


class SingletonRepository(BaseModel):
    """An implicit repo discovered from the filesystem (workspace, product, harness).

    Singletons don't appear in the workspace config — they're materialized by `winter`
    when the corresponding directory exists. They carry only an identifier and the
    role they play.
    """

    name: str
    """Directory name and user-facing label."""

    type: SingletonType
    """Which singleton role this repo plays."""


class ProjectRepositoryConfig(BaseModel):
    """A project repo declared in `[[project_repository]]`.

    Project repos are cloned to `projects/` and worktreed into Greek-letter feature
    directories. They participate in feature branching unless `pinned = true`.
    """

    name: str | None = None
    """Directory name and user-facing label.

    Optional: when omitted, derived from the trailing path segment of `url`
    (with `.git` stripped). Setting `name` overrides the URL-derived default
    and IS the alias mechanism."""

    url: str | None = None
    """Git remote URL."""

    main_branch: str | None = None
    """Main branch name for this repo. Falls back to the workspace default when None."""

    pinned: bool = False
    """When true, the repo always tracks origin/main and is skipped during feature branching."""

    git_excludes: list[str] = Field(default_factory=list)
    """Per-repo entries added to .git/info/exclude in every clone/worktree."""

    cmd: list[str] = Field(default_factory=list)
    """Shell commands run idempotently after clone and in every worktree.

    This is a lightweight trust/bootstrap step (e.g. the `mise trust` equivalent),
    NOT dependency installation. `winter ws init` stays purely structural; declare
    dependency installs, resource creation, and data loading as `[[provision.*]]`
    handlers run by `winter provision <env>` instead."""


class StandaloneRepositoryConfig(BaseModel):
    """A standalone repo declared in `[[standalone_repository]]`.

    Standalone repos are cloned at the workspace root (or the configured `path`),
    skipped during feature branching, and may opt into extension behavior via
    a `winter-ext.toml` file.
    """

    name: str | None = None
    """Directory name and user-facing label.

    Optional: when omitted, derived from the trailing path segment of `url`."""

    url: str | None = None
    """Git remote URL."""

    main_branch: str | None = None
    """Main branch name for this repo. Falls back to the workspace default when None."""

    path: str | None = None
    """Optional clone location relative to the workspace root.

    When unset, the repo clones to `<workspace_root>/<name>/`. Set this to nest
    standalone repos under a subdirectory (e.g. `extensions/winter-backlog`).
    Must be a relative path; absolute paths and `..` segments are rejected."""

    prefix: str | None = None
    """Optional override for the extension symlink prefix.

    When set, takes precedence over `name` from winter-ext.toml. Lets the workspace
    disambiguate between two extensions that would otherwise share a prefix."""

    ref: str | None = None
    """Optional pin — a branch, tag, or commit that winter checks out for this repo.

    Semantics differ from two related fields:

    - ``pinned`` (``ProjectRepositoryConfig`` only, UNRELATED) — means "exclude
      this *project* repo from feature branching entirely."  The term is not
      reused here; standalone repos have no ``pinned`` field.

    - ``main_branch`` — the standalone repo's integration target / tracking
      branch when ``ref`` is absent or is itself a branch name.

    When ``ref`` is set, winter resolves it against the fetched remote refs in
    this order: ``refs/remotes/origin/<ref>`` (branch) → ``refs/tags/<ref>``
    (tag) → ``<ref>^{commit}`` (raw SHA).  First match wins; no match →
    unresolvable-ref error.

    - **absent** — today's behavior: clone tracks the default branch; pull
      integrates the tracked upstream.  No lock entry written.
    - **branch ref** — checkout on that tracking branch; ``main_branch`` is
      effectively set to ``<ref>``; pull fast-forwards to ``origin/<ref>`` and
      rewrites the lock.  A *moving* pin.
    - **tag / commit ref** — detached checkout held exactly at the resolved
      commit; pull never advances it; tracking overridden.  A *frozen* pin.
    """

    config_dir: str | None = None
    """Optional override for the per-extension config/asset directory.

    When unset, winter defaults to `.winter/config/<name>/` relative to the
    workspace root.  When set, must be a relative path under the workspace root
    with no ``..`` segments (same guard as ``path``).  This directory is
    exported as ``WINTER_EXT_CONFIG_DIR`` on every extension dispatch so the
    extension can read and write its writable config/asset files there."""

    git_excludes: list[str] = Field(default_factory=list)
    """Per-repo entries added to .git/info/exclude after clone."""

    cmd: list[str] = Field(default_factory=list)
    """Shell commands run idempotently after clone.

    A lightweight trust/bootstrap step (e.g. the `mise trust` equivalent), NOT
    dependency installation — declare those as `[[provision.*]]` handlers run by
    `winter provision` instead."""


class FileSizeLintConfig(BaseModel):
    """Byte-size thresholds for the built-in agent-facing markdown file-size check.

    The check measures every ``.md`` file in scope and compares it to one of
    two thresholds: the tighter ``injected_bytes`` threshold for files that
    appear in the auto-injected ``@import`` graph (roots: workspace
    ``CLAUDE.md`` and ``CLAUDE.winter.md``), and the looser
    ``reference_bytes`` threshold for all other agent-facing markdown.

    Default values are calibrated to the ~1.5 k-token target from issue #96
    (1 token ≈ 4 bytes → 1 500 tokens ≈ 6 000 bytes for injected files) with a
    2x headroom for reference docs that are consulted on demand rather than
    always loaded into context.

    Configure under ``[core_checks.file_size]`` in ``.winter/config.toml``::

        [core_checks.file_size]
        injected_bytes = 6000
        reference_bytes = 12000
    """

    injected_bytes: int = 6000
    """Maximum byte size for files in the auto-injected @import graph (default 6 000)."""

    reference_bytes: int = 12000
    """Maximum byte size for non-injected agent-facing markdown files (default 12 000)."""


class DashboardConfig(BaseModel):
    """Dashboard layout configuration from the `[tui.dashboard]` config table.

    Controls how repos and envs are arranged in the dashboard grid:
    - `auto` (default): 1 repo → list; repos > envs → repos-as-rows; else → repos-as-columns.
    - `repos-as-rows`: repos are rows, envs are columns (current behavior).
    - `repos-as-columns`: transpose — envs are rows, repos are columns.
    - `list`: one row per (env, repo) with columns env/project/remote/git-status/service-status;
      multi-repo groups under each env elide env/service on repeat rows (both env-scoped),
      while remote is per-repo (each worktree's own upstream) and shows on every row.
    """

    layout: DashboardLayout = DashboardLayout.auto
    """Grid layout mode for the dashboard."""


class KeybindingsConfig(BaseModel):
    """Dashboard keybinding overrides from the `[keybindings]` config table.

    `bindings` maps stable action ids (e.g. `workspace.refresh`,
    `worktree.open_detail`, `app.quit`, `plugin.<name>`) to Neovim-inspired key
    specs; absent ids fall back to their hardcoded defaults. `leader` is the
    token `<leader>` expands to, and `timeoutlen` is the inter-key deadline (ms)
    for multi-key chord sequences, mirroring Neovim's option of the same name.
    """

    leader: str = "\\"
    """Key spec that `<leader>` expands to (default backslash, Neovim's default)."""

    timeoutlen: int = 1000
    """Milliseconds to wait for the next key of a pending chord sequence."""

    bindings: dict[str, str] = Field(default_factory=dict)
    """Map of action id -> key spec. Absent ids keep their hardcoded default."""


_DEFAULT_ENV_ALIASES = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
]


class EnvVarBands(BaseModel):
    """Scope-split env-var bands from ``[env.workspace.vars]`` and ``[env.feature.vars]``.

    ``workspace`` vars are rendered for workspace scope AND inherited as the base layer
    for every feature scope (feature-band entries are overlaid on top; feature wins key
    collisions).  ``feature`` vars are rendered for feature-env scope only — never emitted
    for the workspace scope.  Both bands default to empty dicts when the corresponding
    TOML sub-table is absent.
    """

    model_config = ConfigDict(frozen=True)

    workspace: dict[str, str] = Field(default_factory=dict)
    """Vars from ``[env.workspace.vars]`` — workspace scope only."""

    feature: dict[str, str] = Field(default_factory=dict)
    """Vars from ``[env.feature.vars]`` — feature-env scope (overlaid on workspace band)."""


class SpaceConfig(BaseModel):
    """Artifact-space configuration from the ``[space]`` table.

    The **winter space** is where winter and its extensions write *generated
    artifacts* — harness scores, review manifests, workflow session docs, logs,
    and whatever else an extension owns — as opposed to repo deliverables. It is
    resolved by ``winter space <kind>`` and read by the consuming skill, so the
    location is never hardcoded into any one code harness's home directory.

    ``root`` is the space root. Resolved **relative to the workspace root**,
    unless it is home-relative (``~/...``) or absolute. Default ``.winter`` — so
    the unconfigured space lives inside the workspace and travels with the
    checkout.

    ``kinds`` maps an arbitrary artifact-kind name to a directory override. The
    keys are **dynamic and untyped** — each extension defines its own kinds
    (the ``winter-workflow`` extension uses ``scores``, ``manifests``,
    ``workflows``, ``retrospectives``; another could add ``logs``). A kind with
    no override resolves to a sub-directory of ``root`` named after the kind. An
    override value follows the same three-form rule, except a *relative*
    override is taken **relative to the resolved root** (``~``/absolute escape
    the root). Configure as::

        [space]
        root = "~/.winter"          # workspace-relative (default ".winter"), ~, or absolute

        [space.kinds]
        scores = "audits"           # -> <root>/audits
        logs = "/var/log/winter"    # -> absolute, outside the space root
    """

    model_config = ConfigDict(frozen=True)

    root: str = ".winter"
    """Space root. Workspace-relative (default ``.winter``), home-relative (``~``), or absolute."""

    kinds: dict[str, str] = Field(default_factory=dict)
    """Per-kind directory overrides from ``[space.kinds]``. Dynamic keys; an absent kind
    defaults to a ``<root>/<kind>`` sub-directory."""


class WorkspaceConfig(BaseModel):
    """Immutable configuration snapshot for the current workspace."""

    workspace_root: Path
    """Absolute path to the workspace root, identified by the presence of a .winter/ directory."""

    session_prefix: str
    """Prefix for tmux session names (e.g. 'myproj' produces 'myproj-alpha'). Defaults to 'winter'."""

    main_branch: str
    """Workspace-default main branch. Each project entry can override via its own `main_branch` field."""

    git_excludes: list[str] = Field(default_factory=list)
    """Workspace-wide entries written into every repo's .git/info/exclude on init."""

    base_port: int = 4000
    """Start of this workspace's port band. Per-env port base = base_port + index * ports_per_env."""

    ports_per_env: int = 20
    """Number of ports allocated per feature environment."""

    env_aliases: list[str] = Field(default_factory=lambda: list(_DEFAULT_ENV_ALIASES))
    """Fixed-index env names (1..N). Aliases get stable index slots; all other names hash into the remainder."""

    envs_per_workspace: int = 48
    """Maximum number of feature-env indices (1..envs_per_workspace). Must be >= len(env_aliases) + 2."""

    git_identity: GitIdentity | None = None
    """Git author identity applied to every repo winter-cli manages. Typically from config.local.toml."""

    adopt_extensions: AdoptExtensions = AdoptExtensions.winter
    """Controls which standalone repos contribute skills/agents to .claude/.

    `winter` (default) — only repos with a winter-ext.toml file are processed.
    `all` — any standalone repo with skills/ or agents/ directories is processed.
    `none` — no symlinking happens during init."""

    singleton_repos: list[SingletonRepository] = Field(default_factory=list)
    """Implicit repos discovered from the filesystem (workspace, product, harness)."""

    project_repos: list[ProjectRepositoryConfig] = Field(default_factory=list)
    """Repos declared in `[[project_repository]]`."""

    standalone_repos: list[StandaloneRepositoryConfig] = Field(default_factory=list)
    """Repos declared in `[[standalone_repository]]`."""

    service_orchestrator: str | None = None
    """Deprecated — use `capabilities["service"]` instead (kept for back-compat).

    Previously the direct key for the extension that orchestrates services. At load
    time, when no explicit `capabilities.service` is set, this value is folded into
    `capabilities["service"]` automatically. Still parsed and aliased so existing
    configs without a `[capabilities]` table continue to work unchanged."""

    capabilities: dict[str, list[str]] = Field(default_factory=dict)
    """Maps a capability slot name (e.g. `service`) to an ordered list of provider
    extension names for that slot.

    In `.winter/config.toml` a slot value may be a string OR a list of strings;
    both forms are normalized to a list at parse time (string → one-element list).
    Supersedes the deprecated `service_orchestrator` root key, which is folded into
    `capabilities["service"]` at load time when no explicit `capabilities.service`
    is set. Only `service` is currently a known slot."""

    doctor: str | None = None
    """Optional path to a workspace-level `winter doctor` probe script (relative to workspace_root).

    Symmetric with the per-extension `doctor` field in `winter-ext.toml`. When
    set, the script runs after core probes and before extension probes, and is
    expected to emit one NDJSON probe event per stdout line."""

    lint: list[str] = Field(default_factory=list)
    """Workspace-level `winter lint` check scripts (paths relative to workspace_root).

    Symmetric with the per-extension `lint` field in `winter-ext.toml`. Accepts
    a single path or a list; a bare string is coerced to a one-element list. Each
    script runs before extension checks and emits one NDJSON finding per stdout
    line (optionally with `file`/`line`). Hosts ecosystem-general checks the
    workspace owns but no single extension does. Empty by default."""

    file_size_lint: FileSizeLintConfig = Field(default_factory=FileSizeLintConfig)
    """Byte-size thresholds for the built-in agent-facing markdown file-size check.

    Configure under ``[core_checks.file_size]`` in ``.winter/config.toml`` to
    override the 6 000-byte (injected) and 12 000-byte (reference) defaults."""

    keybindings: KeybindingsConfig = Field(default_factory=KeybindingsConfig)
    """Dashboard keybinding overrides from the `[keybindings]` table."""

    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    """Dashboard layout configuration from the `[tui.dashboard]` table."""

    skill_prefix: str | None = None
    """Workspace-owned skill prefix for workspace_root/skills/ projection.

    When set, `winter ws init` projects skills under `workspace_root/skills/` into
    every code-agent vendor's skills directory using the per-vendor install strategies
    (symlink for ClaudeCode/Codex, copy for OpenCode). Stale `<prefix>-*` entries
    are pruned on each reconcile pass.

    Maps to the top-level `prefix` key in `.winter/config.toml`. When absent,
    the `workspace_root/skills/` directory is not projected into any vendor
    skills directory.
    """

    provision_raw: dict = Field(default_factory=dict)
    """Raw ``[provision]`` table from the merged config.

    Stored without strict parsing so a malformed ``[[provision.*]]`` entry does
    not break unrelated commands (e.g. ``winter ws status``).  Call
    ``parse_provision`` to run the strict ``ProvisionManifestParser`` on demand.
    """

    service_defs_raw: list = Field(default_factory=list)
    """Raw ``[[service]]`` array from the merged workspace config.

    Stored without strict parsing so a malformed entry does not break unrelated
    commands.  Call ``parse_service_defs`` to run the strict
    ``ExtServiceManifestParser`` on demand.
    """

    env_bands: EnvVarBands = Field(default_factory=EnvVarBands)
    """Scope-split env-var bands from ``[env.workspace.vars]`` and ``[env.feature.vars]``.

    ``EnvProvisionerService.compute()`` selects bands by scope: workspace scope
    renders only the ``workspace`` band; feature scope renders ``workspace`` first
    then ``feature`` on top (feature wins key collisions).
    """

    space: SpaceConfig = Field(default_factory=SpaceConfig)
    """Generated-artifact space configuration from the ``[space]`` table.

    ``winter space <kind>`` resolves a directory via :meth:`space_dir`; extensions
    read it instead of hardcoding an artifact path.
    """

    def port_base_for_index(self, index: int) -> int:
        """Return the per-env port base for the given env index.

        Derived from config: ``base_port + index * ports_per_env``.
        """
        return self.base_port + index * self.ports_per_env

    def space_dir(self, kind: str) -> Path:
        """Resolve the absolute directory for an artifact *kind* in the winter space.

        The space root (``space.root``) resolves relative to ``workspace_root``
        unless it is home-relative (``~``) or absolute. The kind's directory is
        its ``space.kinds`` override (defaulting to the bare kind name) resolved
        relative to that root, again with ``~``/absolute as escapes. Pure: this
        computes a path and creates nothing. The ``winter space`` command prints
        it; the caller materializes the directory if it writes into it.
        """
        root = self._resolve_space_path(self.space.root, base=self.workspace_root)
        override = self.space.kinds.get(kind, kind)
        return self._resolve_space_path(override, base=root)

    @staticmethod
    def _resolve_space_path(value: str, base: Path) -> Path:
        """Resolve a space path *value* against *base* by the ~/absolute/relative rule.

        Leading ``~`` expands to the home directory (absolute); an absolute path
        is returned as-is; any other (relative) value joins onto ``base``.
        """
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        return base / candidate
