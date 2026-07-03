from __future__ import annotations

import logging
from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.subprocess_runner import ISubprocessRunner, SubprocessResult
from winter_cli.modules.lint.models import (
    LintScope,
    LintScopeError,
    LintScopeKind,
    LintScopeRequest,
)
from winter_cli.modules.workspace.models import FeatureEnvironment, RepoError
from winter_cli.modules.workspace.pattern_match import resolve_name_patterns
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository

logger = logging.getLogger(__name__)


def parse_porcelain_z(output: str) -> list[str]:
    """Parse the NUL-delimited output of ``git status --porcelain -z``.

    ``-z`` emits raw, NUL-terminated, unquoted paths — so paths with spaces or
    non-ASCII survive intact (plain ``--porcelain`` C-quotes them).  Each entry
    starts with a two-character XY status code followed by a space and the path.
    A rename or copy entry (``R`` or ``C`` anywhere in XY) spans **two** NUL
    fields: ``XY new_path\\0orig_path``; we keep the new path and skip the
    original.  All other entries are a single field.

    Returns the new/current paths in order, one per status entry.
    """
    tokens = output.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if len(token) < 4:
            i += 1
            continue
        status, path = token[:2], token[3:]
        paths.append(path)
        # R/C entries carry the original path in the following NUL field.
        i += 2 if ("R" in status or "C" in status) else 1
    return paths


class LintScopeResolver:
    """Turns a CLI scope request into the concrete content a lint run covers.

    Owns scope selection only — it resolves names and the changed set to a list
    of paths, never inspecting *what* those paths contain. Lint targets the
    **project repos we develop in feature environments** — never the workspace
    root itself (the governance layer, which references everything by design)
    nor the standalone extension clones (released products that linted clean
    before they shipped). The scopes:

      - default (no argument): the feature environment containing the invocation
        directory — every project worktree inside it. Outside any env (e.g. run
        from the workspace root), falls back to every env's project worktrees.
      - `--all`: every feature environment's project worktrees.
      - an env name: every project worktree directory inside that env.
      - a project-repo name: that repo's source checkout. (Standalone-only
        names are rejected — standalone clones are out of scope.)
      - multiple names and/or a bare glob (no `<env>/<repo>` segment): each
        resolved name fans out to its own scope — `resolve()` returns a list,
        one `LintScope` per matched repo/env name.
      - `--changed`: files that are dirty or in un-pushed commits in the git
        repository containing the invocation directory.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        repo_factory: RepositoryFactory,
        worktree_repo: IReadWorkspaceRepository,
        repo_repo: IWriteRepoRepository,
        subprocess_runner: ISubprocessRunner,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._worktree_repo = worktree_repo
        self._repo_repo = repo_repo
        self._subprocess = subprocess_runner

    def resolve(self, request: LintScopeRequest) -> list[LintScope]:
        """Resolve a scope request to the ordered list of `LintScope`s a run should cover.

        Every branch but `names` always yields exactly one scope. `names`
        (one or more literal repo/env names, and/or bare globs) fans out to
        one `LintScope` per resolved name via `_resolve_names` — a glob
        matching nothing yields an empty list (no error; the caller reports
        "nothing matched" and no-ops), while a literal name that resolves to
        neither a repo nor an env still raises immediately, exactly as a
        single-name call did before glob/multi-target support.
        """
        sources = (
            ("a scope name", bool(request.names)),
            ("--all", request.all),
            ("--changed", request.changed),
        )
        chosen = [label for label, on in sources if on]
        if len(chosen) > 1:
            raise LintScopeError(f"{', '.join(chosen)} are mutually exclusive")

        if request.changed:
            return [self._resolve_changed(request.cwd or self._config.workspace_root)]
        if request.names:
            return self._resolve_names(request.names)
        if request.all:
            return [self._resolve_all()]
        return [self._resolve_default(request.cwd or self._config.workspace_root)]

    # ── default (current env) and --all (every env) ──────────────────────────

    def _resolve_default(self, cwd: Path) -> LintScope:
        """Lint the env the caller is standing in, or every env when outside one."""
        env_name = self._env_for_cwd(cwd)
        if env_name is not None:
            paths = self._env_worktree_paths(env_name)
            if paths is not None:
                return LintScope(kind=LintScopeKind.env, label=f"env: {env_name}", paths=paths)
        return self._resolve_all()

    def _resolve_all(self) -> LintScope:
        project_repos = self._repo_factory.get_project_repos()
        paths = [env.path / repo.name for env in self._environments() for repo in project_repos]
        return LintScope(kind=LintScopeKind.all, label="all envs", paths=paths)

    def _env_for_cwd(self, cwd: Path) -> str | None:
        """The feature env whose directory contains `cwd`, or None if outside any env."""
        try:
            rel = cwd.resolve().relative_to(self._config.workspace_root.resolve())
        except ValueError:
            return None
        if not rel.parts:
            return None
        candidate = rel.parts[0]
        return candidate if any(env.name == candidate for env in self._environments()) else None

    # ── named repo(s)/env(s), literal and/or bare glob ───────────────────────

    def _resolve_names(self, names: list[str]) -> list[LintScope]:
        """Resolve NAMES to one `LintScope` per matched name, in deterministic (sorted) order.

        A literal name (no glob char) is always included verbatim — even if
        it names neither a repo nor an env — so `winter lint <typo>` still
        surfaces `_resolve_name`'s own "unknown scope" error instead of
        silently matching nothing. A glob name is expanded against the union
        of project-repo and env names, deduped against the literal names so a
        mixed invocation never resolves the same name twice. Each resolved
        name still goes through `_resolve_name`, so the repo/env ambiguity
        rejection applies per name exactly as it did before multi-target
        support.
        """

        def discover_names() -> set[str]:
            project_repos = self._repo_factory.get_project_repos()
            return {r.name for r in project_repos} | {e.name for e in self._environments()}

        resolved_names = resolve_name_patterns(names, discover_names)
        return [self._resolve_name(name) for name in resolved_names]

    def _resolve_name(self, name: str) -> LintScope:
        repo_path = self._repo_path(name)
        env_paths = self._env_worktree_paths(name)
        if repo_path is not None and env_paths is not None:
            raise LintScopeError(f"`{name}` matches both a repo and an env — rename one to disambiguate")
        if repo_path is not None:
            return LintScope(kind=LintScopeKind.repo, label=f"repo: {name}", paths=[repo_path])
        if env_paths is not None:
            return LintScope(kind=LintScopeKind.env, label=f"env: {name}", paths=env_paths)
        raise LintScopeError(f"unknown scope `{name}` — expected a project-repo name, an env name, --all, or --changed")

    def _repo_path(self, name: str) -> Path | None:
        # Project repos only — standalone clones are released products, out of
        # lint scope. (Repos that are both project and standalone resolve here.)
        for repo in self._repo_factory.get_project_repos():
            if repo.name == name:
                return repo.main_path
        return None

    def _env_worktree_paths(self, name: str) -> list[Path] | None:
        """Resolve an env name to its per-repo worktree directories, or None if no such env."""
        project_repos = self._repo_factory.get_project_repos()
        match = next((env for env in self._environments() if env.name == name), None)
        if match is None:
            return None
        return [match.path / repo.name for repo in project_repos]

    def _environments(self) -> list[FeatureEnvironment]:
        project_repos = self._repo_factory.get_project_repos()
        try:
            workspace = self._repo_repo.get_workspace(
                self._config.workspace_root,
                self._config.service_prefix,
                self._config.main_branch,
            )
            return self._worktree_repo.get_environments(workspace, project_repos)
        except RepoError as exc:
            raise LintScopeError(f"failed to enumerate envs: {exc}") from exc

    # ── --changed ────────────────────────────────────────────────────────────

    def _resolve_changed(self, cwd: Path) -> LintScope:
        root = self._git_toplevel(cwd)
        if root is None:
            raise LintScopeError(f"--changed must run inside a git repository (cwd: {cwd})")

        rel_paths: list[str] = []
        rel_paths.extend(self._dirty_paths(root))
        rel_paths.extend(self._unpushed_paths(root))

        seen: set[str] = set()
        paths: list[Path] = []
        for rel in rel_paths:
            if rel in seen:
                continue
            seen.add(rel)
            paths.append(root / rel)
        return LintScope(kind=LintScopeKind.changed, label=f"changed ({root.name})", paths=paths)

    def _git_toplevel(self, cwd: Path) -> Path | None:
        result = self._git(cwd, "rev-parse", "--show-toplevel")
        if result is None or result.returncode != 0:
            return None
        top = result.stdout.strip()
        return Path(top) if top else None

    def _dirty_paths(self, root: Path) -> list[str]:
        """Working-tree + staged + untracked paths via `git status --porcelain -z`."""
        result = self._git(root, "status", "--porcelain", "-z")
        if result is None or result.returncode != 0:
            return []
        return parse_porcelain_z(result.stdout)

    def _unpushed_paths(self, root: Path) -> list[str]:
        """Files changed in commits ahead of the upstream (or origin/<main>)."""
        base = self._upstream_ref(root)
        if base is None:
            return []
        result = self._git(root, "diff", "--name-only", "-z", f"{base}..HEAD")
        if result is None or result.returncode != 0:
            return []
        return [token for token in result.stdout.split("\0") if token]

    def _upstream_ref(self, root: Path) -> str | None:
        tracking = self._git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if tracking is not None and tracking.returncode == 0 and tracking.stdout.strip():
            return tracking.stdout.strip()
        fallback = f"origin/{self._config.main_branch}"
        verify = self._git(root, "rev-parse", "--verify", "--quiet", fallback)
        if verify is not None and verify.returncode == 0:
            return fallback
        return None

    def _git(self, cwd: Path, *args: str) -> SubprocessResult | None:
        try:
            return self._subprocess.run(["git", "-C", str(cwd), *args])
        except OSError as exc:
            logger.debug("git %s failed in %s: %s", " ".join(args), cwd, exc)
            return None
