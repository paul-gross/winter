"""Tests for EnvBandsProbeService: `[env.<name>.vars]` bands naming an unknown env.

Covers:
  1. No named bands declared → no probe results (silent, like the provision probe).
  2. A named band whose env is registered (via the registry) → no warning.
  3. A named band whose env has an on-disk env directory but no registry entry
     (registry is None, or the registry simply lacks the entry) → no warning.
  4. A named band naming an env that is neither registered nor on disk → WARN
     (never FAIL — an unknown-env band is inert by design, not an error).
  5. A legitimately-named non-alias env (hashed into the index band) with a
     matching band → no false positive; `env_aliases` is never consulted.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from tests.conftest import FakeFilesystem, make_workspace_config
from winter_cli.config.models import EnvBandValue, EnvVarBands, WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.env_bands_probe_service import ENV_BANDS_SOURCE, EnvBandsProbeService
from winter_cli.modules.doctor.env_discovery_service import EnvDiscoveryService
from winter_cli.modules.doctor.models import ProbeStatus

WORKSPACE_ROOT = Path("/ws")


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------


class _InMemoryRegistry:
    def __init__(self) -> None:
        self._data: dict[str, int] = {}

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _config(named: dict[str, dict[str, EnvBandValue]] | None = None) -> WorkspaceConfig:
    return make_workspace_config(
        workspace_root=WORKSPACE_ROOT,
        env_aliases=["alpha", "beta"],
        envs_per_workspace=48,
        env_bands=EnvVarBands(named=named or {}),
    )


def _svc(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    registry: _InMemoryRegistry | None,
) -> EnvBandsProbeService:
    return EnvBandsProbeService(
        config=config,
        registry=registry,
        env_discovery=EnvDiscoveryService(cast(IFilesystemReader, fs)),
    )


def _empty_fs() -> FakeFilesystem:
    return FakeFilesystem(directories={WORKSPACE_ROOT})


def _env_fs(*env_names: str) -> FakeFilesystem:
    """Build a FakeFilesystem with env directories (each with a git-worktree child)."""
    files: dict[Path, str] = {}
    directories = {WORKSPACE_ROOT}
    for name in env_names:
        env_dir = WORKSPACE_ROOT / name
        directories.add(env_dir)
        worktree_dir = env_dir / "my-repo"
        directories.add(worktree_dir)
        files[worktree_dir / ".git"] = f"gitdir: ../../projects/my-repo/.git/worktrees/{name}\n"
    return FakeFilesystem(files=files, directories=directories)


class TestNoNamedBands:
    def test_returns_empty_when_no_named_bands(self) -> None:
        cfg = _config(named={})
        svc = _svc(cfg, _empty_fs(), registry=None)

        assert svc.run() == []


class TestKnownEnv:
    def test_no_warning_when_env_is_registered(self) -> None:
        """A band naming a registered env (even with no on-disk dir) is not flagged."""
        registry = _InMemoryRegistry()
        registry.assign("alpha", 1)
        cfg = _config(named={"alpha": {"FOO": "bar"}})
        svc = _svc(cfg, _empty_fs(), registry)

        assert svc.run() == []

    def test_no_warning_when_env_dir_exists_on_disk(self) -> None:
        """A band naming an env with an on-disk dir is not flagged, even with no registry."""
        cfg = _config(named={"alpha": {"FOO": "bar"}})
        fs = _env_fs("alpha")
        svc = _svc(cfg, fs, registry=None)

        assert svc.run() == []

    def test_no_warning_for_hashed_non_alias_env(self) -> None:
        """A legitimately-named non-alias env (hash band) is not flagged just because
        it is not in env_aliases — env_aliases is never consulted."""
        registry = _InMemoryRegistry()
        registry.assign("my-feature", 12)
        cfg = _config(named={"my-feature": {"FOO": "bar"}})
        svc = _svc(cfg, _empty_fs(), registry)

        assert svc.run() == []


class TestUnknownEnv:
    def test_warns_when_env_is_neither_registered_nor_on_disk(self) -> None:
        registry = _InMemoryRegistry()
        registry.assign("alpha", 1)
        cfg = _config(named={"alfa": {"FOO": "bar"}})
        svc = _svc(cfg, _env_fs("alpha"), registry)

        results = svc.run()

        assert len(results) == 1
        result = results[0]
        assert result.status == ProbeStatus.warn
        assert result.source == ENV_BANDS_SOURCE
        assert "alfa" in result.name
        assert result.remediation is not None
        assert "typo" in result.remediation

    def test_warns_with_no_registry_and_no_matching_dir(self) -> None:
        cfg = _config(named={"ghost": {"FOO": "bar"}})
        svc = _svc(cfg, _empty_fs(), registry=None)

        results = svc.run()

        assert len(results) == 1
        assert results[0].status == ProbeStatus.warn

    def test_never_fails(self) -> None:
        """An unknown-env band is inert by design — the probe must never FAIL."""
        cfg = _config(named={"ghost": {"FOO": "bar"}})
        svc = _svc(cfg, _empty_fs(), registry=None)

        results = svc.run()

        assert all(r.status != ProbeStatus.fail for r in results)

    def test_multiple_unknown_bands_each_warn(self) -> None:
        cfg = _config(named={"ghost": {"FOO": "1"}, "phantom": {"BAR": "2"}})
        svc = _svc(cfg, _empty_fs(), registry=None)

        results = svc.run()

        names = {r.name for r in results}
        assert len(results) == 2
        assert all(r.status == ProbeStatus.warn for r in results)
        assert "env band: ghost" in names
        assert "env band: phantom" in names

    def test_mixed_known_and_unknown_bands(self) -> None:
        registry = _InMemoryRegistry()
        registry.assign("alpha", 1)
        cfg = _config(named={"alpha": {"FOO": "bar"}, "ghost": {"BAZ": "qux"}})
        svc = _svc(cfg, _empty_fs(), registry)

        results = svc.run()

        assert len(results) == 1
        assert results[0].name == "env band: ghost"
