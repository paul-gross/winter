# Environment lifecycle

A feature environment moves through four phases. Winter owns the sequence and one command per phase; each command's
mechanics live in its own reference, linked below — this page is the map, not the manual.

| Phase      | Command                   | Brings                                                                                  |
| ---------- | ------------------------- | --------------------------------------------------------------------------------------- |
| Structural | `winter ws init <env>`    | worktrees, branches, env-index; runs each repo's lightweight `cmd` trust/bootstrap step |
| Readiness  | `winter provision <env>`  | dependencies, resources (databases, queues, buckets), DB migrations, seed data          |
| Run        | `winter service up <env>` | starts the env's services                                                               |
| Teardown   | `winter ws destroy <env>` | provision teardown → extension hooks → worktree/env removal                             |

`winter clean <env>` sits outside this four-phase sequence: it is a handover reset between tenants, not a phase every
env passes through once. See [winter-cli/usage/clean.md](./winter-cli/usage/clean.md) for what it runs and why it
exists.

## Baseline rule: never run an env you have not provisioned

**Structural creation is not readiness.** `winter ws init` gives you worktrees and an env index; it does **not** install
dependencies, migrate databases, or load seed data. `winter service up` starts services against the environment *as it
is* — it does not provision on your behalf. Starting services against an un-provisioned env yields missing dependencies,
un-migrated schemas, and empty seed data, and the environment looks broken.

So before you run or exercise an environment for the first time — or after anything that invalidates its state — run
`winter provision <env>`. Provision is a baseline operation, not an optional extra: `init` → `provision` → `service up`,
in that order. (The two commands stay separate in both directions: `service up` never provisions, and `provision` has
its own `required_services` gate for handlers that need a service running.)

## Provision and the service interface are winter concepts

`winter provision` is a core winter command, and its `dependency → resource → data` chain is winter's, not any
extension's. Service orchestration is likewise a winter-defined interface — the `service` capability slot, with a core
command surface (`winter service …`) and a provider contract. The tmux and docker extensions only *implement* that slot;
the lifecycle, the commands, and the ordering rule above are winter's regardless of which provider (if any) is bound.

## Where each phase is documented

- Structural + teardown mechanics, and the raw-git equivalents: [worktree-ops.md](./worktree-ops.md)
- Readiness command surface (stages, action flags, named selectors):
  [winter-cli/usage/provision.md](./winter-cli/usage/provision.md)
- Run command surface (`up`/`down`/`status`/`restart`, `--wait`, scopes):
  [winter-cli/usage/service.md](./winter-cli/usage/service.md)
- The service provider protocol winter dispatches to:
  [winter-cli/contracts/service-orchestrator.md](./winter-cli/contracts/service-orchestrator.md)
- Handover command surface (stages, named selectors, declared `clean` commands):
  [winter-cli/usage/clean.md](./winter-cli/usage/clean.md)
