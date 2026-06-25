"""Convention test — no whole-`WorkspaceConfig` injection outside narrow carve-outs.

Convention: `winter-harness:/architecture/dependency-injection.md`.

Whole-config injection is a Dependency Inversion violation: high-level
modules end up depending on the low-level app config schema. The
convention permits exactly two categories of module to accept
`WorkspaceConfig` directly:

1. **Translation / loader services** that exist specifically to turn app
   config into runtime types (e.g. `RepositoryFactory`,
   `WriteWinterConfigurationRepository`, `WorkspaceConfigService`).
2. **Workspace-lifecycle services** that reconcile the whole workspace
   against the config (`InitService`, `DestroyService`, `PruneService`,
   the `Extension*Service` family, doctor probe services).

The DI container (`container.py`) is always allowed — it's the binding
point that derives module-scoped state from app config.

Every other module taking `config: WorkspaceConfig` (or `Config`) is a
violation. To extend the carve-out, add the file to `ALLOWED_FILES`
below with a one-line rationale.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conventions.conftest import SRC_ROOT, location, walk_src

CONVENTION_DOC = "winter-harness:/architecture/dependency-injection.md"


# Types we forbid as whole-injection parameters. `Config` is included
# defensively in case a future renaming uses the shorter name.
FORBIDDEN_TYPES = frozenset({"WorkspaceConfig", "Config"})


# Files permitted to accept `WorkspaceConfig` directly. Paths are
# relative to `src/winter_cli/`. Each entry pairs with a rationale in
# `winter-harness:/architecture/dependency-injection.md` — when adding a new
# entry, cite the carve-out (translation vs lifecycle).
ALLOWED_FILES = frozenset(
    {
        # DI container — derives module-scoped state from app config.
        "container.py",
        # Translation / loader services.
        "config/internal/write_winter_configuration_repository.py",  # writes config back
        "config/workspace.py",  # defines WorkspaceConfigService + parse_provision (on-demand strict parse of provision_raw)
        "modules/workspace/repository_factory.py",  # builds ProjectRepository from [[project_repository]]
        # Workspace-lifecycle services — reconcile every declared repo.
        "modules/workspace/init_service.py",
        "modules/workspace/destroy_service.py",
        "modules/workspace/prune_service.py",
        "modules/workspace/extension_symlink_service.py",
        "modules/workspace/extension_hook_service.py",
        "modules/workspace/extension_exclude_service.py",
        "modules/workspace/extension_claudemd_service.py",
        # Doctor probe services — walk every declared repo / extension.
        "modules/doctor/core_probe_service.py",
        "modules/doctor/workspace_probe_service.py",
        "modules/doctor/extension_probe_service.py",
        "modules/doctor/port_probe_service.py",  # doctor probe service — validates port-config invariant and registry drift (lifecycle carve-out: walks every declared env)
        "modules/doctor/skill_probe_service.py",  # doctor probe service — checks per-vendor skill discoverability across all extensions (lifecycle carve-out: walks every declared extension and every CodeAgentVendor)
        # Provision manifest probe service — doctor probe that reads provision_raw
        # and walks every extension's winter-ext.toml for [[provision.*]] validation.
        "modules/provision/manifest_probe_service.py",
        # Lint dispatcher services — walk every declared repo / extension / env.
        "modules/lint/workspace_lint_service.py",
        "modules/lint/extension_lint_service.py",
        "modules/lint/scope_resolver.py",
        # Provision execution service — workspace-lifecycle carve-out: reads env_aliases,
        # envs_per_workspace, base_port, ports_per_env, workspace_root, and project_repos
        # to fan out across all project worktrees and compute env-trio vars.
        "modules/provision/execution_service.py",
        # Provision service — workspace-lifecycle carve-out: collects handlers from
        # workspace config (parse_provision) and walks every standalone repo to gather
        # extension provision manifests; needs workspace_root to locate manifests.
        "modules/provision/provision_service.py",
        # Env-index utility — workspace-lifecycle carve-out: reads base_port, ports_per_env,
        # env_aliases, and envs_per_workspace to compute per-env port assignments.
        "modules/workspace/env_index.py",
    }
)


def _annotation_name(node: ast.expr | None) -> str | None:
    """Best-effort extraction of the simple name from a type annotation."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    return None


def _iter_param_annotations(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[ast.arg, str | None]]:
    """Yield (arg, annotation_name) for every parameter on the function."""
    args = func.args
    params: list[ast.arg] = [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
    ]
    if args.vararg is not None:
        params.append(args.vararg)
    if args.kwarg is not None:
        params.append(args.kwarg)
    return [(p, _annotation_name(p.annotation)) for p in params]


def _relative_module(file_path: Path) -> str | None:
    """Return the path relative to `src/winter_cli/`, or None for out-of-tree files.

    The fixture regression test parses a file under `tests/conventions/fixtures/`,
    which is intentionally outside the source tree — returning None there lets
    the allowlist check fall through.
    """
    try:
        return file_path.relative_to(SRC_ROOT).as_posix()
    except ValueError:
        return None


def find_whole_config_violations(file_path: Path, tree: ast.Module) -> list[str]:
    rel = _relative_module(file_path)
    if rel is not None and rel in ALLOWED_FILES:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for arg, name in _iter_param_annotations(node):
            if name in FORBIDDEN_TYPES:
                violations.append(
                    f"{location(file_path, arg)}: function {node.name!r} takes "
                    f"`{arg.arg}: {name}` — modules consume domain objects, not app config "
                    f"({CONVENTION_DOC})"
                )
    return violations


def test_no_whole_config_injection_outside_allowlist() -> None:
    all_violations: list[str] = []
    for path, tree in walk_src():
        all_violations.extend(find_whole_config_violations(path, tree))
    if all_violations:
        pytest.fail("\n".join(["Whole-Config injection violations:", *all_violations]))


def test_fixture_violation_is_detected() -> None:
    fixture = Path(__file__).parent / "fixtures" / "violating_no_whole_config_injection.py"
    tree = ast.parse(fixture.read_text(encoding="utf-8"), filename=str(fixture))
    violations = find_whole_config_violations(fixture, tree)
    assert violations, "fixture must trigger at least one violation"
    assert any("WorkspaceConfig" in v for v in violations)
