"""Tests for WorkspaceSkillService: unified config-driven workspace-skill projection.

Covers:
  1. Core projection across all three vendors (ClaudeCode symlink, Codex symlink, OpenCode copy).
  2. Dir-equals-prefix naming rule: skills/ws → ws (bare), skills/init → ws-init.
  3. Config defaults: prefix defaults to "ws", skills_dir defaults to "skills".
  4. Custom prefix and custom skills_dir.
  5. No-op when skills_dir is absent (prune pass still runs).
  6. Stale-prune on rename / removal (including bare prefix entry).
  7. Frontmatter validation: SKILL.md must not set name:.
  8. Git-exclude block written for all three vendor dirs.
  9. Exclude block survives finalize_excludes (regression guard).
  10. Idempotency.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeInitReporter
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository
from winter_cli.modules.workspace.workspace_skill_service import WorkspaceSkillService

WORKSPACE_ROOT = Path("/ws")
CLAUDE_SKILLS = WORKSPACE_ROOT / ".claude" / "skills"
CODEX_SKILLS = WORKSPACE_ROOT / ".codex" / "skills"
OPENCODE_SKILLS = WORKSPACE_ROOT / ".opencode" / "skill"
SKILLS_DIR = WORKSPACE_ROOT / "skills"
GIT_EXCLUDE = WORKSPACE_ROOT / ".git" / "info" / "exclude"


def _config(prefix: str = "ws", skills_dir: str = "skills") -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        skill_prefix=prefix,
        skills_dir=skills_dir,
    )


def _service(config: WorkspaceConfig, fs: FakeFilesystem) -> WorkspaceSkillService:
    return WorkspaceSkillService(config=config, fs=fs)


def _seed_skill(fs: FakeFilesystem, name: str, body: str = "---\ndescription: x\n---\n") -> Path:
    """Plant a skill directory under workspace_root/skills/<name>/SKILL.md."""
    skill_dir = SKILLS_DIR / name
    fs.directories.add(skill_dir)
    fs.files[skill_dir / "SKILL.md"] = body
    for parent in skill_dir.parents:
        fs.directories.add(parent)
    # Also seed .git/ so the exclude block is written.
    fs.directories.add(WORKSPACE_ROOT / ".git")
    return skill_dir


def _seed_custom_skill(
    fs: FakeFilesystem, skills_root: Path, name: str, body: str = "---\ndescription: x\n---\n"
) -> Path:
    """Plant a skill directory under a custom skills root."""
    skill_dir = skills_root / name
    fs.directories.add(skill_dir)
    fs.files[skill_dir / "SKILL.md"] = body
    for parent in skill_dir.parents:
        fs.directories.add(parent)
    fs.directories.add(WORKSPACE_ROOT / ".git")
    return skill_dir


# ── 1. Core projection across all three vendors ───────────────────────────────


def test_reconcile_projects_skill_into_claude_skills(init_reporter: FakeInitReporter) -> None:
    """Workspace skills appear as symlinks under .claude/skills/<prefix>-<name>."""
    fs = FakeFilesystem()
    _seed_skill(fs, "do-thing")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    link = CLAUDE_SKILLS / "ws-do-thing"
    assert fs.is_symlink(link)


def test_reconcile_projects_skill_into_codex_skills(init_reporter: FakeInitReporter) -> None:
    """Workspace skills appear as symlinks under .codex/skills/<prefix>-<name>."""
    fs = FakeFilesystem()
    _seed_skill(fs, "do-thing")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    link = CODEX_SKILLS / "ws-do-thing"
    assert fs.is_symlink(link)


def test_reconcile_projects_skill_into_opencode_skill(init_reporter: FakeInitReporter) -> None:
    """Workspace skills appear as real directories under .opencode/skill/<prefix>-<name>."""
    fs = FakeFilesystem()
    _seed_skill(fs, "do-thing", "---\ndescription: x\n---\n# do-thing\n")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    dest = OPENCODE_SKILLS / "ws-do-thing"
    assert fs.is_dir(dest)
    assert not fs.is_symlink(dest)
    assert fs.is_file(dest / "SKILL.md")


def test_reconcile_all_three_vendors_in_one_pass(init_reporter: FakeInitReporter) -> None:
    """A single reconcile call projects into ClaudeCode, Codex, and OpenCode."""
    fs = FakeFilesystem()
    _seed_skill(fs, "my-skill")
    svc = _service(_config("myprefix"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "myprefix-my-skill")
    assert fs.is_symlink(CODEX_SKILLS / "myprefix-my-skill")
    assert fs.is_dir(OPENCODE_SKILLS / "myprefix-my-skill")


def test_reconcile_multiple_skills(init_reporter: FakeInitReporter) -> None:
    """All skills under skills_dir are projected into all three vendors."""
    fs = FakeFilesystem()
    for name in ("init", "fetch", "pull", "push", "setup", "update"):
        _seed_skill(fs, name)
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    for name in ("init", "fetch", "pull", "push", "setup", "update"):
        assert fs.is_symlink(CLAUDE_SKILLS / f"ws-{name}"), f"missing ClaudeCode symlink for ws-{name}"
        assert fs.is_symlink(CODEX_SKILLS / f"ws-{name}"), f"missing Codex symlink for ws-{name}"
        assert fs.is_dir(OPENCODE_SKILLS / f"ws-{name}"), f"missing OpenCode copy for ws-{name}"


def test_reconcile_reports_action(init_reporter: FakeInitReporter) -> None:
    """A successful projection reports a workspace_skills_installed action."""
    fs = FakeFilesystem()
    _seed_skill(fs, "alpha")
    svc = _service(_config("ws"), fs)

    svc.reconcile(init_reporter)

    actions = [(a[0], a[2]) for a in init_reporter.actions]
    assert ("workspace", "workspace_skills_installed") in actions


# ── 2. Dir-equals-prefix naming rule ─────────────────────────────────────────


def test_dir_equals_prefix_projects_bare(init_reporter: FakeInitReporter) -> None:
    """skills/ws/ → ws (bare, no double-prefix), not ws-ws."""
    fs = FakeFilesystem()
    _seed_skill(fs, "ws")  # dir name == prefix
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    # Bare prefix projected in all three vendors.
    assert fs.is_symlink(CLAUDE_SKILLS / "ws"), "expected bare 'ws' symlink in .claude/skills"
    assert fs.is_symlink(CODEX_SKILLS / "ws"), "expected bare 'ws' symlink in .codex/skills"
    assert fs.is_dir(OPENCODE_SKILLS / "ws"), "expected bare 'ws' copy in .opencode/skill"
    # Must NOT create double-prefixed entry.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-ws"), "ws-ws must not be created"
    assert not fs.is_symlink(CODEX_SKILLS / "ws-ws"), "ws-ws must not be created"


def test_dir_not_prefix_projects_with_dash(init_reporter: FakeInitReporter) -> None:
    """skills/init/ → ws-init (prefix + dash + dirname)."""
    fs = FakeFilesystem()
    _seed_skill(fs, "init")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-init")
    assert fs.is_symlink(CODEX_SKILLS / "ws-init")
    assert fs.is_dir(OPENCODE_SKILLS / "ws-init")


def test_full_ws_skill_set_naming(init_reporter: FakeInitReporter) -> None:
    """Validate the full ws-* naming: ws→ws, init→ws-init, fetch→ws-fetch, etc."""
    fs = FakeFilesystem()
    expected_map = {
        "ws": "ws",
        "init": "ws-init",
        "fetch": "ws-fetch",
        "pull": "ws-pull",
        "push": "ws-push",
        "setup": "ws-setup",
        "update": "ws-update",
    }
    for source_name in expected_map:
        _seed_skill(fs, source_name)
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    for source_name, projected_name in expected_map.items():
        assert fs.is_symlink(CLAUDE_SKILLS / projected_name), f"expected {projected_name} from {source_name}"
        assert fs.is_symlink(CODEX_SKILLS / projected_name), f"expected {projected_name} in codex"
        assert fs.is_dir(OPENCODE_SKILLS / projected_name), f"expected {projected_name} in opencode"


# ── 3. Config defaults ────────────────────────────────────────────────────────


def test_skill_prefix_defaults_to_ws() -> None:
    """WorkspaceConfig.skill_prefix defaults to 'ws'."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        main_branch="main",
    )
    assert cfg.skill_prefix == "ws"


def test_skills_dir_defaults_to_skills() -> None:
    """WorkspaceConfig.skills_dir defaults to 'skills'."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        main_branch="main",
    )
    assert cfg.skills_dir == "skills"


def test_reconcile_uses_default_prefix_when_not_configured(init_reporter: FakeInitReporter) -> None:
    """With no explicit prefix, projection uses 'ws' by default."""
    fs = FakeFilesystem()
    _seed_skill(fs, "do-thing")
    # Use default prefix ("ws")
    svc = _service(_config(), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-do-thing")


# ── 4. Custom prefix and skills_dir ──────────────────────────────────────────


def test_custom_prefix(init_reporter: FakeInitReporter) -> None:
    """A custom prefix is used for all projected entries."""
    fs = FakeFilesystem()
    _seed_skill(fs, "my-skill")
    svc = _service(_config(prefix="myprefix"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "myprefix-my-skill")
    assert fs.is_symlink(CODEX_SKILLS / "myprefix-my-skill")
    assert fs.is_dir(OPENCODE_SKILLS / "myprefix-my-skill")
    # Default prefix entries must not be created.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-my-skill")


def test_custom_skills_dir(init_reporter: FakeInitReporter) -> None:
    """A custom skills_dir is read instead of the default 'skills'."""
    fs = FakeFilesystem()
    custom_root = WORKSPACE_ROOT / "my-skills"
    _seed_custom_skill(fs, custom_root, "do-thing")
    svc = _service(_config(skills_dir="my-skills"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-do-thing")
    # Default 'skills/' dir should not be used.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-unknown")


def test_custom_skills_dir_default_still_skills(init_reporter: FakeInitReporter) -> None:
    """When skills_dir not configured, the default 'skills' dir is used."""
    fs = FakeFilesystem()
    _seed_skill(fs, "init")
    svc = _service(_config(), fs)  # skills_dir defaults to "skills"

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-init")


# ── 5. No-op when skills_dir absent ──────────────────────────────────────────


def test_reconcile_noop_when_skills_dir_absent(init_reporter: FakeInitReporter) -> None:
    """skills/ directory absent → reconcile returns True (prune pass still runs)."""
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git")
    # No skills/ directory seeded.
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert not init_reporter.errors


# ── 6. Stale-prune on rename / removal ───────────────────────────────────────


def test_reconcile_prunes_stale_symlinks_on_rename(init_reporter: FakeInitReporter) -> None:
    """When a skill directory is renamed, the old symlink is pruned and the new one installed."""
    fs = FakeFilesystem()
    _seed_skill(fs, "new-name")
    # Pre-plant the stale symlink from the old name.
    fs.directories.add(CLAUDE_SKILLS)
    fs.symlinks[CLAUDE_SKILLS / "ws-old-name"] = Path("../../skills/old-name")
    fs.directories.add(CODEX_SKILLS)
    fs.symlinks[CODEX_SKILLS / "ws-old-name"] = Path("../../skills/old-name")

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    # New name is installed.
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-new-name")
    assert fs.is_symlink(CODEX_SKILLS / "ws-new-name")
    # Old stale symlink is pruned.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-old-name")
    assert not fs.is_symlink(CODEX_SKILLS / "ws-old-name")


def test_reconcile_prunes_stale_copy_on_removal(init_reporter: FakeInitReporter) -> None:
    """When a skill directory is removed, the stale OpenCode copy is pruned."""
    fs = FakeFilesystem()
    _seed_skill(fs, "keep")
    # Pre-plant the stale copy for a removed skill.
    stale_copy = OPENCODE_SKILLS / "ws-removed"
    fs.directories.add(stale_copy)
    fs.files[stale_copy / "SKILL.md"] = "---\nname: ws-removed\n---\n"
    for parent in stale_copy.parents:
        fs.directories.add(parent)

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    # Surviving skill is present.
    assert fs.is_dir(OPENCODE_SKILLS / "ws-keep")
    # Stale copy is removed.
    assert not fs.is_dir(stale_copy)


def test_reconcile_prunes_bare_prefix_stale_symlink(init_reporter: FakeInitReporter) -> None:
    """The bare prefix symlink (e.g. 'ws') is pruned when the source is removed."""
    fs = FakeFilesystem()
    # No source skills; pre-plant stale bare-prefix symlink.
    fs.directories.add(CLAUDE_SKILLS)
    fs.symlinks[CLAUDE_SKILLS / "ws"] = Path("../../skills/ws")
    fs.directories.add(CODEX_SKILLS)
    fs.symlinks[CODEX_SKILLS / "ws"] = Path("../../skills/ws")
    fs.directories.add(WORKSPACE_ROOT / ".git")

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws")
    assert not fs.is_symlink(CODEX_SKILLS / "ws")


def test_reconcile_prunes_bare_prefix_stale_copy(init_reporter: FakeInitReporter) -> None:
    """The bare prefix OpenCode copy is pruned when the source ws/ dir is removed."""
    fs = FakeFilesystem()
    # No source; pre-plant stale bare-prefix copy.
    stale = OPENCODE_SKILLS / "ws"
    fs.directories.add(stale)
    fs.files[stale / "SKILL.md"] = "---\nname: ws\n---\n"
    for parent in stale.parents:
        fs.directories.add(parent)
    fs.directories.add(WORKSPACE_ROOT / ".git")

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert not fs.is_dir(stale)


def test_reconcile_leaves_other_prefix_entries_untouched(init_reporter: FakeInitReporter) -> None:
    """Pruning only removes entries that match the workspace prefix; other prefixes survive."""
    fs = FakeFilesystem()
    _seed_skill(fs, "do-thing")
    # Pre-plant a symlink owned by a different prefix.
    fs.directories.add(CLAUDE_SKILLS)
    fs.symlinks[CLAUDE_SKILLS / "ext-other-skill"] = Path("../../other-ext/skills/other-skill")

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-do-thing")
    assert fs.is_symlink(CLAUDE_SKILLS / "ext-other-skill")  # untouched — different prefix


def test_reconcile_prunes_when_skills_dir_removed(init_reporter: FakeInitReporter) -> None:
    """When skills/ is deleted wholesale, stale <prefix>-* symlinks are pruned."""
    fs = FakeFilesystem()
    # Pre-plant a stale symlink for a skill that was previously projected.
    fs.directories.add(CLAUDE_SKILLS)
    fs.symlinks[CLAUDE_SKILLS / "ws-old-skill"] = Path("../../skills/old-skill")
    fs.directories.add(WORKSPACE_ROOT / ".git")
    # No skills/ directory seeded — simulates wholesale removal.

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    # Stale symlink is pruned even though skills/ no longer exists.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-old-skill")


# ── 7. Frontmatter validation ─────────────────────────────────────────────────


def test_reconcile_rejects_skill_md_with_name_frontmatter(init_reporter: FakeInitReporter) -> None:
    """A workspace skill whose SKILL.md sets `name:` causes reconcile to fail."""
    fs = FakeFilesystem()
    _seed_skill(fs, "bad-skill", "---\nname: overridden\ndescription: x\n---\n")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is False
    assert init_reporter.errors
    error_text = " ".join(e[1] for e in init_reporter.errors)
    assert "name" in error_text
    assert "overridden" in error_text
    # No symlink should have been created for the offending skill.
    assert not fs.is_symlink(CLAUDE_SKILLS / "ws-bad-skill")


def test_reconcile_accepts_skill_md_without_name_frontmatter(init_reporter: FakeInitReporter) -> None:
    """A workspace skill whose SKILL.md omits `name:` is accepted and projected."""
    fs = FakeFilesystem()
    _seed_skill(fs, "good-skill", "---\ndescription: A clean skill\n---\n")
    svc = _service(_config("ws"), fs)

    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert not init_reporter.errors
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-good-skill")


# ── 8. Git-exclude block for all three vendor dirs ────────────────────────────


def test_reconcile_writes_exclude_block(init_reporter: FakeInitReporter) -> None:
    """A managed git-exclude block is written for all three vendor dirs."""
    fs = FakeFilesystem()
    _seed_skill(fs, "init")
    svc = _service(_config("ws"), fs)

    svc.reconcile(init_reporter)

    assert fs.is_file(GIT_EXCLUDE), "expected .git/info/exclude to be written"
    content = fs.read_text(GIT_EXCLUDE)
    assert ".claude/skills/ws-*" in content
    assert ".claude/skills/ws" in content
    assert ".codex/skills/ws-*" in content
    assert ".codex/skills/ws" in content
    assert ".opencode/skill/ws-*" in content
    assert ".opencode/skill/ws" in content


def test_reconcile_exclude_block_uses_workspace_skills_block_name(init_reporter: FakeInitReporter) -> None:
    """The exclude block is namespaced as winter-workspace/workspace-skills."""
    fs = FakeFilesystem()
    _seed_skill(fs, "init")
    svc = _service(_config("ws"), fs)

    svc.reconcile(init_reporter)

    content = fs.read_text(GIT_EXCLUDE)
    assert "winter-workspace/workspace-skills" in content


def test_reconcile_exclude_block_is_idempotent(init_reporter: FakeInitReporter) -> None:
    """Re-running reconcile does not duplicate the exclude block."""
    fs = FakeFilesystem()
    _seed_skill(fs, "init")
    svc = _service(_config("ws"), fs)

    svc.reconcile(init_reporter)
    content_after_first = fs.read_text(GIT_EXCLUDE)
    svc.reconcile(init_reporter)
    content_after_second = fs.read_text(GIT_EXCLUDE)

    assert content_after_first == content_after_second


def test_reconcile_skips_exclude_when_no_git_dir(init_reporter: FakeInitReporter) -> None:
    """.git absent → exclude write is silently skipped; reconcile still returns True."""
    fs = FakeFilesystem()
    # Seed skill but omit .git/
    skill_dir = SKILLS_DIR / "init"
    fs.directories.add(skill_dir)
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: x\n---\n"
    for parent in skill_dir.parents:
        fs.directories.add(parent)

    svc = _service(_config("ws"), fs)
    ok = svc.reconcile(init_reporter)

    assert ok is True
    assert not init_reporter.errors
    assert not fs.is_file(GIT_EXCLUDE)


# ── 9. Exclude block survives finalize_excludes ───────────────────────────────


def test_exclude_block_survives_finalize_excludes(init_reporter: FakeInitReporter) -> None:
    """The workspace-skills exclude block must not be stripped by ExtensionExcludeService.

    _strip_orphan_managed_blocks rejects block names containing '/' so that
    namespaced workspace blocks (winter-dir/*, winter-workspace/*) survive the
    extension-orphan pass. This test reproduces the scenario: write the block
    via WorkspaceSkillService.reconcile(), then run finalize_excludes() with an
    eligible extension, and assert the block is still present.
    """
    fs = FakeFilesystem()
    _seed_skill(fs, "init")

    # Seed one eligible extension so finalize_excludes runs its full orphan-strip pass.
    ext_path = WORKSPACE_ROOT / "winter-workflow"
    fs.directories.add(ext_path)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files: dict[Path, dict] = {manifest_path: {"name": "winter-workflow"}}

    cfg = _config("ws")
    skill_svc = _service(cfg, fs)
    exclude_svc = ExtensionExcludeService(
        config=cfg,
        fs=fs,
        manifest_loader=ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files)),
    )
    repos = [StandaloneRepository(name="winter-workflow", path=ext_path)]

    ok_skill = skill_svc.reconcile(init_reporter)
    assert ok_skill is True, "workspace skill reconcile must succeed"

    content_before = fs.read_text(GIT_EXCLUDE)
    assert "winter-workspace/workspace-skills" in content_before, "block must be present after reconcile"

    ok_ext = exclude_svc.finalize_excludes(repos, init_reporter)
    assert ok_ext is True, "finalize_excludes must succeed"

    content_after = fs.read_text(GIT_EXCLUDE)
    assert "winter-workspace/workspace-skills" in content_after, (
        "workspace-skills exclude block was stripped by finalize_excludes — "
        "block name must contain '/' to survive the orphan pass"
    )


# ── 10. Idempotency ────────────────────────────────────────────────────────────


def test_reconcile_is_idempotent(init_reporter: FakeInitReporter) -> None:
    """Running reconcile twice produces the same result without errors."""
    fs = FakeFilesystem()
    _seed_skill(fs, "my-skill")
    svc = _service(_config("ws"), fs)

    ok1 = svc.reconcile(init_reporter)
    ok2 = svc.reconcile(init_reporter)

    assert ok1 is True
    assert ok2 is True
    assert not init_reporter.errors
    assert fs.is_symlink(CLAUDE_SKILLS / "ws-my-skill")


def test_reconcile_idempotent_opencode_copy(init_reporter: FakeInitReporter) -> None:
    """Second reconcile does not re-copy OpenCode skill when content is unchanged."""
    fs = FakeFilesystem()
    _seed_skill(fs, "my-skill", "---\ndescription: x\n---\n# stable content\n")
    svc = _service(_config("ws"), fs)

    svc.reconcile(init_reporter)
    dest = OPENCODE_SKILLS / "ws-my-skill"
    content_after_first = fs.read_text(dest / "SKILL.md")

    svc.reconcile(init_reporter)
    content_after_second = fs.read_text(dest / "SKILL.md")

    assert content_after_first == content_after_second
    assert not init_reporter.errors


def test_reconcile_is_idempotent_with_bare_prefix_skill(init_reporter: FakeInitReporter) -> None:
    """Second reconcile for bare-prefix skill (skills/ws/) is idempotent."""
    fs = FakeFilesystem()
    _seed_skill(fs, "ws")  # dir == prefix → bare entry
    svc = _service(_config("ws"), fs)

    ok1 = svc.reconcile(init_reporter)
    ok2 = svc.reconcile(init_reporter)

    assert ok1 is True
    assert ok2 is True
    assert not init_reporter.errors
    assert fs.is_symlink(CLAUDE_SKILLS / "ws")
