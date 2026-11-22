//! The git operations over the program port, what they answer on failure, and the commit id a git call returns.

use std::ffi::OsStr;
use std::path::Path;
use std::time::Duration;

mod remote;

use crate::diagnostics::Diagnostics;
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
const CHECKOUT_SPAN: &str = "git checkout";
const FETCH_SPAN: &str = "git fetch";
const STASH_PUSH_SPAN: &str = "git stash push";
const STASH_POP_SPAN: &str = "git stash pop";

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
        &mut || run_for(programs, path, &args, Some(timeout)),
        &mut |duration| (pacing.sleep)(duration),
        &mut || (pacing.draw)(),
        diagnostics,
    )?;
    ended(finished, repo, path, GitOperation::Fetch, &args, &mut *span).map(drop)
}

/// Whether the checkout at `path` has no change: no modified tracked file and no untracked file.
///
/// Any failure of git reads as dirty. Only a git that cannot be started is an error.
pub fn clean(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    repo: &str,
    path: &Path,
) -> Result<bool, GitFailure> {
    let _span = tracer.operation(STATUS_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let finished = run(programs, path, &["status", "--porcelain"])?;
    Ok(finished.exit == Exit::Code(0) && finished.stdout.is_empty())
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

/// Runs git with `args` in `cwd`. A git that cannot be started is an internal failure.
fn run(programs: &dyn RunPrograms, cwd: &Path, args: &[&str]) -> Result<Finished, GitFailure> {
    run_for(programs, cwd, args, None)
}

/// [`run`], killed when it outlives `timeout`.
fn run_for(
    programs: &dyn RunPrograms,
    cwd: &Path,
    args: &[&str],
    timeout: Option<Duration>,
) -> Result<Finished, GitFailure> {
    let args: Vec<&OsStr> = args.iter().map(OsStr::new).collect();
    programs
        .run(&Call {
            program: Program::Git,
            args: &args,
            cwd,
            timeout,
        })
        .map_err(|error| GitFailure::Internal(Box::new(InternalError::unexpected(error))))
}

/// Runs git with `args` in `cwd` for `operation` on the repository `repo`; a non-zero exit is the repo error
/// of the call, which marks `span` failed.
fn succeeded(
    programs: &dyn RunPrograms,
    repo: &str,
    cwd: &Path,
    operation: GitOperation,
    args: &[&str],
    span: &mut dyn OperationSpan,
) -> Result<Finished, GitFailure> {
    let finished = run(programs, cwd, args)?;
    ended(finished, repo, cwd, operation, args, span)
}

/// What a finished call answers for `operation`: itself on a zero exit, else the repo error of the call, which
/// marks `span` failed.
fn ended(
    finished: Finished,
    repo: &str,
    cwd: &Path,
    operation: GitOperation,
    args: &[&str],
    span: &mut dyn OperationSpan,
) -> Result<Finished, GitFailure> {
    if finished.exit == Exit::Code(0) {
        return Ok(finished);
    }
    span.failed(REPO_ERROR_TYPE);
    let stderr = String::from_utf8_lossy(&finished.stderr);
    let stderr = stderr.trim();
    Err(GitFailure::Repo(Box::new(RepoError {
        message: RepoMessage::GitFailure {
            operation,
            repo: repo.to_owned(),
        },
        program: Some(Program::Git.name().to_owned()),
        subcommand: args.first().map(|subcommand| (*subcommand).to_owned()),
        args: args.iter().skip(1).map(|arg| (*arg).to_owned()).collect(),
        cwd: Some(cwd.to_path_buf()),
        exit: Some(finished.exit.number()),
        stderr: (!stderr.is_empty()).then(|| stderr.to_owned()),
    })))
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
