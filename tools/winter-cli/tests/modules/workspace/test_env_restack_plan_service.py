"""Fake-seam tests for `EnvRestackPlanService` — chain shaping, participation,
boundary provenance, and the Decision-6 refusal vocabulary.

`FakeReadRepoRepository` below models the one git fact real winter worktrees
share that a naive per-worktree fake would miss: every worktree of the same
project repo shares one ref namespace (`get_ref_tip`/`fork_point`/
`is_ancestor` all resolve any branch, not just the one the opened worktree
happens to have checked out), gated on whether *that specific worktree's own
directory* is provisioned — the fake's `provisioned` set. That is what makes
"an unprovisioned worktree plans skipped because its env branch resolves
nowhere" a faithful test rather than an artifact of an oversimplified stub.

Boundary and ancestry outcomes that depend on git's own reflog/graph
semantics (whether a fork point survives a rewrite, whether the declared
backwards-chain limit actually reproduces against real git) are exercised
separately, against real git, in
`test_env_restack_plan_service_real_git.py` — this file pins the plan
service's own wiring and refusal logic, which fakes can establish honestly.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.models import (
    BoundarySource,
    FeatureWorktree,
    ProjectRepository,
    RepoStatus,
    RestackLink,
    RestackLinkRepo,
    RestackRefusal,
    Workspace,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, service_prefix="t", main_branch="main")


def _repo(name: str, main_branch: str = "main", pinned: bool = False) -> ProjectRepository:
    return ProjectRepository(
        name=name, main_path=WORKSPACE_ROOT / "projects" / name, main_branch=main_branch, pinned=pinned
    )


class FakeReadRepoRepository:
    """Stub for `IReadRepoRepository` — every read is scoped by
    `(repo_name, ref)`, never `(env_name, repo_name, ref)`, because every
    worktree of one project repo shares its ref namespace. What *is*
    per-worktree is whether the worktree is `provisioned` at all — mirrors
    `ReadRepoRepository`'s own `if not worktree.path.exists(): return
    <empty>` guard on every probe.

    Any `IWriteRepoRepository`-only method raises via `__getattr__`, so a
    service accidentally reaching for a write method surfaces immediately
    rather than silently succeeding against a stub that happens to answer.
    """

    def __init__(self) -> None:
        self.provisioned: set[tuple[str, str]] = set()
        self.ref_tips: dict[tuple[str, str], str] = {}
        self.fork_points: dict[tuple[str, str, str], str] = {}
        self.ancestors: set[tuple[str, str, str]] = set()
        self.rebasing: set[tuple[str, str]] = set()
        self.statuses: dict[tuple[str, str], RepoStatus] = {}
        self.status_calls: list[tuple[str, str]] = []
        self.rebase_check_calls: list[tuple[str, str]] = []
        self.fork_point_calls: list[tuple[str, str, str]] = []

    def _key(self, worktree: FeatureWorktree) -> tuple[str, str]:
        return (worktree.environment.name, worktree.repository.name)

    def get_ref_tip(self, worktree: FeatureWorktree, ref: str) -> str | None:
        if self._key(worktree) not in self.provisioned:
            return None
        # `ref` is `refs/heads/<name>` for a chain element (the real
        # backend-scoped resolution `EnvRestackPlanService` sends), or a
        # bare arbitrary ref for BASE/`--cut`. This flat fake has no
        # branch/tag/remote distinction to speak of — real vs. stale-remote
        # ref resolution is pinned separately, against real git, in
        # `test_env_restack_plan_service_real_git.py` — so it normalizes
        # away the prefix rather than modeling it, keeping every existing
        # `_provision`-registered bare name resolvable unchanged.
        local_name = ref.removeprefix("refs/heads/")
        return self.ref_tips.get((worktree.repository.name, local_name))

    def fork_point(self, worktree: FeatureWorktree, lower_ref: str, upper_ref: str) -> str | None:
        # Both args are chain elements here (the plan service's own
        # `--cut`-bottom-link exception aside, `_fork_point_boundaries` is
        # never reached with an arbitrary BASE/`--cut` ref as `upper_ref`),
        # so both arrive `refs/heads/<name>`-scoped — normalized away the
        # same way `get_ref_tip` above does, for the same reason: real vs.
        # stale/ambiguous ref resolution is pinned separately, against real
        # git, in `test_env_restack_plan_service_real_git.py`.
        key = (worktree.repository.name, lower_ref.removeprefix("refs/heads/"), upper_ref.removeprefix("refs/heads/"))
        self.fork_point_calls.append(key)
        if self._key(worktree) not in self.provisioned:
            return None
        return self.fork_points.get(key)

    def is_ancestor(self, worktree: FeatureWorktree, ancestor_ref: str, ref: str) -> bool:
        if self._key(worktree) not in self.provisioned:
            return False
        return (worktree.repository.name, ancestor_ref, ref) in self.ancestors

    def is_rebase_in_progress(self, worktree: FeatureWorktree) -> bool:
        self.rebase_check_calls.append(self._key(worktree))
        if self._key(worktree) not in self.provisioned:
            return False
        return self._key(worktree) in self.rebasing

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        self.status_calls.append(self._key(worktree))
        if self._key(worktree) not in self.provisioned:
            return RepoStatus(
                name=worktree.repository.name, path=str(worktree.path), main_branch=worktree.repository.main_branch
            )
        return self.statuses.get(
            self._key(worktree),
            RepoStatus(
                name=worktree.repository.name,
                path=str(worktree.path),
                main_branch=worktree.repository.main_branch,
                branch=worktree.environment.name,
            ),
        )

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeReadRepoRepository.{name} called unexpectedly")


@pytest.fixture
def fake() -> FakeReadRepoRepository:
    return FakeReadRepoRepository()


@pytest.fixture
def service(fake: FakeReadRepoRepository) -> EnvRestackPlanService:
    return EnvRestackPlanService(repo_repo=fake)  # type: ignore[arg-type]


def _provision(fake: FakeReadRepoRepository, env: str, repo: str, tip: str) -> None:
    """A healthy worktree: provisioned, `env`'s own branch resolving to `tip`."""
    fake.provisioned.add((env, repo))
    fake.ref_tips[(repo, env)] = tip


def _link_repo(link: RestackLink, repo_name: str) -> RestackLinkRepo:
    return next(r for r in link.repos if r.repo_name == repo_name)


# ── empty chain: the service's own contract, not just the command body's ──


def test_plan_rejects_an_empty_chain_instead_of_raising_indexerror(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """`EnvRestackPlanService` is a public, independently-constructed
    collaborator — the command body's own arity guard (at least one ENV) is
    not the only thing standing between an empty `chain` and this method.
    Without its own precondition, `_missing_ref_refusals` indexes
    `arg_links[-1]` / `reads[-1]` unconditionally and an empty chain
    surfaces as a bare `IndexError` from deep inside a private helper
    instead of a clear contract violation at the boundary that actually
    broke it."""
    with pytest.raises(AssertionError):
        service.plan(workspace, [_repo("demo")], chain=[], base="master")


# ── chain shaping ─────────────────────────────────────────────────────────────


def test_five_env_chain_yields_five_links_ordered_base_ward(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """`restack e5 e4 e3 e2 e1 master` names five links, left-onto-right —
    (e5,e4) (e4,e3) (e3,e2) (e2,e1) (e1,master) — and execution runs
    base-ward first: (e1,master) is `links[0]`, (e5,e4) is `links[-1]`, the
    exact reverse of how the operator typed the chain.
    """
    repo = _repo("demo")
    chain = ["e5", "e4", "e3", "e2", "e1"]
    envs_and_predecessors = [("e5", "e4"), ("e4", "e3"), ("e3", "e2"), ("e2", "e1"), ("e1", "master")]
    for env, predecessor in envs_and_predecessors:
        _provision(fake, env, "demo", f"{env}-tip")
        fake.fork_points[("demo", predecessor, env)] = f"{env}-boundary"
    # The base needs no env worktree of its own — only a resolvable ref,
    # readable through whichever env's worktree the bottom link opens.
    fake.ref_tips[("demo", "master")] = "master-tip"

    plan = service.plan(workspace, [repo], chain=chain, base="master")

    assert plan.refused is False
    assert [(link.env, link.predecessor) for link in plan.links] == list(reversed(envs_and_predecessors))


def test_boundary_and_source_are_frozen_from_pre_run_positions(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    repo = _repo("demo")
    _provision(fake, "env2", "demo", "env2-tip")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    fake.fork_points[("demo", "env1", "env2")] = "env2-boundary"

    plan = service.plan(workspace, [repo], chain=["env2", "env1"], base="master")

    assert plan.refused is False
    bottom, top = plan.links
    assert (bottom.env, bottom.predecessor) == ("env1", "master")
    assert (top.env, top.predecessor) == ("env2", "env1")
    bottom_repo = _link_repo(bottom, "demo")
    top_repo = _link_repo(top, "demo")
    assert (bottom_repo.boundary, bottom_repo.source) == ("env1-boundary", BoundarySource.fork_point)
    assert (top_repo.boundary, top_repo.source) == ("env2-boundary", BoundarySource.fork_point)
    # fork_point is always called with the *pre-run* predecessor name/ref,
    # never a substitute computed from anything execution-time.
    assert ("demo", "master", "env1") in fake.fork_point_calls
    assert ("demo", "env1", "env2") in fake.fork_point_calls


def test_plan_does_not_short_circuit_a_repo_already_at_parity_with_its_predecessor(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """At pre-flight the predecessor's old tip is already an ancestor of the
    env in every healthy stack — a planner that computed an `up-to-date`
    verdict here would no-op the whole stack. The repo must still come back
    fully participating, with its fork-point boundary frozen, not skipped
    and not omitted.
    """
    repo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    # env1's tip actually *is* reachable from master already (a genuinely
    # up-to-date-looking shape) — is_ancestor would answer True here if
    # asked, but nothing about `plan()` should ask.
    fake.ancestors.add(("demo", "master-tip", "env1-tip"))

    plan = service.plan(workspace, [repo], chain=["env1"], base="master")

    assert plan.refused is False
    (link,) = plan.links
    demo = _link_repo(link, "demo")
    assert demo.participates is True
    assert (demo.boundary, demo.source) == ("env1-boundary", BoundarySource.fork_point)


def test_restack_link_repo_carries_no_onto_target_or_up_to_date_field() -> None:
    """Structural pin: neither an `--onto` execution target nor an
    `up-to-date` verdict has a home on the frozen plan shape at all — both
    are resolved only at execution time, against a predecessor's tip *after*
    the link below it has run.
    """
    field_names = {f.name for f in dataclasses.fields(RestackLinkRepo)}
    assert field_names == {"repo_name", "boundary", "source"}
    link_field_names = {f.name for f in dataclasses.fields(RestackLink)}
    assert link_field_names == {"env", "predecessor", "repos"}


# ── participation / skipped ────────────────────────────────────────────────────


def test_partial_stack_plans_skipped_where_a_branch_does_not_resolve(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    demo = _repo("demo")
    other = _repo("other")
    for repo_name in ("demo", "other"):
        fake.ref_tips[(repo_name, "master")] = "master-tip"
    _provision(fake, "env1", "demo", "env1-tip")
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    # "other" never grew an env1 branch in this repo at all.

    plan = service.plan(workspace, [demo, other], chain=["env1"], base="master")

    assert plan.refused is False
    (link,) = plan.links
    assert link.participating_repos == ["demo"]
    assert link.skipped_repos == ["other"]


def test_unprovisioned_worktree_plans_skipped_before_the_detached_head_guard_ever_runs(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """Participation is a ref-resolution question settled before any status
    is read: an unprovisioned worktree's env branch resolves nowhere, so it
    is `skipped`, and the detached-head guard — which would otherwise fire
    on a repo whose checked-out branch differs from its env name — never
    even opens it. Pinned by call-tracking, not by outcome alone: a planner
    that checked status *before* participation would still often produce the
    same `skipped` verdict by coincidence (a fake status default), but it
    would also call `get_worktree_status` for a repo whose worktree was
    never provisioned — which this test catches directly.
    """
    demo = _repo("demo")
    other = _repo("other")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("other", "master")] = "master-tip"
    _provision(fake, "orphan", "other", "orphan-tip")
    fake.fork_points[("other", "master", "orphan")] = "orphan-boundary"
    # "demo" for env "orphan" is never added to `provisioned` — its worktree
    # was never created — and its status, if ever read, would misreport a
    # detached-looking branch (None), which a wrongly-ordered guard would
    # refuse on.
    fake.statuses[("orphan", "demo")] = RepoStatus(name="demo", path="/x", main_branch="main", branch=None)

    plan = service.plan(workspace, [demo, other], chain=["orphan"], base="master")

    assert plan.refused is False
    (link,) = plan.links
    assert link.skipped_repos == ["demo"]
    assert link.participating_repos == ["other"]
    assert ("orphan", "demo") not in fake.status_calls
    assert ("orphan", "demo") not in fake.rebase_check_calls


# ── refused-missing-ref ─────────────────────────────────────────────────────────


def test_refused_missing_ref_when_the_base_resolves_in_no_repo(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """Without this, a mistyped base plans `skipped` in every repo, and since
    `skipped` is not an exit-code input the run would exit 0 having done
    nothing."""
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    # "master" is never registered as a resolvable ref anywhere.

    plan = service.plan(workspace, [demo], chain=["env1"], base="typo-of-master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_missing_ref]
    assert plan.refusals[0].env == "env1"
    assert plan.refusals[0].predecessor == "typo-of-master"
    # `element` names the base value itself — the only thing that
    # distinguishes this refusal from a mistyped chain element's, which
    # would render the identical `env`/`predecessor` pair otherwise.
    assert plan.refusals[0].element == "typo-of-master"
    assert plan.links == []


def test_refused_missing_ref_when_a_chain_element_resolves_in_no_repo(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """A mistyped chain element must refuse exactly once, not twice: its own
    worktree never exists (never `_provision`-ed here), so reading the base
    through that same non-existent worktree also comes back empty — a
    planner that doesn't gate the base-missing check on the element having
    resolved *somewhere* fires both limbs for the one root cause."""
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    # "env1" branch was never created in any repo.

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is True
    assert len(plan.refusals) == 1
    assert plan.refusals[0].result == RestackRefusal.refused_missing_ref
    assert plan.refusals[0].env == "env1"
    # `element` names the chain element itself — the only thing that
    # distinguishes this refusal from a mistyped base's, which would
    # render the identical `env`/`predecessor` pair otherwise.
    assert plan.refusals[0].element == "env1"


def test_refused_missing_ref_when_cut_is_unresolvable_in_a_participating_repo(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """An unresolvable `--cut` refuses the whole run rather than reporting
    that repo `skipped` — `skipped` would leave one repo un-restacked while
    its siblings moved."""
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    # cut ref "landed-elsewhere" never resolves for "demo".

    plan = service.plan(workspace, [demo], chain=["env1"], base="master", cut="landed-elsewhere")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_missing_ref]
    # `element` names `--cut` itself — without it, this refusal renders
    # identically to a mistyped chain element's or base's whenever `repos`
    # happens to line up, sending an operator to the wrong argument.
    assert plan.refusals[0].element == "--cut"
    assert plan.refusals[0].repos == ["demo"]


# ── --cut's three per-repo classes ─────────────────────────────────────────────


def test_cut_classes_are_pinned_separately_across_three_repos(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """The bottom link's three per-repo `--cut` classes, in one run: `applies`
    (cut's tip is an ancestor of the env branch — replays the frozen range),
    `already done` (cut's tip is not an ancestor, but the base's tip is —
    plans with no refusal, `up-to-date` is an execution-time re-derivation
    from this same frozen boundary), and `neither` (refuses
    `refused-cut-not-ancestor`).
    """
    applies_repo = _repo("applies")
    already_done_repo = _repo("already-done")
    neither_repo = _repo("neither")
    repos = [applies_repo, already_done_repo, neither_repo]

    for repo in repos:
        _provision(fake, "env1", repo.name, "env1-tip")
        fake.ref_tips[(repo.name, "master")] = "master-tip"
        fake.ref_tips[(repo.name, "landed-tip")] = "cut-tip"

    fake.ancestors.add(("applies", "cut-tip", "env1-tip"))
    fake.ancestors.add(("already-done", "master-tip", "env1-tip"))
    # "neither" registers no ancestry at all for its own repo name.

    plan = service.plan(workspace, repos, chain=["env1"], base="master", cut="landed-tip")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_cut_not_ancestor]
    assert plan.refusals[0].repos == ["neither"]


def test_cut_applies_and_cut_already_done_both_plan_with_no_refusal(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    applies_repo = _repo("applies")
    already_done_repo = _repo("already-done")
    repos = [applies_repo, already_done_repo]
    for repo in repos:
        _provision(fake, "env1", repo.name, "env1-tip")
        fake.ref_tips[(repo.name, "master")] = "master-tip"
        fake.ref_tips[(repo.name, "landed-tip")] = "cut-tip"
    fake.ancestors.add(("applies", "cut-tip", "env1-tip"))
    fake.ancestors.add(("already-done", "master-tip", "env1-tip"))

    plan = service.plan(workspace, repos, chain=["env1"], base="master", cut="landed-tip")

    assert plan.refused is False
    (link,) = plan.links
    applies = _link_repo(link, "applies")
    already_done = _link_repo(link, "already-done")
    assert (applies.boundary, applies.source) == ("cut-tip", BoundarySource.cut)
    assert (already_done.boundary, already_done.source) == ("cut-tip", BoundarySource.cut)


def test_cut_repeating_a_chain_element_refuses_inverted_order_before_any_repo_is_read(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """`restack env2 master --cut env2` passes the missing-ref and
    cut-ancestry guards outright (a ref is its own ancestor) and would
    otherwise replay nothing while force-moving env2 onto master. The plan
    service refuses it before that — and before opening a single repo, which
    this test pins the same way the sibling chain-repeat test below does:
    `fork_point_calls`/`status_calls` staying empty. (`FakeReadRepoRepository`
    defines `get_ref_tip`, `fork_point`, and `get_worktree_status` itself, so
    its raise-on-any-*undefined*-call `__getattr__` fires only for a
    write-only method a read-only plan service was never going to reach in
    the first place — it constrains nothing about *this* ordering.)
    """
    demo = _repo("demo")

    plan = service.plan(workspace, [demo], chain=["env2"], base="master", cut="env2")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_inverted_order]
    assert plan.refusals[0].env == "env2"
    assert plan.refusals[0].predecessor == "master"
    assert plan.refusals[0].element == "env2"
    assert fake.status_calls == []
    assert fake.fork_point_calls == []


def test_an_element_repeated_within_the_chain_refuses_naming_its_argument_order_link(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """`restack env1 env2 env1 master` names one env at two heights, which no
    stack can satisfy. The refusal is attributed to the first link in argument
    order whose env is the repeated one, so the message points at the higher
    of the two placements — where the operator's typing went wrong — rather
    than at whichever happened to be visited last.
    """
    demo = _repo("demo")

    plan = service.plan(workspace, [demo], chain=["env1", "env2", "env1"], base="master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_inverted_order]
    assert plan.refusals[0].env == "env1"
    assert plan.refusals[0].predecessor == "env2"
    assert plan.refusals[0].element is None
    assert fake.status_calls == []
    assert fake.fork_point_calls == []


# ── refused-dirty: staged/unstaged fire, untracked-only does not ──────────────


@pytest.mark.parametrize(
    "staged_count,unstaged_count",
    [(1, 0), (0, 1)],
    ids=["staged-only", "unstaged-only"],
)
def test_refused_dirty_fires_on_staged_or_unstaged_changes(
    workspace: Workspace,
    service: EnvRestackPlanService,
    fake: FakeReadRepoRepository,
    staged_count: int,
    unstaged_count: int,
) -> None:
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo",
        path="/x",
        main_branch="main",
        branch="env1",
        staged_count=staged_count,
        unstaged_count=unstaged_count,
    )

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_dirty]
    assert plan.refusals[0].repos == ["demo"]


def test_refused_dirty_does_not_fire_on_an_untracked_only_worktree(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """`git rebase` leaves untracked files in place unless it would overwrite
    one — the planner reads `staged_count`/`unstaged_count`, never
    `dirty_files` (which folds untracked paths in), so a worktree whose only
    change is one untracked file must plan clean, not refuse.
    """
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo",
        path="/x",
        main_branch="main",
        branch="env1",
        staged_count=0,
        unstaged_count=0,
        dirty_files=["untracked.txt"],
    )

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is False
    (link,) = plan.links
    assert link.participating_repos == ["demo"]


# ── refused-rebase-in-progress / refused-detached-head ────────────────────────


def test_refused_rebase_in_progress(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    fake.rebasing.add(("env1", "demo"))

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_rebase_in_progress]


def test_refused_detached_head_when_head_is_parked_on_a_different_branch(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    fake.statuses[("env1", "demo")] = RepoStatus(name="demo", path="/x", main_branch="main", branch="some-other-branch")

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_detached_head]


# ── refused-inverted-order (chain-level ancestry) ──────────────────────────────


def test_refused_inverted_order_fires_when_every_repo_carries_commits_and_is_a_proper_ancestor(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    demo = _repo("demo")
    _provision(fake, "env1", "demo", "env1-tip")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.fork_points[("demo", "master", "env1")] = "env1-boundary"
    # env1's tip differs from its boundary (carries commits past it) and is
    # itself a proper ancestor of master's tip — the inverted shape.
    fake.ancestors.add(("demo", "env1-tip", "master-tip"))

    plan = service.plan(workspace, [demo], chain=["env1"], base="master")

    assert plan.refused is True
    assert [r.result for r in plan.refusals] == [RestackRefusal.refused_inverted_order]
    assert plan.refusals[0].repos == ["demo"]


def test_refused_inverted_order_holds_its_fire_when_one_repo_is_exempt(
    workspace: Workspace, service: EnvRestackPlanService, fake: FakeReadRepoRepository
) -> None:
    """A multi-repo workspace commonly has an env carrying commits in one
    repo while sitting exactly at its predecessor's old tip in every other —
    a per-repo ancestry test would refuse that healthy stack everywhere,
    since the refusal is all-or-nothing. The exemption in even one
    participating repo must hold the whole link's fire.

    "exempt" is deliberately built so the *ancestry* half would also fire on
    its own — its boundary (== its own env tip) really is a proper ancestor
    of master's current tip too, matching master having simply advanced
    since the fork. That is what makes this test catch an implementation
    that drops the exemption check specifically: without it, "exempt" would
    satisfy the ancestry loop on its own merits (not by any other early
    return) and the link would wrongly refuse.
    """
    inverted_repo = _repo("inverted")
    exempt_repo = _repo("exempt")
    repos = [inverted_repo, exempt_repo]
    for repo in repos:
        _provision(fake, "env1", repo.name, "env1-tip")
        fake.ref_tips[(repo.name, "master")] = "master-tip"
    fake.fork_points[("inverted", "master", "env1")] = "env1-boundary-inverted"
    fake.ancestors.add(("inverted", "env1-tip", "master-tip"))
    # "exempt": env1's tip *is* its own boundary — zero commits past it —
    # and that boundary is still a proper ancestor of master's (advanced)
    # current tip, so only the exemption check (not the ancestry check, and
    # not an env_tip == predecessor_tip coincidence) holds this repo back.
    fake.fork_points[("exempt", "master", "env1")] = "env1-tip"
    fake.ancestors.add(("exempt", "env1-tip", "master-tip"))

    plan = service.plan(workspace, repos, chain=["env1"], base="master")

    assert plan.refused is False
    (link,) = plan.links
    assert sorted(link.participating_repos) == ["exempt", "inverted"]
