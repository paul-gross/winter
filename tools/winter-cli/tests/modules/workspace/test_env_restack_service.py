"""Fake-seam tests for `EnvRestackService` — base-ward execution ordering,
the execution-time `newbase` read, the cut re-derivation, and stop
semantics on conflict.

`FakeWriteRepoRepository` mirrors `FakeReadRepoRepository`
(`test_env_restack_plan_service.py`): every read/write is scoped by
`(repo_name, ref)`, never `(env_name, repo_name, ref)`, since every worktree
of one project repo shares its ref namespace — this is exactly what makes
"a rebase of the bottom link updates what the top link reads" a faithful
fake rather than an artifact of an oversimplified stub. Plans here are built
directly as `RestackPlan`/`RestackLink`/`RestackLinkRepo` objects rather
than through `EnvRestackPlanService` — the two services are independently
testable by design (Decision 7).

Real-git multi-commit fixtures for conflict-stop and re-run-resume — the
behaviors a fake cannot establish honestly, since they depend on git's own
mid-rebase on-disk state and its replay semantics — live in
`test_env_restack_service_real_git.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.models import (
    BoundarySource,
    FeatureWorktree,
    ProjectRepository,
    RebaseConflict,
    RebaseOntoResult,
    RepoRestackOutcome,
    RestackLink,
    RestackLinkRepo,
    RestackPlan,
    RestackPlanRefusal,
    RestackRefusal,
    RestackResult,
    Workspace,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, service_prefix="t", main_branch="main")


def _repo(name: str) -> ProjectRepository:
    return ProjectRepository(name=name, main_path=WORKSPACE_ROOT / "projects" / name, main_branch="main")


def _link(env: str, predecessor: str, repos: list[RestackLinkRepo]) -> RestackLink:
    return RestackLink(env=env, predecessor=predecessor, repos=repos)


def _link_repo(repo_name: str, boundary: str, source: BoundarySource = BoundarySource.fork_point) -> RestackLinkRepo:
    return RestackLinkRepo(repo_name=repo_name, boundary=boundary, source=source)


class FakeWriteRepoRepository:
    """Stub for `IWriteRepoRepository` — scoped by `(repo_name, ref)`, plus a
    `rebase_effects` table a test pre-loads to say what a clean `rebase_onto`
    call leaves the env branch pointing at, so a later link's `get_ref_tip`
    read genuinely observes the earlier link's mutation rather than a value
    the test hard-coded ahead of time.

    Any `IReadRepoRepository`/`IWriteRepoRepository` method this class
    doesn't define raises via `__getattr__`, so a service call this class
    doesn't expect to receive surfaces immediately.
    """

    def __init__(self) -> None:
        self.ref_tips: dict[tuple[str, str], str] = {}
        self.ancestors: set[tuple[str, str, str]] = set()
        self.conflicts: dict[tuple[str, str], RebaseConflict] = {}
        self.rebase_effects: dict[tuple[str, str], str] = {}
        self.rebase_calls: list[tuple[str, str, str, str, str]] = []

    def get_ref_tip(self, worktree: FeatureWorktree, ref: str) -> str | None:
        # `ref` is `refs/heads/<name>` for the env branch and a non-bottom
        # link's predecessor (the real, backend-scoped resolution
        # `EnvRestackService` sends via `chain_element_ref` — see
        # `FakeReadRepoRepository.get_ref_tip` in
        # `test_env_restack_plan_service.py`, the same normalization), or a
        # bare arbitrary ref for the bottom link's BASE-derived predecessor.
        # This flat fake has no branch/tag distinction to speak of — that is
        # pinned separately, against real git, in
        # `test_env_restack_service_real_git.py` — so it strips the prefix
        # rather than modeling it, keeping every `ref_tips`/`rebase_effects`
        # bare-name key resolvable unchanged.
        return self.ref_tips.get((worktree.repository.name, ref.removeprefix("refs/heads/")))

    def is_ancestor(self, worktree: FeatureWorktree, ancestor_ref: str, ref: str) -> bool:
        return (worktree.repository.name, ancestor_ref, ref) in self.ancestors

    def rebase_onto(self, worktree: FeatureWorktree, newbase: str, oldbase: str, branch: str) -> RebaseOntoResult:
        env_name = worktree.environment.name
        repo_name = worktree.repository.name
        self.rebase_calls.append((env_name, repo_name, newbase, oldbase, branch))
        conflict = self.conflicts.get((env_name, repo_name))
        if conflict is not None:
            return RebaseOntoResult(conflict=conflict)
        new_tip = self.rebase_effects.get((env_name, repo_name))
        if new_tip is not None:
            self.ref_tips[(repo_name, branch)] = new_tip
        return RebaseOntoResult()

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


@pytest.fixture
def fake() -> FakeWriteRepoRepository:
    return FakeWriteRepoRepository()


@pytest.fixture
def service(fake: FakeWriteRepoRepository) -> EnvRestackService:
    return EnvRestackService(repo_repo=fake)  # type: ignore[arg-type]


# ── base-ward ordering and the execution-time newbase read ────────────────────


def test_bottom_link_executes_before_top_link(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """`plan.links` is already ordered base-ward first (index 0 is the
    bottom link) — this test pins that `execute` walks it front to back
    rather than, say, reversing it back into argument order. Both links are
    made to actually need a rebase (not `up_to_date`), so a real
    `rebase_onto` call happens for each and the order is directly observable
    through the fake's call log.
    """
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ref_tips[("demo", "env2")] = "env2-tip"
    # Neither link looks up-to-date: no ancestor facts registered at all.

    bottom = _link("env1", "master", [_link_repo("demo", "env1-boundary")])
    top = _link("env2", "env1", [_link_repo("demo", "env2-boundary")])
    plan = RestackPlan(links=[bottom, top])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    assert [call[0] for call in fake.rebase_calls] == ["env1", "env2"]


def test_newbase_is_read_after_the_link_below_has_run(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """The heart of this phase: `env2`'s `--onto` target must be `env1`'s
    tip *after* the bottom link rebased it, not the pre-run tip `env1`
    started with. An implementation that captures `newbase` for every link
    up front (before executing any of them) would pass `env1-old-tip` here
    instead — this test fails on that exact mutation.
    """
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-old-tip"
    fake.ref_tips[("demo", "env2")] = "env2-tip"
    # Executing the bottom link rebases env1's branch onto master-tip,
    # landing it at a new tip only observable *after* rebase_onto ran.
    fake.rebase_effects[("env1", "demo")] = "env1-new-tip"

    bottom = _link("env1", "master", [_link_repo("demo", "env1-boundary")])
    top = _link("env2", "env1", [_link_repo("demo", "env2-boundary")])
    plan = RestackPlan(links=[bottom, top])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    top_call = next(call for call in fake.rebase_calls if call[0] == "env2")
    _env, _repo_name, newbase, oldbase, branch = top_call
    assert newbase == "env1-new-tip"
    assert oldbase == "env2-boundary"
    assert branch == "env2"


def test_up_to_date_predecessor_tip_skips_the_rebase_call(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """When the predecessor's execution-time tip is already an ancestor of
    the env branch, the repo reports `up_to_date` and `rebase_onto` is never
    called at all — the no-op half of the outcome table."""
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ancestors.add(("demo", "master-tip", "env1-tip"))

    plan = RestackPlan(links=[_link("env1", "master", [_link_repo("demo", "env1-boundary")])])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env1", repo_name="demo", result=RestackResult.up_to_date)]
    assert fake.rebase_calls == []


def test_rebased_outcome_when_a_clean_rebase_actually_moves_the_branch(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"

    plan = RestackPlan(links=[_link("env1", "master", [_link_repo("demo", "env1-boundary")])])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env1", repo_name="demo", result=RestackResult.rebased)]
    assert fake.rebase_calls == [("env1", "demo", "master-tip", "env1-boundary", "env1")]


def test_skipped_repo_is_never_read_or_rebased(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """A non-participating repo (`boundary=None`, frozen by the planner) is
    never touched by execution at all — no `get_ref_tip`, no `rebase_onto`."""
    demo = _repo("demo")
    other = _repo("other")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ancestors.add(("demo", "master-tip", "env1-tip"))
    # "other" is a repo in the workspace but never participated in this link.

    plan = RestackPlan(
        links=[_link("env1", "master", [_link_repo("demo", "env1-boundary"), RestackLinkRepo(repo_name="other")])]
    )

    report = service.execute(workspace, [demo, other], plan)

    assert report.success is True
    assert [o.repo_name for o in report.completed] == ["demo"]
    assert fake.rebase_calls == []


# ── the cut re-derivation and its exemption from the up-to-date shortcut ──────


def test_cut_applies_replays_even_when_the_base_tip_is_already_an_ancestor(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """Decision 5's exemption: a `ws merge <base> <env>` shape makes the
    base's tip already an ancestor of the env branch — the plain
    `up_to_date` test would wrongly no-op here — but the frozen boundary
    (the cut ref) is *still* an ancestor of the env branch too (the excluded
    commits haven't actually been dropped yet), so this repo must still
    reach `rebase_onto` rather than reporting `up_to_date`.

    A wrong implementation that keys the exemption on
    `link_repo.source == BoundarySource.cut` alone (always bypassing
    `up_to_date` for any cut-sourced link, without checking whether the
    boundary is still reachable) would also pass this test — it is paired
    with `test_cut_already_done_reports_up_to_date_without_moving` below,
    which only a correct re-derivation passes.
    """
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    # The base's tip is already an ancestor of env1 (the ws-merge shape)...
    fake.ancestors.add(("demo", "master-tip", "env1-tip"))
    # ...but the cut boundary itself is *also* still an ancestor — the
    # excluded commits are still present, so this must not no-op.
    fake.ancestors.add(("demo", "cut-tip", "env1-tip"))

    plan = RestackPlan(links=[_link("env1", "master", [_link_repo("demo", "cut-tip", source=BoundarySource.cut)])])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env1", repo_name="demo", result=RestackResult.rebased)]
    assert fake.rebase_calls == [("env1", "demo", "master-tip", "cut-tip", "env1")]


def test_cut_already_done_reports_up_to_date_without_moving(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """The other half of the same re-derivation: the cut boundary is *not*
    an ancestor of the env branch (already dropped), so this repo must
    report `up_to_date` and never call `rebase_onto` — even though the
    boundary's `BoundarySource` is `cut`, same as the case above. Only the
    fresh ancestry re-derivation, not the source tag, tells the two apart.
    """
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ancestors.add(("demo", "master-tip", "env1-tip"))
    # cut-tip is registered as resolvable but *not* an ancestor of env1-tip.

    plan = RestackPlan(links=[_link("env1", "master", [_link_repo("demo", "cut-tip", source=BoundarySource.cut)])])

    report = service.execute(workspace, [demo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env1", repo_name="demo", result=RestackResult.up_to_date)]
    assert fake.rebase_calls == []


# ── conflict stop semantics ─────────────────────────────────────────────────────


def test_conflict_on_the_bottom_link_leaves_the_top_link_entirely_untouched(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """A conflict stops the run immediately — no `get_ref_tip`/`rebase_onto`
    call for any repo of any upper link. Pinned via the fake's call log:
    were the top link touched at all, its `rebase_onto` call would appear
    in `fake.rebase_calls`, which this test asserts has exactly one entry.
    """
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ref_tips[("demo", "env2")] = "env2-tip"
    fake.conflicts[("env1", "demo")] = RebaseConflict(replayed_commit="deadbeef", conflicted_paths=["f.txt"])

    bottom = _link("env1", "master", [_link_repo("demo", "env1-boundary")])
    top = _link("env2", "env1", [_link_repo("demo", "env2-boundary")])
    plan = RestackPlan(links=[bottom, top])

    report = service.execute(workspace, [demo], plan)

    assert report.success is False
    assert report.completed == []
    assert report.conflict is not None
    assert report.conflict.env == "env1"
    assert report.conflict.repo_name == "demo"
    assert report.conflict.replayed_commit == "deadbeef"
    assert report.conflict.conflicted_paths == ["f.txt"]
    assert report.remaining.links == [bottom, top]
    assert fake.rebase_calls == [("env1", "demo", "master-tip", "env1-boundary", "env1")]


def test_conflict_in_one_repo_leaves_a_sibling_repo_of_the_same_link_untouched(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """Decision 4: a conflict stops the run per repo, not per link — a
    sibling repo of the *same* conflicting link, ordered after it, is never
    reached either.
    """
    first = _repo("first")
    second = _repo("second")
    for repo_name in ("first", "second"):
        fake.ref_tips[(repo_name, "master")] = "master-tip"
        fake.ref_tips[(repo_name, "env1")] = "env1-tip"
    fake.conflicts[("env1", "first")] = RebaseConflict(replayed_commit="deadbeef", conflicted_paths=["f.txt"])

    link = _link("env1", "master", [_link_repo("first", "boundary"), _link_repo("second", "boundary")])
    plan = RestackPlan(links=[link])

    report = service.execute(workspace, [first, second], plan)

    assert report.success is False
    assert report.completed == []
    assert report.conflict is not None
    assert report.conflict.repo_name == "first"
    assert [call[1] for call in fake.rebase_calls] == ["first"]
    assert report.remaining.links == [link]


def test_completed_repos_before_the_conflict_are_still_reported(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """A repo that finished cleanly before the one that conflicted (in an
    earlier link, here) is still reported in `completed` — the run stopped,
    it didn't roll back."""
    demo = _repo("demo")
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    fake.ref_tips[("demo", "env2")] = "env2-tip"
    fake.rebase_effects[("env1", "demo")] = "env1-new-tip"
    fake.conflicts[("env2", "demo")] = RebaseConflict(replayed_commit="c0ffee", conflicted_paths=["g.txt"])

    bottom = _link("env1", "master", [_link_repo("demo", "env1-boundary")])
    top = _link("env2", "env1", [_link_repo("demo", "env2-boundary")])
    plan = RestackPlan(links=[bottom, top])

    report = service.execute(workspace, [demo], plan)

    assert report.success is False
    assert report.completed == [RepoRestackOutcome(env="env1", repo_name="demo", result=RestackResult.rebased)]
    assert report.conflict is not None
    assert report.conflict.env == "env2"
    assert report.remaining.links == [top]


# ── refuses to execute an already-refused plan ──────────────────────────────


def test_execute_rejects_a_refused_plan_before_touching_the_repo_seam(
    workspace: Workspace, service: EnvRestackService, fake: FakeWriteRepoRepository
) -> None:
    """`RestackPlan`'s own docstring declares `links` empty whenever
    `refusals` is non-empty, but the dataclass itself permits both to be set
    at once — the same reasoning `EnvRestackPlanService.plan` already
    applies to its own non-empty-chain precondition (`plan` is a public,
    independently-constructed collaborator, not a detail private to
    whichever caller happens to check `refused` first). Only the handler
    enforced this before; `execute` must refuse it itself too, and refuse it
    before ever reaching the (write-capable) repo seam — pinned here by a
    plan carrying a link `FakeWriteRepoRepository` would otherwise happily
    execute.
    """
    demo = _repo("demo")
    # Populated exactly as a legitimate, unrefused single-link run would be
    # (mirrors `test_rebased_outcome_when_a_clean_rebase_actually_moves_the_branch`
    # below): without the precondition, `execute` would otherwise complete
    # this successfully, which is what makes `pytest.raises` below pin the
    # precondition itself rather than some unrelated `AssertionError` a
    # half-populated fake would raise for its own reasons.
    fake.ref_tips[("demo", "master")] = "master-tip"
    fake.ref_tips[("demo", "env1")] = "env1-tip"
    refusal = RestackPlanRefusal(RestackRefusal.refused_dirty, "env1", "master", repos=["demo"])
    link = _link("env1", "master", [_link_repo("demo", "env1-boundary")])
    plan = RestackPlan(links=[link], refusals=[refusal])

    with pytest.raises(AssertionError):
        service.execute(workspace, [demo], plan)

    assert fake.rebase_calls == []
