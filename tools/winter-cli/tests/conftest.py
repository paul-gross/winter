from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    WorkspaceConfig,
)
from winter_cli.container import Container
from winter_cli.core.subprocess_runner import SubprocessResult


@pytest.fixture
def tmp_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialize a minimal `.winter/` workspace at tmp_path and chdir into it.

    `WorkspaceConfigService.load()` walks up from cwd looking for a `.winter/`
    directory — chdir-ing into the tmp root lets the real loader run against
    a controlled config without touching the developer's workspace.
    """
    winter_dir = tmp_path / ".winter"
    winter_dir.mkdir()
    (winter_dir / "config.toml").write_text(
        dedent(
            """
            main_branch = "main"
            session_prefix = "test"

            [[project_repository]]
            name = "demo-repo"
            url = "git@example.com:demo/demo-repo.git"
            """
        ).strip()
        + "\n"
    )
    (tmp_path / "projects").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def container(tmp_workspace_root: Path) -> Container:
    """A real DI Container resolved against the tmp workspace.

    Every provider is wired against the tmp_workspace_root config, so resolving
    a service through this fixture exercises the full DI graph — that's the
    end-to-end DI smoke test the issue calls for. Individual service tests can
    still construct collaborators directly when they want tighter control.
    """
    return Container()


@pytest.fixture
def workspace_config(tmp_workspace_root: Path) -> WorkspaceConfig:
    """A hand-rolled WorkspaceConfig anchored at tmp_workspace_root.

    Used by tests that want to construct services directly without going
    through the Container — keeps the config schema explicit in the test.
    """
    return WorkspaceConfig(
        workspace_root=tmp_workspace_root,
        session_prefix="test",
        main_branch="main",
        git_excludes=[],
        git_identity=None,
        adopt_extensions=AdoptExtensions.winter,
        singleton_repos=[SingletonRepository(name=tmp_workspace_root.name, type=SingletonType.workspace)],
        project_repos=[
            ProjectRepositoryConfig(name="demo-repo", url="git@example.com:demo/demo-repo.git"),
        ],
        standalone_repos=[],
    )


@pytest.fixture
def init_reporter() -> FakeInitReporter:
    return FakeInitReporter()


class FakeInitReporter:
    """In-memory reporter that records every IInitReporter event for assertion.

    Used in lieu of a mock so tests can assert against the action vocabulary
    (e.g. `("cloned", "demo-repo")`) without coupling to call ordering.
    """

    def __init__(self) -> None:
        self.targets_started: list[str] = []
        self.targets_completed: list[tuple[str, bool]] = []
        self.actions: list[tuple[str, str, str, str]] = []
        self.errors: list[tuple[str, str]] = []
        self.cmds_started: list[tuple[str, str]] = []
        self.cmd_output: list[tuple[str, str]] = []
        self.cmds_completed: list[tuple[str, str, int]] = []

    def target_started(self, target: str) -> None:
        self.targets_started.append(target)

    def target_completed(self, target: str, success: bool) -> None:
        self.targets_completed.append((target, success))

    def repo_action(self, repo: str, location: str, action: str, detail: str = "") -> None:
        self.actions.append((repo, location, action, detail))

    def repo_error(self, repo: str, error: str) -> None:
        self.errors.append((repo, error))

    def cmd_started(self, repo: str, command: str) -> None:
        self.cmds_started.append((repo, command))

    def cmd_output_line(self, repo: str, line: str) -> None:
        self.cmd_output.append((repo, line))

    def cmd_completed(self, repo: str, command: str, returncode: int) -> None:
        self.cmds_completed.append((repo, command, returncode))


@pytest.fixture
def click_recorder() -> ClickRecorder:
    """A drop-in for the `click` module — captures echo calls instead of writing."""
    return ClickRecorder()


class ClickRecorder:
    """Records `click.echo(message, err=...)` calls instead of writing them.

    DriftWarningService takes a `click` module via DI so output can be captured
    in tests. This recorder satisfies the `Any` type without dragging the real
    click side effects into the test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def echo(self, message: str, err: bool = False, **_: Any) -> None:
        self.calls.append((message, err))


class FakeFilesystem:
    """In-memory filesystem satisfying IFilesystemReader and IFilesystemWriter.

    Tracks files (paths → text content), directories (a set of paths), and
    symlinks (link path → target path). Models just enough behavior for the
    assertions services make: `is_dir`/`is_file`/`exists`, `iterdir`,
    `read_text`/`write_text`, symlinks, mkdir, append, unlink, rmtree.

    Hand-rolled rather than reaching for `pyfakefs` so the fake's behavior is
    legible right next to the tests that use it. Behavior gaps (rename, copy)
    are added on demand.
    """

    def __init__(
        self,
        files: dict[Path, str] | None = None,
        directories: Iterable[Path] = (),
        binary_files: dict[Path, bytes] | None = None,
        symlinks: dict[Path, Path] | None = None,
        executables: Iterable[Path] = (),
    ) -> None:
        self.files: dict[Path, str] = dict(files or {})
        self.binary_files: dict[Path, bytes] = dict(binary_files or {})
        self.directories: set[Path] = set(directories)
        self.symlinks: dict[Path, Path] = dict(symlinks or {})
        self.executables: set[Path] = set(executables)
        # Ensure parents of every seeded file/dir are known directories so
        # is_dir() works on intermediate paths without explicit listing.
        for p in list(self.files) + list(self.binary_files) + list(self.directories):
            for parent in p.parents:
                self.directories.add(parent)

    # ── IFilesystemReader ────────────────────────────────────────────────

    def exists(self, path: Path) -> bool:
        # Real `Path.exists()` follows symlinks — a broken symlink reports
        # False because its target doesn't exist. We mirror that so the
        # broken-symlink branches in prune/extensions work correctly.
        if path in self.symlinks:
            target = self.symlinks[path]
            if not target.is_absolute():
                target = (path.parent / target).resolve()
            return self.exists(target)
        return path in self.files or path in self.binary_files or path in self.directories

    def is_file(self, path: Path) -> bool:
        return path in self.files or path in self.binary_files

    def is_dir(self, path: Path) -> bool:
        return path in self.directories

    def is_symlink(self, path: Path) -> bool:
        return path in self.symlinks

    def iterdir(self, path: Path) -> list[Path]:
        results: set[Path] = set()
        for p in list(self.files) + list(self.binary_files) + list(self.directories) + list(self.symlinks):
            if p.parent == path and p != path:
                results.add(p)
        return sorted(results)

    def read_text(self, path: Path) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_bytes(self, path: Path) -> bytes:
        if path in self.binary_files:
            return self.binary_files[path]
        if path in self.files:
            return self.files[path].encode()
        raise FileNotFoundError(path)

    def readlink(self, path: Path) -> Path:
        if path not in self.symlinks:
            raise FileNotFoundError(path)
        return self.symlinks[path]

    def access_x_ok(self, path: Path) -> bool:
        return path in self.executables

    # ── IFilesystemWriter ────────────────────────────────────────────────

    def mkdir(self, path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        if path in self.directories and not exist_ok:
            raise FileExistsError(path)
        if not parents and path.parent not in self.directories and path.parent != path:
            raise FileNotFoundError(path.parent)
        self.directories.add(path)
        if parents:
            for parent in path.parents:
                self.directories.add(parent)

    def write_text(self, path: Path, data: str) -> None:
        self.files[path] = data
        # Drop any stale symlink or binary at the same path.
        self.binary_files.pop(path, None)
        self.symlinks.pop(path, None)
        for parent in path.parents:
            self.directories.add(parent)

    def append_lines(self, path: Path, lines: Iterable[str]) -> None:
        existing = self.files.get(path, "")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        suffix = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
        self.files[path] = existing + suffix

    def symlink_to(self, link_path: Path, target: Path) -> None:
        if link_path in self.files or link_path in self.binary_files or link_path in self.directories:
            raise FileExistsError(link_path)
        self.symlinks[link_path] = target
        for parent in link_path.parents:
            self.directories.add(parent)

    def unlink(self, path: Path) -> None:
        if path in self.symlinks:
            del self.symlinks[path]
            return
        if path in self.files:
            del self.files[path]
            return
        if path in self.binary_files:
            del self.binary_files[path]
            return
        raise FileNotFoundError(path)

    def rmtree(self, path: Path) -> None:
        self.directories.discard(path)
        for collection in (self.files, self.binary_files, self.symlinks):
            for p in list(collection):
                if p == path or path in p.parents:
                    del collection[p]
        for d in list(self.directories):
            if path in d.parents:
                self.directories.discard(d)

    def chmod(self, path: Path, mode: int) -> None:
        # Track executable bit: any mode with at least one execute bit sets the path executable.
        if mode & 0o111:
            self.executables.add(path)
        else:
            self.executables.discard(path)


class FakeConfigFileReader:
    """IConfigFileReader fake — returns canned dicts keyed by path.

    Raises `ConfigFileReadError` for paths registered as "broken" so error
    paths in services can be exercised. Unknown paths raise FileNotFoundError
    so callers that probe presence via the filesystem first are still
    correctly modeled (presence-then-load).
    """

    def __init__(
        self,
        files: dict[Path, dict] | None = None,
        broken: set[Path] | None = None,
    ) -> None:
        self.files: dict[Path, dict] = dict(files or {})
        self.broken: set[Path] = set(broken or ())

    def load(self, path: Path) -> dict:
        if path in self.broken:
            from winter_cli.core.config_file import ConfigFileReadError

            raise ConfigFileReadError(f"broken {path}")
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeStreamingProcess:
    """IStreamingProcess fake — yields canned lines, returns canned exit code."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self._lines = lines
        self._returncode = returncode

    @property
    def stdout_lines(self) -> Iterator[str]:
        yield from self._lines

    def wait(self) -> int:
        return self._returncode


class FakeSubprocessRunner:
    """ISubprocessRunner fake — records every invocation; canned responses.

    Tests register `(cmd_signature → SubprocessResult)` for `run`,
    `(cmd_signature → (lines, returncode))` for `popen`, and
    `(cmd_signature → returncode)` for `call`. The signature is the joined
    command for stability. Unknown commands raise so test accidentally-fanned-out
    subprocess work surfaces. `call` defaults to exit 0 for unregistered
    commands so passthrough-dispatch tests don't have to canned-register.
    """

    def __init__(
        self,
        run_responses: dict[str, SubprocessResult] | None = None,
        popen_responses: dict[str, tuple[list[str], int]] | None = None,
        call_responses: dict[str, int] | None = None,
    ) -> None:
        self._run_responses = dict(run_responses or {})
        self._popen_responses = dict(popen_responses or {})
        self._call_responses = dict(call_responses or {})
        self.run_calls: list[tuple[list[str], Path | None]] = []
        self.run_envs: list[Any] = []
        self.popen_calls: list[tuple[list[str] | str, Path | None]] = []
        self.popen_envs: list[Any] = []
        self.popen_merge_stderr: list[bool] = []
        self.call_calls: list[tuple[list[str], Path | None]] = []
        self.call_envs: list[Any] = []

    @staticmethod
    def _key(cmd: list[str] | str) -> str:
        return cmd if isinstance(cmd, str) else " ".join(cmd)

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Any = None,
    ) -> SubprocessResult:
        self.run_calls.append((list(cmd), cwd))
        self.run_envs.append(env)
        key = self._key(cmd)
        if key not in self._run_responses:
            raise AssertionError(f"FakeSubprocessRunner.run got unexpected command: {key!r}")
        return self._run_responses[key]

    def call(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Any = None,
    ) -> int:
        self.call_calls.append((list(cmd), cwd))
        self.call_envs.append(env)
        return self._call_responses.get(self._key(cmd), 0)

    @contextmanager
    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        env: Any = None,
        shell: bool = False,
        merge_stderr: bool = True,
    ) -> Iterator[FakeStreamingProcess]:
        self.popen_calls.append((cmd, cwd))
        self.popen_envs.append(env)
        self.popen_merge_stderr.append(merge_stderr)
        key = self._key(cmd)
        if key not in self._popen_responses:
            raise AssertionError(f"FakeSubprocessRunner.popen got unexpected command: {key!r}")
        lines, rc = self._popen_responses[key]
        yield FakeStreamingProcess(lines, rc)


class FakeSpecLoader:
    """ISpecLoader fake — returns a configurable set of supported versions per slot.

    Defaults to returning `{"v1"}` for every slot, matching what the real
    SpecLoader returns for the shipped `service-v1.toml`. Tests that exercise
    version-compat paths can override `supported` for specific slots.

    `load()` always raises NotImplementedError — unit tests that only need
    version-compat checking never call it.
    """

    def __init__(self, supported: dict[str, set[str]] | None = None) -> None:
        self._supported: dict[str, set[str]] = supported or {}

    def supported_versions(self, slot: str) -> set[str]:
        return self._supported.get(slot, {"v1"})

    def load(self, slot: str, version: str) -> Any:
        raise NotImplementedError("FakeSpecLoader.load not implemented")


class FakeGitRepository:
    """IGitRepository fake — records every mutation, returns canned reads.

    Defaults match the most common happy-path: branch="alpha", worktree is
    clean, no upstream set. Tests override per-path state by mutating the
    public dicts directly before constructing the service.
    """

    def __init__(self) -> None:
        # Read state (path → value).
        self.local_branches: dict[Path, list[str]] = {}
        self.tracking_branches: dict[Path, str | None] = {}
        self.worktree_paths: dict[Path, list[Path]] = {}
        self.push_defaults: dict[Path, str | None] = {}
        self.clean_worktrees: set[Path] = set()  # paths considered clean

        # Mutation log — assertion targets.
        self.clones: list[tuple[str, Path]] = []
        self.added_worktrees: list[tuple[Path, Path, str, str | None]] = []
        self.removed_worktrees: list[tuple[Path, Path, bool]] = []
        self.identities: list[tuple[Path, str, str]] = []
        self.upstreams_set: list[tuple[Path, str]] = []
        self.push_default_set: list[Path] = []

    # ── Reads ────────────────────────────────────────────────────────────
    def get_local_branches(self, path: Path) -> list[str]:
        return list(self.local_branches.get(path, []))

    def get_tracking_branch(self, path: Path) -> str | None:
        return self.tracking_branches.get(path)

    def list_worktrees(self, source: Path) -> list[Path]:
        return list(self.worktree_paths.get(source, []))

    def get_push_default(self, path: Path) -> str | None:
        return self.push_defaults.get(path)

    def is_worktree_clean(self, path: Path) -> bool:
        return path in self.clean_worktrees

    # ── Writes ───────────────────────────────────────────────────────────
    def clone(self, url: str, dest: Path) -> None:
        self.clones.append((url, dest))

    def add_worktree(self, source: Path, worktree_path: Path, branch: str, base_branch: str | None = None) -> None:
        self.added_worktrees.append((source, worktree_path, branch, base_branch))

    def remove_worktree(self, source: Path, worktree_path: Path, force: bool) -> None:
        self.removed_worktrees.append((source, worktree_path, force))

    def set_user_identity(self, path: Path, name: str, email: str) -> None:
        self.identities.append((path, name, email))

    def set_upstream_to(self, path: Path, ref: str) -> None:
        self.upstreams_set.append((path, ref))

    def set_push_default_upstream(self, path: Path) -> None:
        self.push_default_set.append(path)
