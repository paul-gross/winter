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
| `workspace:slice-gate`      | `cargo dev gate slice`                           | Formatting, lints, and every test but the recorded scenarios pass.              | Always, before handing a change over.                      |
| `workspace:full-gate`       | `cargo dev gate full`                            | The branch agrees with every recorded scenario.                                 | Before a merge and before a release.                       |
| `workspace:scenario`        | `cargo scenarios`                                | Equality with the recorded reference in exit code, JSON, and state.             | The behaviour exists in the reference tool.                |

A test file narrower than a row is selected with `--test <subject>` in place of `--test '*'`, or of `--tests` in the
`workspace:executable-test` row. That row does not select the recorded-scenario target, which belongs to the full gate.

[testing.md](../standards/testing.md#tiers) describes the tiers behind the first three rows, and
[development.md](../tooling/development.md#the-gates) the steps of the two gates.

## Manual testing

The gates and registration exercises cannot be tests of the tree they change; the interrupt exercise needs SIGINT sent
to the running binary, which [no test sends](../standards/testing.md#what-a-test-never-does). Repository removal also has
a manual exercise of configuration refusal and checkout preservation. All run in a scratch copy or scratch workspace
under a temporary directory, never in the checkout itself. A method that starts the built binary starts it by its path,
as [development.md](../tooling/development.md#running-the-built-binary) says.

The scratch copy needs a git directory of its own for the checks of its `git status`. A clone of the
checkout with the change under test applied has one. A plain copy does not when the checkout is a git worktree: its
`.git` is then a file that points back to the original repository.

### `workspace:manual-gates`

Exercises the gates' own failure behaviour, which cannot be a test of the suite the gates run. Use it when a change
touches the gate driver, a Cargo alias, or the manifest entry of the scenario target.

**Setup.** Make a scratch copy of the checkout under a temporary directory, where no directory at or above the copy
holds a `.winter/config.toml`, as the built-binary rule of [AGENTS.md](../../AGENTS.md#standing-rules) has it, and work
only there. Inject one fault, run the command the table names, record the result, and restore the copy before the next
fault. A library source is any file under `crates/core/src/`.

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

**Setup.** Make a scratch copy of the checkout under a temporary directory, where no directory at or above the copy
holds a `.winter/config.toml`, as the built-binary rule of [AGENTS.md](../../AGENTS.md#standing-rules) has it, and work
only there. Run the two cases in order.

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

### `workspace:manual-interrupt`

Exercises what the built binary does with SIGINT: that `main` calls the installer of `crates/cli/src/interrupt.rs`, and
what an interrupt then does to the process. Use it when a change touches that file or the start of `main`.

Nothing automatic shows this. The unit tests of `interrupt.rs` prove the installer and the handler in a test process
that raises the signal on itself, no test sends SIGINT to the binary, by the rule on signals of
[testing.md](../standards/testing.md#what-a-test-never-does), and no command runs long enough to be interrupted.

**Setup.** Make a scratch copy of the checkout under a temporary directory, where no directory at or above the copy
holds a `.winter/config.toml`, as the built-binary rule of [AGENTS.md](../../AGENTS.md#standing-rules) has it, and work
only there, in bash on Linux. Run `cargo build --workspace --locked`. Then make a pipe and fill it, so that the binary
waits in its first write of the help text until it is signalled or the pipe is read:

```sh
mkfifo full
exec 3<>full
head -c 65536 /dev/zero >&3
```

Run the two cases in order. In each, start the binary as the table says and keep the process id `$!`. Send `kill -INT`
to that id alone, and only once the process is waiting in its write, which `State: S (sleeping)` in `/proc/<id>/status`
shows: a signal that lands before `main` has called the installer meets the default disposition and ends the process by
the signal, with no notice. Read the exit code from `wait` on the id.

| Started                                                                               | Then                                                            | Expected                                                                |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `set -m`, then `target/debug/winter-workspace --help >full 2>notice 3>&- &`, `set +m` | `kill -INT`                                                     | Exit 1; `notice` holds `aborted` and a newline, once.                   |
| `trap '' INT`, then `target/debug/winter-workspace --help >full 2>notice 3>&- &`      | `kill -INT`; then read the pipe: `head -c 65536 <&3 >/dev/null` | The process is still there after the signal. Exit 0; `notice` is empty. |

A script starts a background command with SIGINT ignored unless job control is on, which is why the first case turns it
on. A binary left waiting ends when the shell that holds descriptor 3 ends, since its write then fails.

**Success.** Each case ends as stated, and `git status` of the scratch copy shows only `notice`: git does not list a
pipe.

### `workspace:manual-repo-remove`

Exercises `repo remove` against inline repository arrays and existing checkouts. Use it when changing removal's text
edit, file replacement, or command wiring. Component and executable tests cover the same contract automatically; this
exercise checks the built binary with disposable git repositories.

**Setup.** Build with `cargo build --workspace --locked`, then set `binary` to the absolute path of the resulting
`target/debug/winter-workspace`. Create a fresh temporary directory outside any real workspace; verify that no ancestor
holds `.winter/config.toml`. Work only in a new `workspace/` beneath it, with a `.winter/config.toml` made for this run.
Keep evidence in a sibling `evidence/` directory. Set `OTEL_SDK_DISABLED=true` for every invocation. Never use the
installed `winter`, a real remote, or an existing checkout or worktree as a fixture.

1. **Shared inline refusal.** Write this as `.winter/config.toml`:

   ```toml
   project_repository = [{ name = "api", url = "https://fixture.invalid/api.git" }, { name = "web", url = "https://fixture.invalid/web.git" }]
   ```

   Save a byte-for-byte copy in `../evidence/shared.before`. Run
   `OTEL_SDK_DISABLED=true "$binary" repo remove project/web --json >../evidence/stdout 2>../evidence/stderr`.
   Expect exit 1, zero stdout bytes, and a stderr diagnostic naming `project`, `.winter/config.toml`, and
   `not written as blocks`.
   `cmp .winter/config.toml ../evidence/shared.before` must exit 0. The URL is only configuration text; do not fetch it.
2. **Local inline refusal.** Replace the shared file with an empty file and put the same inline declaration in
   `.winter/config.local.toml`. Save both files in `../evidence/`. Run the same invocation with `--local --json`.
   Expect exit 1, zero stdout bytes (not a JSON success document), and a diagnostic naming `project`,
   `.winter/config.local.toml`, and `not written as blocks`. Compare both files with
   their saved copies using `cmp`: neither may change, including whitespace and comments.
3. **Checkout preservation.** In the temporary directory create a disposable local git repository with one committed
   file, clone it to `workspace/projects/docs`, and add a linked worktree using
   `git -C projects/docs worktree add -b probe ../../env/docs`. Use only this local repository as the remote. Remove
   the local configuration fixture and write a shared `[[project_repository]]` block with `name = "docs"` and `url`
   set to that remote's absolute path. Add a tracked modification and an untracked file in each checkout.

   Before removal, save `git -C <checkout> status --porcelain=v1 --untracked-files=all`,
   `git -C <checkout> show-ref`, and `git -C projects/docs worktree list --porcelain` in `../evidence/`, for both
   `projects/docs` and `env/docs`. Save copies of both checkout trees, including their `.git` directory or file, outside
   the workspace. Snapshot the disposable remote's refs too. Run
   `OTEL_SDK_DISABLED=true "$binary" repo remove project/docs >../evidence/stdout 2>../evidence/stderr`.
   Expect exit 0, stdout `Removed project repository docs` followed by a newline, and empty stderr. The configuration
   loses only the selected block. Repeat the git observations and compare with `cmp`; compare each checkout with its
   saved copy using `diff -r`. Both directories, all tracked and untracked contents, refs, dirty status, and linked
   worktree registration must remain unchanged; the remote's refs must also remain unchanged.

**Success.** Both refusals preserve configuration bytes and emit no stdout; the successful removal changes only the
configuration block, not the checkout or worktree state. Keep exit codes, streams, and before/after comparisons as
evidence. No command in this exercise runs against the real workspace.

## Tools

| Tool             | Use                                                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------------------------ |
| `tool:format`    | `cargo dev fmt` — rewrites formatting; run before the slice gate when its `fmt` step fails.                        |
| `tool:keep-tree` | `WINTER_TEST_KEEP=1` on any test command — keeps the isolated tree and prints its path.                            |
| `tool:record`    | `cargo dev record` — records a scenario with the reference tool; [scenarios.md](./scenarios.md) says when and how. |
