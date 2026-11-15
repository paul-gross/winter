# winter-workspace

This repository is the Rust source of the `winter-workspace` binary, the command-line tool that manages a Winter
workspace.

## Standing rules

- Run `cargo dev gate slice` before handing a change over; run `cargo dev gate full` before a merge.
- A command is one file under `crates/core/src/commands/`; no registry is edited.
- The library returns values; only `crates/cli/src/main.rs` touches process streams, environment, and exit.
