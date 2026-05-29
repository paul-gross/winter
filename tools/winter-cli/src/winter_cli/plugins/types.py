"""Public contract for winter TUI plugins (`plugin.py` + `create_plugin()`).

These names are the plugin author's API surface. Renaming `IWinterPlugin`,
`PluginRegistration`, `IWorktreeRepoDecorator`, `IEnvironmentDecorator`,
`TuiAction`, or `ActionScope` is a breaking change for external plugins —
update the authoring doc in the same change: `winter-harness:/python/plugin-author.md`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import click

from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureWorktree,
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


class ActionScope(enum.Enum):
    workspace = "workspace"
    """Action operates on the entire workspace."""

    feature_environment = "feature_environment"
    """Action operates on a feature environment (e.g. alpha, beta)."""

    feature_worktree = "feature_worktree"
    """Action operates on a specific repo worktree within a feature environment."""


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


ActionContext = WorkspaceContext | FeatureEnvironmentContext | FeatureWorktreeContext

ActionHandler = Callable[[ActionContext], None]


@dataclasses.dataclass
class TuiAction:
    name: str
    """Unique identifier for the action (e.g. 'codediff')."""

    scope: ActionScope
    """Determines what context the handler receives."""

    key: str
    """Keybinding to trigger this action (e.g. 'e')."""

    description: str
    """Short label shown in the TUI footer."""

    handler: ActionHandler
    """Callable invoked with the appropriate context."""


@dataclasses.dataclass
class PluginRegistration:
    commands: list[click.Command] = dataclasses.field(default_factory=list)
    worktree_repo_decorators: list[IWorktreeRepoDecorator] = dataclasses.field(default_factory=list)
    environment_decorators: list[IEnvironmentDecorator] = dataclasses.field(default_factory=list)
    tui_screens: list[Any] = dataclasses.field(default_factory=list)
    tui_actions: list[TuiAction] = dataclasses.field(default_factory=list)
    metadata: dict = dataclasses.field(default_factory=dict)


@runtime_checkable
class IWinterPlugin(Protocol):
    name: str

    def register(self, config: object) -> PluginRegistration: ...
