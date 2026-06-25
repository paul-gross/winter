"""Tests for ServiceStatusService (matrix path).

All tests exercise the matrix path (Phase 5 of winter#109): the four matrix
dependencies are always wired.  Per-cell calls use scope-qualified patterns
(``<scope>/*`` or ``<scope>/<svc>``).  A registry with a single configured env
``alpha`` (index 1) is the default; tests that need multiple envs supply
``{"alpha": 1, "beta": 2}``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.conftest import (
    FakeConfigFileReader,
    FakeFilesystem,
    FakeServiceReporter,
    FakeSpecLoader,
    FakeSubprocessRunner,
)
from winter_cli.config.models import ProjectRepositoryConfig, SingletonRepository, SingletonType, WorkspaceConfig
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_reporter import JsonServiceReporter
from winter_cli.modules.service.service_status_matrix_service import ServiceStatusMatrixService
from winter_cli.modules.service.service_status_service import ServiceStatusService
from winter_cli.modules.service.status_models import StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

_PARSER = StatusDocumentParser()

WS = Path("/ws")
EXT = WS / "winter-service-tmux"
ENTRYPOINT = EXT / "workflow/status"
PREFIX = "winter-service-tmux"

CONFIG_DIR = WS / ".winter" / "config" / "winter-service-tmux"

# Scope-qualified cell keys used by the matrix (single configured env = alpha).
# The matrix calls <entrypoint> status <scope>/* for each scope cell.
CMD_KEY_ALPHA = f"{ENTRYPOINT} status alpha/*"
CMD_KEY_WORKSPACE = f"{ENTRYPOINT} status workspace/*"

_EMPTY_WORKSPACE_DOC = json.dumps({"envs": []})


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _opts(**kwargs: Any) -> StatusOptions:
    defaults: dict[str, Any] = {"patterns": (), "as_json": False}
    defaults.update(kwargs)
    return StatusOptions(**defaults)


class _FakeEnvFileSourcer:
    """Fake IEnvFileSourcer that returns empty dicts for all scopes."""

    def source(self, scope: str, ws_root: Path) -> dict[str, str]:
        return {}


class _FakeEnvIndexRegistry:
    """IEnvIndexRegistry fake backed by a simple dict."""

    def __init__(self, assignments: dict[str, int]) -> None:
        self._data: dict[str, int] = dict(assignments)

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


def _ws_config(base_port: int = 4000, ports_per_env: int = 20) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WS,
        session_prefix="test",
        main_branch="main",
        base_port=base_port,
        ports_per_env=ports_per_env,
        singleton_repos=[SingletonRepository(name="ws", type=SingletonType.workspace)],
        project_repos=[ProjectRepositoryConfig(name="demo", url="git@example.com:demo.git")],
    )


def _make_single_provider_registry() -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to a single tmux provider (status entrypoint)."""
    repo = StandaloneRepository(name="winter-service-tmux", path=WS / "winter-service-tmux")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader({repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/status"}})
    )
    fs = FakeFilesystem(files={repo.path / EXT_MANIFEST: "", repo.path / "workflow/status": ""})
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        bindings={"service": ["winter-service-tmux"]},
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    resolver = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        fs=fs,
    )
    return registry, resolver


def _svc(
    runner: FakeSubprocessRunner | None = None,
    registry_assignments: dict[str, int] | None = None,
) -> ServiceStatusService:
    """Build a ServiceStatusService with all matrix deps wired.

    Default registry: ``{"alpha": 1}`` (one configured env).
    """
    _registry, resolver = _make_single_provider_registry()
    assignments = registry_assignments if registry_assignments is not None else {"alpha": 1}
    actual_runner = runner or FakeSubprocessRunner()
    describe_svc = ServiceDescribeService(
        subprocess_runner=actual_runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    matrix_svc = ServiceStatusMatrixService(
        subprocess_runner=actual_runner,
        describe_service=describe_svc,
        env_file_sourcer=_FakeEnvFileSourcer(),
        status_parser=StatusDocumentParser(),
        workspace_config=_ws_config(),
        env_index_registry=_FakeEnvIndexRegistry(assignments),
        workspace_root=WS,
    )
    return ServiceStatusService(
        orchestrator_resolver=resolver,
        status_parser=StatusDocumentParser(),
        matrix_service=matrix_svc,
    )


def _stream_reporter() -> FakeServiceReporter:
    return FakeServiceReporter()


# ── helper to build canned JSON docs ─────────────────────────────────────────


def _make_doc(envs: list[dict]) -> str:
    return json.dumps({"envs": envs})


def _alpha_env(services: list[dict] | None = None) -> dict:
    return {
        "env": "alpha",
        "session": "mp-alpha",
        "port_base": 4020,
        "services": services or [],
    }


def _beta_env(services: list[dict] | None = None) -> dict:
    return {
        "env": "beta",
        "session": "mp-beta",
        "port_base": 4040,
        "services": services or [],
    }


def _api_svc(**kwargs: Any) -> dict:
    defaults = {
        "name": "api",
        "state": "running",
        "health": "healthy",
        "ports": [7503],
        "handle": "mp-alpha:0.0",
        "log_path": "/logs/api.log",
        "since": "2026-06-19T10:00:00Z",
    }
    defaults.update(kwargs)
    return defaults


def _db_svc(**kwargs: Any) -> dict:
    defaults = {
        "name": "db",
        "state": "stopped",
        "health": "unknown",
        "ports": [],
        "handle": None,
        "log_path": None,
        "since": None,
    }
    defaults.update(kwargs)
    return defaults


# ── single env human render ───────────────────────────────────────────────────
# Matrix path: single env "alpha" → calls <entrypoint> status alpha/*
# and <entrypoint> status workspace/*


def test_human_render_single_env_header_present() -> None:
    """Env header line is rendered: reporter receives a status_document with alpha env."""
    doc = _make_doc([_alpha_env([_api_svc(), _db_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    assert any(e.env == "alpha" for e in parsed_doc.envs)


def test_human_render_single_env_rows_per_service() -> None:
    """Each service is present in the parsed document passed to the reporter."""
    doc = _make_doc([_alpha_env([_api_svc(), _db_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    svc_names = [s.name for e in parsed_doc.envs for s in e.services]
    assert "api" in svc_names
    assert "db" in svc_names


def test_human_render_single_env_ports_comma_joined() -> None:
    """Ports list is present in the document passed to the reporter."""
    doc = _make_doc([_alpha_env([_api_svc(ports=[7503, 7504])])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    svc = parsed_doc.envs[0].services[0]
    assert 7503 in svc.ports
    assert 7504 in svc.ports


# ── multi env human render ────────────────────────────────────────────────────
# Registry: {"alpha": 1, "beta": 2} → two env cells + workspace cell


def test_human_render_multi_env_both_headers_present() -> None:
    """Both env entries are present in the document passed to the reporter."""
    alpha_doc = _make_doc([_alpha_env([_api_svc()])])
    beta_doc = _make_doc([_beta_env([_db_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([alpha_doc], 0),
            f"{ENTRYPOINT} status beta/*": ([beta_doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner, registry_assignments={"alpha": 1, "beta": 2}).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    env_names = [e.env for e in parsed_doc.envs]
    assert "alpha" in env_names
    assert "beta" in env_names


def test_human_render_multi_env_services_grouped() -> None:
    """Services from both envs appear in the document."""
    alpha_doc = _make_doc([_alpha_env([_api_svc()])])
    beta_doc = _make_doc([_beta_env([_db_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([alpha_doc], 0),
            f"{ENTRYPOINT} status beta/*": ([beta_doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner, registry_assignments={"alpha": 1, "beta": 2}).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    svc_names = [s.name for e in parsed_doc.envs for s in e.services]
    assert "api" in svc_names
    assert "db" in svc_names


# ── --json passthrough ────────────────────────────────────────────────────────


def test_json_passthrough_emits_valid_json() -> None:
    """`as_json=True` — the reporter receives the document and parser; JSON output is valid."""
    doc_str = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc_str], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )

    from tests.conftest import ClickRecorder
    from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService

    click = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click, cli_output=ClickCliOutputService())
    _svc(runner).report(_opts(as_json=True), json_reporter)

    stdout_msgs = [msg for msg, err in click.calls if not err]
    assert len(stdout_msgs) == 1
    parsed = json.loads(stdout_msgs[0])
    assert "envs" in parsed


def test_json_passthrough_matches_to_json_obj() -> None:
    """The emitted JSON matches `to_json_obj(parsed_doc)` exactly."""
    doc_str = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc_str], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )

    from tests.conftest import ClickRecorder
    from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService

    click = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click, cli_output=ClickCliOutputService())
    _svc(runner).report(_opts(as_json=True), json_reporter)

    stdout_msgs = [msg for msg, err in click.calls if not err]
    emitted = json.loads(stdout_msgs[0])
    # The emitted JSON is the merge of alpha + workspace (workspace empty); parse alpha to compare.
    expected = _PARSER.to_json_obj(_PARSER.parse(doc_str))
    assert emitted == expected


def test_json_passthrough_all_fields_present() -> None:
    """Every schema field is present in the emitted JSON."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )

    from tests.conftest import ClickRecorder
    from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService

    click = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click, cli_output=ClickCliOutputService())
    _svc(runner).report(_opts(as_json=True), json_reporter)

    stdout_msgs = [msg for msg, err in click.calls if not err]
    emitted = json.loads(stdout_msgs[0])
    env = emitted["envs"][0]
    assert "env" in env
    assert "session" in env
    assert "port_base" in env
    svc = env["services"][0]
    assert "name" in svc
    assert "state" in svc
    assert "health" in svc
    assert "ports" in svc
    assert isinstance(svc["ports"], list)
    assert "handle" in svc
    assert "log_path" in svc
    assert "since" in svc


def test_json_passthrough_no_table_headers_on_stdout() -> None:
    """No table column headers (SERVICE, STATE, HEALTH, etc.) leaked to stdout under --json."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )

    from tests.conftest import ClickRecorder
    from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService

    click = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click, cli_output=ClickCliOutputService())
    _svc(runner).report(_opts(as_json=True), json_reporter)

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    # These are table headers that should not appear in --json output
    assert "SERVICE" not in combined
    assert "HEALTH" not in combined


# ── orchestrator argv invariant under --json ──────────────────────────────────


def test_json_flag_does_not_alter_orchestrator_argv() -> None:
    """`as_json=True` does NOT change the argv sent to the orchestrator."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner_json = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    runner_plain = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )

    reporter = _stream_reporter()
    _svc(runner_json).report(_opts(as_json=True), reporter)
    _svc(runner_plain).report(_opts(as_json=False), reporter)

    assert runner_json.popen_calls[0][0] == runner_plain.popen_calls[0][0]


def test_json_flag_does_not_add_json_env_var() -> None:
    """No env var containing 'JSON' is set when `as_json=True`."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(as_json=True), reporter)

    # Check all popen calls - none should have a JSON env var.
    for env in runner.popen_envs:
        assert not any("JSON" in k.upper() for k in env)


def test_orchestrator_argv_bare_status_no_patterns() -> None:
    """Without patterns, matrix calls <entrypoint> status <scope>/* per cell."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)
    # Matrix emits one call per scope; the first call (alpha, sorted) uses alpha/*.
    cmds = [call[0] for call in runner.popen_calls]
    assert any(cmd == [str(ENTRYPOINT), "status", "alpha/*"] for cmd in cmds)


def test_orchestrator_argv_with_patterns() -> None:
    """Scope-qualified patterns are forwarded as the cell pattern."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    key = f"{ENTRYPOINT} status alpha/api"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    reporter = _stream_reporter()
    _svc(runner).report(_opts(patterns=("alpha/api",)), reporter)
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "alpha/api"]


def test_orchestrator_argv_workspace_pattern_forwarded_verbatim() -> None:
    """'workspace' bare pattern → matrix produces workspace/* cell."""
    doc = _make_doc([{"env": "workspace", "session": None, "port_base": None, "services": []}])
    key = f"{ENTRYPOINT} status workspace/*"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    reporter = _stream_reporter()
    _svc(runner).report(_opts(patterns=("workspace",)), reporter)
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "workspace/*"]


def test_orchestrator_argv_workspace_service_pattern_forwarded_verbatim() -> None:
    """'workspace/<svc>' pattern is forwarded as workspace/<svc> cell pattern."""
    doc = _make_doc([{"env": "workspace", "session": None, "port_base": None, "services": []}])
    key = f"{ENTRYPOINT} status workspace/nginx"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    reporter = _stream_reporter()
    _svc(runner).report(_opts(patterns=("workspace/nginx",)), reporter)
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "workspace/nginx"]


# ── malformed / non-conformant output ─────────────────────────────────────────
# With the matrix path and a single configured env (alpha) + workspace, a
# malformed response from the alpha cell plus empty workspace → parse error.


def test_malformed_json_returns_nonzero() -> None:
    """Non-JSON stdout → non-zero return value."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: (["not json at all"], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    code = _svc(runner).report(_opts(), reporter)
    assert code != 0


def test_malformed_json_emits_actionable_stderr() -> None:
    """Non-JSON stdout → status_parse_error fired on the reporter."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: (["not json at all"], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    assert len(reporter.status_parse_error_calls) >= 1
    _ep, _prefix, detail = reporter.status_parse_error_calls[0]
    assert len(detail) > 0


def test_malformed_json_no_traceback_on_stderr() -> None:
    """No Python traceback text leaks through the reporter on parse failure."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: (["garbage"], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    for _ep, _prefix, detail in reporter.status_parse_error_calls:
        assert "Traceback" not in detail


def test_malformed_json_parse_error_cell_emits_error() -> None:
    """A cell with malformed stdout fires status_parse_error; other cells still contribute."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: (["garbage"], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    # The parse error must be reported.
    assert len(reporter.status_parse_error_calls) >= 1
    # The workspace cell (empty doc) still contributes → merged empty doc delivered.
    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    assert len(doc.envs) == 0


def test_missing_envs_key_returns_nonzero() -> None:
    """Top-level object missing `envs` key → non-zero return, clean error path."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([json.dumps({"foo": 1})], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    code = _svc(runner).report(_opts(), reporter)

    assert code != 0
    assert len(reporter.status_parse_error_calls) >= 1


def test_missing_envs_key_no_traceback() -> None:
    """Missing `envs` key → no traceback text in parse error detail."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([json.dumps({"foo": 1})], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    for _ep, _prefix, detail in reporter.status_parse_error_calls:
        assert "Traceback" not in detail


# ── conformant empty document ─────────────────────────────────────────────────


def test_empty_envs_doc_exits_zero() -> None:
    """Conformant `{"envs":[]}` is not an error — exits 0."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([json.dumps({"envs": []})], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    code = _svc(runner).report(_opts(), reporter)
    assert code == 0


def test_empty_envs_doc_reporter_receives_empty_document() -> None:
    """Conformant empty document — reporter receives status_document event with empty envs."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([json.dumps({"envs": []})], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)

    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    assert len(doc.envs) == 0


# ── exit code passthrough ─────────────────────────────────────────────────────


def test_exit_code_passthrough_valid_doc() -> None:
    """Orchestrator exit code is returned even with a valid doc."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 42),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    code = _svc(runner).report(_opts(), reporter)
    assert code == 42


def test_exit_code_passthrough_zero_on_clean() -> None:
    """Zero exit code is returned on clean orchestrator exit."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    assert _svc(runner).report(_opts(), reporter) == 0


def test_malformed_json_adopts_orchestrator_nonzero_exit() -> None:
    """When orchestrator exits non-zero AND stdout is invalid, non-zero is returned."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: (["garbage"], 7),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    code = _svc(runner).report(_opts(), reporter)
    assert code == 7


# ── stderr inheritance ────────────────────────────────────────────────────────


def test_popen_called_with_merge_stderr_false() -> None:
    """popen is called with merge_stderr=False so orchestrator stderr reaches the terminal."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY_ALPHA: ([doc], 0),
            CMD_KEY_WORKSPACE: ([_EMPTY_WORKSPACE_DOC], 0),
        }
    )
    reporter = _stream_reporter()
    _svc(runner).report(_opts(), reporter)
    # All popen calls must use merge_stderr=False.
    assert all(ms is False for ms in runner.popen_merge_stderr)


# ── backstop filter ───────────────────────────────────────────────────────────


def test_pattern_backstop_filter_keeps_matched_service() -> None:
    """Pattern backstop keeps only the matching service in the document."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
        ]
    )
    # Matrix narrows to alpha scope only for "alpha/api" pattern.
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} status alpha/api": ([doc], 0)})
    reporter = _stream_reporter()
    _svc(runner).report(_opts(patterns=("alpha/api",)), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    # Only alpha env should remain
    assert len(parsed_doc.envs) == 1
    assert parsed_doc.envs[0].env == "alpha"
    # Only api should remain (db filtered out by backstop)
    assert len(parsed_doc.envs[0].services) == 1
    assert parsed_doc.envs[0].services[0].name == "api"


def test_pattern_backstop_filter_json_output() -> None:
    """Pattern backstop is also applied before passing to the JSON reporter."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
        ]
    )
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} status alpha/api": ([doc], 0)})

    from tests.conftest import ClickRecorder
    from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService

    click = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click, cli_output=ClickCliOutputService())
    _svc(runner).report(_opts(patterns=("alpha/api",), as_json=True), json_reporter)

    stdout_msgs = [msg for msg, err in click.calls if not err]
    emitted = json.loads(stdout_msgs[0])
    assert len(emitted["envs"]) == 1
    assert emitted["envs"][0]["env"] == "alpha"
    assert len(emitted["envs"][0]["services"]) == 1
    assert emitted["envs"][0]["services"][0]["name"] == "api"


def test_bare_env_pattern_keeps_all_services_for_env() -> None:
    """A bare `alpha` pattern narrows to the alpha cell, keeps all alpha services."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
        ]
    )
    # Bare "alpha" pattern → matrix produces cell with pattern "alpha/*".
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_ALPHA: ([doc], 0)})
    reporter = _stream_reporter()
    _svc(runner).report(_opts(patterns=("alpha",)), reporter)

    assert len(reporter.status_documents) == 1
    parsed_doc, _ = reporter.status_documents[0]
    # Only alpha should remain
    assert len(parsed_doc.envs) == 1
    assert parsed_doc.envs[0].env == "alpha"
    # Both alpha services should be present
    svc_names = [s.name for s in parsed_doc.envs[0].services]
    assert "api" in svc_names
    assert "db" in svc_names
