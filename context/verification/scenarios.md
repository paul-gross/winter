# Recorded scenarios

A recorded scenario pins one behaviour the reference tool has: a fixture, the steps run in it, and what the reference
did, stored as a record. Replay builds the same fixture, runs the same steps with the binary, and compares. This
document is how to add, record, replay, and debug one. The commands that replay, their filters, and what fails the
`scenarios` step are in [development.md](../tooling/development.md#the-gates); the isolated tree every scenario runs in
is in [testing.md](../standards/testing.md#the-isolated-tree).

## The rule

Nothing in this repository starts the reference tool on its own: no gate, no test, and no replay does. `cargo dev
record` is the only code handed its path, by the contributor who runs it, and it starts the reference only with its
working directory inside a temporary fixture tree. The reference is an executable path and nothing more to this
repository.

## One scenario on disk

```text
scenarios/
  reference.toml             commit = "<40 hex>": the commit of the reference checkout every record is made at
  <command>/                 the command path joined by "-": ws-index/, repo-remove/
    <name>/                  the scenario id is <command>/<name>; both names match [a-z0-9][a-z0-9-]*
      scenario.toml          the description: fixture, steps, comparison rules
      files/                 optional; copied into the workspace directory of the tree, <ROOT>/ws
      dirty/                 optional; copied into <ROOT>/ws after every git step of the fixture
      record/                written by the record command only
        meta.toml            what the record was made with
        steps/<nn>/          01, 02, ... in step order
          exit-code  stdout  stderr
          stdout.json        only for a step whose stdout mode stores JSON
          before/  after/    state snapshots: tree.txt, git.txt, files/
      expected/              optional; written by hand, together with [deviation]
        steps/<nn>/...
```

The scenarios root holds `reference.toml` and command directories alone, a command directory holds scenario directories
alone and at least one, and anything else is malformed: a stray file beside the scenario directories, an empty command
directory, a name outside the pattern.

`<ROOT>` stands for the root of the tree in everything stored, so a record does not depend on where it was made. The
workspace's `.winter/config.toml` and every other workspace file come from `files/`. A file there is copied byte for
byte and may hold bytes that are not UTF-8; such a file is written with a byte tool, as `printf '\377\376\n'` writes the
one of `ws-index/config-not-utf8`.

### scenario.toml

A key the format does not define is an error at every level. The smallest description is a summary and one step:

```toml
summary = "one sentence: the behaviour this scenario pins"

[[step]]
args = ["ws", "index", "two", "--json"]
stdout = "json"
```

| Table                       | Keys                                                                                |
| --------------------------- | ----------------------------------------------------------------------------------- |
| top level                   | `summary`, `needs`, `[fixture]`, `[[step]]`, `[compare]`, `[[stub]]`, `[deviation]` |
| `[fixture]`                 | `workspace_git`, `gitignore`, and the four lists below                              |
| `[[fixture.remote]]`        | `name`, `default_branch` (default `main`), `[[fixture.remote.commit]]`              |
| `[[fixture.remote.commit]]` | `message`, `files`, `delete`, `branch`, `tag`                                       |
| `[[fixture.checkout]]`      | `remote`, `path`                                                                    |
| `[[fixture.worktree]]`      | `checkout`, `path`, `branch`, `start`                                               |
| `[[fixture.git]]`           | `cwd`, `args`: a raw git call, after the entries above                              |
| `[[step]]`                  | `args`, `cwd` (default `.`), `stdin`, `stdout`, `env`                               |
| `[compare]`                 | `message_text`, `git_config_ignore`, `[[compare.fan_out]]`, `[[compare.file]]`      |
| `[[compare.fan_out]]`       | `types`, `key`                                                                      |
| `[[compare.file]]`          | `path`, `mode` (`bytes`, `toml`, `presence`, `ignore`), `reason`                    |
| `[[stub]]`                  | `name`, `exit`, `stdout`, `env`                                                     |
| `[deviation]`               | `decision`                                                                          |

What to know when writing one:

- The fixture is built in this order: remotes, `files/`, checkouts, worktrees, the commit of the workspace root when
  `workspace_git` is true, the raw git calls, `dirty/`. A remote is a bare repository at `<ROOT>/remotes/<name>.git`.
- A step's `stdout` is `text` (the default), `json`, or `json-lines`. Only the last two are compared.
- Every path is relative and stays inside the tree: `cwd` and the fixture paths under `<ROOT>/ws`, `stdin` a regular
  file of the scenario directory. No step argument and no `env` value starts with `/`; a value names a path through
  `<ROOT>`. A step adds variables and replaces none of the pinned environment. A description that breaks one of these is
  malformed, and no step of it is started. Most are found by reading the description, before anything runs. A `cwd` that
  leaves the tree through a link, and an argument that leads out of the tree from that directory, are found on the built
  fixture: after the fixture's git calls and, at record, after the reference was asked for its version.
- A `[[stub]]` is read by the format, and the runner captures no program calls: a scenario that has one is refused at
  record and fails at replay.
- `message_text` lists JSON pointers, in which `*` stands for any one key or index. A string at such a pointer still has
  to be a string, and its text is not compared: name there a message inside JSON output that the binary words
  differently.
- A `message_text` pointer, a fan-out, or a file rule that does not read, or that relaxes nothing in the scenario, fails
  the comparison, and the record command refuses the scenario for it. Such a rule is never dropped in silence, so remove
  one that no longer applies. An explicit byte rule is a strictness override, as described below.
  `git_config_ignore` is the exception: a key that does not read fails likewise, and a key
  that no repository of the scenario has is not reported.

### File comparison

After a step, an unmatched file whose name ends in `.toml` compares as parsed TOML; every other file compares by
bytes. TOML comparison ignores formatting, comments, and table key order, but preserves values and array order. If
either file is not valid TOML, the comparison falls back to bytes.

`[[compare.file]]` overrides the default. Its `path` is a glob over ROOT-relative paths: `*` matches within a segment,
and `**` across segments. The first matching rule wins. Use `mode = "bytes"` when the behaviour pins exact text, and
give its reason; an explicit byte rule is a strictness override, not an unused relaxation. A rule that matches no file
still fails the comparison.

The initial fixture comparison stays byte-strict, including TOML files, and does not apply file rules. Semantic
comparison after a step must not hide a changed starting fixture.

## Add and record a scenario

1. Write `scenarios/<command>/<name>/scenario.toml`, and `files/` with the workspace it starts from.
2. Record it with the reference tool:

   ```bash
   cargo dev record ws-index/alias-suggested --reference <path to the reference executable>
   ```

3. Read `record/`: the stored exit code, JSON, and state are what the binary must now equal. Read `stderr` and a text
   `stdout` as well; they are stored to be read, not compared.
4. Replay it, and commit the scenario directory with its record:

   ```bash
   cargo scenarios ws-index/alias-suggested
   ```

The reference is named by `--reference` or by the variable `WINTER_REFERENCE_BIN`; with neither the command exits 2.
`--keep` leaves both fixture trees on disk and prints their paths. To record every scenario that has no record:

```bash
cargo dev record --all-missing --reference <path to the reference executable>
```

`--all-missing` takes no scenario id and combines with `--reference` alone.

The command reports one line per scenario on stderr: `recorded <id>`, `refused <id>: <reason>`, or `skipped <id>: it
needs <facility>, which the runner lacks`. A run that is refused as a whole reports `record: refused: <reason>` and no
scenario. It exits 0 when every scenario asked for was recorded or skipped and the reference checkout is unchanged, 1
when the run or a scenario was refused, a record was unstable, or the checkout changed, and 2 for arguments that do not
read, a scenario id named twice among them.

### What is refused

The whole run is refused, before the reference is started, when:

- the reference path is relative, missing, or no executable file;
- it is the first `winter` on `PATH`, compared with every link resolved: the installed command is never run;
- no directory above the executable holds `.git`, or the path of that checkout has a character outside
  `[A-Za-z0-9/._-]`;
- the checkout's `HEAD` is not the commit `scenarios/reference.toml` names, or a tracked file of it is modified;
- `scenarios/reference.toml` does not read, no directory of `PATH` holds an executable git, or a git call on the
  checkout fails.

The reference is then asked for its version, once. When that call fails or prints no line, the run is reported as
refused in the same way and records nothing, though the reference was started.

One scenario is refused, and the others of the run are still recorded, when it is malformed, has a `[[stub]]`, has
`record/` while `--rerecord` was not given, or has a `record-*` entry in its directory. A run in which every scenario is
refused or skipped does not start the reference.

A record is made twice, in two trees with different roots, and the two runs are compared as a replay compares. A
difference refuses the record as unstable and nothing is written: the behaviour depends on something the fixture does
not fix.

A written record is checked against this repository: a file an ignore rule matches, or whose path git would not take,
fails the scenario and the record is removed.

### The reference checkout is read, never written

The record command reads the reference's checkout with git before the reference is first started and again after the
last scenario. When `HEAD` moved, a tracked file became modified, or an untracked or ignored file appeared or
disappeared, the run prints `reference checkout changed` and the paths, and exits 1. Do not keep the records of such a
run. A change to the content of a file that was already untracked is not seen.

### Record again

Record again when the description, `files/`, `dirty/`, a stdin file, the pinned environment, or the pinned reference
commit changed: replay fails such a scenario as stale until its record is made again.

```bash
cargo dev record ws-index/alias-suggested --rerecord --reference <path to the reference executable>
```

Review the diff of `record/` like any other change.

While the new record is made, the previous one is set aside as `record-<suffix>/record` in the scenario directory. It is
removed once the new record stands, and moved back when none is written or the new one is refused.

A run that was killed leaves `record-<suffix>` directories in the scenario directory. While one is there, the next run
that names the scenario, and the next for every missing record, refuses the scenario, names the first of them, and
removes nothing; replay and `--list` pass over them. What a directory holds says what it is:

| `record-<suffix>` holds  | It is                                                             |
| ------------------------ | ----------------------------------------------------------------- |
| one entry, `record/`     | The previous record, set aside whole.                             |
| `meta.toml` and `steps/` | A new record the run was writing, which is not known to be whole. |
| nothing                  | A directory the write made for itself.                            |

To resolve it:

1. When a directory holds the previous record and `record/` is absent, the run was killed while the steps ran: move
   `record-<suffix>/record` back to `record/`.
2. When a directory holds the previous record and `record/` is there too, the run was killed after the new record was
   moved into place and before it was checked against this repository. Keep the previous one: delete `record/` and move
   `record-<suffix>/record` back to it.
3. When no directory holds a previous record, the scenario had none. A `record/` that is there was not checked against
   this repository either: delete it.
4. Delete every `record-<suffix>` directory, and run the command again.

## Replay

`cargo scenarios` replays every scenario, and `cargo dev gate full` runs it as its last step.

```bash
cargo scenarios
cargo scenarios repo-remove
cargo scenarios --list
cargo dev gate full
```

`--list` prints each id with one of four states and replays nothing:

| State        | Meaning                                                                                         |
| ------------ | ----------------------------------------------------------------------------------------------- |
| `recorded`   | The description reads and `record/` is there. It does not say the record is current or whole.   |
| `unrecorded` | The description reads and there is no `record/`. Replay fails it.                               |
| `deferred`   | It needs a facility the runner lacks, with or without a record. Replay does not run or fail it. |
| `malformed`  | The description or the entry breaks the format. Replay fails it.                                |

Whether a `recorded` scenario replays is only known by replaying it: a stale record, and one that lost or gained a file,
are found there.

## Read a replay failure

A failed scenario prints `FAILED <id>`, then one block per difference: the id, the dimension, the step or `no step`, and
the subject, with the recorded and the observed value, as a unified diff where a value has more than one line. Under the
blocks stand the first lines of what each such step wrote to stdout and stderr.

| Dimension     | What differs                                                                                                    | Where to look                                                                                                  |
| ------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `Exit`        | The exit code, or the signal, of a step.                                                                        | `record/steps/<nn>/exit-code`, and the step's stderr under the blocks.                                         |
| `Json`        | The JSON a `json` or `json-lines` step printed; the subject is a pointer.                                       | `record/steps/<nn>/stdout.json`.                                                                               |
| `Tree`        | The entries under the root after a step: a path added, missing, or changed.                                     | `record/steps/<nn>/after/tree.txt`.                                                                            |
| `Files`       | The content of a file after a step; the subject is its path. Semantic TOML differences may appear only here, not in `Tree`. | `record/steps/<nn>/after/files/`.                                                                              |
| `Git`         | A block of a repository after a step: refs, status, config, log, and so on.                                     | `record/steps/<nn>/after/git.txt`.                                                                             |
| `Fixture`     | The freshly built fixture is not the one the record started from.                                               | The binary was not run. The fixture builder or git changed: record again.                                      |
| `StaleRecord` | `reference_commit`, `environment`, or `description` of `record/meta.toml`, or the file does not read.           | The binary was not run. Record again.                                                                          |
| `Malformed`   | The scenario itself; the subject is the key or the name at fault.                                               | `scenario.toml`, or the entry named. An unrecorded scenario and a record that lost or gained a file land here. |

`Calls` is a dimension of the format for the calls of stubbed programs, which the runner does not capture.

Two lost files of a record land elsewhere: a lost `meta.toml` is `StaleRecord`, and a lost `stdout.json` is `Json` with
the recorded value `absent`.

What replay compares, and what it does not:

- Compared: the exit code, the parsed JSON, the listing of the tree, the files under the
  [file comparison rules](#file-comparison), and the git state.
- Never compared: stderr, and stdout as text. That covers all of a `text` step's stdout, and the white space and key
  order of a JSON one. A message on stderr or in a `text` stdout that the binary words differently does not fail replay.
  A string inside compared JSON does, unless a `message_text` pointer names it.
- The listing stores of a file's permissions only whether an execute bit is set. A permission change that keeps that is
  not seen.
- A failing run with `--json` that prints nothing to stdout stores an empty JSON stream: `repo-remove/not-found` records
  exit code 1, an empty `stdout.json`, and its message on stderr. A binary that prints a JSON document there differs in
  `Json`.

## Keep the tree

`WINTER_TEST_KEEP=1` on a replay keeps the tree of each failed scenario that got one and prints its path under the
scenario's blocks. A scenario that fails before its fixture is built has no tree to keep: a malformed or unrecorded one,
one whose record is stale, one whose expectation does not read. The tree of a scenario that passed is removed all the
same. At record, `--keep` keeps both trees of each scenario it ran, recorded or not, and prints their paths. Inside a
kept tree, `ws/` is the workspace the steps ran in and `remotes/` holds the fixture's remotes. Remove a kept tree when
done.

## Register a deviation

A deviation is a behaviour in which the binary is decided to differ from the reference. The scenario keeps the
reference's record and adds, by hand, the expectation that replaces part of it. `repo-remove/removes-middle-block` is
one: its `expected/steps/01/after/` holds the `tree.txt` and the `files/ws/.winter/config.toml` the decision asks for,
and its `scenario.toml` names the decision under `[deviation]`.

The order matters:

1. Record the scenario, so `record/` holds what the reference does.
2. Copy the affected files of `record/steps/<nn>/` to `expected/steps/<nn>/` and edit them to the decided behaviour. Add
   `[deviation]` with `decision = "<the id of the decision>"` to `scenario.toml`.
3. Record again with `--rerecord`.

The last step is needed because `record/meta.toml` stores a digest of the description, and `[deviation]` changed
`scenario.toml`: without it the record is stale. Recording never reads or writes `expected/`.

Rules of an expectation:

- `expected/` without `[deviation]`, and `[deviation]` without `expected/`, are each malformed. Add both in one edit.
- It may hold `exit-code`, `stdout`, `stderr`, `stdout.json`, `after/tree.txt`, `after/git.txt`, and
  `after/files/<path>` of a step the record has, and nothing else. `before/` is never replaced.
- `after/tree.txt` and the files under `after/files/` go together: a changed file needs its new hash on its line of
  `tree.txt`, and a file under `expected/` that the listing does not imply fails the read.
- An expected `stdout` of a JSON step changes nothing alone: the comparison reads `stdout.json`, so the expectation
  states that file, with or without `stdout` beside it.
- A deviation that lasts into later steps is restated at each of them.

## Mark a facility

A scenario whose behaviour needs more than a process run to its end in a fixture tree says so with `needs`:

| Facility      | The behaviour needs                                         |
| ------------- | ----------------------------------------------------------- |
| `pty`         | a terminal on a standard stream: prompts, colour, progress. |
| `signal`      | a signal delivered to the process during the step.          |
| `clock`       | a controlled time: timeouts, backoff, compared timestamps.  |
| `daemon`      | a process that outlives the step.                           |
| `follow`      | output observed while the process is still running.         |
| `provider`    | a live service provider beyond a recording stub.            |
| `network`     | a transport other than the fixture remotes.                 |
| `concurrency` | two invocations overlapping in time.                        |

The runner supports none of them yet, so every scenario that names one is deferred: replay lists it as `deferred <id>
needs <facility>`, counts it, does not run it, and does not fail; the record command skips it. A name outside the table
is malformed. Mark the facility rather than leaving the scenario out: the deferred count is the record of what is not
yet verified.
