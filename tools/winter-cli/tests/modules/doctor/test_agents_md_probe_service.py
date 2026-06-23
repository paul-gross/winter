"""Tests for AgentsMdProbeService: AGENTS.md drift doctor check.

Covers:
  1. In-sync AGENTS.md -> pass result.
  2. Missing AGENTS.md -> warn result with remediation hint.
  3. Stale AGENTS.md (source changed without regeneration) -> warn result.
  4. No CLAUDE.md at root -> no result emitted (skipped silently).
  5. Feature-env roots (immediate children with CLAUDE.md) are also probed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.doctor.agents_md_probe_service import AGENTS_SOURCE, AgentsMdProbeService
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.workspace.agents_md_service import (
    AGENTS_MD_FILENAME,
    AgentsMdService,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
    )


def _make_agents_md_svc(fs: FakeFilesystem, config: WorkspaceConfig) -> AgentsMdService:
    return AgentsMdService(config=config, fs=fs)


def _build_sync_agents_md(fs: FakeFilesystem, root: Path, config: WorkspaceConfig) -> None:
    """Generate a fresh AGENTS.md at `root` via the service so it is in sync."""
    from tests.conftest import FakeInitReporter

    svc = _make_agents_md_svc(fs, config)
    svc.generate(root, FakeInitReporter())


# ---------------------------------------------------------------------------
# Workspace root checks
# ---------------------------------------------------------------------------


def test_probe_pass_when_agents_md_in_sync(workspace_config: WorkspaceConfig) -> None:
    """A freshly generated AGENTS.md produces a pass result."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "# AI Index\n",
        }
    )
    _build_sync_agents_md(fs, WORKSPACE_ROOT, workspace_config)

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_
    assert results[0].source == AGENTS_SOURCE


def test_probe_warn_when_agents_md_missing(workspace_config: WorkspaceConfig) -> None:
    """AGENTS.md absent alongside a CLAUDE.md produces a warn result."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "# AI Index\n",
        }
    )
    # Deliberately don't generate AGENTS.md.

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    assert len(results) == 1
    assert results[0].status == ProbeStatus.warn
    assert "missing" in results[0].message.lower()
    assert results[0].remediation is not None
    assert "winter ws init" in (results[0].remediation or "")


def test_probe_warn_when_agents_md_stale(workspace_config: WorkspaceConfig) -> None:
    """Stale AGENTS.md (source changed after generation) produces a warn result."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "Original content\n",
        }
    )
    _build_sync_agents_md(fs, WORKSPACE_ROOT, workspace_config)

    # Mutate a source after generation — now the on-disk file is out of sync.
    fs.files[WORKSPACE_ROOT / "ai" / "index.md"] = "Changed content — now stale\n"

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    assert len(results) == 1
    assert results[0].status == ProbeStatus.warn
    assert "out of sync" in results[0].message.lower()


def test_probe_no_result_when_no_claude_md(workspace_config: WorkspaceConfig) -> None:
    """No result is emitted when the workspace root has no CLAUDE.md."""
    fs = FakeFilesystem(files={})

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    assert results == []


# ---------------------------------------------------------------------------
# Feature-env root discovery
# ---------------------------------------------------------------------------


def test_probe_discovers_env_root_with_claude_md(workspace_config: WorkspaceConfig) -> None:
    """A feature-env child directory with CLAUDE.md is probed for its AGENTS.md."""
    env_root = WORKSPACE_ROOT / "beta"
    fs = FakeFilesystem(
        files={
            # Workspace root has no CLAUDE.md (no probe expected there).
            env_root / "CLAUDE.md": "@ai/index.md\n",
            env_root / "ai" / "index.md": "# Feature content\n",
        }
    )
    _build_sync_agents_md(fs, env_root, workspace_config)

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    # Only the env root is probed (workspace root has no CLAUDE.md).
    assert len(results) == 1
    assert results[0].status == ProbeStatus.pass_


def test_probe_warns_env_root_missing_agents_md(workspace_config: WorkspaceConfig) -> None:
    """A feature-env root with CLAUDE.md but no AGENTS.md emits a warn."""
    env_root = WORKSPACE_ROOT / "beta"
    fs = FakeFilesystem(
        files={
            env_root / "CLAUDE.md": "@ai/index.md\n",
            env_root / "ai" / "index.md": "# Feature content\n",
        }
    )
    # Do NOT generate AGENTS.md.

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    assert any(r.status == ProbeStatus.warn for r in results)


def test_probe_skips_dotdir_children(workspace_config: WorkspaceConfig) -> None:
    """Dot-directories and known infra directories are never treated as env roots."""
    dot_child = WORKSPACE_ROOT / ".claude"
    fs = FakeFilesystem(
        files={
            dot_child / "CLAUDE.md": "content\n",
        }
    )

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))
    results = probe.run()

    # .claude is skipped; workspace root has no CLAUDE.md — so no results.
    assert results == []


def test_probe_drift_detection_idempotency(workspace_config: WorkspaceConfig) -> None:
    """Running the probe multiple times on unchanged in-sync state always returns pass."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "Stable content\n",
        }
    )
    _build_sync_agents_md(fs, WORKSPACE_ROOT, workspace_config)

    probe = AgentsMdProbeService(config=workspace_config, fs=fs, agents_md_svc=_make_agents_md_svc(fs, workspace_config))

    for _ in range(3):
        results = probe.run()
        assert len(results) == 1
        assert results[0].status == ProbeStatus.pass_
