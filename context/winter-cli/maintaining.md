# Maintaining the winter CLI docs

Author- and reviewer-facing rules for changing the agent-facing markdown under `context/winter-cli/`.
Read this before adding, moving, or rewriting a CLI doc; cite it when reviewing such a change.
It governs *where each CLI fact lives*, *how the hubs route to it*, and *what must move when the CLI changes* — not what the CLI does (for that, start at the [hub](./index.md)).

These rules apply only to this tree. The tree is part of the winter core module, so its docs stay self-contained: a CLI doc never points at an extension's files (a `<ext>:/…` reference), because the extractability lint check ([configuration/lint.md](./configuration/lint.md)) treats a core→extension pointer as a layering inversion.

## The tree at a glance

Three surfaces, each a hub that routes to leaves, split by **who reads the fact and what they are doing with it**:

| Surface | Reader & task | Hub |
|---------|---------------|-----|
| `usage/` | The **operator** running a command — invocation, flags, examples, how to read the output. | [usage/index.md](./usage/index.md) |
| `configuration/` | The **author** editing `.winter/config.toml` or `winter-ext.toml` — keys, defaults, merge semantics, validation. | [configuration/index.md](./configuration/index.md) |
| `contracts/` | The **implementer** building a provider winter dispatches to — the wire protocol it conforms to. | (routed from the top [hub](./index.md)) |

Plus the top [hub](./index.md), which routes directly to the cross-cutting leaves — `workflows.md`, `resilience.md`, `root-flags.md`, `setup.md` — each described there by its read-trigger.

## Where each fact belongs

File a fact by the reader's task, not by the subsystem that implements it.

| A fact about… | Owner | Keep it out of |
|---------------|-------|----------------|
| How to *invoke* a command — argv grammar, flags, examples, output meaning | the command's `usage/` leaf | `configuration/`, the hubs |
| A `.winter/config.toml` / `winter-ext.toml` key, its default, validation, merge behavior | the concept's `configuration/` leaf | `usage/` (link to it instead) |
| The wire protocol a provider implements — invocation, injected env, stdout formats, exit codes | `contracts/` | `usage/`, `configuration/` (link to it) |
| A multi-command operating sequence (bootstrap, start a feature, tear down) | `workflows.md` | individual command leaves |
| Cross-cutting runtime behavior (retry, hung-call timeout, config↔filesystem drift) | `resilience.md` | individual command leaves |
| Framing of the topic + the routing table | the nearest hub `index.md` | (nothing else belongs in a hub) |

When a single command spans several reader tasks, it earns one leaf per surface, each pointing at the others: `winter service` has [usage/service.md](./usage/service.md) (operator), the `service` binding in [configuration/capabilities.md](./configuration/capabilities.md) (author), and [contracts/service-orchestrator.md](./contracts/service-orchestrator.md) (implementer).

### Paired usage/configuration topics

`doctor`, `lint`, `provision`, and `capabilities` each have **both** a `usage/` page and a `configuration/` page.
The split is sharp and must stay that way: the `usage/` page owns *running* the command (invocation, flags, how to read its output); the `configuration/` page owns *authoring* what the command consumes (the probe / check / handler / binding schema).
Neither restates the other — each points across the seam.
When you change one side, check the other still points correctly and does not now duplicate what you moved.

## Hubs and routing

- **Every leaf has exactly one routing row in its nearest hub.** Adding a leaf without a row makes it undiscoverable; the hub is the only path to it.
- **A new command group with several sub-commands becomes a sub-directory with its own hub**, the way `usage/ws/` does — not a pile of flat siblings under `usage/`.
- **A routing row states *when to read* the target, never *what is inside it*.** `…you need to start, stop, or inspect a feature env's services` is a trigger that survives edits to the target; a contents inventory is a second copy that goes stale the first time the target changes.
- **Lead a routing table with the link column**, the discriminator (the "when to read" trigger) to its right, so the destinations form one scannable left edge.
- **Hubs carry only framing, a parent pointer, and the routing table** — no schemas, option lists, procedures, or examples copied up from a leaf.
- **Enter at the hub for discovery; link deep only for an exact, already-known fact.** A reader still choosing which sub-topic they need is routed through the hub so the siblings stay visible; a sentence that names one specific anchor (`service.md#local-path-override`) links straight to it.

## Freshness checks

When the CLI changes, the docs move **in the same commit**. Match the change to its row:

| Change to the CLI | Docs that must move with it |
|-------------------|-----------------------------|
| **Add a command** | New `usage/` leaf (or a row in `usage/ws/index.md` for a `ws` sub-command); a routing row in its nearest hub. |
| **Remove a command** | Delete the leaf, delete its routing row, and grep the tree for inbound links to it (`grep -rn '<name>' context/winter-cli`). |
| **Rename a command or flag** | The leaf's title and every example; the routing row; deep links from other leaves; the agents that load these docs (see Consumers below). |
| **Add / change / remove a flag** | The command's `usage/` leaf — its example block and any behavior note. |
| **Add / rename / remove a config key** | The concept's `configuration/` leaf; `configuration/config-files.md` if it is a workspace-level scalar; the routing row if a whole concept appears or disappears. |
| **Change behavior** | The page that owns that fact. If the change crosses the usage↔configuration↔contract seam, edit the **one** owner and confirm the other pages still point at it rather than re-describing it. |
| **Add a capability slot or provider action** | [contracts/service-orchestrator.md](./contracts/service-orchestrator.md) (the wire contract), [configuration/capabilities.md](./configuration/capabilities.md) (binding), and the `usage/` output description. |

Verify the result with `winter lint --changed` ([usage/lint.md](./usage/lint.md)) before pushing.
Its built-in [core checks](./configuration/lint.md#built-in-core-checks) hold every doc under the `reference_bytes` size threshold and reject a pointer into another module; dead-link and routing-reference checking is contributed by a conventions extension rather than core, so it is not guaranteed present — grep inbound links by hand after a rename or move (the freshness rows above).
When a leaf outgrows `reference_bytes`, split the command group into its own sub-hub.

## Consumers to keep working

These docs are loaded as source of truth by agents, so a rename or move is only done when their references move too:

- The **worktree-ops agent** (`.claude/agents/worktree-ops.md`) `@`-loads [usage/index.md](./usage/index.md) and descends into the `winter ws` leaves. Keep the usage hub's routing complete and its leaf paths stable.
- In an assembled workspace (not the standalone repo), the workspace **`CLAUDE.md`** and **`context/workspace-layout.md`** name `context/winter-cli/` as the CLI command reference. A top-level restructure updates those downstream pointers in the same change.

## Reviewer checklist

Cite this list when reviewing a change to `context/winter-cli/`:

1. **Placement** — does each new or moved fact sit with the reader-task that owns it (usage / configuration / contracts / workflow), per the table above?
2. **Single owner** — is every fact stated once, with the other pages pointing rather than re-describing? Flag a duplicated schema, option list, flag set, default, or contract clause.
3. **Paired topics** — for `doctor` / `lint` / `provision` / `capabilities`, is the usage↔configuration seam clean, with no content bled across?
4. **Routing** — does every new leaf have one read-trigger routing row in its nearest hub, and does every removed leaf lose its row? Are the triggers precise enough to discriminate between siblings?
5. **Hub purity** — does each hub carry only framing and routing, with no leaf detail copied up?
6. **Freshness** — for the kind of CLI change in the diff, did every doc in its freshness row move?
7. **Self-containment** — no `<ext>:/…` pointer into an extension; no retrospective "previously / used to" framing.
