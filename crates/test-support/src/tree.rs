//! The isolated tree: the temporary directory every process-level test runs in.

use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use crate::env::{self, PinnedEnv};
use crate::process::Invocation;

/// Names the directory trees are created in; the system temporary directory when unset.
const PARENT_VARIABLE: &str = "WINTER_TEST_TMPDIR";
/// Set to `1`, keeps every tree when it is dropped.
const KEEP_VARIABLE: &str = "WINTER_TEST_KEEP";

/// The directories of a tree, relative to its root.
const DIRECTORIES: [&str; 8] = [
    "ws",
    "remotes",
    "home/.config",
    "cache",
    "tmp",
    "bin",
    "calls",
    "git-template",
];

/// The file that makes a directory a workspace, relative to that directory.
const WORKSPACE_CONFIGURATION: &str = ".winter/config.toml";

/// The entry that makes a directory a git work tree, relative to that directory.
const GIT_ENTRY: &str = ".git";

/// How long an invocation made from a tree may run.
const DEADLINE: Duration = Duration::from_secs(30);

/// What a path a fixture helper is given is used as, which decides where in the tree it may lie.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Use {
    /// A repository, a working directory, or a directory files are written under: at the root of the tree or
    /// under it.
    Base,
    /// Where a repository is created: under the root of the tree, never the root itself, and neither `.git`
    /// of the root nor anything under it. The root must never hold `.git`, since git stops its walk upward
    /// there.
    Destination,
    /// A [`Use::Destination`] that git fills itself, entry by entry: it is absent, or a directory that holds
    /// nothing. Git would write into entries already there, and a link among them would carry its writes
    /// out of the tree.
    Created,
    /// A file that is written, every link of its path followed: under an entry the root already holds,
    /// never an entry of the root itself and never under its `.git`. The entries of the root are the tree's
    /// own, its git configuration and the directories of its layout, and a write adds none to them: `.git`
    /// or `.winter` there would make the root a repository or a workspace. Where it lands is where the file
    /// is put, as a new file in place of whatever entry was there.
    Written,
    /// A directory entry that is removed, every link of its path followed but the entry itself, which is
    /// removed and never followed: under an entry the root already holds, never an entry of the root itself
    /// and never under its `.git`.
    Removed,
}

/// A temporary directory with the layout a test's workspace, remotes, and home live in. No walk upward from
/// inside it reaches a workspace or a git repository, and it is removed when the value is dropped.
///
/// The tree is where the fixture helpers are held: it checks each path they are given and answers where
/// the path lands. That keeps a fixture built from honest inputs inside the tree and off its top-level
/// entries, and refuses the ways out the helpers name. The tree is no sandbox: a test that makes itself a
/// way out by other means is not stopped by it.
///
/// A tree that cannot be removed is named on stderr with the error, and the drop panics unless the thread is
/// already panicking: what is left behind is then the trace of something still writing into the tree, or of a
/// directory a child left unwritable.
#[derive(Debug)]
pub struct TempTree {
    /// Canonical, and made of the characters `A-Z a-z 0-9 / . _ -` alone.
    root: String,
    /// How many commits the fixture helpers have made in the tree.
    commits: Mutex<u64>,
}

impl TempTree {
    /// Creates a tree in the directory `WINTER_TEST_TMPDIR` names, or in the system temporary directory.
    ///
    /// Fails, creating nothing, when that directory or one above it holds a workspace configuration, holds
    /// `.git`, or is a git directory, or when its canonical path has a character outside `[A-Za-z0-9/._-]`.
    pub fn new() -> io::Result<Self> {
        let parent = match std::env::var_os(PARENT_VARIABLE) {
            Some(parent) => PathBuf::from(parent),
            None => std::env::temp_dir(),
        }
        .canonicalize()?;
        refuse_workspace(&parent)?;
        refuse_git_repository(&parent)?;
        within_charset(&parent)?;

        let directory = tempfile::Builder::new()
            .prefix("winter-workspace-test-")
            .tempdir_in(&parent)?;
        let canonical = directory.path().canonicalize()?;
        let root = within_charset(&canonical)?;
        for name in DIRECTORIES {
            std::fs::create_dir_all(canonical.join(name))?;
        }
        std::fs::write(canonical.join("gitconfig"), env::gitconfig(root))?;

        let root = root.to_owned();
        let _ = directory.keep();
        Ok(Self {
            root,
            commits: Mutex::new(0),
        })
    }

    /// The root of the tree: a canonical path.
    pub fn root(&self) -> &Path {
        Path::new(&self.root)
    }

    /// The workspace directory, `ws` under the root.
    pub fn workspace_dir(&self) -> PathBuf {
        self.root().join("ws")
    }

    /// The pinned environment of this tree.
    pub fn env(&self) -> PinnedEnv {
        PinnedEnv::for_root(&self.root)
    }

    /// An invocation of `executable` with no argument and no input, under the pinned environment, in the
    /// workspace directory, with a 30 s deadline.
    pub fn invocation(&self, executable: &Path) -> Invocation {
        Invocation {
            executable: executable.to_path_buf(),
            args: Vec::new(),
            cwd: self.workspace_dir(),
            env: self.env(),
            stdin: Vec::new(),
            timeout: DEADLINE,
        }
    }

    /// Where `path` lands, when it stays in the tree: it is absolute, names no parent directory, and what
    /// exists of it resolves, symbolic links followed, to the root or below it. Where it lands is that
    /// resolved part followed by the rest of `path`, and each [`Use`] but [`Use::Base`] narrows it:
    ///
    /// - a [`Use::Destination`] is spelled below the root and lands below it, and neither as spelled nor as
    ///   it lands is it `.git` of the root or under it;
    /// - a [`Use::Created`] likewise, and where it lands there is nothing, or a directory that holds nothing;
    /// - a [`Use::Written`] lands under an entry of the root that exists and is not `.git`, so it is never
    ///   an entry of the root itself and the directories made on the way to it add none;
    /// - a [`Use::Removed`] likewise, and its last component is not followed: only its parent is resolved.
    ///
    /// A relative path is refused because it would be read against the working directory of the test
    /// process. An entry exists when it is there as a link, wherever the link leads, so a link that leads
    /// nowhere resolves to nothing and is refused; the part of `path` that does not exist yet holds no link
    /// to follow.
    pub(crate) fn landing(&self, path: &Path, used_as: Use) -> Option<PathBuf> {
        let root = self.root();
        if !path.is_absolute() || path.components().any(|part| part == Component::ParentDir) {
            return None;
        }
        let followed = match used_as {
            Use::Removed => path.parent()?,
            _ => path,
        };
        let existing = followed
            .ancestors()
            .find(|ancestor| ancestor.symlink_metadata().is_ok())?;
        let resolved = existing.canonicalize().ok()?;
        let rest = path.strip_prefix(existing).ok()?;
        // Joining nothing would end the path in a separator, which names a directory.
        let lands = if rest.as_os_str().is_empty() {
            resolved.clone()
        } else {
            resolved.join(rest)
        };
        let below = lands.strip_prefix(root).ok()?;
        let depth = below.components().count();
        let in_git_entry = |below: &Path| below.starts_with(GIT_ENTRY);
        let is_destination = || {
            depth > 0
                && !in_git_entry(below)
                && path
                    .strip_prefix(root)
                    .is_ok_and(|spelled| !in_git_entry(spelled))
        };
        let stays = match used_as {
            Use::Base => true,
            Use::Destination => is_destination(),
            Use::Created => is_destination() && absent_or_empty(&lands),
            // What exists of the path ends below the root exactly when the entry of the root it lands
            // under exists.
            Use::Written | Use::Removed => depth > 1 && resolved != root && !in_git_entry(below),
        };
        stays.then_some(lands)
    }

    /// Makes one commit of the tree: runs `make` with the number of commits made in the tree so far, and
    /// counts one more when it succeeds. The count is held meanwhile, so no two commits made in one tree at
    /// once are given the same number, and a commit that was not made leaves its number to the next.
    pub(crate) fn count_commit<T, E>(
        &self,
        make: impl FnOnce(u64) -> Result<T, E>,
    ) -> Result<T, E> {
        let mut made = self.commits.lock().unwrap_or_else(PoisonError::into_inner);
        let outcome = make(*made)?;
        *made += 1;
        Ok(outcome)
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        if std::env::var_os(KEEP_VARIABLE).is_some_and(|keep| keep == "1") {
            let _ = writeln!(io::stderr(), "kept the isolated tree {}", self.root);
        } else if let Err(error) = std::fs::remove_dir_all(&self.root) {
            // Written directly as well as raised: during an unwind nothing is raised, and a test runner may
            // hold back what a panic prints.
            let _ = writeln!(
                io::stderr(),
                "the isolated tree {} could not be removed: {error}",
                self.root
            );
            if !std::thread::panicking() {
                panic!(
                    "the isolated tree {} could not be removed: {error}",
                    self.root
                );
            }
        }
    }
}

/// Whether nothing is at `path`, a path no link is left to follow in, or a directory that holds nothing.
fn absent_or_empty(path: &Path) -> bool {
    match std::fs::read_dir(path) {
        Ok(mut entries) => entries.next().is_none(),
        Err(error) => error.kind() == io::ErrorKind::NotFound,
    }
}

/// Fails when `parent`, or a directory above it, is a workspace.
fn refuse_workspace(parent: &Path) -> io::Result<()> {
    for directory in parent.ancestors() {
        let configuration = directory.join(WORKSPACE_CONFIGURATION);
        if configuration.try_exists()? {
            return Err(io::Error::other(format!(
                "no isolated tree is created in {}: {} is a workspace configuration",
                parent.display(),
                configuration.display()
            )));
        }
    }
    Ok(())
}

/// Fails when `parent`, or a directory above it, is a git work tree or a git directory, a bare repository
/// included. Git is not asked: what is looked for is what git itself looks for on its way upward.
fn refuse_git_repository(parent: &Path) -> io::Result<()> {
    for directory in parent.ancestors() {
        let entry = directory.join(GIT_ENTRY);
        let repository = if entry.symlink_metadata().is_ok() {
            entry
        } else if is_git_directory(directory) {
            directory.to_path_buf()
        } else {
            continue;
        };
        return Err(io::Error::other(format!(
            "no isolated tree is created in {}: {} is a git repository",
            parent.display(),
            repository.display()
        )));
    }
    Ok(())
}

/// Whether `directory` holds `HEAD` beside the directories `objects` and `refs`.
fn is_git_directory(directory: &Path) -> bool {
    directory.join("HEAD").symlink_metadata().is_ok()
        && directory.join("objects").is_dir()
        && directory.join("refs").is_dir()
}

/// `path` as text, when every character of it is one of `A-Z a-z 0-9 / . _ -`.
fn within_charset(path: &Path) -> io::Result<&str> {
    path.to_str()
        .filter(|text| {
            text.chars()
                .all(|character| character.is_ascii_alphanumeric() || "/._-".contains(character))
        })
        .ok_or_else(|| {
            io::Error::other(format!(
                "no isolated tree is created at {}: the path has a character outside [A-Za-z0-9/._-]",
                path.display()
            ))
        })
}
