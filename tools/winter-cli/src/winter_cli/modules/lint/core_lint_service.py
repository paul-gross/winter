from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from winter_cli.config.models import FileSizeLintConfig
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.lint.finding_parser import parse_lint_output
from winter_cli.modules.lint.models import LintCheckOutcome, LintFinding, LintScope, LintStatus
from winter_cli.modules.lint.scope_env import WINTER_CLI_VAR, lint_scope_env

# Source label shown in `winter lint` output for built-in core checks. Matches
# doctor's `CORE_SOURCE` so both surfaces read consistently — core checks ship
# with winter-cli and always run, unlike the opt-in workspace/extension scripts.
CORE_SOURCE = "core"

# The single built-in lint check: module extractability, which validates
# dependency direction across the ecosystem graph (a core module pointing at an
# extension is a layering inversion; an undeclared sibling reference is a dead
# pointer at the consumption edge).
EXTRACTABILITY_CHECK = "extractability"

# Built-in check name for the agent-facing markdown file-size guard.
FILE_SIZE_CHECK = "file-size"

# Built-in check name for the provision required_services reference check.
REQUIRED_SERVICES_CHECK = "required-services"

# Claude @import pattern — line-leading or inline, same as extractability.py.
# Matches `@<path>` where path contains a `/` or `.` (filters out @param-style
# mentions that are not path imports).
_IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s`]+)")
_IMPORT_TRIM = ".,;:!?)]}>\"'"

# Directories skipped when walking for markdown files.
_PRUNE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"})


def default_extractability_script_path() -> Path:
    """Absolute path to the bundled extractability lint script.

    The script lives in the winter repo's `tools/winter-lint/` directory, a
    sibling of the `tools/winter-cli/` tree this package's source resides in.
    Resolved relative to this file (the spec-loader pattern) so it works
    wherever the CLI runs from its source checkout — the only supported
    deployment, since the `winter` launcher always execs winter-cli from the
    workspace's own source tree.
    """
    # .../tools/winter-cli/src/winter_cli/modules/lint/core_lint_service.py
    #     parents[5] == .../tools
    return Path(__file__).resolve().parents[5] / "winter-lint" / "extractability.py"


class FileSizeLintCheck:
    """Checks agent-facing markdown files against configurable byte-size thresholds.

    Files in the auto-injected ``@import`` graph (rooted at ``CLAUDE.md`` and
    ``CLAUDE.winter.md`` in the workspace root) are held to the tighter
    ``injected_bytes`` threshold; all other ``.md`` files in scope are checked
    against the looser ``reference_bytes`` threshold.

    Measurement is by UTF-8 byte length (``len(text.encode())``), not token
    count, so the check is tokenizer-independent and fast.
    """

    def __init__(self, workspace_root: Path, config: FileSizeLintConfig) -> None:
        self._workspace_root = workspace_root
        self._config = config

    # ── public API ───────────────────────────────────────────────────────────

    def check(self, scope: LintScope) -> list[LintFinding]:
        """Return one LintFinding per over-threshold markdown file in scope."""
        injected = self._resolve_injected_set()
        findings: list[LintFinding] = []
        for md_file in self._collect_md_files(scope.paths):
            rel = self._relpath(md_file)
            is_injected = md_file.resolve() in injected
            threshold = self._config.injected_bytes if is_injected else self._config.reference_bytes
            kind = "injected" if is_injected else "reference"
            try:
                size = len(md_file.read_bytes())
            except OSError:
                continue
            if size > threshold:
                findings.append(
                    LintFinding(
                        source=CORE_SOURCE,
                        check=FILE_SIZE_CHECK,
                        status=LintStatus.fail,
                        message=(
                            f"{rel}: {size} bytes exceeds the {kind} threshold of {threshold} bytes"
                        ),
                        file=rel,
                        remediation=(
                            f"Trim or split this file to bring it under {threshold} bytes. "
                            f"Override the threshold in .winter/config.toml under "
                            f"[core_checks.file_size] {'injected_bytes' if is_injected else 'reference_bytes'}."
                        ),
                    )
                )
        return findings

    # ── internals ────────────────────────────────────────────────────────────

    def _resolve_injected_set(self) -> set[Path]:
        """Transitively resolve the @import graph from CLAUDE.md and CLAUDE.winter.md.

        Starts from the two workspace-root injection roots, follows every
        ``@<path>`` import found in each file (line-leading or inline within
        prose), and returns the set of resolved absolute ``Path`` objects that
        are actually present on disk.  Missing files are silently skipped so a
        broken import doesn't abort the check.
        """
        roots = [
            self._workspace_root / "CLAUDE.md",
            self._workspace_root / "CLAUDE.winter.md",
        ]
        visited: set[Path] = set()
        queue: list[Path] = [r.resolve() for r in roots if r.exists()]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            if not current.is_file():
                continue
            try:
                text = current.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                for raw in self._import_paths(line):
                    target = (current.parent / raw).resolve()
                    if target not in visited and target.is_file():
                        queue.append(target)
        return visited

    def _import_paths(self, line: str) -> list[str]:
        """Raw path strings from Claude @import tokens on a line."""
        out: list[str] = []
        for m in _IMPORT_RE.finditer(line):
            raw = m.group(1).rstrip(_IMPORT_TRIM)
            # Keep only path-like tokens (contain / or .) — drops @param-style mentions.
            if "/" in raw or "." in raw:
                out.append(raw)
        return out

    def _collect_md_files(self, paths: list[Path]) -> list[Path]:
        out: list[Path] = []
        seen: set[Path] = set()
        for p in paths:
            if p.is_file():
                if p.suffix == ".md":
                    r = p.resolve()
                    if r not in seen:
                        seen.add(r)
                        out.append(p)
                continue
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
                for name in filenames:
                    if name.endswith(".md"):
                        f = Path(dirpath) / name
                        r = f.resolve()
                        if r not in seen:
                            seen.add(r)
                            out.append(f)
        return out

    def _relpath(self, file: Path) -> str:
        try:
            return str(file.resolve().relative_to(self._workspace_root.resolve()))
        except ValueError:
            return str(file)


class CoreLintService:
    """Runs winter's built-in lint checks — bundled with the CLI, always on.

    The lint counterpart of doctor's `CoreProbeService`: where the workspace and
    extension lint services *discover* opt-in scripts from `.winter/config.toml`
    and each `winter-ext.toml`, this service runs the checks that ship with
    winter itself, with no per-workspace registration. Built-in checks:

    * **extractability** — validates dependency direction across the ecosystem
      graph (``tools/winter-lint/extractability.py``).
    * **file-size** — flags agent-facing markdown files exceeding configurable
      byte-size thresholds, with a tighter limit for auto-injected files.
    * **required-services** — validates ``required_services`` references in
      provision manifests against the merged service catalog from all bound
      service-orchestrator providers.

    Returns one `LintCheckOutcome` tagged `source="core"` per check that
    contributed (even when a check finds nothing), so the dispatcher counts each
    as a contributor — or `None` when the bundled extractability script can't be
    located, so an unusual install degrades to "no core checks" rather than a
    spurious failure.
    """

    def __init__(
        self,
        workspace_root: Path,
        fs: IFilesystemReader,
        subprocess_runner: ISubprocessRunner,
        winter_cli_path: str,
        script_path: Path,
        file_size_config: FileSizeLintConfig | None = None,
        orchestrator_resolver: object | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._fs = fs
        self._subprocess = subprocess_runner
        self._winter_cli_path = winter_cli_path
        self._script_path = script_path
        self._orchestrator_resolver = orchestrator_resolver
        self._file_size_check = FileSizeLintCheck(
            workspace_root,
            file_size_config if file_size_config is not None else FileSizeLintConfig(),
        )

    def run(self, scope: LintScope) -> list[LintCheckOutcome]:
        """Run all built-in core checks and return one outcome per check.

        Returns an empty list when the bundled extractability script cannot be
        found (unusual install without ``tools/winter-lint/``), so the
        dispatcher sees zero contributors rather than a spurious failure.
        """
        if not self._fs.is_file(self._script_path):
            return []

        outcomes: list[LintCheckOutcome] = []

        # ── extractability ───────────────────────────────────────────────────
        env = os.environ.copy()
        env["WINTER_WORKSPACE_DIR"] = str(self._workspace_root)
        env[WINTER_CLI_VAR] = self._winter_cli_path
        env.update(lint_scope_env(scope))

        # Run under the same interpreter that launched winter-cli (guaranteed
        # >= 3.11, which extractability's `tomllib` use requires) rather than
        # the script's `python3` shebang, which may resolve to an older PATH
        # interpreter.
        try:
            result = self._subprocess.run(
                [sys.executable, str(self._script_path)],
                cwd=self._workspace_root,
                env=env,
            )
        except OSError as exc:
            outcomes.append(
                LintCheckOutcome(
                    source=CORE_SOURCE,
                    findings=[
                        LintFinding(
                            source=CORE_SOURCE,
                            check=EXTRACTABILITY_CHECK,
                            status=LintStatus.fail,
                            message=f"failed to invoke extractability lint: {exc}",
                        )
                    ],
                )
            )
        else:
            findings = parse_lint_output(CORE_SOURCE, result.stdout, result.stderr, result.returncode)
            outcomes.append(LintCheckOutcome(source=CORE_SOURCE, findings=findings))

        # ── file-size ────────────────────────────────────────────────────────
        file_size_findings = self._file_size_check.check(scope)
        outcomes.append(LintCheckOutcome(source=CORE_SOURCE, findings=file_size_findings))

        # ── required-services ────────────────────────────────────────────────
        req_svc_findings = self._run_required_services_check(scope)
        outcomes.append(LintCheckOutcome(source=CORE_SOURCE, findings=req_svc_findings))

        return outcomes

    def _run_required_services_check(self, scope: LintScope) -> list[LintFinding]:
        """Build and run the required-services lint check."""
        from winter_cli.modules.lint.required_services_check import RequiredServicesLintCheck
        from winter_cli.modules.service.service_catalog_service import ServiceCatalogService

        # Resolve providers from the orchestrator resolver; gracefully degrade
        # to an empty list if none is registered or resolution fails.
        providers: list = []
        if self._orchestrator_resolver is not None:
            try:
                providers = self._orchestrator_resolver.resolve_all()  # type: ignore[union-attr]
            except Exception:
                # No orchestrator registered or binding error — the check
                # handles the empty-providers case by emitting a warning finding.
                providers = []

        catalog_svc = ServiceCatalogService(self._subprocess, self._workspace_root)
        check = RequiredServicesLintCheck(
            workspace_root=self._workspace_root,
            catalog_service=catalog_svc,
            providers=providers,
        )
        return check.check(scope)
