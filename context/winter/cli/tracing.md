# Tracing

Opt-in OpenTelemetry tracing for winter commands: each command emits a root span, nested under the caller's trace when
the caller passes one, with inner spans for the git calls, service provider calls, readiness waits and provision
handlers it runs. `winter dashboard` is the exception to one trace per command: it is traced as a series of short traces
and exported while it runs. Tracing is off unless an endpoint is set, and a process with tracing off does no tracing
work and loads no OpenTelemetry code. For the hub and the command surface, see [index.md](./index.md).

## Switching it on

Set `WINTER_OTEL_EXPORTER_OTLP_ENDPOINT` to the base URL of an OTLP/HTTP receiver. Winter posts `http/protobuf` to
`<endpoint>/v1/traces`. The supported topology is a collector on the same host.

| Variable                             | Meaning                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL of the OTLP/HTTP receiver, e.g. `http://localhost:4318`. Tracing is on when it is set to a non-blank value.                              |
| `WINTER_OTEL_EXPORTER_OTLP_HEADERS`  | Optional request headers in the standard `key1=value1,key2=value2` format.                                                                        |
| `OTEL_SDK_DISABLED`                  | `true` (any case) turns tracing off even with the endpoint set.                                                                                   |
| `TRACEPARENT`                        | The W3C trace context of the caller's span. When set, it is the parent of the command's root span and its sampled flag decides whether to record. |
| `OTEL_RESOURCE_ATTRIBUTES`           | Resource attributes added to every span, so a caller can tag spans (worker or step ids, for example).                                             |
| `OTEL_SERVICE_NAME`                  | The `service.name` of the spans.                                                                                                                  |

The `service.name` resolves in this order: `OTEL_SERVICE_NAME`, then a `service.name` entry in
`OTEL_RESOURCE_ATTRIBUTES`, then `winter`. A caller that spawns winter owns what its own `OTEL_SERVICE_NAME` does to
winter's spans.

Winter's export pipeline is configured by the variables above alone. The generic exporter variables
(`OTEL_EXPORTER_OTLP_*` and `OTEL_PYTHON_EXPORTER_OTLP_*`: endpoint, headers, timeout, compression, certificates,
credential providers) do not switch tracing on and do not reach winter's export request; they stay in the environment
unchanged for child processes. A new root span is always recorded, and a span under a `TRACEPARENT` follows the caller's
sampled flag.

## The span

| Property  | Value                                                                                                                  |
| --------- | ---------------------------------------------------------------------------------------------------------------------- |
| Name      | `winter` followed by the full command path, such as `winter provision`, `winter ws init`, or `winter service up`.      |
| Attribute | `winter.command`, equal to the span name. `winter.env` joins it when the command targets exactly one feature env.      |
| Parent    | The caller's span from `TRACEPARENT`; a new trace root when it is unset.                                               |
| Start     | The moment winter dispatches the command, or the launch instant in `WINTER_LAUNCH_TIME` when the launcher exports one. |
| Success   | A command that exits 0 leaves the status unset.                                                                        |
| Failure   | Any other exit sets error status and an `error.type` attribute holding the exception class name.                       |

## Launcher-gap coverage

The `winter` launcher shim exports `WINTER_LAUNCH_TIME` as its first statement, so the span starts when the shim does
and covers the `uv` startup before Python runs, a gap of a few hundred milliseconds. Re-run
`./tools/winter-cli/install.sh` to enable it: the shim in `~/.local/bin` is a copy, and the installer replaces it only
when the source shim's version is newer. Until then the span starts at command dispatch. The same holds on a bash
without `$EPOCHREALTIME` (bash older than 5), where the shim exports nothing.

## The target env: `winter.env`

`winter.env` holds the name of the one feature env a command targets. A command sets it from its own arguments alone:
the env part of each target argument (the part before any `/`) resolves to exactly one feature env.

| Target arguments                                            | `winter.env`                                |
| ----------------------------------------------------------- | ------------------------------------------- |
| One env name or `<env>/<repo>`, such as `ws status alpha`   | `alpha`                                     |
| A name that does not exist yet, such as `ws init new-env`   | `new-env`: a literal name counts as written |
| A glob that matches exactly one existing env, such as `al*` | the matched env                             |
| Two or more envs, or a glob that matches several            | not set                                     |
| No target arguments                                         | not set                                     |
| The `workspace` scope, such as `service up workspace`       | not set: the scope is never an env          |
| `lint` names that are repos, such as `lint winter`          | not set: only discovered envs count         |

The trailing arguments that are not targets stay out of the resolution: the FEATURE_BRANCH of `ws connect`, the REF of
`ws reset`, and the BASE of `ws restack`. A command that targets one env and also `workspace` resolves to the env.

Only the invoked command's own arguments count. Work a command does for other envs while it runs, such as the service
auto-start inside `provision alpha beta`, never sets or changes `winter.env`. Resolving a glob reads the workspace's
existing envs, and only when tracing is on; the lookup never changes what the command prints or how it exits.

## Git spans

Every git call winter makes through its GitPython adapters opens a `git <operation>` span, named for the git subcommand
the call performs: `git fetch`, `git pull`, `git push`, `git merge`, `git rebase`, `git reset`, `git checkout`,
`git status`, `git diff`, `git config`, `git rev-parse`, `git rev-list`, `git merge-base`, `git branch`, `git clean`,
`git clone`, `git worktree add`, `git worktree remove`, `git stash push`, and so on. The span is nested under the span
that is active when the call starts: the command span, or an inner span above it. It is the active span while the call
runs, and a call that reaches another adapter call nests the second span inside the first. `git pull` also names the
integrate step that `ws pull` runs after its `git fetch`: a merge or rebase onto the fetched upstream, with no fetch of
its own.

One span covers one adapter call, which opens the repository once and runs every git command it needs. A read of a
worktree that is not on disk still opens its span, so a status span does not prove git ran. Each adapter function that
opens a repository declares its operation, so there is no per-verb list to keep: a new git call is spanned by
construction.

| Attribute     | Value                                                                                                |
| ------------- | ---------------------------------------------------------------------------------------------------- |
| `winter.repo` | The name of the repo the call acts for. Every git span carries it.                                   |
| `winter.env`  | The feature env's name, when the call acts in a feature env's worktree; never set for anything else. |

The attributes come from what the call was made for. The env is never read from a path, so a standalone repo configured
at a path that looks like a worktree carries no env.

| Call                                                                                      | `winter.repo` | `winter.env` |
| ----------------------------------------------------------------------------------------- | ------------- | ------------ |
| A read or write on a feature worktree: status, fetch, pull, push, diff, checkout, restack | set           | set          |
| A source checkout's status, fetch and fast-forward                                        | set           | not set      |
| A standalone repo's status, fetch, pull or push                                           | set           | not set      |

The lifecycle calls that act on a path rather than a repo object, from `ws init`, `ws destroy`, `ws prune` and pin
updates, are told the repo's name and the env by their caller. Cloning, creating or removing a worktree, wiring its
tracking, and setting its identity carry both on a feature worktree, and `winter.repo` only on a source checkout or a
standalone. Two calls in `ws prune` are made for no declared repo and are named by a fallback: the check of an orphan
clone under `projects/` names the repo by the clone's directory, and the orphan-standalone scan names it by the managed
block the clone was found in.

A failed call sets error status and `error.type`, the exception class name such as `RepoError`. Git's message and stderr
never reach the span. A probe that answers "no" when git exits non-zero, such as a ref check, is an answer rather than a
failure and leaves the status unset.

Two kinds of git are not spanned. The read that discovers which envs exist (`git worktree list` on a source checkout) is
workspace discovery rather than an operation on a repo. `lint` and `doctor` run git through the subprocess runner rather
than the GitPython adapters. Git processes that GitPython spawns do not receive a `TRACEPARENT`.

## Service spans

`winter service up` and `winter service down` open one span per cell they dispatch, and `winter service up --wait` opens
one for its readiness wait. Both are nested under the span that is active when the work starts, and each is the active
span while its work runs.

| Span                                           | Opens                                                                           | Attributes                                                              |
| ---------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `service provider up`, `service provider down` | Around one provider call for one cell.                                          | `winter.provider`, the provider's extension name; `winter.scope`        |
| `service readiness wait`                       | Around the whole wait: every status poll, and every describe call a poll makes. | `winter.service.patterns`, the pattern count; `winter.ready`, a boolean |

A cell is one (provider, scope) pair for one pattern, so a scope given several patterns gets a span for each. The
implicit `up workspace` that `service up` dispatches first, and the auto-start inside `winter provision`, open their
cells' spans like any other. A provider call that exits non-zero marks its span failed, with error status and no
`error.type`, since an exit code is not an exception. A readiness wait that times out marks its span failed with
`winter.ready` false.

`up` stays detached: the provider's `up` environment holds no `TRACEPARENT`, so the `up` span records the call, not what
the services it launches go on to do. A provider's `down` call, and the status and describe calls of a readiness poll,
do receive the span in `TRACEPARENT`; see
[contracts/service-orchestrator.md](./contracts/service-orchestrator.md#trace-context-traceparent).

## Provision spans

`winter provision` opens one `provision handler <action>` span for each directory a handler runs in, where `<action>` is
`apply`, `destroy`, `reset` or `clean`. A feature-worktree handler therefore gets one span per project worktree. The
span opens before the directory's first command starts, so the handler process receives its own span in `TRACEPARENT`,
and it ends when the directory's last command does.

| Attribute          | Value                                                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `winter.handler`   | The handler label that provision output shows: source, sub-target and scope, such as `project/data[feature-worktree]`. |
| `winter.env`       | The feature env's name, at feature-environment and feature-worktree scope; not set at workspace scope.                 |
| `winter.repo`      | The project repo's name, when the directory is a project worktree; not set otherwise.                                  |
| `winter.exit_code` | The directory's exit code; not set when the command could not be launched.                                             |

A non-zero exit marks the span failed. A command that cannot be launched marks it failed with `error.type` set to the
launch error's class name, such as `FileNotFoundError`. A run that fails before any directory runs, such as an unknown
handler source, opens no span. `winter clean` and `winter ws destroy` run their handlers through the same code, so they
emit `provision handler clean` and `provision handler destroy`. The handler's output and the launch error's message
never reach a span.

## Propagation to child processes

Every child process winter starts through its subprocess runner carries the innermost active span in `TRACEPARENT`, so a
nested `winter` call or an instrumented tool runs under the span that started it: the command span, or an inner span
opened inside it. The value replaces any `TRACEPARENT` the caller passed, and `TRACESTATE` accompanies it: the child
gets the span's own trace state, or none, never the caller's. A thread-pool task carries the span that was active when
its work was submitted, so a child started by a task, such as a service status matrix cell, names that span rather than
the command span. Winter builds every pool so that this holds. Work on a thread outside such a pool, which holds no span
of its own, carries the command span. Winter's own fresh containers use the same tracer, so the dashboard's `ws init`
runs under a session root of its own, the way every other piece of dashboard work does; see
[The dashboard](#the-dashboard).

With tracing off, a child's environment is exactly what winter passed before tracing existed: the caller's `TRACEPARENT`
and `TRACESTATE`, if any, reach it unchanged.

Services that winter launches start with no `TRACEPARENT` and no `TRACESTATE`, tracing on or off. That covers
`winter service up` and `winter service restart`, and the auto-start of required services inside `winter provision`. A
long-running service therefore never attaches spans to a step that has ended. The provider-side rule is in
[contracts/service-orchestrator.md](./contracts/service-orchestrator.md#trace-context-traceparent).

## Export and exit cost

A one-shot command collects every span in memory and sends them once, in a single request, as it exits. The dashboard
alone also exports while it runs; see [The dashboard](#the-dashboard). Nothing bounds the export: every span of the
command, inner spans included, is in that one request, with no count limit and no sampling of survivors. The exporter
timeout is the cap: **100 ms**. For an ordinary-sized command, a slow, hanging, or refused endpoint, and an unreachable
one given as an IP or a locally resolvable name (`localhost`, an `/etc/hosts` entry), never holds a command past that
cap and never changes its exit code or its stderr.

The cap covers the request, not the encoding before it. Encoding 500 spans takes about 6 ms on an idle machine and
several times that on a loaded one, so a command that opens many inner spans, such as a `ws restack` across dozens of
envs, can exit later than the cap. The exit code and stderr are unaffected.

A collector on the same host answers well inside the cap. A remote endpoint delivers only while connection setup plus
one request fit inside it, roughly two round trips for plain HTTP and three to four with TLS. Beyond that the spans are
dropped without a trace. A remote hostname whose DNS lookup is slow can hold a command past the cap, because the
exporter timeout does not cover name resolution.

## The dashboard

`winter dashboard` runs for as long as the user keeps it open, so one trace holding hours of refreshes would never be
sent and would be useless when it was. With tracing on, the dashboard is traced as a series of short traces instead,
exported while it runs.

**The session span.** The `winter dashboard` command span is the session span. It is parented on the caller's
`TRACEPARENT` like any command span, lasts until the app returns, and is exported at exit.

**Session roots.** Each refresh or user action runs under a root span of its own: a new trace, with no parent, that
carries a span link to the session span. A root is named `dashboard <purpose>` and carries `winter.command`, equal to
`winter dashboard`. The git spans, service and provision spans, and child processes that the work starts nest under the
root like they do under a command span.

| Root                           | Covers                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------- |
| `dashboard refresh workspace`  | One refresh of the workspace screen: on open, every 30 s, and on the refresh key.       |
| `dashboard refresh worktree`   | One refresh of a feature environment's detail screen.                                   |
| `dashboard refresh standalone` | One refresh of a standalone repo's detail screen.                                       |
| `dashboard load repo detail`   | The git read behind the repo detail panel when a row is highlighted.                    |
| `dashboard load agent matrix`  | One build of the Agent matrix screen.                                                   |
| `dashboard ws init`            | The `winter ws init` that the Agent matrix's `i` key runs in process, its git included. |
| `dashboard plugin action`      | One plugin-contributed action.                                                          |

A root is recorded only when the session span is: a dashboard started under an unsampled `TRACEPARENT` records no roots,
and the work runs untraced. Work on the UI thread outside any root holds no span of its own and parents on the session
span.

**Background export.** The dashboard starts an exporter that sends the spans ended since the last flush about every 5 s,
as one request per flush, from a daemon thread. A flush that finds nothing new sends nothing, and the session span is
not in any flush: it ends, and is sent, at exit. Ending a span never waits for a flush, so the UI thread never waits on
the collector. Flushes never overlap. A flush that fails, such as one to a hanging or refused endpoint, drops its batch
without a word; the failure surfaces only under `--verbose` or `WINTER_LOG_LEVEL`, like any export failure, and the
dashboard raises no notice for it. Because the thread is a daemon, it never keeps the process alive. One-shot commands
never start it.

**Exit.** Quitting the dashboard stops background export and spends at most the 100 ms cap in total. It waits for a
flush already in flight, for at most the cap, and then sends the final batch, which holds the session span and every
root that ended since the last flush, with the cap that remains as the request's timeout. A flush that is still in
flight when the cap passes means the final batch is dropped, and the session span with it: against a hanging endpoint a
quit costs the cap at most, and the roots exported earlier are already in the collector.

## Content

Spans carry command paths and names only: the command path, the target env's and repo's names, git operation names, the
provider's extension name, the scope, the handler label, counts, exit codes and booleans, and the `error.type` of a
failure. The attribute keys are `winter.command`, `winter.env`, `winter.repo`, `winter.provider`, `winter.scope`,
`winter.service.patterns`, `winter.ready`, `winter.handler`, `winter.exit_code` and `error.type`. Their resource carries
`service.name`, the attributes the caller supplied in `OTEL_RESOURCE_ATTRIBUTES`, and the SDK's own `telemetry.sdk.*`
attributes and per-process `service.instance.id`. The raw argv, environment variable values, `config.local.toml` values,
and command output stay out of every span, whether git's, a provider's or a provision handler's. Exception messages are
never recorded, because they can embed that output.

## Diagnostics

Failed exports are silent by default; [root-flags.md](./root-flags.md) owns how `--verbose` and `WINTER_LOG_LEVEL`
surface them.
