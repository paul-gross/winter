from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.modules.capability.spec_loader import SpecLoader
from winter_cli.modules.ext.models import NewParams
from winter_cli.modules.ext.scaffold_service import _ENTRYPOINT_REL, ExtScaffoldService


def _real_spec_loader() -> SpecLoader:
    from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader

    return SpecLoader(config_file_reader=TomllibConfigFileReader())


def _svc(fs: FakeFilesystem | None = None) -> ExtScaffoldService:
    return ExtScaffoldService(spec_loader=_real_spec_loader(), fs=fs or FakeFilesystem())


def _params(
    name: str = "my-ext",
    slot: str = "service",
    output_dir: Path = Path("/out/my-ext"),
    force: bool = False,
) -> NewParams:
    return NewParams(name=name, slot=slot, output_dir=output_dir, force=force)


# ── happy path: files are created ────────────────────────────────────────────


def test_scaffold_creates_manifest() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    assert fs.is_file(Path("/out/my-ext/winter-ext.toml"))


def test_scaffold_creates_index_md() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    assert fs.is_file(Path("/out/my-ext/index.md"))


def test_scaffold_creates_entrypoint() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    assert fs.is_file(Path("/out/my-ext") / _ENTRYPOINT_REL)


def test_scaffold_marks_entrypoint_executable() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    assert fs.access_x_ok(Path("/out/my-ext") / _ENTRYPOINT_REL)


def test_scaffold_result_lists_three_files() -> None:
    result = _svc().scaffold(_params())
    assert len(result.created_files) == 3


# ── manifest content ──────────────────────────────────────────────────────────


def test_manifest_contains_name() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params(name="my-ext"))
    content = fs.read_text(Path("/out/my-ext/winter-ext.toml"))
    assert 'name = "my-ext"' in content


def test_manifest_contains_provides_service() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext/winter-ext.toml"))
    assert "[provides]" in content
    assert f'service = "{_ENTRYPOINT_REL}"' in content


def test_manifest_contains_implements_service() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext/winter-ext.toml"))
    assert "[implements]" in content
    assert 'service = "v1"' in content


def test_manifest_implements_version_from_spec_loader() -> None:
    """Version in manifest must match highest version in SpecLoader.supported_versions."""
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext/winter-ext.toml"))
    loader = _real_spec_loader()
    highest = sorted(loader.supported_versions("service"))[-1]
    assert f'service = "{highest}"' in content


# ── index.md content ──────────────────────────────────────────────────────────


def test_index_md_contains_title() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params(name="my-ext"))
    content = fs.read_text(Path("/out/my-ext/index.md"))
    assert "# my-ext" in content


def test_index_md_contains_path_notation() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params(name="my-ext"))
    content = fs.read_text(Path("/out/my-ext/index.md"))
    assert "my-ext:" in content


def test_index_md_contains_provides_section() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext/index.md"))
    assert "What this extension provides" in content


# ── stub content: sourced from spec ──────────────────────────────────────────


def test_stub_contains_all_action_names() -> None:
    """The stub's known-action set must include every action declared in the spec."""
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext") / _ENTRYPOINT_REL)
    spec = _real_spec_loader().load("service", "v1")
    for action in spec.actions:
        assert repr(action.name) in content


def test_stub_references_unknown_exit_code_from_spec() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext") / _ENTRYPOINT_REL)
    assert "_UNKNOWN_EXIT = 2" in content


def test_stub_references_refuse_exit_code_from_spec() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext") / _ENTRYPOINT_REL)
    assert "_REFUSE_EXIT = 3" in content


def test_stub_echoes_argv_to_stderr() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext") / _ENTRYPOINT_REL)
    assert "sys.argv" in content
    assert "stderr" in content


def test_stub_is_python_script() -> None:
    fs = FakeFilesystem()
    _svc(fs).scaffold(_params())
    content = fs.read_text(Path("/out/my-ext") / _ENTRYPOINT_REL)
    assert content.startswith("#!/usr/bin/env python3")


# ── non-empty directory guard ─────────────────────────────────────────────────


def test_raises_file_exists_error_when_dir_not_empty() -> None:
    fs = FakeFilesystem(files={Path("/out/my-ext/existing-file.txt"): "hello"})
    with pytest.raises(FileExistsError, match="already exists"):
        _svc(fs).scaffold(_params())


def test_force_flag_allows_overwriting_non_empty_dir() -> None:
    fs = FakeFilesystem(files={Path("/out/my-ext/existing-file.txt"): "hello"})
    result = _svc(fs).scaffold(_params(force=True))
    assert result.output_dir == Path("/out/my-ext")


def test_empty_existing_dir_does_not_raise() -> None:
    fs = FakeFilesystem(directories=[Path("/out/my-ext")])
    result = _svc(fs).scaffold(_params())
    assert result.output_dir == Path("/out/my-ext")


# ── end-to-end: scaffold output passes verify ─────────────────────────────────


def _make_workspace(base: Path) -> Path:
    """Create a minimal .winter/ workspace at *base* and return *base*."""
    base.mkdir(parents=True, exist_ok=True)
    winter_dir = base / ".winter"
    winter_dir.mkdir()
    (winter_dir / "config.toml").write_text(
        'main_branch = "main"\nsession_prefix = "test"\n'
        '[[project_repository]]\nname = "demo"\nurl = "git@example.com:x/demo.git"\n'
    )
    return base


def test_scaffold_output_passes_verify(tmp_path: Path) -> None:
    """The generated extension must pass winter ext verify with all checks passing.

    This is the defining probe for Phase 4 (AC6 scaffold test): scaffold then verify
    in the same process, using the real spec loader and a real temp directory.
    """
    import subprocess
    import sys

    workspace = _make_workspace(tmp_path / "ws")
    out_dir = tmp_path / "my-test-ext"

    # Run: python -m winter_cli.cli ext new my-test-ext --capability service --dir <out_dir>
    new_result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "new", "my-test-ext",
         "--capability", "service", "--dir", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    assert new_result.returncode == 0, (
        f"ext new failed (exit {new_result.returncode}):\n{new_result.stdout}\n{new_result.stderr}"
    )

    # Verify the generated extension.
    verify_result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    combined = verify_result.stdout + verify_result.stderr
    assert verify_result.returncode == 0, (
        f"ext verify failed (exit {verify_result.returncode}):\n{combined}"
    )
    # All checks should show the pass glyph.
    assert "✓" in combined
    assert "✗" not in combined
