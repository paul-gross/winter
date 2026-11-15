# Structure

The repository is one Cargo workspace of four packages. This document says what each package is for, which may depend on
which, and where the line runs between the library and the binary. Where a particular change goes is in
[changes.md](./changes.md).

## Packages

| Package                         | Directory              | Role                                                                                                                                               |
| ------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `winter-workspace`              | `crates/cli/`          | The binary, the composition root. Binds streams, environment, and working directory; maps the ending to an exit.                                   |
| `winter-workspace-core`         | `crates/core/`         | The library: parsing, commands, domain, adapters. Returns values.                                                                                  |
| `winter-workspace-test-support` | `crates/test-support/` | Isolated trees, the process runner, git and workspace fixtures, and the scenario machinery, for tests and for the scenario recorder of the driver. |
| `winter-workspace-dev`          | `crates/dev/`          | The development driver: the gates, the formatter call, the architecture rules, and the scenario recorder.                                          |

Every command and every domain module lives in the one library. Module boundaries inside it are the unit of structure; a
package per domain would make every new one edit the workspace and binary manifests.

## Allowed edges

A workspace package may depend on another only as this table allows. An edge the table does not list fails the
architecture test, whatever its kind.

| Package                         | Normal dependency               | Dev-dependency                  |
| ------------------------------- | ------------------------------- | ------------------------------- |
| `winter-workspace`              | `winter-workspace-core`         | `winter-workspace-test-support` |
| `winter-workspace-core`         | none                            | `winter-workspace-test-support` |
| `winter-workspace-test-support` | none                            | none                            |
| `winter-workspace-dev`          | `winter-workspace-test-support` | none                            |

The same test holds three more rules of the package graph:

- The workspace has exactly these four members. A fifth fails it, and so does a missing one.
- No workspace package build-depends on another.
- `winter-workspace-test-support` and `winter-workspace-dev` declare `publish = false`.

The table is the limit, and a manifest declares an edge only once a source of its package uses it.

The rules are functions of `crates/dev/src/architecture.rs`, and `crates/dev/tests/architecture.rs` applies them to the
checkout. [The verifiability matrix](../verification/matrix.md) names the command that runs them.

## The Cargo workspace

`default-members` is the binary and the library, so a bare `cargo build` builds the product alone. Every check names the
whole workspace or one package explicitly, and none depends on that default. Where the binary a build produces may be
started is the built-binary rule of [AGENTS.md](../../AGENTS.md#standing-rules).

A third-party dependency is declared in the manifest of the package that uses it, only while a source file of that
package uses it, and is resolved in the committed `Cargo.lock`. The toolchain is pinned in `rust-toolchain.toml`.

The table gives every dependency the four manifests declare, with what uses it. A path in it that begins with `src/` or
`tests/` is relative to the directory of its package. Each is a normal dependency unless its row says otherwise, and a
dependency on a workspace package is an edge of [Allowed edges](#allowed-edges).

| Package                         | Dependency                                                                                            | Used by                                                                                                                                                                                            |
| ------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `winter-workspace`              | `libc` 0.2                                                                                            | The SIGINT handler and its installer in `src/interrupt.rs`                                                                                                                                         |
| `winter-workspace`              | `winter-workspace-core`                                                                               | `src/main.rs`, which calls `run`, and `src/interrupt.rs`, which holds the `Interrupts` value                                                                                                       |
| `winter-workspace`              | `winter-workspace-test-support`, a dev-dependency                                                     | The tests under `tests/`, and the test module of `src/interrupt.rs`                                                                                                                                |
| `winter-workspace-core`         | `clap` 4.6, without its default features, with `std`, `help`, `usage`, `error-context`, `suggestions` | The argument parser of `src/parse.rs`, the registration contract of `src/registry.rs`, the reading of a parsed argument in `src/arguments.rs`, and the arguments a command file declares and reads |
| `winter-workspace-core`         | `thiserror` 2                                                                                         | The error types                                                                                                                                                                                    |
| `winter-workspace-core`         | `toml` 1.2                                                                                            | The parsing of the configuration files and the state file, and `TomlParseError`, which wraps its error                                                                                             |
| `winter-workspace-core`         | `toml_edit` 0.25, without its default features, with `parse`                                          | The edit of a configuration file's text in `src/config_edit.rs`                                                                                                                                    |
| `winter-workspace-core`         | `serde_json` 1                                                                                        | The `--json` results: `src/output.rs` and the command files                                                                                                                                        |
| `winter-workspace-core`         | `sha1` 0.10                                                                                           | The hash slot of a name in `src/env_index.rs`                                                                                                                                                      |
| `winter-workspace-core`         | `winter-workspace-test-support`, a dev-dependency                                                     | The tests under `tests/`, and the test module of `src/files.rs`                                                                                                                                    |
| `winter-workspace-test-support` | `libc` 0.2                                                                                            | The process runner of `src/process.rs`, which prepares a child before it starts, kills its process group, and waits for it                                                                         |
| `winter-workspace-test-support` | `serde_json` 1                                                                                        | The normalisation and the comparison of scenario JSON, under `src/scenario/`                                                                                                                       |
| `winter-workspace-test-support` | `sha1` 0.10                                                                                           | The digest of the pinned environment in `src/env.rs`, and the description digest and state snapshots of a scenario                                                                                 |
| `winter-workspace-test-support` | `tempfile` 3                                                                                          | The isolated tree, the scratch directories of the git fixtures, and the staging directories of a scenario record                                                                                   |
| `winter-workspace-test-support` | `toml` 1.2                                                                                            | The scenario model, and the comparison of two TOML files in `src/scenario/compare.rs`                                                                                                              |
| `winter-workspace-dev`          | `cargo_metadata` 0.23                                                                                 | The package graph the architecture rules read, in `src/architecture.rs`                                                                                                                            |
| `winter-workspace-dev`          | `libc` 0.2                                                                                            | The signal handling of the driver and the kill of a step's process group, in `src/step.rs`                                                                                                         |
| `winter-workspace-dev`          | `tempfile` 3                                                                                          | The temporary home of the record command, and the directory a record is set aside in while its successor is made, in `src/record.rs`                                                               |
| `winter-workspace-dev`          | `toml` 1.2                                                                                            | The alias table of `.cargo/config.toml` at the repository root, read by the architecture rules of `src/architecture.rs`                                                                            |
| `winter-workspace-dev`          | `winter-workspace-test-support`                                                                       | The record command of `src/record.rs`: the scenario machinery and the isolated tree                                                                                                                |

## The library and the binary

The library returns values. Its one entry is `run`, which takes an `Invocation` and returns a `Termination`:

- The `Invocation` carries everything the process would otherwise supply: the arguments, the working directory or the
  error of asking for it, a snapshot of the environment, the program's name and version, the three stream handles,
  whether each output is a terminal, and the `Interrupts` value the handler of the binary shares.
- `run` parses, dispatches, and writes to those handles: results, and the help or version a flag asks for, to the stdout
  handle, and log records to the stderr handle. It never writes the final diagnostic and never exits.
- The `Termination` says how the invocation ended. It answers its exit code and the diagnostic the process boundary
  writes for it, if any.

The binary is two files. `crates/cli/src/main.rs` is the process boundary. Its `main` calls the interrupt installer,
installs the panic hook, snapshots the arguments, the working directory, and the environment, and hands `conclude` three
things: a closure that locks the three streams and calls `run`, a closure that flushes stdout, and stderr. `conclude`
calls `run` inside an unwind catch, flushes stdout, writes the diagnostic of the ending to stderr, with the backtrace of
an internal failure when one was captured, and answers the exit code, which `main` exits with. The hook writes nothing
for a panic of the thread `conclude` runs on: it stores the message and a backtrace for `conclude`, which ends the
invocation as an internal failure. A panic of any other thread goes to the hook that was installed before.

`crates/cli/src/interrupt.rs` holds the SIGINT handler and the one `Interrupts` value of the process, which every
invocation is handed. `main` calls its installer first. A process that starts with SIGINT ignored keeps it ignored: the
installer reads the disposition and installs nothing, and an interrupt does nothing for the whole run. Otherwise the
handler is installed. It notes every interrupt in that value, first. When a command defers the interrupt, the handler
then returns. Any other interrupt ends the process in the handler: it writes the aborted notice to file descriptor 2 and
exits with code 1, flushing nothing and returning no `Termination`. The binary installs no handler for any other signal.

Outside their test modules, no other file names what these two name. `main.rs` alone names the process streams and the
process environment, and answers the exit code of an ending. `interrupt.rs` alone reads or changes the disposition of a
signal, writes to a file descriptor by its number, and ends the process without returning. That is why a test can call
`run` with in-memory handles and observe everything a command does. The test module of `interrupt.rs` is the one
exception: a test of the handler reads the path of its own test executable to start its fixture, and the fixture reads
the variable that names it and writes its lines to the process stdout, because the test that started it reads them
there.

[rust.md](../standards/rust.md) lists what this boundary forbids a library source to name.

## Modules

`crates/core/src/lib.rs` is a facade: its modules are private, and the public surface is the named re-exports it lists.
A command receives a `Context`, built by `run` from the invocation, and reaches the outside only through it. Its `files`
field is the read side of the file port: a domain module that reads a file takes `&dyn ReadFiles` and never names the
file system itself. Its `file_writer` field is the write side, `&dyn WriteFiles`: one read-modify-write of one file
under a lock, which replaces the file through a temporary file beside it.

| Module                          | Holds                                                                                                                                                                |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `process.rs`                    | `run`, `Invocation`, `Identity`, `ProcessEnv`, `Interrupts`; after the parse: help or version written, the debug record, the one `Context` built, the command called |
| `termination.rs`                | `Termination` and the error enums it carries                                                                                                                         |
| `context.rs`                    | `Context`, what a command receives, with `Invoked`, the node being run, and `RootOptions`, the root option values a command can read                                 |
| `logging.rs`                    | `Level` and `Log`, and the resolution of `-v` and `WINTER_LOG_LEVEL` into what an invocation logs                                                                    |
| `registry.rs`                   | `Registration` and `Entry`, and the registry the build script generates                                                                                              |
| `groups.rs`                     | The groups of the command tree and their descriptions                                                                                                                |
| `parse.rs`                      | The argument parser, built from the registry, and what it answers: `Parsed`, with the usage of the node at fault. It writes nothing and calls no command             |
| `arguments.rs`                  | The reading of a parsed argument that the parser guarantees                                                                                                          |
| `output.rs`                     | The writing of a result and the ending of a failed write; see [changes.md](./changes.md#writing-the-result)                                                          |
| `diagnostics.rs`                | The writing of a log record to the stderr handle: the threshold, the two record formats, and a failed write ignored                                                  |
| `commands/`                     | One file per command; see [changes.md](./changes.md#a-command)                                                                                                       |
| `files.rs`                      | The file port `ReadFiles` and `WriteFiles`, and its adapter `OsFiles`; the only source that names the file system                                                    |
| `config_edit.rs`                | The removal of one repository block from the text of a configuration file, and `CatalogError`                                                                        |
| `workspace_files.rs`            | The names of the configuration files and the state file, each relative to a workspace root                                                                           |
| `workspace_root.rs`             | The workspace root of a directory                                                                                                                                    |
| `config_raw.rs`                 | `env_aliases` and `envs_per_workspace`, layered from the two configuration files                                                                                     |
| `state_registry.rs`             | The env index registry of the state file, read only                                                                                                                  |
| `env_index.rs`                  | The index layout, the hash slot of a name, and `lookup`                                                                                                              |
| `repository_catalog.rs`         | The removal of one repository from a configuration file: the order of its checks and the replacement of the file                                                     |
| `index_lookup.rs`               | The lookup of an env name in a workspace: its root, its configuration, its registry, and `lookup`                                                                    |
| `build.rs`, `build/discover.rs` | Command discovery at build time; see [changes.md](./changes.md#a-command)                                                                                            |
| `crates/cli/src/interrupt.rs`   | In the binary, not the library: the SIGINT handler and the one `Interrupts` value of the process                                                                     |

A module `x` is the file `x.rs`, with its children in `x/`. No module is spelled `mod.rs`.

The other two libraries follow the same facade shape. `crates/test-support/src/lib.rs` re-exports by name what tests
use; [testing.md](../standards/testing.md) describes it. `crates/dev/src/lib.rs` holds every module of the driver, and
its one entry is `main_with`; `crates/dev/src/main.rs` calls it with the process arguments, an environment lookup, and
the repository root, and does nothing else, so the driver's behaviour is tested through its library.
