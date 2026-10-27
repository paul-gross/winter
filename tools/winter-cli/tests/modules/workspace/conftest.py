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

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tests.conftest import FakeEnvIndexRegistry, FakeFilesystem, make_workspace_config
from winter_cli.config.local_overlay_repository import ILocalOverlayRepository
from winter_cli.config.models import WorkspaceConfig
from winter_cli.config.workspace import CONFIG_FILE, WINTER_DIR
from winter_cli.core.config_file import ConfigFileReadError
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.workspace.env_index import EnvPortBaseResolver
from winter_cli.modules.workspace.models import ProjectRepository, RepoError, Workspace
from winter_cli.modules.workspace.nested_workspace_runner import INestedWorkspaceRunner
from winter_cli.modules.workspace.nested_workspace_service import NestedWorkspaceService


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


def nested_wt(
    repo: str = "app",
    *,
    dirty: int = 0,
    ahead: int = 0,
    tracking_ahead: int = 0,
    upstream: str | None = None,
    nested: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `environments[].worktrees[]` entry of a schema-v1 `ws status --json` document.

    An *upstream* reads as a present tracking ref; none means no upstream configured.
    """
    wt: dict[str, Any] = {
        "repo": repo,
        "dirty": dirty,
        "ahead": ahead,
        "tracking_ahead": tracking_ahead,
        "upstream": upstream,
        "tracking_ref_present": upstream is not None,
    }
    if nested is not None:
        wt["nested"] = nested
    return wt


def nested_env(name: str, *worktrees: dict[str, Any]) -> dict[str, Any]:
    """One `environments[]` entry of a schema-v1 `ws status --json` document; one clean `app` worktree by default."""
    return {"name": name, "worktrees": list(worktrees) if worktrees else [nested_wt()]}


def nested_status(
    root: Path,
    *envs: dict[str, Any],
    projects: list[dict[str, Any]] | None = None,
    standalones: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A schema-v1 `ws status --json` document, trimmed to the fields the nested reader uses, reporting *root*."""
    return {
        "schema_version": 1,
        "workspace": {"root_path": str(root)},
        "environments": list(envs),
        "projects": projects or [],
        "standalones": standalones or [],
    }


class FakeNestedWorkspaceRunner:
    """INestedWorkspaceRunner fake — canned status documents and exit codes per nested root; records every call.

    `statuses` maps a nested root to the `ws status --json` document
    `status_json` returns (serialized), or to a `RepoError` it raises; a
    document given as a string is returned verbatim, and an unmapped root
    reports itself holding no envs. Every `status_json` call is recorded in
    `status_calls`; every `run` is recorded in `calls` — `ws init` as
    `("init", root)` and `ws destroy` as
    `("destroy_env", root, env, force, strict, provision_teardown)` and
    `service down workspace` as `("service_down", root)` — and, when given, in
    the shared `events` log so a test can assert ordering against other fakes.
    `envs` records the environment of every call, in order. `init_returncode` /
    `init_lines` shape every init; `destroy_returncodes` maps an env name to its
    destroy exit code (default 0) and `destroy_errors` maps one to a `RepoError`
    to raise; `service_down_returncode` is every `service down` exit code.
    """

    def __init__(
        self,
        *,
        statuses: dict[Path, dict[str, Any] | str | RepoError] | None = None,
        init_returncode: int = 0,
        init_lines: list[str] | None = None,
        destroy_returncodes: dict[str, int] | None = None,
        destroy_errors: dict[str, RepoError] | None = None,
        service_down_returncode: int = 0,
        events: list[str] | None = None,
    ) -> None:
        self.statuses = dict(statuses or {})
        self.init_returncode = init_returncode
        self.init_lines = list(init_lines or [])
        self.destroy_returncodes = dict(destroy_returncodes or {})
        self.destroy_errors = dict(destroy_errors or {})
        self.service_down_returncode = service_down_returncode
        self.events = events if events is not None else []
        self.calls: list[tuple] = []
        self.status_calls: list[Path] = []
        self.envs: list[dict[str, str]] = []

    def status_json(self, root: Path, env: Mapping[str, str]) -> str:
        self.status_calls.append(root)
        self.envs.append(dict(env))
        status = self.statuses.get(root, nested_status(root))
        if isinstance(status, RepoError):
            raise status
        return status if isinstance(status, str) else json.dumps(status)

    def run(self, root: Path, args: Sequence[str], env: Mapping[str, str], on_line: Callable[[str], None]) -> int:
        self.envs.append(dict(env))
        if list(args) == ["ws", "init"]:
            self.calls.append(("init", root))
            self.events.append(f"nested_init:{root}")
            for line in self.init_lines:
                on_line(line)
            return self.init_returncode
        if list(args) == ["service", "down", "workspace"]:
            self.calls.append(("service_down", root))
            self.events.append(f"nested_service_down:{root}")
            on_line("stopping workspace")
            return self.service_down_returncode
        assert list(args[:2]) == ["ws", "destroy"], args
        name = args[2]
        flags = set(args[3:])
        self.calls.append(
            ("destroy_env", root, name, "--force" in flags, "--strict" in flags, "--no-provision-teardown" not in flags)
        )
        self.events.append(f"nested_destroy:{name}")
        if name in self.destroy_errors:
            raise self.destroy_errors[name]
        on_line(f"destroying {name}")
        return self.destroy_returncodes.get(name, 0)


def _conforms_fake_nested_workspace_runner(x: FakeNestedWorkspaceRunner) -> INestedWorkspaceRunner:
    return x


class EveryRootHasConfigFilesystem(FakeFilesystem):
    """A `FakeFilesystem` on which every `.winter/config.toml` exists — the nested service's preflight always passes."""

    def is_file(self, path: Path) -> bool:
        return (path.name == CONFIG_FILE and path.parent.name == WINTER_DIR) or super().is_file(path)


class FakeLocalOverlayRepository:
    """ILocalOverlayRepository fake — in-memory committed and local layers per nested root.

    `committed` / `local` map a root to its parsed `config.toml` / `config.local.toml`;
    a root absent from either reads as `{}`, and a root in `malformed` raises
    `ConfigFileReadError` from both methods. `upsert_local` merges into `local`, returns
    whether anything changed, and records each call in `upserts` and, when given, the
    shared `events` log so a test can assert the write precedes the nested init.
    """

    def __init__(
        self,
        *,
        committed: dict[Path, dict[str, Any]] | None = None,
        local: dict[Path, dict[str, Any]] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.committed = dict(committed or {})
        self.local = {root: dict(values) for root, values in (local or {}).items()}
        self.events = events if events is not None else []
        self.upserts: list[tuple[Path, dict[str, Any]]] = []
        self.malformed: set[Path] = set()

    def read_layers(self, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        self._refuse_malformed(root)
        return dict(self.committed.get(root, {})), dict(self.local.get(root, {}))

    def upsert_local(self, root: Path, values: Mapping[str, Any]) -> bool:
        self._refuse_malformed(root)
        self.events.append(f"overlay_write:{root}")
        self.upserts.append((root, dict(values)))
        current = self.local.setdefault(root, {})
        changed = any(key not in current or current[key] != value for key, value in values.items())
        current.update(values)
        return changed

    def _refuse_malformed(self, root: Path) -> None:
        if root in self.malformed:
            raise ConfigFileReadError(f"reading {root / '.winter' / 'config.local.toml'} — Invalid value (at line 1)")


def _conforms_fake_local_overlay_repository(x: FakeLocalOverlayRepository) -> ILocalOverlayRepository:
    return x


def make_nested_service(
    runner: INestedWorkspaceRunner,
    *,
    config: WorkspaceConfig | None = None,
    registry: FakeEnvIndexRegistry | None = None,
    overlay: FakeLocalOverlayRepository | None = None,
    fs: IFilesystemReader | None = None,
    environ: Mapping[str, str] | None = None,
    nested: Sequence[str] = ("lab",),
) -> NestedWorkspaceService:
    """A `NestedWorkspaceService` over fakes.

    The outer config defaults to prefix `t` at `/ws` with a band wide enough
    for any nested footprint; every nested root holds a `.winter/config.toml`
    unless *fs* says otherwise, and the process environment is empty unless
    *environ* is given.
    """
    outer = config or make_workspace_config(ports_per_env=2000)
    return NestedWorkspaceService(
        runner,
        fs=fs or EveryRootHasConfigFilesystem(),
        workspace_root=outer.workspace_root,
        environ=environ if environ is not None else {},
        port_bases=EnvPortBaseResolver(outer, registry or FakeEnvIndexRegistry()),
        service_prefix=outer.service_prefix,
        ports_per_env=outer.ports_per_env,
        overlay_repo=overlay or FakeLocalOverlayRepository(),
        nested_repo_names=nested,
    )
