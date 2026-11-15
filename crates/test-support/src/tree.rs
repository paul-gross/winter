//! The isolated tree: the temporary directory every process-level test runs in.

use std::io::{self, Write};
use std::path::{Path, PathBuf};
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

/// A temporary directory with the layout a test's workspace, remotes, and home live in. No walk upward from
/// inside it reaches a workspace or a git repository, and it is removed when the value is dropped.
///
/// A tree that cannot be removed is named on stderr with the error, and the drop panics unless the thread is
/// already panicking: what is left behind is then the trace of something still writing into the tree, or of a
/// directory a child left unwritable.
#[derive(Debug)]
pub struct TempTree {
    /// Canonical, and made of the characters `A-Z a-z 0-9 / . _ -` alone.
    root: String,
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
        Ok(Self { root })
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
