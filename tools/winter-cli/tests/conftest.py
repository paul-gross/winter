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
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind


def make_workspace_config(**overrides: Any) -> WorkspaceConfig:
    """Build a `WorkspaceConfig` with sane required-field defaults for tests.

    Tests that just need *a* config object (not one exercising any particular
    field) can call this instead of repeating the full constructor. Supplies
    `workspace_root` and `main_branch` — a required field it is easy to forget
    when hand-rolling a `WorkspaceConfig` — plus a default `service_prefix`.
    Pass keyword overrides to customize any field, e.g.
    `make_workspace_config(doctor="context/project/doctor.sh")`.
    """
    defaults: dict[str, Any] = {
        "workspace_root": Path("/ws"),
        "service_prefix": "t",
        "main_branch": "main",
    }
    defaults.update(overrides)
    return WorkspaceConfig(**defaults)


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
            service_prefix = "test"

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
        service_prefix="test",
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

    def style(self, text: str, **_: Any) -> str:
        return text


class FakeServiceReporter:
    """In-memory reporter that records every IServiceReporter event for assertion."""

    def __init__(self) -> None:
        self.status_documents: list[tuple[Any, Any]] = []
        self.log_lines: list[str] = []
        self.no_services_called: int = 0
        self.no_service_matched_calls: list[str] = []
        self.follow_multi_provider_error_calls: list[str] = []
        self.status_parse_error_calls: list[tuple[str, str, str]] = []
        self.describe_parse_error_calls: list[tuple[str, str]] = []
        self.timestamps_warning_called: int = 0
        self.time_filter_warning_called: int = 0
        self.no_match_diagnostic_calls: list[str] = []
        self.readiness_timeout_calls: list[tuple[str, float, tuple[str, ...]]] = []
        self.env_provision_error_calls: list[tuple[str, str]] = []

    def status_document(self, doc: Any, parser: Any) -> None:
        self.status_documents.append((doc, parser))

    def log_line(self, rendered: str) -> None:
        self.log_lines.append(rendered)

    def no_services(self) -> None:
        self.no_services_called += 1

    def no_service_matched(self, token_list: str) -> None:
        self.no_service_matched_calls.append(token_list)

    def follow_multi_provider_error(self, provider_names: str) -> None:
        self.follow_multi_provider_error_calls.append(provider_names)

    def status_parse_error(self, entrypoint: str, prefix: str, detail: str) -> None:
        self.status_parse_error_calls.append((entrypoint, prefix, detail))

    def describe_parse_error(self, provider_name: str, detail: str) -> None:
        self.describe_parse_error_calls.append((provider_name, detail))

    def env_provision_error(self, scope: str, detail: str) -> None:
        self.env_provision_error_calls.append((scope, detail))

    def timestamps_warning(self) -> None:
        self.timestamps_warning_called += 1

    def time_filter_warning(self) -> None:
        self.time_filter_warning_called += 1

    def no_match_diagnostic(self, token_list: str) -> None:
        self.no_match_diagnostic_calls.append(token_list)

    def readiness_timeout(self, env: str, timeout_s: float, unhealthy: tuple[str, ...]) -> None:
        self.readiness_timeout_calls.append((env, timeout_s, unhealthy))


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

    def copytree(self, src: Path, dst: Path) -> None:
        # Mirror shutil.copytree: dst must not already exist. Replicate every
        # directory, file, binary, and symlink rooted at src under dst so a
        # subsequent content hash of dst matches src.
        if dst in self.directories or dst in self.files or dst in self.binary_files:
            raise FileExistsError(dst)
        self.directories.add(dst)
        for parent in dst.parents:
            self.directories.add(parent)
        for d in list(self.directories):
            if src in d.parents:
                self.directories.add(dst / d.relative_to(src))
        for p, text in list(self.files.items()):
            if src in p.parents:
                self.files[dst / p.relative_to(src)] = text
        for p, blob in list(self.binary_files.items()):
            if src in p.parents:
                self.binary_files[dst / p.relative_to(src)] = blob
        for p, target in list(self.symlinks.items()):
            if src in p.parents:
                self.symlinks[dst / p.relative_to(src)] = target

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

    Phase-3 additions:
      ``resolved_refs``   — canned (RefKind, sha) per (path, ref); resolve_ref raises RepoError on miss.
      ``head_commits``    — canned HEAD SHA per path; get_head_commit raises RepoError on miss.
      ``detached_checkouts`` / ``branch_checkouts`` — mutation logs for checkout_detached / checkout_branch.
    """

    def __init__(self) -> None:
        # Read state (path → value).
        self.local_branches: dict[Path, list[str]] = {}
        self.tracking_branches: dict[Path, str | None] = {}
        self.worktree_paths: dict[Path, list[Path]] = {}
        self.push_defaults: dict[Path, str | None] = {}
        self.clean_worktrees: set[Path] = set()  # paths considered clean

        # Phase-3 read state.
        self.resolved_refs: dict[tuple[Path, str], tuple[RefKind, str]] = {}
        self.head_commits: dict[Path, str] = {}

        # Mutation log — assertion targets.
        self.clones: list[tuple[str, Path]] = []
        self.added_worktrees: list[tuple[Path, Path, str, str | None]] = []
        self.removed_worktrees: list[tuple[Path, Path, bool]] = []
        self.identities: list[tuple[Path, str, str]] = []
        self.upstreams_set: list[tuple[Path, str]] = []
        self.push_default_set: list[Path] = []

        # Phase-3 mutation logs.
        self.detached_checkouts: list[tuple[Path, str]] = []
        self.branch_checkouts: list[tuple[Path, str]] = []

        # Phase-6 stash mutation logs.
        self.stash_pushes: list[Path] = []
        self.stash_pops: list[Path] = []

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

    # Phase-3 reads.
    def resolve_ref(self, path: Path, ref: str) -> tuple[RefKind, str]:
        key = (path, ref)
        if key not in self.resolved_refs:
            raise RepoError(
                f"unresolvable ref {ref!r} at {path}: not a branch, tag, or commit SHA",
                cwd=str(path),
            )
        return self.resolved_refs[key]

    def get_head_commit(self, path: Path) -> str:
        if path not in self.head_commits:
            raise RepoError(f"rev-parse HEAD failed at {path}", cwd=str(path))
        return self.head_commits[path]

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

    # Phase-3 writes.
    def checkout_detached(self, path: Path, commit: str) -> None:
        self.detached_checkouts.append((path, commit))

    def checkout_branch(self, path: Path, branch: str) -> None:
        self.branch_checkouts.append((path, branch))

    # Phase-6 stash writes.
    def stash_push(self, path: Path) -> None:
        self.stash_pushes.append(path)

    def stash_pop(self, path: Path) -> None:
        self.stash_pops.append(path)


class FakeEnvIndexRegistry:
    """IEnvIndexRegistry fake — in-memory index store for unit tests.

    Pre-seed ``assignments`` before constructing the service under test; assert on
    ``assignments`` and ``removed`` after the service runs.
    """

    def __init__(self, assignments: dict[str, int] | None = None) -> None:
        self.assignments: dict[str, int] = dict(assignments or {})
        self.removed: list[str] = []

    def get_index(self, name: str) -> int | None:
        return self.assignments.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self.assignments)

    def assign(self, name: str, index: int) -> None:
        self.assignments[name] = index

    def remove(self, name: str) -> None:
        self.assignments.pop(name, None)
        self.removed.append(name)


class FakeConfigLockRepository:
    """IConfigLockRepository fake — in-memory lock store for unit tests.

    Pre-seed ``entries`` before constructing the service under test; assert on
    ``entries`` and ``write_calls`` after the service runs.
    ``write_calls`` records every ``write(entries)`` invocation as a snapshot
    dict so tests can assert the exact state written without coupling to call
    ordering within the service.
    """

    def __init__(self, entries: dict[str, LockEntry] | None = None) -> None:
        self.entries: dict[str, LockEntry] = dict(entries or {})
        self.write_calls: list[dict[str, LockEntry]] = []

    def read(self) -> dict[str, LockEntry]:
        return dict(self.entries)

    def write(self, entries: Iterable[LockEntry]) -> None:
        snapshot = {e.name: e for e in entries}
        self.entries = snapshot
        self.write_calls.append(dict(snapshot))

    def upsert(self, entry: LockEntry) -> None:
        # Mirror the real adapter: read-merge-write, preserving other entries.
        merged = dict(self.entries)
        merged[entry.name] = entry
        self.write(merged.values())
