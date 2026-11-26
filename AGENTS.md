# winter-workspace

This repository is the Rust source of the `winter-workspace` binary, the command-line tool that manages a Winter
workspace.

## Standing rules

- Run `cargo dev gate slice` before handing a change over; run `cargo dev gate full` before a merge.
- A command is one file under `crates/core/src/commands/`; no registry is edited.
- The library returns values; only the binary package touches process streams, environment, sockets, and exit.
- Tests of the library, the binary, and the support package run in an isolated tree from
  `winter-workspace-test-support`; such a test never touches a real workspace, a real repository, the network, or
  anything else outside its tree; the only host programs it runs are git and the base system utilities on the pinned
  `PATH`, and nothing of the developer's own environment reaches it. Two exceptions, and no other: a test may read, and
  never write, the committed `scenarios/` directory; and a test may open or cause the sockets
  [testing.md](./context/standards/testing.md#the-trace-collector) lists, each on the loopback address and each for the
  trace collector of the support package, and no other socket. No test resolves a name. The driver's own tests in
  `crates/dev/` test the tooling: those of the gates and of the architecture rules read the checkout and start Cargo,
  and those of the record command run in an isolated tree that holds a stand-in checkout, and start git and a stand-in
  executable there.
- Nothing in the repository starts the reference tool; `cargo dev record` alone is handed its path.
- The built binary, however it is started outside the tests and the gates, `cargo run` included, runs only with its
  working directory inside an isolated tree, a scratch workspace under a temporary directory, or a scratch copy of the
  checkout under a temporary directory, and only where no directory at or above the working directory holds a
  `.winter/config.toml` other than one made for the run; never in the checkout and never in a real workspace, because it
  resolves the workspace upward from its working directory, `repo add` and `repo remove` write to the configuration they
  find there, and `ws update` fetches, resets branches, stashes, and writes the lock, and, with `--freeze`, the
  configuration, of the workspace it finds there. It is never installed, and never put on `PATH` under the name of the installed `winter`. The isolated tree is the one
  [testing.md](./context/standards/testing.md#the-isolated-tree) describes; a scratch workspace is a directory made for
  the run that holds a `.winter/config.toml` of its own.

## Routing

[context/index.md](./context/index.md) is the root of the context tree. Follow the row that matches the task to its area
index, and from there to one document.

| Area index                                                       | Read when                                                                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| [context/architecture/index.md](./context/architecture/index.md) | You add a command, a module, an error kind, or a dependency, or ask what may depend on what.                 |
| [context/standards/index.md](./context/standards/index.md)       | You write or review Rust, or write a test.                                                                   |
| [context/tooling/index.md](./context/tooling/index.md)           | You set up a checkout, run a gate, or read a gate failure.                                                   |
| [context/index.md](./context/index.md) | You verify a feature or recovery behavior, state how a change is verified, or work on a recorded scenario; follow its verification route. |
