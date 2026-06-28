from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from winter_cli.cli import _cli_group


def _make_workspace(root: Path, config_toml: str = 'main_branch = "master"\n') -> Path:
    """Create a minimal winter workspace (just `.winter/config.toml`) at *root*."""
    (root / ".winter").mkdir(parents=True)
    (root / ".winter" / "config.toml").write_text(config_toml)
    return root


def test_space_prints_default_path_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _make_workspace(tmp_path / "ws")
    monkeypatch.chdir(ws)

    result = CliRunner().invoke(_cli_group, ["space", "scores"])

    assert result.exit_code == 0, result.output
    expected = ws / ".winter" / "scores"
    assert Path(result.output.strip()) == expected
    # Pure resolution: the command creates nothing and writes no ignore file.
    assert not expected.exists()
    assert not (expected / ".gitignore").exists()


def test_space_honors_root_and_kind_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _make_workspace(
        tmp_path / "ws",
        'main_branch = "master"\n[space]\nroot = ".artifacts"\n[space.kinds]\nscores = "audits"\n',
    )
    monkeypatch.chdir(ws)

    result = CliRunner().invoke(_cli_group, ["space", "scores"])

    assert result.exit_code == 0, result.output
    assert Path(result.output.strip()) == ws / ".artifacts" / "audits"


def test_space_resolves_absolute_kind_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside" / "logs"
    ws = _make_workspace(
        tmp_path / "ws",
        f'main_branch = "master"\n[space.kinds]\nlogs = "{outside}"\n',
    )
    monkeypatch.chdir(ws)

    result = CliRunner().invoke(_cli_group, ["space", "logs"])

    assert result.exit_code == 0, result.output
    assert Path(result.output.strip()) == outside


@pytest.mark.parametrize("bad_kind", ["../etc", "a/b", ".", "..", ".hidden"])
def test_space_rejects_path_like_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_kind: str) -> None:
    ws = _make_workspace(tmp_path / "ws")
    monkeypatch.chdir(ws)

    result = CliRunner().invoke(_cli_group, ["space", bad_kind])

    assert result.exit_code == 1
    assert "invalid kind" in result.output
