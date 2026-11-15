# Testing

Which tier a behaviour is tested at, what `winter-workspace-test-support` gives a test, and what a test never does. The
command that runs each tier is in [the verifiability matrix](../verification/matrix.md).

## Tiers

| Tier       | Lives in                         | Starts a process | Proves                                                             |
| ---------- | -------------------------------- | ---------------- | ------------------------------------------------------------------ |
| Unit       | `#[cfg(test)]` beside the code   | No               | A pure rule: parsing, derivation, ordering, classification.        |
| Component  | `crates/core/tests/<subject>.rs` | Git only         | A behaviour of the library, run in process with in-memory handles. |
| Executable | `crates/cli/tests/<subject>.rs`  | The built binary | The process-level contract: streams, exit code, environment.       |

Choose the lowest tier that can make the claim:

- A unit test takes no file, git, or process input and uses nothing from the support package. The one exception is the
  file port, `crates/core/src/files.rs`: its unit tests touch the file system, and only inside a `TempTree` of their
  own.
- A component test runs the library in process and asserts on the returned `Termination` and on what was written. The
  run is built through the scaffold `crates/core/tests/support/in_process.rs`, a module the test file declares by
  `#[path]`. `InProcess` takes the arguments, the working directory, the environment, and stdin, calls the library's
  `run` over in-memory handles, and returns an `Ended` with the `Termination` and both streams as text. A test file
  never builds an `Invocation` itself. A test that needs a stdout of its own, such as one that fails, keeps that writer
  in its file and hands it to `run_with_stdout`. When the behaviour reads or writes files or git state, the test builds
  a fixture workspace with `TempTree`, `Git`, and `WorkspaceBuilder`; the library's manifest declares the support
  package as a dev-dependency once a component test uses it.
- An executable test starts the binary Cargo built, at `env!("CARGO_BIN_EXE_winter-workspace")`, through `invoke`. It is
  for a claim about the process and not about the library.

Each test file is named for its subject and is its own test target, so one file runs alone.

`crates/cli/tests/scenarios.rs` is not a tier to add tests to. It is the target the full gate's last step runs, and
[development.md](../tooling/development.md#the-gates) describes it.

## The isolated tree

`TempTree::new()` creates a directory of its own, the root, with the layout a test's workspace, remotes, and home live
in. Its path is canonical and made only of the characters `A-Z a-z 0-9 / . _ -`.

| Entry                    | Use                                                                 |
| ------------------------ | ------------------------------------------------------------------- |
| `ws/`                    | The workspace directory, and the working directory of an invocation |
| `remotes/`               | Bare repositories, one `<name>.git` each                            |
| `home/`, `home/.config/` | `HOME` and `XDG_CONFIG_HOME`                                        |
| `cache/`                 | `XDG_CACHE_HOME`                                                    |
| `tmp/`                   | `TMPDIR`                                                            |
| `bin/`                   | First on `PATH`, for stub programs                                  |
| `calls/`                 | What stub programs record                                           |
| `git-template/`          | Empty; `GIT_TEMPLATE_DIR`                                           |
| `gitconfig`              | `GIT_CONFIG_GLOBAL`                                                 |

The tree is created in the system temporary directory, or in the directory `WINTER_TEST_TMPDIR` names. Creation fails,
creating nothing, when that directory or one above it holds `.winter/config.toml`, holds `.git`, or is a git directory;
the error names the entry found. So no walk upward from inside a tree reaches a real workspace or a real repository. On
a host whose temporary directory lies inside either, set `WINTER_TEST_TMPDIR` to a directory that does not.

The tree is removed when the value is dropped. With `WINTER_TEST_KEEP=1` it is kept and its path is printed to stderr.

## The pinned environment

Every child the harness starts, git and the binary alike, gets exactly the tree's pinned environment and inherits
nothing. `crates/test-support/src/env.rs` holds the whole table. What it pins:

- `PATH` is the tree's `bin/`, the directory of the host's git, `/usr/bin`, and `/bin`.
- Home, configuration, cache, and temporary directories are the tree's own.
- Git reads the tree's `gitconfig` and no system configuration, never prompts, and stops its search for a repository at
  the tree.
- Author, committer, and their dates are fixed, the time zone is UTC, and the locale is `C.UTF-8`.
- Colour is off and the terminal is `dumb`, 80 columns by 24 lines.

A test that needs another variable adds it with `PinnedEnv::set`. Nothing else of the developer's environment reaches a
child, because the child's environment is cleared first.

## The process runner

`invoke` runs one `Invocation` to its end or to its deadline and returns an `Observation`: both streams and the exit. A
non-zero exit is an observation, not a failure.

- The executable and the working directory are absolute paths. Nothing is looked up on `PATH`, and no shell is involved.
- The child leads its own process group. On the deadline, and on any way out of `invoke`, the group is killed and the
  child reaped. `TempTree::invocation` sets a deadline of 30 seconds.
- Each stream is captured up to 8 MiB; a child that writes more is killed and the invocation fails.
- An invocation that gives no observation is an `InvocationFailure`, which carries its kind and what the child had
  written until then.

## Git fixtures and the workspace builder

`Git` runs the host's git inside one tree, under the pinned environment and a 30-second deadline, and builds
repositories whose object ids are the same on every host: each repository is created with SHA-1 objects and refs stored
as files, and each commit carries a date that follows from how many commits the tree already holds. A clone's remote is
`https://fixture.invalid/<name>.git`, which the tree's `gitconfig` maps to `remotes/<name>.git`, so no host name is
resolved.

`WorkspaceBuilder` builds a fixture workspace under `ws/` from literal inputs: configuration files, remotes, project
clones, environment worktrees, and optionally a root repository.

### What the helpers guarantee

Every path a helper takes is checked before the helper has any effect, with symbolic links resolved. Until every path of
a call has passed, no file is written or removed, no directory is made, and git is not started. A path that fails is
`GitError::Unconfined`, naming it. The workspace builder's writes are held to the same check.

The helpers guarantee that a fixture built from honest inputs has no effect outside its tree, on the top-level entries
of its tree, or on the network, and they refuse the escapes their documentation lists: the type `Git` in
`crates/test-support/src/git.rs` lists them, and the check itself is in `crates/test-support/src/tree.rs`.

They are not a sandbox. A test that deliberately builds a way out by means that list does not name is outside the
guarantee: the helpers owe no refusal for it, and the rule that a test never touches anything outside its tree binds the
test's author. A defect of the helpers is an honest input that has such an effect, or a listed escape that is not
refused.

## What a test never does

The standing rule in [AGENTS.md](../../AGENTS.md) holds for every test of the library, the binary, and the support
package. In practice:

- It creates files only under its own tree, and reaches no workspace or repository that is not a fixture in that tree.
  The one exception is the repository's committed `scenarios/`, which a test may read and never writes: it copies what
  it needs into its tree.
- It starts a child only through `invoke` or `Git`, by an absolute path and under the pinned environment. No shell
  stands between a test and its child, and no name is looked up on `PATH`. A test of the support package that needs a
  child with a behaviour of its own runs `/bin/sh` as that child, with a script the test spells out.
- It resolves no host name. Fixture remotes are the tree's own bare repositories.
- It lets nothing of the developer's environment or home directory reach a child or decide a result. The test process
  reads its own environment only for `WINTER_TEST_` variables and for the directory of the host's git on `PATH`.

The driver's own tests, in `crates/dev/`, test the tooling and not the product. Those of the gates and of the
architecture rules do not use the isolated tree: they start Cargo and `/bin/sh` directly, read the checkout, and write
only under a scratch directory of their own in the system temporary directory.

Those of `cargo dev record` do use it. Each makes a stand-in checkout, a stand-in executable, and a scenarios root
inside a tree of its own. Most run the command over them in the test process, with the stand-in and a `PATH` value as
arguments. Those that assert which trees a run made or left run it in a child test process instead, started through
`invoke` with `WINTER_TEST_TMPDIR` naming a directory of the tree, and hand it the stand-in through a variable: the one
of a run naming no reference, those of a scenario that is not confined, the one that asserts `--keep` leaves both trees,
the one whose tree for the version call cannot be made, and the two whose record step panics over a tree it cannot
remove, which the test then reaches in that directory. Two call `record::refusals` directly and never run the command.
The command starts git from that `PATH` value under a cleared environment of its own, and the stand-in through `invoke`.
Beside its trees it makes one directory in the system temporary directory, the `HOME` of its git calls, and removes it.

## Tests that must fail

A test of the support package that must fail, or that leaves a tree its own drop cannot remove, is a fixture: a test
marked `#[ignore]` that runs only as a child test process of the test that asserts on it. The parent starts it through
`invoke` under its own tree's pinned environment, with `WINTER_TEST_TMPDIR` naming a directory inside that tree and
`WINTER_TEST_FIXTURE` set to the fixture's name. A fixture that does not find its own name in that variable returns at
once, having created nothing.
