from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus
from winter_cli.modules.workspace.agents_md_service import (
    AGENTS_MD_FILENAME,
    CLAUDE_MD_FILENAME,
    AgentsMdService,
    _compute_hash,
)

AGENTS_SOURCE = "agents"

# Directories that are never feature-env roots (system / infrastructure dirs).
_SKIP_DIRS = frozenset({
    "projects", ".git", ".winter", ".claude", ".codex", ".opencode", ".venv",
    "node_modules", "__pycache__",
})


class AgentsMdProbeService:
    """Doctor probe that checks ``AGENTS.md`` is in sync with its resolved sources.

    Discovers roots to check by:
    - The workspace root itself.
    - Every immediate child directory that contains a ``CLAUDE.md`` (feature-env
      roots that ``winter ws init`` would have generated an ``AGENTS.md`` for).

    For each such root the probe:
    1. Checks that ``AGENTS.md`` exists alongside ``CLAUDE.md``.
    2. Re-resolves the @import graph and compares a SHA-256 content hash
       against the on-disk file — flagging drift as a WARNING when they differ.

    This is REPORT-ONLY: the probe never rewrites the file.  Run
    ``winter ws init`` to regenerate.

    Note on ``CoreProbeService._probe_claude_symlinks`` overlap: the core probe
    issues a hard FAIL on any dangling symlink in ``.claude/skills``; this probe
    focuses on AGENTS.md drift and is unrelated to symlink health. No deduplication
    is needed between the two.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemWriter,
        agents_md_svc: AgentsMdService,
    ) -> None:
        self._config = config
        self._fs = fs
        self._svc = agents_md_svc

    def run(self) -> list[ProbeResult]:
        """Run the probe for all CLAUDE.md-bearing roots in the workspace."""
        results: list[ProbeResult] = []
        for root in self._discover_roots():
            result = self._probe_root(root)
            if result is not None:
                results.append(result)
        return results

    def _discover_roots(self) -> list[Path]:
        """Return the workspace root plus any immediate-child env dirs with CLAUDE.md."""
        workspace = self._config.workspace_root
        roots: list[Path] = [workspace]

        if not self._fs.is_dir(workspace):
            return roots

        try:
            children = self._fs.iterdir(workspace)
        except OSError:
            return roots

        for child in sorted(children):
            if not self._fs.is_dir(child):
                continue
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            # Only add a child as an env root when it has its own CLAUDE.md.
            if self._fs.is_file(child / CLAUDE_MD_FILENAME):
                roots.append(child)

        return roots

    def _probe_root(self, root: Path) -> ProbeResult | None:
        claude_md = root / CLAUDE_MD_FILENAME
        if not self._fs.is_file(claude_md):
            return None

        agents_path = root / AGENTS_MD_FILENAME
        label = f"{AGENTS_MD_FILENAME} ({self._rel(root)})"

        if not self._fs.exists(agents_path):
            return ProbeResult(
                source=AGENTS_SOURCE,
                name=label,
                status=ProbeStatus.warn,
                message=f"{AGENTS_MD_FILENAME} missing at {root}",
                remediation="Run `winter ws init` to generate AGENTS.md.",
            )

        try:
            existing = self._fs.read_text(agents_path)
        except OSError as exc:
            return ProbeResult(
                source=AGENTS_SOURCE,
                name=label,
                status=ProbeStatus.warn,
                message=f"could not read {AGENTS_MD_FILENAME}: {exc}",
                remediation="Run `winter ws init` to regenerate AGENTS.md.",
            )

        expected_hash = self._svc.content_hash(root)
        if expected_hash is None:
            # No CLAUDE.md — already filtered above, but guard for safety.
            return None

        actual_hash = _compute_hash(existing)
        if actual_hash != expected_hash:
            return ProbeResult(
                source=AGENTS_SOURCE,
                name=label,
                status=ProbeStatus.warn,
                message=f"{AGENTS_MD_FILENAME} at {root} is out of sync with its sources",
                remediation="Run `winter ws init` to regenerate AGENTS.md.",
            )

        return ProbeResult(
            source=AGENTS_SOURCE,
            name=label,
            status=ProbeStatus.pass_,
            message=f"{AGENTS_MD_FILENAME} in sync",
        )

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._config.workspace_root.resolve()))
        except ValueError:
            return str(path)
