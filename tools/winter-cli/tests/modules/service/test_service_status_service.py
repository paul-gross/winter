from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.conftest import ClickRecorder, FakeConfigFileReader, FakeFilesystem, FakeSpecLoader, FakeSubprocessRunner
from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.orchestrator_resolver import ResolvedOrchestrator, ServiceOrchestratorResolver
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

CMD_KEY_BARE = f"{ENTRYPOINT} status"


def _resolved() -> ResolvedOrchestrator:
    return ResolvedOrchestrator(entrypoint=ENTRYPOINT, ext_dir=EXT, prefix=PREFIX)


def _resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve.return_value = _resolved()
    return resolver


def _opts(**kwargs: Any) -> StatusOptions:
    defaults: dict[str, Any] = {"patterns": (), "as_json": False}
    defaults.update(kwargs)
    return StatusOptions(**defaults)


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


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
    click: ClickRecorder | None = None,
) -> ServiceStatusService:
    _registry, resolver = _make_single_provider_registry()
    return ServiceStatusService(
        subprocess_runner=runner or FakeSubprocessRunner(),
        orchestrator_resolver=resolver,
        status_parser=StatusDocumentParser(),
        cli_output=ClickCliOutputService(),
        click=click or ClickRecorder(),
        workspace_root=WS,
    )


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


def test_human_render_single_env_header_present() -> None:
    """Env header line is echoed before the service table."""
    doc = _make_doc([_alpha_env([_api_svc(), _db_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    assert any("alpha" in m and "session=mp-alpha" in m and "port_base=4020" in m for m in stdout_msgs)


def test_human_render_single_env_rows_per_service() -> None:
    """Each service produces a rendered row in the table output."""
    doc = _make_doc([_alpha_env([_api_svc(), _db_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    assert "api" in combined
    assert "db" in combined
    # State and health values are rendered
    assert "running" in combined
    assert "healthy" in combined
    assert "stopped" in combined


def test_human_render_single_env_ports_comma_joined() -> None:
    """Ports list is rendered as comma-separated string."""
    doc = _make_doc([_alpha_env([_api_svc(ports=[7503, 7504])])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    assert "7503" in combined
    assert "7504" in combined


# ── multi env human render ────────────────────────────────────────────────────


def test_human_render_multi_env_both_headers_present() -> None:
    """Both env headers are echoed when the document has two envs."""
    doc = _make_doc([_alpha_env([_api_svc()]), _beta_env([_db_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    assert any("alpha" in m and "session=mp-alpha" in m for m in stdout_msgs)
    assert any("beta" in m and "session=mp-beta" in m for m in stdout_msgs)


def test_human_render_multi_env_services_grouped() -> None:
    """Services appear under their respective env headers."""
    doc = _make_doc([_alpha_env([_api_svc()]), _beta_env([_db_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    assert "api" in combined
    assert "db" in combined


# ── --json passthrough ────────────────────────────────────────────────────────


def test_json_passthrough_emits_valid_json() -> None:
    """`as_json=True` emits exactly one stdout echo that is valid JSON."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(as_json=True))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    assert len(stdout_msgs) == 1
    parsed = json.loads(stdout_msgs[0])
    assert "envs" in parsed


def test_json_passthrough_matches_to_json_obj() -> None:
    """The emitted JSON matches `to_json_obj(parsed_doc)` exactly."""
    doc_str = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc_str], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(as_json=True))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    emitted = json.loads(stdout_msgs[0])
    expected = _PARSER.to_json_obj(_PARSER.parse(doc_str))
    assert emitted == expected


def test_json_passthrough_all_fields_present() -> None:
    """Every schema field is present in the emitted JSON."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(as_json=True))

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
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(as_json=True))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    # These are table headers that should not appear in --json output
    assert "SERVICE" not in combined
    assert "HEALTH" not in combined


# ── orchestrator argv invariant under --json ──────────────────────────────────


def test_json_flag_does_not_alter_orchestrator_argv() -> None:
    """`as_json=True` does NOT change the argv sent to the orchestrator."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner_json = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    runner_plain = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})

    _svc(runner_json).report(_opts(as_json=True))
    _svc(runner_plain).report(_opts(as_json=False))

    assert runner_json.popen_calls[0][0] == runner_plain.popen_calls[0][0]


def test_json_flag_does_not_add_json_env_var() -> None:
    """No env var containing 'JSON' is set when `as_json=True`."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    _svc(runner).report(_opts(as_json=True))

    env = runner.popen_envs[0]
    assert not any("JSON" in k.upper() for k in env)


def test_orchestrator_argv_bare_status_no_patterns() -> None:
    """Without patterns, argv is exactly `[entrypoint, 'status']`."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    _svc(runner).report(_opts())
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status"]


def test_orchestrator_argv_with_patterns() -> None:
    """Patterns are forwarded verbatim as positional argv tokens."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    key = f"{ENTRYPOINT} status alpha/api"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    _svc(runner).report(_opts(patterns=("alpha/api",)))
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "alpha/api"]


def test_orchestrator_argv_workspace_pattern_forwarded_verbatim() -> None:
    """'workspace' pattern is forwarded verbatim as a positional argv token."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    key = f"{ENTRYPOINT} status workspace"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    _svc(runner).report(_opts(patterns=("workspace",)))
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "workspace"]


def test_orchestrator_argv_workspace_service_pattern_forwarded_verbatim() -> None:
    """'workspace/<svc>' pattern is forwarded verbatim as a positional argv token."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    key = f"{ENTRYPOINT} status workspace/nginx"
    runner = FakeSubprocessRunner(popen_responses={key: ([doc], 0)})
    _svc(runner).report(_opts(patterns=("workspace/nginx",)))
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "status", "workspace/nginx"]


# ── malformed / non-conformant output ─────────────────────────────────────────


def test_malformed_json_returns_nonzero() -> None:
    """Non-JSON stdout → non-zero return value."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: (["not json at all"], 0)})
    code = _svc(runner).report(_opts())
    assert code != 0


def test_malformed_json_emits_actionable_stderr() -> None:
    """Non-JSON stdout → stderr echo with an actionable error message."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: (["not json at all"], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stderr_msgs = [msg for msg, err in click.calls if err]
    assert any("does not emit the structured status document" in m for m in stderr_msgs)


def test_malformed_json_no_traceback_on_stderr() -> None:
    """No Python traceback text leaks to stderr on parse failure."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: (["garbage"], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stderr_msgs = [msg for msg, err in click.calls if err]
    assert not any("Traceback" in m for m in stderr_msgs)


def test_malformed_json_no_schema_on_stdout() -> None:
    """Nothing schema-shaped is emitted to stdout on parse failure."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: (["garbage"], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    # No JSON object/array should appear on stdout
    for msg in stdout_msgs:
        try:
            json.loads(msg)
            pytest.fail(f"schema-shaped JSON found on stdout: {msg!r}")
        except json.JSONDecodeError:
            pass


def test_missing_envs_key_returns_nonzero() -> None:
    """Top-level object missing `envs` key → non-zero return, clean error path."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([json.dumps({"foo": 1})], 0)})
    click = ClickRecorder()
    code = _svc(runner, click).report(_opts())

    assert code != 0
    stderr_msgs = [msg for msg, err in click.calls if err]
    assert any("does not emit the structured status document" in m for m in stderr_msgs)


def test_missing_envs_key_no_traceback() -> None:
    """Missing `envs` key → no traceback on stderr."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([json.dumps({"foo": 1})], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stderr_msgs = [msg for msg, err in click.calls if err]
    assert not any("Traceback" in m for m in stderr_msgs)


# ── conformant empty document ─────────────────────────────────────────────────


def test_empty_envs_doc_exits_zero() -> None:
    """Conformant `{"envs":[]}` is not an error — exits 0."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([json.dumps({"envs": []})], 0)})
    code = _svc(runner).report(_opts())
    assert code == 0


def test_empty_envs_doc_renders_no_services() -> None:
    """Conformant empty document renders 'no services', not a table."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([json.dumps({"envs": []})], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts())

    stdout_msgs = [msg for msg, err in click.calls if not err]
    assert any("no services" in m for m in stdout_msgs)


# ── exit code passthrough ─────────────────────────────────────────────────────


def test_exit_code_passthrough_valid_doc() -> None:
    """Orchestrator exit code is returned even with a valid doc."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 42)})
    code = _svc(runner).report(_opts())
    assert code == 42


def test_exit_code_passthrough_zero_on_clean() -> None:
    """Zero exit code is returned on clean orchestrator exit."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    assert _svc(runner).report(_opts()) == 0


def test_malformed_json_adopts_orchestrator_nonzero_exit() -> None:
    """When orchestrator exits non-zero AND stdout is invalid, non-zero is returned."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: (["garbage"], 7)})
    code = _svc(runner).report(_opts())
    assert code == 7


# ── stderr inheritance ────────────────────────────────────────────────────────


def test_popen_called_with_merge_stderr_false() -> None:
    """popen is called with merge_stderr=False so orchestrator stderr reaches the terminal."""
    doc = _make_doc([_alpha_env([_api_svc()])])
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY_BARE: ([doc], 0)})
    _svc(runner).report(_opts())
    assert runner.popen_merge_stderr == [False]


# ── backstop filter ───────────────────────────────────────────────────────────


def test_pattern_backstop_filter_keeps_matched_service() -> None:
    """Pattern backstop keeps only the matching service in human output."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
            _beta_env([_api_svc(name="worker")]),
        ]
    )
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} status alpha/api": ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(patterns=("alpha/api",)))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    assert "api" in combined
    # db should be filtered out
    assert "db" not in combined
    # beta/worker should be filtered out
    assert "beta" not in combined


def test_pattern_backstop_filter_json_output() -> None:
    """Pattern backstop is also applied before --json emit."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
            _beta_env([_api_svc(name="worker")]),
        ]
    )
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} status alpha/api": ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(patterns=("alpha/api",), as_json=True))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    emitted = json.loads(stdout_msgs[0])
    assert len(emitted["envs"]) == 1
    assert emitted["envs"][0]["env"] == "alpha"
    assert len(emitted["envs"][0]["services"]) == 1
    assert emitted["envs"][0]["services"][0]["name"] == "api"


def test_bare_env_pattern_keeps_all_services_for_env() -> None:
    """A bare `alpha` pattern keeps all of alpha's services, drops beta."""
    doc = _make_doc(
        [
            _alpha_env([_api_svc(), _db_svc()]),
            _beta_env([_api_svc(name="worker")]),
        ]
    )
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} status alpha": ([doc], 0)})
    click = ClickRecorder()
    _svc(runner, click).report(_opts(patterns=("alpha",)))

    stdout_msgs = [msg for msg, err in click.calls if not err]
    combined = "\n".join(stdout_msgs)
    # Both alpha services should be present
    assert "api" in combined
    assert "db" in combined
    # beta should be gone
    assert "beta" not in combined
