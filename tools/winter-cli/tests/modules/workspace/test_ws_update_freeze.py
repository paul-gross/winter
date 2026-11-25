"""`winter ws update --freeze [REPOS]...` — pin every unpinned standalone to its checkout.

Covers:
  - all repos unpinned → each gets `ref = <full HEAD sha>` and a lock entry
  - a mix of pinned and unpinned → pinned ones reported `already_pinned`, left untouched
  - a dirty repo → `refused` naming the repo; `--force` pins it anyway
  - a REPOS glob / literal name → only the matches are considered
  - an unknown literal name raises; a glob matching nothing is a no-op
  - a repo declared only in config.local.toml is written there
  - a missing checkout or a directory that is not a git repo is refused (with or without
    `--force`) and the run carries on with the later repos
  - the real config writer over a fake filesystem: a URL-derived name, and a repo declared
    only in config.local.toml leaving config.toml untouched
  - handler routes `--freeze`/`--force`; the CLI rejects meaningless flag combinations
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Collection
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from tests.conftest import FakeConfigLockRepository, FakeFilesystem, FakeGitRepository
from winter_cli.config.internal.write_winter_configuration_repository import WriteWinterConfigurationRepository
from winter_cli.config.models import (
    AdoptExtensions,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
from winter_cli.modules.workspace.command import ws_update
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.handlers.workspace_handler import EnvUpdateParams, WorkspaceHandler
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.gitpython_repository import GitPythonRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    PullReport,
    RepoError,
    SyncResult,
    Workspace,
)
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_PIN = "d" * 40


class _NoRepoRepository:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"IWriteRepoRepository.{name} called unexpectedly — freeze touches no remote")


class _NoWorktreeRepository:
    def get_environments(self, workspace, project_repos):  # type: ignore[no-untyped-def]
        return []


class FakeWriteConfigRepository:
    """Records `set_standalone_ref` calls; `shared` / `local` name the repos each file declares."""

    def __init__(self, shared: set[str], local: set[str] | None = None) -> None:
        self.shared = shared
        self.local = local or set()
        self.refs: dict[tuple[str, bool], str] = {}

    def set_standalone_ref(self, name: str, ref: str, local: bool = False) -> bool:
        if name not in (self.local if local else self.shared):
            return False
        self.refs[(name, local)] = ref
        return True

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"IWriteWinterConfigurationRepository.{name} called unexpectedly")


class RecordingReporter:
    def __init__(self) -> None:
        self.synced: list[tuple[str, SyncResult, str]] = []
        self.completed_success: bool | None = None

    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def source_checkout_warning(self, repo_name: str, warning: str) -> None:
        return None

    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None:
        assert scope_label == "standalone"
        self.synced.append((repo_name, result, pin_ref))

    def pull_completed(self, success: bool) -> None:
        self.completed_success = success


class Harness:
    """A service wired to fakes, with one on-disk directory per declared standalone."""

    def __init__(
        self,
        root: Path,
        configs: list[StandaloneRepositoryConfig],
        *,
        heads: dict[str, str],
        dirty: Collection[str] = (),
        local_declared: set[str] | None = None,
        missing: Collection[str] = (),
        git_repo: Any = None,
        config_writer: Any = None,
    ) -> None:
        self.fs: FakeFilesystem
        self.git = FakeGitRepository()
        for config in configs:
            name = config.name or _derived_name(config)
            path = root / name
            if name in missing:
                continue
            path.mkdir()
            self.git.head_commits[path] = heads[name]
            if name not in dirty:
                self.git.clean_worktrees.add(path)
        local = local_declared or set()
        self.config_writer = config_writer or FakeWriteConfigRepository(
            shared={c.name for c in configs if c.name and c.name not in local}, local=local
        )
        self.lock = FakeConfigLockRepository()
        self.reporter = RecordingReporter()
        workspace_config = WorkspaceConfig(
            workspace_root=root,
            service_prefix="t",
            main_branch="main",
            adopt_extensions=AdoptExtensions.winter,
            standalone_repos=configs,
        )
        self.service = WorkspaceSyncService(
            env_status_svc=EnvStatusService(
                worktree_repo=_NoWorktreeRepository(),  # type: ignore[arg-type]
                repo_repo=_NoRepoRepository(),  # type: ignore[arg-type]
            ),
            worktree_repo=_NoWorktreeRepository(),  # type: ignore[arg-type]
            repo_repo=_NoRepoRepository(),  # type: ignore[arg-type]
            repo_factory=RepositoryFactory(workspace_config),
            workspace=Workspace(root_path=root, service_prefix="t", main_branch="main"),
            git_ops=GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0),
            git_repo=git_repo or self.git,
            config_lock_repo=self.lock,  # type: ignore[arg-type]
            write_config_repo=self.config_writer,  # type: ignore[arg-type]
        )

    def freeze(self, patterns: list[str] | None = None, force: bool = False) -> PullReport:
        return self.service.freeze_pins(repo_patterns=patterns or [], force=force, reporter=self.reporter)


def _derived_name(config: StandaloneRepositoryConfig) -> str:
    assert config.url is not None
    return config.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _cfg(name: str, ref: str | None = None) -> StandaloneRepositoryConfig:
    return StandaloneRepositoryConfig(name=name, ref=ref)


# ── Service-level tests ────────────────────────────────────────────────────────


def test_freeze_all_unpinned_pins_each_to_its_head_and_writes_lock(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a"), _cfg("lib-b")], heads={"lib-a": SHA_A, "lib-b": SHA_B})

    report = h.freeze()

    assert h.config_writer.refs == {("lib-a", False): SHA_A, ("lib-b", False): SHA_B}
    assert h.lock.entries == {
        "lib-a": LockEntry(name="lib-a", ref=SHA_A, kind=RefKind.commit, commit=SHA_A),
        "lib-b": LockEntry(name="lib-b", ref=SHA_B, kind=RefKind.commit, commit=SHA_B),
    }
    assert h.reporter.synced == [
        ("lib-a", SyncResult.pinned, SHA_A[:8]),
        ("lib-b", SyncResult.pinned, SHA_B[:8]),
    ]
    assert [o.sync_result for o in report.standalone] == [SyncResult.pinned, SyncResult.pinned]
    assert report.success is True
    assert h.reporter.completed_success is True
    # Pinning describes the checkout: no checkout, no fetch, no network.
    assert h.git.detached_checkouts == [] and h.git.branch_checkouts == []


def test_freeze_mixed_leaves_already_pinned_repos_alone(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("lib-a", ref="v1.0"), _cfg("lib-b"), _cfg("lib-c", ref="main")],
        heads={"lib-a": SHA_A, "lib-b": SHA_B, "lib-c": SHA_C},
    )
    existing = LockEntry(name="lib-a", ref="v1.0", kind=RefKind.tag, commit=SHA_PIN)
    h.lock.entries["lib-a"] = existing

    report = h.freeze()

    assert h.config_writer.refs == {("lib-b", False): SHA_B}
    assert h.lock.entries["lib-a"] == existing
    assert "lib-c" not in h.lock.entries
    assert [(o.repo_name, o.sync_result) for o in report.standalone] == [
        ("lib-a", SyncResult.already_pinned),
        ("lib-b", SyncResult.pinned),
        ("lib-c", SyncResult.already_pinned),
    ]
    assert h.reporter.synced[0] == ("lib-a", SyncResult.already_pinned, "v1.0")
    assert report.success is True


def test_freeze_everything_already_pinned_writes_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a", ref="v1.0")], heads={"lib-a": SHA_A})

    report = h.freeze()

    assert h.config_writer.refs == {}
    assert h.lock.write_calls == []
    assert report.success is True


def test_freeze_dirty_repo_is_refused_naming_the_repo_and_others_still_pin(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("lib-a"), _cfg("lib-b")],
        heads={"lib-a": SHA_A, "lib-b": SHA_B},
        dirty={"lib-a"},
    )

    report = h.freeze()

    assert h.config_writer.refs == {("lib-b", False): SHA_B}
    assert "lib-a" not in h.lock.entries
    refused = report.standalone[0]
    assert refused.sync_result is SyncResult.refused
    assert "'lib-a'" in refused.pin_ref and "--force" in refused.pin_ref
    assert report.standalone[1].sync_result is SyncResult.pinned
    assert report.success is False
    assert h.reporter.completed_success is False


def test_freeze_dirty_repo_with_force_is_pinned(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a")], heads={"lib-a": SHA_A}, dirty={"lib-a"})

    report = h.freeze(force=True)

    assert h.config_writer.refs == {("lib-a", False): SHA_A}
    assert h.lock.entries["lib-a"].commit == SHA_A
    assert report.standalone[0].sync_result is SyncResult.pinned
    assert report.success is True


def test_freeze_glob_only_considers_matching_repos(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("winter-a"), _cfg("winter-b"), _cfg("other")],
        heads={"winter-a": SHA_A, "winter-b": SHA_B, "other": SHA_C},
    )

    report = h.freeze(["winter-*"])

    assert h.config_writer.refs == {("winter-a", False): SHA_A, ("winter-b", False): SHA_B}
    assert [o.repo_name for o in report.standalone] == ["winter-a", "winter-b"]
    assert "other" not in h.lock.entries


def test_freeze_literal_names_pin_exactly_those_repos(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("lib-a"), _cfg("lib-b"), _cfg("lib-c")],
        heads={"lib-a": SHA_A, "lib-b": SHA_B, "lib-c": SHA_C},
    )

    h.freeze(["lib-a", "lib-c"])

    assert set(h.config_writer.refs) == {("lib-a", False), ("lib-c", False)}


def test_freeze_literal_naming_an_already_pinned_repo_reports_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a", ref="v1.0")], heads={"lib-a": SHA_A})

    report = h.freeze(["lib-a"])

    assert report.standalone[0].sync_result is SyncResult.already_pinned


def test_freeze_unknown_literal_name_raises(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a")], heads={"lib-a": SHA_A})

    with pytest.raises(RepoError, match="no standalone repo named 'nope'"):
        h.freeze(["nope"])

    assert h.config_writer.refs == {}


def test_freeze_glob_matching_nothing_is_a_noop(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a")], heads={"lib-a": SHA_A})

    report = h.freeze(["zzz-*"])

    assert report.standalone == []
    assert h.config_writer.refs == {}
    assert report.success is True


def test_freeze_writes_to_local_overlay_when_repo_is_declared_there(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("lib-a"), _cfg("lib-b")],
        heads={"lib-a": SHA_A, "lib-b": SHA_B},
        local_declared={"lib-b"},
    )

    h.freeze()

    assert h.config_writer.refs == {("lib-a", False): SHA_A, ("lib-b", True): SHA_B}


def test_freeze_pins_a_commit_that_exists_only_locally(tmp_path: Path) -> None:
    """No remote-reachability check: HEAD is pinned whether or not origin has it."""
    h = Harness(tmp_path, [_cfg("lib-a")], heads={"lib-a": SHA_A})

    report = h.freeze()

    assert report.standalone[0].sync_result is SyncResult.pinned
    assert h.git.repo_names_for("get_head_commit") == ["lib-a"]
    assert {env for _m, _p, env in h.git.env_calls} == {None}


def test_freeze_unreadable_head_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path, [_cfg("lib-a")], heads={"lib-a": SHA_A})
    h.git.head_commits.clear()

    report = h.freeze()

    assert report.standalone[0].sync_result is SyncResult.refused
    assert h.config_writer.refs == {}
    assert report.success is False


def test_freeze_missing_checkout_is_refused_as_not_cloned_and_run_continues(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [_cfg("lib-a"), _cfg("lib-b")],
        heads={"lib-a": SHA_A, "lib-b": SHA_B},
        missing={"lib-a"},
    )

    report = h.freeze()

    refused, pinned = report.standalone
    assert refused.sync_result is SyncResult.refused
    assert "'lib-a'" in refused.pin_ref and "not cloned" in refused.pin_ref
    assert pinned.sync_result is SyncResult.pinned
    assert h.config_writer.refs == {("lib-b", False): SHA_B}
    assert report.success is False


def _git_init_with_commit(path: Path) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": os.environ["PATH"],
        "HOME": str(path),
    }
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("force", [False, True])
def test_freeze_directory_that_is_not_a_git_repo_is_refused_and_run_continues(tmp_path: Path, force: bool) -> None:
    """A real adapter over a plain directory: refused for what it is — not for being "dirty" —
    under either flag, and the later repo is still pinned."""
    h = Harness(
        tmp_path,
        [_cfg("plain-dir"), _cfg("real-repo")],
        heads={"plain-dir": SHA_A, "real-repo": SHA_B},
        git_repo=GitPythonRepository(RepoErrorFactory(), NoopCommandTracer()),
    )
    head = _git_init_with_commit(tmp_path / "real-repo")

    report = h.freeze(force=force)

    refused, pinned = report.standalone
    assert refused.sync_result is SyncResult.refused
    assert "uncommitted" not in refused.pin_ref
    assert "not a git repository" in refused.pin_ref
    assert pinned.sync_result is SyncResult.pinned
    assert h.config_writer.refs == {("real-repo", False): head}
    assert "plain-dir" not in h.lock.entries
    assert report.success is False


# ── Real config writer over a fake filesystem ─────────────────────────────────


def _real_writer_harness(
    tmp_path: Path,
    files: dict[Path, str],
    configs: list[StandaloneRepositoryConfig],
    heads: dict[str, str],
) -> Harness:
    fs = FakeFilesystem(files=files)
    workspace_config = WorkspaceConfig(
        workspace_root=tmp_path,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
    )
    h = Harness(
        tmp_path,
        configs,
        heads=heads,
        config_writer=WriteWinterConfigurationRepository(workspace_config, fs=fs),
    )
    h.fs = fs
    return h


def test_freeze_with_the_real_config_writer_pins_a_url_derived_name(tmp_path: Path) -> None:
    shared_path = tmp_path / ".winter" / "config.toml"
    shared = (
        'main_branch = "main"\n'
        "\n"
        "[[standalone_repository]]\n"
        'url = "git@example.com:org/derived-lib.git"  # name comes from the url\n'
    )
    h = _real_writer_harness(
        tmp_path,
        {shared_path: shared},
        [StandaloneRepositoryConfig(url="git@example.com:org/derived-lib.git")],
        {"derived-lib": SHA_A},
    )

    report = h.freeze()

    assert [o.sync_result for o in report.standalone] == [SyncResult.pinned]
    assert h.fs.files[shared_path] == shared.replace(
        "name comes from the url\n", f'name comes from the url\nref = "{SHA_A}"\n'
    )
    assert h.lock.entries["derived-lib"].commit == SHA_A


def test_freeze_with_the_real_config_writer_pins_a_local_only_repo_and_leaves_config_toml_untouched(
    tmp_path: Path,
) -> None:
    shared_path = tmp_path / ".winter" / "config.toml"
    local_path = tmp_path / ".winter" / "config.local.toml"
    shared = 'main_branch = "main"\n\n[[standalone_repository]]\nname = "shared-lib"\nref = "v1.0"\n'
    local = '[[standalone_repository]]\nname = "local-lib"\nurl = "git@example.com:org/local-lib.git"\n'
    h = _real_writer_harness(
        tmp_path,
        {shared_path: shared, local_path: local},
        [_cfg("shared-lib", ref="v1.0"), _cfg("local-lib")],
        {"shared-lib": SHA_A, "local-lib": SHA_B},
    )

    report = h.freeze()

    assert [o.sync_result for o in report.standalone] == [SyncResult.already_pinned, SyncResult.pinned]
    assert h.fs.files[shared_path] == shared
    assert h.fs.files[local_path] == local + f'ref = "{SHA_B}"\n'
    assert h.lock.entries["local-lib"].commit == SHA_B


# ── Handler + CLI layer ────────────────────────────────────────────────────────


def _handler(sync_svc: Any, reporter: Any) -> WorkspaceHandler:
    from unittest.mock import MagicMock

    reporter_factory = MagicMock()
    reporter_factory.get_pull_reporter.return_value = reporter
    return WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        env_reset_svc=MagicMock(),
        env_clean_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=MagicMock(),
        workspace=MagicMock(),
    )


def test_handler_routes_freeze_to_freeze_pins() -> None:
    from unittest.mock import MagicMock

    sync_svc = MagicMock()
    sync_svc.freeze_pins.return_value = PullReport(envs=[], standalone=[], skipped=[])
    reporter = RecordingReporter()

    _handler(sync_svc, reporter).update(
        EnvUpdateParams(repos=["lib-*"], autostash=False, output_json=False, freeze=True, force=True)
    )

    sync_svc.freeze_pins.assert_called_once_with(repo_patterns=["lib-*"], force=True, reporter=reporter)
    sync_svc.update_pins.assert_not_called()


def test_handler_exits_nonzero_when_a_repo_is_refused() -> None:
    from unittest.mock import MagicMock

    from winter_cli.modules.workspace.models import RepoSyncOutcome

    sync_svc = MagicMock()
    sync_svc.freeze_pins.return_value = PullReport(
        envs=[], standalone=[RepoSyncOutcome(repo_name="lib-a", sync_result=SyncResult.refused)], skipped=[]
    )

    with pytest.raises(SystemExit) as exc:
        _handler(sync_svc, RecordingReporter()).update(
            EnvUpdateParams(repos=[], autostash=False, output_json=True, freeze=True)
        )
    assert exc.value.code == 1


def test_handler_surfaces_freeze_repo_error_as_click_exception() -> None:
    from unittest.mock import MagicMock

    sync_svc = MagicMock()
    sync_svc.freeze_pins.side_effect = RepoError("no standalone repo named 'x'", cwd="")

    with pytest.raises(click.ClickException, match="no standalone repo named 'x'"):
        _handler(sync_svc, RecordingReporter()).update(
            EnvUpdateParams(repos=["x"], autostash=False, output_json=False, freeze=True)
        )


def test_ws_update_help_documents_freeze_and_force() -> None:
    result = CliRunner().invoke(ws_update, ["--help"])

    assert result.exit_code == 0
    assert "--freeze" in result.output
    assert "--force" in result.output


def test_ws_update_force_without_freeze_is_a_usage_error() -> None:
    result = CliRunner().invoke(ws_update, ["--force"])

    assert result.exit_code == 2
    assert "--force only applies with --freeze" in result.output


def test_ws_update_freeze_with_autostash_is_a_usage_error() -> None:
    result = CliRunner().invoke(ws_update, ["--freeze", "--autostash"])

    assert result.exit_code == 2
    assert "--autostash does not apply with --freeze" in result.output
