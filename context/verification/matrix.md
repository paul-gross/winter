# Verifiability matrix

The verification methods available for this repository: the concrete ways a change to it is shown correct. Its shape is
owned by `canon:verifiability-matrix`, and its scope is `workspace`.

An id is stable. A row is added, never renamed or reused, and a row exists only when its command exists and exits 0 on a
clean checkout.

## Commands

Run from the repository root, after the bootstrap of [development.md](../tooling/development.md#bootstrap).

| Method                      | Command                                          | Proves                                                                          | Use when                                                   |
| --------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `workspace:unit-test`       | `cargo test -p winter-workspace-core --lib`      | Pure rules and the file port's isolated filesystem tests. | The rule has no external input, or exercises the documented file-port exception. |
| `workspace:component-test`  | `cargo test -p winter-workspace-core --test '*'` | Library behaviour against fixture workspaces and direct program-adapter behaviour under pinned environments. | The behaviour reads or writes files or git state, or exercises the program adapter. |
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

### `workspace:manual-init`

Exercises workspace creation through the built binary.

**Setup.** Build with `cargo build --workspace --locked`. Use a fresh temporary directory outside any real workspace
under `$BLIZZARD_TMPDIR`; verify that no ancestor holds `.winter/config.toml`. Create an empty `workspace` directory. From it run the absolute
path to `target/debug/winter-workspace init --json` with `OTEL_SDK_DISABLED=true`: expect exit 0, the created statuses,
and only `.winter/config.toml`, `AGENTS.md`, and `CLAUDE.md`. Read the config and the agent files; the config pins the
binary's version, the agent file contains the safety block, and the shim imports it. Run init again: expect exit 1,
no stdout, an already-initialized diagnostic, and all three files unchanged. Run `context init` and check its guidance.

**Success.** Both exits, streams, files, and the guidance match these observations.

### `workspace:manual-registration`

Exercises command discovery: that the build picks up a new command file, and that the build script itself stops the
build, and the same for the topic sources. Use it when a change touches `crates/core/build.rs`, `crates/core/build/discover.rs`,
`crates/core/build/topics.rs`, `crates/core/topics/`,
`crates/core/src/registry.rs`, or `crates/core/src/parse.rs`.

**Setup.** Make a scratch copy of the checkout under a temporary directory, where no directory at or above the copy
holds a `.winter/config.toml`, as the built-binary rule of [AGENTS.md](../../AGENTS.md#standing-rules) has it, and work
only there. Run the cases in order.

1. Add `crates/core/src/commands/ws/probe.rs`. Its `REGISTRATION` is a `Registration::Command` whose `args` returns the
   node it is given and whose `run` returns `Termination::Success`. Then:
   - `cargo build --workspace --locked` exits 0.
   - `target/debug/winter-workspace ws probe` exits 0.
   - `target/debug/winter-workspace --help` lists `ws`.
   - `cargo clippy --locked --workspace --all-targets --all-features -- -D warnings` exits 0.
2. Rename that file to `crates/core/src/commands/ws/Bad.rs`. Then `cargo build --workspace --locked` exits non-zero, and
   the first line of the build script's error output starts with `crates/core/src/commands/ws/Bad.rs`.
3. Add `crates/core/topics/probe/x.md` with front matter `id: probe/x` and any one-line title, and a
   `` `winter-workspace context probe/x` `` span in `crates/core/topics/root.md`. Then `cargo build --workspace --locked`
   exits 0, and the generated `target/debug/build/winter-workspace-core-*/out/topics.rs` holds `probe/x` and its title.
   Change only that title and build again: the generated file holds the new title.
4. Restore the topic sources, then make the body of `crates/core/topics/safety.md` 16 lines. Then
   `cargo build --workspace --locked` exits non-zero, and the first line of the build script's error output starts with
   `crates/core/topics/safety.md`.

**Success.** Each run ends as stated, and `git status` of the scratch copy shows only the files that case added or changed, and nothing after cases 3 and 4 restore them.

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

### `workspace:manual-repo-add`

Exercises `repo add` through the built binary. Use it when changing addition's checks, block layout, or command wiring.
Component and scenario tests cover the same contract automatically.

**Setup.** Build with `cargo build --workspace --locked`, then set `binary` to the absolute path of the resulting
`target/debug/winter-workspace`. Create a fresh temporary directory outside any real workspace; verify that no ancestor
holds `.winter/config.toml`. Work only in a new `workspace/` beneath it, with a `.winter/config.toml` made for this run.
Keep evidence in a sibling `evidence/` directory. Set `OTEL_SDK_DISABLED=true` for every invocation.

1. **Project add.** The shared file holds a comment line and one `[[project_repository]]`. Save a `find` listing of the
   workspace. Run `"$binary" repo add https://fixture.invalid/web.git --json`. Expect exit 0, the document
   `{"added": true, "kind": "project", "url": ...}` on stdout, and empty stderr. The file gains one block and keeps the
   comment; a second `find` listing differs only in that file: no `projects/` entry, no lock or state file.
2. **Local standalone.** Run
   `repo add git@fixture.invalid:tools/x.git --standalone --path tools/x --git-exclude a --git-exclude b --local`. Expect
   exit 0 and a created `.winter/config.local.toml`; `cmp` shows the shared file unchanged. `repo list --json` then
   lists both entries.
3. **Refusals preserve bytes.** Run a duplicate URL, `--standalone --pinned`, and, in a fresh shared file holding an
   inline `project_repository = [{ ... }]`, a plain add. Also run `--standalone --path via`, where `via` is a symbolic
   link (`ln -s tools/x workspace/via`) to the directory of the standalone step 2 declared. Each exits 1 with zero stdout bytes, and `cmp` shows every
   configuration file unchanged.

**Success.** The exits and streams are as stated, the before/after comparisons are kept as evidence, and nothing runs
against a real workspace.

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

### `workspace:manual-ws-fingerprint`

Exercises `ws fingerprint` through the built binary. Use it when changing the digest, the scratch index, the git
operations it runs, or the command wiring. Component tests cover the same contract automatically; this exercise checks
the built binary against disposable repositories.

**Setup.** Build with `cargo build --workspace --locked`, then set `binary` to the absolute path of the resulting
`target/debug/winter-workspace`. Create a fresh temporary directory outside any real workspace; verify that no ancestor
holds `.winter/config.toml`. Work only in a new `workspace/` beneath it, with a `.winter/config.toml` made for this
run: `workspace/` is a git repository whose committed file declares one `[[standalone_repository]]` named `tools`, and
`workspace/tools` is a clone of a local bare repository made in the temporary directory. Keep evidence in a sibling
`evidence/` directory, and set `TMPDIR` to an empty `evidence/tmp/`. Set `OTEL_SDK_DISABLED=true` for every invocation.
Use no real remote and no existing checkout.

1. **Digest line.** Run `"$binary" ws fingerprint`. Expect exit 0, one line of 64 lowercase hex characters, and empty
   stderr.
2. **JSON.** Run it with `--json`. Expect `digest` equal to that line, and `workspace` and `standalones` entries holding
   `name`, `commit`, `tree`, and `dirty`. Recompute the digest from the trees of the
   document: write `{"standalones":[{"name":..,"tree":..}],"version":1,"workspace":<tree>}` with keys sorted and no
   whitespace (`jq -S -c` does), and take its `sha256sum`; it must give the same value.
3. **Read-only.** Save `sha256sum` and `stat -c %y` of `.git/index`, and `git show-ref`, of both repositories. Run the
   command again and compare: all are unchanged, and `evidence/tmp/` is empty.
4. **Tracked content only.** An untracked file in the root leaves the digest unchanged. A staged edit to a tracked file
   changes it, and `dirty` becomes true.
5. **Missing checkout.** Move the `tools` checkout away. Expect exit 1, zero stdout bytes, and a stderr that holds
   `cannot fingerprint tools: <path> does not exist`.

**Success.** The exits and streams are as stated, and the before/after comparisons are kept as evidence. No command in
this exercise runs against the real workspace.

### `workspace:manual-ws-update`

Exercises `ws update` through the built binary. Use it when changing the re-pin flow, the freeze flow, the selection of standalones, the
rows or events the command writes, or the command wiring. Component tests cover the same contract automatically; this
exercise checks the built binary against disposable repositories.

**Setup.** Build with `cargo build --workspace --locked`, then set `binary` to the absolute path of the resulting
`target/debug/winter-workspace`. Create a fresh temporary directory outside any real workspace; verify that no ancestor
holds `.winter/config.toml`. Work only in a new `workspace/` beneath it, with a `.winter/config.toml` made for this run:
it declares one `[[standalone_repository]]` with `name = "lib"` and `ref = "v1"`, and `workspace/lib` is a clone of a
local bare repository made in the temporary directory, whose history has two commits, tagged `v1` and `v2`. Keep
evidence in a sibling `evidence/` directory. Set `OTEL_SDK_DISABLED=true` for every invocation. Use no real remote and
no existing checkout.

1. **Re-pin.** Run `"$binary" ws update`. Expect exit 0 and, on stdout, `→ pulling`, `[standalone/lib] re-pinned →`
   followed by 8 hex characters, a blank line, and `✓ pull complete`. `.winter/config.lock` now holds an entry for
   `lib`, and `HEAD` of the clone is `v1`.
2. **Up to date.** Save a copy of the lock and run the command again. Expect `[standalone/lib] up to date`, and `cmp`
   shows the bytes of the lock unchanged.
3. **JSON.** Change the ref to `v2` and run with `--json`. Each line of stdout parses with `jq`. Expect `pull_started`,
   then `repo_synced` whose `result` is `re_pinned`, then `pull_completed` whose `success` is true. `HEAD` is `v2`.
4. **Dirty checkout.** Change the ref back to `v1` and modify a tracked file that the two tags hold alike. Without
   `--autostash`: exit 1, stderr holds `[standalone/lib] pin error: refusing to re-pin 'lib'`, and `HEAD` is unchanged.
   With `--autostash`: exit 0 and `re-pinned`, `git diff` still shows the modification, and `git stash list` is empty.
5. **Refused names.** Run `ws update nope`, then `ws update a/b`. Each exits 1 with zero stdout bytes, and stderr names
   the pattern.
6. **Nothing else changed.** Save `git show-ref` of the bare repository before the first step and after step 5: it is
   unchanged, and nothing outside `workspace/` changed.

The freeze steps use a second `[[standalone_repository]]` named `tools`, with no `ref` and a comment line on the block
after it, cloned from its own local bare repository. Save a copy of `config.toml` first.

7. **Freeze.** Run `"$binary" ws update --freeze`. Expect exit 0, `[standalone/lib] already pinned @ v1`,
   `[standalone/tools] pinned →` followed by 8 hex characters, and `✓ pull complete`. `diff` against the saved copy
   shows exactly one added line, `ref = "<40 hex>"`, after the last key of `tools`, with the comment still below it. The
   lock entry for `tools` has `kind = "commit"`, and `HEAD` and `git show-ref` of the `tools` clone are unchanged.
8. **Frozen again.** Run it again. Expect `already pinned @ <40 hex>` for `tools`, and `cmp` shows both files unchanged.
9. **Dirty freeze.** Remove the `ref` line of `tools` and modify a tracked file in `tools`. `--freeze` exits 1 and
   stderr holds `[standalone/tools] refused:` and `uncommitted changes`. `--freeze --force` exits 0 with `pinned`.
10. **Freeze JSON.** Run `--freeze --json`. Each line parses with `jq`, and each `repo_synced` has a `result` of
    `pinned`, `already_pinned`, or `refused`.
11. **Usage errors.** `ws update --force` and `ws update --freeze --autostash` each exit 2, stderr gives the rule's
    wording, and nothing changed.
12. **Remotes unchanged.** `git show-ref` of both bare remotes is unchanged from before the first step.

**Success.** The exits and streams are as stated, and the before/after comparisons are kept as evidence. No command in
this exercise runs against the real workspace.

### `workspace:manual-ws-init`

Exercises bare workspace reconciliation, concurrent invocations, and recovery after interruption. Use it when changing
bootstrap orchestration, `ws init`, or its served guidance. This is a Linux/Bash exercise using `setsid`, `ps`, and
`jq` for manual supervision and JSON inspection. No service is needed. Never install the binary, run it in the development workspace, or use
real remotes. Automated coverage remains in the unit, component, and executable tiers; this method is not new
recorded-reference parity.

**Setup.** Create a fresh temporary directory outside any real workspace; verify that no ancestor holds
`.winter/config.toml`. Work only in a new `workspace/` beneath it, with a `.winter/config.toml` made for this run;
the fixture helper gives each case its own such directory. After the repository bootstrap, run the following from
the checkout. In a managed feature
environment, complete its provision step first. Keep this shell for the exercise. Do not enable `set -e`: expected
failures must have their exit codes captured.

```bash
cargo build --workspace --locked || exit 1
binary=$(realpath target/debug/winter-workspace)
git_bin=$(command -v git)
base=$(realpath "${BLIZZARD_TMPDIR:?set the lease scratch directory}")
p=$base
while :; do
  if test -e "$p/.git" || test -e "$p/.winter/config.toml" ||
     { test -f "$p/HEAD" && test -d "$p/objects" && test -d "$p/refs"; }; then
    printf 'Unsafe scratch ancestor: %s\n' "$p" >&2; exit 1
  fi
  test "$p" = / && break
  p=$(dirname "$p")
done
tree=$(mktemp -d "$base/manual-ws-init.XXXXXX")
mkdir -p "$tree"/{bin,home/.config,cache,tmp,git-template,remotes,evidence}
evidence=$tree/evidence
printf '[url "%s/remotes/"]\n\tinsteadOf = https://fixture.invalid/\n' "$tree" >"$tree/gitconfig"
pin=(env -i
  "PATH=$tree/bin:$(dirname "$git_bin"):/usr/bin:/bin"
  "HOME=$tree/home" "XDG_CONFIG_HOME=$tree/home/.config" "XDG_CACHE_HOME=$tree/cache"
  "TMPDIR=$tree/tmp" "GIT_CONFIG_GLOBAL=$tree/gitconfig" GIT_CONFIG_NOSYSTEM=1
  "GIT_TEMPLATE_DIR=$tree/git-template" "GIT_CEILING_DIRECTORIES=$base" GIT_TERMINAL_PROMPT=0
  "GIT_AUTHOR_NAME=Fixture Author" GIT_AUTHOR_EMAIL=author@fixture.invalid
  "GIT_COMMITTER_NAME=Fixture Committer" GIT_COMMITTER_EMAIL=committer@fixture.invalid
  GIT_AUTHOR_DATE=2001-01-01T00:00:00+0000 GIT_COMMITTER_DATE=2001-01-01T00:00:00+0000
  TZ=UTC LC_ALL=C.UTF-8 LANG=C.UTF-8 NO_COLOR=1 TERM=dumb COLUMNS=80 LINES=24 OTEL_SDK_DISABLED=true)
g() { "${pin[@]}" "$git_bin" "$@"; }
for name in api healthy tools; do
  g init -b main "$tree/seed-$name"
  printf 'fixture\n' >"$tree/seed-$name/README"
  test "$name" != healthy || printf '# fixture extension\n' >"$tree/seed-$name/winter-ext.toml"
  g -C "$tree/seed-$name" add .
  g -C "$tree/seed-$name" commit -m seed
  g -C "$tree/seed-$name" tag v1
  g clone --bare "$tree/seed-$name" "$tree/remotes/$name.git"
  g -C "$tree/remotes/$name.git" show-ref >"$evidence/remote-$name.before"
done
fixture() {
  ws=$tree/$1/workspace
  mkdir -p "$ws/.winter"
  cat >"$ws/.winter/config.toml" <<'TOML'
prefix = "probe"
[[project_repository]]
name = "api"
url = "https://fixture.invalid/api.git"
cmd = ["printf 'api-ready\\n'"]
[[standalone_repository]]
name = "tools"
url = "https://fixture.invalid/tools.git"
path = "tools"
TOML
  if test "${2:-git}" = git; then
    g init -b main "$ws"
    mkdir -p "$ws/.git/info"
    g -C "$ws" add .winter/config.toml
    g -C "$ws" commit -m fixture
  fi
}
run() {
  local label=$1; shift
  (cd "$ws" && "${pin[@]}" "$binary" "$@") \
    >"$evidence/$label.stdout" 2>"$evidence/$label.stderr"
  printf '%s\n' "$?" >"$evidence/$label.exit"
}
snapshot() {
  (cd "$ws" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum)
}
```

The `pin` array reproduces `crates/test-support/src/env.rs`'s whole environment, not additions to the caller's
environment. All Git and binary invocations below use it. Fixture URLs are rewritten to the seeded local bare remotes;
do not substitute network URLs. Check every setup command succeeded before proceeding.

**First run and repeat.**

```bash
fixture first
run first-json ws init --json
cp "$ws/.git/info/exclude" "$evidence/exclude.before"
stat -c '%i %s %y %z' "$ws/.git/info/exclude" >"$evidence/exclude.stat.before"
run repeat-text ws init
cmp "$evidence/exclude.before" "$ws/.git/info/exclude"
stat -c '%i %s %y %z' "$ws/.git/info/exclude" >"$evidence/exclude.stat.after"
cmp "$evidence/exclude.stat.before" "$evidence/exclude.stat.after"
g -C "$ws/projects/api" rev-parse HEAD >"$evidence/api.head"
g -C "$ws/tools" rev-parse HEAD >"$evidence/tools.head"
g -C "$ws" status --porcelain=v1 --untracked-files=all >"$evidence/first.status"
```

Both runs exit 0; status is empty. Compare the HEADs to the corresponding seeded `v1` commits. The first run clones
both repositories. The repeat clones nothing, reruns `api-ready`, and neither replaces nor writes the exclude file.
Inspect its exact block markers (`# >>> NAME (managed by winter)` / `# <<< NAME`): one each for
`winter-dir/projects` (`/projects/`), `winter-workspace/artifacts` (`/.winter/logs/`), `winter-config`
(`.winter/config/**/*.local.*`), `tools` (`/tools/`), and `winter-workspace/workspace-skills`. The last holds
six paths: under each of `.claude` and `.codex`, the `skills` directory holds patterns `probe-*` and `probe`;
under `.opencode`, the `skill` directory holds the same two patterns. It has no success event.

Parse JSON stdout one line at a time (not as one document):

```bash
while IFS= read -r line; do
  printf '%s\n' "$line" | jq -e '
    {target_started: ["target"], target_completed: ["target","success"],
     repo_action: ["repo","location","action","detail"], repo_error: ["repo","error"],
     cmd_started: ["repo","command"], cmd_output_line: ["repo","line"],
     cmd_completed: ["repo","command","returncode"]} as $fields |
    (keys | sort) == (($fields[.type] + ["type"]) | sort)' || exit 1
done <"$evidence/first-json.stdout"
jq -s -e '
  ([.[] | select(.type == "target_started") | .target] == ["projects/","standalone/"]) and
  all(.[] | select(.type == "target_completed"); .success)
' "$evidence/first-json.stdout"
```

Projects complete before standalones start; repository-local order is clone/exists, excludes if changed, commands,
then pin if applicable. Cross-repository interleaving is permitted. JSON has no human rows and no stderr on success.
Human stdout has target/action/command/output rows; successful command completion adds no row.

**Failure, ownership, and modes.** Use `fixture NAME` for each independent case below, so faults do not contaminate
later cases. Edit only that scratch fixture. Capture each invocation with `run NAME ws init --json`, then repeat
failures without `--json` to check stderr routing. Save changed files before and after with `cp`, `snapshot`, and
`stat` as above.

| Case and exact fixture change | Required observation |
| --- | --- |
| Failure continuation: replace `printf 'api-ready\\n'` in the config with `exit 7`; append a `[[project_repository]]` named `healthy`, URL `https://fixture.invalid/healthy.git`, and `cmd = ["printf 'healthy-ready\\n'"]`. Write `AGENTS.md` containing `<!-- >>> winter-workspace/safety (managed by winter) -->`, `old`, `<!-- <<< winter-workspace/safety -->`, and `user tail`, each on its own line. | Exit 1; `api` reports command returncode 7 and failed/unattempted step accounting; projects completes false. Healthy and tools still clone, healthy prints its marker, finalization writes the eligible blocks, safety refresh reports `agents_md_updated`, and `user tail` remains. Human error, failed-target, and nonzero command-completion rows are on stderr. Restore api's successful command and expect exit 0. |
| Retraction: after a successful baseline, remove the entire `[[standalone_repository]]` block and rerun. | Tools' checkout and HEAD remain; only its eligible block disappears, unrelated managed blocks remain. `winter-extensions` reports `cleared`; standalone target is skipped, but finalization runs. With the healthy project extension declared as above, instead expect its `healthy` block containing `/projects/healthy/` to remain and the detail to report remaining names. |
| Unowned safety: write `user rules` to `AGENTS.md`; also try a fresh fixture with no `AGENTS.md`. | Exit 0; bytes remain identical, or file remains absent; no safety action and no `CLAUDE.md` creation. |
| Safety conflict: write only the safety begin marker and `old` with no end marker. | Exit 1 after both targets; error names workspace and `AGENTS.md`; bytes unchanged. |
| Projects only: remove the standalone block before the first run. | Exit 0; `winter-config` exists; no standalone target and no `winter-extensions` action. |
| Adoption disabled: insert `adopt_extensions = "none"` before the first table header. | Exit 0; tools still clones, `winter-config` exists, but no tools block and no `winter-extensions` action. Add a stale simple-name block manually and rerun: it is preserved. |
| Non-Git root: use `fixture nongit nongit`. | Both HEADs exist after exit 0; no `.git`, exclude file, or workspace-exclude action is created at the root. |
| Initial configuration failure: replace config with `prefix = [`; separately run from an empty directory below `$tree` with no workspace ancestor. | Exit 1 before any target event or writes; compare snapshots. |
| Finalization failure alone: add `cmd = ["mv ../.git/info/exclude ../.git/info/exclude.saved; mkdir ../.git/info/exclude"]` to tools' block. | Projects completes successfully; finalization fails with `winter-extensions`, exit 1, yet the outdated owned safety block from the first case is refreshed. Remove the fault command and restore exclude by removing the empty directory and moving the saved file back before rerunning. |

For the finalization-failure case, use the explicit `tools` path from `fixture` and add the outdated safety block
before running. Its command changes only the scratch root's exclude after the projects target finishes. Do not use
permission-based faults that can silently succeed as root.

**Pinned state and concurrent runs.** Use a separate fresh `fixture concurrent`; add `ref = "v1"` inside the standalone
block. Lock creation is an intentional tracked artifact, not part of the clean-root assertion above. Replace api's
`cmd` with the following TOML line, a causal two-party barrier:

```toml
cmd = ["mkdir -p ../../barrier; if mkdir ../../barrier/one 2>/dev/null; then :; else mkdir ../../barrier/two; fi; printf 'overlap-ready\\n'; i=0; until test -d ../../barrier/two; do i=$((i+1)); test $i -lt 300 || exit 9; sleep 0.1; done"]
```

Preclone api using `g clone https://fixture.invalid/api.git "$ws/projects/api"` (first `mkdir -p "$ws/projects"`),
so both invocations reach this command rather than losing the barrier participant to a clone race.

```bash
run concurrent-a ws init --json & a=$!
run concurrent-b ws init --json & b=$!
wait "$a"; wait "$b"
# Restore api's original successful cmd before the recovery run.
run concurrent-third ws init --json
cp "$ws/.winter/config.lock" "$evidence/lock.before"
run pinned-repeat ws init --json
cmp "$evidence/lock.before" "$ws/.winter/config.lock"
```

Both barrier directories and both readiness events must exist; a timeout is a failed exercise, not concurrency
evidence. Both initial exits are 0, or one is 1 solely for the permitted tools destination/clone race. Unrelated Git
contention is not allowed. The third and repeat exit 0. Inspect excludes for exactly one of every expected block and
the lock for exactly one tools record, with tag `v1`'s commit. Every seeded checkout has HEAD. A held pin may report
`pinned` again but must not rewrite the lock. Retain separate streams and exits for both processes.

**Interrupt/restart.** Use `fixture interrupt`. Replace api's command with
`cmd = ["printf 'interrupt-ready\\n'; exec sleep 300"]`. The following manual supervisor keeps a session leader
alive until explicitly released, reserving its process-group ID throughout signalling. Its binary runs in the
foreground, so it does not inherit the ignored SIGINT of an asynchronous shell command. Do not turn this into a test
that sends SIGINT; tests forbid that. Run this block in the same setup shell:

```bash
cat >"$tree/interrupt.sh" <<'SH'
#!/bin/bash
binary=$1 ws=$2 evidence=$3
# setsid starts a new session; this shell stays its leader until the release FIFO is written.
trap ':' INT
cd "$ws" || exit 1
printf '%s\n' "$$" >"$evidence/interrupt.leader"
"$binary" ws init --json >"$evidence/interrupt.stdout" 2>"$evidence/interrupt.stderr"
printf '%s\n' "$?" >"$evidence/interrupt.exit"
IFS= read -r release <"$evidence/interrupt.release"
SH
mkfifo "$evidence/interrupt.release"
# Keep the FIFO open without blocking. Do not pass this descriptor to the supervisor.
exec {release_fd}<>"$evidence/interrupt.release"
# Job control resets SIGINT for the launched job. setsid --fork then creates a separate session.
set -m
"${pin[@]}" setsid --fork --wait bash "$tree/interrupt.sh" "$binary" "$ws" "$evidence" \
  {release_fd}>&- &
launcher=$!
set +m
deadline=$((SECONDS+30))
until test -s "$evidence/interrupt.leader"; do
  test "$SECONDS" -lt "$deadline" || { echo 'No leader; retain tree'; break; }
  sleep .05
done
group=$(cat "$evidence/interrupt.leader")
leader_start=$(awk '{print $22}' "/proc/$group/stat")
owned_group() {
  test -n "$group" && test "$group" -gt 1 &&
  test "$(awk '{print $22}' "/proc/$group/stat" 2>/dev/null)" = "$leader_start" &&
  test "$(ps -o sid= -p "$group" | tr -d ' ')" = "$group" &&
  test "$(ps -o pgid= -p "$group" | tr -d ' ')" = "$group" &&
  test "$group" != "$(ps -o pgid= -p "$$" | tr -d ' ')"
}
deadline=$((SECONDS+30))
until jq -e 'select(.type == "cmd_output_line" and .line == "interrupt-ready")' \
    "$evidence/interrupt.stdout" >/dev/null 2>&1; do
  test "$SECONDS" -lt "$deadline" || { echo 'No readiness; retain tree'; break; }
  sleep .05
done
# Require readiness again; a timed-out loop must never authorize SIGINT.
jq -e 'select(.type == "cmd_output_line" and .line == "interrupt-ready")' \
  "$evidence/interrupt.stdout" >/dev/null &&
  owned_group && kill -INT -- "-$group"
deadline=$((SECONDS+10))
until test -s "$evidence/interrupt.exit"; do
  test "$SECONDS" -lt "$deadline" || { echo 'No exit; use failure cleanup below'; break; }
  sleep .05
done
cat "$evidence/interrupt.exit"                 # must be 1
printf 'aborted\n' | cmp - "$evidence/interrupt.stderr"
# Wait for every member other than the reserved leader to disappear, including zombies.
deadline=$((SECONDS+10))
while ps -eo pid=,sid= | awk -v g="$group" '$2 == g && $1 != g {found=1} END {exit !found}'; do
  test "$SECONDS" -lt "$deadline" || { echo 'Survivor; use failure cleanup below'; break; }
  sleep .05
done
ps -eo pid=,ppid=,pgid=,sid=,stat= >"$evidence/interrupt.processes"
# Only on success, with no other session member remaining:
if test "$(cat "$evidence/interrupt.exit")" = 1 &&
   ! ps -eo pid=,sid= | awk -v g="$group" '$2 == g && $1 != g {found=1} END {exit !found}'; then
  printf 'release\n' >&"$release_fd"
  group=                         # never signal this ID after releasing the leader
  deadline=$((SECONDS+10))
  while ps -o stat= -p "$launcher" | grep -q '^[[:space:]]*[^Z[:space:]]'; do
    test "$SECONDS" -lt "$deadline" || { echo 'Launcher survived; retain tree'; break; }
    sleep .05
  done
  if ! ps -o stat= -p "$launcher" | grep -q '^[[:space:]]*[^Z[:space:]]'; then
    wait "$launcher"
  fi
fi
exec {release_fd}>&-
```

Do not proceed after any failed observation. If the leader is still held and cleanup is needed, first call
`owned_group` and only on success send `kill -KILL -- "-$group"` once, then clear `group`. Observe the launcher
with `ps -o stat= -p "$launcher"` for at most ten seconds; call `wait "$launcher"` only after it is absent or a
zombie. If it or descendants survive, report failure and retain the tree, rather than waiting forever or sending
another signal to a possibly reused ID. On success the binary was reaped by its supervisor; orphaned children must
have disappeared through the host's reaper before the leader is released. The final `wait` reaps the owned launcher.
The bounded readiness polling is observation, not a sleep-based assumption that the child is ready.
Replace the slow command with the original successful command, then `run restart ws init --json`. Expect exit 0,
both checkout HEADs, no duplicate blocks or lock entries, and the successful command marker. Never send a signal to
`0`, `-1`, an inferred process name, or any other environment's process group.

**Discovery and refusal.** In the recovered fixture:

```bash
run help ws init --help
snapshot >"$evidence/refusal.before"
run target-refused ws init alpha
run all-refused ws init --all
snapshot >"$evidence/refusal.after"
cmp "$evidence/refusal.before" "$evidence/refusal.after"
run context-ws context ws
run context-init context ws/init
```

Help and context exit 0; both unsupported forms exit 2 with usage diagnostics and no reconciliation events or state
changes. `context ws` points to `ws/init`; that topic describes targets, ordered repository steps, repeat commands,
failure exits, refresh ownership, and the work bare reconciliation leaves out. It names no unsupported command form
as implemented and links no absent prune topic. Run `workspace:manual-registration` separately in its own disposable
scratch checkout; retain that method's evidence too. Broken-output termination and stopped admission/joining are
checked by the executable and component methods, not by sending more signals here.

**Cold discovery/design probe, before push.** Give a fresh read-only agent the target checkout path and only this
task cue: “Verify workspace reconciliation after an interrupted bootstrap.” Do not name this method or document.
Retain its navigation and proposed exercise. Record **reached?** separately from **behaved?**: it must discover
`AGENTS.md` → `context/index.md` → `context/verification/index.md` → this matrix, find this method, and propose an
absolute built-binary path, scratch local fixtures, the pinned environment, readiness-based owned-group SIGINT, and
restart evidence rather than running against the real workspace. Correct context and repeat with a fresh agent if
either answer is no. This design probe does not substitute for executing the CLI exercise.

**Success.** Every case has the stated exit, streams, and state; interruption ends and reaps the owned children before
restart, and both cold-probe answers are yes.

**Evidence and cleanup.** Keep exits, both streams, configuration versions, state snapshots, exclude bytes and
metadata, HEADs, lock records, barrier evidence, and the two cold-probe answers under method id
`workspace:manual-ws-init`. Record every table case's expected/observed result. Compare each remote's `show-ref`
with `remote-NAME.before`; no remote ref may change. Wait for both concurrent invocations and the interrupt
supervisor before cleanup. On an unexpected hang, stop only the known owned processes/groups and record failure;
never delete their tree while they run. Archive evidence outside `$tree`, then remove only the exact path printed
by `printf '%s\n' "$tree"` after checking it is the `manual-ws-init.*` directory created above. No installed binary,
source checkout, real workspace, or service is changed.

## Tools

| Tool             | Use                                                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------------------------ |
| `tool:format`    | `cargo dev fmt` — rewrites formatting; run before the slice gate when its `fmt` step fails.                        |
| `tool:keep-tree` | `WINTER_TEST_KEEP=1` on any test command — keeps the isolated tree and prints its path.                            |
| `tool:record`    | `cargo dev record` — records a scenario with the reference tool; [scenarios.md](./scenarios.md) says when and how. |
