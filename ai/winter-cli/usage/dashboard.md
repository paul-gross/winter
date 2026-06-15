# `winter dashboard` — interactive TUI

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter dashboard
```

Interactive TUI showing workspace status, feature environments, and repo details. Navigate with keyboard. Every key below is the **default** for a stable *action id* and can be remapped — see [Keybindings](#keybindings).

**Default keys:**

- `r` — refresh. `L` — open the **Log tab** (captured `RepoError` entries with subcommand, args, cwd, and stderr; inspect a failure without re-running the command). `q` — quit (workspace) / back (detail screens).
- `enter` — drill into the focused row's detail view. `h` / `j` / `k` / `l` — move the detail-screen cursor.
- `ctrl+k` / `ctrl+j` — jump table focus.
- `c` — clear the Log tab (Log screen only; not remappable).

**Tracking glyphs** in the repo rows: `[+N, -N]` shows commits ahead/behind upstream; `[+]` marks an unborn upstream ref (the local branch tracks a remote that doesn't exist yet); the pin glyph marks pinned repos.

**Main-checkout indicator** in the repo-name label: each repo-name label in the feature-worktrees grid carries a status suffix sourced from the project's main checkout under `projects/<repo>` — dirty-file count (red), `+N`/`-N` commits ahead/behind `origin/<main>` (green/yellow). A clean, up-to-date checkout shows nothing.

**Multi-remote indicator** in the env header: the branch shown for an env is its primary — the first *connected* non-pinned repo's remote feature branch. When the env's worktrees point at more than one distinct remote branch — e.g. after a per-worktree `winter ws connect <env>/<repo>` — the header gains a `+N` suffix (`feature-x+N`), where `N` is the number of *additional* distinct remotes. So `feature-auth+2` means three distinct remote branches across the env. No suffix means every connected worktree shares one branch.

## Keybindings

Every built-in action listed below has a stable **action id**. A `[keybindings]` table in `.winter/config.toml` (with the `.winter/config.local.toml` overlay applying per-machine) maps action ids to key specs; an id with no entry keeps its default. Invalid specs and unknown ids are reported as a dashboard toast and otherwise ignored — the rest of the bindings still load. See [setup.md](../setup.md#keybindings) for the config schema. (The Log tab is a separate screen whose `q`/`r`/`c` keys are fixed and not part of this table.)

| Action id | Default | Action |
|-----------|---------|--------|
| `app.quit` | `q` | Quit the dashboard (offered on the workspace screen) |
| `workspace.refresh` | `r` | Re-read all git status |
| `workspace.open_log` | `L` | Open the Log tab |
| `worktree.open_detail` | `<enter>` | Drill into the focused worktree / standalone row |
| `workspace.jump_prev` | `<C-k>` | Jump focus to the first table |
| `workspace.jump_next` | `<C-j>` | Jump focus to the last table |
| `worktree.refresh` | `r` | Re-read the env's git status |
| `worktree.open_log` | `L` | Open the Log tab |
| `worktree.back` | `q` | Back to the workspace screen |
| `worktree.cursor_left` / `worktree.cursor_down` / `worktree.cursor_up` / `worktree.cursor_right` | `h` / `j` / `k` / `l` | Move the repo cursor |
| `standalone.refresh` | `r` | Re-read the standalone repo's status |
| `standalone.open_log` | `L` | Open the Log tab |
| `standalone.back` | `q` | Back to the workspace screen |
| `plugin.<name>` | the plugin's `TuiAction.key` | Run a plugin-contributed action (see `winter-harness:/architecture/plugin-author.md`) |

**Key-spec grammar** (Neovim-inspired):

- **Single printable keys** are written bare: `s`, `D`, `,`. Uppercase means the shifted key.
- **Special keys** use angle brackets: `<enter>` (`<CR>`), `<tab>`, `<space>`, `<escape>`, `<backspace>`, `<up>`/`<down>`/`<left>`/`<right>`, `<f1>`…`<f12>`.
- **Modifier chords**: `<C-s>` (ctrl), `<A-s>` / `<M-s>` (alt / meta), `<S-s>` (shift), composed as `<C-A-s>`. These normalize to Textual tokens (`ctrl+s`, `alt+s`, `ctrl+alt+s`); `<S-` on a letter is just the uppercase letter.
- **`<leader>` prefix** expands to the configured `leader` key (default `\`), e.g. `<leader>S`.
- **Multi-key sequences** are an ordered run of the above: `<leader>S`, `gd`, `<C-x><C-s>`. The next key must arrive within `timeoutlen` milliseconds; if it doesn't and the keys so far are themselves a complete binding, that binding fires (Neovim's resolution). Avoid sequence keys the focused table already consumes (arrows, `enter`, `pageup`/`pagedown`) — the table intercepts them before the chord engine sees them. (This is an authoring caveat, not validated by the parser.)
