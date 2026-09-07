from __future__ import annotations

from winter_cli.modules.workspace.models import (
    BoundarySource,
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    RepoError,
    RepoRestackOutcome,
    RestackConflict,
    RestackFailure,
    RestackLink,
    RestackLinkRepo,
    RestackPlan,
    RestackReport,
    RestackResult,
    Workspace,
)
from winter_cli.modules.workspace.ref_resolver import chain_element_ref, resolve_ref
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository


class EnvRestackService:
    """Executes an already-validated `RestackPlan` base-ward first — bottom
    link to top, repo by repo — stopping at the first conflict.

    `plan.links` is already ordered for base-ward execution by
    `EnvRestackPlanService.plan`, so this class only ever walks it front to
    back. Neither the `--onto` target nor the `up-to-date` verdict is
    frozen on the plan (Decision 5): both are resolved here, fresh, against
    the predecessor's *execution-time* tip — the whole reason execution is
    its own service rather than a method on the planner. Write-capable —
    takes `IWriteRepoRepository`, the write half of the seam the planner
    never touches, so "refuse before any mutation" stays a property of the
    planner's constructor rather than something this class has to reason
    about.
    """

    def __init__(self, repo_repo: IWriteRepoRepository) -> None:
        self._repo_repo = repo_repo

    def execute(
        self,
        workspace: Workspace,
        project_repos: list[ProjectRepository],
        plan: RestackPlan,
    ) -> RestackReport:
        """Run every link in `plan.links`, index 0 first.

        `plan.links[0]`'s predecessor is always the trailing base — an
        arbitrary ref, possibly carrying a `{main}`-token — resolved per repo
        before its tip is read. Every other link's predecessor is a chain
        element, scoped through `chain_element_ref` the same way
        `EnvRestackPlanService._gather_reads` scopes it at plan time — never
        a bare name, which a same-named tag could satisfy instead of the
        local branch the predecessor actually is. Both are read from the
        *env's own* worktree: worktrees of one project repo share a ref
        namespace, so the predecessor resolves there too.

        A conflict in any repo of any link stops the run immediately —
        no further repo of that same link, and no upper link, is touched.
        The returned report carries every repo that finished before the
        stop, the conflict detail, and the unfinished tail of `plan.links`
        (the conflicting link itself onward, since not every one of its
        repos completed). A non-conflict git failure out of the repo seam
        (`RepoError` — `rebase_onto` raises it whenever the on-disk state it
        left behind isn't actually a conflict-stop, e.g. `refused-dirty`'s
        deliberate untracked-file gap) stops the run the same way, reported
        through `failure` instead of `conflict` — caught here rather than
        left to unwind past every already-completed outcome, discarding the
        record of repos this same run already rewrote.

        `plan.refused` is this method's own precondition, mirroring
        `EnvRestackPlanService.plan`'s non-empty-chain assertion: `plan` is a
        public, independently-constructed collaborator, not a detail private
        to `RestackHandler`, the only caller that happens to check `refused`
        today.
        """
        assert not plan.refused, "EnvRestackService.execute requires an unrefused plan"
        repos_by_name = {repo.name: repo for repo in project_repos}
        completed: list[RepoRestackOutcome] = []

        for link_index, link in enumerate(plan.links):
            is_bottom = link_index == 0
            environment = self._environment(workspace, link.env)
            for link_repo in link.repos:
                if not link_repo.participates:
                    continue
                repo = repos_by_name[link_repo.repo_name]
                worktree = FeatureWorktree(workspace=workspace, environment=environment, repository=repo)
                predecessor_ref = (
                    resolve_ref(link.predecessor, repo) if is_bottom else chain_element_ref(link.predecessor)
                )

                try:
                    outcome = self._execute_repo(worktree, link, link_repo, predecessor_ref)
                except RepoError as exc:
                    return RestackReport(
                        completed=completed,
                        failure=RestackFailure(env=link.env, repo_name=link_repo.repo_name, message=str(exc)),
                        remaining=RestackPlan(links=plan.links[link_index:]),
                    )
                if isinstance(outcome, RestackConflict):
                    return RestackReport(
                        completed=completed,
                        conflict=outcome,
                        remaining=RestackPlan(links=plan.links[link_index:]),
                    )
                completed.append(outcome)

        return RestackReport(completed=completed)

    def _execute_repo(
        self,
        worktree: FeatureWorktree,
        link: RestackLink,
        link_repo: RestackLinkRepo,
        predecessor_ref: str,
    ) -> RepoRestackOutcome | RestackConflict:
        """Classify and, unless already up to date, replay one participating
        repo of one link — Decision 5's execution-time test, plus its
        cut-only exemption.

        `oldbase` is the frozen boundary; `newbase` is the predecessor's
        tip read *now*, after every link below this one has already run.
        The three-class cut recognition — `cut applies` vs. `cut already
        done` — is re-derived here from the frozen boundary sha alone,
        never from `link_repo.source == BoundarySource.cut` by itself: that
        field only gates *whether* the re-derivation runs at all (only a
        cut-sourced link can have it diverge from the plain `up_to_date`
        test), the ancestry check below is what actually decides the
        outcome. A repo classed `cut applies` — its frozen boundary is
        still an ancestor of the env branch, meaning the excluded range
        hasn't been dropped yet — is exempt from the `up_to_date` shortcut
        even when the predecessor's tip already is an ancestor (the `ws
        merge <base> <env>` shape Decision 4 names), so it always reaches
        `rebase_onto` instead of reporting a stale `up_to_date`.

        `env_tip` is read as `chain_element_ref(link.env)` — never a bare
        name — same reasoning as `EnvRestackPlanService._gather_reads`: a
        same-named tag would otherwise win the lookup ahead of the local
        branch `git rebase --onto`'s own `branch` argument (below) actually
        operates on, deciding `up_to_date`/`cut_exempt` against a commit
        that isn't the branch at all. `rebase_onto`'s own `branch` argument
        stays the bare `link.env`, deliberately not scoped the same way:
        passing it a fully-qualified ref leaves HEAD detached after the
        rebase instead of attached to the branch (measured against git
        2.43), where the bare name already resolves to the local branch
        over a same-named tag through git's own checkout disambiguation.
        """
        env_tip = self._repo_repo.get_ref_tip(worktree, chain_element_ref(link.env))
        newbase = self._repo_repo.get_ref_tip(worktree, predecessor_ref)
        oldbase = link_repo.boundary
        assert env_tip is not None, "participating repo's env branch must resolve at execution time"
        assert newbase is not None, "participating repo's predecessor must resolve at execution time"
        assert oldbase is not None, "participating repo carries a frozen boundary by construction"

        cut_exempt = link_repo.source == BoundarySource.cut and self._repo_repo.is_ancestor(worktree, oldbase, env_tip)
        up_to_date = not cut_exempt and self._repo_repo.is_ancestor(worktree, newbase, env_tip)
        if up_to_date:
            return RepoRestackOutcome(env=link.env, repo_name=link_repo.repo_name, result=RestackResult.up_to_date)

        rebase_result = self._repo_repo.rebase_onto(worktree, newbase, oldbase, link.env)
        if rebase_result.conflict is not None:
            return RestackConflict(
                env=link.env,
                predecessor=link.predecessor,
                repo_name=link_repo.repo_name,
                replayed_commit=rebase_result.conflict.replayed_commit,
                conflicted_paths=rebase_result.conflict.conflicted_paths,
            )
        return RepoRestackOutcome(env=link.env, repo_name=link_repo.repo_name, result=RestackResult.rebased)

    def _environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        # Index is unused by restack — only `path` (fed to every
        # `FeatureWorktree` above) and `name` matter here, mirroring
        # `EnvRestackPlanService._environment`.
        return FeatureEnvironment(workspace=workspace, name=name, index=0, path=workspace.root_path / name)
