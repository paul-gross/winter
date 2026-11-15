# Rust

The rules for Rust in the library, `crates/core/`, and the binary, `crates/cli/`. Some are checked by the architecture
test or by the gates, and the rest are checked in review; each section says which.

## Output and environment access

The library returns values and the binary alone touches the process, as
[structure.md](../architecture/structure.md#the-library-and-the-binary) describes. No source under `crates/core/src/`
may contain any of these tokens:

| Area             | Forbidden tokens                                                                         |
| ---------------- | ---------------------------------------------------------------------------------------- |
| Standard streams | `std::io::stdout`, `std::io::stderr`, `io::stdout(`, `io::stderr(`                       |
| Printing         | `println!`, `eprintln!`, `print!`, `eprint!`, `dbg!`                                     |
| Process exit     | `std::process::exit`, `process::exit(`                                                   |
| Environment      | `std::env::var`, `std::env::vars`, `std::env::args`, `std::env::current_dir`             |
| File system      | `std::fs`, in every source except `crates/core/src/files.rs`, the place of the file port |

A command writes its result to the stdout handle of its `Context` through `crates/core/src/output.rs`, as
[changes.md](../architecture/changes.md#writing-the-result) describes, and reads the environment and the working
directory from the `Context`.

The architecture test checks this by scanning each source as text. Nothing is exempt: a test module inside the library
is checked like the rest, and so is a comment. A failure names the file, the line, and the token.

## Module files

A module `x` is `x.rs`, with its children in `x/`. The architecture test fails on any `mod.rs` under `crates/core/src/`.

## Errors

- An error type is a `thiserror` enum or struct that carries the values its message names.
- `InternalError` alone implements `Display` and `Error` by hand. Its variants hold a backtrace, and for a field of that
  type the derive generates a method over a standard-library API this repository does not assume to be stable.
- No `String` error crosses a module boundary.
- No `unwrap`, `expect`, or `panic!` is applied to a value that comes from the user, a file, or a child process.

These are checked in review.

## Unsafe

`unsafe` appears only with a comment that states why it is sound. This is checked in review.

## Lint attributes

rustfmt and Clippy run with their defaults: the repository holds no configuration file for either, and no workspace lint
table. Warnings are denied by the gate's `clippy` step, which covers compiler and Clippy warnings of every package,
target, and feature; [development.md](../tooling/development.md#the-gates) has the step.

Three items of the library carry `#[allow(dead_code, reason = "…")]` permanently: `Context`, `Registration`, and
`Entry`. They are contracts that command files fill, and command files are discovered at build time, so a tree with no
command or no alias reads only part of them and must still pass a gate that denies warnings. `#[expect(dead_code)]` is
never used on them, because the first command would turn the expectation into a failure.

No other item of the library carries a lint attribute for code that nothing uses. Such code is not written until
something uses it.
