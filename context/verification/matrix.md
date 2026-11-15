# Verifiability matrix

The verification methods available for this repository: the concrete ways a change to it is shown correct. Its shape is
owned by `canon:verifiability-matrix`, and its scope is `workspace`.

An id is stable. A row is added, never renamed or reused, and a row exists only when its command exists and exits 0 on a
clean checkout.

## Commands

Run from the repository root, after the bootstrap of [development.md](../tooling/development.md#bootstrap).

| Method                      | Command                                          | Proves                                                                          | Use when                                                   |
| --------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `workspace:unit-test`       | `cargo test -p winter-workspace-core --lib`      | A pure rule: parsing, derivation, ordering, classification.                     | The rule has no file, git, or process input.               |
| `workspace:component-test`  | `cargo test -p winter-workspace-core --test '*'` | A behaviour against a fixture workspace, run in process with in-memory handles. | The behaviour reads or writes files or git state.          |
| `workspace:executable-test` | `cargo test -p winter-workspace --tests`         | Process-level contract: streams, exit code, environment.                        | The claim is about the process, not the library.           |
| `workspace:architecture`    | `cargo test -p winter-workspace-dev`             | Package edges, library output rules, context-tree rules, workflow and aliases.  | The change adds a package, an edge, or a context document. |
| `workspace:slice-gate`      | `cargo dev gate slice`                           | The change is mergeable.                                                        | Always, before handing a change over.                      |
| `workspace:full-gate`       | `cargo dev gate full`                            | The branch agrees with every recorded scenario.                                 | Before a merge and before a release.                       |
| `workspace:scenario`        | `cargo scenarios`                                | Equality with the recorded reference in exit code, JSON, and state.             | The behaviour exists in the reference tool.                |

A test file narrower than a row is selected with `--test <subject>` in place of `--test '*'`, or of `--tests` in the
`workspace:executable-test` row. That row does not select the recorded-scenario target, which belongs to the full gate.

[testing.md](../standards/testing.md#tiers) describes the tiers behind the first three rows, and
[development.md](../tooling/development.md#the-gates) the steps of the two gates.

## Manual testing

Two behaviours cannot be tests of the tree they change, and are exercised by hand. Both run in a scratch copy of the
checkout under a temporary directory, never in the checkout itself.

The scratch copy needs a git directory of its own, since each method ends by reading its `git status`. A clone of the
checkout with the change under test applied has one. A plain copy does not when the checkout is a git worktree: its
`.git` is then a file that points back to the original repository.

### `workspace:manual-gates`

Exercises the gates' own failure behaviour, which cannot be a test of the suite the gates run. Use it when a change
touches the gate driver, a Cargo alias, or the manifest entry of the scenario target.

**Setup.** Make a scratch copy of the checkout under a temporary directory and work only there. Inject one fault, run
the command the table names, record the result, and restore the copy before the next fault. A library source is any file
under `crates/core/src/`.

| Fault                                                                                  | Command                | Expected                                                          |
| -------------------------------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------- |
| A mis-indented line in a library source                                                | `cargo dev gate slice` | Exit 1; the summary names `fmt`.                                  |
| An unused `let` binding in a library source                                            | `cargo dev gate slice` | Exit 1; the summary names `clippy`.                               |
| `let _x = 1u8.clone();` in a library source                                            | `cargo dev gate slice` | Exit 1; the summary names `clippy`.                               |
| A new unit test in a library source whose body is `assert_eq!(std::process::id(), 0);` | `cargo dev gate slice` | Exit 1; the summary names `test`, and neither `fmt` nor `clippy`. |
| An empty directory `scenarios/x-y/z/` at the repository root                           | `cargo scenarios`      | Exit 1; the output names the directory.                           |
| The same empty directory                                                               | `cargo dev gate full`  | Exit 1; the summary names `scenarios`.                            |
| The same empty directory                                                               | `cargo dev gate slice` | Exit 0.                                                           |

**Success.** Each run ends with the stated exit and summary, every later step still ran, and `git status` of the scratch
copy shows only the injected edit. The empty directory adds no tracked file, so there `git status` shows nothing.

The second and third faults differ in kind: the unused binding is a compiler warning and the needless `clone` a Clippy
warning, and the `clippy` step denies both.

The fourth fault fails when the test runs and at no other step. A process id is never 0, and the condition is not a
constant, so the `clippy` step, which lints test code as well, reports nothing about it. An assertion on a constant,
such as `assert!(false)`, would fail the `clippy` step too and is not used.

The last fault needs no edit to the scenario target, which fails on a scenario directory it cannot replay; an empty one
is such a directory.

### `workspace:manual-registration`

Exercises command discovery: that the build picks up a new command file, and that the build script itself stops the
build. Use it when a change touches `crates/core/build.rs`, `crates/core/build/discover.rs`,
`crates/core/src/registry.rs`, or `crates/core/src/parse.rs`.

**Setup.** Make a scratch copy of the checkout under a temporary directory and work only there. Run the two cases in
order.

1. Add `crates/core/src/commands/ws/probe.rs`. Its `REGISTRATION` is a `Registration::Command` whose `args` returns the
   node it is given and whose `run` returns `Termination::Success`. Then:
   - `cargo build --workspace --locked` exits 0.
   - `target/debug/winter-workspace ws probe` exits 0.
   - `target/debug/winter-workspace --help` lists `ws`.
   - `cargo clippy --locked --workspace --all-targets --all-features -- -D warnings` exits 0.
2. Rename that file to `crates/core/src/commands/ws/Bad.rs`. Then `cargo build --workspace --locked` exits non-zero, and
   the first line of the build script's error output starts with `crates/core/src/commands/ws/Bad.rs`.

**Success.** Each run ends as stated, and `git status` of the scratch copy shows only the one added file.

Everything else about discovery is automatic: the path rules, the message of each, and the generated registry are unit
tests of the library, under `workspace:unit-test`.

## Tools

| Tool             | Use                                                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------------------------ |
| `tool:format`    | `cargo dev fmt` — rewrites formatting; run before the slice gate when its `fmt` step fails.                        |
| `tool:keep-tree` | `WINTER_TEST_KEEP=1` on any test command — keeps the isolated tree and prints its path.                            |
| `tool:record`    | `cargo dev record` — records a scenario with the reference tool; [scenarios.md](./scenarios.md) says when and how. |
