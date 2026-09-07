from __future__ import annotations

import re

import click

from winter_cli.modules.workspace.models.domain_model import ProjectRepository, StandaloneRepository

ALIASES: tuple[str, ...] = ("main", "master", "default")
"""Interchangeable `{...}` token names — each expands to the repo's configured main branch."""

_TOKEN_RE = re.compile(r"(?<!@)\{([^{}]*)\}")


def chain_element_ref(name: str) -> str:
    """`name` as the *local branch* a restack chain element must resolve to,
    `refs/heads/<name>` — never a bare name.

    Git's own disambiguation order (`gitrevisions(7)`) checks
    `refs/tags/<name>` *ahead of* `refs/heads/<name>`, so a bare name would
    resolve a repo's tag of the same name even with no local branch at all —
    the one non-branch shape guaranteed to win over an absent branch — and,
    when both a tag and a branch of that name exist, `git merge-base
    --fork-point` on the bare name either resolves to the wrong (tag) commit
    or refuses outright with "Ambiguous refname". A chain element names a
    branch specifically — the env being restacked, or another chain element
    it replays onto — everywhere it is handed to git for a read (`get_ref_tip`,
    `is_ancestor`, `fork_point`), in both `EnvRestackPlanService` and
    `EnvRestackService`.

    Never applied to `BASE` or `--cut`: both are arbitrary refs by design,
    resolved instead by `resolve_ref` above. Also never applied to the
    `branch` argument `rebase_onto` passes to `git rebase --onto` — passing a
    fully-qualified ref there leaves HEAD detached instead of attached to the
    branch (git's own checkout-disambiguation, invoked for a bare name,
    already resolves to the local branch over a same-named tag), so that
    argument stays a bare chain-element name.
    """
    return f"refs/heads/{name}"


def resolve_ref(ref: str, repo: ProjectRepository | StandaloneRepository) -> str:
    """Expand per-repo `{main}` / `{master}` / `{default}` tokens in `ref` against `repo.main_branch`.

    Config-only, no network. The three aliases are interchangeable — each expands
    to `repo.main_branch`, which already folds the per-repo override over the
    workspace default (`RepositoryFactory` resolves that once at construction, so
    by the time a repo reaches this function `main_branch` is the effective
    value). A token may appear anywhere in `ref` — bare `{main}` and
    `origin/{main}` both work. A `ref` with no `{...}` token is returned
    unchanged, byte-for-byte. Git's own `@{...}` syntax (`@{u}`, `HEAD@{1}`,
    `master@{yesterday}`) is never treated as a winter token — only an
    unprefixed `{...}` group is — so those refs pass through unchanged for
    git to resolve natively.

    Raises `click.ClickException` — refusing before any git operation runs — when
    `ref` contains a `{...}` token that isn't one of the accepted aliases, naming
    the unknown token and listing the accepted set. Braces rather than angle
    brackets: `<main>` is a shell redirection operator and never reaches the CLI
    in the first place. Also raises when a token expands but `repo.main_branch`
    is unset, naming the repo, rather than silently substituting an empty
    segment.
    """

    def _expand(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in ALIASES:
            accepted = ", ".join(f"{{{alias}}}" for alias in ALIASES)
            raise click.ClickException(f"Unknown ref token '{{{token}}}' in '{ref}' — accepted aliases: {accepted}")
        if not repo.main_branch:
            raise click.ClickException(
                f"Repo '{repo.name}' has no main_branch configured — cannot expand '{{{token}}}' in '{ref}'"
            )
        return repo.main_branch

    return _TOKEN_RE.sub(_expand, ref)
