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

The architecture test checks the token table by scanning each source as text. Nothing is exempt: a test module inside
the library is checked like the rest, and so is a comment. A failure names the file, the line, and the token.

A module reports a condition by handing a `Record` to `diagnostics::record` in `crates/core/src/diagnostics.rs`, with
the stderr handle and the `Log` of the invocation. That function alone decides whether the record is written and in
which format; no other module formats a log record or writes one to the stderr handle itself. This rule is checked in
review: the scan for tokens does not see it.

The library opens no socket, reads no clock, and names no tracing crate. What it knows of tracing is in
`crates/core/src/trace.rs`: the settings, read from the environment snapshot at an instant the binary gives it, and the
port `CommandTracer`, which `run` announces the command to. The binary implements that port in
`crates/cli/src/trace.rs`, the one source file that names a tracing crate. No source of the library or of the binary
names an asynchronous runtime or starts one: the HTTP client of the exporter runs its own. This is checked in review.

## Module files

A module `x` is `x.rs`, with its children in `x/`. The architecture test fails on any `mod.rs` under `crates/core/src/`.

## Errors

- An error type is a `thiserror` enum or struct that carries the values its message names.
- `InternalError` alone implements `Display` and `Error` by hand. Its variants hold a backtrace, and for a field of that
  type the derive generates a method over a standard-library API this repository does not assume to be stable.
- No `String` error crosses a module boundary.
- `Termination` stays narrow whatever its error types grow to. A variant that carries an error type holds it in a `Box`,
  and the fields a variant holds inline are scalars or one `String`. The site that builds such an ending boxes the error
  there, and the error types themselves hold no box for this purpose. A fallible function of the library returns
  `Result<_, Termination>` by value. No lint attribute and no lint setting is the answer to an error that is too large.
  A unit test in `crates/core/src/termination.rs` checks that `Termination` is at most four words wide.
- A broken invariant of the parser is an internal failure, built through the constructor `InternalError::unexpected`,
  and never a usage error: a registry the command tree cannot be built with, a parsed value the node's own parser should
  have supplied, a command selected under an empty command path, root options the parser accepted that do not read on
  their own.
- No `unwrap`, `expect`, or `panic!` is applied to a value that comes from the user, a file, or a child process.
- A panic ends the invocation as the internal failure: the hook and `conclude` in `crates/cli/src/main.rs` turn it into
  `Termination::Internal(InternalError::Panic { .. })`, with its error line on stderr and exit code 1. Three panics of
  the binary's tracer are the exception, because tracing never changes how an invocation ends: one raised while
  `crates/cli/src/trace.rs` builds the exporter and opens the span is caught there and leaves tracing unavailable; one
  raised on the export thread, which ends and exports the span, is caught on that thread and is a failed export; and one
  raised on the thread that calls the tracer to end the span is caught by `conclude` and dropped. No library code
  outside a test module catches an unwind. A test module may catch one to turn a panic of the code under test into a
  failure it reports itself, as the registry consistency check of `crates/core/src/parse.rs` does around the parser's
  debug assertions.

The width of `Termination` is checked by its unit test, and the ending of a panic that reaches `conclude` by the unit
tests of `crates/cli/src/main.rs`. The rest is checked in review, the absence of an unwind catch in library code outside
a test module included.

## Unsafe

In the library and the binary, `unsafe` appears in `crates/cli/src/interrupt.rs` and `crates/cli/src/trace.rs` alone. In
`interrupt.rs` it is where the disposition of SIGINT is read and the handler is installed, where the handler reads its
state, writes its notice, and ends the process, and where its tests set the disposition and raise the signal. In
`trace.rs` it is the one block that removes the generic exporter variables from the process environment, which is sound
because `main` calls it while the process has one thread, and in its test module the fixture that sets the disposition
of SIGINT and raises the signal on itself. Each block carries a comment that states why it is sound. This is checked in
review.

## Lint attributes

rustfmt and Clippy run with their defaults: the repository holds no configuration file for either, and no workspace lint
table. Warnings are denied by the gate's `clippy` step, which covers compiler and Clippy warnings of every package,
target, and feature; [development.md](../tooling/development.md#the-gates) has the step.

Five items of the library carry `#[allow(dead_code, reason = "…")]` permanently: `Context`, `Invoked`, `RootOptions`,
`Registration`, and `Entry`. They are contracts that command files fill, and command files are discovered at build time,
so a tree with no command or no alias reads only part of them and must still pass a gate that denies warnings.
`#[expect(dead_code)]` is never used on them, because the first command would turn the expectation into a failure.

No other item of the library carries a lint attribute for code that nothing uses. Such code is not written until
something uses it.
