from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeConfigFileReader, FakeFilesystem
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.provision.manifest_probe_service import (
    PROVISION_SOURCE,
    ProvisionManifestProbeService,
)
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WORKSPACE_ROOT = Path("/ws")
EXT_PATH = WORKSPACE_ROOT / "my-ext"


def _build_config(
    provision_raw: dict | None = None,
    adopt: AdoptExtensions = AdoptExtensions.winter,
) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=adopt,
        provision_raw=provision_raw or {},
    )


def _build_service(
    config: WorkspaceConfig,
    *,
    ext_manifest_data: dict | None = None,
    ext_present: bool = False,
) -> tuple[ProvisionManifestProbeService, StandaloneRepository]:
    files: dict[Path, str] = {}
    if ext_present:
        files[EXT_PATH / EXT_MANIFEST] = ""

    fs = FakeFilesystem(files=files)
    config_files: dict[Path, dict] = {}
    if ext_present and ext_manifest_data is not None:
        config_files[EXT_PATH / EXT_MANIFEST] = ext_manifest_data

    config_reader = FakeConfigFileReader(config_files)
    loader = ExtensionManifestLoader(config_file_reader=config_reader)
    svc = ProvisionManifestProbeService(
        config=config,
        fs=fs,
        manifest_loader=loader,
        config_file_reader=config_reader,
    )
    repo = StandaloneRepository(name="my-ext", path=EXT_PATH)
    return svc, repo


# ── workspace-config provision ────────────────────────────────────────────────


def test_no_provision_declared_emits_no_results() -> None:
    config = _build_config(provision_raw={})
    svc, repo = _build_service(config)

    results = svc.run([repo])

    assert results == []


def test_well_formed_workspace_manifest_emits_single_pass() -> None:
    raw = {
        "dependency": [{"scope": "feature-worktree", "apply": "scripts/install.sh"}],
        "resource": [
            {
                "scope": "workspace",
                "apply": "scripts/create-db.sh",
                "destroy": "scripts/drop-db.sh",
                "required_services": ["workspace/postgres"],
            }
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_
    assert results[0].source == PROVISION_SOURCE
    assert "2 provision handler(s) valid" in results[0].message


def test_bad_scope_emits_fail_naming_location() -> None:
    raw = {
        "dependency": [{"scope": "bad-scope", "apply": "scripts/install.sh"}],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert r.source == PROVISION_SOURCE
    assert "bad-scope" in r.message
    assert "scope" in r.message
    assert "dependency" in r.message


def test_missing_apply_emits_fail() -> None:
    raw = {
        "resource": [{"scope": "workspace"}],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message
    assert "resource" in r.message


def test_required_services_on_dependency_emits_fail() -> None:
    raw = {
        "dependency": [
            {
                "scope": "feature-environment",
                "apply": "scripts/install.sh",
                "required_services": ["workspace/postgres"],
            }
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "required_services" in r.message
    assert "dependency" in r.message


def test_unknown_key_in_entry_emits_fail() -> None:
    raw = {
        "data": [
            {
                "scope": "feature-environment",
                "apply": "scripts/seed.sh",
                "bogus_key": "oops",
            }
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "bogus_key" in r.message


def test_multiple_bad_entries_emit_multiple_fail_results() -> None:
    """Per-finding: every bad entry produces its own ProbeResult."""
    raw = {
        "dependency": [
            # entry 0: bad scope
            {"scope": "not-valid", "apply": "scripts/install.sh"},
            # entry 1: missing apply
            {"scope": "workspace"},
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    # Must be two separate fail results — one per bad entry.
    assert len(results) == 2
    assert all(r.status == ProbeStatus.fail for r in results)
    # First result mentions bad scope.
    assert "not-valid" in results[0].message
    # Second result mentions missing apply.
    assert "apply" in results[1].message


def test_mixed_good_and_bad_entries_all_bad_are_reported() -> None:
    """Good entry does not suppress reporting of the bad one."""
    raw = {
        "resource": [
            # entry 0: good
            {
                "scope": "workspace",
                "apply": "scripts/create-db.sh",
            },
            # entry 1: bad scope
            {
                "scope": "totally-wrong",
                "apply": "scripts/other.sh",
            },
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)

    results = svc.run([])

    # Only the bad entry produces a fail (the good one is counted but not
    # separately emitted as a pass when there are failures).
    fail_results = [r for r in results if r.status == ProbeStatus.fail]
    assert len(fail_results) == 1
    assert "totally-wrong" in fail_results[0].message


# ── extension provision ───────────────────────────────────────────────────────


def test_extension_well_formed_provision_emits_no_results() -> None:
    """A valid extension manifest passes strict parse; probe emits nothing."""
    ext_data = {
        "name": "my-ext",
        "provision": {
            "dependency": [{"scope": "feature-worktree", "apply": "scripts/install.sh"}],
        },
    }
    config = _build_config()
    svc, repo = _build_service(config, ext_manifest_data=ext_data, ext_present=True)

    results = svc.run([repo])

    # A valid extension manifest raises no error in the strict loader, so
    # the probe returns [] for it (manifest.provision already parsed cleanly).
    assert results == []


def test_extension_bad_scope_emits_fail_attributed_to_extension() -> None:
    """A malformed extension provision entry produces a per-finding fail."""
    # We need the manifest to fail strictly (so the probe falls back to raw
    # TOML validation), then the raw data contains a bad scope.
    # The strict parser raises RepoError on bad scope — so it will fail to
    # load, and the probe reads the raw TOML and validates leniently.
    ext_data = {
        "name": "my-ext",
        "provision": {
            "dependency": [{"scope": "bad-scope", "apply": "scripts/install.sh"}],
        },
    }
    config = _build_config()
    svc, repo = _build_service(config, ext_manifest_data=ext_data, ext_present=True)

    results = svc.run([repo])

    assert len(results) >= 1
    fail_results = [r for r in results if r.status == ProbeStatus.fail]
    assert len(fail_results) == 1
    r = fail_results[0]
    assert "bad-scope" in r.message
    # Source must be PROVISION_SOURCE (the in-core probe).
    assert r.source == PROVISION_SOURCE


def test_extension_missing_apply_emits_fail() -> None:
    ext_data = {
        "name": "my-ext",
        "provision": {
            "resource": [{"scope": "workspace"}],
        },
    }
    config = _build_config()
    svc, repo = _build_service(config, ext_manifest_data=ext_data, ext_present=True)

    results = svc.run([repo])

    fail_results = [r for r in results if r.status == ProbeStatus.fail]
    assert len(fail_results) == 1
    assert "apply" in fail_results[0].message


def test_adopt_none_skips_extension_probes() -> None:
    """When adopt_extensions=none, extension provision probes are skipped."""
    ext_data = {
        "name": "my-ext",
        "provision": {
            "dependency": [{"scope": "bad-scope", "apply": "scripts/install.sh"}],
        },
    }
    config = _build_config(adopt=AdoptExtensions.none)
    svc, repo = _build_service(config, ext_manifest_data=ext_data, ext_present=True)

    results = svc.run([repo])

    assert results == []


def test_no_ext_manifest_file_emits_no_results() -> None:
    """Extensions without a winter-ext.toml are silently skipped."""
    config = _build_config()
    # ext_present=False → no file on disk
    svc, repo = _build_service(config, ext_present=False)

    results = svc.run([repo])

    assert results == []


# ── apply field: string | list validation ─────────────────────────────────────


def test_apply_string_ok() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": "echo hello"}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_apply_list_ok() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": ["echo hello", "echo world"]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_apply_empty_string_fail() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": ""}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message
    assert "empty string" in r.message


def test_apply_empty_list_fail() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": []}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message
    assert "empty list" in r.message


def test_apply_list_with_non_string_element_fail() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": ["echo ok", 42]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message


def test_apply_list_with_empty_string_element_fail() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": ["echo ok", ""]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message


def test_apply_neither_str_nor_list_fail() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": 123}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "apply" in r.message


# ── destroy field: optional, same validation ──────────────────────────────────


def test_destroy_absent_ok() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": "echo hello"}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_destroy_string_ok() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": "echo destroy"}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_destroy_list_ok() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": ["echo a", "echo b"]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_destroy_empty_string_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": ""}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "destroy" in r.message


def test_destroy_empty_list_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": []}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "destroy" in r.message
    assert "empty list" in r.message


def test_destroy_non_string_element_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": ["ok", None]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "destroy" in r.message


def test_destroy_neither_str_nor_list_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": 99}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "destroy" in r.message


# ── reset field: optional, same validation ────────────────────────────────────


def test_reset_absent_ok() -> None:
    raw = {"dependency": [{"scope": "feature-worktree", "apply": "echo hello"}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_reset_string_ok() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": "echo reset"}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_reset_list_ok() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": ["echo a", "echo b"]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_reset_empty_string_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": ""}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "reset" in r.message


def test_reset_empty_list_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": []}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "reset" in r.message
    assert "empty list" in r.message


def test_reset_non_string_element_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": [True]}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "reset" in r.message


def test_reset_neither_str_nor_list_fail() -> None:
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "reset": 3.14}]}
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "reset" in r.message


# ── multi-field well-formed manifest ─────────────────────────────────────────


def test_well_formed_multi_field_manifest_emits_single_pass() -> None:
    """An entry with apply/destroy/reset all as lists yields a single pass."""
    raw = {
        "resource": [
            {
                "scope": "workspace",
                "apply": ["echo step-1", "echo step-2"],
                "destroy": ["echo clean-1", "echo clean-2"],
                "reset": "echo reset",
                "required_services": ["workspace/postgres"],
            }
        ],
        "dependency": [
            {"scope": "feature-worktree", "apply": "echo install"},
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_
    assert "2 provision handler(s) valid" in results[0].message


# ── invalid destroy/reset emit per-field findings ────────────────────────────


def test_invalid_destroy_and_reset_both_reported() -> None:
    """Both destroy and reset violations on the same entry produce two findings."""
    raw = {
        "resource": [
            {
                "scope": "workspace",
                "apply": "echo ok",
                "destroy": [],
                "reset": 0,
            }
        ],
    }
    config = _build_config(provision_raw=raw)
    svc, _repo = _build_service(config)
    results = svc.run([])
    assert len(results) == 2
    assert all(r.status == ProbeStatus.fail for r in results)
    messages = " ".join(r.message for r in results)
    assert "destroy" in messages
    assert "reset" in messages


def test_workspace_and_extension_findings_combined() -> None:
    """Workspace and extension findings are both reported in the same run."""
    raw = {
        "data": [{"scope": "bad-scope", "apply": "scripts/seed.sh"}],
    }
    ext_data = {
        "name": "my-ext",
        "provision": {
            "resource": [{"scope": "workspace"}],  # missing apply
        },
    }
    config = _build_config(provision_raw=raw)
    svc, repo = _build_service(config, ext_manifest_data=ext_data, ext_present=True)

    results = svc.run([repo])

    fail_results = [r for r in results if r.status == ProbeStatus.fail]
    # One from workspace (bad scope), one from extension (missing apply).
    assert len(fail_results) == 2
