"""Service-level tests for `WorkspaceMergeService.merge_all`.

These exercise the orchestration layer with fake repos. End-to-end git
behavior of `merge_ref` is covered in
`tests/modules/workspace/internal/test_write_repo_repository_merge.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.merge_reporter import IMergeReporter
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureWorktree,
    MergeMode,
    MergeResult,
    PinnedScope,
    ProjectRepository,
    RepoMergeOutcome,
    RepoScope,
    StandaloneRepository,
    Workspace,
)
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_merge_service import WorkspaceMergeService

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
            ProjectRepositoryConfig(name="other", url="git@example.com:org/other.git"),
        ],
    )


class _FakeWorkspaceRepo:
    """Returns the configured env list verbatim — no filesystem probing."""

    def __init__(self, env_names: list[str]) -> None:
        self._env_names = env_names

    def get_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        idx = self._env_names.index(name) + 1 if name in self._env_names else 99
        return FeatureEnvironment(
            workspace=workspace,
            name=name,
            index=idx,
            path=workspace.root_path / name,
        )

    def get_environments(
        self, workspace: Workspace, project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        return [self.get_environment(workspace, name) for name in self._env_names]

    def get_environment_status(
        self, env: FeatureEnvironment, project_repos: list[ProjectRepository]
    ) -> FeatureEnvironmentStatus:
        return FeatureEnvironmentStatus(environment=env, feature_branch=None)


class _RecordingRepoRepo:
    """Captures merge_ref / merge_ref_standalone calls and returns canned outcomes."""

    def __init__(
        self,
        worktree_outcomes: dict[tuple[str, str], RepoMergeOutcome],
        standalone_outcomes: dict[str, RepoMergeOutcome] | None = None,
    ) -> None:
        self._worktree_outcomes = worktree_outcomes
        self._standalone_outcomes = standalone_outcomes or {}
        self.merge_calls: list[tuple[str, str, str, MergeMode, bool]] = []
        self.standalone_calls: list[tuple[str, str, MergeMode, bool]] = []

    def merge_ref(
        self,
        worktree: FeatureWorktree,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        key = (worktree.environment.name, worktree.repository.name)
        self.merge_calls.append((*key, source_ref, mode, autostash))
        return self._worktree_outcomes[key]

    def merge_ref_standalone(
        self,
        repo: StandaloneRepository,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        self.standalone_calls.append((repo.name, source_ref, mode, autostash))
        return self._standalone_outcomes[repo.name]


class _RecordingReporter:
    def __init__(self) -> None:
        self.started: str | None = None
        self.completed: bool | None = None
        self.repo_events: list[tuple[str, str, MergeResult, int, int]] = []

    def merge_started(self, source_ref: str) -> None:
        self.started = source_ref

    def merge_completed(self, success: bool) -> None:
        self.completed = success

    def repo_merged(
        self,
        scope_label: str,
        repo_name: str,
        result: MergeResult,
        ahead: int,
        behind: int,
    ) -> None:
        self.repo_events.append((scope_label, repo_name, result, ahead, behind))


def _conforms_recording_reporter(x: _RecordingReporter) -> IMergeReporter:
    return x


def _make_service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    workspace_repo: _FakeWorkspaceRepo,
    repo_repo: _RecordingRepoRepo,
    *,
    repo_factory: Any = None,
) -> WorkspaceMergeService:
    env_status_svc = EnvStatusService(
        worktree_repo=workspace_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
    )
    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    factory = repo_factory if repo_factory is not None else RepositoryFactory(workspace_config)
    return WorkspaceMergeService(
        env_status_svc=env_status_svc,
        worktree_repo=workspace_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=factory,
        workspace=workspace,
        git_ops=git_ops,
    )


@pytest.fixture
def materialize_paths() -> Iterator[Any]:
    """Pin Path.exists to True for the given set; restored after the test."""
    patchers: list[Any] = []

    def materialize(paths: set[Path]) -> None:
        existing = set(paths)
        orig_exists = Path.exists

        def fake_exists(self: Path) -> bool:
            return self in existing or orig_exists(self)

        p = patch.object(Path, "exists", fake_exists)
        p.start()
        patchers.append(p)

    yield materialize
    for p in patchers:
        p.stop()


# --- empty / no-op shapes -----------------------------------------------------


def test_merge_all_returns_empty_report_when_no_worktrees_materialized(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """No worktrees on disk + no standalones → empty report and *no events fire*.

    Mirrors pull_all's empty-target behavior: skip start/complete so the
    stream output isn't misleading ("→ merging X / ✓ merge complete" for
    a no-op) and the NDJSON contract stays consistent with pull.
    """
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(worktree_outcomes={})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)

    reporter = _RecordingReporter()
    report = svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=None,
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=reporter,
    )

    assert report.source_ref == "alpha"
    assert report.envs == []
    assert report.standalone == []
    assert report.success is True
    assert repo_repo.merge_calls == []
    assert reporter.started is None
    assert reporter.completed is None
    assert reporter.repo_events == []


def test_merge_all_empty_patterns_matches_no_project_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """Regression: empty patterns must NOT fan the source ref into every worktree.

    Guards the `winter ws merge alpha` footgun — with project worktrees on
    disk and no target pattern, nothing is merged (the command layer rejects
    the empty pattern; the service stays safe regardless by matching nothing
    rather than defaulting to `*/*`).
    """
    workspace_repo = _FakeWorkspaceRepo(env_names=["alpha", "gamma"])
    repo_repo = _RecordingRepoRepo(worktree_outcomes={})
    for env in ("alpha", "gamma"):
        materialize_paths({workspace.root_path / env / "demo", workspace.root_path / env / "other"})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)

    report = svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=[],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    assert repo_repo.merge_calls == []
    assert report.envs == []
    assert report.success is True


# --- happy path: patterns + standalones --------------------------------------


def test_merge_all_applies_source_ref_uniformly_across_matched_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """Source ref and mode propagate unchanged to every per-repo call."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.fast_forwarded),
            ("gamma", "other"): RepoMergeOutcome("other", MergeResult.up_to_date),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)

    report = svc.merge_all(
        source_ref="origin/master",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.no_ff,
        autostash=True,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    for _env, _repo, source_ref, mode, autostash in repo_repo.merge_calls:
        assert source_ref == "origin/master"
        assert mode == MergeMode.no_ff
        assert autostash is True
    assert report.success is True
    assert [o.repo_name for env in report.envs for o in env.repos] == ["demo", "other"]


def test_merge_all_failure_when_any_repo_diverges(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """A diverged repo flips report.success and the completed event."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.diverged, ahead=2, behind=3),
            ("gamma", "other"): RepoMergeOutcome("other", MergeResult.fast_forwarded),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)
    reporter = _RecordingReporter()

    report = svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=reporter,
    )

    assert report.success is False
    assert reporter.completed is False
    diverged = next(o for env in report.envs for o in env.repos if o.result == MergeResult.diverged)
    assert diverged.ahead == 2
    assert diverged.behind == 3


def test_merge_all_failure_when_source_ref_missing_in_any_repo(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """skipped_missing_ref counts as failure for exit-code purposes."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome(
                "demo",
                MergeResult.skipped_missing_ref,
                error="source ref not found: missing",
            ),
            ("gamma", "other"): RepoMergeOutcome("other", MergeResult.up_to_date),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)

    report = svc.merge_all(
        source_ref="missing",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    assert report.success is False


# --- pattern matching ---------------------------------------------------------


def test_merge_all_filters_worktrees_by_pattern_across_envs(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """`*/demo` selects every env's `demo` worktree and ignores the others."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["alpha", "gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("alpha", "demo"): RepoMergeOutcome("demo", MergeResult.up_to_date),
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.fast_forwarded),
        }
    )
    for env in ("alpha", "gamma"):
        materialize_paths({workspace.root_path / env / "demo", workspace.root_path / env / "other"})
    svc = _make_service(workspace, workspace_config, workspace_repo, repo_repo)

    report = svc.merge_all(
        source_ref="master",
        scope=RepoScope.project,
        patterns=["*/demo"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    assert {(env, repo) for (env, repo, _, _, _) in repo_repo.merge_calls} == {
        ("alpha", "demo"),
        ("gamma", "demo"),
    }
    assert {env.env for env in report.envs} == {"alpha", "gamma"}


# --- pinned scope -------------------------------------------------------------


def _pinned_factory(workspace: Workspace, standalone: list[StandaloneRepository] | None = None) -> Any:
    """RepoFactory stub with one pinned and one unpinned project repo, plus optional standalones."""
    unpinned_demo = ProjectRepository(
        name="demo",
        main_path=workspace.root_path / "projects" / "demo",
        main_branch="main",
        pinned=False,
    )
    pinned_other = ProjectRepository(
        name="other",
        main_path=workspace.root_path / "projects" / "other",
        main_branch="main",
        pinned=True,
    )
    standalones = standalone or []

    class _StubFactory:
        def get_project_repos(self) -> list[ProjectRepository]:
            return [unpinned_demo, pinned_other]

        def get_standalone_repos(self) -> list[StandaloneRepository]:
            return standalones

    return _StubFactory()


def test_merge_all_includes_pinned_by_default(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """Default PinnedScope.include: both pinned and unpinned worktrees are merged."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.fast_forwarded),
            ("gamma", "other"): RepoMergeOutcome("other", MergeResult.up_to_date),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(
        workspace,
        workspace_config,
        workspace_repo,
        repo_repo,
        repo_factory=_pinned_factory(workspace),
    )

    svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    assert {repo for (_env, repo, _, _, _) in repo_repo.merge_calls} == {"demo", "other"}


def test_merge_all_exclude_pinned_drops_pinned_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """`--exclude-pinned`: only non-pinned worktrees are merged."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.fast_forwarded),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(
        workspace,
        workspace_config,
        workspace_repo,
        repo_repo,
        repo_factory=_pinned_factory(workspace),
    )

    svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.exclude,
        reporter=_RecordingReporter(),
    )

    assert {repo for (_env, repo, _, _, _) in repo_repo.merge_calls} == {"demo"}


def test_merge_all_only_pinned_drops_non_pinned_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """`--only-pinned`: only pinned worktrees are merged."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "other"): RepoMergeOutcome("other", MergeResult.fast_forwarded),
        }
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", env_path / "other"})
    svc = _make_service(
        workspace,
        workspace_config,
        workspace_repo,
        repo_repo,
        repo_factory=_pinned_factory(workspace),
    )

    svc.merge_all(
        source_ref="alpha",
        scope=RepoScope.project,
        patterns=["gamma"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.only,
        reporter=_RecordingReporter(),
    )

    assert {repo for (_env, repo, _, _, _) in repo_repo.merge_calls} == {"other"}


# --- standalone scope ---------------------------------------------------------


def test_merge_all_standalone_only_skips_project_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """RepoScope.standalone: project worktrees are not touched; standalones are."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    standalone = StandaloneRepository(name="stand", path=workspace.root_path / "stand")
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={},
        standalone_outcomes={
            "stand": RepoMergeOutcome("stand", MergeResult.fast_forwarded),
        },
    )
    materialize_paths({workspace.root_path / "stand"})
    svc = _make_service(
        workspace,
        workspace_config,
        workspace_repo,
        repo_repo,
        repo_factory=_pinned_factory(workspace, standalone=[standalone]),
    )

    report = svc.merge_all(
        source_ref="master",
        scope=RepoScope.standalone,
        patterns=None,
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.include,
        reporter=_RecordingReporter(),
    )

    assert repo_repo.merge_calls == []
    assert [c[0] for c in repo_repo.standalone_calls] == ["stand"]
    assert [o.repo_name for o in report.standalone] == ["stand"]
    assert report.envs == []


def test_merge_all_all_scope_merges_projects_and_standalones(
    workspace: Workspace, workspace_config: WorkspaceConfig, materialize_paths: Any
) -> None:
    """RepoScope.all: both project worktrees and standalones are merged."""
    workspace_repo = _FakeWorkspaceRepo(env_names=["gamma"])
    standalone = StandaloneRepository(name="stand", path=workspace.root_path / "stand")
    repo_repo = _RecordingRepoRepo(
        worktree_outcomes={
            ("gamma", "demo"): RepoMergeOutcome("demo", MergeResult.up_to_date),
        },
        standalone_outcomes={
            "stand": RepoMergeOutcome("stand", MergeResult.fast_forwarded),
        },
    )
    env_path = workspace.root_path / "gamma"
    materialize_paths({env_path / "demo", workspace.root_path / "stand"})
    svc = _make_service(
        workspace,
        workspace_config,
        workspace_repo,
        repo_repo,
        repo_factory=_pinned_factory(workspace, standalone=[standalone]),
    )

    svc.merge_all(
        source_ref="master",
        scope=RepoScope.all,
        patterns=["gamma/demo"],
        mode=MergeMode.ff_only,
        autostash=False,
        pinned_scope=PinnedScope.exclude,
        reporter=_RecordingReporter(),
    )

    assert [c[1] for c in repo_repo.merge_calls] == ["demo"]
    assert [c[0] for c in repo_repo.standalone_calls] == ["stand"]
