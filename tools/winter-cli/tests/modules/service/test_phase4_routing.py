"""Phase 4 tests: multi-provider routing for restart, logs, and status.

Covers:
- restart routing: owning provider receives matched services; non-owning provider not called
- logs routing + merge: non-follow fans out to both owning providers; single-owner routes correctly
- logs -f single vs multi (D2): follow on single owner streams; follow on multiple owners errors
- merged status (AC5): two providers same env → merged; different envs → concatenated;
  non-conformant provider → error names that provider
- single-provider back-compat (D1): restart/logs/status all behave as before with one provider
- describe resilience: broken provider describe does not abort logs for a good provider
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
from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.models import LogOptions
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService
from winter_cli.modules.service.service_logs_service import ServiceLogsService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_reporter import JsonServiceReporter
from winter_cli.modules.service.service_status_service import ServiceStatusService
from winter_cli.modules.service.status_models import StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
EXT_A = WS / "provider-a"
EXT_B = WS / "provider-b"
ENTRYPOINT_A = EXT_A / "workflow/service"
ENTRYPOINT_B = EXT_B / "workflow/service"


# ── helpers ───────────────────────────────────────────────────────────────────


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _make_two_provider_registry(
    provider_a_name: str = "provider-a",
    provider_b_name: str = "provider-b",
) -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to two providers (A then B, in that order)."""
    repo_a = StandaloneRepository(name=provider_a_name, path=WS / provider_a_name)
    repo_b = StandaloneRepository(name=provider_b_name, path=WS / provider_b_name)
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {
                repo_a.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
                repo_b.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
            }
        )
    )
    fs = FakeFilesystem(
        files={
            repo_a.path / EXT_MANIFEST: "",
            repo_a.path / "workflow/service": "",
            repo_b.path / EXT_MANIFEST: "",
            repo_b.path / "workflow/service": "",
        }
    )
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo_a, repo_b]),
        manifest_loader=loader,
        bindings={"service": [provider_a_name, provider_b_name]},
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    resolver = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([repo_a, repo_b]),
        manifest_loader=loader,
        fs=fs,
    )
    return registry, resolver


def _make_single_provider_registry() -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to a single provider."""
    repo = StandaloneRepository(name="provider-a", path=WS / "provider-a")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}}
        )
    )
    fs = FakeFilesystem(files={repo.path / EXT_MANIFEST: "", repo.path / "workflow/service": ""})
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        bindings={"service": ["provider-a"]},
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


def _describe_json(*services: str) -> str:
    return json.dumps({"services": list(services)})


def _describe_result(json_str: str) -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout=json_str, stderr="")


def _status_doc(env: str, services: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "envs": [
                {
                    "env": env,
                    "session": f"mp-{env}",
                    "port_base": 4020,
                    "services": services or [],
                }
            ]
        }
    )


def _svc_entry(name: str, state: str = "running", health: str = "healthy") -> dict:
    return {
        "name": name,
        "state": state,
        "health": health,
        "ports": [],
        "handle": None,
        "log_path": None,
        "since": None,
    }


def _make_dispatch(
    runner: FakeSubprocessRunner,
    registry: CapabilityRegistryService,
    resolver: ServiceOrchestratorResolver,
    reporter: FakeServiceReporter | None = None,
) -> ServiceDispatchService:
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    fan_out = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
    )
    return ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        fan_out_service=fan_out,
        describe_service=describe_svc,
        workspace_root=WS,
        reporter=reporter,
    )


def _make_logs(
    runner: FakeSubprocessRunner,
    registry: CapabilityRegistryService,
    resolver: ServiceOrchestratorResolver,
) -> tuple[ServiceLogsService, FakeServiceReporter]:
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    svc = ServiceLogsService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        describe_service=describe_svc,
        workspace_root=WS,
    )
    reporter = FakeServiceReporter()
    return svc, reporter


def _make_status(
    runner: FakeSubprocessRunner,
    registry: CapabilityRegistryService,
    resolver: ServiceOrchestratorResolver,
    as_json: bool = False,
    with_describe: bool = False,
) -> tuple[ServiceStatusService, FakeServiceReporter]:
    describe_svc: ServiceDescribeService | None = None
    if with_describe:
        describe_svc = ServiceDescribeService(
            subprocess_runner=runner,
            describe_parser=DescribeResultParser(),
            workspace_root=WS,
        )
    svc = ServiceStatusService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        status_parser=StatusDocumentParser(),
        workspace_root=WS,
        describe_service=describe_svc,
    )
    reporter = FakeServiceReporter()
    return svc, reporter


def _log_opts(**kwargs: Any) -> LogOptions:
    defaults: dict[str, Any] = {
        "patterns": (),
        "follow": False,
        "tail": 200,
        "since_rfc3339": "",
        "until_rfc3339": "",
        "timestamps": False,
    }
    defaults.update(kwargs)
    return LogOptions(**defaults)


def _status_opts(**kwargs: Any) -> StatusOptions:
    defaults: dict[str, Any] = {"patterns": (), "as_json": False}
    defaults.update(kwargs)
    return StatusOptions(**defaults)


# ── restart routing ───────────────────────────────────────────────────────────


def test_restart_routes_to_owning_provider_only() -> None:
    """restart backend with backend owned by provider-b → only provider-b invoked."""
    registry, resolver = _make_two_provider_registry()

    describe_key_a = f"{ENTRYPOINT_A} describe"
    describe_key_b = f"{ENTRYPOINT_B} describe"

    runner = FakeSubprocessRunner(
        run_responses={
            describe_key_a: _describe_result(_describe_json("frontend")),
            describe_key_b: _describe_result(_describe_json("backend")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    code = dispatch.dispatch("restart", ["backend"])

    assert code == 0
    # Only provider-b's restart should be called.
    call_cmds = [cmd for cmd, _cwd in runner.call_calls]
    assert [str(ENTRYPOINT_B), "restart", "backend"] in call_cmds
    # Provider-a must NOT be invoked for restart.
    assert not any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in call_cmds)


def test_restart_provider_a_not_called_when_b_owns_service() -> None:
    """Explicit assertion: provider-a's restart is not in the recorded calls."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend", "web")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend", "worker")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    dispatch.dispatch("restart", ["backend"])

    restart_calls = [cmd for cmd, _cwd in runner.call_calls if "restart" in cmd]
    assert all(str(ENTRYPOINT_A) not in cmd[0] for cmd in restart_calls)


def test_restart_each_provider_gets_its_own_services() -> None:
    """When patterns match services across two providers, each gets only its owned services."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend", "web")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend", "worker")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    # Pattern matches all services across both providers.
    dispatch.dispatch("restart", ["frontend", "backend"])

    call_cmds = [cmd for cmd, _cwd in runner.call_calls]
    # Provider-a should restart frontend
    assert any(str(ENTRYPOINT_A) in cmd[0] and "frontend" in cmd for cmd in call_cmds)
    # Provider-b should restart backend
    assert any(str(ENTRYPOINT_B) in cmd[0] and "backend" in cmd for cmd in call_cmds)


def test_restart_no_match_pattern_invokes_no_provider() -> None:
    """A pattern matching no owned service → no provider invoked, no error."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    code = dispatch.dispatch("restart", ["nonexistent-service"])

    assert code == 0
    restart_calls = [cmd for cmd, _ in runner.call_calls if "restart" in cmd]
    assert len(restart_calls) == 0


def test_restart_single_provider_no_describe_call() -> None:
    """D1: single provider restart → no describe call; patterns forwarded verbatim."""
    registry, resolver = _make_single_provider_registry()
    runner = FakeSubprocessRunner()  # no run_responses; any run() raises
    dispatch = _make_dispatch(runner, registry, resolver)

    dispatch.dispatch("restart", ["alpha/api", "*/backend"])

    # No describe call.
    assert runner.run_calls == []
    # The single provider's restart was called with the verbatim patterns.
    assert len(runner.call_calls) == 1
    cmd = runner.call_calls[0][0]
    assert cmd == [str(EXT_A / "workflow/service"), "restart", "alpha/api", "*/backend"]


# ── logs routing + merge ──────────────────────────────────────────────────────


def test_logs_non_follow_spans_two_providers_and_merges_output() -> None:
    """Non-follow logs spanning two providers fans out to both and merges output."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        popen_responses={
            # Provider-a receives the original env-scoped pattern "alpha/frontend" on argv.
            f"{ENTRYPOINT_A} logs alpha/frontend --tail 200": (
                ['{"env":"alpha","svc":"frontend","msg":"frontend-line"}'],
                0,
            ),
            # Provider-b receives the original env-scoped pattern "alpha/backend" on argv.
            f"{ENTRYPOINT_B} logs alpha/backend --tail 200": (
                ['{"env":"alpha","svc":"backend","msg":"backend-line"}'],
                0,
            ),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    # Use <env>/<svc> patterns — segment-aware matching.
    code = logs.stream(_log_opts(patterns=("alpha/frontend", "alpha/backend")), reporter)

    assert code == 0
    combined = "\n".join(reporter.log_lines)
    assert "frontend-line" in combined
    assert "backend-line" in combined


def test_logs_single_owner_routes_only_to_that_provider() -> None:
    """A pattern owned by provider-a routes only to provider-a."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        popen_responses={
            # Provider-a receives the original env-scoped pattern "alpha/frontend" on argv.
            f"{ENTRYPOINT_A} logs alpha/frontend --tail 200": (
                ['{"env":"alpha","svc":"frontend","msg":"a-only-line"}'],
                0,
            ),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/frontend",)), reporter)

    assert code == 0
    combined = "\n".join(reporter.log_lines)
    assert "a-only-line" in combined
    # Provider-b should NOT have been called.
    assert not any(str(ENTRYPOINT_B) in str(cmd) for cmd, _ in runner.popen_calls)


# ── logs -f (follow) D2 ───────────────────────────────────────────────────────


def test_logs_follow_single_owner_streams() -> None:
    """follow=True on a selection resolving to one owning provider streams normally."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        popen_responses={
            # Provider-a receives the original env-scoped pattern "alpha/frontend" on argv,
            # followed by the render flags (--tail always, --follow because follow=True).
            f"{ENTRYPOINT_A} logs alpha/frontend --tail 200 --follow": (
                ['{"env":"alpha","svc":"frontend","msg":"follow-line"}'],
                0,
            ),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/frontend",), follow=True), reporter)

    assert code == 0
    combined = "\n".join(reporter.log_lines)
    assert "follow-line" in combined


def test_logs_follow_multi_provider_errors_with_actionable_message() -> None:
    """follow=True spanning multiple owning providers → error with actionable message, no stream."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        # No popen_responses: opening any stream should raise AssertionError.
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/frontend", "alpha/backend"), follow=True), reporter)

    assert code == 1
    # Error message should be recorded on the reporter.
    assert len(reporter.follow_multi_provider_error_calls) == 1
    # The reporter receives the provider_names string; the full message is in the reporter impl.
    # No stream should have been opened.
    assert len(runner.popen_calls) == 0


def test_logs_follow_multi_provider_error_mentions_providers() -> None:
    """The D2 error message names the conflicting providers."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    logs.stream(_log_opts(patterns=("alpha/frontend", "alpha/backend"), follow=True), reporter)

    assert len(reporter.follow_multi_provider_error_calls) == 1
    provider_names = reporter.follow_multi_provider_error_calls[0]
    assert "provider-a" in provider_names
    assert "provider-b" in provider_names


def test_logs_single_provider_no_describe_call() -> None:
    """D1: single provider logs → no describe call; patterns forwarded verbatim."""
    registry, resolver = _make_single_provider_registry()
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{EXT_A / 'workflow/service'} logs alpha/api --tail 200": (
                ['{"env":"alpha","svc":"api","msg":"log-line"}'],
                0,
            ),
        }
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/api",)), reporter)

    assert code == 0
    assert runner.run_calls == []  # No describe call.


# ── merged status (AC5) ───────────────────────────────────────────────────────


def test_status_merge_same_env_from_two_providers() -> None:
    """Two providers each reporting the same env → merged into one env with both providers' services."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status": (
                [_status_doc("alpha", [_svc_entry("frontend")])],
                0,
            ),
            f"{ENTRYPOINT_B} status": (
                [_status_doc("alpha", [_svc_entry("backend")])],
                0,
            ),
        }
    )
    svc, reporter = _make_status(runner, registry, resolver)
    code = svc.report(_status_opts(), reporter)

    assert code == 0
    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    # Only one env (alpha) — merged.
    assert len(doc.envs) == 1
    svc_names = [s.name for s in doc.envs[0].services]
    assert "frontend" in svc_names
    assert "backend" in svc_names


def test_status_merge_different_envs_concatenated() -> None:
    """Different envs from different providers → concatenated in the merged document."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status": (
                [_status_doc("alpha", [_svc_entry("frontend")])],
                0,
            ),
            f"{ENTRYPOINT_B} status": (
                [_status_doc("beta", [_svc_entry("backend")])],
                0,
            ),
        }
    )
    svc, reporter = _make_status(runner, registry, resolver)
    code = svc.report(_status_opts(), reporter)

    assert code == 0
    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    env_names = [e.env for e in doc.envs]
    assert "alpha" in env_names
    assert "beta" in env_names


def test_status_merge_json_output_merged() -> None:
    """Merged status with --json emits a single merged JSON document."""
    from tests.conftest import ClickRecorder

    _registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status": (
                [_status_doc("alpha", [_svc_entry("frontend")])],
                0,
            ),
            f"{ENTRYPOINT_B} status": (
                [_status_doc("alpha", [_svc_entry("backend")])],
                0,
            ),
        }
    )
    svc = ServiceStatusService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        status_parser=StatusDocumentParser(),
        workspace_root=WS,
    )
    click_rec = ClickRecorder()
    json_reporter = JsonServiceReporter(click=click_rec, cli_output=ClickCliOutputService())
    code = svc.report(_status_opts(as_json=True), json_reporter)

    assert code == 0
    stdout_msgs = [msg for msg, err in click_rec.calls if not err]
    assert len(stdout_msgs) == 1
    parsed = json.loads(stdout_msgs[0])
    assert "envs" in parsed
    assert len(parsed["envs"]) == 1
    svc_names = [s["name"] for s in parsed["envs"][0]["services"]]
    assert "frontend" in svc_names
    assert "backend" in svc_names


def test_status_nonconformant_provider_names_that_provider() -> None:
    """A non-conformant provider doc → error names THAT provider specifically."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status": (["not valid json"], 0),
            f"{ENTRYPOINT_B} status": ([_status_doc("alpha", [_svc_entry("backend")])], 0),
        }
    )
    svc, reporter = _make_status(runner, registry, resolver)
    code = svc.report(_status_opts(), reporter)

    assert code != 0
    assert len(reporter.status_parse_error_calls) >= 1
    ep, _prefix, _detail = reporter.status_parse_error_calls[0]
    # Error must name the specific provider.
    assert str(ENTRYPOINT_A) in ep or "provider-a" in ep


def test_status_single_provider_no_merge() -> None:
    """D1: single provider status → existing single-provider behavior unchanged."""
    registry, resolver = _make_single_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            f"{EXT_A / 'workflow/service'} status": (
                [_status_doc("alpha", [_svc_entry("frontend")])],
                0,
            ),
        }
    )
    svc, reporter = _make_status(runner, registry, resolver)
    code = svc.report(_status_opts(), reporter)

    assert code == 0
    # Only one popen call — no fan-out.
    assert len(runner.popen_calls) == 1
    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    svc_names = [s.name for e in doc.envs for s in e.services]
    assert "frontend" in svc_names


# ── status merge model tests ──────────────────────────────────────────────────


def test_merge_status_documents_empty_list() -> None:
    """merge_status_documents([]) returns an empty document."""
    from winter_cli.modules.service.status_merge import merge_status_documents
    from winter_cli.modules.service.status_models import StatusDocument

    result = merge_status_documents([])
    assert result == StatusDocument(envs=())


def test_merge_status_documents_single_doc() -> None:
    """merge_status_documents with one document returns its content unchanged."""
    from winter_cli.modules.service.status_merge import merge_status_documents
    from winter_cli.modules.service.status_models import EnvStatus, ServiceStatus, StatusDocument

    svc = ServiceStatus(name="api", state="running", health="healthy", ports=(), handle=None, log_path=None, since=None)
    env = EnvStatus(env="alpha", session="mp-alpha", port_base=4020, services=(svc,))
    doc = StatusDocument(envs=(env,))

    result = merge_status_documents([doc])
    assert result.envs[0].env == "alpha"
    assert len(result.envs[0].services) == 1
    assert result.envs[0].services[0].name == "api"


def test_merge_status_documents_same_env_first_non_null_scalar() -> None:
    """Same env from two docs: first-non-null wins for session and port_base."""
    from winter_cli.modules.service.status_merge import merge_status_documents
    from winter_cli.modules.service.status_models import EnvStatus, ServiceStatus, StatusDocument

    svc_a = ServiceStatus(name="a", state="running", health="healthy", ports=(), handle=None, log_path=None, since=None)
    svc_b = ServiceStatus(name="b", state="stopped", health="unknown", ports=(), handle=None, log_path=None, since=None)

    doc_a = StatusDocument(envs=(EnvStatus(env="alpha", session="sess-a", port_base=4020, services=(svc_a,)),))
    doc_b = StatusDocument(envs=(EnvStatus(env="alpha", session=None, port_base=None, services=(svc_b,)),))

    result = merge_status_documents([doc_a, doc_b])
    assert len(result.envs) == 1
    env = result.envs[0]
    # First-non-null: session from doc_a wins.
    assert env.session == "sess-a"
    assert env.port_base == 4020
    # Both services concatenated.
    svc_names = {s.name for s in env.services}
    assert "a" in svc_names
    assert "b" in svc_names


def test_merge_status_documents_different_envs_concatenated() -> None:
    """Different env names → concatenated in order."""
    from winter_cli.modules.service.status_merge import merge_status_documents
    from winter_cli.modules.service.status_models import EnvStatus, StatusDocument

    doc_a = StatusDocument(envs=(EnvStatus(env="alpha", session=None, port_base=None, services=()),))
    doc_b = StatusDocument(envs=(EnvStatus(env="beta", session=None, port_base=None, services=()),))

    result = merge_status_documents([doc_a, doc_b])
    assert len(result.envs) == 2
    assert result.envs[0].env == "alpha"
    assert result.envs[1].env == "beta"


# ── override (D7): --service-orchestrator collapses fan-out to one provider ───


EXT_OVERRIDE = WS / "override-ext"
ENTRYPOINT_OVERRIDE = EXT_OVERRIDE / "workflow/service"


def _make_override_resolver(
    two_provider_registry: CapabilityRegistryService,
) -> ServiceOrchestratorResolver:
    """Build a resolver with an active path-mode override pointing at EXT_OVERRIDE.

    The registry has two providers (provider-a and provider-b) configured via
    capabilities.service = [...].  The override collapses fan-out to the single override
    extension (EXT_OVERRIDE), bypassing both configured providers.
    """
    # Seed the override extension in the filesystem so path-mode resolution passes.
    override_repo = StandaloneRepository(name="override-ext", path=EXT_OVERRIDE)
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {
                EXT_OVERRIDE / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
                # also provide the two-provider manifests so registry lookups work
                EXT_A / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
                EXT_B / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
            }
        )
    )
    fs = FakeFilesystem(
        files={
            EXT_OVERRIDE / EXT_MANIFEST: "",
            EXT_OVERRIDE / "workflow/service": "",
        }
    )
    return ServiceOrchestratorResolver(
        registry=two_provider_registry,
        repo_factory=_StubRepoFactory([override_repo]),
        manifest_loader=loader,
        fs=fs,
        override=str(EXT_OVERRIDE),  # path-mode override (contains /)
        workspace_root=WS,
    )


def _empty_status_doc() -> str:
    return json.dumps({"envs": []})


def test_override_wins_over_configured_list_for_status() -> None:
    """--service-orchestrator override routes status to the override provider only.

    With two providers in capabilities.service and an active override pointing at
    EXT_OVERRIDE, resolve_all() on the resolver returns only the override provider.
    ServiceStatusService must call only the override entrypoint — not provider-a or b.
    """
    registry, _resolver = _make_two_provider_registry()
    override_resolver = _make_override_resolver(registry)

    override_ep = str(ENTRYPOINT_OVERRIDE)

    runner2 = FakeSubprocessRunner(
        popen_responses={
            override_ep + " status": ([_empty_status_doc()], 0),
        }
    )
    status_svc2 = ServiceStatusService(
        subprocess_runner=runner2,
        orchestrator_resolver=override_resolver,
        status_parser=StatusDocumentParser(),
        workspace_root=WS,
    )
    reporter = FakeServiceReporter()
    from winter_cli.modules.service.status_models import StatusOptions

    code = status_svc2.report(StatusOptions(patterns=(), as_json=False), reporter)

    assert code == 0
    # Only the override entrypoint was called (checked via popen_calls).
    popen_cmds = [cmd for cmd, _cwd in runner2.popen_calls]
    assert any(override_ep in " ".join(cmd) for cmd in popen_cmds), (
        f"override entrypoint {override_ep!r} not called; popen_calls={runner2.popen_calls}"
    )
    # Neither provider-a nor provider-b was called.
    assert not any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in popen_cmds)
    assert not any(str(ENTRYPOINT_B) in " ".join(cmd) for cmd in popen_cmds)


def test_override_wins_over_configured_list_for_up() -> None:
    """--service-orchestrator override routes up to the override provider only.

    With two providers in capabilities.service and an active override, resolve_all()
    on the resolver returns only the override provider.  ServiceDispatchService's up
    fan-out must call only the override's up — not provider-a or b.
    """
    registry, _resolver = _make_two_provider_registry()
    override_resolver = _make_override_resolver(registry)

    override_ep = str(ENTRYPOINT_OVERRIDE)
    runner = FakeSubprocessRunner()
    fan_out = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
    )
    dispatch_svc = ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=override_resolver,
        fan_out_service=fan_out,
        describe_service=ServiceDescribeService(
            subprocess_runner=runner,
            describe_parser=DescribeResultParser(),
            workspace_root=WS,
        ),
        workspace_root=WS,
    )

    code = dispatch_svc.dispatch("up", ["alpha"])

    assert code == 0
    # Only the override's up was called (via call_calls).
    call_cmds = [cmd for cmd, _cwd in runner.call_calls]
    assert any(override_ep in " ".join(cmd) and "up" in cmd for cmd in call_cmds), (
        f"override entrypoint up not called; call_calls={runner.call_calls}"
    )
    # Neither provider-a nor provider-b was called.
    assert not any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in call_cmds)
    assert not any(str(ENTRYPOINT_B) in " ".join(cmd) for cmd in call_cmds)


# ── two-segment pattern routing (item 5) ─────────────────────────────────────


def test_restart_two_segment_pattern_forwards_env_scoped_token() -> None:
    """restart with a two-segment pattern (alpha/backend) forwards the original
    token to the owning provider — not just the bare service name 'backend'.
    """
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    dispatch.dispatch("restart", ["alpha/backend"])

    # Provider-b (owner of "backend") must receive the original token "alpha/backend".
    restart_calls = [cmd for cmd, _ in runner.call_calls if "restart" in cmd]
    assert len(restart_calls) == 1
    cmd = restart_calls[0]
    assert str(ENTRYPOINT_B) in cmd[0]
    # The owning provider receives the full env-scoped token, not the bare name.
    assert cmd == [str(ENTRYPOINT_B), "restart", "alpha/backend"]


def test_restart_cross_env_pattern_forwards_original_token() -> None:
    """restart with a cross-env pattern (*/backend) forwards the token verbatim."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        }
    )
    dispatch = _make_dispatch(runner, registry, resolver)
    dispatch.dispatch("restart", ["*/backend"])

    restart_calls = [cmd for cmd, _ in runner.call_calls if "restart" in cmd]
    assert len(restart_calls) == 1
    cmd = restart_calls[0]
    assert str(ENTRYPOINT_B) in cmd[0]
    # Provider-b receives the original cross-env pattern, not the bare name.
    assert "*/backend" in cmd


# ── no-match diagnostic (item 6) ─────────────────────────────────────────────


def test_restart_no_match_emits_stderr_diagnostic() -> None:
    """Multi-provider restart with no matching service → reporter gets no_match_diagnostic, exit 0."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        }
    )
    reporter = FakeServiceReporter()
    dispatch = _make_dispatch(runner, registry, resolver, reporter=reporter)
    code = dispatch.dispatch("restart", ["nonexistent-service"])

    assert code == 0
    assert len(reporter.no_match_diagnostic_calls) >= 1
    combined = " ".join(reporter.no_match_diagnostic_calls)
    assert "nonexistent-service" in combined


def test_logs_no_match_emits_stderr_diagnostic() -> None:
    """Multi-provider logs with no matching service → reporter gets no_service_matched."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        # No popen_responses: no provider should be invoked.
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    logs.stream(_log_opts(patterns=("alpha/nonexistent",)), reporter)

    assert len(reporter.no_service_matched_calls) >= 1
    combined = " ".join(reporter.no_service_matched_calls)
    assert "alpha/nonexistent" in combined  # token list contains the unmatched pattern


# ── describe resilience: broken provider does not abort good provider's logs ──


def test_logs_good_provider_streams_when_other_provider_broken_describe() -> None:
    """Scenario 1: service owned by good provider streams even if another provider's describe is broken."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            # provider-a emits valid describe: owns 'frontend'
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            # provider-b emits empty/invalid describe
            f"{ENTRYPOINT_B} describe": SubprocessResult(returncode=0, stdout="", stderr=""),
        },
        popen_responses={
            f"{ENTRYPOINT_A} logs alpha/frontend --tail 200": (
                ['{"env":"alpha","svc":"frontend","msg":"resilience-ok"}'],
                0,
            ),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/frontend",)), reporter)

    # Exit 0: the good provider served the request.
    assert code == 0
    # The log line from the good provider was rendered.
    assert any("resilience-ok" in line for line in reporter.log_lines)
    # One describe_parse_error warning was emitted for the broken provider.
    assert len(reporter.describe_parse_error_calls) == 1
    assert reporter.describe_parse_error_calls[0][0] == "provider-b"


def test_logs_broken_provider_owns_requested_service_returns_nonzero() -> None:
    """Scenario 2: broken provider would have owned the requested service → non-zero exit with warning."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            # provider-a emits valid describe: owns 'frontend'
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            # provider-b emits invalid describe; would have owned 'backend'
            f"{ENTRYPOINT_B} describe": SubprocessResult(returncode=0, stdout="not-json", stderr=""),
        },
        # No popen_responses: no provider should be invoked.
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    # Request 'backend', which only the broken provider would have owned.
    code = logs.stream(_log_opts(patterns=("backend",)), reporter)

    # Non-zero: ownership could not be resolved.
    assert code != 0
    # The broken provider was named in the warning.
    assert len(reporter.describe_parse_error_calls) == 1
    assert reporter.describe_parse_error_calls[0][0] == "provider-b"
    # No log lines were emitted.
    assert reporter.log_lines == []


def test_logs_both_providers_good_streams_correctly() -> None:
    """Scenario 3: both providers good → service owned by either provider streams normally."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        popen_responses={
            f"{ENTRYPOINT_B} logs alpha/backend --tail 200": (
                ['{"env":"alpha","svc":"backend","msg":"both-good"}'],
                0,
            ),
        },
    )
    logs, reporter = _make_logs(runner, registry, resolver)
    code = logs.stream(_log_opts(patterns=("alpha/backend",)), reporter)

    assert code == 0
    assert any("both-good" in line for line in reporter.log_lines)
    # No describe errors.
    assert reporter.describe_parse_error_calls == []


# ── status ownership routing (issue #108) ────────────────────────────────────


def test_status_scope_qualified_routes_only_to_owning_provider() -> None:
    """Scope-qualified status pattern dispatches only to the provider that owns the service.

    workspace/rabbitmq is owned by provider-b.  Provider-a must NOT be invoked,
    so it never emits the spurious 'matched no services' warning.
    """
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("rabbitmq")),
        },
        popen_responses={
            # Only provider-b should be invoked.
            f"{ENTRYPOINT_B} status workspace/rabbitmq": (
                [_status_doc("workspace", [_svc_entry("rabbitmq")])],
                0,
            ),
        },
    )
    svc, reporter = _make_status(runner, registry, resolver, with_describe=True)
    code = svc.report(_status_opts(patterns=("workspace/rabbitmq",)), reporter)

    assert code == 0
    assert len(reporter.status_documents) == 1
    # Provider-a must NOT have been invoked.
    popen_cmds = [cmd for cmd, _cwd in runner.popen_calls]
    assert not any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in popen_cmds)
    # Provider-b WAS invoked.
    assert any(str(ENTRYPOINT_B) in " ".join(cmd) for cmd in popen_cmds)


def test_status_scope_qualified_non_owning_provider_not_invoked() -> None:
    """Explicit assertion: the non-owning provider's popen is never called for a scope-qualified query."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("rabbitmq")),
        },
        popen_responses={
            f"{ENTRYPOINT_B} status workspace/rabbitmq": (
                [_status_doc("workspace", [_svc_entry("rabbitmq")])],
                0,
            ),
        },
    )
    svc, reporter = _make_status(runner, registry, resolver, with_describe=True)
    svc.report(_status_opts(patterns=("workspace/rabbitmq",)), reporter)

    # No popen call to provider-a at all.
    assert all(str(ENTRYPOINT_A) not in " ".join(cmd) for cmd, _cwd in runner.popen_calls)


def test_status_bare_pattern_retains_full_fan_out() -> None:
    """Bare patterns (no '/') fan out to all providers unchanged."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        popen_responses={
            # Both providers must be invoked for a bare pattern.
            f"{ENTRYPOINT_A} status": (
                [_status_doc("alpha", [_svc_entry("frontend")])],
                0,
            ),
            f"{ENTRYPOINT_B} status": (
                [_status_doc("alpha", [_svc_entry("rabbitmq")])],
                0,
            ),
        },
    )
    svc, reporter = _make_status(runner, registry, resolver, with_describe=True)
    code = svc.report(_status_opts(), reporter)

    assert code == 0
    # Both providers were invoked.
    popen_cmds = [cmd for cmd, _cwd in runner.popen_calls]
    assert any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in popen_cmds)
    assert any(str(ENTRYPOINT_B) in " ".join(cmd) for cmd in popen_cmds)
    # Merged result contains services from both.
    assert len(reporter.status_documents) == 1
    doc, _ = reporter.status_documents[0]
    svc_names = [s.name for e in doc.envs for s in e.services]
    assert "frontend" in svc_names
    assert "rabbitmq" in svc_names


def test_status_collect_scope_qualified_routes_to_owning_provider_only() -> None:
    """collect() with a scope-qualified pattern dispatches only to the owning provider."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("rabbitmq")),
        },
        popen_responses={
            f"{ENTRYPOINT_B} status workspace/rabbitmq": (
                [_status_doc("workspace", [_svc_entry("rabbitmq")])],
                0,
            ),
        },
    )
    svc, _reporter = _make_status(runner, registry, resolver, with_describe=True)
    doc = svc.collect(("workspace/rabbitmq",))

    assert doc is not None
    svc_names = [s.name for e in doc.envs for s in e.services]
    assert "rabbitmq" in svc_names
    # Provider-a was never polled.
    popen_cmds = [cmd for cmd, _cwd in runner.popen_calls]
    assert not any(str(ENTRYPOINT_A) in " ".join(cmd) for cmd in popen_cmds)


def test_status_unowned_scope_qualified_yields_no_service_matched() -> None:
    """An unowned scope-qualified pattern emits a single no_service_matched message."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
        # No popen_responses: no provider should be invoked.
    )
    svc, reporter = _make_status(runner, registry, resolver, with_describe=True)
    code = svc.report(_status_opts(patterns=("workspace/unknown-service",)), reporter)

    # Non-zero exit (service not found).
    assert code != 0
    # A single actionable no_service_matched message — not per-provider noise.
    assert len(reporter.no_service_matched_calls) == 1
    assert "workspace/unknown-service" in reporter.no_service_matched_calls[0]
    # No provider was invoked.
    assert runner.popen_calls == []


def test_status_unowned_scope_qualified_collect_returns_none() -> None:
    """collect() for an unowned scope-qualified pattern returns None (treated as not-running)."""
    registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("frontend")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("backend")),
        },
    )
    svc, _reporter = _make_status(runner, registry, resolver, with_describe=True)
    doc = svc.collect(("workspace/unknown-service",))

    assert doc is None
    # No provider was invoked.
    assert runner.popen_calls == []
