# Structure

The repository is one Cargo workspace of four packages. This document says what each package is for, which may depend on
which, and where the line runs between the library and the binary. Where a particular change goes is in
[changes.md](./changes.md).

## Packages

| Package                         | Directory              | Role                                                                                       |
| ------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------ |
| `winter-workspace`              | `crates/cli/`          | The binary. Binds streams, environment, and working directory; maps the ending to an exit. |
| `winter-workspace-core`         | `crates/core/`         | The library: parsing, commands, domain, adapters. Returns values.                          |
| `winter-workspace-test-support` | `crates/test-support/` | Isolated trees, the process runner, and git and workspace fixtures for tests.              |
| `winter-workspace-dev`          | `crates/dev/`          | The development driver: the gates, the formatter call, and the architecture rules.         |

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

`default-members` is the binary and the library, so a bare `cargo build` or `cargo run` produces the product alone.
Every check names the whole workspace or one package explicitly, and none depends on that default.

A third-party dependency is declared in the manifest of the package that uses it, only while a source file of that
package uses it, and is resolved in the committed `Cargo.lock`. The toolchain is pinned in `rust-toolchain.toml`.

## The library and the binary

The library returns values. Its one entry is `run`, which takes an `Invocation` and returns a `Termination`:

- The `Invocation` carries everything the process would otherwise supply: the arguments, the working directory, a
  snapshot of the environment, the program's name and version, the three stream handles, and whether each output is a
  terminal.
- `run` parses, dispatches, and writes results, and the help or version a flag asks for, to those handles. It never
  writes the final diagnostic and never exits.
- The `Termination` says how the invocation ended. It answers its exit code and the diagnostic the process boundary
  writes for it, if any.

`crates/cli/src/main.rs` is the process boundary and the whole binary. It snapshots the arguments, the working
directory, and the environment, locks the three streams, calls `run`, flushes stdout, writes the diagnostic to stderr,
and exits with the termination's code. It is the only file that names the process streams, the process environment, or
the process exit, which is why a test can call `run` with in-memory handles and observe everything a command does.

[rust.md](../standards/rust.md) lists what this boundary forbids a library source to name.

## Modules

`crates/core/src/lib.rs` is a facade: its modules are private, and the public surface is the named re-exports it lists.
A command receives a `Context`, built by `run` from the invocation, and reaches the outside only through it. Its `files`
field is the read side of the file port: a domain module that reads a file takes `&dyn ReadFiles` and never names the
file system itself. Its `file_writer` field is the write side, `&dyn WriteFiles`: one read-modify-write of one file
under a lock, which replaces the file through a temporary file beside it.

| Module                          | Holds                                                                                                             |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `process.rs`                    | `run`, `Invocation`, `Identity`, `ProcessEnv`                                                                     |
| `termination.rs`                | `Termination` and the error enums it carries                                                                      |
| `context.rs`                    | `Context`, what a command receives                                                                                |
| `registry.rs`                   | `Registration` and `Entry`, and the registry the build script generates                                           |
| `groups.rs`                     | The groups of the command tree and their descriptions                                                             |
| `parse.rs`                      | The argument parser, built from the registry, and the dispatch to a command                                       |
| `output.rs`                     | The writing of a result and the ending of a failed write; see [changes.md](./changes.md#writing-the-result)       |
| `commands/`                     | One file per command; see [changes.md](./changes.md#a-command)                                                    |
| `files.rs`                      | The file port `ReadFiles` and `WriteFiles`, and its adapter `OsFiles`; the only source that names the file system |
| `config_edit.rs`                | The removal of one repository block from the text of a configuration file, and `CatalogError`                     |
| `workspace_files.rs`            | The names of the configuration files and the state file, each relative to a workspace root                        |
| `workspace_root.rs`             | The workspace root of a directory                                                                                 |
| `config_raw.rs`                 | `env_aliases` and `envs_per_workspace`, layered from the two configuration files                                  |
| `state_registry.rs`             | The env index registry of the state file, read only                                                               |
| `env_index.rs`                  | The index layout, the hash slot of a name, and `lookup`                                                           |
| `repository_catalog.rs`         | The removal of one repository from a configuration file: the order of its checks and the replacement of the file  |
| `index_lookup.rs`               | The lookup of an env name in a workspace: its root, its configuration, its registry, and `lookup`                 |
| `build.rs`, `build/discover.rs` | Command discovery at build time; see [changes.md](./changes.md#a-command)                                         |

A module `x` is the file `x.rs`, with its children in `x/`. No module is spelled `mod.rs`.

The other two libraries follow the same facade shape. `crates/test-support/src/lib.rs` re-exports by name what tests
use; [testing.md](../standards/testing.md) describes it. `crates/dev/src/lib.rs` holds every module of the driver, and
its one entry is `main_with`; `crates/dev/src/main.rs` calls it with the process arguments, an environment lookup, and
the repository root, and does nothing else, so the driver's behaviour is tested through its library.
