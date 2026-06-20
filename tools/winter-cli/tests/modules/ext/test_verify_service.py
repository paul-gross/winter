from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.spec_loader import SpecLoader
from winter_cli.modules.ext.models import VerifyReport
from winter_cli.modules.ext.verify_service import (
    _PROBE_ENV,
    _PROBE_PATTERN,
    _SENTINEL,
    _UNKNOWN_ACTION,
    ConformanceVerifyService,
)
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
EXT_DIR = WS / "my-ext"
ENTRYPOINT = EXT_DIR / "workflow/service"


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


class _StubRegistry:
    """Minimal registry stub — try_resolve_extension bypasses the registry entirely."""

    def resolve(self, slot: Any) -> Any:  # pragma: no cover
        raise AssertionError("registry.resolve should not be called by verify")


def _real_spec_loader() -> SpecLoader:
    from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader

    return SpecLoader(config_file_reader=TomllibConfigFileReader())


# ── factory helpers ───────────────────────────────────────────────────────────


def _svc(
    *,
    runner: FakeSubprocessRunner,
    repos: list[StandaloneRepository] | None = None,
    manifests: dict[Path, dict] | None = None,
    files: dict[Path, str] | None = None,
) -> ConformanceVerifyService:
    """Build a ConformanceVerifyService wired with fake collaborators."""
    repos = repos or []
    manifests = manifests or {}
    files = files or {}
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(manifests))
    fs = FakeFilesystem(files=files)
    spec_loader = _real_spec_loader()
    orchestrator_resolver = ServiceOrchestratorResolver(
        registry=_StubRegistry(),  # type: ignore[arg-type]
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        fs=fs,
        override=None,
        workspace_root=WS,
    )
    return ConformanceVerifyService(
        subprocess_runner=runner,
        orchestrator_resolver=orchestrator_resolver,
        spec_loader=spec_loader,
        workspace_root=WS,
    )


def _local_svc(runner: FakeSubprocessRunner, ext_dir: Path = EXT_DIR) -> ConformanceVerifyService:
    """Service pre-wired with a valid local-path extension."""
    return _svc(
        runner=runner,
        manifests={ext_dir / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={
            ext_dir / EXT_MANIFEST: "",
            ext_dir / "workflow/service": "",
        },
    )


# ── path-mode: conforming extension (all checks pass) ────────────────────────


def _conforming_runner(ep: Path = ENTRYPOINT) -> FakeSubprocessRunner:
    """Runner whose responses satisfy all three check kinds for the service spec."""
    ep_str = str(ep)

    def run_response(action: str, *args: str) -> SubprocessResult:
        argv_str = " ".join([ep_str, action, *args])
        # Echo argv as stdout so forwards-params sentinel check passes.
        return SubprocessResult(returncode=0, stdout=argv_str, stderr="")

    responses: dict[str, SubprocessResult] = {
        # accepts-action checks (exit 0 for each of the 6 declared actions)
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        # refuses-unknown check (exit 2 = non-zero)
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        # forwards-params check (echo sentinel back in stdout)
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    return FakeSubprocessRunner(run_responses=responses)


def test_conforming_extension_all_checks_pass() -> None:
    runner = _conforming_runner()
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert not report.any_failed
    assert report.setup_failure is None
    assert all(r.passed for r in report.results)


def test_conforming_extension_reports_accepts_action_checks() -> None:
    runner = _conforming_runner()
    report = _local_svc(runner).verify(str(EXT_DIR))
    check_ids = {r.check_id for r in report.results}
    assert "accepts-up" in check_ids
    assert "accepts-down" in check_ids
    assert "accepts-status" in check_ids
    assert "accepts-restart" in check_ids
    assert "accepts-logs" in check_ids
    assert "accepts-describe" in check_ids


def test_conforming_extension_reports_refuses_unknown_check() -> None:
    runner = _conforming_runner()
    report = _local_svc(runner).verify(str(EXT_DIR))
    refuses = next(r for r in report.results if r.check_id == "refuses-unknown")
    assert refuses.passed


def test_conforming_extension_reports_forwards_params_check() -> None:
    runner = _conforming_runner()
    report = _local_svc(runner).verify(str(EXT_DIR))
    forwards = next(r for r in report.results if r.check_id == "forwards-params")
    assert forwards.passed


# ── failure class A: action rejected with exit 2 (rejects valid action) ──────


def test_action_rejected_with_exit_2_fails_accepts_check() -> None:
    """When the entrypoint returns exit 2 for a declared action, that check fails."""
    ep_str = str(ENTRYPOINT)
    # Clobber the "up" response to return exit 2 (unknown-action signal).
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert report.any_failed
    accepts_up = next(r for r in report.results if r.check_id == "accepts-up")
    assert not accepts_up.passed
    assert accepts_up.observed_exit == 2


def test_action_rejected_report_has_descriptive_detail() -> None:
    ep_str = str(ENTRYPOINT)
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    accepts_up = next(r for r in report.results if r.check_id == "accepts-up")
    assert "up" in accepts_up.detail
    assert "2" in accepts_up.detail


# ── failure class B: accepts unknown action (exit 0 for unknown) ─────────────


def test_accepts_unknown_action_fails_refuses_check() -> None:
    """When exit is 0 for an unknown action, the refuses-unknown check fails."""
    ep_str = str(ENTRYPOINT)
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        # Returns exit 0 for the unknown action — fails the refuses-unknown check.
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert report.any_failed
    refuses = next(r for r in report.results if r.check_id == "refuses-unknown")
    assert not refuses.passed
    assert refuses.observed_exit == 0


def test_accepts_unknown_action_detail_mentions_exit_0() -> None:
    ep_str = str(ENTRYPOINT)
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    refuses = next(r for r in report.results if r.check_id == "refuses-unknown")
    assert "exit 0" in refuses.detail


# ── failure class C: drops params (sentinel not echoed back) ─────────────────


def test_drops_params_fails_forwards_check() -> None:
    """When stdout/stderr does not contain the sentinel, forwards-params fails."""
    ep_str = str(ENTRYPOINT)
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        # Empty stdout/stderr — sentinel not echoed, drops params.
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(returncode=0, stdout="", stderr=""),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert report.any_failed
    forwards = next(r for r in report.results if r.check_id == "forwards-params")
    assert not forwards.passed


def test_drops_params_detail_mentions_sentinel() -> None:
    ep_str = str(ENTRYPOINT)
    responses = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":[]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(returncode=0, stdout="", stderr=""),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    forwards = next(r for r in report.results if r.check_id == "forwards-params")
    assert _SENTINEL in forwards.detail


# ── setup failures ────────────────────────────────────────────────────────────


def test_setup_failure_when_extension_dir_missing() -> None:
    runner = FakeSubprocessRunner(run_responses={})
    svc = _svc(runner=runner, files={})
    report = svc.verify(str(EXT_DIR))
    assert report.setup_failure is not None
    assert report.any_failed
    assert report.results == []


def test_setup_failure_when_manifest_missing() -> None:
    runner = FakeSubprocessRunner(run_responses={})
    svc = _svc(
        runner=runner,
        files={EXT_DIR / "something": ""},  # dir implicitly exists, but no manifest
    )
    report = svc.verify(str(EXT_DIR))
    assert report.setup_failure is not None


def test_setup_failure_when_no_service_entrypoint_declared() -> None:
    runner = FakeSubprocessRunner(run_responses={})
    svc = _svc(
        runner=runner,
        manifests={EXT_DIR / EXT_MANIFEST: {}},  # no orchestrate_services / provides
        files={EXT_DIR / EXT_MANIFEST: ""},
    )
    report = svc.verify(str(EXT_DIR))
    assert report.setup_failure is not None


def test_setup_failure_when_entrypoint_file_missing() -> None:
    runner = FakeSubprocessRunner(run_responses={})
    svc = _svc(
        runner=runner,
        manifests={EXT_DIR / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={EXT_DIR / EXT_MANIFEST: ""},  # manifest present; entrypoint absent
    )
    report = svc.verify(str(EXT_DIR))
    assert report.setup_failure is not None


# ── name-mode resolution ──────────────────────────────────────────────────────


def test_name_mode_resolves_installed_extension() -> None:
    repo = StandaloneRepository(name="my-ext", path=EXT_DIR)
    runner = _conforming_runner()
    svc = _svc(
        runner=runner,
        repos=[repo],
        manifests={EXT_DIR / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={EXT_DIR / EXT_MANIFEST: "", EXT_DIR / "workflow/service": ""},
    )
    report = svc.verify("my-ext")
    assert not report.any_failed


def test_name_mode_setup_failure_when_extension_not_installed() -> None:
    runner = FakeSubprocessRunner(run_responses={})
    svc = _svc(runner=runner, repos=[])
    report = svc.verify("nonexistent-ext")
    assert report.setup_failure is not None


# ── VerifyReport.any_failed ───────────────────────────────────────────────────


def test_any_failed_true_when_setup_failure_set() -> None:
    report = VerifyReport(setup_failure="something went wrong")
    assert report.any_failed


def test_any_failed_false_when_all_results_pass() -> None:
    from winter_cli.modules.ext.models import CheckResult

    result = CheckResult(check_id="x", passed=True, detail="ok", argv=[], observed_exit=0)
    report = VerifyReport(results=[result])
    assert not report.any_failed


def test_any_failed_true_when_one_result_fails() -> None:
    from winter_cli.modules.ext.models import CheckResult

    result = CheckResult(check_id="x", passed=False, detail="fail", argv=[], observed_exit=2)
    report = VerifyReport(results=[result])
    assert report.any_failed


# ── env vars forwarded to subprocess ─────────────────────────────────────────


def test_verify_sets_winter_env_vars_on_subprocess() -> None:
    """Dispatches must set WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX."""
    runner = _conforming_runner()
    _local_svc(runner).verify(str(EXT_DIR))

    assert runner.run_calls, "expected at least one subprocess.run call"
    env = runner.run_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(EXT_DIR)


def test_verify_cwd_is_workspace_root() -> None:
    runner = _conforming_runner()
    _local_svc(runner).verify(str(EXT_DIR))
    _, cwd = runner.run_calls[0]
    assert cwd == WS


# ── emits-describe-json check ─────────────────────────────────────────────────


def _runner_with_bad_describe_json(ep: Path = ENTRYPOINT) -> FakeSubprocessRunner:
    """Runner whose describe action emits malformed JSON — fails emits-describe-json."""
    ep_str = str(ep)
    responses: dict[str, SubprocessResult] = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        # describe emits malformed JSON — not a valid {"services": [...]} object.
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout="this is not json", stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    return FakeSubprocessRunner(run_responses=responses)


def _runner_with_non_object_describe(ep: Path = ENTRYPOINT) -> FakeSubprocessRunner:
    """Runner whose describe action emits a JSON array (not an object) — fails check."""
    ep_str = str(ep)
    responses: dict[str, SubprocessResult] = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        # describe emits a JSON array, not an object — fails the check.
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='["service-a", "service-b"]', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    return FakeSubprocessRunner(run_responses=responses)


def test_conforming_extension_emits_describe_json_check_passes() -> None:
    """A conforming extension (emitting {\"services\": []}) passes the emits-describe-json check."""
    runner = _conforming_runner()
    report = _local_svc(runner).verify(str(EXT_DIR))
    check_ids = {r.check_id for r in report.results}
    assert "emits-describe-json" in check_ids
    emits_check = next(r for r in report.results if r.check_id == "emits-describe-json")
    assert emits_check.passed


def test_malformed_describe_json_fails_emits_describe_json_check() -> None:
    """An extension emitting non-JSON for describe fails the emits-describe-json check."""
    runner = _runner_with_bad_describe_json()
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert report.any_failed
    emits_check = next(r for r in report.results if r.check_id == "emits-describe-json")
    assert not emits_check.passed
    # Detail should mention the parse failure.
    assert "parseable" in emits_check.detail or "parse" in emits_check.detail.lower()


def test_non_object_describe_json_fails_emits_describe_json_check() -> None:
    """An extension emitting a JSON array (not an object) for describe fails the check."""
    runner = _runner_with_non_object_describe()
    report = _local_svc(runner).verify(str(EXT_DIR))
    assert report.any_failed
    emits_check = next(r for r in report.results if r.check_id == "emits-describe-json")
    assert not emits_check.passed


def test_valid_services_list_passes_emits_describe_json_check() -> None:
    """An extension emitting {\"services\": [\"a\", \"b\"]} passes the check."""
    ep_str = str(ENTRYPOINT)
    responses: dict[str, SubprocessResult] = {
        f"{ep_str} up {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} down {_PROBE_ENV}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} status": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} restart {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        f"{ep_str} logs {_PROBE_PATTERN}": SubprocessResult(returncode=0, stdout="", stderr=""),
        # Valid: non-empty services list.
        f"{ep_str} describe": SubprocessResult(returncode=0, stdout='{"services":["api","worker"]}', stderr=""),
        f"{ep_str} {_UNKNOWN_ACTION}": SubprocessResult(returncode=2, stdout="", stderr=""),
        f"{ep_str} status {_SENTINEL}/__svc__": SubprocessResult(
            returncode=0, stdout=f"{_SENTINEL}/__svc__", stderr=""
        ),
    }
    runner = FakeSubprocessRunner(run_responses=responses)
    report = _local_svc(runner).verify(str(EXT_DIR))
    emits_check = next(r for r in report.results if r.check_id == "emits-describe-json")
    assert emits_check.passed
