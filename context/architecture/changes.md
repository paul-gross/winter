# Where a change goes

Most changes add a file that something discovers, and edit no file another change also edits. This document lists what
is discovered, what is a shared edit, and the files each kind of change touches. The packages and their boundaries are
in [structure.md](./structure.md).

## What is added without editing a tracked file

| Adds                    | Where                                                       | Discovered by                        |
| ----------------------- | ----------------------------------------------------------- | ------------------------------------ |
| A command               | `crates/core/src/commands/`, as [below](#a-command)         | The library's build script           |
| A component test file   | `crates/core/tests/<subject>.rs`                            | Cargo's test-target discovery        |
| An executable test file | `crates/cli/tests/<subject>.rs`                             | Cargo's test-target discovery        |
| A context document      | `context/<area>/<subject>.md`, linked from the area's index | Nothing: the index is edited by hand |

## What stays a shared edit

| Change                                    | File                                                                       |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| A domain module                           | One `mod` line in `crates/core/src/lib.rs`, or in the parent module's file |
| A `Termination` variant                   | `crates/core/src/termination.rs`                                           |
| A `CommandError` or `ConfigError` variant | `crates/core/src/termination.rs`                                           |
| A port, or a field of `Context`           | `crates/core/src/context.rs` and `crates/core/src/process.rs`              |
| A third-party crate                       | The package's `Cargo.toml`, and `Cargo.lock`                               |
| A module only a domain entry may call     | The list of names in `crates/dev/src/architecture.rs`                      |

Work that is built as parallel changes lands these edits first, in one change, with the module files it declares present
and empty. The parallel changes then fill files that each of them alone owns.

## A command

A command is one file, and its place in the command tree follows from where the file is:

| File                                         | Command          |
| -------------------------------------------- | ---------------- |
| `crates/core/src/commands/<name>.rs`         | `<name>`         |
| `crates/core/src/commands/<group>/<name>.rs` | `<group> <name>` |

The build script `crates/core/build.rs` walks the directory on every build and generates the module declarations and the
registry that `crates/core/src/parse.rs` builds the parser from. Nothing else lists commands. The directory exists only
while it holds a command file, since git tracks no empty directory, and without it the registry is empty and the build
passes.

The build fails when an entry under `commands/` breaks one of these rules, and the first line the build script writes to
its error output starts with the offending path:

- The file stem matches `[a-z][a-z0-9_]*`. The command's name is the stem with each `_` replaced by `-`.
- A directory is one of the four groups: `ext`, `repo`, `service`, `ws`. A group that holds no command does not exist in
  the tree: help omits it, and its name is an unknown command.
- A file sits in `commands/` or in one group directory, and nothing sits deeper.
- Every file is a `.rs` file, and none is `mod.rs`.
- The file's text contains the identifier `REGISTRATION`.

The file defines `pub(crate) const REGISTRATION` of the type `crate::registry::Registration`, in one of two forms:

- `Registration::Command { args, run, hidden }`. `args` receives the node, already named from the file, and returns it
  with its description, arguments, and options. `run` is called once parsing has succeeded, with the parsed arguments
  and the `Context`; it writes the result through the context and returns a `Termination`. A hidden command is left out
  of help and still dispatched.
- `Registration::Alias { of }`, a second name for a sibling command in the same group, with the same arguments and the
  same `run`. `of` is the sibling's command name.

A unit test in `crates/core/src/parse.rs` checks the registry as a whole and fails naming the file that registers the
offending entry: an alias of something that is not a command beside it, an alias of an alias, or a top-level command
with the name of a group.

An option that takes a value is declared with `allow_hyphen_values(true)`, so that the token after it is its value
whatever it looks like: `--service-orchestrator -x`. A positional is never given that setting, and a positional that
starts with `-` needs `--` before it. The unit test
`every_value_taking_option_accepts_a_value_that_starts_with_a_hyphen` in `crates/core/src/parse.rs` checks every option
of the root and of every registered command, and fails naming the command and the option.

An option takes one value each time it is given: the parser's reading of the command path steps over one token after an
option that takes a value. The unit test `no_option_takes_more_than_one_value_at_one_giving` in the same file fails
naming the command and the option that takes more tokens than one, whether it is declared with a number of values or
with more than one value name; it reads the count from the built tree.

A command file holds the node's arguments and the adapter from the parsed arguments to one call into a domain module.
Logic that two commands share lives in a domain module, never under `commands/`. The file holds:

- the node's arguments, and the typed parameters read from the parsed arguments. Its `params` returns `Result<Params,
  Termination>` and reads a value the node's parser requires or defaults through `arguments::required`. When the parsed
  arguments do not hold that value the fault is the node's own parser, so `required` answers an internal failure, which
  `run` returns; a command file builds no usage error for it;
- one call into a domain module, which is handed `cx.files`, `cx.file_writer`, and `cx.cwd` as they are;
- the mapping of that call's typed outcome to a `Termination`;
- the text of the human line and the fields of the `--json` document, written as [below](#writing-the-result).

A command file does not spell its own command path as a string literal. The path follows from where the file is, and
reaches `run` as `cx.invoked`, with the usage of the node.

A command file names none of these:

| Area                                 | Names a command file may not hold                                                  |
| ------------------------------------ | ---------------------------------------------------------------------------------- |
| The file port                        | `ReadFiles`, `WriteFiles`, `OsFiles`                                               |
| Modules a domain entry stands on     | `workspace_root`, `workspace_files`, `config_raw`, `config_edit`, `state_registry` |
| File formats                         | `toml::`, `toml_edit`                                                              |
| Writing, and how a failed write ends | `writeln!`, `write!(`, `to_writer`, `InternalError`, `Backtrace`                   |

The architecture test checks the names by scanning each file under `crates/core/src/commands/` as text. A test module
and a comment are checked like the rest, and a failure names the file, the line, and the name.

The list is a deny-list, and the scan sees nothing but these names. It cannot tell one call from logic of its own that
is written without them, and it does not see a module that is not on the list. A file that holds none of the names can
still reach what they stand for:

- an item of a listed module through a re-export, by name or through a glob import of the crate root: the test module of
  `commands/repo/remove.rs` names `crate::RepositoryKind`, which `config_edit.rs` defines;
- a crate under another name: after `use toml as t;` no line holds `toml::`;
- the port through the handles of the context: `cx.files.read(..)` and `cx.file_writer.update(..)` name no type of the
  port, and `cx.out.write_all(..)` writes with none of the listed names.

Those remain a matter of review. A change that adds a module only a domain entry may call adds its name to the list in
`crates/dev/src/architecture.rs`.

Its tests are unit tests of the argument mapping in the file itself, and a component test at
`crates/core/tests/<group>_<name>.rs` for its behaviour in process, run through the scaffold
`crates/core/tests/support/in_process.rs`. [testing.md](../standards/testing.md) describes the tiers.

Command files are declared only in generated code, which `cargo fmt` does not follow. The driver's format step names
each of them explicitly, so they are checked and rewritten like every other source;
[development.md](../tooling/development.md#formatting) describes the step.

The verifiability matrix has a manual method for a change to discovery itself:
[`workspace:manual-registration`](../verification/matrix.md#workspacemanual-registration).

### Writing the result

`run` ends by writing its result through `crates/core/src/output.rs`, and returns what that call returns:
`output::document` with the document under `--json`, and `output::line` with the human line otherwise. The command file
builds the value and the text, and never writes to a handle itself or inspects a write error. The parser writes nothing:
`crates/core/src/process.rs` writes the help and the version line the parser answers through the same module, with
`output::text` and `output::line`.

Each of the three functions hands its result to the handle whole, in one write. A line-buffered handle then keeps
nothing of a write that fails, so the diagnostic of that failure is still written after it.

`output.rs` alone decides how a failed write ends:

| The write                                        | Ending                                                                              |
| ------------------------------------------------ | ----------------------------------------------------------------------------------- |
| Succeeds                                         | `Termination::Success`                                                              |
| Fails with `std::io::ErrorKind::BrokenPipe`      | `Termination::BrokenPipe`: exit 1 and no diagnostic, since the reader closed stdout |
| Fails with any other error, or cannot be encoded | `Termination::Internal(InternalError::Unexpected { .. })`, with the error as source |

## A domain module

A domain module is a file `crates/core/src/<domain>.rs`, with its children in `crates/core/src/<domain>/`, declared by
one `mod` line in `lib.rs` or in its parent. Modules stay private; what the binary or a test needs is re-exported by
name from `lib.rs`.

The function a command file calls, the module's entry, returns a typed value or the module's failure, never a
`Termination`. The command file maps the failure: `repository_catalog::remove` fails with `RemoveError`, a sum of the
module's own that the command file takes apart into `Termination::Command` and `Termination::Config`, and
`index_lookup::answer` fails with `ConfigError` alone, which becomes `Termination::Config`.

## An error

`Termination` has one variant per way an invocation ends, and each error enum it carries has one variant per failure. A
variant is added by the behaviour that first returns it, in `crates/core/src/termination.rs`. A new variant that carries
an error type holds it in a `Box`, under the rule on the width of `Termination` in
[rust.md](../standards/rust.md#errors). A domain module's own error enum lives in the module; `CommandError` gains a
variant that wraps it, and `lib.rs` re-exports the enum with the types its variants carry.
[rust.md](../standards/rust.md#errors) has the rules an error type follows.

## A dependency

A third-party crate goes into the manifest of the package whose source uses it, and into `Cargo.lock`. The gates resolve
nothing over the network, so a changed `Cargo.lock` needs the bootstrap of
[development.md](../tooling/development.md#bootstrap) again.

A dependency between two workspace packages has to be an edge [structure.md](./structure.md#allowed-edges) allows, and a
fifth package fails the architecture test. Either one is a change to the rules in `crates/dev/src/architecture.rs` and
to that document, not only to a manifest.
