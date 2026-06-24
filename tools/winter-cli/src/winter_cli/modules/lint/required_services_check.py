"""Lint check that validates ``required_services`` entries against the service catalog.

Reads all ``[[provision.resource]]`` and ``[[provision.data]]`` handlers from:
- The workspace ``.winter/config.toml`` (via ``WorkspaceConfig.provision_raw``)
- Each installed extension's ``winter-ext.toml``

Correlates every ``required_services`` entry against the merged service catalog
(obtained by invoking each bound provider with the ``catalog`` action via
``ServiceCatalogService``).  Emits one ``LintFinding`` per unknown reference,
with ``file``/``line`` pointing at the offending TOML entry and a near-miss
suggestion list.

When no service orchestrator is registered but ``required_services`` entries
exist, emits a finding that the references cannot be validated rather than
silently passing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from winter_cli.modules.lint.models import LintFinding, LintScope, LintStatus
from winter_cli.modules.service.service_catalog_service import (
    WORKSPACE_SCOPE,
    ServiceCatalog,
    ServiceCatalogService,
)

if TYPE_CHECKING:
    from winter_cli.modules.capability.models import ResolvedCapability

CORE_SOURCE = "core"
CHECK_NAME = "required-services"

# TOML key that we scan for line numbers.
_REQUIRED_SERVICES_KEY = "required_services"


def _validate_token(token: str) -> bool:
    """Return True when *token* is a well-formed scope-qualified reference.

    Accepted forms:
    - ``workspace/<name>``  — workspace-scoped reference.
    - ``<env>/<name>``      — env-scoped reference (any non-empty env name).
    """
    parts = token.split("/", 1)
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


class RequiredServicesLintCheck:
    """Validates ``required_services`` references against the merged service catalog.

    Instantiated once per lint run by ``CoreLintService``.  The catalog is
    fetched lazily on the first ``check()`` call so the subprocess invocations
    only happen when the check is actually running (not on construction).
    """

    def __init__(
        self,
        workspace_root: Path,
        catalog_service: ServiceCatalogService,
        providers: list[ResolvedCapability],
    ) -> None:
        self._workspace_root = workspace_root
        self._catalog_service = catalog_service
        self._providers = providers
        self._catalog: ServiceCatalog | None = None
        self._catalog_fetched = False

    # ── public API ───────────────────────────────────────────────────────────

    def check(self, scope: LintScope) -> list[LintFinding]:  # scope reserved for future filter-by-scope support
        """Return findings for all unknown ``required_services`` references."""
        findings: list[LintFinding] = []

        # Collect all (file_path, source_text) pairs to scan.
        sources = self._collect_sources()

        has_any_refs = False
        for file_path, source_text, source_label in sources:
            refs_with_lines = self._extract_refs_with_lines(source_text)
            if refs_with_lines:
                has_any_refs = True
                findings.extend(self._check_refs(file_path, refs_with_lines, source_label))

        # No required_services anywhere — nothing to check.
        if not has_any_refs:
            return findings

        # When refs exist but no providers are registered, we couldn't fetch
        # a catalog.  Emit a single finding rather than silently passing.
        # Note: _catalog_fetched may already be True (set during _check_refs
        # above), so we key off _providers being empty instead.
        if not self._providers:
            rel = self._rel(self._workspace_config_path())
            findings.append(
                LintFinding(
                    source=CORE_SOURCE,
                    check=CHECK_NAME,
                    status=LintStatus.warn,
                    message=(
                        "required_services references cannot be validated: "
                        "no service orchestrator is registered in .winter/config.toml"
                    ),
                    file=rel,
                    remediation=(
                        "Register a service orchestrator via `service_orchestrator` or "
                        "`[capabilities] service` in .winter/config.toml, or remove "
                        "the required_services declarations."
                    ),
                )
            )

        return findings

    # ── internals ────────────────────────────────────────────────────────────

    def _get_catalog(self) -> ServiceCatalog | None:
        """Return the merged catalog, fetching it once on demand.

        Returns ``None`` when no providers are registered (so callers can
        distinguish "no catalog possible" from "empty catalog").
        """
        if self._catalog_fetched:
            return self._catalog
        self._catalog_fetched = True
        if not self._providers:
            self._catalog = None
            return None
        self._catalog = self._catalog_service.build(self._providers)
        return self._catalog

    def _workspace_config_path(self) -> Path:
        return self._workspace_root / ".winter" / "config.toml"

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._workspace_root.resolve()))
        except ValueError:
            return str(path)

    def _collect_sources(self) -> list[tuple[Path, str, str]]:
        """Return (file_path, source_text, source_label) for all manifest files.

        Reads `.winter/config.toml` for the workspace provision table, and
        each installed extension's `winter-ext.toml`.  Files that cannot be
        read are silently skipped.
        """
        sources: list[tuple[Path, str, str]] = []

        # Workspace config.
        ws_config = self._workspace_config_path()
        if ws_config.exists():
            try:
                text = ws_config.read_text(encoding="utf-8", errors="replace")
                sources.append((ws_config, text, "project"))
            except OSError:
                pass

        # Extension manifests.
        ext_base = self._workspace_root / ".winter" / "ext"
        if ext_base.is_dir():
            for ext_dir in sorted(ext_base.iterdir()):
                if not ext_dir.is_dir():
                    continue
                manifest_path = ext_dir / "winter-ext.toml"
                if manifest_path.exists():
                    try:
                        text = manifest_path.read_text(encoding="utf-8", errors="replace")
                        # Use the dir name as source label.
                        sources.append((manifest_path, text, ext_dir.name))
                    except OSError:
                        pass

        return sources

    def _extract_refs_with_lines(self, source_text: str) -> list[tuple[str, int]]:
        """Return (ref_value, line_number) pairs for every required_services entry.

        Parses the TOML source text looking for ``required_services = [...]``
        blocks.  Each string value in those arrays is returned with the 1-based
        line number of the ``required_services`` key.
        """
        result: list[tuple[str, int]] = []
        lines = source_text.splitlines()

        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith(_REQUIRED_SERVICES_KEY):
                rest = stripped[len(_REQUIRED_SERVICES_KEY) :].lstrip()
                if rest.startswith("="):
                    key_line = i + 1  # 1-based
                    # Collect the array value — may span multiple lines.
                    array_text = rest[1:].lstrip()
                    j = i
                    # If the open bracket is on this line but no close bracket yet,
                    # keep accumulating.
                    if "[" in array_text:
                        while "]" not in array_text and j + 1 < len(lines):
                            j += 1
                            array_text += " " + lines[j]
                    # Extract string values from the accumulated array text.
                    for match in re.finditer(r'"([^"]*)"', array_text):
                        result.append((match.group(1), key_line))
                    for match in re.finditer(r"'([^']*)'", array_text):
                        result.append((match.group(1), key_line))
                    i = j + 1
                    continue
            i += 1

        return result

    def _check_refs(
        self,
        file_path: Path,
        refs_with_lines: list[tuple[str, int]],
        source_label: str,
    ) -> list[LintFinding]:
        """Validate each (ref, line) against the catalog and emit findings."""
        findings: list[LintFinding] = []
        rel = self._rel(file_path)
        catalog = self._get_catalog()

        for ref, line in refs_with_lines:
            if not _validate_token(ref):
                findings.append(
                    LintFinding(
                        source=CORE_SOURCE,
                        check=CHECK_NAME,
                        status=LintStatus.fail,
                        message=(
                            f"{rel}: required_services entry {ref!r} in {source_label!r} "
                            f"is not a valid scope-qualified name. "
                            f"Expected format: 'workspace/<service>' or '<env>/<service>'."
                        ),
                        file=rel,
                        line=line,
                        remediation=(
                            "Use 'workspace/<service>' for workspace-scoped services "
                            "or '<env>/<service>' for env-scoped services."
                        ),
                    )
                )
                continue

            if catalog is None:
                # No orchestrator registered — already emitted a single warning above.
                continue

            if not catalog.contains(ref):
                misses = catalog.near_misses(ref)
                scope, svc_name = ref.split("/", 1)
                scope_desc = "workspace-scoped" if scope == WORKSPACE_SCOPE else f"env-scoped (scope={scope!r})"
                suggestion = ""
                if misses:
                    suggestion = f" Did you mean: {', '.join(misses)}?"
                findings.append(
                    LintFinding(
                        source=CORE_SOURCE,
                        check=CHECK_NAME,
                        status=LintStatus.fail,
                        message=(
                            f"{rel}: unknown {scope_desc} service {ref!r} in {source_label!r}. "
                            f"No provider declares a service named {svc_name!r} "
                            f"with scope {'workspace' if scope == WORKSPACE_SCOPE else 'project'!r}."
                            f"{suggestion}"
                        ),
                        file=rel,
                        line=line,
                        remediation=(
                            f"Check the service name for typos. "
                            f"Available services: {', '.join(catalog.all_qualified_names()) or '(none)'}"
                        ),
                    )
                )

        return findings
