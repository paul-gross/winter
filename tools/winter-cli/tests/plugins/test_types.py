"""Unit tests for TuiAction.scopes normalization and ActionInvocation delegation.

Covers the Phase 1 type-layer changes (winter#58): multi-scope `TuiAction`
creation, empty-sequence rejection, and `ActionInvocation.__getattr__` pass-through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winter_cli.modules.workspace.models import StandaloneRepository
from winter_cli.plugins.types import (
    ActionInvocation,
    ActionScope,
    StandaloneRepoContext,
    TuiAction,
)

_NOP = lambda inv: None  # noqa: E731  — minimal handler placeholder


class TestTuiActionScopesNormalization:
    def test_single_scope_produces_frozenset_with_one_element(self) -> None:
        action = TuiAction(
            name="x",
            scope=ActionScope.workspace,
            key="a",
            description="d",
            handler=_NOP,
        )
        assert action.scopes == frozenset({ActionScope.workspace})

    def test_list_of_two_scopes_produces_frozenset_with_both(self) -> None:
        action = TuiAction(
            name="x",
            scope=[ActionScope.workspace, ActionScope.standalone_repository],
            key="a",
            description="d",
            handler=_NOP,
        )
        assert action.scopes == frozenset({ActionScope.workspace, ActionScope.standalone_repository})

    def test_list_of_all_four_scopes(self) -> None:
        all_scopes = list(ActionScope)
        action = TuiAction(
            name="x",
            scope=all_scopes,
            key="a",
            description="d",
            handler=_NOP,
        )
        assert action.scopes == frozenset(ActionScope)

    def test_empty_sequence_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="at least one ActionScope"):
            TuiAction(
                name="x",
                scope=[],
                key="a",
                description="d",
                handler=_NOP,
            )

    def test_scope_field_preserved_for_single_value(self) -> None:
        action = TuiAction(
            name="x",
            scope=ActionScope.feature_environment,
            key="a",
            description="d",
            handler=_NOP,
        )
        assert action.scope is ActionScope.feature_environment

    def test_scope_field_preserved_for_list(self) -> None:
        scopes_in = [ActionScope.workspace, ActionScope.standalone_repository]
        action = TuiAction(
            name="x",
            scope=scopes_in,
            key="a",
            description="d",
            handler=_NOP,
        )
        assert action.scope is scopes_in


class TestActionInvocationDelegation:
    def _make_invocation(self) -> tuple[ActionInvocation, StandaloneRepoContext]:
        repo = StandaloneRepository(name="winter-harness", path=Path("/ws/harness"))
        ctx = StandaloneRepoContext(repo=repo)
        inv = ActionInvocation(scope=ActionScope.standalone_repository, context=ctx)
        return inv, ctx

    def test_scope_returns_originating_scope(self) -> None:
        inv, _ = self._make_invocation()
        assert inv.scope is ActionScope.standalone_repository

    def test_context_returns_inner_context(self) -> None:
        inv, ctx = self._make_invocation()
        assert inv.context is ctx

    def test_repo_delegates_to_inner_context(self) -> None:
        inv, ctx = self._make_invocation()
        assert inv.repo is ctx.repo
        assert inv.repo.name == "winter-harness"

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        inv, _ = self._make_invocation()
        with pytest.raises(AttributeError, match="ActionInvocation"):
            _ = inv.nonexistent_attribute  # type: ignore[attr-defined]
