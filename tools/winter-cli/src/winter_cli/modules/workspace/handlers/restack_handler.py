from __future__ import annotations

import dataclasses
import sys
from typing import Any

import click

from winter_cli.core.cli_output_service import ICliOutputService
from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.handlers.json_render import echo_json, to_dict
from winter_cli.modules.workspace.models import (
    RestackConflict,
    RestackFailure,
    RestackLink,
    RestackLinkRepo,
    RestackOutcome,
    RestackPlan,
    RestackReport,
    Workspace,
)
from winter_cli.modules.workspace.repository_factory import RepositoryFactory


@dataclasses.dataclass
class RestackParams:
    chain: list[str]
    """Chain positionals, top of stack first — the command body has already
    shaped these (arity, no glob, no `/`), so this class carries them as
    plain literal env names."""

    base: str
    """The trailing positional the bottom-most chain element rebases onto —
    an arbitrary ref, resolved per repo (including `{main}`-style tokens)
    by the plan service, never by this handler."""

    cut: str | None = None
    dry_run: bool = False
    output_json: bool = False


class RestackHandler:
    """Handles `winter ws restack` invocations: plans the chain, then either
    previews it (`--dry-run`) or executes it, rendering the result as text
    or one `--json` document and exiting 1 on refusal or conflict.

    Takes its own `EnvRestackPlanService` / `EnvRestackService` pair rather
    than routing through the omnibus `WorkspaceHandler` — the plan/execute
    split (Decision 7) is a property of the services, and this class keeps
    that same separation on its own side of the seam: it always plans first,
    and only ever reaches `EnvRestackService` when the plan is unrefused and
    `--dry-run` was not asked for. A `--dry-run` run therefore makes zero
    calls into the write-capable execute service at all.
    """

    def __init__(
        self,
        plan_svc: EnvRestackPlanService,
        execute_svc: EnvRestackService,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        cli_output_svc: ICliOutputService,
    ) -> None:
        self._plan_svc = plan_svc
        self._execute_svc = execute_svc
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._cli_output_svc = cli_output_svc

    def run(self, params: RestackParams) -> None:
        project_repos = self._repo_factory.get_project_repos()
        plan = self._plan_svc.plan(self._workspace, project_repos, params.chain, params.base, params.cut)

        if plan.refused:
            self._render_refusal(plan, params.output_json)
            sys.exit(1)

        if params.dry_run:
            self._render_plan(plan, params.output_json)
            return

        report = self._execute_svc.execute(self._workspace, project_repos, plan)
        self._render_report(plan, report, params.output_json)
        if not report.success:
            sys.exit(1)

    # ── refusal ──────────────────────────────────────────────────────────

    def _render_refusal(self, plan: RestackPlan, output_json: bool) -> None:
        if output_json:
            echo_json({"refusals": [to_dict(r) for r in plan.refusals]})
            return
        out = self._cli_output_svc
        click.echo(f"{out.style('✗', 'red')} restack refused — no changes made.")
        for refusal in plan.refusals:
            repos = f" ({', '.join(refusal.repos)})" if refusal.repos else ""
            element = f" [{refusal.element}]" if refusal.element is not None else ""
            click.echo(
                f"  {out.style(refusal.result.value, 'bold')}: {refusal.env} onto {refusal.predecessor}{repos}{element}"
            )

    # ── dry-run: the plan alone, no outcomes ────────────────────────────

    def _render_plan(self, plan: RestackPlan, output_json: bool) -> None:
        if output_json:
            echo_json({"links": [_link_plan_dict(link) for link in plan.links]})
            return
        out = self._cli_output_svc
        click.echo(f"{out.style('Plan', 'bold')} (dry run — no changes made):")
        for link in plan.links:
            self._render_link_table(link, outcome_by_key=None)

    # ── execution report: boundary, source, and outcome per repo ───────

    def _render_report(self, plan: RestackPlan, report: RestackReport, output_json: bool) -> None:
        outcome_by_key: dict[tuple[str, str], RestackOutcome] = {
            (o.env, o.repo_name): RestackOutcome(o.result.value) for o in report.completed
        }
        if report.conflict is not None:
            outcome_by_key[(report.conflict.env, report.conflict.repo_name)] = RestackOutcome.conflict

        if output_json:
            echo_json(
                {
                    "links": [_link_execution_dict(link, outcome_by_key) for link in plan.links],
                    "completed": [to_dict(o) for o in report.completed],
                    "conflict": to_dict(report.conflict) if report.conflict is not None else None,
                    "failure": to_dict(report.failure) if report.failure is not None else None,
                    "remaining": to_dict(report.remaining),
                }
            )
            return

        for link in plan.links:
            self._render_link_table(link, outcome_by_key)
        if report.conflict is not None:
            self._render_conflict(report.conflict)
        elif report.failure is not None:
            self._render_failure(report.failure)
        else:
            click.echo(f"\n{self._cli_output_svc.style('✓', 'green')} restack complete.")

    def _render_link_table(
        self, link: RestackLink, outcome_by_key: dict[tuple[str, str], RestackOutcome] | None
    ) -> None:
        out = self._cli_output_svc
        headers = ["REPO", "BOUNDARY", "SOURCE"] + (["OUTCOME"] if outcome_by_key is not None else [])
        rows: list[list[str]] = []
        for link_repo in link.repos:
            row = [
                link_repo.repo_name,
                link_repo.boundary or "-",
                link_repo.source.value if link_repo.source is not None else "-",
            ]
            if outcome_by_key is not None:
                outcome = _outcome_for(link, link_repo, outcome_by_key)
                row.append(outcome.value if outcome is not None else "-")
            rows.append(row)
        click.echo(f"\n{out.style(link.env, 'bold')} onto {link.predecessor}")
        for line in out.render_table(rows, headers=headers):
            click.echo(line)

    def _render_conflict(self, conflict: RestackConflict) -> None:
        out = self._cli_output_svc
        click.echo(
            f"\n{out.style('✗', 'red')} Conflict replaying {out.style(conflict.replayed_commit, 'bold')} "
            f"in {out.style(conflict.env, 'bold')} onto {out.style(conflict.predecessor, 'bold')} "
            f"({out.style(conflict.repo_name, 'bold')})."
        )
        click.echo("Conflicted files:")
        for path in conflict.conflicted_paths:
            click.echo(f"  {path}")
        click.echo(
            "Resolve in that worktree and run `git rebase --continue`, then re-run "
            "the same restack command with the same --cut to continue."
        )

    def _render_failure(self, failure: RestackFailure) -> None:
        out = self._cli_output_svc
        click.echo(
            f"\n{out.style('✗', 'red')} {out.style(failure.repo_name, 'bold')} "
            f"({out.style(failure.env, 'bold')}) failed: {failure.message}"
        )


def _outcome_for(
    link: RestackLink, link_repo: RestackLinkRepo, outcome_by_key: dict[tuple[str, str], RestackOutcome]
) -> RestackOutcome | None:
    """The render vocabulary's outcome for one repo of one link — `skipped`
    for a non-participating repo, whatever `outcome_by_key` carries for a
    participating one (`None` for a repo the run never reached, e.g. one
    past a conflict or failure stop)."""
    if not link_repo.participates:
        return RestackOutcome.skipped
    return outcome_by_key.get((link.env, link_repo.repo_name))


def _repo_plan_dict(link_repo: RestackLinkRepo) -> dict[str, Any]:
    return {
        "repo_name": link_repo.repo_name,
        "boundary": link_repo.boundary,
        "source": link_repo.source.value if link_repo.source is not None else None,
    }


def _link_plan_dict(link: RestackLink) -> dict[str, Any]:
    return {"env": link.env, "predecessor": link.predecessor, "repos": [_repo_plan_dict(r) for r in link.repos]}


def _link_execution_dict(link: RestackLink, outcome_by_key: dict[tuple[str, str], RestackOutcome]) -> dict[str, Any]:
    repos: list[dict[str, Any]] = []
    for link_repo in link.repos:
        view = _repo_plan_dict(link_repo)
        outcome = _outcome_for(link, link_repo, outcome_by_key)
        view["outcome"] = outcome.value if outcome is not None else None
        repos.append(view)
    return {"env": link.env, "predecessor": link.predecessor, "repos": repos}
