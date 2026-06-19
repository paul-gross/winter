"""Stable action ids for every built-in dashboard action.

Each built-in action has a documented, config-addressable id (`workspace.refresh`,
`worktree.open_detail`, `app.quit`, ...). Config maps these ids to key specs; an
absent id falls back to the `default` here. Plugin actions get ids of the form
`plugin.<name>` (built dynamically from the plugin registry).

The agent-facing list of ids lives in `winter:/ai/winter-cli/usage/dashboard.md` —
keep it in sync when adding, renaming, or removing an entry here.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import ActionScope

PLUGIN_ID_PREFIX = "plugin."


@dataclasses.dataclass(frozen=True)
class ActionBinding:
    """A dashboard action, its default key spec, and the Textual action it runs."""

    action_id: str
    """Stable, config-addressable id (e.g. 'workspace.refresh')."""

    default: str
    """Default key spec, used when config has no override for this id."""

    action: str
    """Textual action target dispatched when the key fires (e.g. 'refresh')."""

    description: str
    """Footer label."""

    show: bool = True
    """Whether the binding appears in the footer."""

    default_is_token: bool = False
    """When True, `default` is a literal Textual key token (a plugin's
    `TuiAction.key`), taken verbatim rather than parsed through the user-facing
    key-spec grammar. A config override always uses the grammar regardless."""


# `app.quit` is offered on the workspace screen (the only screen where quitting,
# rather than going back, is the q action). Detail screens bind their own back.
# Its Textual target is the `app.`-namespaced `app.quit`, not a bare `quit`:
# bindings install on the screen, and Textual dispatches a bare action against
# the screen namespace, which has no `action_quit` (that method lives on App).
WORKSPACE_ACTIONS: tuple[ActionBinding, ...] = (
    ActionBinding("workspace.refresh", "r", "refresh", "Refresh"),
    ActionBinding("workspace.open_log", "L", "open_log", "Log"),
    ActionBinding("workspace.cycle_layout", "t", "cycle_layout", "Cycle layout"),
    ActionBinding("app.quit", "q", "app.quit", "Quit"),
    # Lives on the workspace screen (the grid's row → detail drill-in) but keeps
    # the issue's `worktree.open_detail` id since it opens a worktree's detail.
    ActionBinding("worktree.open_detail", "<enter>", "open_detail", "Open", show=False),
    ActionBinding("workspace.jump_prev", "<C-k>", "jump_prev", "Jump prev", show=False),
    ActionBinding("workspace.jump_next", "<C-j>", "jump_next", "Jump next", show=False),
)

WORKTREE_DETAIL_ACTIONS: tuple[ActionBinding, ...] = (
    ActionBinding("worktree.refresh", "r", "refresh", "Refresh"),
    ActionBinding("worktree.open_log", "L", "open_log", "Log"),
    ActionBinding("worktree.back", "q", "back", "Back"),
    ActionBinding("worktree.cursor_left", "h", "cursor_left", "Left", show=False),
    ActionBinding("worktree.cursor_down", "j", "cursor_down", "Down", show=False),
    ActionBinding("worktree.cursor_up", "k", "cursor_up", "Up", show=False),
    ActionBinding("worktree.cursor_right", "l", "cursor_right", "Right", show=False),
)

STANDALONE_DETAIL_ACTIONS: tuple[ActionBinding, ...] = (
    ActionBinding("standalone.refresh", "r", "refresh", "Refresh"),
    ActionBinding("standalone.open_log", "L", "open_log", "Log"),
    ActionBinding("standalone.back", "q", "back", "Back"),
)


def all_builtin_action_ids() -> set[str]:
    """Every built-in action id, across all screens — the unknown-id allowlist."""
    return {
        ab.action_id
        for group in (WORKSPACE_ACTIONS, WORKTREE_DETAIL_ACTIONS, STANDALONE_DETAIL_ACTIONS)
        for ab in group
    }


def plugin_action_bindings(
    registry: PluginRegistry,
    scopes: Iterable[ActionScope],
) -> list[ActionBinding]:
    """Build `ActionBinding`s for plugin actions in `scopes`, id'd `plugin.<name>`."""
    bindings: list[ActionBinding] = []
    for scope in scopes:
        for action in registry.actions_for_scope(scope):
            bindings.append(
                ActionBinding(
                    action_id=f"{PLUGIN_ID_PREFIX}{action.name}",
                    default=action.key,
                    action=f"plugin_{action.name}",
                    description=action.description,
                    default_is_token=True,
                )
            )
    return bindings
