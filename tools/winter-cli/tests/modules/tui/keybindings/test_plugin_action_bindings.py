"""Tests for plugin_action_bindings() one-binding-per-action shape (issue/58, phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from winter_cli.modules.tui.keybindings.actions import (
    WORKSPACE_ACTIONS,
    plugin_action_bindings,
)
from winter_cli.plugins.types import ActionScope, TuiAction


def _make_action(name: str, scope: ActionScope | list[ActionScope], key: str = "e") -> TuiAction:
    return TuiAction(
        name=name,
        scope=scope,
        key=key,
        description=f"Do {name}",
        handler=MagicMock(),
    )


class _FakeRegistry:
    def __init__(self, actions: list[TuiAction]) -> None:
        self.tui_actions = actions

    def actions_for_scope(self, scope: ActionScope) -> list[TuiAction]:
        return [a for a in self.tui_actions if scope in a.scopes]


# --- multi-scope action: one binding, intersection on .scopes ----------------


def test_multi_scope_action_yields_one_binding() -> None:
    """A multi-scope TuiAction produces exactly ONE ActionBinding."""
    action = _make_action(
        "codediff",
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="d",
    )
    registry = _FakeRegistry([action])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
    )

    assert len(bindings) == 1
    b = bindings[0]
    assert b.action_id == "plugin.codediff"
    assert b.action == "plugin_codediff"
    assert b.default == "d"
    assert b.default_is_token is True
    assert b.description == "Do codediff"


def test_multi_scope_action_scopes_is_intersection() -> None:
    """The binding's .scopes is the intersection of action.scopes and requested scopes."""
    action = _make_action(
        "codediff",
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="d",
    )
    registry = _FakeRegistry([action])

    # Only ask for standalone_repository — intersection is {standalone_repository}
    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.standalone_repository],
    )

    assert len(bindings) == 1
    assert bindings[0].scopes == frozenset({ActionScope.standalone_repository})


def test_multi_scope_action_full_intersection_when_all_requested() -> None:
    """When all action scopes are requested, the full intersection is preserved."""
    action = _make_action(
        "multi",
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="m",
    )
    registry = _FakeRegistry([action])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        list(ActionScope),  # all scopes
    )

    assert len(bindings) == 1
    assert bindings[0].scopes == frozenset({ActionScope.feature_worktree, ActionScope.standalone_repository})


# --- single-scope action: filtered or included --------------------------------


def test_single_scope_action_filtered_when_scope_not_requested() -> None:
    """A single-scope action is excluded when its scope isn't in the requested set."""
    action = _make_action("worktree_only", ActionScope.feature_worktree, key="w")
    registry = _FakeRegistry([action])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.standalone_repository],  # worktree not requested
    )

    assert bindings == []


def test_single_scope_action_included_when_scope_requested() -> None:
    """A single-scope action is included (one binding) when its scope is requested."""
    action = _make_action("worktree_only", ActionScope.feature_worktree, key="w")
    registry = _FakeRegistry([action])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.feature_worktree],
    )

    assert len(bindings) == 1
    assert bindings[0].action_id == "plugin.worktree_only"
    assert bindings[0].scopes == frozenset({ActionScope.feature_worktree})


# --- multiple actions: no duplicates -----------------------------------------


def test_two_actions_yield_two_bindings() -> None:
    """Two distinct actions each produce exactly one binding."""
    a1 = _make_action("alpha", ActionScope.feature_worktree, key="a")
    a2 = _make_action("beta", ActionScope.standalone_repository, key="b")
    registry = _FakeRegistry([a1, a2])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
    )

    assert len(bindings) == 2
    ids = {b.action_id for b in bindings}
    assert ids == {"plugin.alpha", "plugin.beta"}


def test_multi_scope_action_does_not_duplicate() -> None:
    """A multi-scope action matching multiple requested scopes produces only ONE binding."""
    action = _make_action(
        "shared",
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="s",
    )
    registry = _FakeRegistry([action])

    bindings = plugin_action_bindings(
        registry,  # type: ignore[arg-type]
        [ActionScope.feature_worktree, ActionScope.standalone_repository],
    )

    assert len(bindings) == 1
    assert bindings[0].action_id == "plugin.shared"


# --- built-in bindings have empty scopes -------------------------------------


def test_builtin_bindings_have_empty_scopes() -> None:
    """All built-in ActionBindings keep empty scopes (the default)."""
    for group in (WORKSPACE_ACTIONS,):
        for ab in group:
            assert ab.scopes == frozenset(), (
                f"Built-in binding {ab.action_id!r} should have empty scopes, got {ab.scopes!r}"
            )


def test_empty_registry_returns_empty_list() -> None:
    """An empty registry produces no bindings."""
    registry = _FakeRegistry([])
    bindings = plugin_action_bindings(registry, list(ActionScope))  # type: ignore[arg-type]
    assert bindings == []
