from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import AdoptExtensions, CodeAgentVendor, SkillInstall, WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus
from winter_cli.modules.workspace.extension_manifest import (
    EXT_MANIFEST,
    ExtensionManifestLoader,
)
from winter_cli.modules.workspace.extension_skill_install import CopySkillStrategy
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

SKILL_SOURCE = "skills"


class SkillProbeService:
    """Doctor probe that checks per-vendor skill discoverability for all extensions.

    For each installed extension and each ``CodeAgentVendor``, verifies that the
    projected ``<prefix>-*`` entries under the vendor's skills directory are in
    sync with their source:

    - **Symlink vendors** (ClaudeCode, Codex) — every ``<prefix>-*`` symlink
      resolves to an existing source directory containing ``SKILL.md`` (no
      broken links).
    - **Copy vendors** (OpenCode) — every ``<prefix>-*`` copy exists and its
      content hash matches the live source (no stale copies). Uses the SAME
      hash logic as ``CopySkillStrategy`` so the probe and the installer share
      one source of truth.
    - **All vendors** — no orphaned ``<prefix>-*`` entries whose source is gone;
      the projected set matches the source skill set.

    This is REPORT-ONLY: the probe never mutates or re-syncs. Drift is a
    WARNING, not a hard failure. Run ``winter ws init`` to repair.

    Note on dangling-symlink overlap with ``CoreProbeService._probe_claude_symlinks``:
    that probe issues a hard FAIL on *any* dangling link in ``.claude/skills``;
    this probe issues a per-vendor WARN with richer copy/orphan detection. Both
    behaviors are intentional — they serve different audiences (hard-fail for the
    core health check; contextual WARN for the skill-discoverability audit).
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemReader,
        manifest_loader: ExtensionManifestLoader,
    ) -> None:
        self._config = config
        self._fs = fs
        self._manifest_loader = manifest_loader

    def run(self, standalone_repos: list[StandaloneRepository]) -> list[ProbeResult]:
        if self._config.adopt_extensions == AdoptExtensions.none:
            return []

        results: list[ProbeResult] = []
        for vendor in CodeAgentVendor:
            results.extend(self._probe_vendor(vendor, standalone_repos))
        return results

    # ── Per-vendor probe ──────────────────────────────────────────────────

    def _probe_vendor(self, vendor: CodeAgentVendor, standalone_repos: list[StandaloneRepository]) -> list[ProbeResult]:
        """Check all extensions for one vendor and emit one probe result."""
        skills_dir = self._config.workspace_root / vendor.skills_subpath

        # Collect the full expected set of projected skill names across all extensions.
        expected: dict[str, Path] = {}  # projected name → source dir
        known_prefixes: set[str] = set()
        for repo in standalone_repos:
            ext_expected, prefix = self._expected_skills_with_prefix(repo, vendor)
            expected.update(ext_expected)
            if prefix is not None:
                known_prefixes.add(prefix)

        # Collect the actual set of <prefix>-* entries currently in skills_dir,
        # scoped only to known extension prefixes so first-party skills (e.g.
        # ws-init, ws-push) are never falsely flagged as orphans.
        actual: dict[str, Path] = self._actual_skills(skills_dir, known_prefixes)

        issues: list[str] = []

        # Check for orphans: projected entries with no live source.
        for name, actual_path in sorted(actual.items()):
            if name not in expected:
                issues.append(f"orphaned: {name} (no live source)")
                continue
            # Check health of the actual entry for this vendor's strategy.
            issue = self._check_entry(vendor, name, actual_path, expected[name])
            if issue:
                issues.append(issue)

        # Check for missing projections: source skills with no projected entry.
        for name in sorted(expected):
            if name not in actual:
                issues.append(f"missing: {name} (source exists, not projected)")

        label = f"skill discoverability: {vendor.value}"
        if issues:
            return [
                ProbeResult(
                    source=SKILL_SOURCE,
                    name=label,
                    status=ProbeStatus.warn,
                    message="; ".join(issues),
                    remediation="Run `winter ws init` to sync skill projections.",
                )
            ]
        n_skills = len(expected)
        return [
            ProbeResult(
                source=SKILL_SOURCE,
                name=label,
                status=ProbeStatus.pass_,
                message=f"{n_skills} skill(s) in sync",
            )
        ]

    # ── Expected skills from extensions ──────────────────────────────────

    def _expected_skills_with_prefix(
        self, repo: StandaloneRepository, vendor: CodeAgentVendor
    ) -> tuple[dict[str, Path], str | None]:
        """Return ``({projected_name: source_dir}, prefix)`` for one extension + vendor.

        Returns ``({}, None)`` when the extension doesn't qualify (no manifest in
        winter mode, no skills dir, manifest load error). This mirrors the permissive
        approach of the install path: failures in individual extensions don't abort
        the probe.
        """
        mode = self._config.adopt_extensions
        manifest_path = repo.path / EXT_MANIFEST
        manifest_present = self._fs.is_file(manifest_path)

        if mode == AdoptExtensions.winter and not manifest_present:
            return {}, None

        try:
            manifest = self._manifest_loader.load(repo, manifest_path if manifest_present else None)
        except RepoError:
            return {}, None

        skills_root = self._resolve_existing_dir(repo.path, manifest.skills_dirs)
        if skills_root is None:
            return {}, manifest.prefix

        prefix = manifest.prefix
        result: dict[str, Path] = {}
        try:
            entries = self._fs.iterdir(skills_root)
        except OSError:
            return {}, prefix
        for entry in sorted(entries):
            if not self._fs.is_dir(entry):
                continue
            if not self._fs.is_file(entry / "SKILL.md"):
                continue
            name = f"{prefix}-{entry.name}"
            result[name] = entry
        return result, prefix

    def _expected_skills(self, repo: StandaloneRepository, vendor: CodeAgentVendor) -> dict[str, Path]:
        """Return ``{projected_name: source_dir}`` for one extension + vendor."""
        skills, _ = self._expected_skills_with_prefix(repo, vendor)
        return skills

    # ── Actual skills in target dir ───────────────────────────────────────

    def _actual_skills(self, skills_dir: Path, known_prefixes: set[str]) -> dict[str, Path]:
        """Return ``{entry_name: full_path}`` for extension-owned entries in skills_dir.

        Only entries whose name starts with ``<known_prefix>-`` (for any known
        extension prefix) are included. Entries that don't match any known extension
        prefix are outside this probe's jurisdiction (e.g. first-party ``ws-*``
        skills) and are silently skipped rather than reported as orphans.
        """
        if not self._fs.is_dir(skills_dir):
            return {}

        # Pre-compute the set of prefix-with-dash strings for fast startswith checks.
        prefix_markers = {f"{p}-" for p in known_prefixes}

        result: dict[str, Path] = {}
        try:
            entries = self._fs.iterdir(skills_dir)
        except OSError:
            return result
        for entry in entries:
            # A projected entry is either a symlink (symlink vendors) or a
            # directory (copy vendors). Skip plain files and entries with no dash.
            if not (self._fs.is_symlink(entry) or self._fs.is_dir(entry)):
                continue
            if "-" not in entry.name:
                continue
            # Only collect entries whose name starts with a known extension prefix.
            if prefix_markers and not any(entry.name.startswith(m) for m in prefix_markers):
                continue
            result[entry.name] = entry
        return result

    # ── Per-entry health check ────────────────────────────────────────────

    def _check_entry(self, vendor: CodeAgentVendor, name: str, actual_path: Path, source_dir: Path) -> str | None:
        """Return an issue description for one projected entry, or None if healthy."""
        if vendor.skill_install is SkillInstall.symlink:
            return self._check_symlink(name, actual_path, source_dir)
        return self._check_copy(vendor, name, actual_path, source_dir)

    def _check_symlink(self, name: str, link_path: Path, source_dir: Path) -> str | None:
        """Detect a broken symlink (target path does not resolve to an existing dir).

        Note: ``CoreProbeService._probe_claude_symlinks`` also FAILs on dangling
        links in ``.claude/skills``; this probe issues a per-vendor WARN with
        richer context. The overlap is intentional — see the class docstring.
        """
        if not self._fs.is_symlink(link_path):
            # Not a symlink at all — unexpected for a symlink vendor.
            return f"broken: {name} (expected symlink, found non-symlink)"
        # exists() follows symlinks — False means the target is gone.
        if not self._fs.exists(link_path):
            return f"broken symlink: {name} (target missing)"
        # The target exists but may not be a directory containing SKILL.md;
        # the installer ensures that, so a mis-targeted link is also an error.
        if not self._fs.is_dir(source_dir) or not self._fs.is_file(source_dir / "SKILL.md"):
            return f"broken: {name} (source SKILL.md missing)"
        return None

    def _check_copy(self, vendor: CodeAgentVendor, name: str, dest_dir: Path, source_dir: Path) -> str | None:
        """Detect a missing or content-stale copy using the same hash as CopySkillStrategy."""
        if not self._fs.is_dir(dest_dir):
            return f"missing copy: {name}"

        # Reuse CopySkillStrategy.content_hash, which accepts an IFilesystemReader
        # and computes the same digest as the installer.
        strategy = CopySkillStrategy(self._fs, vendor)  # type: ignore[arg-type]
        src_hash = strategy.content_hash(source_dir, skill_name=name)
        dst_hash = strategy.content_hash(dest_dir)
        if src_hash != dst_hash:
            return f"stale copy: {name} (content hash mismatch)"
        return None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve_existing_dir(self, base: Path, candidates: tuple[str, ...]) -> Path | None:
        """Return the first candidate path under `base` that exists as a directory."""
        for candidate in candidates:
            path = base / candidate
            if self._fs.is_dir(path):
                return path
        return None


__all__ = ["SKILL_SOURCE", "SkillProbeService"]
