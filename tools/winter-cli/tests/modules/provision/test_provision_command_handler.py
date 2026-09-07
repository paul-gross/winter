"""Tests for `ProvisionCommandHandler` PATTERNS resolution and fan-out.

Covers:
- A single literal env pattern provisions just that env (no discovery).
- Multiple literal env patterns provision each, in deterministic (sorted) order.
- A glob pattern provisions every discovered env it matches.
- A glob matching no discovered env prints a message and provisions nothing.
- A mixed literal + glob invocation dedupes overlapping envs.
- A failed env's non-zero summary exit code propagates to sys.exit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from winter_cli.modules.provision.handler import ProvisionCommandHandler, ProvisionParams
from winter_cli.modules.provision.manifest import ProvisionAction


class _FakeProvisionService:
    """Records env_name per `run()` call; returns a configurable status per env."""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self._statuses = statuses or {}

    def run(
        self,
        env_name: str,
        subtarget: str | None,
        action: ProvisionAction,
        seed: bool,
        no_service_check: bool,
        reporter: Any,
        dry_run: bool = False,
        name_selector: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append(env_name)
        status = self._statuses.get(env_name, "ok")
        return SimpleNamespace(status=status, exit_code=0 if status == "ok" else 1)


def _make_handler(
    discovered_envs: list[str] | None = None,
    statuses: dict[str, str] | None = None,
) -> tuple[ProvisionCommandHandler, _FakeProvisionService, MagicMock]:
    workspace_repo = MagicMock()
    workspace_repo.get_environments.return_value = [
        SimpleNamespace(name=n) for n in (discovered_envs if discovered_envs is not None else ["alpha", "beta"])
    ]

    repo_factory = MagicMock()
    repo_factory.get_project_repos.return_value = []

    provision_svc = _FakeProvisionService(statuses)
    handler = ProvisionCommandHandler(
        provision_service=provision_svc,  # type: ignore[arg-type]
        stream_reporter=MagicMock(),
        json_reporter=MagicMock(),
        workspace_repo=workspace_repo,
        repo_factory=repo_factory,
        workspace=MagicMock(),
    )
    return handler, provision_svc, workspace_repo


def test_single_literal_env_provisions_just_that_env() -> None:
    handler, svc, workspace_repo = _make_handler()
    handler.run(ProvisionParams(patterns=["alpha"]))

    assert svc.calls == ["alpha"]
    workspace_repo.get_environments.assert_not_called()


def test_multiple_literal_envs_provisions_each_in_deterministic_order() -> None:
    handler, svc, _ = _make_handler()
    handler.run(ProvisionParams(patterns=["beta", "alpha"]))

    assert svc.calls == ["alpha", "beta"]


def test_glob_matching_several_provisions_each_discovered_env() -> None:
    handler, svc, workspace_repo = _make_handler(discovered_envs=["alpha", "beta", "gamma"])
    handler.run(ProvisionParams(patterns=["*"]))

    assert svc.calls == ["alpha", "beta", "gamma"]
    workspace_repo.get_environments.assert_called_once()


def test_glob_matching_none_provisions_nothing_and_reports(capsys: pytest.CaptureFixture[Any]) -> None:
    handler, svc, _ = _make_handler(discovered_envs=["alpha", "beta"])
    handler.run(ProvisionParams(patterns=["zzz-*"]))

    assert svc.calls == []
    assert "No environments matched" in capsys.readouterr().out


def test_literal_and_glob_combo_dedupes_overlap() -> None:
    handler, svc, _ = _make_handler(discovered_envs=["alpha", "beta"])
    handler.run(ProvisionParams(patterns=["alpha", "*"]))

    assert svc.calls == ["alpha", "beta"]


def test_failed_env_exits_nonzero() -> None:
    handler, _, _ = _make_handler(statuses={"alpha": "error"})

    with pytest.raises(SystemExit) as excinfo:
        handler.run(ProvisionParams(patterns=["alpha"]))

    assert excinfo.value.code != 0


def test_all_succeed_no_system_exit() -> None:
    handler, _, _ = _make_handler()
    handler.run(ProvisionParams(patterns=["alpha"]))  # no SystemExit raised


def test_one_of_multiple_envs_failing_still_exits_nonzero() -> None:
    handler, svc, _ = _make_handler(statuses={"beta": "error"})

    with pytest.raises(SystemExit):
        handler.run(ProvisionParams(patterns=["alpha", "beta"]))

    # Both envs still ran despite the failure.
    assert svc.calls == ["alpha", "beta"]
