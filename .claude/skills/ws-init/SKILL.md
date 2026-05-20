---
name: ws-init
description: Non-interactive idempotent reconcile of the workspace, a feature environment, or a project repo against .winter/config.toml
model: opus
allowed-tools: Bash, Read
---

Bring the workspace, a single feature environment, or a single project repo to the state declared in `.winter/config.toml`. Non-interactive, idempotent, and safe to re-run any time.

`/ws-init` is the "I just cloned this — make it work" path. It only *applies* declared config; it never *changes* config. Anything that would require a configuration decision (declaring a new repo, setting workspace remote, choosing git identity) belongs in `/ws-setup`, not here.

The skill is a thin wrapper over `winter ws init`. It picks the right form, layers in per-environment post-init from `workspace:/ai/project/project-setup.md`, and halts cleanly on any of the precondition or runtime failures enumerated in the **Non-interactivity contract** below. See [ai/winter-cli/usage.md](./ai/winter-cli/usage.md) and [ai/worktree-ops.md](./ai/worktree-ops.md) for the underlying primitives.

## Preflight

Before dispatching, confirm the workspace is configured enough to run:

1. Read `.winter/config.toml`. If it doesn't exist, stop and report:
   > `.winter/config.toml` not found — this workspace isn't configured. Run `/ws-setup` to declare workspace remote, repos, and git identity. (If `/ws-setup` also reports config missing, create the file first: `mkdir -p .winter && touch .winter/config.toml`, then re-run.)
2. If `.winter/config.toml` has zero `[[project_repository]]` *and* zero `[[standalone_repository]]` entries, stop and report: "No repositories declared in `.winter/config.toml` — there's nothing to initialize. Run `/ws-setup` to declare repos." A workspace with only standalones (e.g. extension-only) is valid; don't halt on it.

Never edit `.winter/config.toml` or `.winter/config.local.toml` from this skill.

## Dispatch on the argument

Parse `$ARGUMENTS` for a single optional name.

- **No argument** → workspace mode.
- **A name** → resolve against the config and the filesystem:
  - declared `[[project_repository]]` or `[[standalone_repository]]` name → repo mode
  - directory `./<name>/` exists at the workspace root with worktree contents (i.e. a previously created feature environment) → environment mode
  - neither → treat as a new feature environment name (the CLI will create it on first init)

If a name matches **more than one** declared entity, or matches both a declared repo and an existing feature environment directory, stop and report the ambiguity — don't auto-resolve. Tell the user to rename one of them. (The skill has no way to disambiguate in this state: `winter ws init <name>` would treat the name as an env, and `winter ws init` would reconcile every source checkout — neither isolates a single repo, so the only durable fix is to make the name unambiguous in the config.) Don't attempt to disambiguate by directory carve-out lists.

## Workspace mode (no argument)

Run the catch-all reconcile:

```bash
winter ws init --all
```

This clones any missing project repos into `projects/`, ensures every declared standalone repo is cloned at its `path`, re-applies git identity / `cmd` / `git_excludes` everywhere, re-processes each standalone extension (rewriting `CLAUDE.winter.md` and the managed `<prefix>-*` symlinks in `.claude/`), and re-runs `winter ws init <env>` for every existing feature environment directory (which fires each extension's `on_env_init` hook again).

After it completes, surface the salient lines from the CLI's per-step output: what was cloned, what was already present, which envs were touched, any extension hooks that ran. If the CLI exits non-zero, stop and surface the failing per-repo errors — don't keep going.

## Environment mode (`<env>` argument)

Reconcile a single feature environment:

```bash
winter ws init <env>
```

If `./<env>/` doesn't already exist, this creates it (per-repo worktrees on branch `<env>`, `.winter.env` seeded with `WINTER_ENV` / `WINTER_ENV_INDEX` / `WINTER_PORT_BASE`, every extension's `on_env_init` hook fired). If it already exists, the CLI re-applies the per-repo reconcile and is a no-op for already-correct state.

If the CLI exits non-zero, stop and surface the per-repo errors — don't fall through to the post-init step on a half-built env.

Then, if `workspace:/ai/project/project-setup.md` exists, apply its post-init steps to `<env>`. Read the file and follow its instructions for this environment — it typically appends project-specific variables to `<env>/.winter.env` below the managed block, provisions per-environment databases, generates other env files, etc. If a step describes state that's already present (e.g. an env file that already has the project block), report it as `already-applied` and move on rather than re-running.

**`project-setup.md` is assumed agent-runnable without prompts.** If a step would require user input (a missing secret, a decision the config doesn't cover), surface that as a halt with the specific gap rather than asking the question. The non-interactivity contract extends to the post-init pass.

If `workspace:/ai/project/project-setup.md` doesn't exist, skip the post-init step and note that in the report — don't treat it as an error.

Report what changed: directories created vs. already present, hooks fired, per-step post-init results (`applied`, `already-applied`, `skipped (not present)` when the file is absent, or `halted (needs <gap>)` for an interactivity gap).

## Project repo mode (`<name>` argument)

Reconcile a single declared project (or standalone) repo. The CLI doesn't expose a per-repo init primitive, so use the workspace-level reconcile:

```bash
winter ws init
```

Same as workspace mode without `--all` — reconciles every source checkout (projects + standalones) and skips the per-env pass. Idempotent across all repos, so it's safe to run to satisfy a single-repo request. Note that this also re-processes standalone extensions (rewriting `CLAUDE.winter.md` and `.claude/` symlinks); that's a no-op when nothing has changed but worth mentioning so the user isn't surprised that the extension wiring may be touched.

If the CLI exits non-zero, stop and surface the per-repo errors.

Filter the report to focus on `<name>`: whether it was cloned this run, refreshed (cmd re-ran, excludes rewritten, extension reprocessed), or left unchanged. Mention any other side effects briefly so the user isn't surprised that other repos were touched.

## Non-interactivity contract

- Never ask the user a question whose answer is in the config — git identity, declared repos, declared standalones, `main_branch`, `cmd`, `pinned`, all come from `.winter/config.toml` and `.winter/config.local.toml`.
- Never ask the user to confirm a step. The CLI primitives are idempotent — just run them and report what happened.
- The skill halts and reports (without prompting) in five cases:
  1. `.winter/config.toml` missing
  2. Zero repositories declared (no `[[project_repository]]` *and* no `[[standalone_repository]]` entries)
  3. Target name is ambiguous (matches multiple declared entities, or both a repo and an existing env directory)
  4. The underlying CLI exits non-zero
  5. A `project-setup.md` post-init step would require user input (a missing secret, an unspecified decision)

In each halt case, report the gap and route the user appropriately — `/ws-setup` for missing/incomplete config, rename advice for ambiguity, the per-repo CLI errors for CLI failures, the specific gap for post-init interactivity. Never try to repair config.

## Report

End with a concise summary the user can scan in one read. Use this template:

```
## ws-init: <target or "workspace">

- <step>: <result>
- <step>: <result>
...

<closing line>
```

Where `<result>` is one of: `cloned`, `refreshed`, `skipped (already present)`, `applied`, `already-applied`, `skipped (not present)`, `halted (needs <gap>)`, or the specific error from the CLI. Use `already-applied` for a post-init step whose target state is already in place, and `skipped (not present)` only when the `project-setup.md` file itself doesn't exist. The closing line is:

- `Already in declared state. Re-run any time.` — when every step was a no-op
- A one-line summary of what changed — when any step did work (e.g. `Cloned 1 repo, refreshed 3, fired 1 extension hook.`)
- The halt reason and route — when the skill stopped on a precondition (e.g. `Halted: no [[project_repository]] entries declared. Run /ws-setup.`)

$ARGUMENTS
