from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field


class SingletonType(enum.Enum):
    workspace = "workspace"
    """The workspace repo itself — contains .winter/, ai/, workflow scripts."""

    product = "product"
    """The product branch worktree — orphan branch for plans and TODOs."""

    harness = "harness"
    """The agentic harness repo — cross-repo documentation at ai/harness/."""


class AdoptExtensions(enum.Enum):
    none = "none"
    """Never symlink skills/agents from standalone repos."""

    winter = "winter"
    """Symlink skills/agents only when the standalone repo declares winter-ext.toml."""

    all = "all"
    """Symlink skills/agents from any standalone repo, with or without winter-ext.toml."""


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
    """Shell commands run idempotently after clone and in every worktree."""


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

    git_excludes: list[str] = Field(default_factory=list)
    """Per-repo entries added to .git/info/exclude after clone."""

    cmd: list[str] = Field(default_factory=list)
    """Shell commands run idempotently after clone."""


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

    doctor: str | None = None
    """Optional path to a workspace-level `winter doctor` probe script (relative to workspace_root).

    Symmetric with the per-extension `doctor` field in `winter-ext.toml`. When
    set, the script runs after core probes and before extension probes, and is
    expected to emit one NDJSON probe event per stdout line."""

    lint: str | None = None
    """Optional path to a workspace-level `winter lint` check script (relative to workspace_root).

    Symmetric with the per-extension `lint` field in `winter-ext.toml`. When
    set, the script runs before extension checks and emits one NDJSON finding
    per stdout line (optionally with `file`/`line`). Hosts ecosystem-general
    checks the workspace owns but no single extension does."""

    keybindings: KeybindingsConfig = Field(default_factory=KeybindingsConfig)
    """Dashboard keybinding overrides from the `[keybindings]` table."""
