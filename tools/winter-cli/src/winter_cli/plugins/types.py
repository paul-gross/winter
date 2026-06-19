"""Public contract for winter TUI plugins (`plugin.py` + `create_plugin()`).

These names are the plugin author's API surface. Renaming `IWinterPlugin`,
`PluginRegistration`, `IWorktreeRepoDecorator`, `IEnvironmentDecorator`,
`IDetailPanel`, `DetailPanelContext`, `TuiAction`, `ActionScope`, or
`ActionInvocation` is a breaking change for external plugins — update the
authoring doc in the same change: `winter-harness:/architecture/plugin-author.md`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

import click

from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureWorktree,
    StandaloneRepository,
    Workspace,
)


@runtime_checkable
class IWorktreeRepoDecorator(Protocol):
    """Mutates a worktree-repo status row with extension-contributed badges.

    Called once per repo per refresh; populate `repo_status.extensions[<key>] = <value>`
    to surface a badge in the dashboard's repo row.
    """

    def __call__(self, repo_status: object, repo_path: object) -> None: ...


@runtime_checkable
class IEnvironmentDecorator(Protocol):
    """Mutates a feature-environment status with extension-contributed badges.

    Called once per environment per refresh; populate
    `env_status.extensions[<key>] = <value>` to surface a badge in the env's
    column header (matrix grid) and detail-screen header. The plugin owns the
    rendering decision — anything you put in `extensions.values()` is appended
    to the cell verbatim, joined by spaces.
    """

    def __call__(self, env_status: object, env_path: object) -> None: ...


@dataclasses.dataclass
class DetailPanelContext:
    """The focused repo a detail panel renders for.

    Exactly one field is set: `worktree` in a feature-environment detail view
    (the repo row the cursor is on), `repo` in a standalone detail view. This is
    the panel analog of the `worktree` / `repo` that `IWorktreeRepoDecorator`
    and `IEnvironmentDecorator` are handed — a panel reads it to decide what to
    render and must not mutate it.
    """

    worktree: FeatureWorktree | None = None
    repo: StandaloneRepository | None = None


@runtime_checkable
class IDetailPanel(Protocol):
    """Contributes a named, read-only info panel to the detail screen.

    Rendered as a tab alongside the built-in repo info, in both the feature-env
    and standalone detail views. `name` is a stable identifier; `title` is the
    tab label. `render` is called once per detail refresh with the focused
    repo's `DetailPanelContext` and returns rich-console markup (a `str`) or any
    Rich renderable shown in the panel body. Raising is isolated by the screen —
    the panel shows an error state and the rest of the screen keeps rendering,
    matching the decorator error handling.
    """

    name: str
    title: str

    def render(self, context: DetailPanelContext) -> object: ...


class ActionScope(enum.Enum):
    workspace = "workspace"
    """Action operates on the entire workspace."""

    feature_environment = "feature_environment"
    """Action operates on a feature environment (e.g. alpha, beta)."""

    feature_worktree = "feature_worktree"
    """Action operates on a specific repo worktree within a feature environment."""

    standalone_repository = "standalone_repository"
    """Action operates on a standalone repo (singleton or user-declared) in the standalone panel."""


SuspendFn = Callable[[], "contextlib.AbstractContextManager[None]"]


@dataclasses.dataclass
class WorkspaceContext:
    workspace: Workspace
    suspend: SuspendFn | None = None


@dataclasses.dataclass
class FeatureEnvironmentContext:
    environment: FeatureEnvironment
    suspend: SuspendFn | None = None


@dataclasses.dataclass
class FeatureWorktreeContext:
    worktree: FeatureWorktree
    suspend: SuspendFn | None = None


@dataclasses.dataclass
class StandaloneRepoContext:
    repo: StandaloneRepository
    suspend: SuspendFn | None = None


ActionContext = WorkspaceContext | FeatureEnvironmentContext | FeatureWorktreeContext | StandaloneRepoContext


@dataclasses.dataclass
class ActionInvocation:
    """Wrapper passed to every action handler.

    Carries the originating `scope` so multi-scope handlers can branch on which
    area triggered the action, and the resolved `context` holding the selection.
    Unknown attribute lookups delegate to the inner `context` so existing
    handlers that read `ctx.repo`, `ctx.worktree`, `ctx.environment`,
    `ctx.workspace`, or `ctx.suspend` keep working without modification.
    """

    scope: ActionScope
    context: ActionContext

    def __getattr__(self, name: str) -> Any:
        # Delegate unknown attributes to the wrapped context so handlers
        # written against the bare context keep working. Only reached for
        # attributes not found normally (so `scope`/`context` never recurse).
        try:
            return getattr(self.context, name)
        except AttributeError:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}") from None


ActionHandler = Callable[[ActionInvocation], None]


@dataclasses.dataclass
class TuiAction:
    name: str
    """Unique identifier for the action (e.g. 'codediff')."""

    scope: ActionScope | Sequence[ActionScope]
    """Determines what context the handler receives.

    Accepts a single `ActionScope` or a sequence of scopes. All internal code
    reads the normalized `scopes` frozenset instead; `scope` is the author-facing
    input surface only.
    """

    key: str
    """Keybinding to trigger this action (e.g. 'e')."""

    description: str
    """Short label shown in the TUI footer."""

    handler: ActionHandler
    """Callable invoked with the appropriate context."""

    scopes: frozenset[ActionScope] = dataclasses.field(init=False, repr=False)
    """Normalized read surface — always a frozenset regardless of how `scope` was provided."""

    def __post_init__(self) -> None:
        if isinstance(self.scope, ActionScope):
            object.__setattr__(self, "scopes", frozenset({self.scope}))
        else:
            normalized = frozenset(self.scope)
            if not normalized:
                raise ValueError("TuiAction requires at least one ActionScope")
            object.__setattr__(self, "scopes", normalized)


@dataclasses.dataclass
class PluginRegistration:
    commands: list[click.Command] = dataclasses.field(default_factory=list)
    worktree_repo_decorators: list[IWorktreeRepoDecorator] = dataclasses.field(default_factory=list)
    environment_decorators: list[IEnvironmentDecorator] = dataclasses.field(default_factory=list)
    detail_panels: list[IDetailPanel] = dataclasses.field(default_factory=list)
    tui_screens: list[Any] = dataclasses.field(default_factory=list)
    tui_actions: list[TuiAction] = dataclasses.field(default_factory=list)
    metadata: dict = dataclasses.field(default_factory=dict)


@runtime_checkable
class IWinterPlugin(Protocol):
    name: str

    def register(self, config: object) -> PluginRegistration: ...
