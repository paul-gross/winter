from __future__ import annotations

import dataclasses
from collections import Counter

from winter_cli.modules.workspace.models import (
    BoundarySource,
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    RestackLink,
    RestackLinkRepo,
    RestackPlan,
    RestackPlanRefusal,
    RestackRefusal,
    Workspace,
)
from winter_cli.modules.workspace.ref_resolver import chain_element_ref, resolve_ref
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository


@dataclasses.dataclass
class _RepoRead:
    """One repo's raw, uninterpreted reads for one link — gathered once so
    every guard below reasons from the same frozen values instead of
    re-reading git."""

    worktree: FeatureWorktree
    predecessor_ref: str
    """The predecessor ref string actually passed to git —
    `refs/heads/<name>` (`chain_element_ref`) for every link but the
    bottom-most, whose predecessor is the trailing base, an arbitrary ref
    left as-is after its own `{main}`-token expansion."""
    env_tip: str | None
    predecessor_tip: str | None


@dataclasses.dataclass
class _ParticipatingRead:
    """`_RepoRead` narrowed to the case both tips actually resolved — the
    shape every guard past participation reasons over, so none of them
    re-litigates whether a repo participates."""

    worktree: FeatureWorktree
    predecessor_ref: str
    env_tip: str
    predecessor_tip: str


def _participating(reads: dict[str, _RepoRead]) -> dict[str, _ParticipatingRead]:
    participating: dict[str, _ParticipatingRead] = {}
    for repo_name, r in reads.items():
        if r.env_tip is not None and r.predecessor_tip is not None:
            participating[repo_name] = _ParticipatingRead(
                worktree=r.worktree,
                predecessor_ref=r.predecessor_ref,
                env_tip=r.env_tip,
                predecessor_tip=r.predecessor_tip,
            )
    return participating


class EnvRestackPlanService:
    """Turns an already-shaped restack chain into a validated, per-link,
    per-repo `RestackPlan` — every boundary and every participating repo
    frozen from pre-run positions, and every refusal owned here.

    Read-only: takes `IReadRepoRepository` alone, so "refuse before any
    mutation" is a property of this class's constructor, not just its
    behavior. Nothing here computes an `--onto` execution target or an
    `up-to-date` verdict — both depend on a predecessor's tip *after* the
    link below it has run, which this service never does.
    """

    def __init__(self, repo_repo: IReadRepoRepository) -> None:
        self._repo_repo = repo_repo

    def plan(
        self,
        workspace: Workspace,
        project_repos: list[ProjectRepository],
        chain: list[str],
        base: str,
        cut: str | None = None,
    ) -> RestackPlan:
        """Plan a restack of `chain` (top of stack first, descending) onto
        `base`, across every non-pinned repo in `project_repos`.

        `chain` and `base` are taken as already-shaped: literal env names in
        argument order, with no glob expansion and no *shape* check (empty,
        glob-shaped, `/`-qualified) — that belongs to the command body that
        calls this. A non-empty `chain` is this method's own precondition
        rather than the command body's to keep, though: this is a public,
        independently-constructed collaborator, not a detail private to the
        command body that happens to be the only caller today, and
        `_missing_ref_refusals` below indexes the bottom-most link
        unconditionally — an empty `chain` would otherwise surface as a bare
        `IndexError` deep inside a private helper instead of a contract
        violation at the boundary that actually broke it. `cut` is an
        arbitrary ref, resolved per repo like `base`, and supplies the
        bottom-most link's boundary in place of a derived fork point.
        """
        assert chain, "EnvRestackPlanService.plan requires a non-empty chain"
        repos = [r for r in project_repos if not r.pinned]
        arg_links = [(chain[i], chain[i + 1] if i + 1 < len(chain) else base) for i in range(len(chain))]
        bottom_index = len(chain) - 1

        repeated = self._repeated_element(chain, base, cut)
        if repeated is not None:
            env, predecessor, element = repeated
            return RestackPlan(
                refusals=[RestackPlanRefusal(RestackRefusal.refused_inverted_order, env, predecessor, element=element)]
            )

        reads = self._gather_reads(workspace, repos, arg_links, bottom_index)

        refusals: list[RestackPlanRefusal] = list(self._missing_ref_refusals(arg_links, reads))

        boundaries: list[dict[str, tuple[str, BoundarySource]]] = [{} for _ in arg_links]
        for i, (env_name, predecessor_name) in enumerate(arg_links):
            participating = _participating(reads[i])

            refusals.extend(self._worktree_safety_refusals(env_name, predecessor_name, participating))

            if i == bottom_index and cut is not None:
                refusals.extend(
                    self._cut_boundaries(env_name, predecessor_name, repos, participating, cut, boundaries[i])
                )
            else:
                refusals.extend(self._fork_point_boundaries(env_name, predecessor_name, participating, boundaries[i]))

            inverted = self._inverted_order_refusal(env_name, predecessor_name, participating, boundaries[i])
            if inverted is not None:
                refusals.append(inverted)

        if refusals:
            return RestackPlan(refusals=refusals)

        links: list[RestackLink] = []
        for i in reversed(range(len(chain))):
            env_name, predecessor_name = arg_links[i]
            link_repos = []
            for repo in repos:
                frozen = boundaries[i].get(repo.name)
                boundary, source = frozen if frozen is not None else (None, None)
                link_repos.append(RestackLinkRepo(repo_name=repo.name, boundary=boundary, source=source))
            links.append(RestackLink(env=env_name, predecessor=predecessor_name, repos=link_repos))

        return RestackPlan(links=links)

    def _gather_reads(
        self,
        workspace: Workspace,
        repos: list[ProjectRepository],
        arg_links: list[tuple[str, str]],
        bottom_index: int,
    ) -> list[dict[str, _RepoRead]]:
        """One `get_ref_tip` pair per link per repo — the env branch and its
        predecessor, both read from the env's own worktree. Worktrees of one
        project repo share a ref namespace, so the predecessor (another
        chain element, or the base) resolves there too whenever the env's
        own worktree is provisioned; when it isn't, both reads come back
        `None` and the repo is `skipped` for that link without ever probing
        worktree status.

        A chain element — the env itself, and the predecessor of every link
        but the bottom-most — is resolved as `refs/heads/<name>` specifically,
        never a bare name: participation is meant to mean "this repo has
        that env's local branch", and a bare name would also match a tag of
        the same name (`gitrevisions(7)`'s disambiguation order checks
        `refs/tags/<name>` ahead of `refs/heads/<name>`) with no local
        branch behind it at all, silently pulling a repo into a link it has
        no business joining. `BASE` and `--cut` stay arbitrary refs, resolved
        by the caller (`resolve_ref`, above) and passed through unwrapped —
        that flexibility is by design for those two, never for a chain
        element. The scoped predecessor ref is stored on `_RepoRead` itself
        (not just used to read `predecessor_tip` here), so every later guard
        that hands `predecessor_ref` back to git (`_fork_point_boundaries`)
        inherits the same scoping rather than reaching for the bare name
        again.
        """
        reads: list[dict[str, _RepoRead]] = []
        for i, (env_name, predecessor_name) in enumerate(arg_links):
            is_bottom = i == bottom_index
            environment = self._environment(workspace, env_name)
            per_repo: dict[str, _RepoRead] = {}
            for repo in repos:
                worktree = FeatureWorktree(workspace=workspace, environment=environment, repository=repo)
                predecessor_ref = (
                    resolve_ref(predecessor_name, repo) if is_bottom else chain_element_ref(predecessor_name)
                )
                per_repo[repo.name] = _RepoRead(
                    worktree=worktree,
                    predecessor_ref=predecessor_ref,
                    env_tip=self._repo_repo.get_ref_tip(worktree, chain_element_ref(env_name)),
                    predecessor_tip=self._repo_repo.get_ref_tip(worktree, predecessor_ref),
                )
            reads.append(per_repo)
        return reads

    def _environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        # Index is unused by restack — only `path` (fed to every
        # `FeatureWorktree` above) and `name` matter here, so a dummy index
        # avoids pulling in the env-index registry just to number an env this
        # service never renders in an ordered list.
        return FeatureEnvironment(workspace=workspace, name=name, index=0, path=workspace.root_path / name)

    def _repeated_element(self, chain: list[str], base: str, cut: str | None) -> tuple[str, str, str | None] | None:
        """The (env, predecessor, element) of the link to attribute a
        repeated-element refusal to, or `None` when no element repeats.

        An "element" is any chain member, the trailing base, or `--cut` — the
        cut ref counts too, since `restack env2 master --cut env2` would
        otherwise pass every other guard and replay nothing while
        force-moving `env2` onto `master`. A chain/base repeat already names
        the colliding value through `env` itself (the loop below finds it by
        equality), so `element` is `None` there — the same "every other
        refusal" default `RestackPlanRefusal.element` documents. A `--cut`
        collision doesn't: `(chain[-1], base)` alone reads identically to a
        bottom link with no `--cut` involved at all, naming neither `--cut`
        nor the value that collided, so `element` carries the literal cut
        ref there.
        """
        chain_and_base = [*chain, base]
        counts = Counter(chain_and_base)
        repeated_value = next((value for value, count in counts.items() if count > 1), None)
        if repeated_value is not None:
            for i, env_name in enumerate(chain):
                if env_name == repeated_value:
                    predecessor = chain[i + 1] if i + 1 < len(chain) else base
                    return env_name, predecessor, None
        if cut is not None and cut in chain_and_base:
            return chain[-1], base, cut
        return None

    def _missing_ref_refusals(
        self, arg_links: list[tuple[str, str]], reads: list[dict[str, _RepoRead]]
    ) -> list[RestackPlanRefusal]:
        """An element that resolves in no repo at all — distinct from a repo
        simply not participating in one link. Left unrefused, a mistyped
        element plans `skipped` everywhere and the run exits 0 having done
        nothing.

        The base limb is gated on the bottom link's *participating* set —
        repos where the bottom env's own branch actually resolved — rather
        than every repo unconditionally. `predecessor_tip` is read from the
        bottom env's own worktree (`_gather_reads`'s per-repo `FeatureWorktree`
        is built from `env_name`, not `predecessor_name`), so a chain
        element that resolves nowhere leaves that worktree unopenable in
        every repo; reading the base through it then comes back `None`
        everywhere too, for a reason that has nothing to do with the base
        itself. Without the gate, a single mistyped chain element fires
        both limbs — two byte-identical refusals for one root cause. When
        no repo participates at all, there is nothing to gate the base
        check on, so it stays silent and the element limb above is left to
        carry the refusal alone.
        """
        refusals: list[RestackPlanRefusal] = []
        for i, (env_name, predecessor_name) in enumerate(arg_links):
            if all(r.env_tip is None for r in reads[i].values()):
                refusals.append(
                    RestackPlanRefusal(RestackRefusal.refused_missing_ref, env_name, predecessor_name, element=env_name)
                )
        bottom_env, bottom_predecessor = arg_links[-1]
        bottom_participants = [r for r in reads[-1].values() if r.env_tip is not None]
        if bottom_participants and all(r.predecessor_tip is None for r in bottom_participants):
            refusals.append(
                RestackPlanRefusal(
                    RestackRefusal.refused_missing_ref,
                    bottom_env,
                    bottom_predecessor,
                    element=bottom_predecessor,
                )
            )
        return refusals

    def _worktree_safety_refusals(
        self, env_name: str, predecessor_name: str, participating: dict[str, _ParticipatingRead]
    ) -> list[RestackPlanRefusal]:
        """The three worktree guards, over participating repos only — a repo
        whose env branch doesn't resolve there never reaches
        `get_worktree_status` or `is_rebase_in_progress` at all."""
        dirty: list[str] = []
        rebase_in_progress: list[str] = []
        detached_head: list[str] = []
        for repo_name, r in participating.items():
            status = self._repo_repo.get_worktree_status(r.worktree)
            if status.staged_count > 0 or status.unstaged_count > 0:
                dirty.append(repo_name)
            if self._repo_repo.is_rebase_in_progress(r.worktree):
                rebase_in_progress.append(repo_name)
            if status.branch != env_name:
                detached_head.append(repo_name)

        refusals: list[RestackPlanRefusal] = []
        if dirty:
            refusals.append(RestackPlanRefusal(RestackRefusal.refused_dirty, env_name, predecessor_name, sorted(dirty)))
        if rebase_in_progress:
            refusals.append(
                RestackPlanRefusal(
                    RestackRefusal.refused_rebase_in_progress, env_name, predecessor_name, sorted(rebase_in_progress)
                )
            )
        if detached_head:
            refusals.append(
                RestackPlanRefusal(
                    RestackRefusal.refused_detached_head, env_name, predecessor_name, sorted(detached_head)
                )
            )
        return refusals

    def _fork_point_boundaries(
        self,
        env_name: str,
        predecessor_name: str,
        participating: dict[str, _ParticipatingRead],
        out: dict[str, tuple[str, BoundarySource]],
    ) -> list[RestackPlanRefusal]:
        """`git merge-base --fork-point <predecessor> <env>` for every
        participating repo — the predecessor's reflog, not its current
        graph, so a rewrite of the predecessor doesn't pull its own past
        commits back into the env's replay.

        `env_name` is scoped through `chain_element_ref` here too, same as
        `r.predecessor_ref` already is: a same-named tag on the *env* side
        makes `git merge-base --fork-point <pred> <env>` either resolve the
        wrong (tag) commit or, when the *predecessor* side is also
        ambiguous, exit 128 with "Ambiguous refname" — read by `fork_point`
        as no fork point at all, refusing `refused-unknown-boundary` for a
        cause that refusal's documented recourse doesn't cover.
        """
        unknown: list[str] = []
        for repo_name, r in participating.items():
            boundary = self._repo_repo.fork_point(r.worktree, r.predecessor_ref, chain_element_ref(env_name))
            if boundary is None:
                unknown.append(repo_name)
            else:
                out[repo_name] = (boundary, BoundarySource.fork_point)
        if unknown:
            return [
                RestackPlanRefusal(RestackRefusal.refused_unknown_boundary, env_name, predecessor_name, sorted(unknown))
            ]
        return []

    def _cut_boundaries(
        self,
        env_name: str,
        predecessor_name: str,
        repos: list[ProjectRepository],
        participating: dict[str, _ParticipatingRead],
        cut: str,
        out: dict[str, tuple[str, BoundarySource]],
    ) -> list[RestackPlanRefusal]:
        """`--cut`'s per-repo classification for the bottom-most link: the
        cut's tip stands in for a fork point, read directly rather than
        derived. A repo where the cut's tip is an ancestor of the env branch
        gets it as its boundary outright; one where it isn't, but the base's
        tip already is, reads the cut as already applied — still frozen with
        the cut's tip, since it's the execution-time re-check of that same
        ancestry, not a different boundary, that turns this into a no-op; a
        repo matching neither has nothing for the cut to assert and refuses.
        """
        repos_by_name = {repo.name: repo for repo in repos}
        missing_ref: list[str] = []
        not_ancestor: list[str] = []
        for repo_name, r in participating.items():
            cut_ref = resolve_ref(cut, repos_by_name[repo_name])
            cut_tip = self._repo_repo.get_ref_tip(r.worktree, cut_ref)
            if cut_tip is None:
                missing_ref.append(repo_name)
                continue
            cut_applies = self._repo_repo.is_ancestor(r.worktree, cut_tip, r.env_tip)
            cut_already_done = self._repo_repo.is_ancestor(r.worktree, r.predecessor_tip, r.env_tip)
            if cut_applies or cut_already_done:
                out[repo_name] = (cut_tip, BoundarySource.cut)
            else:
                not_ancestor.append(repo_name)

        refusals: list[RestackPlanRefusal] = []
        if missing_ref:
            refusals.append(
                RestackPlanRefusal(
                    RestackRefusal.refused_missing_ref,
                    env_name,
                    predecessor_name,
                    sorted(missing_ref),
                    element="--cut",
                )
            )
        if not_ancestor:
            refusals.append(
                RestackPlanRefusal(
                    RestackRefusal.refused_cut_not_ancestor, env_name, predecessor_name, sorted(not_ancestor)
                )
            )
        return refusals

    def _inverted_order_refusal(
        self,
        env_name: str,
        predecessor_name: str,
        participating: dict[str, _ParticipatingRead],
        boundaries: dict[str, tuple[str, BoundarySource]],
    ) -> RestackPlanRefusal | None:
        """Whether this link is a chain-level inversion — judged once per
        link across every repo that got a boundary, never per repo.

        A repo where the env carries no commits past its frozen boundary
        (its tip equals the boundary sha) is exempt: nothing there is at
        risk, and the link fast-forwards. The refusal fires only when no
        participating repo is exempt and the env's tip is a proper ancestor
        of the predecessor's pre-run tip in every one of them — a lone
        exempt repo, common when a healthy stack sits at parity with its
        predecessor everywhere but where new work landed, holds the
        refusal's fire.
        """
        if not boundaries:
            return None
        if any(participating[repo_name].env_tip == boundary for repo_name, (boundary, _source) in boundaries.items()):
            return None
        for repo_name in boundaries:
            r = participating[repo_name]
            if r.env_tip == r.predecessor_tip:
                return None
            if not self._repo_repo.is_ancestor(r.worktree, r.env_tip, r.predecessor_tip):
                return None
        return RestackPlanRefusal(
            RestackRefusal.refused_inverted_order, env_name, predecessor_name, sorted(boundaries.keys())
        )
