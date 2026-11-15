# Development

How to set up a checkout, which commands verify a change, and how to read what they print. All of them run from the
repository root.

## Bootstrap

The toolchain is pinned in `rust-toolchain.toml`: one Rust release, with rustfmt and Clippy. With `rustup` installed,
`rustup toolchain install` in the checkout installs it.

Dependencies are fetched once per checkout, and again whenever `Cargo.lock` changes:

```bash
cargo fetch --locked
```

The bootstrap is the only step that needs the network, for the toolchain install and for this fetch. Every command below
runs offline against what the bootstrap fetched.

## The gates

Two commands check a change. The slice gate is run before a change is handed over, and the full gate before a merge.

```bash
cargo dev gate slice
cargo dev gate full
```

`cargo dev` is an alias, defined in `.cargo/config.toml`, that builds and runs the development driver in `crates/dev/`.
A gate runs its steps in order, each as one Cargo process:

| Step        | Gate        | What it runs                                                                      |
| ----------- | ----------- | --------------------------------------------------------------------------------- |
| `fmt`       | slice, full | rustfmt in check mode over the whole workspace                                    |
| `clippy`    | slice, full | Clippy over every package, target, and feature, with warnings denied              |
| `test`      | slice, full | The tests of every package                                                        |
| `scenarios` | full        | The test target `crates/cli/tests/scenarios.rs`, which the `test` step leaves out |

What holds for every step:

- A later step runs whether or not an earlier one failed, so one run reports everything that is wrong.
- Warnings are denied in the `clippy` step alone. It covers compiler warnings as well as Clippy's, in test code too.
- Each step may run for 15 minutes. After that its process group is killed and the step is failed.
- A gate changes no tracked file: its `fmt` step only checks.
- No step uses the network.

The `scenarios` step runs the target that replays recorded scenarios from a `scenarios/` directory at the repository
root. A scenario whose description reads and that needs a facility the runner lacks is deferred: it is listed as
deferred, is not run, and does not fail the step, even where it has no record or a stale one. The target replays every
other scenario directory it finds with the binary Cargo built, each in an isolated tree of its own, and prints one line
per scenario, its differences under a scenario that failed, and a last line with the counts of those that passed,
failed, and were deferred. Without a scenario directory it reports zero scenarios and passes. It fails when a scenario
that is not deferred differs from its record, runs past its deadline, or has no record or one that is stale or does not
read whole, when a scenario is malformed, and when `scenarios/` holds anything else that is no scenario directory: only
`reference.toml` and command directories at the root, and only scenario directories in a command directory.

The target is run alone with `cargo scenarios [FILTER]... [--list]`. A filter keeps the entries whose id contains it.
Every other entry is left out of the report, a malformed one included: it is not replayed and not counted, so it does
not fail a filtered run. The whole root is read all the same, so a root or a command directory that cannot be read fails
a filtered run too. A filter that matches nothing reports zero scenarios and passes. `--list` prints each id with its
state and replays nothing. `WINTER_SCENARIO_JOBS` sets how many scenarios run at once, and with `WINTER_TEST_KEEP=1` the
tree of each failed scenario is kept and its path printed.

The CI workflow, `.github/workflows/check.yml`, runs the bootstrap, its toolchain install and its fetch, and then the
full gate. It runs nothing a contributor cannot run locally.

## Formatting

```bash
cargo dev fmt
```

This rewrites formatting, and is the only driver command that changes a tracked file. Run it when the `fmt` step fails,
then run the gate again.

Use it in place of a bare `cargo fmt`. `cargo fmt` follows `mod` declarations from each target's root, so it reaches
neither a command file, which only generated code declares, nor `crates/core/build/discover.rs`, which the build script
includes. The `fmt` step and `cargo dev fmt` pass those files to rustfmt explicitly.

## Reading a gate's output

The output of each step passes through unchanged as it is produced. The driver adds one summary line, the last line on
stderr:

| Summary                                  | Exit | Meaning                                                |
| ---------------------------------------- | ---- | ------------------------------------------------------ |
| `gate <name>: ok`                        | 0    | Every step passed.                                     |
| `gate <name>: failed: <step>[, <step>…]` | 1    | The named steps failed, timed out, or could not start. |
| A usage line, with no summary            | 2    | The arguments name no command the driver has.          |

To read a failure, take the step names from the summary and find each step's own output above it. A step that timed out
or could not start has a line from the driver that begins with the step's name.

An interrupt, SIGINT or SIGTERM, kills the running step's process group, fails that step, and ends the run with exit 1;
the steps after it do not start.

## One tier or one file

The gates run every test. While working on one thing, a narrower run is quicker. The commands for each tier are in
[the verifiability matrix](../verification/matrix.md); a single test file is a test target of its own:

```bash
cargo test -p winter-workspace-core --test root
```

## Running the built binary

A build writes the binary to `target/debug/winter-workspace`, and a release build to `target/release/winter-workspace`.
Where it may run is a standing rule of [AGENTS.md](../../AGENTS.md#standing-rules). It is on no `PATH`: from a directory
that rule permits, it is started by its path, as in `<checkout>/target/debug/winter-workspace --help`, where
`<checkout>` is the path of the repository root.

## Markdown

`dprint.json` and `.rumdl.toml` configure Markdown formatting and lint for an editor and for hand-run tools. No gate
runs either tool: the gates need nothing but the pinned toolchain.
