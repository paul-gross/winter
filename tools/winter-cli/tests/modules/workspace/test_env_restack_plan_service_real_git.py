"""Real-git tests for `EnvRestackPlanService` — the boundary and ancestry
outcomes a fake cannot establish honestly, driven through the real
`ReadRepoRepository` against actual worktrees in `tmp_path`.

Every env here is a real `git worktree add` linked worktree of one shared
`.git`, matching the shape `winter ws init` leaves behind — so a fork-point
lookup issued from one env's worktree resolving another env's branch is the
real git ref-sharing behavior, not a fake's approximation of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.modules.workspace.conftest import add_env_worktree, commit, git_cmd, init_project, init_repo
from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.internal.read_repo_repository import ReadRepoRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import BoundarySource, ProjectRepository, Workspace


@pytest.fixture
def service() -> EnvRestackPlanService:
    return EnvRestackPlanService(repo_repo=ReadRepoRepository(RepoErrorFactory()))


def test_plan_freezes_the_boundary_from_the_predecessors_reflog_across_an_amend(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """The plan service's own wiring to `fork_point`, proven against a real
    rewrite: amending `pred` after `env` forked from it erases the fork
    commit from `pred`'s graph, but `plan()` still freezes `env`'s boundary
    at the pre-amend commit — read from `pred`'s reflog, not its current
    tip.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    pred_path = add_env_worktree(main_path, tmp_path, "pred", "main")
    forked_from = commit(pred_path, "f.txt", "root\npred-1\n", "pred commit 1")
    env_path = add_env_worktree(main_path, tmp_path, "env", "pred")
    commit(env_path, "g.txt", "env-1\n", "env commit 1")
    git_cmd(pred_path, "commit", "-q", "--amend", "-m", "pred commit 1 (amended)")
    # The fork commit is gone from pred's own graph now — only its reflog has it.
    assert forked_from not in git_cmd(pred_path, "log", "--format=%H", "pred")

    plan = service.plan(workspace, [repo], chain=["env"], base="pred")

    assert plan.refused is False
    (link,) = plan.links
    demo = link.repos[0]
    assert demo.repo_name == "demo"
    assert demo.boundary == forked_from
    assert demo.source == BoundarySource.fork_point


def test_backwards_chain_where_the_upper_element_has_not_advanced_is_not_refused(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """Declared limit, not correct behavior: naming `env1` above `env2` even
    though `env2` is the one that actually branched from `env1` is not
    caught by `refused-inverted-order`. `env1` has not advanced since `env2`
    branched from it, so `merge-base --fork-point env2 env1` puts the frozen
    boundary at `env1`'s own tip — zero commits past it, the exemption every
    participating repo gets here — and the ancestry half never gets a
    chance to fire either, since `env1`'s tip really is an ancestor of
    `env2`'s. The chain plans clean and would collapse the stack into one
    branch on execution; that collapse is exactly the accepted escape this
    test pins, not a bug to fix.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    env1_tip = commit(env1_path, "f.txt", "root\nenv1-1\n", "env1 commit 1")
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(env2_path, "g.txt", "env2-1\n", "env2 commit 1")

    plan = service.plan(workspace, [repo], chain=["env1"], base="env2")

    assert plan.refused is False
    (link,) = plan.links
    demo = link.repos[0]
    assert demo.boundary == env1_tip
    assert demo.source == BoundarySource.fork_point
    # The exemption this test pins: env1's tip *is* its own frozen boundary.
    tip_after = git_cmd(env1_path, "rev-parse", "env1").strip()
    assert tip_after == demo.boundary


def test_backwards_chain_where_the_upper_element_has_advanced_is_not_refused_either(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """Same backwards chain, one further commit on `env1` after `env2`
    branched from it. The exemption lapses — `env1` now carries a commit
    past its frozen boundary — but so does the ancestry half
    `refused-inverted-order` also requires: `env1`'s tip is no longer an
    ancestor of `env2`'s, since it moved off in its own direction. Declared
    limit again, pinned by the same absence of a refusal, for the opposite
    reason from the unadvanced case above.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    branch_point = commit(env1_path, "f.txt", "root\nenv1-1\n", "env1 commit 1")
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(env2_path, "g.txt", "env2-1\n", "env2 commit 1")
    commit(env1_path, "f.txt", "root\nenv1-1\nenv1-2\n", "env1 commit 2")

    plan = service.plan(workspace, [repo], chain=["env1"], base="env2")

    assert plan.refused is False
    (link,) = plan.links
    demo = link.repos[0]
    assert demo.boundary == branch_point
    assert demo.source == BoundarySource.fork_point
    env1_tip = git_cmd(env1_path, "rev-parse", "env1").strip()
    assert env1_tip != demo.boundary  # env1 carries a commit past its boundary now
    env2_tip = git_cmd(env2_path, "rev-parse", "env2").strip()
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", env1_tip, env2_tip], cwd=env1_path, capture_output=True
    )
    assert is_ancestor.returncode != 0  # env1's tip is no longer an ancestor of env2's


def _init_repo_family(tmp_path: Path, repo_name: str) -> ProjectRepository:
    """A second project repo's own `.git`, independent of `demo`'s — proves
    the per-repo `--cut` classification doesn't accidentally share state
    across repos in the same env directories."""
    main_path = tmp_path / f"{repo_name}-checkout"
    init_repo(main_path)
    commit(main_path, "f.txt", "root\n", "root")
    return ProjectRepository(name=repo_name, main_path=main_path, main_branch="main")


def test_cut_applies_vs_already_done_against_real_ancestry(tmp_path: Path, service: EnvRestackPlanService) -> None:
    """`--cut`'s two non-refusing classes, against real `merge-base
    --is-ancestor` rather than a fake's pre-seeded booleans, in two separate
    repos of the same run: one where the cut's tip genuinely is an ancestor
    of the bottom env's branch (replays the frozen range), and one where it
    isn't but the base's tip already is (the env sits on the base by some
    other means, `--cut` a no-op).
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")

    applies_repo = _init_repo_family(tmp_path, "applies")
    applies_main = tmp_path / "applies-checkout"
    landed_path = add_env_worktree(applies_main, tmp_path, "landed", "main", repo_name="applies")
    cut_tip_applies = commit(landed_path, "f.txt", "root\nlanded\n", "squash-landed predecessor")
    env_path = add_env_worktree(applies_main, tmp_path, "env", "landed", repo_name="applies")
    commit(env_path, "g.txt", "env-1\n", "env commit 1")
    add_env_worktree(applies_main, tmp_path, "base", "main", repo_name="applies")

    already_done_repo = _init_repo_family(tmp_path, "already-done")
    already_done_main = tmp_path / "already-done-checkout"
    landed_path_2 = add_env_worktree(already_done_main, tmp_path, "landed", "main", repo_name="already-done")
    commit(landed_path_2, "f.txt", "root\nlanded\n", "squash-landed predecessor, unrelated")
    base_path_2 = add_env_worktree(already_done_main, tmp_path, "base", "main", repo_name="already-done")
    commit(base_path_2, "f.txt", "root\nbase-1\n", "base commit 1")
    env_path_2 = add_env_worktree(already_done_main, tmp_path, "env", "base", repo_name="already-done")

    plan = service.plan(workspace, [applies_repo, already_done_repo], chain=["env"], base="base", cut="landed")

    assert plan.refused is False
    (link,) = plan.links
    applies = next(r for r in link.repos if r.repo_name == "applies")
    already_done = next(r for r in link.repos if r.repo_name == "already-done")
    assert applies.boundary == cut_tip_applies
    assert applies.source == BoundarySource.cut
    assert already_done.source == BoundarySource.cut
    already_done_cut_tip = git_cmd(landed_path_2, "rev-parse", "landed").strip()
    assert already_done.boundary == already_done_cut_tip
    # The shape that makes this "already done": the cut's tip is unreachable
    # from env, but the base's tip is.
    env_tip = git_cmd(env_path_2, "rev-parse", "env").strip()
    cut_reachable = subprocess.run(
        ["git", "merge-base", "--is-ancestor", already_done_cut_tip, env_tip],
        cwd=env_path_2,
        capture_output=True,
    )
    assert cut_reachable.returncode != 0


# --------------------------------------------------------------------------- #
# chain elements resolve as refs/heads/<name> — never a bare name that a
# stale remote-tracking ref of the same name could satisfy instead.
# --------------------------------------------------------------------------- #


def test_a_tag_of_the_same_name_does_not_make_a_repo_participate_as_a_chain_element(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """The failure shape a bare `rev-parse <name>` lookup falls into: repo
    `demo` carries a *tag* named `env1` — real git's own disambiguation
    order (`gitrevisions(7)`) checks `refs/tags/<name>` ahead of
    `refs/heads/<name>`, so this is the one non-branch ref shape guaranteed
    to win a bare-name lookup over an *absent* local branch of the same
    name — but no local `env1` branch: `env1` never got a real worktree in
    this repo at all, so `env2`'s own worktree — real, and the only one
    `demo` has on disk — is the one a bare-name lookup for its predecessor
    `env1` would resolve from, and a bare `git rev-parse env1` run there
    finds the tag since no local branch shadows it. (Measured directly
    against git 2.43.0: a same-named `refs/remotes/<remote>/<name>` does
    *not* win a bare lookup — only the `<remote>/<name>` two-segment form
    does — so it isn't a usable repro here; a tag is.) Sibling repo `other`
    has a genuine `env1` worktree, so `env1` still resolves *somewhere* and
    the whole run isn't refused outright — isolating the participation
    question this test is actually about. Participation is meant to mean
    "this repo has that env's local branch"; `demo`, which only ever had a
    tag of that name, has no business joining either link.
    """
    workspace, demo_repo = init_project(tmp_path)
    demo_main = tmp_path / "main-checkout"
    demo_env2 = add_env_worktree(demo_main, tmp_path, "env2", "main", repo_name="demo")
    commit(demo_env2, "g.txt", "env2-1\n", "env2 commit 1 (demo)")
    # "env1" never got a real worktree/local branch in "demo" at all — only
    # a tag of the same name.
    git_cmd(demo_main, "tag", "env1", "main")

    other_repo = _init_repo_family(tmp_path, "other")
    other_main = other_repo.main_path
    add_env_worktree(other_main, tmp_path, "env1", "main", repo_name="other")
    other_env2 = add_env_worktree(other_main, tmp_path, "env2", "env1", repo_name="other")
    commit(other_env2, "g.txt", "env2-1\n", "env2 commit 1 (other)")

    plan = service.plan(workspace, [demo_repo, other_repo], chain=["env2", "env1"], base="main")

    assert plan.refused is False
    bottom_link, top_link = plan.links
    assert (bottom_link.env, bottom_link.predecessor) == ("env1", "main")
    assert (top_link.env, top_link.predecessor) == ("env2", "env1")
    # "demo" sits out both links — env1 never got a real worktree there at
    # all (bottom link), and the top link's predecessor — env1 again — must
    # not resolve through the tag either. "other", with a genuine env1
    # worktree, participates in both normally.
    assert bottom_link.skipped_repos == ["demo"]
    assert bottom_link.participating_repos == ["other"]
    assert top_link.skipped_repos == ["demo"]
    assert top_link.participating_repos == ["other"]


def test_a_real_local_branch_of_the_same_name_does_make_the_repo_participate(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """The other half of the same fix: once `env1` is a real local branch —
    a genuine linked worktree, exactly what a healthy stack looks like —
    the repo participates in the link whose predecessor names it, and gets
    a real fork-point boundary. Without this half, a fix that simply
    refused to resolve chain elements at all (rather than scoping them to
    `refs/heads/<name>`) would also make this skip and this test would
    catch that.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    env1_tip = commit(env1_path, "f.txt", "root\nenv1-1\n", "env1 commit 1")
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(env2_path, "g.txt", "env2-1\n", "env2 commit 1")

    plan = service.plan(workspace, [repo], chain=["env2", "env1"], base="main")

    assert plan.refused is False
    bottom_link, top_link = plan.links
    assert bottom_link.participating_repos == ["demo"]
    assert top_link.participating_repos == ["demo"]
    demo = top_link.repos[0]
    assert demo.boundary == env1_tip
    assert demo.source == BoundarySource.fork_point


# --------------------------------------------------------------------------- #
# a predecessor chain element's own boundary read is scoped too — not only
# the participation read `_gather_reads` already scopes.
# --------------------------------------------------------------------------- #


def test_a_tag_on_a_non_bottom_links_predecessor_does_not_make_fork_point_ambiguous(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """`_fork_point_boundaries` hands `r.predecessor_ref` to
    `git merge-base --fork-point` as the *lower* ref — the reflog side —
    for every link but the bottom-most. For a non-bottom link that ref is
    another chain element (here `env1`, the top link `env2`'s predecessor),
    which must be scoped through `chain_element_ref` there too, not only
    where `_gather_reads` already scopes it for the participation read.

    Measured directly against git 2.43: with both `refs/heads/env1` and
    `refs/tags/env1` present, `git merge-base --fork-point env1 env2` (bare)
    exits 128 with "fatal: Ambiguous refname" — read by `fork_point` as no
    fork point at all, refusing `refused-unknown-boundary` for the top link
    even though `env1`'s real branch and `env2` share perfectly ordinary
    history. `git merge-base --fork-point refs/heads/env1 env2` (lower ref
    qualified) resolves cleanly to `env1`'s own tip at the point `env2`
    forked from it — the same boundary the bottom link's own participation
    read already established `env1` really carries.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    env1_tip = commit(env1_path, "f.txt", "root\nenv1-1\n", "env1 commit 1")
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(env2_path, "g.txt", "env2-1\n", "env2 commit 1")
    add_env_worktree(main_path, tmp_path, "master", "main")
    # A tag "env1" pointing somewhere else entirely — main's tip, not the
    # branch's — makes a bare `env1` lookup ambiguous without changing
    # what the real local branch resolves to.
    git_cmd(main_path, "tag", "env1", "main")

    plan = service.plan(workspace, [repo], chain=["env2", "env1"], base="master")

    assert plan.refused is False
    bottom_link, top_link = plan.links
    assert (bottom_link.env, bottom_link.predecessor) == ("env1", "master")
    assert (top_link.env, top_link.predecessor) == ("env2", "env1")
    demo = top_link.repos[0]
    assert demo.boundary == env1_tip
    assert demo.source == BoundarySource.fork_point


def test_a_tag_on_the_envs_own_name_does_not_silently_pick_the_wrong_fork_point(
    tmp_path: Path, service: EnvRestackPlanService
) -> None:
    """The other half of the same `_fork_point_boundaries` scoping: `env_name`
    itself — the *upper* ref `git merge-base --fork-point` takes — must also
    go through `chain_element_ref`, not only `r.predecessor_ref`.

    Unlike an ambiguous *lower* ref (which exits 128), an ambiguous upper ref
    doesn't fail at all: git 2.43 prints a warning to stderr and resolves the
    bare name to the tag anyway, silently freezing the boundary at whatever
    commit the tag happens to name — here, `main`'s tip, an ancestor of
    `env1` that reads as a superficially plausible sha — instead of `env1`'s
    real tip, the point `env2`'s real local branch actually forked from.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    env1_tip = commit(env1_path, "f.txt", "root\nenv1-1\n", "env1 commit 1")
    add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(tmp_path / "env2" / "demo", "g.txt", "env2-1\n", "env2 commit 1")
    # A tag "env2" pointing at main's tip — an ancestor of env1, and
    # therefore of the branch env2 too, so a wrong resolution here doesn't
    # even look obviously wrong.
    git_cmd(main_path, "tag", "env2", "main")

    plan = service.plan(workspace, [repo], chain=["env2"], base="env1")

    assert plan.refused is False
    (link,) = plan.links
    demo = link.repos[0]
    assert demo.boundary == env1_tip
    assert demo.source == BoundarySource.fork_point
