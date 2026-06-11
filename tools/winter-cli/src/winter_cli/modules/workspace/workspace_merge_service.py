from __future__ import annotations

import concurrent.futures
import dataclasses
import logging

from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.merge_reporter import IMergeReporter
from winter_cli.modules.workspace.models import (
    EnvMergeReport,
    FeatureEnvironment,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    MergeMode,
    MergeReport,
    PinnedScope,
    ProjectRepository,
    RepoMergeOutcome,
    RepoScope,
    StandaloneRepository,
    Workspace,
)
from winter_cli.modules.workspace.pattern_match import matches_any_pattern
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _MergeTarget:
    """Per-worktree merge target resolved up-front for fan-out."""

    env_name: str
    worktree: FeatureWorktree


class WorkspaceMergeService:
    """Merges an arbitrary source ref into pattern-matched project worktrees and standalones.

    Sibling of `WorkspacePullService` / `WorkspacePushService`: same
    pattern-matching, same scope flags (`--standalone` / `--all`), same
    pinned-scope flags, same parallelism cap, same NDJSON-shaped events.
    The one shape difference is the explicit `source_ref` arg — unlike
    pull (which integrates the tracked upstream), merge takes the ref to
    integrate as a parameter so callers can fold one env into another or
    pull `master` in without the implicit fetch + ff-first semantics of
    `sync`. No fetch happens here — callers run `winter ws fetch` first
    if they need fresh refs.
    """

    def __init__(
        self,
        env_status_svc: EnvStatusService,
        worktree_repo: IReadWorkspaceRepository,
        repo_repo: IWriteRepoRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        git_ops: GitOpsService,
    ) -> None:
        self._env_status_svc = env_status_svc
        self._worktree_repo = worktree_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._git_ops = git_ops

    def merge_all(
        self,
        source_ref: str,
        scope: RepoScope,
        patterns: list[str] | None,
        mode: MergeMode,
        autostash: bool,
        pinned_scope: PinnedScope,
        reporter: IMergeReporter,
    ) -> MergeReport:
        """Merge `source_ref` into worktrees matched by `patterns`, and/or standalone repos.

        `patterns` filters project worktrees by segment-aware glob over
        `<env>/<repo>`. An empty list matches *no* project worktrees —
        there is no implicit "all worktrees" fan-out; callers wanting every
        env's every worktree pass `*/*` explicitly (the `ws merge` command
        rejects an empty pattern up front). `pinned_scope` controls
        whether pinned project worktrees are included (default), excluded,
        or operated on alone. Standalone repos (when in scope) are
        included regardless of `pinned_scope` — they don't carry the pin
        flag. Per-repo events fire on `reporter` as each merge finishes,
        in completion order.
        """
        patterns = patterns or []
        project_repos = self._repo_factory.get_project_repos()
        envs = self._select_envs(scope, project_repos)
        standalone_repos = self._repo_factory.get_standalone_repos() if scope.includes_standalone else []
        env_worktrees_by_env = self._build_env_worktrees_map(envs, project_repos)

        matched_by_env: dict[str, list[FeatureWorktree]] = {
            env.name: [
                wt
                for wt in env_worktrees_by_env[env.name].worktrees
                if self._matches_pinned_scope(wt, pinned_scope)
                and matches_any_pattern(env.name, wt.repository.name, patterns)
            ]
            for env in envs
        }
        matched_envs = [env for env in envs if matched_by_env[env.name]]

        targets = self._build_merge_targets(matched_envs, matched_by_env)
        targets = [
            t
            for t in targets
            if self._warn_unless_present(
                t.worktree.path,
                f"{t.env_name}/{t.worktree.repository.name}",
                t.env_name,
            )
        ]
        standalone_repos = self._drop_missing_standalones(standalone_repos)

        if not targets and not standalone_repos:
            # Mirror pull_all — return without firing start/complete so the
            # stream and NDJSON contracts both stay clean for the empty case.
            # The handler renders "Nothing to merge" in stream mode.
            return MergeReport(source_ref=source_ref, envs=[], standalone=[])

        reporter.merge_started(source_ref)

        outcomes_by_env: dict[str, list[RepoMergeOutcome]] = {env.name: [] for env in matched_envs}
        standalone_outcomes: list[RepoMergeOutcome] = []

        # Group project targets by repo so each project repo's worktrees
        # run serially within a group (they share .git) while groups run
        # in parallel up to the pool's slot count. Standalones fan out
        # independently.
        targets_by_repo: dict[str, list[_MergeTarget]] = {}
        for t in targets:
            targets_by_repo.setdefault(t.worktree.repository.name, []).append(t)

        with self._git_ops.executor() as pool:
            project_futures: dict[concurrent.futures.Future, str] = {}
            standalone_futures: dict[concurrent.futures.Future, str] = {}
            for repo_name, group in targets_by_repo.items():
                fut = pool.submit(self._merge_group, group, source_ref, mode, autostash, reporter)
                project_futures[fut] = repo_name
            for repo in standalone_repos:
                fut = pool.submit(self._merge_standalone, repo, source_ref, mode, autostash)
                standalone_futures[fut] = repo.name

            for fut in concurrent.futures.as_completed({**project_futures, **standalone_futures}):
                if fut in project_futures:
                    for env_name_, outcome in fut.result():
                        outcomes_by_env[env_name_].append(outcome)
                else:
                    outcome = fut.result()
                    reporter.repo_merged(
                        "standalone",
                        outcome.repo_name,
                        outcome.result,
                        outcome.ahead,
                        outcome.behind,
                    )
                    standalone_outcomes.append(outcome)

        env_reports: list[EnvMergeReport] = []
        for env in matched_envs:
            if not outcomes_by_env[env.name]:
                continue
            repo_order = [t.worktree.repository.name for t in targets if t.env_name == env.name]
            env_outcomes = sorted(outcomes_by_env[env.name], key=lambda o: repo_order.index(o.repo_name))
            env_reports.append(EnvMergeReport(env=env.name, repos=env_outcomes))

        standalone_outcomes.sort(key=lambda o: o.repo_name)
        report = MergeReport(source_ref=source_ref, envs=env_reports, standalone=standalone_outcomes)
        reporter.merge_completed(report.success)
        return report

    def _build_merge_targets(
        self,
        envs: list[FeatureEnvironment],
        matched_by_env: dict[str, list[FeatureWorktree]],
    ) -> list[_MergeTarget]:
        return [_MergeTarget(env_name=env.name, worktree=wt) for env in envs for wt in matched_by_env[env.name]]

    def _build_env_worktrees_map(
        self,
        envs: list[FeatureEnvironment],
        project_repos: list[ProjectRepository],
    ) -> dict[str, FeatureEnvironmentWorktrees]:
        return {env.name: self._env_status_svc.get_feature_environment_worktrees(env, project_repos) for env in envs}

    def _merge_group(
        self,
        targets: list[_MergeTarget],
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
        reporter: IMergeReporter,
    ) -> list[tuple[str, RepoMergeOutcome]]:
        """Merge each worktree in a per-repo group serially, emit events as we go.

        Worktrees of one project repo share `.git`, so we serialize merges
        within a group to avoid concurrent index writes. Across groups,
        the executor parallelizes.
        """
        results: list[tuple[str, RepoMergeOutcome]] = []
        for t in targets:
            outcome = self._repo_repo.merge_ref(t.worktree, source_ref, mode, autostash)
            reporter.repo_merged(
                t.env_name,
                outcome.repo_name,
                outcome.result,
                outcome.ahead,
                outcome.behind,
            )
            results.append((t.env_name, outcome))
        return results

    def _merge_standalone(
        self,
        repo: StandaloneRepository,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        return self._repo_repo.merge_ref_standalone(repo, source_ref, mode, autostash)

    @staticmethod
    def _matches_pinned_scope(wt: FeatureWorktree, pinned_scope: PinnedScope) -> bool:
        if wt.repository.pinned:
            return pinned_scope.matches_pinned
        return pinned_scope.matches_non_pinned

    def _select_envs(
        self,
        scope: RepoScope,
        project_repos: list[ProjectRepository],
    ) -> list[FeatureEnvironment]:
        if not scope.includes_project:
            return []
        return self._worktree_repo.get_environments(self._workspace, project_repos)

    @staticmethod
    def _warn_unless_present(path, label: str, init_target: str | None) -> bool:
        """Same shape as `WorkspaceSyncService._warn_unless_present` (which
        still uses `click.echo` — pre-existing). Service-layer user-facing
        warnings should go through the logger per
        `winter-harness:/python/logging.md`; sync's call site is debt to
        clean up separately."""
        if path.exists():
            return True
        fix = f"winter ws init {init_target}" if init_target else "winter ws init"
        logger.warning("%s — missing on disk (run `%s`)", label, fix)
        return False

    def _drop_missing_standalones(self, repos: list[StandaloneRepository]) -> list[StandaloneRepository]:
        return [r for r in repos if self._warn_unless_present(r.path, f"standalone/{r.name}", None)]
