"""Shared real-git fixture helpers for the `workspace` feature subtree.

Lifted out of `test_env_restack_service_real_git.py` and
`test_env_restack_plan_service_real_git.py` the moment a second file needed
the identical set (`winter-context:/standards/testing.md`'s "lift to
`conftest.py` the moment a second file needs it" rule) — both build the same
shape: one project repo's canonical checkout (`init_project`), with every env
a real `git worktree add` linked worktree off that same `.git`
(`add_env_worktree`), matching what `winter ws init` actually leaves behind.

`rebase.backend` and `rebase.updateRefs` are pinned explicitly in
`init_repo` rather than left to the ambient global git config: the latter in
particular is directly relevant to a restack chain, whose links share one ref
namespace on stacked branches — a developer machine with it enabled globally
would otherwise get different `rebase --onto` side effects (extra refs
updated in lockstep) than CI, silently, for every test in this subtree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from winter_cli.modules.workspace.models import ProjectRepository, Workspace


def git_cmd(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path: Path, default_branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    git_cmd(path, "init", "-q", "-b", default_branch)
    git_cmd(path, "config", "user.email", "t@t.com")
    git_cmd(path, "config", "user.name", "tester")
    git_cmd(path, "config", "commit.gpgsign", "false")
    git_cmd(path, "config", "rebase.backend", "merge")
    git_cmd(path, "config", "rebase.updateRefs", "false")


def commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    git_cmd(path, "add", "-A")
    git_cmd(path, "commit", "-q", "-m", message)
    return git_cmd(path, "rev-parse", "HEAD").strip()


def init_project(tmp_path: Path) -> tuple[Workspace, ProjectRepository]:
    """One project repo's canonical checkout — every env worktree added below
    is linked off this same `.git`, so all of them share one ref namespace."""
    main_path = tmp_path / "main-checkout"
    init_repo(main_path)
    commit(main_path, "f.txt", "root\n", "root")
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    project_repo = ProjectRepository(name="demo", main_path=main_path, main_branch="main")
    return workspace, project_repo


def add_env_worktree(main_path: Path, tmp_path: Path, env_name: str, base_ref: str, repo_name: str = "demo") -> Path:
    """A linked worktree at `<tmp_path>/<env_name>/<repo_name>`, checked out
    on a new branch named `env_name` forked from `base_ref` — the real shape
    `FeatureWorktree.path` (`environment.path / repository.name`) expects."""
    env_dir = tmp_path / env_name
    env_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = env_dir / repo_name
    git_cmd(main_path, "worktree", "add", "-b", env_name, str(worktree_path), base_ref)
    return worktree_path
