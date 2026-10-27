"""End to end: an outer workspace whose `lab` project repo (`nested = true`) is itself a workspace.

Real git throughout, and the nested calls run a real winter CLI child process —
this checkout's own, started as `python -m winter_cli.cli` so no `PATH` shim is
involved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent

import pytest
from dependency_injector import providers

from tests.conftest import FakeInitReporter
from tests.modules.workspace.conftest import commit, git_cmd, init_repo
from winter_cli.container import Container
from winter_cli.modules.workspace.internal.subprocess_nested_workspace_runner import SubprocessNestedWorkspaceRunner
from winter_cli.modules.workspace.nested_env import nested_child_env

WINTER = (sys.executable, "-m", "winter_cli.cli")

LAB_GITIGNORE = """\
/.winter/config.local.toml
/.winter/state.toml
/AGENTS.winter.md
"""


def _bare_from(tmp_path: Path, name: str, files: dict[str, str], executables: tuple[str, ...] = ()) -> Path:
    """A bare repo `<name>.git` holding one commit of *files* on `main`; the *executables* among them are mode 755."""
    work = tmp_path / "src" / name
    init_repo(work)
    for rel, content in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(content)
        if rel in executables:
            (work / rel).chmod(0o755)
    git_cmd(work, "add", "-A")
    git_cmd(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git_cmd(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def outer_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    app = _bare_from(tmp_path, "app", {"README": "app\n"})
    lab_config = f"""\
        main_branch = "main"
        service_prefix = "lab"

        [[project_repository]]
        name = "app"
        url = "{app}"
        """
    lab = _bare_from(
        tmp_path,
        "lab",
        {".winter/config.toml": dedent(lab_config), ".gitignore": LAB_GITIGNORE, "AGENTS.md": "# Lab\n"},
    )
    root = tmp_path / "outer"
    init_repo(root)
    outer_config = f"""\
        main_branch = "main"
        service_prefix = "outer"
        base_port = 5000
        ports_per_env = 200

        [[project_repository]]
        name = "lab"
        url = "{lab}"
        nested = true
        envs = 3
        """
    (root / ".winter").mkdir()
    (root / ".winter" / "config.toml").write_text(dedent(outer_config))
    # The outer machine-local overlay: only the `[git]` identity is meant to reach the nested workspace.
    (root / ".winter" / "config.local.toml").write_text(
        'secret = "x"\n\n[git]\nuser.name = "Ada"\nuser.email = "ada@example.com"\n'
    )
    commit(root, ".gitignore", "/projects/\n/alpha/\n/.winter/state.toml\n/AGENTS.winter.md\n", "init")
    monkeypatch.chdir(root)
    return root


def _container() -> Container:
    container = Container()
    container.nested_workspace_runner.override(
        providers.Singleton(
            SubprocessNestedWorkspaceRunner,
            subprocess_runner=container.subprocess_runner,
            error_factory=container.repo_error_factory,
            command=WINTER,
        )
    )
    return container


def test_nested_workspace_is_initialized_reported_and_destroyed_with_its_env(outer_root: Path) -> None:
    lab_alpha = outer_root / "alpha" / "lab"
    container = _container()
    reporter = FakeInitReporter()

    assert container.init_svc().reconcile_workspace(reporter) is True
    assert container.init_svc().reconcile_env("alpha", reporter) is True, reporter.errors

    # The nested workspace was initialized inside the env worktree, cloning its own projects...
    assert (lab_alpha / "projects" / "app" / "README").is_file()
    assert ("lab", "winter ws init", 0) in reporter.cmds_completed
    # The outer env's band and prefix were delegated: alpha is index 1, so its band starts at 5000 + 1 * 100.
    delegated = tomllib.loads((lab_alpha / ".winter" / "config.local.toml").read_text())
    # ...and so was the `[git]` identity the entry inherits by default, but not the outer `secret`.
    assert delegated == {
        "git": {"user": {"name": "Ada", "email": "ada@example.com"}},
        "base_port": 5300,
        "service_prefix": "outer-alpha-lab",
        "env_aliases": [],
        "envs_per_workspace": 4,
    }
    # The nested init applied the inherited identity to the clones it made.
    identity = git_cmd(lab_alpha / "projects" / "app", "config", "user.email")
    assert identity.strip() == "ada@example.com"
    # ...while the outer source checkout was cloned but never initialized as a workspace.
    assert (outer_root / "projects" / "lab" / ".winter" / "config.toml").is_file()
    assert not (outer_root / "projects" / "lab" / "projects").exists()
    # A nested repo is no extension: its root AGENTS.md reaches no AGENTS.winter.md bullet.
    agents_winter = outer_root / "AGENTS.winter.md"
    assert not agents_winter.exists() or "lab" not in agents_winter.read_text()

    _nested_winter(lab_alpha, outer_root, "ws", "init", "n1")
    assert (lab_alpha / "n1" / "app").is_dir()

    snapshot = container.workspace_snapshot_svc().collect()
    lab_wt = next(wt for wt in snapshot.environments[0].worktrees if wt.repo == "lab")
    assert lab_wt.nested is not None
    assert (lab_wt.nested.env_count, lab_wt.nested.dirty, lab_wt.nested.error) == (1, False, None)
    assert lab_wt.dirty == 0

    # A commit only n1's branch holds lives inside the outer worktree: destroy refuses rather than delete it.
    n1_app = lab_alpha / "n1" / "app"
    (n1_app / "work.txt").write_text("unpushed\n")
    git_cmd(n1_app, "add", "-A")
    git_cmd(n1_app, "-c", "user.email=t@t.com", "-c", "user.name=tester", "commit", "-q", "-m", "unpushed work")
    refused_reporter = FakeInitReporter()
    refused = container.destroy_svc().destroy_env(
        "alpha", force=False, strict=False, dry_run=False, reporter=refused_reporter
    )
    assert refused is False
    assert (
        _unpushed_refusal(refused_reporter)
        == "lab (nested: n1/app (unpushed branch), projects/app (local-only commits))"
    )
    assert (lab_alpha / "n1").is_dir()

    # Pushed to its upstream but not merged, the branch is held by a remote: no refusal for it.
    git_cmd(n1_app, "push", "-q", "-u", "origin", "HEAD")

    # A nested env destroyed from inside the nested workspace leaves its branch in the nested source checkout,
    # and a stash there is local work too: neither is in any env, and both would go with the outer worktree.
    _nested_winter(lab_alpha, outer_root, "ws", "init", "n2")
    n2_app = lab_alpha / "n2" / "app"
    (n2_app / "kept.txt").write_text("kept\n")
    git_cmd(n2_app, "add", "-A")
    git_cmd(n2_app, "-c", "user.email=t@t.com", "-c", "user.name=tester", "commit", "-q", "-m", "kept work")
    n2_branch = git_cmd(n2_app, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _nested_winter(lab_alpha, outer_root, "ws", "destroy", "n2")
    assert not n2_app.exists()
    nested_app = lab_alpha / "projects" / "app"
    (nested_app / "README").write_text("stashed\n")
    git_cmd(nested_app, "-c", "user.email=t@t.com", "-c", "user.name=tester", "stash", "-q")

    kept_reporter = FakeInitReporter()
    kept = container.destroy_svc().destroy_env(
        "alpha", force=False, strict=False, dry_run=False, reporter=kept_reporter
    )
    assert kept is False
    assert _unpushed_refusal(kept_reporter) == "lab (nested: projects/app (local-only commits, stashes))"
    git_cmd(nested_app, "stash", "drop", "-q")
    git_cmd(nested_app, "branch", "-q", "-D", n2_branch)

    destroy_reporter = FakeInitReporter()
    ok = container.destroy_svc().destroy_env(
        "alpha", force=False, strict=False, dry_run=False, reporter=destroy_reporter
    )

    assert ok is True, destroy_reporter.errors
    assert [a[3] for a in destroy_reporter.actions if a[2] == "nested_env_destroyed"] == ["n1"]
    # The nested workspace binds no service provider, so no `service down` ran there: it would have failed.
    assert [cmd for _, cmd in destroy_reporter.cmds_started if "service" in cmd] == []
    assert not (outer_root / "alpha").exists()
    worktrees = git_cmd(outer_root / "projects" / "lab", "worktree", "list")
    assert str(lab_alpha) not in worktrees


STUB_PROVIDER = """\
#!/bin/sh
echo "$*" >> "$STUB_SERVICE_LOG"
"""


def test_destroy_stops_the_nested_workspace_services_of_a_bound_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _bare_from(
        tmp_path,
        "stubsvc",
        {
            "winter-ext.toml": 'name = "stubsvc"\nprefix = "stub"\norchestrate_services = "orch"\n',
            "orch": STUB_PROVIDER,
        },
        executables=("orch",),
    )
    lab_config = f"""\
        main_branch = "main"
        service_prefix = "lab"

        [capabilities]
        service = "stubsvc"

        [[standalone_repository]]
        name = "stubsvc"
        url = "{stub}"
        """
    lab = _bare_from(tmp_path, "lab", {".winter/config.toml": dedent(lab_config), ".gitignore": LAB_GITIGNORE})
    root = tmp_path / "outer"
    init_repo(root)
    outer_config = f"""\
        main_branch = "main"
        service_prefix = "outer"
        ports_per_env = 2000

        [[project_repository]]
        name = "lab"
        url = "{lab}"
        nested = true
        """
    (root / ".winter").mkdir()
    (root / ".winter" / "config.toml").write_text(dedent(outer_config))
    commit(root, ".gitignore", "/projects/\n/alpha/\n/.winter/state.toml\n/AGENTS.winter.md\n", "init")
    monkeypatch.chdir(root)
    service_log = tmp_path / "service.log"
    monkeypatch.setenv("STUB_SERVICE_LOG", str(service_log))
    container = _container()
    reporter = FakeInitReporter()
    assert container.init_svc().reconcile_workspace(reporter) is True
    assert container.init_svc().reconcile_env("alpha", reporter) is True, reporter.errors

    dry_reporter = FakeInitReporter()
    assert container.destroy_svc().destroy_env("alpha", force=False, strict=False, dry_run=True, reporter=dry_reporter)
    assert [a[2] for a in dry_reporter.actions if "nested" in a[2]] == ["would_stop_nested_workspace_services"]
    assert not service_log.exists()

    destroy_reporter = FakeInitReporter()
    ok = container.destroy_svc().destroy_env(
        "alpha", force=False, strict=False, dry_run=False, reporter=destroy_reporter
    )

    assert ok is True, destroy_reporter.errors
    assert service_log.read_text() == "down workspace\n"
    stopped = [a[1] for a in destroy_reporter.actions if a[2] == "nested_workspace_services_stopped"]
    assert stopped == [str(root / "alpha" / "lab")]
    assert not (root / "alpha").exists()


def _nested_winter(nested_root: Path, outer_root: Path, *args: str) -> None:
    """Run this checkout's winter inside *nested_root*, as a user there would, and require it to succeed."""
    done = subprocess.run(
        [*WINTER, *args],
        cwd=nested_root,
        env=nested_child_env(os.environ, outer_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _unpushed_refusal(reporter: FakeInitReporter) -> str:
    """What the unpushed-work refusal *reporter* holds names, between the colon and the remedy."""
    [message] = [msg for _, msg in reporter.errors if "unpushed work in a nested workspace" in msg]
    return message.split("worktree: ", 1)[1].split(". Push", 1)[0]
