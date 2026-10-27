from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeFilesystem,
    FakeInitReporter,
    FakeWorkspaceExcludeLocator,
)
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
    )


@pytest.fixture
def workspace_config_none() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.none,
    )


def _service(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict[Path, dict],
) -> ExtensionExcludeService:
    return ExtensionExcludeService(
        config=workspace_config,
        fs=fs,
        manifest_loader=ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files)),
        exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
    )


def _seed_minimal_extension(fs: FakeFilesystem, config_files: dict[Path, dict], name: str) -> None:
    """Plant just the manifest under WORKSPACE_ROOT/<name>/ so exclude resolution succeeds."""
    ext_path = WORKSPACE_ROOT / name
    fs.directories.add(ext_path)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files[manifest_path] = {"name": name}


def test_finalize_excludes_writes_one_block_per_extension(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    config_files: dict[Path, dict] = {}
    _seed_minimal_extension(fs, config_files, "ext-a")
    _seed_minimal_extension(fs, config_files, "ext-b")
    repos = [
        StandaloneRepository(name="ext-a", path=WORKSPACE_ROOT / "ext-a"),
        StandaloneRepository(name="ext-b", path=WORKSPACE_ROOT / "ext-b"),
    ]
    svc = _service(workspace_config, fs, config_files)

    ok = svc.finalize_excludes(repos, init_reporter)
    assert ok is True

    exclude_path = WORKSPACE_ROOT / ".git" / "info" / "exclude"
    content = fs.files[exclude_path]
    assert "# >>> ext-a (managed by winter)" in content
    assert "# >>> ext-b (managed by winter)" in content
    assert "/ext-a/" in content
    assert ".claude/skills/ext-a-*" in content
    assert ".codex/skills/ext-a-*" in content
    assert ".opencode/skill/ext-a-*" in content
    assert ".claude/agents/ext-a-*" in content
    assert ".codex/agents/ext-a-*" in content
    assert ".opencode/agent/ext-a-*" in content


def test_finalize_excludes_writes_bare_prefix_lines_for_skills_only(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Bare `<prefix>` lines (no `-` suffix) are written for skills, but not for agents."""
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    config_files: dict[Path, dict] = {}
    _seed_minimal_extension(fs, config_files, "ext-a")
    repos = [StandaloneRepository(name="ext-a", path=WORKSPACE_ROOT / "ext-a")]
    svc = _service(workspace_config, fs, config_files)

    ok = svc.finalize_excludes(repos, init_reporter)
    assert ok is True

    exclude_path = WORKSPACE_ROOT / ".git" / "info" / "exclude"
    content = fs.files[exclude_path]
    exact_lines = content.splitlines()

    assert ".claude/skills/ext-a" in exact_lines
    assert ".codex/skills/ext-a" in exact_lines
    assert ".opencode/skill/ext-a" in exact_lines

    assert ".claude/agents/ext-a" not in exact_lines


# ---------------------------------------------------------------------------
# Phase-3: winter-config block tests
# ---------------------------------------------------------------------------

EXCLUDE_PATH = WORKSPACE_ROOT / ".git" / "info" / "exclude"
WINTER_CONFIG_BEGIN = "# >>> winter-config (managed by winter)"
WINTER_CONFIG_END = "# <<< winter-config"
WINTER_CONFIG_BODY = ".winter/config/**/*.local.*"


def test_winter_config_block_is_written(workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter) -> None:
    """finalize_excludes writes the winter-config block with the correct body line."""
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    svc = _service(workspace_config, fs, {})

    ok = svc.finalize_excludes([], init_reporter)
    assert ok is True

    content = fs.files[EXCLUDE_PATH]
    assert WINTER_CONFIG_BEGIN in content
    assert WINTER_CONFIG_BODY in content
    assert WINTER_CONFIG_END in content


def test_winter_config_block_is_idempotent(workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter) -> None:
    """A second call to finalize_excludes leaves the winter-config block unchanged."""
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    svc = _service(workspace_config, fs, {})

    svc.finalize_excludes([], init_reporter)
    content_after_first = fs.files[EXCLUDE_PATH]

    svc.finalize_excludes([], init_reporter)
    content_after_second = fs.files[EXCLUDE_PATH]

    assert content_after_first == content_after_second
    assert content_after_second.count(WINTER_CONFIG_BEGIN) == 1


def test_winter_config_block_written_in_none_mode(
    workspace_config_none: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """The winter-config block is written even when adopt_extensions=none."""
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    svc = _service(workspace_config_none, fs, {})

    ok = svc.finalize_excludes([], init_reporter)
    assert ok is True

    content = fs.files[EXCLUDE_PATH]
    assert WINTER_CONFIG_BEGIN in content
    assert WINTER_CONFIG_BODY in content


def test_orphan_stripper_does_not_remove_winter_config(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """The orphan-stripper never removes the winter-config block, even with empty eligible_names."""
    # Pre-seed the exclude file with the winter-config block already present.
    existing = "\n".join([WINTER_CONFIG_BEGIN, WINTER_CONFIG_BODY, WINTER_CONFIG_END, ""])
    fs = FakeFilesystem(
        files={EXCLUDE_PATH: existing},
        directories=[WORKSPACE_ROOT / ".git" / "info"],
    )
    svc = _service(workspace_config, fs, {})

    # Call with no repos → eligible_names is empty, so orphan stripper sees winter-config
    # as a candidate. It must be spared.
    ok = svc.finalize_excludes([], init_reporter)
    assert ok is True

    content = fs.files[EXCLUDE_PATH]
    assert WINTER_CONFIG_BEGIN in content
    assert WINTER_CONFIG_BODY in content


# ---------------------------------------------------------------------------
# [excludes] declarations
# ---------------------------------------------------------------------------


def _declaring(
    fs: FakeFilesystem, config_files: dict[Path, dict], name: str, excludes: object, **extra: object
) -> StandaloneRepository:
    _seed_minimal_extension(fs, config_files, name)
    config_files[WORKSPACE_ROOT / name / "winter-ext.toml"] = {"name": name, "excludes": excludes, **extra}
    return StandaloneRepository(name=name, path=WORKSPACE_ROOT / name)


def _block(content: str, name: str) -> list[str]:
    lines = content.splitlines()
    start = lines.index(f"# >>> {name} (managed by winter)")
    return lines[start : lines.index(f"# <<< {name}", start) + 1]


def test_excludes_paths_and_space_kinds_append_after_projection_rules(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/.winter/pc/", "build/out"], "space_kinds": ["scores"]})
    svc = _service(workspace_config, fs, files)

    assert svc.finalize_excludes([repo], init_reporter) is True

    block = _block(fs.files[EXCLUDE_PATH], "ext-a")
    assert block[-4:] == ["/.winter/pc/", "/build/out", "/.winter/scores/", "# <<< ext-a"]
    assert block.index("/ext-a/") < block.index("/.winter/pc/")
    assert init_reporter.errors == []


def test_excludes_absent_leaves_block_unchanged(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    _seed_minimal_extension(fs, files, "ext-a")
    repo = StandaloneRepository(name="ext-a", path=WORKSPACE_ROOT / "ext-a")
    _service(workspace_config, fs, files).finalize_excludes([repo], init_reporter)
    assert _block(fs.files[EXCLUDE_PATH], "ext-a")[-2:] == [".opencode/agent/ext-a-*", "# <<< ext-a"]


def test_excludes_repeat_init_is_idempotent(workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"], "space_kinds": ["scores"]})
    svc = _service(workspace_config, fs, files)
    svc.finalize_excludes([repo], init_reporter)
    first = fs.files[EXCLUDE_PATH]
    svc.finalize_excludes([repo], init_reporter)
    assert fs.files[EXCLUDE_PATH] == first


def test_excludes_removal_and_relocation_update_block(init_reporter: FakeInitReporter) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"], "space_kinds": ["scores"]})

    def config(**space: object) -> WorkspaceConfig:
        return WorkspaceConfig(
            workspace_root=WORKSPACE_ROOT,
            service_prefix="t",
            main_branch="main",
            adopt_extensions=AdoptExtensions.winter,
            space=space,
        )

    _service(config(), fs, files).finalize_excludes([repo], init_reporter)
    assert "/.winter/scores/" in fs.files[EXCLUDE_PATH]
    _service(config(kinds={"scores": "out/scores"}), fs, files).finalize_excludes([repo], init_reporter)
    assert "/.winter/scores/" not in fs.files[EXCLUDE_PATH]
    assert "/.winter/out/scores/" in fs.files[EXCLUDE_PATH]
    files[WORKSPACE_ROOT / "ext-a" / "winter-ext.toml"] = {"name": "ext-a"}
    _service(config(), fs, files).finalize_excludes([repo], init_reporter)
    assert "/x/" not in fs.files[EXCLUDE_PATH]
    assert "scores" not in _block(fs.files[EXCLUDE_PATH], "ext-a")[-3]


def test_excludes_removing_last_extension_strips_block_keeps_config(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"]})
    svc = _service(workspace_config, fs, files)
    svc.finalize_excludes([repo], init_reporter)
    svc.finalize_excludes([], init_reporter)
    content = fs.files[EXCLUDE_PATH]
    assert "ext-a" not in content
    assert WINTER_CONFIG_BEGIN in content


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ({"scores": "/elsewhere/scores"}, None),
        ({"scores": "~/scores"}, None),
        ({"scores": "../up"}, None),
        ({"scores": "a/../b"}, "/.winter/b/"),
        ({"scores": "we*ird[1]"}, r"/.winter/we\*ird\[1\]/"),
    ],
)
def test_excludes_space_kind_containment_and_escaping(
    init_reporter: FakeInitReporter, kinds: dict[str, str], expected: str | None
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"space_kinds": ["scores"]})
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        space={"kinds": kinds},
    )
    _service(config, fs, files).finalize_excludes([repo], init_reporter)
    block = _block(fs.files[EXCLUDE_PATH], "ext-a")
    extras = [line for line in block if line.startswith("/.winter") or "scores" in line]
    assert (expected in block) if expected else extras == []


def test_excludes_space_kind_resolving_to_workspace_root_is_error(init_reporter: FakeInitReporter) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"], "space_kinds": ["scores"]})
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        space={"root": ".", "kinds": {"scores": "."}},
    )
    _service(config, fs, files).finalize_excludes([repo], init_reporter)
    assert [r for r, _ in init_reporter.errors] == ["ext-a"]
    assert "workspace root" in init_reporter.errors[0][1]
    assert "/x/" not in fs.files[EXCLUDE_PATH]
    assert "/ext-a/" in fs.files[EXCLUDE_PATH]


@pytest.mark.parametrize(
    "excludes",
    [
        "x",
        {"paths": "/x"},
        {"paths": [1]},
        {"paths": [""]},
        {"paths": ["!/x"]},
        {"paths": ["a/../b"]},
        {"paths": ["/"]},
        {"paths": ["~/x"]},
        {"paths": ["C:/x"]},
        {"paths": ["//host/x"]},
        {"paths": ["a\nb"]},
        {"paths": ["a\x00b"]},
        {"space_kinds": ["a/b"]},
        {"space_kinds": ["../x"]},
        {"space_kinds": "scores"},
        {"bogus": []},
    ],
)
def test_excludes_malformed_declaration_reports_and_falls_back(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter, excludes: object
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", excludes)
    _service(workspace_config, fs, files).finalize_excludes([repo], init_reporter)
    assert [r for r, _ in init_reporter.errors] == ["ext-a"]
    assert "excludes" in init_reporter.errors[0][1]
    assert _block(fs.files[EXCLUDE_PATH], "ext-a") == ["# >>> ext-a (managed by winter)", "/ext-a/", "# <<< ext-a"]


def test_excludes_ignored_when_repo_not_an_extension(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"]})
    repo = StandaloneRepository(name="ext-a", path=repo.path, extension=False)
    _service(workspace_config, fs, files).finalize_excludes([repo], init_reporter)
    assert _block(fs.files[EXCLUDE_PATH], "ext-a") == ["# >>> ext-a (managed by winter)", "/ext-a/", "# <<< ext-a"]


def test_excludes_ignored_in_none_mode(workspace_config_none: WorkspaceConfig, init_reporter: FakeInitReporter) -> None:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    files: dict[Path, dict] = {}
    repo = _declaring(fs, files, "ext-a", {"paths": ["/x/"]})
    _service(workspace_config_none, fs, files).finalize_excludes([repo], init_reporter)
    assert "/x/" not in fs.files[EXCLUDE_PATH]


def test_escaped_rules_match_literally_under_real_git(tmp_path: Path) -> None:
    """Git-level check: escaped directory names ignore only the literal directory."""
    import subprocess

    from winter_cli.modules.workspace.extension_exclude_service import _escape_pattern

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=False)

    git("init", "-q")
    rule = "/" + _escape_pattern("we*ird[1]") + "/"
    (tmp_path / ".git" / "info").mkdir(exist_ok=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(rule + "\n")

    assert git("check-ignore", "-q", "we*ird[1]/f").returncode == 0
    assert git("check-ignore", "-q", "weXird1/f").returncode == 1
