from __future__ import annotations

import concurrent.futures
import dataclasses
import logging

import click

from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.fetch_reporter import IFetchReporter
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.models import (
    EnvSkipped,
    EnvSyncReport,
    FeatureEnvironment,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    FetchReport,
    ProjectRepository,
    PullMode,
    PullReport,
    RepoError,
    RepoFetchOutcome,
    RepoScope,
    RepoSyncOutcome,
    StandaloneRepository,
    SyncResult,
    Workspace,
)
from winter_cli.modules.workspace.pattern_match import matches_any_pattern
from winter_cli.modules.workspace.pull_reporter import IPullReporter
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _PullTarget:
    """Per-worktree integration target resolved up-front for fan-out.

    `target_ref` is the explicit ref a pinned worktree integrates from
    (`origin/<main_branch>`). For a non-pinned worktree it is None — the
    integration ref is that worktree's *own* tracking branch, resolved
    per-worktree at integrate time (a missing upstream becomes a
    `no_upstream` skip). Resolving non-pinned refs lazily keeps the git
    read off worktrees that turn out to be missing on disk, which the
    caller filters out before the integrate stage.
    """

    env_name: str
    worktree: FeatureWorktree
    target_ref: str | None


class WorkspaceSyncService:
    """Network-touching git operations across envs and standalone repos.

    Owns `fetch_all` (fetch every matched source repo and fast-forward its
    local main) and `pull_all` (fetch + integrate worktrees against their
    tracked upstream). Parallelism is bounded by the shared `GitOpsService`
    executor so a wide workspace doesn't overwhelm SSH.
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

    def fetch_all(
        self,
        scope: RepoScope,
        patterns: list[str] | None,
        reporter: IFetchReporter,
    ) -> FetchReport:
        """Fetch unique project repos matched by `patterns`, and/or standalone repos.

        `patterns` filters project worktrees by segment-aware glob over
        `<env>/<repo>` (empty list ⇒ `*/*`); any matching worktree pulls its
        project repo into the fetch set. Worktrees of a project repo share
        the source checkout's `.git`, so a single fetch updates remote refs
        for every env. We run that fetch through `sync_ff_only` against the
        source checkout, which both fetches `origin` (refreshing the shared
        refs every worktree sees) and fast-forwards the source checkout's
        local main — keeping the base that `winter ws init` branches new envs
        off of current. One `[project/<repo>]` event fires per repo.
        Standalone repos are independent clones, fetched per-repo. Events fire
        in completion order.
        """
        patterns = patterns or ["*/*"]
        project_repos = self._repo_factory.get_project_repos()
        envs = self._select_envs(scope, project_repos)
        standalone_repos = self._repo_factory.get_standalone_repos() if scope.includes_standalone else []

        env_worktrees_by_env = self._build_env_worktrees_map(envs, project_repos)
        matched_by_env: dict[str, list[FeatureWorktree]] = {
            env.name: [
                wt
                for wt in env_worktrees_by_env[env.name].worktrees
                if matches_any_pattern(env.name, wt.repository.name, patterns)
            ]
            for env in envs
        }
        matched_envs = [env for env in envs if matched_by_env[env.name]]
        all_worktrees: list[tuple[str, FeatureWorktree]] = [
            (env.name, wt) for env in matched_envs for wt in matched_by_env[env.name]
        ]

        all_worktrees = self._drop_missing_worktrees(all_worktrees)
        standalone_repos = self._drop_missing_standalones(standalone_repos)

        # Pick one representative worktree per unique project repo — its
        # `.repository` carries the source-checkout path we fetch + ff. The
        # source `.git` is shared, so any worktree resolves the same repo.
        # Insertion order is preserved by the dict so output ordering stays
        # stable when fetches complete in deterministic order (rare; see
        # as_completed below).
        repo_reps: dict[str, FeatureWorktree] = {}
        for _, wt in all_worktrees:
            repo_reps.setdefault(wt.repository.name, wt)

        if not repo_reps and not standalone_repos:
            return FetchReport(projects=[], standalone=[])

        reporter.fetch_started()

        project_results: list[RepoFetchOutcome] = []
        standalone_results: list[RepoFetchOutcome] = []

        with self._git_ops.executor() as pool:
            future_keys: dict[concurrent.futures.Future, tuple[str, str]] = {}
            for repo_name, wt in repo_reps.items():
                fut = pool.submit(self._repo_repo.sync_ff_only, wt.repository)
                future_keys[fut] = ("project", repo_name)
            for repo in standalone_repos:
                fut = pool.submit(self._repo_repo.fetch_standalone, repo)
                future_keys[fut] = ("standalone", repo.name)

            for fut in concurrent.futures.as_completed(future_keys):
                scope_label, repo_name = future_keys[fut]
                outcome = self._collect_fetch(fut, repo_name)
                reporter.repo_fetched(scope_label, repo_name, outcome.success, outcome.commits, outcome.error)
                if scope_label == "project":
                    project_results.append(outcome)
                else:
                    standalone_results.append(outcome)

        project_results.sort(key=lambda o: list(repo_reps).index(o.repo_name))
        standalone_results.sort(key=lambda o: o.repo_name)
        report = FetchReport(projects=project_results, standalone=standalone_results)
        reporter.fetch_completed(report.success)
        return report

    def pull_all(
        self,
        scope: RepoScope,
        patterns: list[str] | None,
        mode: PullMode,
        autostash: bool,
        reporter: IPullReporter,
    ) -> PullReport:
        """Fetch + integrate (ff-only / merge / rebase) project worktrees matched
        by `patterns`, and/or standalone repos.

        `patterns` filters project worktrees by segment-aware glob over
        `<env>/<repo>` (empty list ⇒ `*/*`). Pinned worktrees integrate from
        `origin/<main_branch>`; non-pinned worktrees integrate from *their
        own* tracking branch (resolved per worktree), and a non-pinned
        worktree with no upstream is reported as `no_upstream` and skipped;
        standalone repos integrate from their tracked upstream. Per-repo
        events fire on `reporter` as each integrate finishes, in completion
        order.
        """
        patterns = patterns or ["*/*"]
        project_repos = self._repo_factory.get_project_repos()
        envs = self._select_envs(scope, project_repos)
        standalone_repos = self._repo_factory.get_standalone_repos() if scope.includes_standalone else []
        env_worktrees_by_env = self._build_env_worktrees_map(envs, project_repos)

        matched_by_env: dict[str, list[FeatureWorktree]] = {
            env.name: [
                wt
                for wt in env_worktrees_by_env[env.name].worktrees
                if matches_any_pattern(env.name, wt.repository.name, patterns)
            ]
            for env in envs
        }
        matched_envs = [env for env in envs if matched_by_env[env.name]]

        targets, skipped = self._build_pull_targets(matched_envs, matched_by_env)
        targets = [
            t
            for t in targets
            if self._warn_unless_present(t.worktree.path, f"{t.env_name}/{t.worktree.repository.name}", t.env_name)
        ]
        standalone_repos = self._drop_missing_standalones(standalone_repos)

        if not targets and not standalone_repos and not skipped:
            return PullReport(envs=[], standalone=[], skipped=[])

        reporter.pull_started()

        # Surface env-level skips up front so the stream reads as: phase header →
        # skips → per-repo results → summary. The pinned worktrees in a skipped
        # env still flow through the integrate stage below.
        for skip in skipped:
            reporter.env_skipped(skip.env, skip.reason)

        # Group integrate targets by source repo so each project repo gets
        # one shared fetch. Within a group, integrates run serially (they're
        # local-only and fast); across groups they run in parallel up to the
        # pool's slot count. A slow fetch only blocks its own group's slot.
        # Fetch errors are logged but don't abort: stale local refs just
        # produce up-to-date / diverged outcomes from the integrate.
        targets_by_repo: dict[str, list[_PullTarget]] = {}
        for t in targets:
            targets_by_repo.setdefault(t.worktree.repository.name, []).append(t)

        outcomes_by_env: dict[str, list[RepoSyncOutcome]] = {env.name: [] for env in matched_envs}
        standalone_outcomes: list[RepoSyncOutcome] = []

        with self._git_ops.executor() as pool:
            project_futures: dict[concurrent.futures.Future, str] = {}
            standalone_futures: dict[concurrent.futures.Future, str] = {}
            for repo_name, group in targets_by_repo.items():
                fut = pool.submit(
                    self._fetch_then_integrate_group,
                    group,
                    mode,
                    autostash,
                    reporter,
                )
                project_futures[fut] = repo_name
            for repo in standalone_repos:
                fut = pool.submit(
                    self._fetch_then_integrate_standalone,
                    repo,
                    mode,
                    autostash,
                )
                standalone_futures[fut] = repo.name

            for fut in concurrent.futures.as_completed({**project_futures, **standalone_futures}):
                if fut in project_futures:
                    # Group task already emitted per-worktree events itself —
                    # we just collect outcomes for the final report.
                    for env_name_, outcome in fut.result():
                        outcomes_by_env[env_name_].append(outcome)
                else:
                    outcome = fut.result()
                    reporter.repo_synced(
                        "standalone",
                        outcome.repo_name,
                        outcome.sync_result,
                        outcome.commits,
                        outcome.ahead,
                        outcome.behind,
                    )
                    standalone_outcomes.append(outcome)

        env_reports: list[EnvSyncReport] = []
        for env in matched_envs:
            if not outcomes_by_env[env.name]:
                continue
            repo_order = [t.worktree.repository.name for t in targets if t.env_name == env.name]
            env_outcomes = self._sort_outcomes(outcomes_by_env[env.name], repo_order)
            success = all(o.sync_result != SyncResult.diverged for o in env_outcomes)
            env_reports.append(EnvSyncReport(env=env.name, repos=env_outcomes, success=success))

        standalone_outcomes.sort(key=lambda o: o.repo_name)
        report = PullReport(envs=env_reports, standalone=standalone_outcomes, skipped=skipped)
        reporter.pull_completed(report.success)
        return report

    def _build_pull_targets(
        self,
        envs: list[FeatureEnvironment],
        matched_by_env: dict[str, list[FeatureWorktree]],
    ) -> tuple[list[_PullTarget], list[EnvSkipped]]:
        """Resolve per-worktree pull targets.

        Each worktree pulls from its own ref independently — there is no
        env-wide feature branch and no env-level skip. Pinned worktrees
        always pull from `origin/<main_branch>` (pinned repos stay excluded
        from feature-branch pulls). Non-pinned worktrees carry a `None`
        `target_ref`: their integration ref is their own tracking branch,
        resolved per-worktree in `_fetch_then_integrate_group` so the read
        lands on a worktree known to exist (the caller filters out
        worktrees missing on disk before the integrate stage) and so each
        connected worktree pulls from its real upstream regardless of repo
        order or whether any *other* repo is connected. A non-pinned
        worktree with no upstream becomes a `no_upstream` skip at that
        stage.

        The returned skip list is always empty — kept in the signature for
        symmetry with the report's `skipped` field and the standalone path.
        """
        targets: list[_PullTarget] = []
        for env in envs:
            for wt in matched_by_env[env.name]:
                target_ref = f"origin/{wt.repository.main_branch}" if wt.repository.pinned else None
                targets.append(_PullTarget(env_name=env.name, worktree=wt, target_ref=target_ref))
        return targets, []

    def _build_env_worktrees_map(
        self,
        envs: list[FeatureEnvironment],
        project_repos: list[ProjectRepository],
    ) -> dict[str, FeatureEnvironmentWorktrees]:
        return {env.name: self._env_status_svc.get_feature_environment_worktrees(env, project_repos) for env in envs}

    @staticmethod
    def _warn_unless_present(path, label: str, init_target: str | None) -> bool:
        """Return True if `path` exists; otherwise warn the user and return False.

        Surfaces newly-added config entries whose worktrees / clones haven't
        been provisioned yet — happens when a repo is added to
        `.winter/config.toml` but `winter ws init` hasn't been re-run.
        """
        if path.exists():
            return True
        fix = f"winter ws init {init_target}" if init_target else "winter ws init"
        click.echo(f"warning: {label} — missing on disk (run `{fix}`)", err=True)
        return False

    def _drop_missing_worktrees(
        self,
        items: list[tuple[str, FeatureWorktree]],
    ) -> list[tuple[str, FeatureWorktree]]:
        return [
            (env_name, wt)
            for (env_name, wt) in items
            if self._warn_unless_present(wt.path, f"{env_name}/{wt.repository.name}", env_name)
        ]

    def _drop_missing_standalones(
        self,
        repos: list[StandaloneRepository],
    ) -> list[StandaloneRepository]:
        return [r for r in repos if self._warn_unless_present(r.path, f"standalone/{r.name}", None)]

    def _fetch_then_integrate_group(
        self,
        targets: list[_PullTarget],
        mode: PullMode,
        autostash: bool,
        reporter: IPullReporter,
    ) -> list[tuple[str, RepoSyncOutcome]]:
        """Fetch a project repo once, then integrate each of its worktrees.

        Worktrees of a project repo share a `.git`, so a single
        `git fetch origin` from any of them updates remote refs for every
        worktree — we fetch from the first and run integrate sequentially
        for the rest. A non-pinned target carries no explicit ref: we read
        its own tracking branch here (post-fetch, on a worktree known to
        exist) and integrate from that, or emit a `no_upstream` skip when
        it has none — mirroring the standalone path. Per-worktree events
        are emitted on `reporter` from inside this task so the user sees
        them as soon as each integrate lands, even within the same group.
        """
        first_wt = targets[0].worktree
        try:
            self._repo_repo.fetch(first_wt)
        except RepoError as exc:
            logger.warning("Fetch failed for %s: %s", first_wt.repository.name, exc)
        results: list[tuple[str, RepoSyncOutcome]] = []
        for t in targets:
            outcome = self._integrate_target(t, mode, autostash)
            reporter.repo_synced(
                t.env_name,
                outcome.repo_name,
                outcome.sync_result,
                outcome.commits,
                outcome.ahead,
                outcome.behind,
            )
            results.append((t.env_name, outcome))
        return results

    def _integrate_target(self, target: _PullTarget, mode: PullMode, autostash: bool) -> RepoSyncOutcome:
        """Integrate one worktree from its resolved ref.

        Pinned targets carry an explicit `origin/<main_branch>` ref.
        Non-pinned targets resolve their own tracking branch here; a
        worktree with no upstream yields `no_upstream` (parity with
        `integrate_standalone`) instead of being forced onto a foreign ref.
        """
        target_ref = target.target_ref
        if target_ref is None:
            target_ref = self._repo_repo.get_worktree_upstream(target.worktree)
            if target_ref is None:
                return RepoSyncOutcome(
                    repo_name=target.worktree.repository.name,
                    sync_result=SyncResult.no_upstream,
                )
        return self._repo_repo.integrate(target.worktree, target_ref, mode, autostash)

    def _fetch_then_integrate_standalone(
        self,
        repo: StandaloneRepository,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        try:
            self._repo_repo.fetch_standalone(repo)
        except RepoError as exc:
            logger.warning("Fetch failed for standalone %s: %s", repo.name, exc)
        return self._repo_repo.integrate_standalone(repo, mode, autostash)

    @staticmethod
    def _sort_outcomes(outcomes: list[RepoSyncOutcome], repo_order: list[str]) -> list[RepoSyncOutcome]:
        return sorted(outcomes, key=lambda o: repo_order.index(o.repo_name))

    def _select_envs(
        self,
        scope: RepoScope,
        project_repos: list[ProjectRepository],
    ) -> list[FeatureEnvironment]:
        """Resolve envs to operate on based on scope.

        Returns no envs when scope excludes project repos (e.g. --standalone).
        Pattern filtering happens at the worktree level in the caller.
        """
        if not scope.includes_project:
            return []
        return self._worktree_repo.get_environments(self._workspace, project_repos)

    @staticmethod
    def _collect_fetch(fut: concurrent.futures.Future, repo_name: str) -> RepoFetchOutcome:
        try:
            # Project repos run `sync_ff_only`, which returns the ff commit
            # count; standalone repos run `fetch_standalone`, which returns
            # None (no local branch is advanced). Normalize the latter to 0.
            result = fut.result()
            commits = result if isinstance(result, int) else 0
            return RepoFetchOutcome(repo_name=repo_name, success=True, commits=commits)
        except RepoError as exc:
            return RepoFetchOutcome(repo_name=repo_name, success=False, error=str(exc))
