//! The fixture of a scenario, built in an isolated tree: its remotes, the files of `files/`, its checkouts
//! and worktrees, the commit of the workspace root, its raw git calls, and the files of `dirty/`, in that
//! order.
//!
//! A description decides what is written and which git calls are made on the machine a test runs on, so
//! nothing of it is used unread. What needs no tree is settled before anything is done; what needs one is
//! asked of the tree, through the one check the git helpers use, immediately before each write and each
//! call.
//!
//! Before any directory is made and before git is started:
//!
//! - a scenario with a `[[stub]]` is refused, since nothing here captures the calls of a program;
//! - a checkout names a declared remote, and a worktree a declared checkout;
//! - a branch, a tag, a start point, and a default branch are no option and no path: none is empty, starts
//!   with `-`, `/`, or `~`, or has `.` or `..` as a segment;
//! - a path stays at or below its base once `.` and `..` are read out of it;
//! - where a checkout or a worktree is created has no `.git` segment;
//! - a file a commit writes or deletes has no `.git` segment, which would put it into the git directory of
//!   the repository the commit is made in;
//! - the first argument of a raw git call is no option, and no argument of it is a URL other than a
//!   fixture remote's, names a transport helper, or is a short option directly followed by an absolute
//!   path, a home directory, or a parent directory.
//!
//! On the tree, each immediately before the write or the call it guards, and with nothing done after one
//! fails:
//!
//! - where a checkout or a worktree is created, the checkout a worktree is added to, and the working
//!   directory of a raw git call stay in the tree with every symbolic link followed;
//! - where a checkout or a worktree is created lands inside no `.git` and inside no remote of the fixture;
//! - the repository git finds from the working directory of a raw git call keeps its git directory and its
//!   common directory in the tree;
//! - no argument of a raw git call, read as a path from its working directory, no part of one after a `=`,
//!   and no value that directly follows the letters of a short option leads out of the tree through a
//!   symbolic link;
//! - an entry of `files/` or `dirty/` lands below an entry of the root, inside no `.git` and inside no
//!   remote of the fixture, and a symbolic link among them is copied as a link and never followed.
//!
//! The git directories known here are those two kinds: an entry named `.git`, and the bare repository of a
//! declared remote. One a raw git call makes under another name, as `init --bare` does, is not known.
//!
//! Every argument of a raw git call is read as an address and as a path, whatever git takes it for, so
//! the message of a commit that reads as either is refused as well.
//!
//! That is where it ends. The arguments of a raw git call are otherwise passed to git as written. None of
//! these is recognised: a host in the form `host:path`, for which git starts `ssh`; an option or a
//! configuration that makes git run a program, which then runs and writes where it likes; a path reached
//! through a syntax of git's own. A description is kept inside its tree; it is not run in a sandbox.

use std::ffi::OsString;
use std::fmt;
use std::io;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use super::model::{Malformed, Scenario};
use crate::git::{CommitSpec, Git, GitError};
use crate::tree::{TempTree, Use};

/// The token a file of `files/` or `dirty/` holds in place of the root of the tree.
pub(super) const ROOT_TOKEN: &[u8] = b"<ROOT>";

/// The message of the one commit of the workspace root repository.
const ROOT_COMMIT_MESSAGE: &str = "Workspace root";

/// The branch of the workspace root repository.
const ROOT_BRANCH: &str = "main";

/// The start of the URL of every fixture remote, which the tree maps to its own `remotes` directory.
const FIXTURE_URL: &str = "https://fixture.invalid/";

/// The entry that holds a repository's git directory.
pub(super) const GIT_ENTRY: &str = ".git";

/// The entry of the root of the tree that holds the bare repository `<name>.git` of every remote.
pub(super) const REMOTES_DIRECTORY: &str = "remotes";

/// Why the fixture of a scenario was not built.
#[derive(Debug)]
pub enum FixtureError {
    /// The description may not be built as written: it has a `[[stub]]`, names a remote or a checkout it
    /// does not declare, or names something that does not stay inside the tree. The write or the call that
    /// would have used the value was not made, and none after it.
    Malformed(Malformed),
    /// The isolated tree could not be created.
    Tree(io::Error),
    /// A step of the build failed: git refused a call, or a file could not be read or written.
    Failed {
        /// The scenario id.
        scenario: String,
        /// The key of the description whose entry was being built.
        key: String,
        error: Box<GitError>,
    },
}

impl fmt::Display for FixtureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Malformed(malformed) => malformed.fmt(f),
            Self::Tree(error) => write!(f, "the isolated tree could not be created: {error}"),
            Self::Failed {
                scenario,
                key,
                error,
            } => write!(
                f,
                "the fixture of scenario {scenario} was not built at `{key}`: {error}"
            ),
        }
    }
}

impl std::error::Error for FixtureError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Malformed(malformed) => Some(malformed),
            Self::Tree(error) => Some(error),
            Self::Failed { error, .. } => Some(error.as_ref()),
        }
    }
}

/// Builds the fixture of `scenario` in a tree of its own and answers the tree.
///
/// A description that is refused before anything is done, as the module describes, creates no tree. A build
/// that fails later removes its tree with the error.
pub fn build(scenario: &Scenario) -> Result<TempTree, FixtureError> {
    let plan = Plan::of(scenario).map_err(FixtureError::Malformed)?;
    let tree = TempTree::new().map_err(FixtureError::Tree)?;
    Builder::new(scenario, &tree, &plan)?.build(&plan)?;
    Ok(tree)
}

/// Builds the fixture of `scenario` in `tree`, a tree nothing was built in yet.
///
/// A description that is refused before anything is done, as the module describes, leaves the tree as it
/// was. A build that fails later leaves what it had built until then.
pub fn build_in(scenario: &Scenario, tree: &TempTree) -> Result<(), FixtureError> {
    let plan = Plan::of(scenario).map_err(FixtureError::Malformed)?;
    Builder::new(scenario, tree, &plan)?.build(&plan)
}

/// What a description asks to be built, read: every path with `.` and `..` read out of it, every value
/// that becomes a git argument known to be no option, every reference resolved.
struct Plan {
    remotes: Vec<PlannedRemote>,
    checkouts: Vec<PlannedCheckout>,
    worktrees: Vec<PlannedWorktree>,
    calls: Vec<PlannedCall>,
    /// The content of `.gitignore` of the workspace root repository, when the workspace directory is one;
    /// the inner value is `None` when no such file is written.
    root_repository: Option<Option<String>>,
}

struct PlannedRemote {
    place: String,
    name: String,
    default_branch: String,
    commits: Vec<CommitSpec>,
}

struct PlannedCheckout {
    place: String,
    remote: String,
    /// Relative to the workspace directory; empty for the directory itself.
    path: String,
}

struct PlannedWorktree {
    place: String,
    checkout: String,
    path: String,
    branch: String,
    start: String,
}

struct PlannedCall {
    place: String,
    cwd: String,
    args: Vec<String>,
}

impl Plan {
    /// Reads the fixture of `scenario`, starting nothing and creating nothing.
    fn of(scenario: &Scenario) -> Result<Self, Malformed> {
        let refuse = |key: &str, place: &str, reason: String| Malformed {
            scenario: scenario.id.clone(),
            key: key.to_owned(),
            reason: format!("in {place}: {reason}"),
        };
        if !scenario.stubs.is_empty() {
            return Err(refuse(
                "stub",
                "[[stub]] 1",
                "the fixture builder captures no program call, so a scenario with a stub is not built"
                    .to_owned(),
            ));
        }
        let path = |key: &str, place: &str, value: &str| {
            normalized(value).ok_or_else(|| {
                refuse(
                    key,
                    place,
                    format!("{value:?} is absolute or leads above its base"),
                )
            })
        };
        let name = |key: &str, place: &str, value: &str| match refused_name(value) {
            Some(reason) => Err(refuse(key, place, reason)),
            None => Ok(value.to_owned()),
        };
        let destination = |place: &str, value: &str| {
            let destination = path("path", place, value)?;
            if destination.split('/').any(is_git_entry) {
                return Err(refuse(
                    "path",
                    place,
                    format!("{value:?} names a git directory or a place inside one"),
                ));
            }
            Ok(destination)
        };
        let fixture = &scenario.fixture;

        let mut remotes = Vec::new();
        for (index, remote) in fixture.remotes.iter().enumerate() {
            let place = format!("[[fixture.remote]] {}", index + 1);
            let mut commits = Vec::new();
            for (index, commit) in remote.commits.iter().enumerate() {
                let place = format!("[[fixture.remote.commit]] {}", index + 1);
                let file = |key: &str, value: &str| {
                    let file = path(key, &place, value)?;
                    if file.is_empty() || file.split('/').any(is_git_entry) {
                        return Err(refuse(
                            key,
                            &place,
                            format!("{value:?} names the repository itself or its git directory"),
                        ));
                    }
                    Ok(file)
                };
                let mut files = Vec::new();
                for (relative, content) in &commit.files {
                    files.push((file("files", relative)?, content.clone().into_bytes()));
                }
                let mut delete = Vec::new();
                for relative in &commit.delete {
                    delete.push(file("delete", relative)?);
                }
                commits.push(CommitSpec {
                    message: commit.message.clone(),
                    files,
                    delete,
                    branch: commit
                        .branch
                        .as_deref()
                        .map(|branch| name("branch", &place, branch))
                        .transpose()?,
                    tag: commit
                        .tag
                        .as_deref()
                        .map(|tag| name("tag", &place, tag))
                        .transpose()?,
                });
            }
            if !is_remote_name(&remote.name) {
                return Err(refuse(
                    "name",
                    &place,
                    format!(
                        "{:?} is not one segment, which matches [A-Za-z0-9][A-Za-z0-9._-]*",
                        remote.name
                    ),
                ));
            }
            if remotes
                .iter()
                .any(|known: &PlannedRemote| known.name == remote.name)
            {
                return Err(refuse(
                    "name",
                    &place,
                    format!("the remote {:?} is declared twice", remote.name),
                ));
            }
            remotes.push(PlannedRemote {
                default_branch: name("default_branch", &place, &remote.default_branch)?,
                name: remote.name.clone(),
                commits,
                place,
            });
        }

        let mut checkouts = Vec::new();
        for (index, checkout) in fixture.checkouts.iter().enumerate() {
            let place = format!("[[fixture.checkout]] {}", index + 1);
            if !remotes.iter().any(|remote| remote.name == checkout.remote) {
                return Err(refuse(
                    "remote",
                    &place,
                    format!("{:?} is not a declared remote", checkout.remote),
                ));
            }
            checkouts.push(PlannedCheckout {
                remote: checkout.remote.clone(),
                path: destination(&place, &checkout.path)?,
                place,
            });
        }

        let mut worktrees = Vec::new();
        for (index, worktree) in fixture.worktrees.iter().enumerate() {
            let place = format!("[[fixture.worktree]] {}", index + 1);
            let checkout = path("checkout", &place, &worktree.checkout)?;
            if !checkouts.iter().any(|known| known.path == checkout) {
                return Err(refuse(
                    "checkout",
                    &place,
                    format!("{:?} is not a declared checkout", worktree.checkout),
                ));
            }
            worktrees.push(PlannedWorktree {
                checkout,
                path: destination(&place, &worktree.path)?,
                branch: name("branch", &place, &worktree.branch)?,
                start: name("start", &place, &worktree.start)?,
                place,
            });
        }

        let mut calls = Vec::new();
        for (index, call) in fixture.git.iter().enumerate() {
            let place = format!("[[fixture.git]] {}", index + 1);
            if let Some(reason) = refused_arguments(&call.args) {
                return Err(refuse("args", &place, reason));
            }
            calls.push(PlannedCall {
                cwd: path("cwd", &place, &call.cwd)?,
                args: call.args.clone(),
                place,
            });
        }

        Ok(Self {
            remotes,
            checkouts,
            worktrees,
            calls,
            root_repository: fixture.workspace_git.then(|| fixture.gitignore.clone()),
        })
    }
}

/// Builds one plan in one tree.
struct Builder<'a> {
    scenario: &'a Scenario,
    tree: &'a TempTree,
    git: Git<'a>,
    /// The workspace directory of the tree.
    workspace: PathBuf,
    /// The bare repository of every remote of the plan.
    remotes: Vec<PathBuf>,
}

impl<'a> Builder<'a> {
    fn new(scenario: &'a Scenario, tree: &'a TempTree, plan: &Plan) -> Result<Self, FixtureError> {
        let git = Git::new(tree).map_err(|error| FixtureError::Failed {
            scenario: scenario.id.clone(),
            key: "fixture".to_owned(),
            error: Box::new(error),
        })?;
        Ok(Self {
            scenario,
            tree,
            git,
            workspace: tree.workspace_dir(),
            remotes: plan
                .remotes
                .iter()
                .map(|remote| {
                    tree.root()
                        .join(REMOTES_DIRECTORY)
                        .join(format!("{}.git", remote.name))
                })
                .collect(),
        })
    }

    fn build(&self, plan: &Plan) -> Result<(), FixtureError> {
        // A repository is created only where nothing is yet, so the workspace root repository comes
        // first; its one commit is made where the order of the build has it.
        if plan.root_repository.is_some() {
            self.git
                .init(&self.workspace, ROOT_BRANCH)
                .map_err(|error| self.failed("workspace_git", "[fixture]", error))?;
        }
        for remote in &plan.remotes {
            self.git
                .bare_remote(&remote.name, &remote.default_branch, &remote.commits)
                .map_err(|error| self.failed("name", &remote.place, error))?;
        }
        self.copy("files")?;
        for checkout in &plan.checkouts {
            self.checkout(checkout)?;
        }
        for worktree in &plan.worktrees {
            self.worktree(worktree)?;
        }
        if let Some(gitignore) = &plan.root_repository {
            let spec = CommitSpec {
                message: ROOT_COMMIT_MESSAGE.to_owned(),
                files: gitignore
                    .iter()
                    .map(|content| (".gitignore".to_owned(), content.clone().into_bytes()))
                    .collect(),
                ..CommitSpec::default()
            };
            self.git
                .commit(&self.workspace, &spec)
                .map_err(|error| self.failed("workspace_git", "[fixture]", error))?;
        }
        for call in &plan.calls {
            self.call(call)?;
        }
        self.copy("dirty")
    }

    fn checkout(&self, checkout: &PlannedCheckout) -> Result<(), FixtureError> {
        let place = &checkout.place;
        let destination = self.in_workspace(&checkout.path);
        self.created(place, &destination)?;
        self.git
            .clone_remote(&checkout.remote, &destination)
            .map_err(|error| self.failed("remote", place, error))
    }

    fn worktree(&self, worktree: &PlannedWorktree) -> Result<(), FixtureError> {
        let place = &worktree.place;
        let repository = self.in_workspace(&worktree.checkout);
        let destination = self.in_workspace(&worktree.path);
        self.stays("checkout", place, &repository, Use::Base)?;
        self.created(place, &destination)?;
        self.git
            .add_worktree(&repository, &destination, &worktree.branch, &worktree.start)
            .map_err(|error| {
                // Git refused: the start point is at fault when the checkout has no such commit.
                let commit = format!("{}^{{commit}}", worktree.start);
                let unknown = matches!(error, GitError::Failed { .. })
                    && self
                        .git
                        .run(&repository, &["rev-parse", "--verify", "--quiet", &commit])
                        .is_err();
                let key = if unknown { "start" } else { "checkout" };
                self.failed(key, place, error)
            })
    }

    /// Fails unless `destination`, where a checkout or a worktree is created, stays in the tree and lands
    /// in no git directory.
    fn created(&self, place: &str, destination: &Path) -> Result<(), FixtureError> {
        let lands = self.stays("path", place, destination, Use::Destination)?;
        if self.in_git_directory(&lands) {
            return Err(self.refuse(
                "path",
                place,
                format!("{} lands in a git directory", destination.display()),
            ));
        }
        Ok(())
    }

    /// Makes one raw git call, once its working directory, the repository git finds from there, and each
    /// of its arguments read as a path stay in the tree.
    fn call(&self, call: &PlannedCall) -> Result<(), FixtureError> {
        let place = &call.place;
        let cwd = self.in_workspace(&call.cwd);
        let lands = self.stays("cwd", place, &cwd, Use::Base)?;
        self.repository_stays(place, &cwd, &lands)?;
        for argument in &call.args {
            for part in argument.split('=').chain(short_option_values(argument)) {
                if self.tree.landing(&cwd.join(part), Use::Base).is_none() {
                    return Err(self.refuse(
                        "args",
                        place,
                        format!(
                            "{argument:?} reads as a path that does not stay inside the fixture"
                        ),
                    ));
                }
            }
        }
        let arguments: Vec<&str> = call.args.iter().map(String::as_str).collect();
        self.git
            .run(&cwd, &arguments)
            .map_err(|error| self.failed("args", place, error))?;
        Ok(())
    }

    /// Fails unless the repository git finds from `cwd`, a directory of the tree that lands at `lands`,
    /// keeps its git directory and its common directory in the tree. Where git finds none, no `.git` entry
    /// lies between the directory and the root of the tree: one git cannot read is one it may still write
    /// through. Git only reads.
    fn repository_stays(&self, place: &str, cwd: &Path, lands: &Path) -> Result<(), FixtureError> {
        let outside = || {
            self.refuse(
                "cwd",
                place,
                format!(
                    "the repository at {} does not stay inside the fixture",
                    cwd.display()
                ),
            )
        };
        let arguments = [
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ];
        match self.git.run(cwd, &arguments) {
            Ok(answer) => {
                let mut named = answer.lines().map(Path::new);
                let (Some(git_directory), Some(common_directory), None) =
                    (named.next(), named.next(), named.next())
                else {
                    return Err(outside());
                };
                for directory in [git_directory, common_directory] {
                    if self.tree.landing(directory, Use::Base).is_none() {
                        return Err(outside());
                    }
                }
                Ok(())
            }
            Err(GitError::Failed { .. }) => {
                let found = lands
                    .ancestors()
                    .take_while(|directory| directory.starts_with(self.tree.root()))
                    .any(|directory| directory.join(GIT_ENTRY).symlink_metadata().is_ok());
                if found { Err(outside()) } else { Ok(()) }
            }
            Err(error) => Err(self.failed("cwd", place, error)),
        }
    }

    /// Copies the directory `name` of the scenario directory, `files` or `dirty`, into the workspace
    /// directory, when the scenario has it.
    fn copy(&self, name: &str) -> Result<(), FixtureError> {
        let source = self.scenario.dir.join(name);
        match source.symlink_metadata() {
            Ok(metadata) if metadata.is_dir() => self.copy_directory(name, &source, ""),
            Ok(_) => Err(self.refuse(
                name,
                &format!("{name}/"),
                "it is not a directory".to_owned(),
            )),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(self.unread(name, &source, error)),
        }
    }

    /// Copies the entries of `source`, the directory `relative` of the directory `name` of the scenario,
    /// in the byte order of their names. No link is followed: one is copied as a link.
    fn copy_directory(
        &self,
        name: &str,
        source: &Path,
        relative: &str,
    ) -> Result<(), FixtureError> {
        let place = format!("{name}/");
        let unread = |path: &Path, error| self.unread(name, path, error);
        let mut entries = std::fs::read_dir(source)
            .and_then(|entries| entries.collect::<io::Result<Vec<_>>>())
            .map_err(|error| unread(source, error))?;
        entries.sort_by_key(|entry| entry.file_name());

        for entry in entries {
            let from = entry.path();
            let file_name = entry.file_name();
            let Some(entry_name) = file_name.to_str() else {
                return Err(self.refuse(
                    name,
                    &place,
                    format!("the name of {} is not UTF-8", from.display()),
                ));
            };
            let relative = if relative.is_empty() {
                entry_name.to_owned()
            } else {
                format!("{relative}/{entry_name}")
            };
            let to = self.workspace.join(&relative);
            let outside = || {
                self.refuse(
                    name,
                    &place,
                    format!(
                        "{relative:?} does not stay inside the fixture, or lands in a git directory"
                    ),
                )
            };
            let kind = entry.file_type().map_err(|error| unread(&from, error))?;
            if is_git_entry(entry_name) {
                return Err(outside());
            }
            if kind.is_symlink() {
                let target = std::fs::read_link(&from).map_err(|error| unread(&from, error))?;
                let target = OsString::from_vec(self.with_root(target.as_os_str().as_bytes()));
                let lands = self.lands(&to, Use::Removed).ok_or_else(outside)?;
                match std::fs::remove_file(&lands) {
                    Err(error) if error.kind() != io::ErrorKind::NotFound => Err(error),
                    _ => Ok(()),
                }
                .and_then(|()| std::os::unix::fs::symlink(&target, &lands))
                .map_err(|error| self.unwritten(name, &place, &to, error))?;
            } else if kind.is_dir() {
                let lands = self.lands(&to, Use::Written).ok_or_else(outside)?;
                std::fs::create_dir_all(&lands)
                    .map_err(|error| self.unwritten(name, &place, &to, error))?;
                self.copy_directory(name, &from, &relative)?;
            } else if kind.is_file() {
                let content = std::fs::read(&from).map_err(|error| unread(&from, error))?;
                let executable = entry
                    .metadata()
                    .map_err(|error| unread(&from, error))?
                    .permissions()
                    .mode()
                    & 0o111
                    != 0;
                self.lands(&to, Use::Written).ok_or_else(outside)?;
                self.git
                    .write_file(&self.workspace, &relative, &self.with_root(&content))
                    .map_err(|error| self.failed(name, &place, error))?;
                if executable {
                    // The file just written is a new one, so the path lands at it and at nothing else.
                    let lands = self.lands(&to, Use::Written).ok_or_else(outside)?;
                    std::fs::set_permissions(&lands, std::fs::Permissions::from_mode(0o755))
                        .map_err(|error| self.unwritten(name, &place, &to, error))?;
                }
            } else {
                return Err(self.refuse(
                    name,
                    &place,
                    format!("{relative:?} is no file, directory, or symbolic link"),
                ));
            }
        }
        Ok(())
    }

    /// Where `path` lands when the tree confines it as what it is used as, and it lands in no git
    /// directory.
    fn lands(&self, path: &Path, used_as: Use) -> Option<PathBuf> {
        self.tree
            .landing(path, used_as)
            .filter(|lands| !self.in_git_directory(lands))
    }

    /// Whether `lands`, where a path lands in the tree, is a git directory known here or inside one: a
    /// part of it is a `.git` entry, or it is at or below the bare repository of a remote of the plan.
    fn in_git_directory(&self, lands: &Path) -> bool {
        lands
            .components()
            .any(|part| part.as_os_str().to_str().is_some_and(is_git_entry))
            || self.remotes.iter().any(|remote| lands.starts_with(remote))
    }

    /// Where `path`, the value of `key`, lands, when the tree confines it as what it is used as.
    fn stays(
        &self,
        key: &str,
        place: &str,
        path: &Path,
        used_as: Use,
    ) -> Result<PathBuf, FixtureError> {
        self.tree.landing(path, used_as).ok_or_else(|| {
            self.refuse(
                key,
                place,
                format!("{} does not stay inside the fixture", path.display()),
            )
        })
    }

    /// `content` with every root token replaced by the root of the tree.
    fn with_root(&self, content: &[u8]) -> Vec<u8> {
        let root = self.tree.root().as_os_str().as_bytes();
        let mut replaced = Vec::with_capacity(content.len());
        let mut rest = content;
        while let Some(at) = rest
            .windows(ROOT_TOKEN.len())
            .position(|window| window == ROOT_TOKEN)
        {
            replaced.extend_from_slice(&rest[..at]);
            replaced.extend_from_slice(root);
            rest = &rest[at + ROOT_TOKEN.len()..];
        }
        replaced.extend_from_slice(rest);
        replaced
    }

    /// The path `relative` of the workspace directory; the directory itself when `relative` is empty.
    fn in_workspace(&self, relative: &str) -> PathBuf {
        if relative.is_empty() {
            self.workspace.clone()
        } else {
            self.workspace.join(relative)
        }
    }

    fn refuse(&self, key: &str, place: &str, reason: String) -> FixtureError {
        FixtureError::Malformed(Malformed {
            scenario: self.scenario.id.clone(),
            key: key.to_owned(),
            reason: format!("in {place}: {reason}"),
        })
    }

    /// The error of a helper that failed while the entry at `place` was built: a path the helper found
    /// not to stay in the tree makes the description malformed at `key`.
    fn failed(&self, key: &str, place: &str, error: GitError) -> FixtureError {
        match error {
            GitError::Unconfined { path } => self.refuse(
                key,
                place,
                format!("{} does not stay inside the fixture", path.display()),
            ),
            error => FixtureError::Failed {
                scenario: self.scenario.id.clone(),
                key: key.to_owned(),
                error: Box::new(error),
            },
        }
    }

    /// The error of an entry of the scenario directory that could not be read.
    fn unread(&self, key: &str, path: &Path, error: io::Error) -> FixtureError {
        FixtureError::Failed {
            scenario: self.scenario.id.clone(),
            key: key.to_owned(),
            error: Box::new(GitError::File {
                path: path.to_path_buf(),
                error,
            }),
        }
    }

    /// The error of an entry of the tree that could not be made.
    fn unwritten(&self, key: &str, place: &str, path: &Path, error: io::Error) -> FixtureError {
        let error = GitError::File {
            path: path.to_path_buf(),
            error,
        };
        self.failed(key, place, error)
    }
}

/// `path` with `.` and `..` read out of it, segment by segment, when it is relative and no prefix of it
/// ends above its base; empty for the base itself.
fn normalized(path: &str) -> Option<String> {
    if path.is_empty() || path.starts_with('/') || path.contains('\0') {
        return None;
    }
    let mut segments = Vec::new();
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                segments.pop()?;
            }
            _ => segments.push(segment),
        }
    }
    Some(segments.join("/"))
}

/// Whether `name` names the entry that holds a git directory, in any letter case, as git itself reads it.
fn is_git_entry(name: &str) -> bool {
    name.eq_ignore_ascii_case(GIT_ENTRY)
}

/// Whether `name` is one path segment, `[A-Za-z0-9][A-Za-z0-9._-]*`.
fn is_remote_name(name: &str) -> bool {
    name.bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

/// Why `name` may not be given to git as a branch, a tag, or a start point, when it may not: git would read
/// it as an option or as a path.
fn refused_name(name: &str) -> Option<String> {
    let reason = if name.is_empty() {
        "is empty"
    } else if name.contains('\0') {
        "holds a NUL"
    } else if name.starts_with('-') {
        "starts with \"-\", which git reads as an option"
    } else if name.starts_with('/') || name.starts_with('~') {
        "names an absolute path or a home directory"
    } else if name
        .split('/')
        .any(|segment| segment == "." || segment == "..")
    {
        "has \".\" or \"..\" as a segment"
    } else {
        return None;
    };
    Some(format!("{name:?} {reason}"))
}

/// Why `args` may not be the arguments of a raw git call, when they may not, for what needs no tree: the
/// first is no subcommand but an option, or one reads as the address of a repository that is not a fixture
/// remote, whatever git would take it for.
fn refused_arguments(args: &[String]) -> Option<String> {
    match args.first() {
        None => return Some("a git call names a subcommand".to_owned()),
        Some(first) if first.is_empty() || first.starts_with('-') => {
            return Some(format!("{first:?} is not a git subcommand"));
        }
        Some(_) => {}
    }
    args.iter().find_map(|argument| {
        if argument.split('=').any(names_another_repository) {
            return Some(format!(
                "{argument:?} reads as the address of a repository that is not a fixture remote"
            ));
        }
        short_option_values(argument)
            .any(|value| {
                value.starts_with(['/', '~']) || value.split('/').next() == Some("..")
            })
            .then(|| {
                format!(
                    "{argument:?} reads as a short option followed by an absolute path, a home directory, \
                     or a parent directory"
                )
            })
    })
}

/// What may be the value of a short option in `argument`, when it is one or several short options written
/// together, as `-o<value>` and `-ko<value>` are: every rest of it that follows letters and digits alone.
fn short_option_values(argument: &str) -> impl Iterator<Item = &str> {
    let letters = match argument.strip_prefix('-') {
        Some(rest) if !rest.starts_with('-') => rest,
        _ => "",
    };
    letters
        .char_indices()
        .take_while(|(_, letter)| letter.is_ascii_alphanumeric())
        .map(|(at, _)| &letters[at + 1..])
        .filter(|value| !value.is_empty())
}

/// Whether `value` is a URL that is not under the fixture host, or names a transport helper, as
/// `<helper>::<address>` does.
fn names_another_repository(value: &str) -> bool {
    let scheme = |end: usize| {
        end > 0
            && value[..end]
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"+.-".contains(&byte))
    };
    let helper = value.find("::").is_some_and(scheme);
    let url = value.contains("://") && !value.starts_with(FIXTURE_URL);
    helper || url
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_is_read_segment_by_segment_and_never_ends_above_its_base() {
        for (path, read) in [
            (".", ""),
            ("feature-one/alpha", "feature-one/alpha"),
            ("a/../b", "b"),
            ("./a//b/", "a/b"),
            ("a/b/../..", ""),
        ] {
            assert_eq!(normalized(path).as_deref(), Some(read), "{path}");
        }
        for path in ["", "/tmp/x", "..", "../ws", "a/../../b", "a\0b"] {
            assert_eq!(normalized(path), None, "{path:?}");
        }
    }

    #[test]
    fn a_name_given_to_git_is_no_option_and_no_path() {
        for name in [
            "main",
            "feature/one",
            "origin/main",
            "HEAD~1",
            "v1.0",
            "a..b",
        ] {
            assert_eq!(refused_name(name), None, "{name}");
        }
        for name in [
            "", "-f", "--detach", "/tmp/x", "~/x", "a/../b", "./a", "..", "a\0b",
        ] {
            assert!(refused_name(name).is_some(), "{name:?}");
        }
    }

    #[test]
    fn a_raw_git_call_starts_with_a_subcommand_and_names_fixture_remotes_alone() {
        let args = |args: &[&str]| args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>();
        for accepted in [
            &["commit", "--allow-empty", "-m", "local: a change"][..],
            &["fetch", "https://fixture.invalid/alpha.git"],
            &[
                "remote",
                "set-url",
                "origin",
                "https://fixture.invalid/beta.git",
            ],
            &[
                "config",
                "remote.origin.url=https://fixture.invalid/beta.git",
            ],
            &["push", "origin", "main:side"],
            &["log", "--format=%H::%s"],
            &["log", "-n1", "-U3"],
            &["commit", "--allow-empty", "-mfix: a/b"],
        ] {
            assert_eq!(refused_arguments(&args(accepted)), None, "{accepted:?}");
        }
        for refused in [
            &[][..],
            &[""],
            &["-C", "x", "status"],
            &["--git-dir=x", "status"],
            &["fetch", "https://example.com/alpha.git"],
            &["push", "file:///tmp/x", "main"],
            &["clone", "ssh://host/x"],
            &[
                "clone",
                "--origin=x",
                "--config=remote.x.url=git://host/x",
                "x",
            ],
            &["fetch", "ext::sh -c x"],
            &["format-patch", "-o/tmp/x", "HEAD"],
            &["format-patch", "-ko/tmp/x", "HEAD"],
            &["format-patch", "-o~/x", "HEAD"],
            &["format-patch", "-o../x", "HEAD"],
            &["format-patch", "-o..", "HEAD"],
            &[
                "remote",
                "add",
                "x",
                "https://fixture.invalid.example.com/x.git",
            ],
        ] {
            assert!(refused_arguments(&args(refused)).is_some(), "{refused:?}");
        }
    }

    #[test]
    fn an_argument_is_refused_for_what_it_reads_as() {
        let args = |args: &[&str]| args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>();
        for refused in [
            &["commit", "--allow-empty", "-m", "see http://example.com"][..],
            &["commit", "--allow-empty", "-m", "std::fmt: tidy"],
            &["commit", "--allow-empty", "-mfoo/bar"],
        ] {
            let reason = refused_arguments(&args(refused)).expect("a reason");
            assert!(reason.contains(" reads as "), "{reason}");
            assert!(!reason.contains(" names "), "{reason}");
        }
    }

    #[test]
    fn a_git_entry_is_recognised_in_any_letter_case() {
        for name in [".git", ".GIT", ".Git"] {
            assert!(is_git_entry(name), "{name}");
        }
        for name in ["git", ".gitignore", ".github", "a.git"] {
            assert!(!is_git_entry(name), "{name}");
        }
    }
}
