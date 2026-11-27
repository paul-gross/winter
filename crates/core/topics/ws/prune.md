---
id: ws/prune
title: Remove material no configuration declares
---

`winter-workspace ws prune` finds material the workspace once managed and no configuration declares now, lists it,
asks, and removes what cannot hold work. Lock and state records are not changed.

## What it finds

- **`project_clone`:** a directory under `projects/` that no declared project names.
- **`standalone_clone`:** a path named by a workspace exclude block whose extension is no longer declared.
- **`broken_symlink`:** a link in `.claude/skills`, `.claude/agents`, or `.codex/skills` whose target does not exist.
- **`orphan_agent_copy`:** an agent file in `.claude/agents`, `.codex/agents`, or `.opencode/agent` that such a block
  names by its prefix.

The checkout of a declared repository, and a directory that holds one, is never a candidate, whatever a block names.

Each line of the preview names the kind, the path, and the evidence that the workspace managed it: `projects/`, the
exclude block, or a dangling link.

## What stays

A clone is retained, marked `!` with the reason under it, when the first of these holds:

1. it resolves outside the workspace;
2. it is not a git clone: it holds no `.git`;
3. it is a linked worktree: its `.git` is a file;
4. it has linked worktrees;
5. it has uncommitted or untracked changes, or its status cannot be read;
6. it has commits no remote-tracking branch holds, or its history cannot be read;
7. it has stashes, or its stash list cannot be read.

Links and agent copies are never retained. Ignored files do not count as changes and go with a removed clone. Every
candidate is checked again just before its removal, and one that changed since the scan is skipped. Retained material
is never removed by this command, under any option: delete it by hand once you are sure nothing in it is wanted.

## Options

- `winter-workspace ws prune --dry-run` prints the preview and changes nothing.
- `winter-workspace ws prune --force` removes without asking. It does not remove what is retained.
- `winter-workspace ws prune --json --dry-run` and `winter-workspace ws prune --json --force` print one JSON document
  with each orphan's `kind`, `path`, `safe_to_remove`, `notes`, and `action`. `--json` needs one of the two: alone it
  is refused with exit code 1 before anything is scanned, because JSON output never prompts. With `--force`, event lines
  of the exclude refresh may come before the document, which then carries `excludes_updated`.

Without `--force` the command asks `Remove <n> orphan(s)?` and reads the answer from stdin, a pipe included: `y` or
`yes` removes; `n`, an empty line, or the end of input exits 1 with `aborted` and removes nothing. When every orphan
is retained it removes nothing, does not ask, and exits 0.

After a removal the exclude blocks of extensions are refreshed; the block of anything retained is kept as it is. The
exit code is 0 once the removals ran, whatever each reported: read the `remove`, `skip`, and `error` lines.

Read `winter-workspace context ws/init` to restore a declared checkout that is missing.
