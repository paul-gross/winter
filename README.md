# winter-workspace

This repository is the Rust source of the `winter-workspace` binary. The binary is under construction and has the
commands its `--help` lists: `winter-workspace --help` names the command groups, and `--help` after a group names its
commands. The binary is not the installed `winter` and does not replace it.

## Building

The repository is a Cargo workspace. With a Rust toolchain installed through `rustup`, build the binary from the
repository root:

```bash
cargo build
```

The binary is written to `target/debug/winter-workspace`.

`repo remove` edits the workspace configuration it finds at or above the working directory. [AGENTS.md](./AGENTS.md)
states where the binary may run. It is on no `PATH`: from a directory that rule permits, it is started by its path, as
in `<checkout>/target/debug/winter-workspace --help`, where `<checkout>` is the path of the repository root.

`repo remove <type>/<name>` removes a `[[project_repository]]` or `[[standalone_repository]]` block from the shared
configuration (`--local` selects the local file). Inline arrays such as `project_repository = [{ ... }]` are refused:
the command fails without changing the file or printing a success result. Removal edits configuration only; existing
checkouts, linked worktrees, their contents, and git state are preserved.

## Tracing, exit codes, and logging

### Tracing

A command can send one trace span to a trace collector over the network. It does so only when tracing is switched on,
and it is off unless the environment switches it on.

- **The switch.** Tracing is on when `WINTER_OTEL_EXPORTER_OTLP_ENDPOINT` is set and not blank. The installed `winter`
  reads the same variable, so a shell set up for that tool switches tracing on for this binary as well.
- **The kill switch.** `OTEL_SDK_DISABLED=true` turns tracing off, whatever the endpoint variable holds.
- **Ignored.** The generic `OTEL_EXPORTER_OTLP_*` variables neither switch tracing on nor change the request.
- **Where it goes.** The span is sent in one `POST` request to the value of the endpoint variable followed by
  `/v1/traces`, at the end of each invocation that names a command or a group, its `--help` and its usage errors
  included. `--version`, the root help, and a command line that names neither a command nor a group send nothing.
  Nothing is sent either when `TRACEPARENT` says its caller did not sample the trace, or when the exporter cannot be set
  up, as on a machine whose certificate store holds no certificate.
- **What is sent.**
  - The name of the span, which is `winter` followed by the command path, as in `winter ws index`, and the same text as
    the attribute `winter.command`.
  - The attribute `error.type` when the exit code is not 0: one name from a fixed list that says how the invocation
    ended, such as `Usage`.
  - The instants the span started and ended, and its ids. The start is the time `WINTER_LAUNCH_TIME` gives when that
    variable holds a valid one. When `TRACEPARENT` names a sampled trace, the trace id and the parent span id are the
    ones it gives.
  - The service name, which is `OTEL_SERVICE_NAME`, else the `service.name` entry of `OTEL_RESOURCE_ATTRIBUTES`, else
    `winter`, and every other entry of `OTEL_RESOURCE_ATTRIBUTES`.
  - The headers listed in `WINTER_OTEL_EXPORTER_OTLP_HEADERS`, which may hold credentials, with two exceptions. A
    `content-type` is not sent: the export always carries the protobuf one. A header whose value is not legal in an HTTP
    header is not sent, and a log record under a logging switch names it, without its value.
  - What the tracing library and the HTTP client add of their own, such as the name of the instrumentation scope and a
    `User-Agent` header.
- **What is never sent.** No argument, no option value, no path, no message, nothing a command wrote, and no value of
  the environment other than those named in this section.
- **Redirects.** A redirect the collector answers is followed, so the request and the configured headers can go to an
  address the collector names.
- **Proxies.** The HTTP client reads the proxy variables `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`, in
  capitals or in lower case. When one names a proxy for the endpoint, the export goes through that proxy, and a user
  name and a password in the address of the proxy are sent to the proxy.
- **The bound.** The end of a command waits for the export for 100 ms at most.
- **A failed export.** It changes neither stdout nor the exit code. It is reported only as a log record on stderr, and
  only under a logging switch: `-v`, or a valid `WINTER_LOG_LEVEL` at `ERROR` or below.

### Exit codes

| Code | Meaning     | When                                                                                                          |
| ---- | ----------- | ------------------------------------------------------------------------------------------------------------- |
| `0`  | Success     | A command completed; `--help`; `--version`.                                                                   |
| `1`  | Failure     | A command failed; a configuration error; a stdout that cannot be written; an internal failure; an interrupt.  |
| `2`  | Usage error | An unknown command or option, a missing or unexpected argument, or no command: the bare root or a bare group. |

An interrupt, Ctrl-C, ends the invocation at once with the notice `aborted` on stderr and exit code 1, not 130. A
process that was started with the interrupt signal ignored leaves it ignored, and an interrupt does nothing to it.

### Logging

Log records go to stderr. Without a switch the threshold is `WARNING`. `-v` logs at `DEBUG` and wins over the variable.
`WINTER_LOG_LEVEL` sets the threshold by name, in any letter case: `DEBUG`, `INFO`, `WARNING` or `WARN`, `ERROR`,
`CRITICAL` or `FATAL`, and `NOTSET`, which leaves the threshold at `WARNING`. Any other value is ignored silently.

## Contributing

[AGENTS.md](./AGENTS.md) is the entry point for contributors and their agents. [PRINCIPLES.md](./PRINCIPLES.md) states
the tenets behind Winter and why it exists.

## License

Source-available under the Functional Source License 1.1 (FSL-1.1-ALv2). Each release converts to Apache License 2.0 two
years after it ships. See [LICENSE](./LICENSE).
