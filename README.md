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

`init` creates a workspace in the directory it is started in: `.winter/config.toml`, `AGENTS.md`, and `CLAUDE.md`. A
directory that already holds `.winter/config.toml` is refused. Like the other mutating commands, it must only be
exercised in the scratch locations permitted by [AGENTS.md](./AGENTS.md). Read `winter-workspace context init` for what
it does with agent files that are already there.

`repo add` and `repo remove` edit the workspace configuration it finds at or above the working directory, and
`ws update` fetches, resets branches, stashes, and writes the lock of the workspace it finds there, and, with `--freeze`, its configuration.
`ws prune` removes directories, links, and files of the workspace it finds there and rewrites its exclude file.
[AGENTS.md](./AGENTS.md) states where the binary may run. It is on no `PATH`: from a directory that rule permits, it is
started by its path, as in `<checkout>/target/debug/winter-workspace --help`, where `<checkout>` is the path of the
repository root.

`repo remove <type>/<name>` removes a `[[project_repository]]` or `[[standalone_repository]]` block from the shared
configuration (`--local` selects the local file). Inline arrays such as `project_repository = [{ ... }]` are refused:
the command fails without changing the file or printing a success result. Removal edits configuration only; existing
checkouts, linked worktrees, their contents, and git state are preserved.

`repo add <URL>` appends a `[[project_repository]]` block to the shared configuration, or a `[[standalone_repository]]`
block with `--standalone`; `--local` writes the machine-local file, which is created when absent. Inline arrays such as
`project_repository = [{ ... }]` are refused without changing the file. A standalone whose location, with symbolic links
followed, is that of a declared standalone is refused, and the file is left unchanged. Adding edits configuration only:
nothing is cloned.

`ws init` clones missing source checkouts and standalones, verifies existing ones, and updates managed workspace
content. It runs configured repository commands on every invocation, including repeats; those commands must be safe to
rerun. Like the other mutating commands, it must only be exercised in the scratch locations permitted by
[AGENTS.md](./AGENTS.md). Read `winter-workspace context ws/init` for its ordering, recovery, and scope.

`ws prune` removes material the workspace once managed and no configuration declares now: undeclared clones under
`projects/`; clones and agent copies that a leftover workspace exclude block names; and dangling links in
`.claude/skills`, `.claude/agents`, and `.codex/skills`. It retains a clone that could hold work, for example one with
uncommitted or untracked changes, with commits no remote-tracking branch holds, with stashes or linked worktrees, or
with no `.git`. This command never removes retained material: delete it by hand. It lists what it found and asks first:
`--dry-run` only lists, `--force` does not ask, and `--json` needs one of the two. Once the removals ran it exits 0,
even when one of them failed, so read its lines. Ignored files go with a removed clone. Read `winter-workspace context
ws/prune`.

`ws worktrees --status` adds the git counts of each location: the commits ahead of and behind the main branch of
`origin` and the number of dirty files for a worktree, the same against the upstream of its branch for the workspace
root, and none for a standalone. It runs the `git` found on `PATH`, searching `/bin` and then `/usr/bin` when `PATH` is
unset. Git gets the environment the binary was given, less `WINTER_LAUNCH_TIME`, with `GIT_SSH_COMMAND` set to `ssh -o
ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3` unless the environment already sets it, an empty
value included, and with `LANGUAGE` and `LC_ALL` set to `C`. A git call that fails for a worktree ends the command with
exit code 1: nothing is written to stdout, and the failure is written once to stderr, and once more as a log record
under `-v`.

`ws fingerprint` prints a 64-character hexadecimal digest of the workspace definition: the tracked content of the
workspace repository, and the name and tracked content of each standalone. `--json` prints the digest with the commit,
tree, and dirty flag of each repository. Untracked files, `projects/`, feature environments, and history are outside it,
so two checkouts of one definition have one digest. It reads no network and never writes the index of a repository: its
only writes to a repository are unreferenced objects, and it makes a scratch index under `TMPDIR`, or `/tmp` when that
is unset or empty, which it removes. A missing checkout or a failed git call ends the command with exit code 1.

`ws update --freeze [REPOS]...` pins each selected standalone, pinned or not, to its current commit: it writes
`ref = "<commit>"` into the configuration file that declares it, `.winter/config.toml` first and then
`.winter/config.local.toml`, and records a `commit` entry in `.winter/config.lock`. It uses no network and checks
nothing out. A standalone that already declares a `ref` is reported as already pinned. A checkout that is missing, that
is no repository, or that has uncommitted changes is refused and the command ends with exit code 1, and `--force` pins
a dirty checkout. A standalone that neither file declares as a block is refused. `--force` without `--freeze`, and
`--autostash` with `--freeze`, are usage errors with exit code 2.

`ws update [REPOS]...` re-pins the standalone repositories that declare a `ref`: it fetches `origin`, resolves the ref
again as a remote branch, then a tag, then a commit, checks the result out, and records it in `.winter/config.lock`. A
branch is reset to `origin/<ref>`, so commits only the local branch held are left to the reflog. A standalone already on
the recorded commit is reported up to date and the lock is not rewritten. Each REPO is a name or a glob over the names;
a name that is no pinned standalone ends the command with exit code 1 before anything is changed, and a glob that
matches nothing is no error. A checkout with uncommitted changes is not re-pinned and the command ends with exit code 1,
unless `--autostash` is given, which stashes the changes and restores them afterwards; when the restore conflicts, a
warning names the checkout and the stash is kept. One standalone that fails does not stop the next. `--json` prints one
event per line as it happens.

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

| Code | Meaning     | When                                                                                                                                                     |
| ---- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | Success     | A command completed; `--help`; `--version`.                                                                                                              |
| `1`  | Failure     | A command failed; a configuration error; a failed git call; a stdout that cannot be written; an internal failure; a declined confirmation; an interrupt. |
| `2`  | Usage error | An unknown command or option, a missing or unexpected argument, or no command: the bare root or a bare group.                                            |

An interrupt, Ctrl-C, ends the invocation at once with the notice `aborted` on stderr and exit code 1, not 130. A
process that was started with the interrupt signal ignored leaves it ignored, and an interrupt does nothing to it.

### Logging

Log records go to stderr. Without a switch the threshold is `WARNING`. `-v` logs at `DEBUG` and wins over the variable.
`WINTER_LOG_LEVEL` sets the threshold by name, in any letter case: `DEBUG`, `INFO`, `WARNING` or `WARN`, `ERROR`,
`CRITICAL` or `FATAL`, and `NOTSET`, which leaves the threshold at `WARNING`. Any other value is ignored silently.

A warning that is no log record is written to stderr whatever the threshold. `repo list` writes one when the projects
the configuration declares and the directories under `projects/` disagree: one line that names the declared projects
with no directory and the directories no project declares.

## Contributing

[AGENTS.md](./AGENTS.md) is the entry point for contributors and their agents. [PRINCIPLES.md](./PRINCIPLES.md) states
the tenets behind Winter and why it exists.

## License

Source-available under the Functional Source License 1.1 (FSL-1.1-ALv2). Each release converts to Apache License 2.0 two
years after it ships. See [LICENSE](./LICENSE).
