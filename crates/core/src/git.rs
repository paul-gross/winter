//! The git operations over the program port, what they answer on failure, and the commit id a git call returns.

use std::ffi::{OsStr, OsString};
use std::io;
use std::iter;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

mod remote;

use crate::diagnostics::Diagnostics;
use crate::files::{ReadFiles, ScratchFiles};
use crate::process::ProcessEnv;
use crate::programs::{Call, Exit, Finished, Program, RunPrograms};
use crate::termination::{GitOperation, InternalError, RepoError, RepoMessage};
use crate::trace::{CommandTracer, OperationSpan};

/// A commit or object id as git prints it. A lock commit is kept as read, unvalidated.
pub type Sha = String;

/// What a git operation answers on failure: the call's repo error, or a git that could not be started.
#[derive(Debug)]
pub enum GitFailure {
    Repo(Box<RepoError>),
    Internal(Box<InternalError>),
}

/// The operation spans of the git operations, named `git <operation>`.
pub(crate) const REV_PARSE_SPAN: &str = "git rev-parse";
const STATUS_SPAN: &str = "git status";
const WRITE_TREE_SPAN: &str = "git write-tree";
const CHECKOUT_SPAN: &str = "git checkout";
const FETCH_SPAN: &str = "git fetch";
const STASH_PUSH_SPAN: &str = "git stash push";
const STASH_POP_SPAN: &str = "git stash pop";
const CONFIG_SPAN: &str = "git config";
const CLONE_SPAN: &str = "git clone";
const REV_LIST_SPAN: &str = "git rev-list";

/// The one attribute of a span: the name of the repository the operation works on.
pub(crate) const REPO_ATTRIBUTE: &str = "winter.repo";

/// The `error.type` of a span whose operation ended in a repo error.
pub(crate) const REPO_ERROR_TYPE: &str = "RepoError";

/// The commit `HEAD` names in the checkout at `path`, the checkout of the repository `repo`.
///
/// Fails when `path` is no repository or `HEAD` names no commit.
pub fn head(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Sha, GitFailure> {
    let mut span = tracer.operation(REV_PARSE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["rev-parse", "HEAD"];
    let finished = succeeded(programs, repo, path, GitOperation::Head, &args, &mut *span)?;
    Ok(String::from_utf8_lossy(&finished.stdout).trim().to_owned())
}

/// Fetches `origin` into the checkout at `path`, the checkout of the repository `repo`: `git fetch origin`,
/// bounded by `WINTER_GIT_TIMEOUT_S` of `env`, read on every call, and attempted up to three times while it
/// fails in a way that is transient. A tag the remote moved is kept as it is locally.
///
/// One span covers every attempt. A retry and a timeout value that is unusable are reported through
/// `diagnostics`.
pub fn fetch(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    env: &ProcessEnv,
    diagnostics: &mut Diagnostics<'_>,
    repo: &str,
    path: &Path,
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(FETCH_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let timeout = remote::timeout(env, diagnostics);
    let pacing = programs.pacing();
    let args = ["fetch", "origin"];
    let finished = remote::attempts(
        &mut || run_for(programs, path, &args, Some(timeout), &[]),
        &mut |duration| (pacing.sleep)(duration),
        &mut || (pacing.draw)(),
        diagnostics,
    )?;
    ended(finished, repo, path, GitOperation::Fetch, &args, &mut *span).map(drop)
}

/// The variables by which an environment points git at a repository, or at a part of one, other than the one
/// it finds from its working directory. Git exports several of them to its hooks and aliases.
const LOCATING_VARIABLES: [&str; 12] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_GRAFT_FILE",
    "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
];

/// A program port whose every call runs without [`LOCATING_VARIABLES`], so that git answers for the
/// repository of the working directory of the call. A caller whose answer decides a removal runs git
/// through it.
pub(crate) struct OwnRepository<'a>(pub(crate) &'a dyn RunPrograms);

impl RunPrograms for OwnRepository<'_> {
    fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
        let remove: Vec<&OsStr> = call
            .remove
            .iter()
            .copied()
            .chain(LOCATING_VARIABLES.iter().map(OsStr::new))
            .collect();
        self.0.run(&Call {
            remove: &remove,
            ..*call
        })
    }
}

/// Whether the checkout at `path` has no change: no modified tracked file and no untracked file. An untracked
/// file counts whatever `status.showUntrackedFiles` is set to.
///
/// Any failure of git reads as dirty. Only a git that cannot be started is an error.
pub fn clean(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let _span = tracer.operation(STATUS_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["status", "--porcelain", "--untracked-files=normal"];
    let finished = run(programs, path, &args)?;
    Ok(finished.exit == Exit::Code(0) && finished.stdout.is_empty())
}

/// Whether the checkout at `path`, the checkout of the repository `repo`, holds a commit that no
/// remote-tracking branch holds: one reachable from `HEAD`, a local branch, or a tag.
///
/// A history that cannot be read is an error, a `HEAD` that names no commit among them.
pub fn local_only(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let mut span = tracer.operation(REV_LIST_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = [
        "rev-list",
        "--max-count=1",
        "HEAD",
        "--branches",
        "--tags",
        "--not",
        "--remotes",
    ];
    let operation = GitOperation::LocalOnly;
    let finished = succeeded(programs, repo, path, operation, &args, &mut *span)?;
    Ok(!finished.stdout.is_empty())
}

/// Whether the repository of the checkout at `path`, the checkout of the repository `repo`, holds a stash.
///
/// Exit 1 is how git says there is none. Any other non-zero exit is an error.
pub fn stashed(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let mut span = tracer.operation(REV_PARSE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["rev-parse", "--verify", "--quiet", "refs/stash"];
    let finished = run(programs, path, &args)?;
    if finished.exit == Exit::Code(1) {
        return Ok(false);
    }
    let operation = GitOperation::StashList;
    ended(finished, repo, path, operation, &args, &mut *span).map(|_| true)
}

/// The tree of the tracked content of the checkout at `path`, the checkout of the repository `repo`: what
/// `HEAD` plus every change to a tracked file makes, whether staged or not.
///
/// The tree is written from a scratch index under `temp`, a copy of the real one, or a `HEAD` read into a new
/// one where the real one does not exist, so the real index is never written and git has no stale stat entry
/// to trust. The scratch directory is removed on every way out.
pub fn tree(
    files: &dyn ReadFiles,
    scratch: &dyn ScratchFiles,
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    temp: &Path,
    repo: &str,
    path: &Path,
) -> Result<Sha, GitFailure> {
    let mut span = tracer.operation(WRITE_TREE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let located = succeeded(
        programs,
        repo,
        path,
        GitOperation::ContentTree,
        &["rev-parse", "--git-path", "index"],
        &mut *span,
    )?;
    let real = path.join(String::from_utf8_lossy(&located.stdout).trim());
    let (index, seeded) = if files.is_file(&real) {
        (scratch.scratch_copy(temp, &real), true)
    } else {
        (scratch.scratch_path(temp, "index"), false)
    };
    let index =
        index.map_err(|error| GitFailure::Internal(Box::new(InternalError::unexpected(error))))?;
    let variable: OsString = index.path().into();
    let env = [(OsStr::new("GIT_INDEX_FILE"), variable.as_os_str())];
    let mut step = |args: &[&str]| -> Result<Finished, GitFailure> {
        let finished = run_for(programs, path, args, None, &env)?;
        ended(
            finished,
            repo,
            path,
            GitOperation::ContentTree,
            args,
            &mut *span,
        )
    };
    if !seeded {
        step(&["read-tree", "HEAD"])?;
    }
    step(&["add", "-u"])?;
    let written = step(&["write-tree"])?;
    Ok(String::from_utf8_lossy(&written.stdout).trim().to_owned())
}

/// Whether a tracked file of the checkout at `path`, the checkout of the repository `repo`, differs from
/// the index or from `HEAD`. An untracked file is no change, and git takes no optional lock.
///
/// Unlike [`clean`], a failure of git is an error.
pub fn dirty(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let mut span = tracer.operation(STATUS_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["status", "--porcelain", "--untracked-files=no"];
    let env = [(OsStr::new("GIT_OPTIONAL_LOCKS"), OsStr::new("0"))];
    let finished = run_for(programs, path, &args, None, &env)?;
    let finished = ended(
        finished,
        repo,
        path,
        GitOperation::TrackedStatus,
        &args,
        &mut *span,
    )?;
    Ok(!finished.stdout.is_empty())
}

/// Whether `rev` names an object in the repository at `path`. A non-zero exit is no match, not an error.
///
/// Opens no span: the resolution that calls it opens the one its verifies share.
pub fn verify(programs: &dyn RunPrograms, path: &Path, rev: &str) -> Result<bool, GitFailure> {
    Ok(verified(programs, path, rev)?.is_some())
}

/// The id `rev` names in the repository at `path`, `None` when it names no object. Opens no span.
pub(crate) fn verified(
    programs: &dyn RunPrograms,
    path: &Path,
    rev: &str,
) -> Result<Option<Sha>, GitFailure> {
    let finished = run(programs, path, &["rev-parse", "--verify", rev])?;
    Ok((finished.exit == Exit::Code(0))
        .then(|| String::from_utf8_lossy(&finished.stdout).trim().to_owned()))
}

/// Resets the local branch `branch` to `origin/<branch>`, tracking it, and checks it out. Commits only the
/// local branch held are left to the reflog.
pub fn checkout_branch(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    branch: &str,
) -> Result<(), GitFailure> {
    let origin = format!("origin/{branch}");
    checkout(
        programs,
        tracer,
        repo,
        path,
        &["checkout", "-B", branch, "--track", &origin],
    )
}

/// Checks out the commit `commit` with a detached `HEAD`.
pub fn checkout_detached(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    commit: &str,
) -> Result<(), GitFailure> {
    checkout(
        programs,
        tracer,
        repo,
        path,
        &["checkout", "--detach", commit],
    )
}

fn checkout(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    args: &[&str],
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(CHECKOUT_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    succeeded(
        programs,
        repo,
        path,
        GitOperation::Checkout,
        args,
        &mut *span,
    )
    .map(drop)
}

/// Stashes the changes of the tracked files of the checkout at `path`. Untracked files are not stashed: a
/// tree dirty only by them stashes nothing, and a later [`stash_pop`] takes whatever stash is on top.
pub fn stash_push(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(STASH_PUSH_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    succeeded(
        programs,
        repo,
        path,
        GitOperation::StashPush,
        &["stash", "push"],
        &mut *span,
    )
    .map(drop)
}

/// Restores the stash on top of the stack into the checkout at `path`, and drops it.
pub fn stash_pop(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(STASH_POP_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    succeeded(
        programs,
        repo,
        path,
        GitOperation::StashPop,
        &["stash", "pop"],
        &mut *span,
    )
    .map(drop)
}

/// Clones `url` into `dest`, the checkout of the repository `repo`, running git in `root`: `git clone
/// [--branch <branch>] -- <url> <dest>`, with the whole history and no submodule recursion. Without a `branch`
/// the clone lands on the remote's default `HEAD`. It has no timeout and is not retried.
pub fn clone(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    root: &Path,
    url: &str,
    dest: &Path,
    branch: Option<&str>,
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(CLONE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let mut args = vec![OsStr::new("clone")];
    if let Some(branch) = branch {
        args.extend([OsStr::new("--branch"), OsStr::new(branch)]);
    }
    args.extend([OsStr::new("--"), OsStr::new(url), dest.as_os_str()]);
    succeeded(programs, repo, root, GitOperation::Clone, &args, &mut *span).map(drop)
}

/// What a git read answered, `None` when git had no answer, and the call that asked, for an error that rests
/// on it. The message of `call` is that of a failure of the operation; a caller states its own.
pub(crate) struct Probe<T> {
    pub(crate) answer: Option<T>,
    pub(crate) call: Box<RepoError>,
}

/// The top directory of the work tree that holds `path`, as git names it; `None` when git answers none, a
/// non-zero exit.
pub fn toplevel(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Option<PathBuf>, GitFailure> {
    probe_toplevel(programs, tracer, repo, path).map(|probe| probe.answer)
}

/// [`toplevel`], with the call.
pub(crate) fn probe_toplevel(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Probe<PathBuf>, GitFailure> {
    let _span = tracer.operation(REV_PARSE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["rev-parse", "--show-toplevel"];
    let finished = run(programs, path, &args)?;
    let call = call_error(&finished, repo, path, GitOperation::Toplevel, &args);
    let answer = (finished.exit == Exit::Code(0)).then(|| line(finished.stdout).into());
    Ok(Probe { answer, call })
}

/// The `remote.origin.url` of the checkout at `path`, as its configuration holds it; `None` when it is unset.
pub fn origin_url(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Option<OsString>, GitFailure> {
    probe_origin_url(programs, tracer, repo, path).map(|probe| probe.answer)
}

/// [`origin_url`], with the call.
pub(crate) fn probe_origin_url(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Probe<OsString>, GitFailure> {
    let mut span = tracer.operation(CONFIG_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["config", "--get", "remote.origin.url"];
    let operation = GitOperation::OriginUrl;
    let finished = run(programs, path, &args)?;
    let call = call_error(&finished, repo, path, operation, &args);
    // Exit 1 is how git says the key is unset.
    if finished.exit == Exit::Code(1) {
        return Ok(Probe { answer: None, call });
    }
    let finished = ended(finished, repo, path, operation, &args, &mut *span)?;
    let answer = Some(line(finished.stdout));
    Ok(Probe { answer, call })
}

/// Sets `key` to `value` in the configuration of the repository of the checkout at `path`.
pub fn set_config(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    key: &str,
    value: &str,
) -> Result<(), GitFailure> {
    let args = [key, value].map(OsStr::new);
    config_write(programs, tracer, repo, path, &args)
}

/// The git directory whose `info/exclude` the checkout at `path` reads, absolute, as git names it.
pub(crate) fn exclude_git_directory(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<PathBuf, GitFailure> {
    exclude_lookup(programs, tracer, repo, path, "--absolute-git-dir")
}

/// The common directory behind the exclude file of the checkout at `path`, as git names it: absolute, or
/// relative to `path`.
pub(crate) fn exclude_common_directory(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<PathBuf, GitFailure> {
    exclude_lookup(programs, tracer, repo, path, "--git-common-dir")
}

/// One `git rev-parse <flag>` of the lookup of an exclude file, which is the operation a failure names.
fn exclude_lookup(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    flag: &str,
) -> Result<PathBuf, GitFailure> {
    let mut span = tracer.operation(REV_PARSE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = ["rev-parse", flag];
    let finished = succeeded(
        programs,
        repo,
        path,
        GitOperation::ExcludeLocate,
        &args,
        &mut *span,
    )?;
    Ok(line(finished.stdout).into())
}

const WORKTREE_CONFIG: &str = "extensions.worktreeConfig";
const EXCLUDES_FILE: &str = "core.excludesFile";

/// Whether the repository of the checkout at `path` has `extensions.worktreeConfig` on in its own
/// configuration.
pub(crate) fn worktree_config_enabled(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let args = ["--local", "--type=bool", "--get", WORKTREE_CONFIG].map(OsStr::new);
    let value = config_get(programs, tracer, repo, path, &args)?;
    Ok(value.is_some_and(|value| value == "true"))
}

/// Turns `extensions.worktreeConfig` on for the repository of the checkout at `path`.
pub(crate) fn enable_worktree_config(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<(), GitFailure> {
    let args = [WORKTREE_CONFIG, "true"].map(OsStr::new);
    config_write(programs, tracer, repo, path, &args)
}

/// The value of `key` in the configuration file `file`, `None` when it is unset there.
pub(crate) fn file_setting(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    file: &Path,
    key: &str,
) -> Result<Option<OsString>, GitFailure> {
    let args = [
        OsStr::new("--file"),
        file.as_os_str(),
        OsStr::new("--get"),
        OsStr::new(key),
    ];
    config_get(programs, tracer, repo, path, &args)
}

/// Sets `key` to `value` in the configuration file `file`.
pub(crate) fn set_file_setting(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    file: &Path,
    key: &str,
    value: &OsStr,
) -> Result<(), GitFailure> {
    let args = [
        OsStr::new("--file"),
        file.as_os_str(),
        OsStr::new(key),
        value,
    ];
    config_write(programs, tracer, repo, path, &args)
}

/// Removes `key` from the configuration file `file`, which must hold it once.
pub(crate) fn unset_file_setting(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    file: &Path,
    key: &str,
) -> Result<(), GitFailure> {
    let args = [
        OsStr::new("--file"),
        file.as_os_str(),
        OsStr::new("--unset"),
        OsStr::new(key),
    ];
    config_write(programs, tracer, repo, path, &args)
}

/// The `core.excludesFile` the worktree at `path` has of its own, `None` when it has none. It is the text as
/// git holds it: git reads a trailing `/` as part of the name, which a comparison of paths would not.
pub(crate) fn worktree_excludes_file(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<Option<OsString>, GitFailure> {
    let args = ["--worktree", "--get", EXCLUDES_FILE].map(OsStr::new);
    config_get(programs, tracer, repo, path, &args)
}

/// Sets the `core.excludesFile` of the worktree at `path` alone to `file`.
pub(crate) fn set_worktree_excludes_file(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    file: &Path,
) -> Result<(), GitFailure> {
    let args = [
        OsStr::new("--worktree"),
        OsStr::new(EXCLUDES_FILE),
        file.as_os_str(),
    ];
    config_write(programs, tracer, repo, path, &args)
}

/// What `git config <args>`, a `--get`, reads in `path`: `None` on exit 1, which is how git says the key is
/// unset. Any other non-zero exit is an error.
fn config_get(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    args: &[&OsStr],
) -> Result<Option<OsString>, GitFailure> {
    let mut span = tracer.operation(CONFIG_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = config_arguments(args);
    let finished = run(programs, path, &args)?;
    if finished.exit == Exit::Code(1) {
        return Ok(None);
    }
    let finished = ended(
        finished,
        repo,
        path,
        GitOperation::Config,
        &args,
        &mut *span,
    )?;
    Ok(Some(line(finished.stdout)))
}

/// Runs `git config <args>`, a write of git configuration, in `path`.
fn config_write(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
    args: &[&OsStr],
) -> Result<(), GitFailure> {
    let mut span = tracer.operation(CONFIG_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let args = config_arguments(args);
    succeeded(
        programs,
        repo,
        path,
        GitOperation::Config,
        &args,
        &mut *span,
    )
    .map(drop)
}

fn config_arguments<'a>(args: &[&'a OsStr]) -> Vec<&'a OsStr> {
    iter::once(OsStr::new("config"))
        .chain(args.iter().copied())
        .collect()
}

/// The one line git wrote, without its line end, with every other byte kept.
fn line(mut stdout: Vec<u8>) -> OsString {
    if stdout.last() == Some(&b'\n') {
        stdout.pop();
    }
    OsString::from_vec(stdout)
}

/// Runs git with `args` in `cwd`. A git that cannot be started is an internal failure.
fn run<A: AsRef<OsStr>>(
    programs: &dyn RunPrograms,
    cwd: &Path,
    args: &[A],
) -> Result<Finished, GitFailure> {
    run_for(programs, cwd, args, None, &[])
}

/// [`run`], killed when it outlives `timeout`.
fn run_for<A: AsRef<OsStr>>(
    programs: &dyn RunPrograms,
    cwd: &Path,
    args: &[A],
    timeout: Option<Duration>,
    env: &[(&OsStr, &OsStr)],
) -> Result<Finished, GitFailure> {
    let args: Vec<&OsStr> = args.iter().map(AsRef::as_ref).collect();
    programs
        .run(&Call {
            program: Program::Git,
            stdin: crate::programs::Stdin::Null,
            remove: &[],
            args: &args,
            cwd,
            timeout,
            env,
        })
        .map_err(|error| GitFailure::Internal(Box::new(InternalError::unexpected(error))))
}

/// Runs git with `args` in `cwd` for `operation` on the repository `repo`; a non-zero exit is the repo error
/// of the call, which marks `span` failed.
fn succeeded<A: AsRef<OsStr>>(
    programs: &dyn RunPrograms,
    repo: &str,
    cwd: &Path,
    operation: GitOperation,
    args: &[A],
    span: &mut dyn OperationSpan,
) -> Result<Finished, GitFailure> {
    let finished = run(programs, cwd, args)?;
    ended(finished, repo, cwd, operation, args, span)
}

/// What a finished call answers for `operation`: itself on a zero exit, else the repo error of the call, which
/// marks `span` failed.
fn ended<A: AsRef<OsStr>>(
    finished: Finished,
    repo: &str,
    cwd: &Path,
    operation: GitOperation,
    args: &[A],
    span: &mut dyn OperationSpan,
) -> Result<Finished, GitFailure> {
    if finished.exit == Exit::Code(0) {
        return Ok(finished);
    }
    span.failed(REPO_ERROR_TYPE);
    Err(GitFailure::Repo(call_error(
        &finished, repo, cwd, operation, args,
    )))
}

/// The repo error that carries the finished call of git made for `operation`.
fn call_error<A: AsRef<OsStr>>(
    finished: &Finished,
    repo: &str,
    cwd: &Path,
    operation: GitOperation,
    args: &[A],
) -> Box<RepoError> {
    let stderr = String::from_utf8_lossy(&finished.stderr);
    let stderr = stderr.trim();
    let mut args = args
        .iter()
        .map(|arg| arg.as_ref().to_string_lossy().into_owned());
    Box::new(RepoError {
        message: RepoMessage::GitFailure {
            operation,
            repo: repo.to_owned(),
        },
        program: Some(Program::Git.name().to_owned()),
        subcommand: args.next(),
        args: args.collect(),
        cwd: Some(cwd.to_path_buf()),
        exit: Some(finished.exit.number()),
        stderr: (!stderr.is_empty()).then(|| stderr.to_owned()),
    })
}

#[cfg(test)]
mod tests {
    use std::io;

    use super::*;
    use crate::trace::NoTrace;

    struct CannotStart;

    impl RunPrograms for CannotStart {
        fn run(&self, _: &Call<'_>) -> io::Result<Finished> {
            Err(io::Error::other("no git"))
        }
    }

    #[test]
    fn head_answers_internal_when_the_port_cannot_start_git() {
        let answer = head(&CannotStart, &NoTrace, "api", Path::new("/nowhere"));

        assert!(matches!(answer, Err(GitFailure::Internal(_))), "{answer:?}");
    }
}
