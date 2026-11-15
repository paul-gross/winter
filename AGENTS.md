# winter-workspace

This repository is the Rust source of the `winter-workspace` binary, the command-line tool that manages a Winter
workspace.

## Standing rules

- Run `cargo dev gate slice` before handing a change over; run `cargo dev gate full` before a merge.
- A command is one file under `crates/core/src/commands/`; no registry is edited.
- The library returns values; only `crates/cli/src/main.rs` touches process streams, environment, and exit.
- Tests of the library, the binary, and the support package run in an isolated tree from
  `winter-workspace-test-support`; such a test never touches a real workspace, a real repository, the network, or
  anything else outside its tree; the only host programs it runs are git and the base system utilities on the pinned
  `PATH`, and nothing of the developer's own environment reaches it. The one exception: a test may read, and never
  writes, the repository's committed `scenarios/`, copying what it needs into its tree. The driver's own tests in
  `crates/dev/` test the tooling: those of the gates and of the architecture rules read the checkout and start Cargo,
  and those of the record command run in an isolated tree that holds a stand-in checkout, and start git and a stand-in
  executable there.
- Nothing in the repository starts the reference tool; `cargo dev record` alone is handed its path.

## Routing

[context/index.md](./context/index.md) is the root of the context tree. Follow the row that matches the task to its area
index, and from there to one document.

| Area index                                                       | Read when                                                                                    |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| [context/architecture/index.md](./context/architecture/index.md) | You add a command, a module, an error kind, or a dependency, or ask what may depend on what. |
| [context/standards/index.md](./context/standards/index.md)       | You write or review Rust, or write a test.                                                   |
| [context/tooling/index.md](./context/tooling/index.md)           | You set up a checkout, run a gate, or read a gate failure.                                   |
| [context/verification/index.md](./context/verification/index.md) | You state how a change is verified, or check that its verification is complete.              |
