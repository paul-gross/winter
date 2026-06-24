"""Tests for RequiredServicesLintCheck.

Covers:
- No provision manifests → no findings
- Valid references against catalog → pass (no findings)
- Typo / unknown reference → fail finding with file/line + near-miss
- Scope mismatch: workspace ref → env service (and vice versa) → fail
- Multi-provider merge: references from both providers are resolved
- Missing orchestrator with required_services → warn finding
- Malformed token (no slash) → fail finding
- Extension manifest required_services are also checked
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.lint.models import LintScope, LintScopeKind, LintStatus
from winter_cli.modules.lint.required_services_check import RequiredServicesLintCheck
from winter_cli.modules.service.service_catalog_service import ServiceCatalogService

WS = Path("/fake/workspace")
EXT1 = Path("/fake/workspace/.winter/ext/winter-service-tmux")

PROVIDER = ResolvedCapability(
    slot=CapabilitySlot.service,
    extension_name="winter-service-tmux",
    entrypoint=EXT1 / "workflow/orchestrate",
    ext_dir=EXT1,
    prefix="wst",
    config_dir=WS / ".winter/config/winter-service-tmux",
)
_EP = str(PROVIDER.entrypoint)

SCOPE = LintScope(kind=LintScopeKind.all, label="all", paths=[WS])


def _run_ok(services: list[str]) -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout=json.dumps({"services": services}), stderr="")


def _make_check(
    tmp_ws: Path,
    *,
    catalog_services: list[str] | None = None,
    providers: list[ResolvedCapability] | None = None,
) -> RequiredServicesLintCheck:
    """Build a RequiredServicesLintCheck wired with fake collaborators.

    ``catalog_services`` is the list of scope-qualified service names returned
    by the fake provider when invoked with ``catalog``.
    """
    if providers is None:
        if catalog_services is not None:
            ep = str(PROVIDER.entrypoint)
            runner = FakeSubprocessRunner(run_responses={f"{ep} catalog": _run_ok(catalog_services)})
            providers = [PROVIDER]
        else:
            runner = FakeSubprocessRunner()
            providers = []
    else:
        if catalog_services is not None:
            ep = str(PROVIDER.entrypoint)
            runner = FakeSubprocessRunner(run_responses={f"{ep} catalog": _run_ok(catalog_services)})
        else:
            runner = FakeSubprocessRunner()

    catalog_svc = ServiceCatalogService(subprocess_runner=runner, workspace_root=tmp_ws)
    return RequiredServicesLintCheck(workspace_root=tmp_ws, catalog_service=catalog_svc, providers=providers)


def _write_config(tmp_ws: Path, content: str) -> None:
    """Write `.winter/config.toml` in *tmp_ws*."""
    cfg = tmp_ws / ".winter"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text(content, encoding="utf-8")


def _write_ext_manifest(tmp_ws: Path, ext_name: str, content: str) -> None:
    """Write a `winter-ext.toml` under `.winter/ext/<ext_name>/`."""
    ext_dir = tmp_ws / ".winter" / "ext" / ext_name
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "winter-ext.toml").write_text(content, encoding="utf-8")


# ── no provision → no findings ────────────────────────────────────────────────


def test_no_provision_no_findings(tmp_path: Path) -> None:
    """No provision manifest → no findings even without an orchestrator."""
    _write_config(tmp_path, "main_branch = 'master'\n")
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert findings == []


# ── valid references → no findings ───────────────────────────────────────────


def test_valid_workspace_ref_passes(tmp_path: Path) -> None:
    """A valid workspace/ reference against a workspace service → no findings."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/postgres"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert findings == []


def test_valid_env_ref_passes(tmp_path: Path) -> None:
    """A valid env-scoped reference matches a ``*/`` service in the catalog."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["alpha/api"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["*/api"])
    findings = check.check(SCOPE)
    assert findings == []


# ── typo / unknown reference → fail finding ───────────────────────────────────


def test_typo_unknown_service_emits_finding(tmp_path: Path) -> None:
    """A typo'd service name emits a fail finding with file/line."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/postgrs"]
""",
    )
    # Real service is "postgres", reference is "postgrs" (typo)
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    f = findings[0]
    assert f.status == LintStatus.fail
    assert "postgrs" in f.message
    assert f.file is not None
    assert ".winter/config.toml" in f.file
    assert f.line is not None and f.line > 0
    # Near-miss suggestion should mention "postgres"
    assert "workspace/postgres" in (f.message or "")


def test_unknown_service_lists_available(tmp_path: Path) -> None:
    """Unknown reference finding includes the remediation with available services."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/nosuchservice"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["workspace/postgres", "*/api"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    f = findings[0]
    assert f.status == LintStatus.fail
    assert f.remediation is not None
    assert "workspace/postgres" in f.remediation or "*/api" in f.remediation


# ── scope mismatch ────────────────────────────────────────────────────────────


def test_workspace_ref_does_not_match_env_service(tmp_path: Path) -> None:
    """A workspace/ ref does not match a ``*/`` (env) service."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/api"]
""",
    )
    # api is env-scoped, not workspace-scoped
    check = _make_check(tmp_path, catalog_services=["*/api"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    assert findings[0].status == LintStatus.fail
    assert "workspace" in findings[0].message


def test_env_ref_does_not_match_workspace_service(tmp_path: Path) -> None:
    """An env-scoped ref does not match a workspace service."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["beta/postgres"]
""",
    )
    # postgres is workspace-scoped
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    assert findings[0].status == LintStatus.fail


# ── multi-provider merge ──────────────────────────────────────────────────────


def test_multi_provider_merge_resolves_refs(tmp_path: Path) -> None:
    """References are resolved against the merged catalog from all providers."""
    # Two providers: tmux provides rabbitmq workspace, docker provides */api
    EP_A = str(PROVIDER.entrypoint)

    EXT2 = tmp_path / ".winter" / "ext" / "winter-service-docker"
    PROVIDER_B = ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name="winter-service-docker",
        entrypoint=EXT2 / "workflow/service",
        ext_dir=EXT2,
        prefix="wsd",
        config_dir=tmp_path / ".winter/config/winter-service-docker",
    )
    EP_B = str(PROVIDER_B.entrypoint)

    runner = FakeSubprocessRunner(
        run_responses={
            f"{EP_A} catalog": _run_ok(["workspace/rabbitmq"]),
            f"{EP_B} catalog": _run_ok(["*/api"]),
        }
    )
    catalog_svc = ServiceCatalogService(subprocess_runner=runner, workspace_root=tmp_path)

    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/rabbitmq", "beta/api"]
""",
    )
    check = RequiredServicesLintCheck(
        workspace_root=tmp_path,
        catalog_service=catalog_svc,
        providers=[PROVIDER, PROVIDER_B],
    )
    findings = check.check(SCOPE)
    assert findings == []


# ── missing orchestrator ──────────────────────────────────────────────────────


def test_missing_orchestrator_emits_warn_finding(tmp_path: Path) -> None:
    """When required_services exists but no orchestrator is registered, emit a warning."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/postgres"]
""",
    )
    # No providers
    runner = FakeSubprocessRunner()
    catalog_svc = ServiceCatalogService(subprocess_runner=runner, workspace_root=tmp_path)
    check = RequiredServicesLintCheck(workspace_root=tmp_path, catalog_service=catalog_svc, providers=[])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    assert findings[0].status == LintStatus.warn
    assert "no service orchestrator" in findings[0].message.lower()


# ── extension manifest required_services ─────────────────────────────────────


def test_extension_manifest_required_services_checked(tmp_path: Path) -> None:
    """required_services in a winter-ext.toml are also validated."""
    _write_config(tmp_path, "main_branch = 'master'\n")
    _write_ext_manifest(
        tmp_path,
        "my-ext",
        """
name = "my-ext"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/nosuch"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    assert findings[0].status == LintStatus.fail
    assert "nosuch" in findings[0].message


def test_extension_manifest_valid_ref_no_findings(tmp_path: Path) -> None:
    """A valid required_services in an extension manifest produces no findings."""
    _write_config(tmp_path, "main_branch = 'master'\n")
    _write_ext_manifest(
        tmp_path,
        "my-ext",
        """
name = "my-ext"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/postgres"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert findings == []


# ── malformed token ───────────────────────────────────────────────────────────


def test_malformed_token_emits_fail(tmp_path: Path) -> None:
    """A token without a '/' separator is malformed and emits a fail finding."""
    _write_config(
        tmp_path,
        """
main_branch = "master"
[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["noslash"]
""",
    )
    check = _make_check(tmp_path, catalog_services=["workspace/postgres"])
    findings = check.check(SCOPE)
    assert len(findings) == 1
    f = findings[0]
    assert f.status == LintStatus.fail
    assert "noslash" in f.message


# ── line number tracking ──────────────────────────────────────────────────────


def test_finding_includes_line_number(tmp_path: Path) -> None:
    """Fail findings include a positive line number pointing at the required_services key."""
    _write_config(
        tmp_path,
        """main_branch = "master"

[[provision.resource]]
scope = "feature-environment"
apply = "scripts/apply.sh"
required_services = ["workspace/missing"]
""",
    )
    check = _make_check(tmp_path, catalog_services=[])
    findings = check.check(SCOPE)
    assert findings
    f = findings[0]
    assert f.line is not None
    assert f.line > 0
