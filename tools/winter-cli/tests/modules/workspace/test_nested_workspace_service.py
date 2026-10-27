from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.conftest import FakeEnvIndexRegistry, FakeFilesystem, FakeInitReporter, make_workspace_config
from tests.modules.workspace.conftest import (
    FakeLocalOverlayRepository,
    FakeNestedWorkspaceRunner,
    make_nested_service,
    nested_env,
    nested_status,
    nested_wt,
)
from winter_cli.modules.workspace.models import NestedEnvState, NestedWorkspaceState, ProjectRepository, RepoError
from winter_cli.modules.workspace.nested_env import NESTED_CHAIN_VAR

OUTER = Path("/ws")
ROOT = OUTER / "alpha" / "lab"
LAB = ProjectRepository(name="lab", main_path=Path("/ws/projects/lab"), main_branch="main", nested=True)
TWO_ENVS = NestedWorkspaceState(envs=(NestedEnvState(name="n1"), NestedEnvState(name="n2")))


def test_reconcile_runs_nested_init_and_streams_its_output_as_the_repo(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(init_lines=["→ projects/", "✓ projects/ done"])

    make_nested_service(runner).reconcile(LAB, ROOT, "alpha", init_reporter)

    assert runner.status_calls == [ROOT]
    assert runner.calls == [("init", ROOT)]
    assert init_reporter.cmds_started == [("lab", "winter ws init")]
    assert init_reporter.cmd_output == [("lab", "→ projects/"), ("lab", "✓ projects/ done")]
    assert init_reporter.cmds_completed == [("lab", "winter ws init", 0)]


def _delegating(
    *,
    envs: int | None = None,
    outer_ppe: int = 200,
    committed: dict | None = None,
    local: dict | None = None,
    outer_local: dict | None = None,
    inherit_local: tuple[str, ...] = ("git",),
) -> tuple[ProjectRepository, FakeLocalOverlayRepository, list[str], FakeNestedWorkspaceRunner]:
    events: list[str] = []
    repo = ProjectRepository(
        name="lab",
        main_path=Path("/ws/projects/lab"),
        main_branch="main",
        nested=True,
        envs=envs,
        inherit_local=inherit_local,
    )
    overlay = FakeLocalOverlayRepository(
        committed={ROOT: committed or {"ports_per_env": 20, "envs_per_workspace": 4}},
        local={ROOT: local or {}, OUTER: outer_local or {}},
        events=events,
    )
    return repo, overlay, events, FakeNestedWorkspaceRunner(events=events)


def _service(runner: FakeNestedWorkspaceRunner, overlay: FakeLocalOverlayRepository, *, outer_ppe: int = 200):
    return make_nested_service(
        runner,
        config=make_workspace_config(service_prefix="outer", ports_per_env=outer_ppe, base_port=4000),
        registry=FakeEnvIndexRegistry({"alpha": 1}),
        overlay=overlay,
    )


def test_reconcile_delegates_the_outer_port_base_and_prefix_before_the_nested_init(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, events, runner = _delegating()

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    # Index 1 of an outer band of 200 starts at 4200; the parent owns slice 0, so the only child starts 100 later.
    assert overlay.local[ROOT] == {"base_port": 4300, "service_prefix": "outer-alpha-lab"}
    assert events == [f"overlay_write:{ROOT}", f"nested_init:{ROOT}"]
    assert [a[2] for a in init_reporter.actions] == ["nested_overlay_written"]


def test_reconcile_with_envs_clears_the_aliases_and_sets_the_envs_per_workspace(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(envs=3)

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {
        "base_port": 4300,
        "service_prefix": "outer-alpha-lab",
        "env_aliases": [],
        "envs_per_workspace": 4,
    }


def test_reconcile_resolves_the_outer_port_base_from_the_registry_before_the_formula(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating()
    service = make_nested_service(
        runner,
        config=make_workspace_config(service_prefix="outer", ports_per_env=200, base_port=4000),
        registry=FakeEnvIndexRegistry({"alpha": 7}),
        overlay=overlay,
    )

    service.reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT]["base_port"] == 5500


def test_reconcile_rewrites_the_delegated_keys_on_every_init(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(local={"base_port": 9999, "service_prefix": "stale", "keep": "me"})
    service = _service(runner, overlay)

    service.reconcile(repo, ROOT, "alpha", init_reporter)
    service.reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"base_port": 4300, "service_prefix": "outer-alpha-lab", "keep": "me"}
    assert len(overlay.upserts) == 2
    assert [a[2] for a in init_reporter.actions] == ["nested_overlay_written"]


def test_reconcile_reports_a_malformed_nested_config_as_a_repo_error_and_runs_nothing(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating()
    overlay.malformed.add(ROOT)

    with pytest.raises(RepoError, match=r"config\.local\.toml — Invalid value"):
        _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.upserts == []
    assert runner.calls == []


def test_reconcile_refuses_a_footprint_larger_than_the_outer_band_and_writes_and_runs_nothing(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(envs=4)

    with pytest.raises(RepoError) as refused:
        _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    message = str(refused.value)
    assert "120 ports" in message
    assert "holds only 100" in message
    assert "`envs = 3`" in message
    assert "`ports_per_env` to at least 240" in message
    assert overlay.upserts == []
    assert runner.calls == []
    assert init_reporter.cmds_started == []


def test_reconcile_refusal_names_the_ports_per_env_set_only_in_the_nested_local_overlay(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(envs=4, committed={"envs_per_workspace": 4}, local={"ports_per_env": 25})

    with pytest.raises(RepoError) as refused:
        _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    # (4 + 1 + 1) x 25: the local layer's ports_per_env, not the default 20 of the committed one.
    message = str(refused.value)
    assert "150 ports" in message
    assert "ports_per_env of 25" in message
    assert "`envs = 2`" in message
    assert overlay.upserts == []


def test_reconcile_judges_the_nested_defaults_when_envs_is_unset(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(committed={"ports_per_env": 20})

    with pytest.raises(RepoError, match=r"980 ports.*holds only 100"):
        _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.upserts == []


def test_reconcile_suggests_lowering_the_nested_ports_when_no_envs_count_can_fit(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(envs=1, committed={"ports_per_env": 50})

    with pytest.raises(RepoError, match="lower the nested workspace's `ports_per_env`") as refused:
        _service(runner, overlay, outer_ppe=200).reconcile(repo, ROOT, "alpha", init_reporter)

    assert "150 ports" in str(refused.value)


def test_reconcile_accepts_a_footprint_that_exactly_fills_the_outer_band(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(envs=3)

    _service(runner, overlay, outer_ppe=200).reconcile(repo, ROOT, "alpha", init_reporter)

    assert runner.calls == [("init", ROOT)]


OUTER_LOCAL = {
    "git": {"user": {"name": "Ada", "email": "ada@example.com"}},
    "secret": "x",
    "base_port": 1,
    "project_repository": [{"name": "private", "url": "git@example.com:me/private.git"}],
    "standalone_repository": [{"name": "shared", "url": "git@example.com:me/shared.git"}],
}
DELEGATED = {"base_port": 4300, "service_prefix": "outer-alpha-lab"}


def test_reconcile_inherits_only_the_git_table_by_default(init_reporter: FakeInitReporter) -> None:
    repo, overlay, events, runner = _delegating(outer_local=OUTER_LOCAL)

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"git": OUTER_LOCAL["git"], **DELEGATED}
    assert events == [f"overlay_write:{ROOT}", f"nested_init:{ROOT}"]
    (written,) = [a for a in init_reporter.actions if a[2] == "nested_overlay_written"]
    assert written[3] == "base_port = 4300, service_prefix = 'outer-alpha-lab'; inherited git"
    assert "ada@example.com" not in written[3]


def test_reconcile_reads_the_inherited_keys_from_the_outer_root_not_the_nested_one(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(
        local={"git": {"user": {"name": "nested"}}}, outer_local={"git": {"user": {"name": "outer"}}}
    )

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT]["git"] == {"user": {"name": "outer"}}


def test_reconcile_inherits_exactly_the_named_keys(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local=OUTER_LOCAL, inherit_local=("secret",))

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"secret": "x", **DELEGATED}


def test_reconcile_with_an_empty_inherit_list_copies_nothing_and_leaves_the_nested_keys_alone(
    init_reporter: FakeInitReporter,
) -> None:
    repo, overlay, _, runner = _delegating(
        outer_local=OUTER_LOCAL, inherit_local=(), local={"git": {"user": {"name": "nested"}}, "keep": 1}
    )

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"git": {"user": {"name": "nested"}}, "keep": 1, **DELEGATED}
    (written,) = [a for a in init_reporter.actions if a[2] == "nested_overlay_written"]
    assert "inherited" not in written[3]


def test_reconcile_skips_a_named_key_the_outer_overlay_does_not_hold(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local=OUTER_LOCAL, inherit_local=("git", "missing"))

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"git": OUTER_LOCAL["git"], **DELEGATED}
    assert runner.calls == [("init", ROOT)]


def test_reconcile_never_inherits_a_repository_array_even_when_named(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(
        outer_local=OUTER_LOCAL, inherit_local=("git", "project_repository", "standalone_repository")
    )

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"git": OUTER_LOCAL["git"], **DELEGATED}


def test_reconcile_lets_a_delegated_key_win_over_an_inherited_one(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local=OUTER_LOCAL, inherit_local=("base_port", "git"))

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT]["base_port"] == 4300


def test_reconcile_judges_the_footprint_with_the_inherited_keys_applied(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local={"ports_per_env": 50}, inherit_local=("ports_per_env",))

    with pytest.raises(RepoError, match="250 ports"):
        _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.upserts == []
    assert runner.calls == []


def test_reconcile_leaves_the_nested_keys_it_is_not_told_to_inherit(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local=OUTER_LOCAL, local={"secret": "nested", "keep": 1})

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.local[ROOT] == {"secret": "nested", "keep": 1, "git": OUTER_LOCAL["git"], **DELEGATED}


def test_the_inherited_table_is_a_copy_of_the_outer_overlay(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(outer_local={"git": {"user": {"name": "Ada"}}})

    _service(runner, overlay).reconcile(repo, ROOT, "alpha", init_reporter)
    overlay.local[ROOT]["git"]["user"]["name"] = "changed"

    assert overlay.local[OUTER]["git"] == {"user": {"name": "Ada"}}


def test_reconcile_raises_when_nested_init_exits_non_zero(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(init_returncode=1)

    with pytest.raises(RepoError, match="exited with code 1"):
        make_nested_service(runner).reconcile(LAB, ROOT, "alpha", init_reporter)

    assert init_reporter.cmds_completed == [("lab", "winter ws init", 1)]


def test_state_reads_the_nested_status_json() -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: nested_status(ROOT, nested_env("n1"), nested_env("n2"))})

    assert make_nested_service(runner).state(ROOT) == TWO_ENVS
    assert runner.status_calls == [ROOT]


def test_destroy_envs_destroys_each_env_and_passes_the_flags_through(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()

    ok = make_nested_service(runner).destroy_envs(
        LAB, ROOT, TWO_ENVS, force=True, strict=True, provision_teardown=False, reporter=init_reporter
    )

    assert ok is True
    assert runner.calls == [
        ("destroy_env", ROOT, "n1", True, True, False),
        ("destroy_env", ROOT, "n2", True, True, False),
    ]
    destroyed = [(a[1], a[3]) for a in init_reporter.actions if a[2] == "nested_env_destroyed"]
    assert destroyed == [(str(ROOT / "n1"), "n1"), (str(ROOT / "n2"), "n2")]
    assert ("lab", "destroying n1") in init_reporter.cmd_output


def test_destroy_envs_attempts_every_env_and_reports_failure(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(
        destroy_returncodes={"n1": 1}, destroy_errors={"n2": RepoError("winter resolved another workspace")}
    )
    three = NestedWorkspaceState(envs=(*TWO_ENVS.envs, NestedEnvState(name="n3")))

    ok = make_nested_service(runner).destroy_envs(
        LAB, ROOT, three, force=False, strict=False, provision_teardown=True, reporter=init_reporter
    )

    assert ok is False
    assert [c[2] for c in runner.calls] == ["n1", "n2", "n3"]
    assert [a[3] for a in init_reporter.actions if a[2] == "nested_env_destroyed"] == ["n3"]
    errors = [e for repo, e in init_reporter.errors if repo == "lab"]
    assert any("winter ws destroy n1" in e and "code 1" in e for e in errors)
    assert any("n2" in e and "another workspace" in e for e in errors)


def test_nested_workspace_state_counts_envs_dirtiness_and_unpushed_work() -> None:
    clean = NestedWorkspaceState(envs=(NestedEnvState(name="n1"),))
    dirty = NestedWorkspaceState(envs=(NestedEnvState(name="n1"), NestedEnvState(name="n2", dirty=("app",))))
    unpushed = NestedWorkspaceState(
        envs=(NestedEnvState(name="n1", unpushed=("app", "web")),), checkouts=("projects/app",)
    )

    assert (clean.env_count, clean.dirty, clean.unpushed) == (1, False, ())
    assert (dirty.env_count, dirty.dirty) == (2, True)
    assert unpushed.unpushed == ("n1/app", "n1/web", "projects/app")
    assert NestedWorkspaceState(envs=()).dirty is False


# ── guards: preflight, verification, chain ────────────────────────────────────


def test_a_root_path_mismatch_raises_and_runs_no_nested_command(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: nested_status(OUTER, nested_env("n1"))})
    overlay = FakeLocalOverlayRepository()
    service = make_nested_service(runner, overlay=overlay)

    for call in (
        lambda: service.state(ROOT),
        lambda: service.reconcile(LAB, ROOT, "alpha", init_reporter),
    ):
        with pytest.raises(RepoError, match="resolved workspace '/ws', not the nested workspace"):
            call()
    destroyed = service.destroy_envs(
        LAB, ROOT, TWO_ENVS, force=True, strict=False, provision_teardown=True, reporter=init_reporter
    )

    assert destroyed is False
    assert runner.calls == []
    assert overlay.upserts == []
    assert sum("not the nested workspace" in msg for _, msg in init_reporter.errors) == 2


def test_a_missing_root_path_raises() -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: {"schema_version": 1, "workspace": {}, "environments": []}})

    with pytest.raises(RepoError, match="not the nested workspace"):
        make_nested_service(runner).state(ROOT)


def test_a_root_without_a_winter_config_raises_before_running_anything(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()

    with pytest.raises(RepoError, match=r"no \.winter/config\.toml"):
        make_nested_service(runner, fs=FakeFilesystem()).reconcile(LAB, ROOT, "alpha", init_reporter)

    assert runner.status_calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not json", "is not JSON"),
        (json.dumps({**nested_status(ROOT), "schema_version": 2}), "schema_version 2"),
        ("[]", "the status document is list"),
        (json.dumps({**nested_status(ROOT), "environments": [{"name": "n1"}]}), r"malformed: .*worktrees"),
        (json.dumps(nested_status(ROOT, nested_env("n1", {**nested_wt(), "dirty": None}))), r"malformed: .*dirty"),
    ],
)
def test_an_unusable_status_raises_repo_error(stdout: str, message: str) -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: stdout})

    with pytest.raises(RepoError, match=message) as excinfo:
        make_nested_service(runner).state(ROOT)

    assert excinfo.value.cwd == str(ROOT)
    assert "winter ws status --json" in str(excinfo.value)


def test_a_failed_status_call_propagates() -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: RepoError("nested workspace status failed")})

    with pytest.raises(RepoError, match="status failed"):
        make_nested_service(runner).state(ROOT)


def test_a_root_is_verified_once_and_reused_by_every_later_call(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(statuses={ROOT: nested_status(ROOT, nested_env("n1"), nested_env("n2"))})
    service = make_nested_service(runner)

    state = service.state(ROOT)
    service.destroy_envs(LAB, ROOT, state, force=False, strict=False, provision_teardown=True, reporter=init_reporter)
    service.reconcile(LAB, ROOT, "alpha", init_reporter)

    assert runner.status_calls == [ROOT]
    assert [c[0] for c in runner.calls] == ["destroy_env", "destroy_env", "init"]


def test_every_child_runs_with_the_scrubbed_env_and_the_chain_extended_by_this_workspace_root(
    init_reporter: FakeInitReporter,
) -> None:
    environ = {
        "WINTER_INVOCATION_CWD": "/outer",
        "WINTER_PORT_BASE": "4020",
        "WINTER_LOG_LEVEL": "debug",
        "VIRTUAL_ENV": "/outer/.venv",
        "PATH": "/outer/.venv/bin:/usr/bin",
        NESTED_CHAIN_VAR: "/up",
    }
    runner = FakeNestedWorkspaceRunner()

    make_nested_service(runner, environ=environ).reconcile(LAB, ROOT, "alpha", init_reporter)

    assert len(runner.envs) == 2
    for env in runner.envs:
        assert env == {"WINTER_LOG_LEVEL": "debug", "PATH": "/usr/bin", NESTED_CHAIN_VAR: f"/up:{OUTER}"}


def test_a_process_whose_own_root_is_on_the_chain_refuses_every_nested_call(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()
    service = make_nested_service(runner, environ={NESTED_CHAIN_VAR: str(OUTER)})

    with pytest.raises(RepoError, match="which an enclosing nested call already runs in"):
        service.state(ROOT)
    with pytest.raises(RepoError, match="which an enclosing nested call already runs in"):
        service.reconcile(LAB, ROOT, "alpha", init_reporter)

    assert runner.status_calls == []
    assert runner.calls == []


def test_a_root_already_on_the_chain_is_refused_without_running_anything() -> None:
    runner = FakeNestedWorkspaceRunner()
    service = make_nested_service(runner, environ={NESTED_CHAIN_VAR: f"/elsewhere:{ROOT}"})

    with pytest.raises(RepoError, match="already being run by an enclosing winter call"):
        service.state(ROOT)

    assert runner.status_calls == []


def test_a_legitimate_nested_child_still_reads_its_own_nested_workspaces() -> None:
    deep = ROOT / "n1" / "deep"
    runner = FakeNestedWorkspaceRunner()
    child = make_nested_service(
        runner, config=make_workspace_config(workspace_root=ROOT), environ={NESTED_CHAIN_VAR: str(OUTER)}
    )

    assert child.state(deep) == NestedWorkspaceState(envs=())
    assert runner.envs[0][NESTED_CHAIN_VAR] == f"{OUTER}:{ROOT}"


SIBLINGS = tuple(OUTER / env / "lab" for env in ("alpha", "beta", "gamma"))


class _MisResolvingWinter(FakeNestedWorkspaceRunner):
    """Every `winter` started in a nested root resolves the outer workspace instead.

    Each status call stands in for one spawned child process: a winter at the
    outer root, running with the env it was started with, whose own
    `ws status` reads every sibling nested worktree before reporting the outer
    root.
    """

    def status_json(self, root: Path, env: Mapping[str, str]) -> str:
        super().status_json(root, env)
        child = make_nested_service(self, environ=env)
        for sibling in SIBLINGS:
            with contextlib.suppress(RepoError):
                child.state(sibling)
        return json.dumps(nested_status(OUTER))


def test_a_mis_resolved_child_stops_at_once_instead_of_fanning_out_to_every_sibling() -> None:
    runner = _MisResolvingWinter()
    parent = make_nested_service(runner)

    for root in SIBLINGS:
        with pytest.raises(RepoError, match="not the nested workspace"):
            parent.state(root)

    # One spawn per sibling: each child finds its own root (the outer one) on
    # the chain and refuses its own nested reads. Chaining target roots only,
    # the children would spawn 3 + 3x2 + 3x2x1 = 15 times.
    assert runner.status_calls == list(SIBLINGS)


CHILDREN = ("blizzard-workspace", "winter-workspace", "winter-harness-engineering")


def _child(name: str, *, envs: int | None = 1) -> ProjectRepository:
    return ProjectRepository(
        name=name, main_path=Path(f"/ws/projects/{name}"), main_branch="main", nested=True, envs=envs, inherit_local=()
    )


def _siblings(
    env: str,
    index: int,
    *,
    band: int = 240,
    order: tuple[str, ...] = CHILDREN,
    committed: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], dict[str, FakeNestedWorkspaceRunner]]:
    """Reconcile every child of *env* at registry *index* and return each child's written overlay and its runner."""
    roots = {name: OUTER / env / name for name in order}
    overlay = FakeLocalOverlayRepository(
        committed={root: (committed or {}).get(name, {"ports_per_env": 20}) for name, root in roots.items()},
        local={root: {} for root in roots.values()},
    )
    runners = {name: FakeNestedWorkspaceRunner() for name in order}
    for name in order:
        service = make_nested_service(
            runners[name],
            config=make_workspace_config(service_prefix="outer", ports_per_env=band, base_port=6000),
            registry=FakeEnvIndexRegistry({env: index}),
            overlay=overlay,
            nested=order,
        )
        service.reconcile(_child(name), roots[name], env, FakeInitReporter())
    return {name: overlay.local[root] for name, root in roots.items()}, runners


def test_three_children_get_distinct_slices_and_prefixes_in_the_alpha_and_beta_example() -> None:
    expected = {
        "alpha": (1, (6300, 6360, 6420)),
        "beta": (2, (6540, 6600, 6660)),
    }
    for env, (index, bases) in expected.items():
        written, _ = _siblings(env, index)
        assert [written[name]["base_port"] for name in CHILDREN] == list(bases)
        assert [written[name]["service_prefix"] for name in CHILDREN] == [f"outer-{env}-{name}" for name in CHILDREN]


def test_the_slice_assignment_is_deterministic_across_repeated_init() -> None:
    first, _ = _siblings("alpha", 1)
    second, _ = _siblings("alpha", 1)

    assert first == second


def test_reordering_the_nested_repos_reassigns_the_slices() -> None:
    reordered = tuple(reversed(CHILDREN))

    written, _ = _siblings("alpha", 1, order=reordered)

    assert [written[name]["base_port"] for name in reordered] == [6300, 6360, 6420]


def test_a_band_that_does_not_divide_evenly_leaves_the_remainder_unused() -> None:
    written, _ = _siblings("alpha", 1, band=250)

    # 250 // 4 = 62; alpha's band starts at 6000 + 250.
    assert [written[name]["base_port"] for name in CHILDREN] == [6312, 6374, 6436]


def test_each_child_is_checked_against_its_own_footprint() -> None:
    committed = {"winter-workspace": {"ports_per_env": 30}}

    with pytest.raises(RepoError, match=r"90 ports.*holds only 60.*at least 360"):
        _siblings("alpha", 1, committed=committed)


def test_a_zero_sized_slice_is_refused_before_any_write_or_nested_init(init_reporter: FakeInitReporter) -> None:
    repo, overlay, _, runner = _delegating(envs=1)

    with pytest.raises(RepoError, match=r"holds only 0"):
        _service(runner, overlay, outer_ppe=1).reconcile(repo, ROOT, "alpha", init_reporter)

    assert overlay.upserts == []
    assert runner.calls == []
