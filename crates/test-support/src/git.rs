//! Git fixture helpers: deterministic repositories and bare remotes inside one isolated tree.

use std::ffi::OsStr;
use std::fmt;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};

use crate::env::PinnedEnv;
use crate::process::{Exit, InvocationFailure, Observation, invoke};
use crate::tree::{TempTree, Use};

/// The placeholder host of every fixture remote; the tree's global git configuration maps it to the tree's
/// `remotes` directory at transport time.
const FIXTURE_URL: &str = "https://fixture.invalid/";

/// 2000-01-01T00:00:00+0000 in seconds since the epoch: the date of the first commit made in a tree.
const FIRST_COMMIT: u64 = 946_684_800;

/// How far apart, in seconds, the dates of two successive commits of one tree are.
const COMMIT_STEP: u64 = 60;

/// Git, run inside one isolated tree: every call gets the tree's pinned environment and a 30 s deadline.
///
/// Every path a helper takes is checked before the helper does anything: until each path of a call is known
/// to stay in the tree, symbolic links followed, no file is written or removed, no directory is made, and git
/// is not started. Only paths are checked: the arguments [`Git::run`] is given are passed to git unread.
///
/// The root of the tree is never made a repository, and no repository is created at its `.git` or under
/// it. [`Git::init`] and [`Git::bare_remote`] create a repository only where nothing is, or in a directory
/// that holds nothing, so git fills no entry that was there before it. A commit is made only at the top of
/// a work tree below the root, and a commit or a worktree only in a repository whose git directory and
/// common directory stay in the tree: git is asked for all three, reading only, before the call has any
/// effect.
///
/// The files a helper writes and the entries it removes itself, those of a commit and of the workspace
/// builder, are never entries of the root, by whatever path and through whatever symbolic link they are
/// reached: the git configuration of the tree, which is what keeps fixture URLs off the network, and the
/// directories of its layout are neither replaced nor removed. Such a file lies under an entry the root
/// already holds, so writing it adds none: neither `.git` nor `.winter` appears there. A written file is
/// always a new file: an entry already where it lands is replaced and never opened, so a hard link there
/// carries nothing to whatever else shares its content. A repository is created where git is told to, and
/// git puts none in place of a file. Every repository is created with SHA-1 objects and refs stored as
/// files, whatever the defaults of the installed git are.
///
/// That is the whole of it. A fixture built from honest inputs has no effect outside its tree, on the
/// entries of the root, or on the network, and the ways out named here are refused. The helpers are no
/// sandbox: a test that makes itself a way out by a means not named here is not stopped, and keeping it
/// inside the tree is for whoever writes it.
#[derive(Debug)]
pub struct Git<'t> {
    tree: &'t TempTree,
    executable: PathBuf,
}

/// One commit of a fixture repository.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CommitSpec {
    pub message: String,
    /// Path relative to the repository and content; the content replaces the file, as a new file in its
    /// place. A symbolic link at the path is followed, and the file it leads to stays in the tree and is the
    /// one replaced.
    pub files: Vec<(String, Vec<u8>)>,
    /// Paths relative to the repository of files the commit removes. A symbolic link is removed itself,
    /// wherever it leads, and never followed; the directories on the way to an entry are followed and stay
    /// in the tree.
    pub delete: Vec<String>,
    /// The branch the commit is made on, created from `HEAD` when absent; the current branch when `None`.
    pub branch: Option<String>,
    /// A lightweight tag put on the new commit.
    pub tag: Option<String>,
}

/// Why a helper did not do what it was asked.
#[derive(Debug)]
pub enum GitError {
    /// No `git` is on the pinned `PATH` outside the tree.
    NotFound,
    /// Git ran and exited non-zero.
    Failed {
        /// The arguments git was given, without the program itself.
        args: Vec<String>,
        stderr: String,
        exit: Exit,
    },
    /// Git gave no observation: it could not be started, or outran its deadline.
    NotObserved {
        args: Vec<String>,
        failure: InvocationFailure,
    },
    /// A path a helper was given does not stay where it is confined to: the tree for a repository or a
    /// working directory; below the root of the tree, outside its `.git`, for a repository that is created,
    /// and for [`Git::init`] and [`Git::bare_remote`] a place that is absent or an empty directory; the top
    /// of a work tree below the root for the repository of a commit; the tree for the git directory and the
    /// common directory of the repository of a commit or a worktree; the repository, and under an entry the
    /// root already holds, for a file of a commit; one path segment for the name of a remote. A relative
    /// path does not stay, and neither does one that a symbolic link leads out of the tree.
    Unconfined { path: PathBuf },
    /// A file or directory of a fixture could not be written or removed.
    File { path: PathBuf, error: io::Error },
}

impl fmt::Display for GitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound => f.write_str("no git is on the pinned PATH"),
            Self::Failed { args, stderr, exit } => {
                write!(
                    f,
                    "git {} ended with {exit:?}: {}",
                    args.join(" "),
                    stderr.trim_end()
                )
            }
            Self::NotObserved { args, failure } => {
                write!(f, "git {} gave no result: {failure}", args.join(" "))
            }
            Self::Unconfined { path } => {
                write!(f, "{} does not stay inside the fixture", path.display())
            }
            Self::File { path, error } => write!(f, "{}: {error}", path.display()),
        }
    }
}

impl std::error::Error for GitError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::NotObserved { failure, .. } => Some(failure),
            Self::File { error, .. } => Some(error),
            Self::NotFound | Self::Failed { .. } | Self::Unconfined { .. } => None,
        }
    }
}

impl<'t> Git<'t> {
    /// Git for `tree`. Fails when the pinned `PATH` of the tree names no directory outside the tree that
    /// holds an executable `git`: a stub of that name in the tree's `bin` is what a test observes, never what
    /// builds its fixtures.
    pub fn new(tree: &'t TempTree) -> Result<Self, GitError> {
        let env = tree.env();
        let path = env
            .variables()
            .find(|(name, _)| *name == "PATH")
            .map(|(_, value)| value.to_owned())
            .unwrap_or_default();
        let executable = host_git(&path, tree.root()).ok_or(GitError::NotFound)?;
        Ok(Self { tree, executable })
    }

    /// Runs `git <args>` in `cwd`, a directory under the root of the tree, and answers what it wrote to
    /// stdout. A non-zero exit is [`GitError::Failed`].
    pub fn run(&self, cwd: &Path, args: &[&str]) -> Result<String, GitError> {
        self.run_under(self.tree.env(), cwd, args)
    }

    /// Creates a repository at `path`, a path below the root of the tree, with `HEAD` on `branch`, SHA-1
    /// objects, and refs stored as files. The directories that lead to it are created. Nothing is at
    /// `path` before, or a directory that holds nothing: any other is [`GitError::Unconfined`].
    pub fn init(&self, path: &Path, branch: &str) -> Result<(), GitError> {
        let path = self.confined(path, Use::Created)?;
        self.run(self.tree.root(), &init_arguments(false, branch, path))?;
        Ok(())
    }

    /// Makes the commit `spec` describes in the repository at `repo` and answers its object id.
    ///
    /// `repo` is the top of a git work tree below the root of the tree. Once every path of the call has
    /// passed, git is asked where the work tree begins, reading only, before anything else is done: at the
    /// root of the tree, in a directory that is no repository, in a bare repository, and in a directory
    /// inside a work tree, no file is written or removed and no branch is switched to. Nor in a repository
    /// whose git directory or common directory, as git names them, does not stay in the tree: a `.git` that
    /// is a symbolic link, or a file that names a git directory, leading out of it.
    ///
    /// The commit holds every change of the working tree, the files and deletions of `spec` included, and is
    /// made even when it changes nothing. Its author and committer date is that of the n-th commit the
    /// helpers make in the tree: 2000-01-01T00:00:00+0000 plus n minutes, n from 0. Only a commit that was
    /// made counts: one git refuses leaves its date to the next.
    pub fn commit(&self, repo: &Path, spec: &CommitSpec) -> Result<String, GitError> {
        self.base(repo)?;
        let files = spec
            .files
            .iter()
            .map(|(relative, content)| Ok((self.file(repo, relative, Use::Written)?, content)))
            .collect::<Result<Vec<_>, GitError>>()?;
        let deleted = spec
            .delete
            .iter()
            .map(|relative| self.file(repo, relative, Use::Removed))
            .collect::<Result<Vec<_>, GitError>>()?;
        self.top_of_work_tree(repo)?;
        self.git_directories(repo)?;

        if let Some(branch) = &spec.branch {
            self.switch_to(repo, branch)?;
        }
        // Each path is checked again where it is used: the branch switched to may have brought a link.
        for (path, content) in files {
            self.write(path, content)?;
        }
        for path in deleted {
            self.landing(&path, Use::Removed)?;
            std::fs::remove_file(&path).map_err(|error| GitError::File { path, error })?;
        }
        self.run(repo, &["add", "--all"])?;

        self.tree.count_commit(|made| {
            let date = format!("@{} +0000", FIRST_COMMIT + made * COMMIT_STEP);
            let mut env = self.tree.env();
            env.set("GIT_AUTHOR_DATE", &date);
            env.set("GIT_COMMITTER_DATE", &date);
            let arguments = ["commit", "--quiet", "--allow-empty", "-m", &spec.message];
            self.run_under(env, repo, &arguments)
        })?;
        if let Some(tag) = &spec.tag {
            self.run(repo, &["tag", tag])?;
        }
        let id = self.run(repo, &["rev-parse", "HEAD"])?;
        Ok(id.trim_end().to_owned())
    }

    /// Builds the bare repository `remotes/<name>.git` of the tree from `commits`, with `HEAD` on
    /// `default_branch`, and answers its path.
    ///
    /// The commits are made in a scratch repository and pushed to the bare one by its path, so the bare
    /// repository has no remote and its configuration holds no path. Nothing is at `remotes/<name>.git`
    /// before, or a directory that holds nothing: any other is [`GitError::Unconfined`].
    pub fn bare_remote(
        &self,
        name: &str,
        default_branch: &str,
        commits: &[CommitSpec],
    ) -> Result<PathBuf, GitError> {
        let bare = self.remote_path(name)?;
        let destination = self.confined(&bare, Use::Created)?;
        let scratch_parent = self.scratch_parent()?;
        for spec in commits {
            leads_downward(spec)?;
        }
        self.run(
            self.tree.root(),
            &init_arguments(true, default_branch, destination),
        )?;
        if commits.is_empty() {
            return Ok(bare);
        }

        let scratch = scratch_in(scratch_parent)?;
        let repository = scratch.path().join("repository");
        self.init(&repository, default_branch)?;
        for spec in commits {
            self.commit(&repository, spec)?;
        }
        self.run(
            &repository,
            &[
                "push",
                "--quiet",
                destination,
                "refs/heads/*:refs/heads/*",
                "refs/tags/*:refs/tags/*",
            ],
        )?;
        remove(scratch)?;
        Ok(bare)
    }

    /// Clones the remote `name` of the tree to `dest`, a path below the root of the tree, from
    /// `https://fixture.invalid/<name>.git`, with refs stored as files. The bare repository the URL is mapped
    /// to stays in the tree, like any repository a helper reads or writes; one the tree lacks is for git to
    /// report.
    pub fn clone_remote(&self, name: &str, dest: &Path) -> Result<(), GitError> {
        let url = remote_url(name)?;
        self.base(&self.remote_path(name)?)?;
        let dest = self.destination(dest)?;
        self.run(
            self.tree.root(),
            &["clone", "--quiet", "--ref-format=files", &url, dest],
        )?;
        Ok(())
    }

    /// Adds the worktree `dest`, a path below the root of the tree, to the repository at `repo`, on the new
    /// branch `branch` made from `start`. The branch gets no upstream. The git directory and the common
    /// directory of the repository, as git names them when asked, reading only, stay in the tree, or
    /// nothing is done.
    pub fn add_worktree(
        &self,
        repo: &Path,
        dest: &Path,
        branch: &str,
        start: &str,
    ) -> Result<(), GitError> {
        let dest = self.destination(dest)?;
        self.git_directories(repo)?;
        self.run(
            repo,
            &[
                "worktree",
                "add",
                "--quiet",
                "--no-track",
                "-b",
                branch,
                dest,
                start,
            ],
        )?;
        Ok(())
    }

    /// Adds the commit `spec` describes to the remote `name` of the tree, as a commit made elsewhere, and
    /// answers its object id.
    ///
    /// The commit is made in a scratch clone and pushed with its tag. When `spec` names no branch, it
    /// continues the remote's default branch. When it names a branch the remote has, it continues that
    /// branch from the remote's tip. When it names a branch the remote lacks, the branch is created from the
    /// tip of the remote's default branch, the commit is made on it, and it is pushed as a new branch of the
    /// remote: the default branch does not move.
    pub fn push_to_remote(&self, name: &str, spec: &CommitSpec) -> Result<String, GitError> {
        self.base(&self.remote_path(name)?)?;
        let scratch_parent = self.scratch_parent()?;
        leads_downward(spec)?;

        let scratch = scratch_in(scratch_parent)?;
        let clone = scratch.path().join("clone");
        self.clone_remote(name, &clone)?;
        if let Some(branch) = &spec.branch {
            let theirs = format!("refs/remotes/origin/{branch}");
            if self.has_ref(&clone, &theirs)?
                && !self.has_ref(&clone, &format!("refs/heads/{branch}"))?
            {
                self.run(
                    &clone,
                    &["branch", "--quiet", "--no-track", branch, &theirs],
                )?;
            }
        }
        let id = self.commit(&clone, spec)?;
        let tag = spec.tag.as_ref().map(|tag| format!("refs/tags/{tag}"));
        let mut args = vec!["push", "--quiet", "origin", "HEAD"];
        args.extend(tag.as_deref());
        self.run(&clone, &args)?;
        remove(scratch)?;
        Ok(id)
    }

    /// Puts `HEAD` of the repository at `repo` on `branch`, creating the branch from `HEAD` when absent.
    fn switch_to(&self, repo: &Path, branch: &str) -> Result<(), GitError> {
        let current = self.observe(self.tree.env(), repo, &["symbolic-ref", "--quiet", "HEAD"])?;
        let wanted = format!("refs/heads/{branch}");
        if current.exit == Exit::Code(0)
            && String::from_utf8_lossy(&current.stdout).trim_end() == wanted
        {
            return Ok(());
        }
        if self.has_ref(repo, &wanted)? {
            self.run(repo, &["checkout", "--quiet", branch])?;
        } else {
            self.run(repo, &["checkout", "--quiet", "-b", branch])?;
        }
        Ok(())
    }

    /// Whether the repository at `repo` has the ref `name`, given in full.
    fn has_ref(&self, repo: &Path, name: &str) -> Result<bool, GitError> {
        let args = ["show-ref", "--verify", "--quiet", name];
        let observation = self.observe(self.tree.env(), repo, &args)?;
        Ok(observation.exit == Exit::Code(0))
    }

    /// As [`Git::run`], answering the bytes git wrote as they are, for a caller that must tell output that is
    /// not UTF-8 from output that is.
    pub(crate) fn run_bytes(&self, cwd: &Path, args: &[&str]) -> Result<Vec<u8>, GitError> {
        let observation = self.observe(self.tree.env(), cwd, args)?;
        if observation.exit != Exit::Code(0) {
            return Err(GitError::Failed {
                args: owned(args),
                stderr: String::from_utf8_lossy(&observation.stderr).into_owned(),
                exit: observation.exit,
            });
        }
        Ok(observation.stdout)
    }

    /// As [`Git::run`], under `env` in place of the pinned environment of the tree.
    fn run_under(&self, env: PinnedEnv, cwd: &Path, args: &[&str]) -> Result<String, GitError> {
        let observation = self.observe(env, cwd, args)?;
        if observation.exit != Exit::Code(0) {
            return Err(GitError::Failed {
                args: owned(args),
                stderr: String::from_utf8_lossy(&observation.stderr).into_owned(),
                exit: observation.exit,
            });
        }
        Ok(String::from_utf8_lossy(&observation.stdout).into_owned())
    }

    /// Runs `git <args>` in `cwd` under `env` and answers what was seen of it, whatever its exit.
    fn observe(&self, env: PinnedEnv, cwd: &Path, args: &[&str]) -> Result<Observation, GitError> {
        self.base(cwd)?;
        let mut invocation = self.tree.invocation(&self.executable);
        invocation.args = args.iter().map(Into::into).collect();
        invocation.cwd = cwd.to_path_buf();
        invocation.env = env;
        invoke(invocation).map_err(|failure| GitError::NotObserved {
            args: owned(args),
            failure,
        })
    }

    /// Writes `content` to the file `relative` under `base`, a directory of the tree, creating the
    /// directories that lead to it.
    pub(crate) fn write_file(
        &self,
        base: &Path,
        relative: &str,
        content: &[u8],
    ) -> Result<(), GitError> {
        self.write(self.file(base, relative, Use::Written)?, content)
    }

    /// Writes `content` to the file `path` when it stays in the tree and is no entry of the root, creating
    /// the directories that lead to it. The file is a new one: an entry where `path` lands is removed, not
    /// opened, and the file is created only where no entry is, so nothing that shares the content of the
    /// old entry is written to.
    fn write(&self, path: PathBuf, content: &[u8]) -> Result<(), GitError> {
        let lands = self.landing(&path, Use::Written)?;
        let written = match lands.parent() {
            Some(parent) => std::fs::create_dir_all(parent),
            None => Ok(()),
        }
        .and_then(|()| match std::fs::remove_file(&lands) {
            Err(error) if error.kind() != io::ErrorKind::NotFound => Err(error),
            _ => Ok(()),
        })
        .and_then(|()| std::fs::File::create_new(&lands))
        .and_then(|mut file| file.write_all(content));
        written.map_err(|error| GitError::File { path, error })
    }

    /// The path of the file `relative` of the directory `base`, when `relative` leads downward and the tree
    /// confines the file as what it is used as.
    fn file(&self, base: &Path, relative: &str, used_as: Use) -> Result<PathBuf, GitError> {
        let path = base.join(within(relative)?);
        self.landing(&path, used_as)?;
        Ok(path)
    }

    /// Fails unless the git directory and the common directory of the repository at `repo`, a path that
    /// stays in the tree, stay in the tree as git names them, and answers the two, the git directory first.
    /// Git only reads.
    pub(crate) fn git_directories(&self, repo: &Path) -> Result<(PathBuf, PathBuf), GitError> {
        let arguments = [
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ];
        let answer = self.run(repo, &arguments)?;
        let mut named = answer.lines().map(Path::new);
        // One line for each directory asked for: a path that holds a line break is not told apart.
        let (Some(git_directory), Some(common_directory), None) =
            (named.next(), named.next(), named.next())
        else {
            return Err(GitError::Unconfined {
                path: repo.to_path_buf(),
            });
        };
        self.base(git_directory)?;
        self.base(common_directory)?;
        Ok((git_directory.to_path_buf(), common_directory.to_path_buf()))
    }

    /// Fails unless `repo`, a path that stays in the tree, is the top of a git work tree below the root of
    /// the tree. Git only reads.
    fn top_of_work_tree(&self, repo: &Path) -> Result<(), GitError> {
        let unconfined = || GitError::Unconfined {
            path: repo.to_path_buf(),
        };
        if repo.canonicalize().ok().as_deref() == Some(self.tree.root()) {
            return Err(unconfined());
        }
        self.work_tree_top(repo)
    }

    /// Fails unless `repo`, a path that stays in the tree, is the top of the work tree git reports for the
    /// repository there. Git only reads, and of the work tree it reports it resolves the path alone.
    pub(crate) fn work_tree_top(&self, repo: &Path) -> Result<(), GitError> {
        let answer = self.run(repo, &["rev-parse", "--show-toplevel"])?;
        let top = answer.strip_suffix('\n').unwrap_or(&answer);
        if repo.canonicalize().ok().as_deref() != Some(Path::new(top)) {
            return Err(GitError::Unconfined {
                path: repo.to_path_buf(),
            });
        }
        Ok(())
    }

    /// `path` as text, when it may be a repository or a working directory of the tree.
    fn base<'p>(&self, path: &'p Path) -> Result<&'p str, GitError> {
        self.confined(path, Use::Base)
    }

    /// `path` as text, when a repository may be created there.
    fn destination<'p>(&self, path: &'p Path) -> Result<&'p str, GitError> {
        self.confined(path, Use::Destination)
    }

    /// `path` as text, when the tree confines it as what it is used as.
    fn confined<'p>(&self, path: &'p Path, used_as: Use) -> Result<&'p str, GitError> {
        self.landing(path, used_as)?;
        path.to_str().ok_or_else(|| GitError::Unconfined {
            path: path.to_path_buf(),
        })
    }

    /// Where `path` lands, every link the tree follows for what it is used as resolved, when the tree
    /// confines it as that.
    fn landing(&self, path: &Path, used_as: Use) -> Result<PathBuf, GitError> {
        self.tree
            .landing(path, used_as)
            .ok_or_else(|| GitError::Unconfined {
                path: path.to_path_buf(),
            })
    }

    /// The path of the bare repository of the remote `name`.
    fn remote_path(&self, name: &str) -> Result<PathBuf, GitError> {
        let file = format!("{}.git", segment(name)?);
        Ok(self.tree.root().join("remotes").join(file))
    }

    /// The `tmp` directory of the tree, where scratch directories are made, when it stays in the tree.
    fn scratch_parent(&self) -> Result<PathBuf, GitError> {
        let parent = self.tree.root().join("tmp");
        self.base(&parent)?;
        Ok(parent)
    }
}

/// The arguments that create a repository at `path` with `HEAD` on `branch`. Both formats are named, so
/// that a git with other defaults makes the same object ids and the same configuration.
fn init_arguments<'a>(bare: bool, branch: &'a str, path: &'a str) -> Vec<&'a str> {
    let mut arguments = vec![
        "init",
        "--quiet",
        "--object-format=sha1",
        "--ref-format=files",
    ];
    if bare {
        arguments.push("--bare");
    }
    arguments.extend(["-b", branch, path]);
    arguments
}

/// A directory of its own in `parent`, removed when the value is dropped.
fn scratch_in(parent: PathBuf) -> Result<tempfile::TempDir, GitError> {
    tempfile::Builder::new()
        .prefix("fixture-")
        .tempdir_in(&parent)
        .map_err(|error| GitError::File {
            path: parent,
            error,
        })
}

/// Fails when a file `spec` writes or deletes is named by a path that does not lead downward.
fn leads_downward(spec: &CommitSpec) -> Result<(), GitError> {
    let written = spec.files.iter().map(|(relative, _)| relative);
    for relative in written.chain(&spec.delete) {
        within(relative)?;
    }
    Ok(())
}

/// The first executable `git` in a directory that `path`, a `PATH` value, names outside the tree at `root`.
fn host_git(path: &OsStr, root: &Path) -> Option<PathBuf> {
    std::env::split_paths(path)
        .filter(|directory| directory.is_absolute() && !directory.starts_with(root))
        .map(|directory| directory.join("git"))
        .find(|git| {
            std::fs::metadata(git).is_ok_and(|metadata| {
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            })
        })
}

/// The URL every clone of the remote `name` has as its origin.
fn remote_url(name: &str) -> Result<String, GitError> {
    Ok(format!("{FIXTURE_URL}{}.git", segment(name)?))
}

/// `name`, when it is one path segment that names a directory entry.
fn segment(name: &str) -> Result<&str, GitError> {
    let mut parts = Path::new(name).components();
    match (parts.next(), parts.next()) {
        (Some(Component::Normal(part)), None) if part == name => Ok(name),
        _ => Err(GitError::Unconfined { path: name.into() }),
    }
}

/// `relative` as a path, when it is not empty and every part of it leads downward.
fn within(relative: &str) -> Result<&Path, GitError> {
    let path = Path::new(relative);
    let mut parts = path.components().peekable();
    if parts.peek().is_none() || !parts.all(|part| matches!(part, Component::Normal(_))) {
        return Err(GitError::Unconfined {
            path: path.to_path_buf(),
        });
    }
    Ok(path)
}

/// Removes a scratch directory now, so that a failure to remove it is reported.
fn remove(scratch: tempfile::TempDir) -> Result<(), GitError> {
    let path = scratch.path().to_path_buf();
    scratch
        .close()
        .map_err(|error| GitError::File { path, error })
}

fn owned(args: &[&str]) -> Vec<String> {
    args.iter().map(|argument| (*argument).to_owned()).collect()
}

#[cfg(test)]
mod tests {
    use std::ffi::{OsStr, OsString};
    use std::fs::Permissions;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

    use super::host_git;
    use crate::tree::TempTree;

    /// A file named `git` in `directory` with the permission bits `mode`.
    fn file_named_git(directory: &Path, mode: u32) -> PathBuf {
        let file = directory.join("git");
        std::fs::write(&file, "#!/bin/sh\n").expect("a file");
        std::fs::set_permissions(&file, Permissions::from_mode(mode)).expect("its permissions");
        file
    }

    fn path_of(directories: &[&Path]) -> OsString {
        std::env::join_paths(directories).expect("a PATH")
    }

    #[test]
    fn git_is_the_first_executable_of_that_name_in_a_directory_outside_the_tree() {
        let tree = TempTree::new().expect("an isolated tree");
        let host = TempTree::new().expect("a second tree, for directories outside the first");
        let stubs = tree.root().join("bin");
        file_named_git(&stubs, 0o755);
        let without = host.root().join("tmp");
        let not_executable = host.root().join("cache");
        file_named_git(&not_executable, 0o644);
        let first = host.root().join("bin");
        let second = host.root().join("calls");
        let found = file_named_git(&first, 0o755);
        file_named_git(&second, 0o755);

        let none = path_of(&[&stubs, &without, &not_executable, Path::new("relative")]);
        let some = path_of(&[&stubs, &without, &not_executable, &first, &second]);

        assert_eq!(host_git(OsStr::new(""), tree.root()), None);
        assert_eq!(host_git(&none, tree.root()), None);
        assert_eq!(host_git(&some, tree.root()), Some(found));
    }
}
