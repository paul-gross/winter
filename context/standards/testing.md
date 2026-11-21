# Testing

Which tier a behaviour is tested at, what `winter-workspace-test-support` gives a test, and what a test never does. The
command that runs each tier is in [the verifiability matrix](../verification/matrix.md).

## Tiers

| Tier       | Lives in                         | Starts a process | Proves                                                             |
| ---------- | -------------------------------- | ---------------- | ------------------------------------------------------------------ |
| Unit       | `#[cfg(test)]` beside the code   | No, except below | A pure rule: parsing, derivation, ordering, classification.        |
| Component  | `crates/core/tests/<subject>.rs` | Git only         | A behaviour of the library, run in process with in-memory handles. |
| Executable | `crates/cli/tests/<subject>.rs`  | The built binary | The process-level contract: streams, exit code, environment.       |

Choose the lowest tier that can make the claim:

- A unit test takes no file, git, or process input and uses nothing from the support package. There are two exceptions.
  The unit tests of the file port, `crates/core/src/files.rs`, touch the file system, and only inside a `TempTree` of
  their own. In the binary package, a unit test whose subject ends its own process or changes its own environment starts
  a child test of its own test binary through `invoke`, as [Tests that must fail](#tests-that-must-fail) describes.
  There are three such subjects: the SIGINT handler of `crates/cli/src/interrupt.rs`; the removal of the generic
  exporter variables in `crates/cli/src/trace.rs`; and the tracer of that file under the handler, whose test holds the
  `Collector` its child exports to and writes the certificate file the child reads. A child that takes an interrupt
  raises the signal on itself, and the test sends none.
- A component test also calls an entry re-exported from `lib.rs`, against the git fixtures, for an entry that no command
  of the library reaches yet or that another epic's command calls.
- A component test runs the library in process and asserts on the returned `Termination` and on what was written. The
  run is built through the scaffold `crates/core/tests/support/in_process.rs`, a module the test file declares by
  `#[path]`. `InProcess` takes the arguments, the working directory, the environment, and stdin, calls the library's
  `run` over in-memory handles, and returns an `Ended` with the `Termination` and both streams as text. `Ended` carries
  two assertions on an ending, each taking a subject that names the case in the message of a failure: `Ended::document`
  asserts a success with an empty stderr and answers stdout parsed as JSON, and `Ended::wrote_nothing` asserts an empty
  stdout, an empty stderr, and an exit code, and answers the `Termination`. A test file never builds an `Invocation`
  itself. A test that needs a stdout of its own, such as one that fails, keeps that writer in its file and hands it to
  `run_with_stdout`. When the behaviour reads or writes files or git state, the test builds a fixture workspace with
  `TempTree`, `Git`, and `WorkspaceBuilder`; the library's manifest declares the support package as a dev-dependency
  once a component test uses it. A helper that more than one component test file needs has one owner: the support
  package for one that reads or builds a tree, as `snapshot` reads every entry below a directory into a map of `Found`
  values, and `Ended` for an assertion on what `run` answered. No component test file holds a copy of `snapshot`, of
  `Found`, or of either assertion as a helper. A component test of a behaviour that starts git lets the library start it
  through its own program port, and hands `InProcess` the whole pinned environment of its tree with `InProcess::pinned`,
  which takes `PinnedEnv::variables`. An invocation left with the empty environment would have that port search the
  directories of the host that stand in for a missing `PATH`, and let git read the configuration of the host. The git it
  starts is the host's, on the pinned `PATH`, or a stand-in the test wrote to `bin/` of its tree, which comes first
  there and records its calls under `calls/`.
- An executable test starts the binary Cargo built, at `env!("CARGO_BIN_EXE_winter-workspace")`, through `invoke`. It is
  for a claim about the process and not about the library. `invoke_with_stdout` starts it with a stdout that cannot be
  written, one of the three faults of `StdoutFault`: `ClosedPipe`, a pipe whose reader is gone; `Full`, a device with no
  room; `Closed`, no stdout at all. No other tier gives the binary such a stdout: how it ends under each is held in
  `crates/cli/tests/closed_stdout.rs`.

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

## The trace collector

`Collector`, in `crates/test-support/src/otlp.rs`, is a stand-in for a trace collector: a listener on 127.0.0.1, on a
port the system chooses, served by one thread in the test process. It binds the loopback address and nothing else,
resolves no name, lives outside the isolated tree, and holds no file.

The sockets a test opens or causes are these, each on 127.0.0.1, and no other:

- a `Collector` the test started, and the bound socket of a `RefusedEndpoint`, which never listens;
- the client side of a connection to a `Collector`, from the test itself or from a child the test starts and hands the
  endpoint to;
- a connection attempt to the address of a `RefusedEndpoint` that is still held, from the test or from such a child,
  which reaches no listener and is refused;
- in the support package's own tests of the collector, and nowhere else, a listener of the test's own: the listener a
  collector serves, bound without a collector, with the client side of the connections the test makes to it while it is
  bound; the port of a collector that was dropped, bound again to show it free; the listener whose bind to the port of a
  held `RefusedEndpoint` fails; and the port of a dropped `RefusedEndpoint`, bound to show it free.

What the collector gives a test:

- `Collector::start` takes the `Answer` every request gets: `Ok`, a success status; `Status`, the status it names;
  `Hang`, no answer; `Stall`, the head of a success status that announces a body of 1048576 bytes, and none of that
  body; or `Drip`, the same head and then one byte of the body every 20 ms. The connection of each of the last three is
  held open until the collector stops, and no longer.
- A test of a bound in time holds its peer until the subject has ended, and waits out no stall of a fixed length: it
  stops the collector only after the child has ended, so a subject that waits for its peer does not end by itself and
  fails at the deadline of `invoke`.
- `endpoint` is the address a client is given, `http://127.0.0.1:<port>`.
- The thread reads one HTTP/1.1 request from each connection: the request line, the headers, and a body of
  `Content-Length` bytes, within five seconds. Anything else, a TLS handshake or a chunked body for one, counts in
  `connections` and adds no request.
- `requests` stops the collector and returns what it read: method, target, headers with their names in lower case, and
  body. A connection made before the stop is still served, for five seconds at most once the thread has seen the stop,
  so a client that goes on connecting does not keep a stopped collector alive. Dropping the collector stops it the same
  way; either joins the thread and frees the port.
- `RefusedEndpoint::hold` gives a value that owns a socket bound to a loopback port the system chose, which never
  listens; its `endpoint` is an address of the same form. A connection to it is refused for as long as the value lives,
  and no TCP socket that is bound on 127.0.0.1 and asks the system for a port is given that one, the listener of a
  `Collector` among them. A test keeps the value until every child it handed the endpoint to has ended: once the value
  is dropped, the port may be given to anyone.

`decode_export` reads the protobuf body of a trace export request into a `ReceivedExport`: the string-valued resource
attributes, the scope's name, and each span as a `ReceivedSpan`. It is written by hand from the field numbers of the
format, which its documentation lists, and the support package depends on no tracing crate, so a test compares what an
exporter wrote with what it should have written. It skips every field it does not read, and a message it cannot read
through is an error.

## A test that wants a span

The pinned environment turns tracing off: it sets the kill switch, `OTEL_SDK_DISABLED`, to `true`. A test that wants the
binary to trace does three things through `PinnedEnv::set`:

- it sets `OTEL_SDK_DISABLED` to `false`, which clears the kill switch;
- it sets `WINTER_OTEL_EXPORTER_OTLP_ENDPOINT` to the endpoint of a `Collector` it started or of a `RefusedEndpoint` it
  holds;
- it sets `SSL_CERT_FILE` to a certificate file it wrote under `tmp/` of its tree.

The exporter reads its trust store when it is built, for an `http` endpoint as for an `https` one, and with
`SSL_CERT_FILE` set it reads that file and not the store of the host. The file holds the text of
`crates/cli/tests/data/trust-root.pem`, which the test compiles in: one self-signed certificate made for the tests,
whose private key was discarded. No test reads the committed file when it runs, and no test completes a TLS handshake,
so the certificate judges nothing. A test of a machine without certificates names an empty file of its tree instead, and
tracing is then unavailable. No test sets the variable that names a directory of certificates.

The real exporter is built only in a child a test starts through `invoke` under the pinned environment, and never in a
test process, where it would read the proxy, tracing, and certificate variables of the developer. That child is the
built binary, in `crates/cli/tests/trace.rs`, by preference. The one other is the child test of
`crates/cli/src/trace.rs` whose subject ends its own process. Every other test of the tracer in that file goes through
`Tracer::with_exporter` with an exporter of its own that records what it is handed and opens no socket.

## What a test never does

The standing rule in [AGENTS.md](../../AGENTS.md) holds for every test of the library, the binary, and the support
package. In practice:

- It creates files only under its own tree, and reaches no workspace or repository that is not a fixture in that tree.
  The one exception is the repository's committed `scenarios/`, which a test may read and never writes: it copies what
  it needs into its tree.
- It starts a child only through `invoke`, `invoke_with_stdout`, or `Git`, by an absolute path and under the pinned
  environment. No shell stands between a test and its child, and no name is looked up on `PATH`. A test of the support
  package that needs a child with a behaviour of its own runs `/bin/sh` as that child, with a script the test spells
  out.
- It resolves no host name. Fixture remotes are the tree's own bare repositories.
- It opens no socket but those [The trace collector](#the-trace-collector) lists.
- It sends no signal to another process. There are two exceptions, and no other. The runner sends SIGKILL to the process
  group of the child it started, before it reaps that child, on every way out of the call, an unwind included. One test
  of the support package, in `crates/test-support/tests/process.rs`, starts a process that leaves that group, where the
  runner's kill does not reach it, and ends it with SIGKILL by the id the process reported. No test sends SIGINT, or any
  signal but SIGKILL, to another process. The runner exposes no process id, so a test that needs a signalled child has
  the child signal itself. The driver is outside this rule: its step runner, in `crates/dev/src/step.rs`, kills the
  process group of a step it ends.
- It lets nothing of the developer's environment or home directory reach a child or decide a result. The test process
  reads its own environment only for `WINTER_TEST_` variables and for the directory of the host's git on `PATH`. The two
  fixtures of `crates/cli/src/trace.rs` are the exception: each is a child that reads the whole environment it was
  started with, which is the pinned one of its parent's tree.

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

The unit tests of the SIGINT handler, in `crates/cli/src/interrupt.rs`, use the same pattern for a subject that ends its
own process. Each starts its fixture as a child test of its own test binary, at `std::env::current_exe()` and by the
exact name of the fixture. The fixture sets the disposition of SIGINT it is to start with, default or ignore, calls the
installer, writes a line to stdout, and raises SIGINT on itself, and the test reads the exit and both streams of the
child once it has ended. The parent sends its child no signal but the runner's SIGKILL of its group, as
[What a test never does](#what-a-test-never-does) has it.

The unit tests of `crates/cli/src/trace.rs` have two fixtures of the same pattern. One calls the removal of the generic
exporter variables on a snapshot of its own environment, which its parent gave two such variables, and asserts that the
process holds neither afterwards and the snapshot both. The other installs the handler, has the library announce a
command to a tracer built from its environment, which names the parent's `Collector`, writes a line to stdout, and
raises SIGINT on itself; its parent asserts the exit code 1 and that the collector read no request.
