from __future__ import annotations

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.workspace.handlers import (
    DestroyParams,
    EnvCheckoutParams,
    EnvConnectParams,
    EnvDiffParams,
    EnvDisconnectParams,
    EnvFetchParams,
    EnvIndexParams,
    EnvListParams,
    EnvMergeParams,
    EnvPullParams,
    EnvPushParams,
    EnvStatusParams,
    EnvWorktreesParams,
    InitParams,
    RepoAddParams,
    RepoListParams,
    RepoRemoveParams,
    WorkspacePruneParams,
)
from winter_cli.modules.workspace.models import DiffMode, MergeMode, PinnedScope, PullMode, RepoScope


def _resolve_scope(standalone: bool, all_flag: bool) -> RepoScope:
    if standalone and all_flag:
        raise click.ClickException("--standalone and --all are mutually exclusive")
    if standalone:
        return RepoScope.standalone
    if all_flag:
        return RepoScope.all
    return RepoScope.project


def _resolve_pull_mode(ff_only: bool, merge: bool, rebase: bool) -> PullMode:
    chosen = [name for name, flag in (("--ff-only", ff_only), ("--merge", merge), ("--rebase", rebase)) if flag]
    if len(chosen) > 1:
        raise click.ClickException(f"{', '.join(chosen)} are mutually exclusive")
    if merge:
        return PullMode.merge
    if rebase:
        return PullMode.rebase
    return PullMode.ff_only


def _resolve_merge_mode(ff_only: bool, merge: bool, no_ff: bool) -> MergeMode:
    chosen = [name for name, flag in (("--ff-only", ff_only), ("--merge", merge), ("--no-ff", no_ff)) if flag]
    if len(chosen) > 1:
        raise click.ClickException(f"{', '.join(chosen)} are mutually exclusive")
    if merge:
        return MergeMode.merge
    if no_ff:
        return MergeMode.no_ff
    return MergeMode.ff_only


def _resolve_merge_pinned_scope(exclude_pinned: bool, only_pinned: bool) -> PinnedScope:
    if exclude_pinned and only_pinned:
        raise click.ClickException("--exclude-pinned and --only-pinned are mutually exclusive")
    if only_pinned:
        return PinnedScope.only
    if exclude_pinned:
        return PinnedScope.exclude
    return PinnedScope.include


def _resolve_pinned_scope(include_pinned: bool, only_pinned: bool) -> PinnedScope:
    if include_pinned and only_pinned:
        raise click.ClickException("--include-pinned and --only-pinned are mutually exclusive")
    if only_pinned:
        return PinnedScope.only
    if include_pinned:
        return PinnedScope.include
    return PinnedScope.exclude


def _validate_pattern(pattern: str) -> None:
    if not pattern:
        raise click.ClickException("Empty pattern is not allowed")
    if pattern.count("/") > 1:
        raise click.ClickException(f"Invalid pattern '{pattern}' — expected <env>/<repo> (one '/' max)")


@click.group("ws")
def ws_group():
    """Manage feature environments."""


@ws_group.command("init")
@click.argument("target", required=False)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Reconcile projects/, standalone repos, and every existing feature environment.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_init(ctx: click.Context, target: str | None, all_flag: bool, output_json: bool):
    """Reconcile source checkouts, standalone repos, or a feature environment against the config.

    \b
      winter ws init              # reconcile projects/ and standalone repos
      winter ws init alpha        # reconcile the alpha/ env (create if missing)
      winter ws init --all        # reconcile projects/, standalone repos, and every env
    """
    container = cli_ctx(ctx).container
    handler = container.init_handler()
    handler.run(InitParams(target=target, all=all_flag, output_json=output_json))


@ws_group.command("destroy")
@click.argument("env")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Bypass dirty-worktree check and pass --force to `git worktree remove`.",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Abort teardown if any on_env_destroy hook exits non-zero.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the plan without running hooks or removing anything.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_destroy(ctx: click.Context, env: str, force: bool, strict: bool, dry_run: bool, output_json: bool):
    """Tear down a feature env: fire on_env_destroy hooks, then remove every per-repo worktree and the env dir.

    \b
      winter ws destroy alpha               # standard teardown
      winter ws destroy alpha --dry-run     # print the plan; no side effects
      winter ws destroy alpha --force       # bypass dirty checks and force git worktree remove
      winter ws destroy alpha --strict      # abort if any hook exits non-zero

    Manual env removal (raw `rm -rf` plus `git worktree remove`) bypasses the
    extension hooks the same way manual env creation bypasses `on_env_init`.
    """
    container = cli_ctx(ctx).container
    handler = container.destroy_handler()
    handler.run(
        DestroyParams(
            env=env,
            force=force,
            strict=strict,
            dry_run=dry_run,
            output_json=output_json,
        )
    )


@ws_group.command("list")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_list(ctx: click.Context, output_json: bool):
    """List all feature environments."""
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.list(EnvListParams(output_json=output_json))


@ws_group.command("worktrees")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.option(
    "--status",
    "with_status",
    is_flag=True,
    default=False,
    help="Include per-repo git status (ahead/behind/dirty). Slower — does git work per repo.",
)
@click.pass_context
def ws_worktrees(ctx: click.Context, output_json: bool, with_status: bool):
    """List feature-environment <env>/<repo> worktrees and standalone repos.

    Emits all existing feature-environment worktrees across every env (kind
    "worktree"), followed by all standalone repositories (kind "standalone"),
    and finally the implicit workspace repo — the workspace root itself — as a
    single kind "workspace" entry labelled <workspace>. The other singleton
    repos (product, harness) are not included. Entries whose directory does not
    exist on disk are omitted. Intended for use by editor integrations (e.g. a
    Neovim fuzzy-finder plugin) to enumerate locations for `cd`.

    \b
      winter ws worktrees                    # human-readable table
      winter ws worktrees --json             # JSON array for machine consumption
      winter ws worktrees --json --status    # JSON with ahead/behind/dirty per repo
    """
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.worktrees(EnvWorktreesParams(output_json=output_json, with_status=with_status))


@ws_group.command("status")
@click.argument("env", required=False)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_status(ctx: click.Context, env: str | None, output_json: bool):
    """Show status for a feature environment (defaults to all)."""
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.status(EnvStatusParams(env=env, output_json=output_json))


@ws_group.command("connect")
@click.argument("env")
@click.argument("feature_branch")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_connect(ctx: click.Context, env: str, feature_branch: str, output_json: bool):
    """Connect a feature environment to a remote feature branch."""
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.connect(EnvConnectParams(env=env, feature_branch=feature_branch, output_json=output_json))


@ws_group.command("disconnect")
@click.argument("env")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_disconnect(ctx: click.Context, env: str, output_json: bool):
    """Disconnect a feature environment from its feature branch."""
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.disconnect(EnvDisconnectParams(env=env, output_json=output_json))


@ws_group.command("checkout")
@click.argument("env")
@click.argument("feature_branch")
@click.option("--force", is_flag=True, default=False, help="Bypass dirty / abandonment safety checks.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_checkout(ctx: click.Context, env: str, feature_branch: str, force: bool, output_json: bool):
    """Adopt a remote feature branch into ENV, all-or-nothing across every repo.

    Connects every non-pinned project worktree to `origin/FEATURE_BRANCH` and
    resets each to it — or to the repo's `origin/<main>` when FEATURE_BRANCH
    doesn't exist yet (a new branch started from main, created on first push).
    No network — run `winter ws fetch` first if you want fresh remote-tracking
    refs.

    Phase 1 checks each repo for: working tree dirty (staged or unstaged), and
    *abandonment* — commits on the worktree's branch that aren't on the branch
    it's moving away from (its own current upstream, falling back to
    `origin/<main>` when unconnected). If any repo is dirty or would abandon
    work (and --force is not set), the whole command refuses with a per-repo
    report — no connect and no `git reset --hard` runs anywhere.
    """
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.checkout(
        EnvCheckoutParams(
            env=env,
            feature_branch=feature_branch,
            force=force,
            output_json=output_json,
        )
    )


@ws_group.command("fetch")
@click.argument("patterns", nargs=-1)
@click.option(
    "--standalone",
    is_flag=True,
    default=False,
    help="Fetch standalone repos only (PATTERNS are not accepted).",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Also fetch every standalone repo, in addition to pattern-matched project worktrees.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_fetch(
    ctx: click.Context,
    patterns: tuple[str, ...],
    standalone: bool,
    all_flag: bool,
    output_json: bool,
):
    """Fetch refs from origin for project worktrees matched by PATTERNS, and fast-forward their source checkouts.

    Each PATTERN is a segment-aware glob over `<env>/<repo>`. Bare env names
    (no `/`) are treated as `<env>/*`. Pinned and non-pinned worktrees are
    both fetched.

    Beyond refreshing remote-tracking refs, each matched project repo's
    source checkout (`projects/<repo>`) has its local main fast-forwarded to
    `origin/<main-branch>` — worktrees share that `.git`, so it's one fetch
    per repo, and it keeps the base `winter ws init` branches new envs off of
    current. Feature worktrees are never touched. A diverged source-checkout
    main (it should only ever track main) is reported as a failed fetch for
    that repo. Standalone repos are fetched only; they have no source checkout.

    \b
      winter ws fetch                       # every env's project worktrees
      winter ws fetch alpha                 # alpha's project worktrees (== 'alpha/*')
      winter ws fetch alpha/winter          # one specific worktree
      winter ws fetch '*/winter'            # every env's winter worktree
      winter ws fetch --standalone          # standalone repos only
      winter ws fetch --all                 # every env's project worktrees + standalone
    """
    if standalone and patterns:
        raise click.ClickException("PATTERNS cannot be combined with --standalone")
    scope = _resolve_scope(standalone, all_flag)
    for pattern in patterns:
        _validate_pattern(pattern)
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.fetch(EnvFetchParams(patterns=list(patterns), scope=scope, output_json=output_json))


@ws_group.command("pull")
@click.argument("patterns", nargs=-1)
@click.option(
    "--standalone",
    is_flag=True,
    default=False,
    help="Pull standalone repos only (PATTERNS are not accepted).",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Also pull every standalone repo, in addition to pattern-matched project worktrees.",
)
@click.option(
    "--ff-only",
    "ff_only",
    is_flag=True,
    default=False,
    help="Refuse to integrate diverged branches (default).",
)
@click.option(
    "--merge",
    is_flag=True,
    default=False,
    help="Fall back to a 3-way merge commit when ff-only fails.",
)
@click.option(
    "--rebase",
    is_flag=True,
    default=False,
    help="Replay local commits on top of upstream instead of merging.",
)
@click.option(
    "--autostash",
    is_flag=True,
    default=False,
    help="Stash dirty working tree before pulling, restore after.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_pull(
    ctx: click.Context,
    patterns: tuple[str, ...],
    standalone: bool,
    all_flag: bool,
    ff_only: bool,
    merge: bool,
    rebase: bool,
    autostash: bool,
    output_json: bool,
):
    """Fetch + integrate project worktrees matched by PATTERNS (ff-only by default).

    Each PATTERN is a segment-aware glob over `<env>/<repo>`. Bare env names
    (no `/`) are treated as `<env>/*`. Pinned worktrees pull from their main
    branch; non-pinned pull from the feature branch set by `winter ws connect`;
    standalone repos pull from whatever their local branch tracks. Diverged
    repos are reported and left untouched unless --merge or --rebase is given.

    \b
      winter ws pull                       # ff-only against every env's tracked upstream
      winter ws pull alpha                 # alpha's project worktrees (== 'alpha/*')
      winter ws pull alpha/winter          # one specific worktree
      winter ws pull '*/winter' --rebase   # rebase every env's winter worktree
      winter ws pull --autostash           # stash dirty tree first, restore after
      winter ws pull --standalone          # standalone repos only
      winter ws pull --all                 # every env's project worktrees + standalone
    """
    if standalone and patterns:
        raise click.ClickException("PATTERNS cannot be combined with --standalone")
    scope = _resolve_scope(standalone, all_flag)
    mode = _resolve_pull_mode(ff_only, merge, rebase)
    for pattern in patterns:
        _validate_pattern(pattern)
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.pull(
        EnvPullParams(
            patterns=list(patterns),
            scope=scope,
            mode=mode,
            autostash=autostash,
            output_json=output_json,
        )
    )


@ws_group.command("merge")
@click.argument("source_ref")
@click.argument("patterns", nargs=-1)
@click.option(
    "--standalone",
    is_flag=True,
    default=False,
    help="Merge SOURCE_REF into standalone repos only (PATTERNS are not accepted).",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Also merge into every standalone repo, in addition to pattern-matched project worktrees.",
)
@click.option(
    "--ff-only",
    "ff_only",
    is_flag=True,
    default=False,
    help="Refuse to integrate diverged branches (default).",
)
@click.option(
    "--merge",
    "merge_flag",
    is_flag=True,
    default=False,
    help="Fall back to a 3-way merge commit when ff-only fails.",
)
@click.option(
    "--no-ff",
    "no_ff",
    is_flag=True,
    default=False,
    help="Always create a merge commit, even when fast-forward is possible.",
)
@click.option(
    "--autostash",
    is_flag=True,
    default=False,
    help="Stash dirty working tree before merging, restore after.",
)
@click.option(
    "--exclude-pinned",
    "exclude_pinned",
    is_flag=True,
    default=False,
    help="Exclude pinned project worktrees from the merge set.",
)
@click.option(
    "--only-pinned",
    "only_pinned",
    is_flag=True,
    default=False,
    help="Merge only pinned project worktrees (excludes non-pinned).",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_merge(
    ctx: click.Context,
    source_ref: str,
    patterns: tuple[str, ...],
    standalone: bool,
    all_flag: bool,
    ff_only: bool,
    merge_flag: bool,
    no_ff: bool,
    autostash: bool,
    exclude_pinned: bool,
    only_pinned: bool,
    output_json: bool,
):
    """Merge SOURCE_REF into project worktrees matched by PATTERNS (ff-only by default).

    SOURCE_REF is the same string applied to every selected repo — typically
    an env name (`alpha`), the workspace main branch (`master`), or an
    explicit remote ref (`origin/master`). At least one target PATTERN is
    required when project worktrees are in scope — there is no implicit
    "all worktrees" default; pass '*/*' to mean every env's every worktree.
    Each PATTERN is a segment-aware glob over `<env>/<repo>`; bare env names
    (no `/`) are treated as `<env>/*`. Pinned worktrees are included by
    default. Standalone repos are reached via --standalone / --all.

    Diverged repos are reported and left untouched unless --merge or --no-ff
    is given (mutually exclusive). Conflicts during a fallback merge abort
    and report diverged — no in-progress merge is left for manual
    resolution. No fetch happens here — run `winter ws fetch` first if you
    need fresh refs.

    \b
      winter ws merge alpha gamma                 # merge alpha into gamma's project worktrees (== 'gamma/*')
      winter ws merge alpha gamma/winter          # merge alpha into one specific worktree
      winter ws merge master '*/*'                # merge master into every env's every worktree (explicit)
      winter ws merge master '*/winter'           # merge master into every env's winter worktree
      winter ws merge origin/master gamma --merge # merge with 3-way fallback on divergence
      winter ws merge master gamma --no-ff        # force a merge commit even if ff is possible
      winter ws merge alpha gamma --autostash     # stash dirty tree first, restore after
      winter ws merge alpha gamma --exclude-pinned   # skip pinned worktrees in gamma
      winter ws merge master --standalone         # merge master into standalone repos only
      winter ws merge master '*/*' --all          # merge master into every worktree + every standalone
    """
    if standalone and patterns:
        raise click.ClickException("PATTERNS cannot be combined with --standalone")
    if standalone and (exclude_pinned or only_pinned):
        raise click.ClickException(
            "--exclude-pinned / --only-pinned cannot be combined with --standalone (standalone repos aren't pinned)"
        )
    scope = _resolve_scope(standalone, all_flag)
    if scope.includes_project and not patterns:
        raise click.ClickException(
            "a target PATTERN is required — refusing to merge SOURCE_REF into every env's every worktree. "
            "Pass an explicit target (e.g. `gamma`, `gamma/winter`, or '*/*' to mean every worktree)."
        )
    mode = _resolve_merge_mode(ff_only, merge_flag, no_ff)
    pinned_scope = _resolve_merge_pinned_scope(exclude_pinned, only_pinned)
    for pattern in patterns:
        _validate_pattern(pattern)
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.merge(
        EnvMergeParams(
            source_ref=source_ref,
            patterns=list(patterns),
            scope=scope,
            mode=mode,
            autostash=autostash,
            pinned_scope=pinned_scope,
            output_json=output_json,
        )
    )


@ws_group.command("push")
@click.argument("patterns", nargs=-1)
@click.option(
    "--standalone",
    is_flag=True,
    default=False,
    help="Push standalone repos only (PATTERNS are not accepted).",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Also push every standalone repo, in addition to pattern-matched project worktrees.",
)
@click.option(
    "--include-pinned",
    "include_pinned",
    is_flag=True,
    default=False,
    help="Include pinned project worktrees in the push set.",
)
@click.option(
    "--only-pinned",
    "only_pinned",
    is_flag=True,
    default=False,
    help="Push only pinned project worktrees (excludes non-pinned).",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_push(
    ctx: click.Context,
    patterns: tuple[str, ...],
    standalone: bool,
    all_flag: bool,
    include_pinned: bool,
    only_pinned: bool,
    output_json: bool,
):
    """Push project worktrees matched by PATTERNS to their tracked upstreams.

    Each PATTERN is a segment-aware glob over `<env>/<repo>`. Bare env names
    (no `/`) are treated as `<env>/*`. Pass any number of patterns to push
    exactly the set you want. Non-pinned worktrees are pushed by default;
    pass --include-pinned or --only-pinned to change that.

    Non-pinned worktrees push HEAD:refs/heads/<feature-branch>; pinned
    worktrees plain-push to whatever their local branch tracks. Standalone
    repos (reached via --standalone or --all) plain-push too. Only repos with
    commits ahead of upstream are pushed.

    To push a single standalone repo, use raw git instead — patterns don't
    apply to standalone repos.

    \b
      winter ws push                              # every env's non-pinned worktrees
      winter ws push alpha                        # alpha's non-pinned worktrees (== 'alpha/*')
      winter ws push alpha/winter                 # one specific worktree
      winter ws push '*/winter'                   # every env's winter worktree
      winter ws push 'alpha/*' 'beta/*'           # alpha + beta non-pinned worktrees
      winter ws push --include-pinned             # every env, pinned and non-pinned
      winter ws push --only-pinned                # every env, pinned only
      winter ws push 'alpha/winter' --include-pinned   # push alpha/winter even if pinned
      winter ws push --standalone                 # standalone repos only
      winter ws push --all                        # non-pinned worktrees + standalone
    """
    if standalone and patterns:
        raise click.ClickException("PATTERNS cannot be combined with --standalone")
    if standalone and (include_pinned or only_pinned):
        raise click.ClickException(
            "--include-pinned / --only-pinned cannot be combined with --standalone (standalone repos aren't pinned)"
        )
    scope = _resolve_scope(standalone, all_flag)
    pinned_scope = _resolve_pinned_scope(include_pinned, only_pinned)
    for pattern in patterns:
        _validate_pattern(pattern)
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.push(
        EnvPushParams(
            patterns=list(patterns),
            scope=scope,
            pinned_scope=pinned_scope,
            output_json=output_json,
        )
    )


@ws_group.command("prune")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List what would be removed; don't delete anything.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip confirmation. Still refuses repos with uncommitted work or attached worktrees.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_prune(ctx: click.Context, dry_run: bool, force: bool, output_json: bool):
    """Remove disk state for repos no longer in the workspace config.

    Detects orphan project clones under projects/, orphan standalone clones
    referenced by stale entries in .git/info/exclude, and broken symlinks
    under .claude/skills/ and .claude/agents/. Refuses to delete repos with
    uncommitted changes or attached worktrees.
    """
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.prune(WorkspacePruneParams(dry_run=dry_run, force=force, output_json=output_json))


@ws_group.command("index")
@click.argument("name")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_index(ctx: click.Context, name: str, output_json: bool):
    """Print the port-offset index for a feature environment name.

    Greek letters get fixed indices 1..24. Any other name is hashed
    deterministically into 26..281 via SHA-1.
    """
    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.index(EnvIndexParams(name=name, output_json=output_json))


@ws_group.command("diff")
@click.argument("env")
@click.option("--staged", is_flag=True, help="Show staged changes (index vs HEAD).")
@click.option("--branch", is_flag=True, help="Show full branch diff (HEAD vs main).")
@click.option("--repo", default=None, help="Limit to a single repo.")
@click.option("--no-headers", is_flag=True, help="Omit repo separator headers.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def ws_diff(
    ctx: click.Context,
    env: str,
    staged: bool,
    branch: bool,
    repo: str | None,
    no_headers: bool,
    output_json: bool,
):
    """Show unified diff across all repos in a feature environment."""
    if staged and branch:
        raise click.ClickException("--staged and --branch are mutually exclusive")

    if staged:
        mode = DiffMode.staged
    elif branch:
        mode = DiffMode.branch
    else:
        mode = DiffMode.uncommitted

    container = cli_ctx(ctx).container
    handler = container.workspace_handler()
    handler.diff(
        EnvDiffParams(
            env=env,
            mode=mode,
            repo_filter=repo,
            no_headers=no_headers,
            output_json=output_json,
        )
    )


@click.group("repo")
def repo_group():
    """Manage repositories."""


@repo_group.command("list")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def repo_list(ctx: click.Context, output_json: bool):
    """List all repositories."""
    container = cli_ctx(ctx).container
    handler = container.repo_handler()
    handler.list(RepoListParams(output_json=output_json))


@repo_group.command("add")
@click.argument("url")
@click.option(
    "--standalone",
    is_flag=True,
    default=False,
    help="Add as a standalone repository instead of a project repository.",
)
@click.option("--name", default=None, help="Override URL-derived name.")
@click.option("--main-branch", default=None, help="Per-repo main branch (overrides workspace default).")
@click.option(
    "--git-exclude",
    "git_excludes",
    multiple=True,
    help="Add a .git/info/exclude entry (repeatable).",
)
@click.option("--cmd", "cmds", multiple=True, help="Post-clone command to run (repeatable).")
@click.option("--pinned", is_flag=True, default=False, help="Pin the repo to its main branch (project only).")
@click.option(
    "--path",
    default=None,
    help="Override clone path (standalone only, relative to workspace root).",
)
@click.option("--prefix", default=None, help="Extension symlink prefix (standalone only).")
@click.option(
    "--local",
    is_flag=True,
    default=False,
    help="Write to config.local.toml instead of the shared config.toml.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def repo_add(
    ctx: click.Context,
    url: str,
    standalone: bool,
    name: str | None,
    main_branch: str | None,
    git_excludes: tuple[str, ...],
    cmds: tuple[str, ...],
    pinned: bool,
    path: str | None,
    prefix: str | None,
    local: bool,
    output_json: bool,
):
    """Add a repository to the workspace config.

    URL may be http(s)://, ssh://, git://, or user@host:path. Defaults to a
    project repository; pass --standalone to add as a standalone instead.
    Writes to .winter/config.toml unless --local is given, in which case the
    entry lands in .winter/config.local.toml (auto-created if missing).
    """
    container = cli_ctx(ctx).container
    handler = container.repo_handler()
    handler.add(
        RepoAddParams(
            url=url,
            standalone=standalone,
            name=name,
            main_branch=main_branch,
            git_excludes=list(git_excludes),
            cmd=list(cmds),
            pinned=pinned,
            path=path,
            prefix=prefix,
            local=local,
            output_json=output_json,
        )
    )


@repo_group.command("remove")
@click.argument("target")
@click.option(
    "--local",
    is_flag=True,
    default=False,
    help="Remove from config.local.toml instead of the shared config.toml.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def repo_remove(ctx: click.Context, target: str, local: bool, output_json: bool):
    """Remove a repository from the workspace config.

    \b
    TARGET takes the form '<type>/<name>':
      winter repo remove project/winter
      winter repo remove standalone/winter-harness
    """
    if "/" not in target:
        raise click.ClickException(
            "Argument must be in the form '<type>/<name>' (e.g. project/winter, standalone/winter-harness)"
        )
    kind, _, name = target.partition("/")
    container = cli_ctx(ctx).container
    handler = container.repo_handler()
    handler.remove(RepoRemoveParams(kind=kind, name=name, local=local, output_json=output_json))
