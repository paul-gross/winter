"""Tests for SkillProbeService: per-vendor skill discoverability probe.

Covers:
  1. Healthy state — all vendors pass when projections match sources.
  2. Broken symlink — symlink vendor (ClaudeCode) flags a broken symlink as WARN.
  3. Stale copy — copy vendor (OpenCode) flags a content-hash mismatch as WARN.
  4. Orphaned entry — a projected entry whose source is gone, for any vendor.
  5. Missing projection — a source skill with no projected entry.
  6. adopt_extensions = none — returns no results.
  7. Winter mode + no manifest — extension skipped silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from tests.conftest import FakeFilesystem
from winter_cli.config.models import AdoptExtensions, CodeAgentVendor, WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.doctor.skill_probe_service import SKILL_SOURCE, SkillProbeService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WORKSPACE_ROOT = Path("/ws")

# Vendor skill dirs (derived from CodeAgentVendor.skills_subpath)
CLAUDE_SKILLS = WORKSPACE_ROOT / ".claude" / "skills"
CODEX_SKILLS = WORKSPACE_ROOT / ".codex" / "skills"
OPENCODE_SKILLS = WORKSPACE_ROOT / ".opencode" / "skill"

# Extension source layout
EXT_ROOT = WORKSPACE_ROOT / "winter-workflow"
EXT_SKILLS = EXT_ROOT / "skills"


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _config(adopt_extensions: AdoptExtensions = AdoptExtensions.all) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=adopt_extensions,
    )


def _manifest_loader(fs: FakeFilesystem) -> ExtensionManifestLoader:
    from tests.conftest import FakeConfigFileReader

    return ExtensionManifestLoader(config_file_reader=FakeConfigFileReader())


def _svc(config: WorkspaceConfig, fs: FakeFilesystem) -> SkillProbeService:
    loader = _manifest_loader(fs)
    return SkillProbeService(
        config=config,
        fs=cast(IFilesystemReader, fs),
        manifest_loader=loader,
    )


def _repo(name: str = "winter-workflow", prefix: str | None = None) -> StandaloneRepository:
    return StandaloneRepository(name=name, path=WORKSPACE_ROOT / name, prefix=prefix)


def _skill_source(name: str) -> tuple[Path, Path]:
    """Return (skill_dir, skill_md) for a skill named `name` under EXT_SKILLS."""
    d = EXT_SKILLS / name
    return d, d / "SKILL.md"


# ---------------------------------------------------------------------------
# 1. Healthy state
# ---------------------------------------------------------------------------


class TestHealthyState:
    def test_symlink_vendor_passes_when_all_links_resolve(self) -> None:
        """ClaudeCode vendor: all symlinks point to existing source dirs → PASS."""
        skill_dir, skill_md = _skill_source("glacier")
        link = CLAUDE_SKILLS / "winter-workflow-glacier"
        target = Path("../../winter-workflow/skills/glacier")

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, skill_dir, CLAUDE_SKILLS},
            symlinks={link: target},
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        assert claude_result.status == ProbeStatus.pass_
        assert claude_result.source == SKILL_SOURCE

    def test_copy_vendor_passes_when_content_matches(self) -> None:
        """OpenCode vendor: copy content hash matches source → PASS.

        OpenCode's CopySkillStrategy applies the OpenCodeSkillNameTransform to
        SKILL.md when hashing the source (with skill_name set) but hashes the
        destination as-is. So the installed copy's SKILL.md must contain the
        transformed content for the hashes to match. We mirror exactly what the
        installer writes to the destination.
        """
        skill_dir, skill_md = _skill_source("do-thing")
        dest_dir = OPENCODE_SKILLS / "winter-workflow-do-thing"
        dest_md = dest_dir / "SKILL.md"
        # Raw source content (no frontmatter).
        source_content = "# do-thing"
        # What the installer writes to the copy after applying OpenCodeSkillNameTransform.
        dest_content = "---\nname: winter-workflow-do-thing\n---\n\n# do-thing"

        fs = FakeFilesystem(
            files={skill_md: source_content, dest_md: dest_content},
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                skill_dir,
                OPENCODE_SKILLS,
                dest_dir,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        oc_result = next(r for r in results if "opencode" in r.name)
        assert oc_result.status == ProbeStatus.pass_

    def test_no_extensions_all_pass(self) -> None:
        """With no standalone repos, every vendor emits a 0-skill PASS."""
        fs = FakeFilesystem(directories={WORKSPACE_ROOT})
        svc = _svc(_config(), fs)

        results = svc.run([])
        assert results, "expected results for each vendor"
        assert all(r.status == ProbeStatus.pass_ for r in results)

    def test_one_result_per_vendor(self) -> None:
        """run() emits exactly one result per CodeAgentVendor."""
        fs = FakeFilesystem(directories={WORKSPACE_ROOT})
        svc = _svc(_config(), fs)

        results = svc.run([])
        vendor_count = len(list(CodeAgentVendor))
        assert len(results) == vendor_count


# ---------------------------------------------------------------------------
# 2. Broken symlink (symlink vendor)
# ---------------------------------------------------------------------------


class TestBrokenSymlink:
    def test_broken_symlink_warns_for_claude(self) -> None:
        """A dangling symlink under .claude/skills → WARN with 'broken symlink' message."""
        skill_dir, skill_md = _skill_source("glacier")
        link = CLAUDE_SKILLS / "winter-workflow-glacier"
        broken_target = Path("../../winter-workflow/skills/gone")  # target doesn't exist

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, skill_dir, CLAUDE_SKILLS},
            symlinks={link: broken_target},
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        assert claude_result.status == ProbeStatus.warn
        assert "broken symlink" in claude_result.message
        assert "winter-workflow-glacier" in claude_result.message
        assert claude_result.remediation is not None
        assert "winter ws init" in claude_result.remediation

    def test_broken_symlink_warns_for_codex(self) -> None:
        """A dangling symlink under .codex/skills → WARN."""
        skill_dir, skill_md = _skill_source("glacier")
        link = CODEX_SKILLS / "winter-workflow-glacier"
        broken_target = Path("../../winter-workflow/skills/gone")

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, skill_dir, CODEX_SKILLS},
            symlinks={link: broken_target},
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        codex_result = next(r for r in results if "codex" in r.name)
        assert codex_result.status == ProbeStatus.warn
        assert "broken symlink" in codex_result.message


# ---------------------------------------------------------------------------
# 3. Stale copy (copy vendor)
# ---------------------------------------------------------------------------


class TestStaleCopy:
    def test_stale_copy_warns_for_opencode(self) -> None:
        """OpenCode copy with different content from source → WARN with 'stale copy' message."""
        skill_dir, skill_md = _skill_source("do-thing")
        dest_dir = OPENCODE_SKILLS / "winter-workflow-do-thing"
        dest_md = dest_dir / "SKILL.md"

        fs = FakeFilesystem(
            files={
                skill_md: "# original content",
                dest_md: "# different stale content",
            },
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                skill_dir,
                OPENCODE_SKILLS,
                dest_dir,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        oc_result = next(r for r in results if "opencode" in r.name)
        assert oc_result.status == ProbeStatus.warn
        assert "stale copy" in oc_result.message
        assert "winter-workflow-do-thing" in oc_result.message
        assert oc_result.remediation is not None
        assert "winter ws init" in oc_result.remediation

    def test_missing_copy_warns_for_opencode(self) -> None:
        """Source skill exists but copy is missing from .opencode/skill → WARN."""
        skill_dir, skill_md = _skill_source("do-thing")

        fs = FakeFilesystem(
            files={skill_md: "# content"},
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                skill_dir,
                OPENCODE_SKILLS,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        oc_result = next(r for r in results if "opencode" in r.name)
        assert oc_result.status == ProbeStatus.warn
        assert "missing" in oc_result.message


# ---------------------------------------------------------------------------
# 4. Orphaned entry (projected with no source)
# ---------------------------------------------------------------------------


class TestOrphanedEntry:
    def test_orphaned_symlink_warns_for_claude(self) -> None:
        """Symlink exists in .claude/skills but source dir is gone → WARN 'orphaned'."""
        # There is NO source skill in the extension's skills dir,
        # but there IS a symlink in the vendor skills dir.
        link = CLAUDE_SKILLS / "winter-workflow-deleted"
        target = Path("../../winter-workflow/skills/deleted")

        fs = FakeFilesystem(
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, CLAUDE_SKILLS},
            symlinks={link: target},
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        assert claude_result.status == ProbeStatus.warn
        assert "orphaned" in claude_result.message

    def test_orphaned_copy_warns_for_opencode(self) -> None:
        """Copy exists in .opencode/skill but source is gone → WARN 'orphaned'."""
        dest_dir = OPENCODE_SKILLS / "winter-workflow-deleted"
        dest_md = dest_dir / "SKILL.md"

        fs = FakeFilesystem(
            files={dest_md: "# old"},
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                OPENCODE_SKILLS,
                dest_dir,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        oc_result = next(r for r in results if "opencode" in r.name)
        assert oc_result.status == ProbeStatus.warn
        assert "orphaned" in oc_result.message


# ---------------------------------------------------------------------------
# 5. Missing projection (source exists, no projected entry)
# ---------------------------------------------------------------------------


class TestMissingProjection:
    def test_missing_projection_warns_for_claude(self) -> None:
        """Source skill exists in extension but no symlink in .claude/skills → WARN 'missing'."""
        skill_dir, skill_md = _skill_source("new-skill")

        fs = FakeFilesystem(
            files={skill_md: "# new"},
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                skill_dir,
                CLAUDE_SKILLS,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        assert claude_result.status == ProbeStatus.warn
        assert "missing" in claude_result.message


# ---------------------------------------------------------------------------
# 6. adopt_extensions = none
# ---------------------------------------------------------------------------


class TestAdoptExtensionsNone:
    def test_returns_empty_when_adopt_none(self) -> None:
        """When adopt_extensions=none, no probe results are emitted."""
        fs = FakeFilesystem(directories={WORKSPACE_ROOT})
        svc = _svc(_config(adopt_extensions=AdoptExtensions.none), fs)

        results = svc.run([_repo()])
        assert results == []


# ---------------------------------------------------------------------------
# 7. Winter mode — extension without manifest is skipped
# ---------------------------------------------------------------------------


class TestWinterModeNoManifest:
    def test_extension_skipped_in_winter_mode_without_manifest(self) -> None:
        """In 'winter' mode, an extension with no winter-ext.toml is silently skipped."""
        skill_dir, skill_md = _skill_source("glacier")
        # No winter-ext.toml file in EXT_ROOT

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={
                WORKSPACE_ROOT,
                EXT_ROOT,
                EXT_SKILLS,
                skill_dir,
                CLAUDE_SKILLS,
            },
        )
        svc = _svc(_config(adopt_extensions=AdoptExtensions.winter), fs)
        repo = _repo()

        results = svc.run([repo])
        # No skills should be expected from the extension, so no projected skills either.
        # All vendors should pass (0 skills in sync).
        assert all(r.status == ProbeStatus.pass_ for r in results)


# ---------------------------------------------------------------------------
# 8. Custom prefix
# ---------------------------------------------------------------------------


class TestCustomPrefix:
    def test_custom_prefix_is_used_in_projected_names(self) -> None:
        """A repo with a workspace-level prefix override uses that prefix."""
        skill_dir, skill_md = _skill_source("glacier")
        # Projected with prefix "wf" (override), not "winter-workflow"
        link = CLAUDE_SKILLS / "wf-glacier"
        target = Path("../../winter-workflow/skills/glacier")

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, skill_dir, CLAUDE_SKILLS},
            symlinks={link: target},
        )
        svc = _svc(_config(), fs)
        repo = _repo(prefix="wf")

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        assert claude_result.status == ProbeStatus.pass_


# ---------------------------------------------------------------------------
# 9. First-party skills are not flagged as orphans
# ---------------------------------------------------------------------------


class TestFirstPartySkillsNotOrphaned:
    def test_first_party_ws_skills_not_flagged_as_orphans(self) -> None:
        """First-party ws-* skills in .claude/skills are NOT reported as orphaned.

        The probe must only inspect entries whose prefix matches a known extension
        prefix. The built-in workspace skills (ws-init, ws-push, etc.) use the
        ``ws-`` prefix, which no extension declares, so they must be silently
        ignored — not reported as orphans — even when extensions are adopted.
        """
        skill_dir, skill_md = _skill_source("glacier")
        ext_link = CLAUDE_SKILLS / "winter-workflow-glacier"
        ext_target = Path("../../winter-workflow/skills/glacier")

        # First-party ws-* symlinks that live alongside the extension skill.
        ws_init_link = CLAUDE_SKILLS / "ws-init"
        ws_push_link = CLAUDE_SKILLS / "ws-push"
        ws_target_init = Path("../../some/internal/ws-init")
        ws_target_push = Path("../../some/internal/ws-push")

        fs = FakeFilesystem(
            files={skill_md: "# skill"},
            directories={WORKSPACE_ROOT, EXT_ROOT, EXT_SKILLS, skill_dir, CLAUDE_SKILLS},
            symlinks={
                ext_link: ext_target,
                ws_init_link: ws_target_init,
                ws_push_link: ws_target_push,
            },
        )
        svc = _svc(_config(), fs)
        repo = _repo()

        results = svc.run([repo])
        claude_result = next(r for r in results if "claude-code" in r.name)
        # The ws-* entries must not cause an orphan warning.
        assert claude_result.status == ProbeStatus.pass_, (
            f"Expected PASS but got {claude_result.status}: {claude_result.message}"
        )
