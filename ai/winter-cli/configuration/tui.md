# Dashboard & keybindings

The `winter dashboard` TUI reads its default layout and key bindings from `.winter/config.toml`; the `config.local.toml` overlay applies per-machine, merging key-by-key. For what the dashboard does at runtime, see [../usage/dashboard.md](../usage/dashboard.md).

## Dashboard layout

The `winter dashboard` TUI can render the feature-worktrees grid in four orientations. Set the default in a `[tui.dashboard]` table; the `config.local.toml` overlay applies per-machine, merging key-by-key.

```toml
[tui.dashboard]
layout = "auto"   # auto | repos-as-columns | repos-as-rows | list
```

Accepted values: `auto` (default), `repos-as-columns`, `repos-as-rows`, `list`. See [the dashboard Layouts reference](../usage/dashboard.md#layouts) for what each layout does and how `auto` resolves.

An unknown `layout` value is a config error at startup. The `t` key cycles layouts live for the current session (overriding the configured default); see [usage/dashboard.md#layouts](../usage/dashboard.md#layouts).

## Keybindings

The `winter dashboard` TUI binds each action to a configurable key. Override the defaults in a `[keybindings]` table; the `config.local.toml` overlay applies per-machine, merging key-by-key.

```toml
[keybindings]
leader = "\\"          # what <leader> expands to (default backslash); single key spec
timeoutlen = 1000      # ms to wait for the next key of a chord sequence (Neovim's timeoutlen)

# Action id -> key spec. Quoted ids keep the dotted name flat (not nested tables).
# Absent ids keep their built-in default. Full id list + grammar: usage/dashboard.md#keybindings.
[keybindings.bindings]
"workspace.refresh" = "<C-r>"     # modifier chord
"worktree.open_detail" = "o"      # rebind Enter for opening a row's detail
"workspace.open_log" = "<leader>l" # leader chord sequence
"plugin.codediff" = "<leader>d"   # remap a plugin action by its plugin.<name> id
```

The `[keybindings.bindings]` keys are *quoted* action ids — the quotes keep a dotted id (`workspace.refresh`) a flat key instead of a nested TOML table. For the action-id reference, the full key-spec grammar, and the invalid-spec / unknown-id behavior, see [usage/dashboard.md#keybindings](../usage/dashboard.md#keybindings).
