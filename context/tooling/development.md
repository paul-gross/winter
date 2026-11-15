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

This is the only step that needs the network. Everything below runs offline against what it fetched.

## The gates

Two commands decide whether a change is ready. The slice gate is run before a change is handed over, and the full gate
before a merge.

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
root. The target holds no replay yet: without a scenario directory it reports zero scenarios and passes, and it fails on
any scenario directory it finds.

The CI workflow, `.github/workflows/check.yml`, installs the toolchain, runs the bootstrap, and runs the full gate. It
runs nothing a contributor cannot run locally.

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

## Markdown

`dprint.json` and `.rumdl.toml` configure Markdown formatting and lint for an editor and for hand-run tools. No gate
runs either tool: the gates need nothing but the pinned toolchain.
