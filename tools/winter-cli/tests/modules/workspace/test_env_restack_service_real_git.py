"""Real-git tests for `EnvRestackService` — conflict-stop and re-run-resume,
the behaviors this phase exists to prove and that a fake cannot establish
honestly: a fake's `rebase_onto` returns whatever a test tells it to, but
whether a worktree is genuinely left mid-rebase on disk, and whether
`git rebase --continue` really does leave the branch in a state the next
run reads as `up-to-date`, is only provable against real git.

Every env here is a real `git worktree add` linked worktree of one shared
`.git`, matching the shape `winter ws init` leaves behind, and every plan is
produced by the real `EnvRestackPlanService` before being handed to
`EnvRestackService.execute` — so a resume test drives the exact two-service
pipeline an operator would, not a hand-assembled plan a builder might get
wrong in a way both services would happen to agree on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import git
import pytest

from tests.modules.workspace.conftest import add_env_worktree, commit, git_cmd, init_project, init_repo
from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.write_repo_repository import WriteRepoRepository
from winter_cli.modules.workspace.models import ProjectRepository, RepoRestackOutcome, RestackResult, Workspace


def _rev_parse(path: Path, ref: str) -> str:
    return git_cmd(path, "rev-parse", ref).strip()


def _is_ancestor(path: Path, ancestor: str, ref: str) -> bool:
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, ref], cwd=path, capture_output=True)
    return result.returncode == 0


def _subjects(path: Path, rng: str) -> list[str]:
    """Commit subjects (first line of each message) in `rng`, oldest-to-caller
    order as `git log` itself reports them.

    `git rebase --onto` abandons every original sha by construction — an
    ancestry probe against a pre-run sha is true for *any* boundary,
    including one that excluded nothing and simply replayed everything as
    fresh shas (`_is_ancestor` alone can't distinguish a correct `--cut`
    from one that did nothing). Comparing subject sets instead pins what
    actually survived the replay: a boundary frozen too low would pull a
    predecessor's own subject into the range too; one frozen too high would
    drop one of the env's own.
    """
    return [line for line in git_cmd(path, "log", "--format=%s", rng).splitlines() if line]


def _head_branch(path: Path) -> str | None:
    """The local branch HEAD is attached to, or `None` if HEAD is detached —
    `git symbolic-ref -q HEAD` exits non-zero exactly when detached, the
    same signal `git status`'s "HEAD detached from ..." line is built on."""
    result = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=path, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _rebase_in_progress(path: Path) -> bool:
    with git.Repo(str(path)) as r:
        git_dir = Path(r.git.rev_parse("--git-dir"))
        if not git_dir.is_absolute():
            git_dir = path / git_dir
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


@pytest.fixture
def plan_service() -> EnvRestackPlanService:
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    return EnvRestackPlanService(repo_repo=WriteRepoRepository(error_factory, git_ops))


@pytest.fixture
def exec_service() -> EnvRestackService:
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    return EnvRestackService(repo_repo=WriteRepoRepository(error_factory, git_ops))


def test_conflicting_rebase_leaves_the_worktree_mid_rebase_and_reports_the_detail(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """A genuine content conflict between `env`'s own commit and `master`'s
    stops the run, leaves real on-disk rebase state behind, and the report
    names the exact commit git stopped on and the exact conflicted path.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    env_path = add_env_worktree(main_path, tmp_path, "env", "main")
    conflicting_commit = commit(env_path, "f.txt", "root\nenv-change\n", "env commit 1")
    master_path = add_env_worktree(main_path, tmp_path, "master", "main")
    commit(master_path, "f.txt", "root\nmaster-change\n", "master commit 1")

    plan = plan_service.plan(workspace, [repo], chain=["env"], base="master")
    assert plan.refused is False

    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is False
    assert report.conflict is not None
    assert report.conflict.env == "env"
    assert report.conflict.repo_name == "demo"
    assert report.conflict.replayed_commit == conflicting_commit
    assert report.conflict.conflicted_paths == ["f.txt"]
    assert _rebase_in_progress(env_path) is True
    assert report.remaining.links == plan.links


def test_rerun_after_rebase_continue_reads_the_resolved_link_up_to_date_and_continues(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """Two-link chain (`env2` onto `env1` onto `master`); the bottom link
    conflicts, is resolved by hand with `git rebase --continue` — matching
    what an operator actually runs — and a fresh `plan()` + `execute()` with
    the *same* chain reads the bottom link `up_to_date` (its predecessor's
    tip is now an ancestor of `env1`'s rewritten branch) and replays `env2`
    onto `env1`'s new tip.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    master_path = add_env_worktree(main_path, tmp_path, "master", "main")
    commit(master_path, "f.txt", "root\nmaster-change\n", "master commit 1")
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    commit(env1_path, "f.txt", "root\nenv1-change\n", "env1 commit 1")
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "env1")
    commit(env2_path, "g.txt", "env2-1\n", "env2 commit 1")

    chain = ["env2", "env1"]
    plan = plan_service.plan(workspace, [repo], chain=chain, base="master")
    assert plan.refused is False
    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is False
    assert report.conflict is not None
    assert report.conflict.env == "env1"
    assert _rebase_in_progress(env1_path) is True
    assert _rebase_in_progress(env2_path) is False  # the upper link was never touched

    # Operator resolves the conflict exactly as `git rebase --continue` expects.
    (env1_path / "f.txt").write_text("root\nmaster-change\nenv1-change\n")
    git_cmd(env1_path, "add", "-A")
    git_cmd(env1_path, "-c", "core.editor=true", "rebase", "--continue")
    assert _rebase_in_progress(env1_path) is False
    env1_tip_after_resolve = _rev_parse(env1_path, "env1")

    resumed_plan = plan_service.plan(workspace, [repo], chain=chain, base="master")
    assert resumed_plan.refused is False
    resumed_report = exec_service.execute(workspace, [repo], resumed_plan)

    assert resumed_report.success is True
    assert resumed_report.completed[0].env == "env1"
    assert resumed_report.completed[0].result == RestackResult.up_to_date
    assert resumed_report.completed[1].env == "env2"
    assert resumed_report.completed[1].result == RestackResult.rebased
    # env1's branch didn't move again on the resumed run's up-to-date leg.
    assert _rev_parse(env1_path, "env1") == env1_tip_after_resolve
    # env2 now replays onto env1's *post-resolve* tip, not its pre-conflict one.
    assert _is_ancestor(env2_path, env1_tip_after_resolve, "env2") is True


def test_cut_resume_multi_repo_fixture(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """A `--cut` run's bottom link conflicts in one repo while a sibling
    participating repo is never reached; resolved with
    `git rebase --continue`; re-run with the same chain and the same
    `--cut` reports the resolved repo `up_to_date` (Decision 4's `cut
    already done`), replays the frozen range in the sibling, continues
    upward through the top link, and the excluded commits are absent from
    both repos afterward.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")

    # "conflicting" repo: env's own commit collides with base's on the same file.
    conflicting_main = tmp_path / "conflicting-checkout"
    init_repo(conflicting_main)
    commit(conflicting_main, "f.txt", "root\n", "root")
    conflicting_repo = ProjectRepository(name="conflicting", main_path=conflicting_main, main_branch="main")
    landed_c = add_env_worktree(conflicting_main, tmp_path, "landed", "main", repo_name="conflicting")
    cut_tip_c = commit(landed_c, "f.txt", "root\nlanded\n", "squash-landed predecessor")
    env_c = add_env_worktree(conflicting_main, tmp_path, "env", "landed", repo_name="conflicting")
    excluded_commit_c = commit(env_c, "f.txt", "root\nlanded\nenv-conflicting\n", "env commit (conflicting)")
    base_c = add_env_worktree(conflicting_main, tmp_path, "base", "main", repo_name="conflicting")
    commit(base_c, "f.txt", "root\nbase-change\n", "base commit (conflicting)")

    # "clean" repo: same shape, but env's commit doesn't collide with base's.
    clean_main = tmp_path / "clean-checkout"
    init_repo(clean_main)
    commit(clean_main, "f.txt", "root\n", "root")
    clean_repo = ProjectRepository(name="clean", main_path=clean_main, main_branch="main")
    landed_k = add_env_worktree(clean_main, tmp_path, "landed", "main", repo_name="clean")
    cut_tip_k = commit(landed_k, "f.txt", "root\nlanded\n", "squash-landed predecessor")
    env_k = add_env_worktree(clean_main, tmp_path, "env", "landed", repo_name="clean")
    commit(env_k, "g.txt", "env-clean-1\n", "env commit (clean)")
    base_k = add_env_worktree(clean_main, tmp_path, "base", "main", repo_name="clean")
    commit(base_k, "h.txt", "base-clean-1\n", "base commit (clean)")

    # Pre-run subjects for the commit-preservation check below, captured
    # before either repo's `env` branch moves at all.
    pre_subjects_c = _subjects(env_c, f"{cut_tip_c}..env")
    pre_subjects_k = _subjects(env_k, f"{cut_tip_k}..env")

    plan = plan_service.plan(workspace, [conflicting_repo, clean_repo], chain=["env"], base="base", cut="landed")
    assert plan.refused is False

    report = exec_service.execute(workspace, [conflicting_repo, clean_repo], plan)

    assert report.success is False
    assert report.conflict is not None
    assert report.conflict.repo_name == "conflicting"
    assert report.conflict.replayed_commit == excluded_commit_c
    assert _rebase_in_progress(env_c) is True
    assert _rebase_in_progress(env_k) is False  # the sibling was never reached

    # Operator resolves the conflict exactly as `git rebase --continue` expects.
    (env_c / "f.txt").write_text("root\nbase-change\n")
    git_cmd(env_c, "add", "-A")
    git_cmd(env_c, "-c", "core.editor=true", "rebase", "--continue")
    assert _rebase_in_progress(env_c) is False

    resumed_plan = plan_service.plan(
        workspace, [conflicting_repo, clean_repo], chain=["env"], base="base", cut="landed"
    )
    assert resumed_plan.refused is False
    resumed_report = exec_service.execute(workspace, [conflicting_repo, clean_repo], resumed_plan)

    assert resumed_report.success is True
    outcomes = {o.repo_name: o.result for o in resumed_report.completed}
    assert outcomes == {"conflicting": RestackResult.up_to_date, "clean": RestackResult.rebased}

    # Both repos' env branches now sit on their own base.
    base_c_tip = _rev_parse(base_c, "base")
    base_k_tip = _rev_parse(base_k, "base")
    assert _is_ancestor(env_c, base_c_tip, "env") is True
    assert _is_ancestor(env_k, base_k_tip, "env") is True
    # `_is_ancestor(env, cut_tip, "env") is False` alone would hold for *any*
    # boundary — `rebase --onto` abandons original shas by construction, so
    # that probe can't tell a correct `--cut` from one that excluded
    # nothing and just replayed everything as fresh shas. Subject sets can:
    # the squash-landed subject is gone from both repos' post-run ranges.
    post_subjects_c = _subjects(env_c, f"{base_c_tip}..env")
    post_subjects_k = _subjects(env_k, f"{base_k_tip}..env")
    assert "squash-landed predecessor" not in post_subjects_c
    assert "squash-landed predecessor" not in post_subjects_k
    # "clean" replays untouched — its own commit subject survives the
    # replay unchanged, proving the boundary excluded exactly the
    # squash-landed range and nothing of the env's own.
    assert post_subjects_k == pre_subjects_k
    # "conflicting" was hand-resolved to content identical to `base`'s own
    # (the conflict-resolution step above writes the same text `base
    # commit (conflicting)` already committed), so its replayed commit
    # became empty and `rebase --continue` dropped it — `env` now sits
    # exactly on `base`'s tip, not one commit past it. `pre_subjects_c`
    # (still the original, pre-resolution subject) is deliberately not
    # compared for equality here: the drop is `git rebase`'s own default
    # empty-commit handling, not a boundary bug.
    assert pre_subjects_c == ["env commit (conflicting)"]
    assert post_subjects_c == []


def test_env_with_no_commits_past_its_boundary_fast_forwards_and_reports_rebased(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """`env` never grew a commit of its own past where it forked from
    `master` — `boundary == env`'s own tip. `rebase_onto` still runs (the
    `up_to_date` shortcut only fires once `env`'s branch already sits *past*
    the predecessor's tip, which isn't true here), replays zero commits, and
    fast-forwards `env`'s branch to master's tip outright — reported
    `rebased`, per Decision 5, not a distinct outcome of its own.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    master_path = add_env_worktree(main_path, tmp_path, "master", "main")
    master_tip = commit(master_path, "f.txt", "root\nmaster-change\n", "master commit 1")
    env_path = add_env_worktree(main_path, tmp_path, "env", "main")
    # "env" carries no commit of its own past the fork point from master.

    plan = plan_service.plan(workspace, [repo], chain=["env"], base="master")
    assert plan.refused is False

    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env", repo_name="demo", result=RestackResult.rebased)]
    assert _rev_parse(env_path, "env") == master_tip
    # `rebase_onto`'s `branch` argument is what keeps HEAD attached to the
    # env branch rather than left detached on the replayed tip — dropping
    # that argument would detach HEAD here and no other assertion would
    # notice.
    assert _head_branch(env_path) == "env"


def test_a_non_conflict_repo_error_stops_the_run_and_reports_failure_without_discarding_earlier_completions(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """`rebase_onto` raises `RepoError` (not a conflict) whenever the git
    failure it caught doesn't leave the worktree mid-rebase — the exact
    shape `refused-dirty`'s deliberate untracked-file gap produces: an
    untracked path in `bbb`'s `env` worktree collides with a tracked file
    `master` adds, so `git rebase --onto` aborts during its own initial
    checkout with "untracked working tree files would be overwritten",
    before the sequencer ever starts (no `rebase-merge`/`rebase-apply`
    directory is ever created — this is genuinely not a conflict-stop).

    `aaa` has no such collision and rebases cleanly first — `execute` walks
    repos in argument order within a link — so this also proves the run
    doesn't discard `aaa`'s already-completed outcome when `bbb`'s seam
    raises.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")

    aaa_main = tmp_path / "aaa-checkout"
    init_repo(aaa_main)
    commit(aaa_main, "f.txt", "root\n", "root")
    aaa_repo = ProjectRepository(name="aaa", main_path=aaa_main, main_branch="main")
    aaa_master_path = add_env_worktree(aaa_main, tmp_path, "master", "main", repo_name="aaa")
    commit(aaa_master_path, "new.txt", "new\n", "master adds new.txt")
    aaa_env_path = add_env_worktree(aaa_main, tmp_path, "env", "main", repo_name="aaa")
    commit(aaa_env_path, "own.txt", "own\n", "env commit (aaa)")

    bbb_main = tmp_path / "bbb-checkout"
    init_repo(bbb_main)
    commit(bbb_main, "f.txt", "root\n", "root")
    bbb_repo = ProjectRepository(name="bbb", main_path=bbb_main, main_branch="main")
    bbb_master_path = add_env_worktree(bbb_main, tmp_path, "master", "main", repo_name="bbb")
    commit(bbb_master_path, "new.txt", "new\n", "master adds new.txt")
    bbb_env_path = add_env_worktree(bbb_main, tmp_path, "env", "main", repo_name="bbb")
    bbb_env_commit = commit(bbb_env_path, "own.txt", "own\n", "env commit (bbb)")
    # An untracked file colliding with the one `master` adds as tracked —
    # `refused-dirty`'s worktree-safety guard deliberately excludes
    # untracked files, so `plan()` does not refuse this run.
    (bbb_env_path / "new.txt").write_text("untracked\n")

    plan = plan_service.plan(workspace, [aaa_repo, bbb_repo], chain=["env"], base="master")
    assert plan.refused is False

    report = exec_service.execute(workspace, [aaa_repo, bbb_repo], plan)

    assert report.success is False
    assert report.conflict is None
    assert report.failure is not None
    assert report.failure.env == "env"
    assert report.failure.repo_name == "bbb"
    assert "untracked" in report.failure.message
    # aaa already completed before bbb's seam raised — not discarded with the stack frame.
    assert report.completed == [RepoRestackOutcome(env="env", repo_name="aaa", result=RestackResult.rebased)]
    assert report.remaining.links == plan.links
    # bbb's env branch was never actually touched by the aborted attempt.
    assert _rev_parse(bbb_env_path, "env") == bbb_env_commit
    assert _rebase_in_progress(bbb_env_path) is False


def test_a_tag_named_like_the_env_does_not_make_execution_read_the_wrong_tip(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """`_execute_repo` reads `env_tip` via `chain_element_ref(link.env)` —
    never a bare name — because `rebase_onto`'s own `branch` argument
    (also `link.env`, deliberately left bare) always operates on the real
    local branch; a bare `get_ref_tip` lookup must resolve to that same
    branch or the `up_to_date`/`cut_exempt` ancestry tests below are decided
    against a commit that was never actually replayed.

    A `--cut` bottom link isolates this precisely: `fork_point` is never
    called for it (`_cut_boundaries` reads tips directly), so a same-named
    tag's ambiguity never surfaces at plan time — only here, at execution.
    With a tag `env` pointing at `base`'s own tip, a buggy bare lookup reads
    `env_tip` as that tag: `cut_exempt` (boundary vs. tag) comes back False
    since the squash-landed boundary shares no history with `base`, and
    `up_to_date` (newbase vs. tag) then comes back True — the same commit
    compared with itself — so the run would report a stale `up_to_date` and
    replay nothing. Read through the real branch instead, `env`'s own commit
    genuinely sits past the squash-landed boundary (`cut_exempt` is True),
    so the run always reaches `rebase_onto` and actually drops the excluded
    commit.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    main_path = tmp_path / "main-checkout"
    init_repo(main_path)
    commit(main_path, "f.txt", "root\n", "root")
    repo = ProjectRepository(name="demo", main_path=main_path, main_branch="main")

    landed_path = add_env_worktree(main_path, tmp_path, "landed", "main")
    cut_tip = commit(landed_path, "f.txt", "root\nlanded\n", "squash-landed predecessor")
    env_path = add_env_worktree(main_path, tmp_path, "env", "landed")
    commit(env_path, "g.txt", "env-1\n", "env commit 1")
    base_path = add_env_worktree(main_path, tmp_path, "base", "main")
    base_tip = commit(base_path, "h.txt", "base-1\n", "base commit 1")
    # A tag "env" pointing at base's own tip — ambiguous with the real
    # local branch "env", and (deliberately) unrelated to "landed"'s history.
    git_cmd(main_path, "tag", "env", "base")
    # `refs/heads/env` here, not bare "env": the tag just created makes a
    # bare lookup ambiguous, and this oracle must stay independent of the
    # very bug the rest of the test exists to catch.
    pre_subjects = _subjects(env_path, f"{cut_tip}..refs/heads/env")

    plan = plan_service.plan(workspace, [repo], chain=["env"], base="base", cut="landed")
    assert plan.refused is False

    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env", repo_name="demo", result=RestackResult.rebased)]
    # env's own commit now sits on base, and the excluded commit is gone.
    assert _is_ancestor(env_path, base_tip, "env") is True
    # The tag itself was never touched by the (real-branch) rebase.
    assert _rev_parse(env_path, "refs/tags/env") == base_tip
    # Subject sets, not `_is_ancestor(env, cut_tip, "env") is False` alone
    # (true for any boundary once shas are abandoned by the replay): the
    # excluded, squash-landed subject is gone, and env's own commit subject
    # survived the replay unchanged.
    post_subjects = _subjects(env_path, f"{base_tip}..refs/heads/env")
    assert "squash-landed predecessor" not in post_subjects
    assert post_subjects == pre_subjects == ["env commit 1"]


def test_a_tag_named_like_a_non_bottom_links_predecessor_reads_the_real_post_rebase_tip(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """The `newbase` read for a non-bottom link's predecessor must also go
    through `chain_element_ref` — not only the env's own tip
    (`test_a_tag_named_like_the_env_does_not_make_execution_read_the_wrong_tip`).

    Two-link chain `env2` onto `env1` onto `master`; a tag `env1` — created
    before either link runs, so it stays put — points at `master`'s
    *original* tip. The bottom link (`env1` onto `master`) runs first and
    genuinely moves the `env1` *branch* past that point. A bare newbase
    lookup for the top link would still resolve to the stale tag instead of
    `env1`'s real, just-rebased tip: `env2` would then replay onto `master`'s
    original tip directly, silently dropping `env1`'s own commit (`g.txt`)
    from `env2`'s history entirely, rather than sitting on top of it.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    master_path = add_env_worktree(main_path, tmp_path, "master", "main")
    commit(master_path, "h.txt", "master-own\n", "master commit 1")
    # The tag is created now, before either link runs, and never moves again.
    git_cmd(main_path, "tag", "env1", "master")
    env1_path = add_env_worktree(main_path, tmp_path, "env1", "main")
    commit(env1_path, "g.txt", "env1-own\n", "env1 commit 1")
    # `refs/heads/env1` rather than the bare name: `git worktree add` itself
    # refuses an ambiguous starting point outright — this is test setup
    # plumbing, not the production read path under test.
    env2_path = add_env_worktree(main_path, tmp_path, "env2", "refs/heads/env1")
    commit(env2_path, "i.txt", "env2-own\n", "env2 commit 1")

    plan = plan_service.plan(workspace, [repo], chain=["env2", "env1"], base="master")
    assert plan.refused is False

    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is True
    assert [o.result for o in report.completed] == [RestackResult.rebased, RestackResult.rebased]
    # `refs/heads/env1` here, not bare "env1": the tag above makes a bare
    # lookup ambiguous, and this oracle must stay independent of the very
    # bug the test exists to catch.
    env1_tip_after_rebase = _rev_parse(env1_path, "refs/heads/env1")
    # env2 sits on top of env1's real, just-rebased tip — not on the stale tag.
    assert _is_ancestor(env2_path, env1_tip_after_rebase, "env2") is True
    # env1's own commit (g.txt) survived into env2's history; a newbase read
    # through the tag would have replayed env2's commit directly onto
    # master, silently dropping it.
    assert (env2_path / "g.txt").exists()
    assert (env2_path / "h.txt").exists()
    assert (env2_path / "i.txt").exists()


def test_amend_boundary_execution_preserves_exactly_the_envs_own_commit_subjects(
    tmp_path: Path, plan_service: EnvRestackPlanService, exec_service: EnvRestackService
) -> None:
    """Commit preservation across a real amend-driven boundary shift, all the
    way through `execute()` — the plan-service half of this scenario
    (`test_plan_freezes_the_boundary_from_the_predecessors_reflog_across_an_amend`
    in `test_env_restack_plan_service_real_git.py`) only proves the frozen
    boundary sha survives the amend; it never calls `execute()`, so nothing
    there proves the *replay* built from that boundary actually reproduces
    `env`'s own history and nothing else.

    Every existing post-run assertion elsewhere in this file is an outcome
    enum, a tip equality, or an ancestry probe — none of which would catch a
    boundary frozen one commit too low (replaying `pred`'s own amended
    commit into `env` too) or too high (dropping `env`'s own commit
    instead). Subject-set equality catches both: `pred`'s subject must never
    appear in `env`'s post-run range, and `env`'s own subject must survive
    unchanged.
    """
    workspace, repo = init_project(tmp_path)
    main_path = tmp_path / "main-checkout"
    pred_path = add_env_worktree(main_path, tmp_path, "pred", "main")
    forked_from = commit(pred_path, "f.txt", "root\npred-1\n", "pred commit 1")
    env_path = add_env_worktree(main_path, tmp_path, "env", "pred")
    commit(env_path, "g.txt", "env-1\n", "env commit 1")
    pre_subjects = _subjects(env_path, f"{forked_from}..env")
    git_cmd(pred_path, "commit", "-q", "--amend", "-m", "pred commit 1 (amended)")
    # The fork commit is gone from pred's own graph now — only its reflog has it.
    assert forked_from not in git_cmd(pred_path, "log", "--format=%H", "pred")

    plan = plan_service.plan(workspace, [repo], chain=["env"], base="pred")
    assert plan.refused is False
    (link,) = plan.links
    assert link.repos[0].boundary == forked_from

    report = exec_service.execute(workspace, [repo], plan)

    assert report.success is True
    assert report.completed == [RepoRestackOutcome(env="env", repo_name="demo", result=RestackResult.rebased)]
    new_base_tip = _rev_parse(pred_path, "pred")
    post_subjects = _subjects(env_path, f"{new_base_tip}..env")
    assert "pred commit 1 (amended)" not in post_subjects
    assert post_subjects == pre_subjects == ["env commit 1"]
