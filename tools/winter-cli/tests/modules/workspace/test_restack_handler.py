"""Tests for `RestackHandler` — plan/execute dispatch, text and `--json`
rendering, and exit codes.

Wires the *real* `EnvRestackPlanService` / `EnvRestackService` against fake
repo Protocols (mirroring `test_env_restack_plan_service.py` /
`test_env_restack_service.py`), rather than mocking the two services
directly, so a handler bug in how it threads `workspace` / `project_repos` /
`plan` between the two calls would actually surface here instead of being
hidden behind a service-level mock.

Covers:
- `--dry-run` renders the plan alone (boundary + source, no outcome) in text
  and `--json`, and makes zero calls of any kind into the execute service's
  repo seam — pinned with a fake that raises on any attribute access, not by
  inspecting output.
- A successful full run renders boundary, source, *and* outcome per repo in
  text and `--json`, and returns normally (exit 0).
- A pre-flight refusal renders the refusal vocabulary in text and `--json`
  and exits 1 — before the execute service is ever touched.
- A conflict stops the run, exits 1, and round-trips through `--json` with
  env, repo, replayed commit, conflicted files, completed outcomes, and the
  remaining plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.handlers.restack_handler import RestackHandler, RestackParams
from winter_cli.modules.workspace.models import (
    FeatureWorktree,
    ProjectRepository,
    RebaseConflict,
    RebaseOntoResult,
    RepoStatus,
    Workspace,
)

WORKSPACE_ROOT = Path("/ws")


def _repo(name: str) -> ProjectRepository:
    return ProjectRepository(name=name, main_path=WORKSPACE_ROOT / "projects" / name, main_branch="main")


class _FakeReadRepoRepository:
    """`IReadRepoRepository` stub for the plan half — scoped by
    `(repo_name, ref)`, gated on whether `(env, repo)` is `provisioned`,
    mirroring `FakeReadRepoRepository` in `test_env_restack_plan_service.py`.
    """

    def __init__(self) -> None:
        self.provisioned: set[tuple[str, str]] = set()
        self.ref_tips: dict[tuple[str, str], str] = {}
        self.fork_points: dict[tuple[str, str, str], str] = {}
        self.statuses: dict[tuple[str, str], RepoStatus] = {}

    def _key(self, worktree: FeatureWorktree) -> tuple[str, str]:
        return (worktree.environment.name, worktree.repository.name)

    def get_ref_tip(self, worktree: FeatureWorktree, ref: str) -> str | None:
        if self._key(worktree) not in self.provisioned:
            return None
        # A chain element resolves as `refs/heads/<name>`; BASE/`--cut`
        # resolve as an arbitrary bare ref. This flat fake has no real
        # branch/tag/remote distinction, so it normalizes the prefix away
        # rather than modeling it — mirrors `FakeReadRepoRepository` in
        # `test_env_restack_plan_service.py`.
        local_name = ref.removeprefix("refs/heads/")
        return self.ref_tips.get((worktree.repository.name, local_name))

    def fork_point(self, worktree: FeatureWorktree, lower_ref: str, upper_ref: str) -> str | None:
        if self._key(worktree) not in self.provisioned:
            return None
        # Both args arrive `refs/heads/<name>`-scoped for a chain element;
        # normalized away the same way `get_ref_tip` above does.
        key = (worktree.repository.name, lower_ref.removeprefix("refs/heads/"), upper_ref.removeprefix("refs/heads/"))
        return self.fork_points.get(key)

    def is_ancestor(self, worktree: FeatureWorktree, ancestor_ref: str, ref: str) -> bool:
        return False

    def is_rebase_in_progress(self, worktree: FeatureWorktree) -> bool:
        return False

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        return self.statuses.get(
            self._key(worktree),
            RepoStatus(
                name=worktree.repository.name,
                path=str(worktree.path),
                main_branch=worktree.repository.main_branch,
                branch=worktree.environment.name,
            ),
        )


class _FakeWriteRepoRepository:
    """`IWriteRepoRepository` stub for the execute half — mirrors
    `FakeWriteRepoRepository` in `test_env_restack_service.py`: a
    `rebase_effects` table says what a clean `rebase_onto` moves the branch
    to, so a later read genuinely observes the earlier call's effect."""

    def __init__(self) -> None:
        self.ref_tips: dict[tuple[str, str], str] = {}
        self.ancestors: set[tuple[str, str, str]] = set()
        self.conflicts: dict[tuple[str, str], RebaseConflict] = {}
        self.rebase_effects: dict[tuple[str, str], str] = {}
        self.rebase_calls: list[tuple[str, str, str, str, str]] = []

    def get_ref_tip(self, worktree: FeatureWorktree, ref: str) -> str | None:
        # `ref` is `refs/heads/<name>` for the env branch and a non-bottom
        # link's predecessor; normalized away the same way
        # `FakeWriteRepoRepository.get_ref_tip` does in
        # `test_env_restack_service.py`.
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


class _RaisingWriteRepoRepository:
    """Every attribute access raises — wired as the execute service's repo
    seam for the `--dry-run` no-write-calls test, so *any* call at all
    (read or write) surfaces immediately as a test failure rather than a
    silent no-op a mock's default return value would mask."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_RaisingWriteRepoRepository.{name} called — --dry-run must touch no repo seam")


def _provision(fake: _FakeReadRepoRepository, env: str, repo: str, tip: str) -> None:
    fake.provisioned.add((env, repo))
    fake.ref_tips[(repo, env)] = tip


def _cli_output_svc() -> MagicMock:
    svc = MagicMock()
    svc.style.side_effect = lambda text, _style: text
    svc.render_table.return_value = []
    return svc


def _make_handler(
    read_fake: Any,
    write_fake: Any,
    project_repos: list[ProjectRepository],
    workspace: Workspace,
    cli_output_svc: MagicMock,
) -> RestackHandler:
    repo_factory = MagicMock()
    repo_factory.get_project_repos.return_value = project_repos
    return RestackHandler(
        plan_svc=EnvRestackPlanService(repo_repo=read_fake),
        execute_svc=EnvRestackService(repo_repo=write_fake),
        repo_factory=repo_factory,
        workspace=workspace,
        cli_output_svc=cli_output_svc,
    )


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, service_prefix="t", main_branch="main")


# ── --dry-run: plan alone, zero write-seam calls ────────────────────────────


def test_dry_run_makes_zero_calls_into_the_execute_services_repo_seam(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load-bearing --dry-run guarantee: pinned with a fake that raises
    on *any* attribute access, not by inspecting rendered output. If the
    handler reached into `EnvRestackService` at all for a dry run, this
    test would fail with the fake's own AssertionError, not a rendering
    mismatch."""
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    handler.run(RestackParams(chain=["env1"], base="master", dry_run=True, output_json=False))

    # Reaching this line at all is the assertion: `_RaisingWriteRepoRepository`
    # raises `AssertionError` from *any* attribute access, so a handler bug
    # that reached into the execute service for a dry run would have failed
    # this test with that error rather than let execution continue here.
    # No SystemExit either — a clean, unrefused dry run returns normally.
    out = capsys.readouterr().out
    assert "env1 onto master" in out


def test_dry_run_json_renders_boundary_and_source_with_no_outcome_field(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    handler.run(RestackParams(chain=["env1"], base="master", dry_run=True, output_json=True))

    doc = json.loads(capsys.readouterr().out)
    assert doc == {
        "links": [
            {
                "env": "env1",
                "predecessor": "master",
                "repos": [{"repo_name": "demo", "boundary": "boundary-sha", "source": "fork-point"}],
            }
        ]
    }


def test_dry_run_text_renders_no_outcome_column(workspace: Workspace) -> None:
    """A wrong implementation that always renders the OUTCOME column (even
    when there's no report to source it from) would pass a mere
    presence-of-boundary check; asserting the exact headers passed to
    render_table catches that."""
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"
    cli_output_svc = _cli_output_svc()

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, cli_output_svc)
    handler.run(RestackParams(chain=["env1"], base="master", dry_run=True, output_json=False))

    cli_output_svc.render_table.assert_called_once()
    _, kwargs = cli_output_svc.render_table.call_args
    assert kwargs["headers"] == ["REPO", "BOUNDARY", "SOURCE"]
    rows = cli_output_svc.render_table.call_args[0][0]
    assert rows == [["demo", "boundary-sha", "fork-point"]]


# ── successful full run: boundary, source, and outcome together ────────────


def test_full_run_success_renders_outcome_alongside_boundary_and_source(workspace: Workspace) -> None:
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"

    write_fake = _FakeWriteRepoRepository()
    write_fake.ref_tips[("demo", "master")] = "master-tip"
    write_fake.ref_tips[("demo", "env1")] = "env1-tip"
    write_fake.rebase_effects[("env1", "demo")] = "env1-new-tip"
    cli_output_svc = _cli_output_svc()

    handler = _make_handler(read_fake, write_fake, [_repo("demo")], workspace, cli_output_svc)
    handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=False))

    assert write_fake.rebase_calls == [("env1", "demo", "master-tip", "boundary-sha", "env1")]
    cli_output_svc.render_table.assert_called_once()
    _, kwargs = cli_output_svc.render_table.call_args
    assert kwargs["headers"] == ["REPO", "BOUNDARY", "SOURCE", "OUTCOME"]
    rows = cli_output_svc.render_table.call_args[0][0]
    assert rows == [["demo", "boundary-sha", "fork-point", "rebased"]]


def test_full_run_success_json_round_trips_boundary_source_and_outcome(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"

    write_fake = _FakeWriteRepoRepository()
    write_fake.ref_tips[("demo", "master")] = "master-tip"
    write_fake.ref_tips[("demo", "env1")] = "env1-tip"
    write_fake.ancestors.add(("demo", "master-tip", "env1-tip"))  # already an ancestor -> up-to-date

    handler = _make_handler(read_fake, write_fake, [_repo("demo")], workspace, _cli_output_svc())
    handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=True))

    doc = json.loads(capsys.readouterr().out)
    assert doc["links"] == [
        {
            "env": "env1",
            "predecessor": "master",
            "repos": [
                {"repo_name": "demo", "boundary": "boundary-sha", "source": "fork-point", "outcome": "up-to-date"}
            ],
        }
    ]
    assert doc["completed"] == [{"env": "env1", "repo_name": "demo", "result": "up-to-date"}]
    assert doc["conflict"] is None
    assert doc["remaining"] == {"links": [], "refusals": []}
    assert write_fake.rebase_calls == []  # up-to-date never reaches rebase_onto


# ── refusal: pre-flight, all-or-nothing, exit 1 ─────────────────────────────


def test_refusal_never_reaches_the_execute_services_repo_seam(workspace: Workspace) -> None:
    """A refused plan must exit before `EnvRestackService` is ever called —
    pinned the same way as the --dry-run guarantee, with a fake that raises
    on any attribute access, so a builder who wires refusal handling after
    the execute call (rather than before it) fails here immediately."""
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"
    read_fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo", path="/ws/env1/demo", main_branch="main", branch="env1", staged_count=1
    )

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit) as excinfo:
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=False))
    assert excinfo.value.code == 1


def test_refusal_text_names_the_code_env_and_repo(workspace: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"
    read_fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo", path="/ws/env1/demo", main_branch="main", branch="env1", staged_count=1
    )

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit):
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=False))

    out = capsys.readouterr().out
    assert "refused-dirty" in out
    assert "env1" in out
    assert "demo" in out


def test_refusal_json_names_the_code_env_and_repos(workspace: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"
    read_fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo", path="/ws/env1/demo", main_branch="main", branch="env1", staged_count=1
    )

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit) as excinfo:
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=True))
    assert excinfo.value.code == 1

    doc = json.loads(capsys.readouterr().out)
    assert doc == {
        "refusals": [
            {
                "result": "refused-dirty",
                "env": "env1",
                "predecessor": "master",
                "repos": ["demo"],
                "element": None,
            }
        ]
    }


def test_dry_run_does_not_bypass_a_refusal(workspace: Workspace) -> None:
    """`--dry-run` previews an unrefused plan; a refused run is refused
    regardless of `--dry-run`, since nothing about it is safe to preview
    either."""
    read_fake = _FakeReadRepoRepository()
    _provision(read_fake, "env1", "demo", "env1-tip")
    _provision(read_fake, "master", "demo", "master-tip")
    read_fake.fork_points[("demo", "master", "env1")] = "boundary-sha"
    read_fake.statuses[("env1", "demo")] = RepoStatus(
        name="demo", path="/ws/env1/demo", main_branch="main", branch="env1", staged_count=1
    )

    handler = _make_handler(read_fake, _RaisingWriteRepoRepository(), [_repo("demo")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit) as excinfo:
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=True, output_json=False))
    assert excinfo.value.code == 1


# ── conflict: stops the run, exits 1, round-trips through --json ──────────


def _two_repo_conflict_setup(workspace: Workspace) -> tuple[_FakeReadRepoRepository, _FakeWriteRepoRepository]:
    read_fake = _FakeReadRepoRepository()
    write_fake = _FakeWriteRepoRepository()
    for repo_name in ("alpha", "beta"):
        _provision(read_fake, "env1", repo_name, f"{repo_name}-env1-tip")
        _provision(read_fake, "master", repo_name, f"{repo_name}-master-tip")
        read_fake.fork_points[(repo_name, "master", "env1")] = f"{repo_name}-boundary"
        write_fake.ref_tips[(repo_name, "master")] = f"{repo_name}-master-tip"
        write_fake.ref_tips[(repo_name, "env1")] = f"{repo_name}-env1-tip"
    write_fake.rebase_effects[("env1", "alpha")] = "alpha-new-tip"  # alpha rebases cleanly
    write_fake.conflicts[("env1", "beta")] = RebaseConflict(
        replayed_commit="deadbeef", conflicted_paths=["a.txt", "b.txt"]
    )
    return read_fake, write_fake


def test_conflict_exits_one_and_stops_the_run(workspace: Workspace) -> None:
    read_fake, write_fake = _two_repo_conflict_setup(workspace)
    handler = _make_handler(read_fake, write_fake, [_repo("alpha"), _repo("beta")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit) as excinfo:
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=False))
    assert excinfo.value.code == 1
    assert [c[1] for c in write_fake.rebase_calls] == ["alpha", "beta"]


def test_conflict_text_names_the_replayed_commit_and_conflicted_files(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    read_fake, write_fake = _two_repo_conflict_setup(workspace)
    handler = _make_handler(read_fake, write_fake, [_repo("alpha"), _repo("beta")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit):
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=False))

    out = capsys.readouterr().out
    assert "deadbeef" in out
    assert "a.txt" in out
    assert "b.txt" in out
    assert "beta" in out
    # Replayed *in* env1 (checked out there), *onto* master (its
    # predecessor) — not the reverse a wrong implementation could render
    # instead, naming the predecessor as what was replayed onto the env.
    assert "in env1" in out
    assert "onto master" in out
    assert "onto env1" not in out


def test_conflict_json_round_trips_env_repo_replayed_commit_conflicted_files_completed_and_remaining(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    read_fake, write_fake = _two_repo_conflict_setup(workspace)
    handler = _make_handler(read_fake, write_fake, [_repo("alpha"), _repo("beta")], workspace, _cli_output_svc())

    with pytest.raises(SystemExit) as excinfo:
        handler.run(RestackParams(chain=["env1"], base="master", dry_run=False, output_json=True))
    assert excinfo.value.code == 1

    doc = json.loads(capsys.readouterr().out)

    assert doc["conflict"] == {
        "env": "env1",
        "predecessor": "master",
        "repo_name": "beta",
        "replayed_commit": "deadbeef",
        "conflicted_paths": ["a.txt", "b.txt"],
    }
    assert doc["completed"] == [{"env": "env1", "repo_name": "alpha", "result": "rebased"}]
    assert doc["remaining"] == {
        "links": [
            {
                "env": "env1",
                "predecessor": "master",
                "repos": [
                    {"repo_name": "alpha", "boundary": "alpha-boundary", "source": "fork-point"},
                    {"repo_name": "beta", "boundary": "beta-boundary", "source": "fork-point"},
                ],
            }
        ],
        "refusals": [],
    }
    # The per-link merged view carries both: the completed repo's outcome and
    # the conflicting repo's, distinguishable from a repo that was never reached.
    merged_repos = doc["links"][0]["repos"]
    assert {"repo_name": "alpha", "boundary": "alpha-boundary", "source": "fork-point", "outcome": "rebased"} in (
        merged_repos
    )
    assert {"repo_name": "beta", "boundary": "beta-boundary", "source": "fork-point", "outcome": "conflict"} in (
        merged_repos
    )
