from __future__ import annotations

from pathlib import Path
from typing import cast

from tests.conftest import FakeFilesystem
from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.config_file import IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.doctor.core_probe_service import CORE_SOURCE, CoreProbeService
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository

WORKSPACE_ROOT = Path("/ws")
CLAUDE_AGENTS = WORKSPACE_ROOT / ".claude" / "agents"
CLAUDE_SKILLS = WORKSPACE_ROOT / ".claude" / "skills"


def _build_service(fs: FakeFilesystem) -> CoreProbeService:
    """Construct a CoreProbeService for direct-method tests.

    The claude-symlinks probe only touches `config` and `fs`; the remaining
    collaborators are stubbed because exercising `run()` would drag in
    git/repo plumbing irrelevant to this probe.
    """
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
    )
    return CoreProbeService(
        config=config,
        fs=cast(IFilesystemReader, fs),
        subprocess_runner=cast(ISubprocessRunner, None),
        config_file_reader=cast(IConfigFileReader, None),
        repo_factory=cast(RepositoryFactory, None),
        worktree_repo=cast(IReadWorkspaceRepository, None),
        repo_repo=cast(IWriteRepoRepository, None),
    )


def test_claude_symlinks_probe_returns_none_when_directories_absent() -> None:
    fs = FakeFilesystem(directories={WORKSPACE_ROOT})
    svc = _build_service(fs)

    assert svc._probe_claude_symlinks() is None


def test_claude_symlinks_probe_passes_when_all_symlinks_resolve() -> None:
    agent_target = WORKSPACE_ROOT / "ai" / "harness" / "agents" / "code-reviewer.md"
    skill_target = WORKSPACE_ROOT / "ai" / "harness" / "skills" / "verify"
    skill_marker = skill_target / "SKILL.md"
    fs = FakeFilesystem(
        files={agent_target: "...", skill_marker: "..."},
        directories={CLAUDE_AGENTS, CLAUDE_SKILLS, skill_target},
        symlinks={
            CLAUDE_AGENTS / "wh-code-reviewer.md": Path("../../ai/harness/agents/code-reviewer.md"),
            CLAUDE_SKILLS / "wh-verify": Path("../../ai/harness/skills/verify"),
        },
    )
    svc = _build_service(fs)

    result = svc._probe_claude_symlinks()

    assert result is not None
    assert result.status == ProbeStatus.pass_
    assert result.source == CORE_SOURCE
    assert result.name == ".claude symlinks"


def test_claude_symlinks_probe_fails_and_names_every_orphan() -> None:
    fs = FakeFilesystem(
        directories={CLAUDE_AGENTS, CLAUDE_SKILLS},
        symlinks={
            CLAUDE_AGENTS / "wf-agentic-development-manager.md": Path(
                "../../ai/workflow/agents/agentic-development-manager.md"
            ),
            CLAUDE_SKILLS / "wf-old-skill": Path("../../ai/workflow/skills/old-skill"),
        },
    )
    svc = _build_service(fs)

    result = svc._probe_claude_symlinks()

    assert result is not None
    assert result.status == ProbeStatus.fail
    assert ".claude/agents/wf-agentic-development-manager.md" in result.message
    assert ".claude/skills/wf-old-skill" in result.message
    assert result.remediation is not None
    assert "winter ws init" in result.remediation


def test_claude_symlinks_probe_ignores_regular_files_and_dirs() -> None:
    """Plain README.md / regular subdirs under .claude/ must not register as orphans."""
    valid_target = WORKSPACE_ROOT / "ai" / "harness" / "agents" / "code-reviewer.md"
    readme = CLAUDE_AGENTS / "README.md"
    plain_subdir = CLAUDE_AGENTS / "docs"
    fs = FakeFilesystem(
        files={valid_target: "...", readme: "extension-installed notes"},
        directories={CLAUDE_AGENTS, plain_subdir},
        symlinks={
            CLAUDE_AGENTS / "wh-code-reviewer.md": Path("../../ai/harness/agents/code-reviewer.md"),
        },
    )
    svc = _build_service(fs)

    result = svc._probe_claude_symlinks()

    assert result is not None
    assert result.status == ProbeStatus.pass_


def test_claude_symlinks_probe_runs_when_only_one_subdir_present() -> None:
    """`.claude/agents` alone (no `.claude/skills`) still gets walked."""
    fs = FakeFilesystem(
        directories={CLAUDE_AGENTS},
        symlinks={CLAUDE_AGENTS / "wf-gone.md": Path("../../missing/agent.md")},
    )
    svc = _build_service(fs)

    result = svc._probe_claude_symlinks()

    assert result is not None
    assert result.status == ProbeStatus.fail
    assert ".claude/agents/wf-gone.md" in result.message
