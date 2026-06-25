"""Tests for ProvisionServiceCheck — Phase 5 required-services gate.

Injects fakes for:
- ServiceStatusService (via FakeStatusService returning crafted StatusDocuments)
- ServiceDispatchService (via FakeDispatchService recording up calls)
- RepoError raised from status service to simulate missing orchestrator

Cross-env token policy: only workspace/<svc> and <env>/<svc> tokens allowed.
"""

from __future__ import annotations

import pytest

from winter_cli.modules.provision.manifest import ProvisionHandler, ProvisionScope
from winter_cli.modules.provision.service_check_service import ProvisionServiceCheck
from winter_cli.modules.service.status_models import EnvStatus, ServiceStatus, StatusDocument
from winter_cli.modules.workspace.models import RepoError

ENV_NAME = "alpha"


# ---------------------------------------------------------------------------
# Fake status document builders
# ---------------------------------------------------------------------------


def _make_service_status(name: str, state: str, health: str = "unknown") -> ServiceStatus:
    return ServiceStatus(name=name, state=state, health=health, ports=(), handle=None, log_path=None, since=None)


def _make_doc(env: str, services: list[ServiceStatus]) -> StatusDocument:
    return StatusDocument(envs=(EnvStatus(env=env, session=None, port_base=None, services=tuple(services)),))


def _merge_docs(*docs: StatusDocument) -> StatusDocument:
    envs = tuple(env for doc in docs for env in doc.envs)
    return StatusDocument(envs=envs)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStatusService:
    """Fake for ServiceStatusService — returns a canned StatusDocument on collect()."""

    def __init__(self, doc: StatusDocument | None = None, raises: Exception | None = None) -> None:
        self._doc = doc
        self._raises = raises
        self.collect_calls: list[tuple[str, ...]] = []

    def collect(self, patterns: tuple[str, ...]) -> StatusDocument | None:
        self.collect_calls.append(patterns)
        if self._raises is not None:
            raise self._raises
        return self._doc


class FakeDispatchService:
    """Fake for ServiceDispatchService — records dispatch() calls and returns 0."""

    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        self.dispatch_calls: list[tuple[str, list[str]]] = []

    def dispatch(self, action: str, positionals: list[str]) -> int:
        self.dispatch_calls.append((action, list(positionals)))
        return self._exit_code


# ---------------------------------------------------------------------------
# Helper: build ProvisionServiceCheck with injected fakes
# ---------------------------------------------------------------------------


def _make_check(
    doc: StatusDocument | None = None,
    status_raises: Exception | None = None,
    dispatch_exit: int = 0,
) -> tuple[ProvisionServiceCheck, FakeStatusService, FakeDispatchService]:
    status_svc = FakeStatusService(doc=doc, raises=status_raises)
    dispatch_svc = FakeDispatchService(exit_code=dispatch_exit)
    check = ProvisionServiceCheck(
        status_svc=status_svc,  # type: ignore[arg-type]
        dispatch_svc=dispatch_svc,  # type: ignore[arg-type]
    )
    return check, status_svc, dispatch_svc


def _handler(required_services: tuple[str, ...] = (), subtarget: str = "resource") -> ProvisionHandler:
    return ProvisionHandler(
        subtarget=subtarget,
        scope=ProvisionScope.workspace,
        apply=("scripts/apply.sh",),
        source="project",
        required_services=required_services,
    )


# ---------------------------------------------------------------------------
# Tests: no required_services → returns None
# ---------------------------------------------------------------------------


def test_no_required_services_returns_none() -> None:
    """When no handler has required_services, ensure() returns None (no check needed)."""
    check, status_svc, dispatch_svc = _make_check()
    handlers = [_handler(required_services=()), _handler(required_services=())]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is None
    assert not status_svc.collect_calls
    assert not dispatch_svc.dispatch_calls


# ---------------------------------------------------------------------------
# Tests: no_service_check=True → skipped
# ---------------------------------------------------------------------------


def test_no_service_check_returns_skipped_without_calling_status() -> None:
    """--no-service-check skips all queries and returns 'skipped'."""
    check, status_svc, dispatch_svc = _make_check()
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=True)
    assert result == "skipped"
    assert not status_svc.collect_calls
    assert not dispatch_svc.dispatch_calls


def test_no_service_check_skips_even_when_no_required_services() -> None:
    """no_service_check=True on handlers with no required_services still returns None (step 1 exits first)."""
    check, status_svc, _ = _make_check()
    handlers = [_handler(required_services=())]
    # Step 1: union is empty → returns None before reaching step 2
    result = check.ensure(handlers, ENV_NAME, no_service_check=True)
    assert result is None
    assert not status_svc.collect_calls


# ---------------------------------------------------------------------------
# Tests: all services up → no dispatch, returns "ok"
# ---------------------------------------------------------------------------


def test_all_services_up_returns_ok_no_dispatch() -> None:
    """All required services running → no dispatch, returns 'ok'."""
    doc = _make_doc("workspace", [_make_service_status("postgres", state="running")])
    check, status_svc, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result == "ok"
    assert len(status_svc.collect_calls) == 1
    assert not dispatch_svc.dispatch_calls


def test_all_env_services_up_returns_ok() -> None:
    """Env-scoped services that are running → no dispatch."""
    doc = _make_doc(ENV_NAME, [_make_service_status("backend", state="running")])
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=(f"{ENV_NAME}/backend",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result == "ok"
    assert not dispatch_svc.dispatch_calls


# ---------------------------------------------------------------------------
# Tests: health unknown/unhealthy but running → treated as UP
# ---------------------------------------------------------------------------


def test_running_with_unhealthy_health_treated_as_up() -> None:
    """State=running, health=unhealthy → still counted as UP (running-state gates, not health)."""
    doc = _make_doc("workspace", [_make_service_status("postgres", state="running", health="unhealthy")])
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result == "ok"
    assert not dispatch_svc.dispatch_calls


def test_running_with_unknown_health_treated_as_up() -> None:
    """State=running, health=unknown → UP, no dispatch."""
    doc = _make_doc("workspace", [_make_service_status("postgres", state="running", health="unknown")])
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result == "ok"
    assert not dispatch_svc.dispatch_calls


# ---------------------------------------------------------------------------
# Tests: service stopped → dispatch up for owning scope
# ---------------------------------------------------------------------------


def test_one_workspace_service_down_dispatches_workspace_up() -> None:
    """A stopped workspace service → dispatch up for 'workspace', no down call."""
    doc = _make_doc("workspace", [_make_service_status("postgres", state="stopped")])
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and result.startswith("started:")
    assert "workspace" in result
    # Only one 'up workspace' dispatch call, never a 'down'
    assert dispatch_svc.dispatch_calls == [("up", ["workspace"])]


def test_one_env_service_down_dispatches_env_up() -> None:
    """A stopped env-scoped service → dispatch up for the env."""
    doc = _make_doc(ENV_NAME, [_make_service_status("backend", state="stopped")])
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=(f"{ENV_NAME}/backend",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    assert dispatch_svc.dispatch_calls == [("up", [ENV_NAME])]


def test_workspace_and_env_services_both_down_dispatches_both_scopes() -> None:
    """When both workspace and env services are down, dispatch up for both scopes."""
    ws_doc = _make_doc("workspace", [_make_service_status("postgres", state="stopped")])
    env_doc = _make_doc(ENV_NAME, [_make_service_status("backend", state="stopped")])
    combined = _merge_docs(ws_doc, env_doc)
    check, _, dispatch_svc = _make_check(doc=combined)
    handlers = [
        _handler(required_services=("workspace/postgres",)),
        _handler(required_services=(f"{ENV_NAME}/backend",)),
    ]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    dispatched_scopes = {call[1][0] for call in dispatch_svc.dispatch_calls if call[0] == "up"}
    assert dispatched_scopes == {"workspace", ENV_NAME}
    # No 'down' calls ever
    assert all(call[0] == "up" for call in dispatch_svc.dispatch_calls)


def test_one_service_up_one_service_down_dispatches_only_down_scope() -> None:
    """Only the scope with a down service is dispatched; the up one is left alone."""
    doc = _merge_docs(
        _make_doc("workspace", [_make_service_status("postgres", state="running")]),
        _make_doc(ENV_NAME, [_make_service_status("backend", state="stopped")]),
    )
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [
        _handler(required_services=("workspace/postgres", f"{ENV_NAME}/backend")),
    ]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    # Only env scope dispatched (workspace/postgres was running)
    assert dispatch_svc.dispatch_calls == [("up", [ENV_NAME])]


# ---------------------------------------------------------------------------
# Tests: service absent from status → treated as down
# ---------------------------------------------------------------------------


def test_service_absent_from_status_treated_as_down() -> None:
    """A service not in the status document at all is treated as down → dispatch up."""
    # Empty document — no env, no service
    doc = StatusDocument(envs=())
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    assert dispatch_svc.dispatch_calls == [("up", ["workspace"])]


def test_none_status_document_means_all_down() -> None:
    """A None status document (no parseable output) means all services treated as down."""
    check, _, dispatch_svc = _make_check(doc=None)
    handlers = [_handler(required_services=("workspace/postgres",))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    assert dispatch_svc.dispatch_calls == [("up", ["workspace"])]


# ---------------------------------------------------------------------------
# Tests: missing orchestrator
# ---------------------------------------------------------------------------


def test_missing_orchestrator_with_required_services_raises() -> None:
    """No orchestrator registered + required_services non-empty → ClickException."""
    import click

    error = RepoError("no service orchestrator registered")
    check, _, dispatch_svc = _make_check(status_raises=error)
    handlers = [_handler(required_services=("workspace/postgres",))]
    with pytest.raises(click.ClickException) as exc_info:
        check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert "no service orchestrator" in str(exc_info.value.format_message()).lower()
    # No dispatch attempted
    assert not dispatch_svc.dispatch_calls


def test_missing_orchestrator_without_required_services_no_raise() -> None:
    """No orchestrator + no required_services → returns None (orchestrator not consulted)."""
    error = RepoError("no service orchestrator registered")
    check, status_svc, _ = _make_check(status_raises=error)
    handlers = [_handler(required_services=())]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is None
    # status service was never called
    assert not status_svc.collect_calls


# ---------------------------------------------------------------------------
# Tests: union of required_services across handlers
# ---------------------------------------------------------------------------


def test_union_of_required_services_across_handlers() -> None:
    """Union of required_services from multiple handlers is checked as one batch."""
    doc = _merge_docs(
        _make_doc("workspace", [_make_service_status("postgres", state="running")]),
        _make_doc(ENV_NAME, [_make_service_status("redis", state="running")]),
    )
    check, status_svc, _ = _make_check(doc=doc)
    handlers = [
        _handler(required_services=("workspace/postgres",)),
        _handler(required_services=(f"{ENV_NAME}/redis",)),
    ]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result == "ok"
    # One collect call covering both tokens
    assert len(status_svc.collect_calls) == 1
    queried = set(status_svc.collect_calls[0])
    assert queried == {"workspace/postgres", f"{ENV_NAME}/redis"}


# ---------------------------------------------------------------------------
# Tests: cross-env token rejection
# ---------------------------------------------------------------------------


def test_cross_env_token_raises() -> None:
    """A token referencing a foreign env is rejected with a clear error."""
    import click

    check, _, _ = _make_check()
    handlers = [_handler(required_services=("gamma/backend",))]
    with pytest.raises(click.ClickException) as exc_info:
        check.ensure(handlers, ENV_NAME, no_service_check=False)
    msg = exc_info.value.format_message()
    assert "gamma" in msg
    assert ENV_NAME in msg


def test_malformed_token_raises() -> None:
    """A token without a '/' separator raises ClickException."""
    import click

    check, _, _ = _make_check()
    handlers = [_handler(required_services=("postgres",))]
    with pytest.raises(click.ClickException) as exc_info:
        check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert "malformed" in exc_info.value.format_message().lower()


# ---------------------------------------------------------------------------
# Tests: dispatch failure → raises ClickException
# ---------------------------------------------------------------------------


def test_dispatch_up_failure_raises_click_exception() -> None:
    """Non-zero exit from dispatch(up) raises a ClickException."""
    import click

    doc = _make_doc("workspace", [_make_service_status("postgres", state="stopped")])
    check, _, _dispatch_svc = _make_check(doc=doc, dispatch_exit=1)
    handlers = [_handler(required_services=("workspace/postgres",))]
    with pytest.raises(click.ClickException) as exc_info:
        check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert "exit code" in exc_info.value.format_message()


# ---------------------------------------------------------------------------
# Tests: scope deduplication — same scope started only once even with multiple
# down services from that scope
# ---------------------------------------------------------------------------


def test_multiple_down_workspace_services_dispatch_workspace_only_once() -> None:
    """Two down workspace services → dispatch workspace up exactly once."""
    doc = _make_doc(
        "workspace",
        [
            _make_service_status("postgres", state="stopped"),
            _make_service_status("rabbitmq", state="stopped"),
        ],
    )
    check, _, dispatch_svc = _make_check(doc=doc)
    handlers = [_handler(required_services=("workspace/postgres", "workspace/rabbitmq"))]
    result = check.ensure(handlers, ENV_NAME, no_service_check=False)
    assert result is not None and "started:" in result
    # workspace dispatched exactly once
    up_calls = [c for c in dispatch_svc.dispatch_calls if c[0] == "up"]
    assert up_calls == [("up", ["workspace"])]
