//! The state snapshot of an isolated tree: every entry under its root as `tree.txt`, every repository as a
//! section of `git.txt`, and under `files/` the content of each file that changed since the snapshot before.
//!
//! What a snapshot holds of the place it was taken in is bounded, not nothing. Every path is relative to
//! the root of the tree, and entries and sections are sorted by the bytes of their paths. Of an entry, the
//! listing takes its name, its kind, whether an execute bit is set, and the content of a file or the
//! target of a link: no time, owner, or inode of an entry is written. Git reads those itself to answer for
//! the `status` block, and the times and names in the `log` block are those of the commits. The root is
//! replaced by its token in the content of a file that is
//! valid UTF-8, in the target of a link, and in what git answers. Three things stay as the host has them:
//! a file that is not valid UTF-8 keeps its bytes, the root among them; a link whose target lies outside
//! the tree keeps that path; and the `config` block keeps the keys git wrote from probing the filesystem
//! when it made the repository, unless the scenario ignores them. A snapshot that differs between two
//! roots for one of these is for whoever compares the two to find.
//!
//! Taking one changes nothing. No symbolic link is followed while the tree is listed: a link is listed
//! with its target and left at that. Git is run through [`Git`], in the directory of a repository, and
//! three of the paths it reads are held to the tree, in two steps. First it names the git directory and
//! the common directory of every repository, and both must stay in the tree, every link resolved; that
//! question is asked before anything else and already makes git read the configuration of the repository
//! and resolve the paths it names. Then, for a repository that is not bare, the work tree git reports must
//! be the directory of the repository, before `status` and `stash`, the two commands that read a work
//! tree, run.
//! The one command that would refresh the index is told not to.
//!
//! Those two steps are all that is checked. A path that the configuration or a file of a repository names
//! beyond the three, a configuration file it includes, an excludes file, an alternate object store, or the
//! place of a linked work tree, is not held to the tree: git reads the first three where they are, and of
//! the last it looks for the `.git` entry and writes the path into the `worktrees` block as it is.
//!
//! The entries `bin`, `calls`, `git-template`, `cache`, and `tmp` of the root are left out of the snapshot
//! as a whole: nothing at or beneath them is listed or stored, and a repository beneath them has no
//! section. Also left out of `tree.txt` and of the stored content are what a git directory holds, the
//! directory itself being listed, and every directory named `__pycache__`, with what it holds. A git
//! directory is a directory named `.git`, a directory `<name>.git` directly under `remotes`, and every
//! directory git names as the git directory or the common directory of a repository of the tree, whatever
//! its name: the one a `.git` link or a `.git` file leads to is not entered, while the link or the file is
//! listed as the entry it is. The repositories are found and asked for these directories before an entry
//! is listed.
//!
//! A repository is a directory that holds an entry `.git`, and each of those directories under `remotes`.
//! One that git cannot read fails the snapshot with git's error; one whose git directory or common
//! directory leaves the tree, or whose work tree is not its own directory, fails it as unconfined; one that
//! lies at or beneath a git directory of the tree, which would be read and not listed, fails it as
//! unrepresentable; one for which git answers with a git directory that its `.git` entry neither is
//! nor names, as it does for an empty `.git` inside a repository, fails it as foreign; and one of which
//! git, its `.git` file, or its exclude file says something that is not UTF-8 fails it as unrepresentable.
//! A `.git` file that names the git directory of another work tree is a repository as git reads it, and
//! has its section.
//!
//! Under `files/` a path is kept in its stored form, which [`stored_path`] writes and [`tree_path`] reads:
//! a stored snapshot is committed to a repository, and no segment of a stored path may be special to it.
//! `tree.txt` and the keys of [`Snapshot::files`] hold the paths of the tree.
//!
//! An entry is listed only when its line of `tree.txt` reads back as that entry, so the listing of a
//! snapshot [`take`] answers reads back equal and no two such listings that differ are written alike. A
//! [`Snapshot`] put together by other means is written as it is, and one whose lines are not in the order
//! of their paths does not read. The blocks of `git.txt` are not as exact: a block holds lines, so what git
//! or a file says with and without its last line end, or with a carriage return before a line end, and an
//! exclude file that is absent or empty, are written alike.
//!
//! A stored snapshot is read together with the one before it, and reads only as the whole it was written
//! as: [`Snapshot::read`] fails when a file the two listings imply was stored is absent, when the content
//! of a stored file does not hash to the value on its line of `tree.txt`, when `files/` holds a file that
//! nothing implies, when a line of `tree.txt` does not follow the one before it by the bytes of its path,
//! and when `tree.txt`, `git.txt`, or `files` is not what a snapshot directory holds, a link in its place
//! included. No link is followed while a stored snapshot is read.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::fmt;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use sha1::{Digest, Sha1};

use super::fixture::{GIT_ENTRY, REMOTES_DIRECTORY};
use super::model::Compare;
use super::normalize;
use crate::git::{Git, GitError};
use crate::process::Exit;
use crate::tree::{TempTree, Use};

/// The directory of a step that holds the snapshot taken before it.
pub const BEFORE: &str = "before";

/// The directory of a step that holds the snapshot taken after it.
pub const AFTER: &str = "after";

/// The file of a snapshot directory that lists the entries of the tree.
pub(super) const TREE_FILE: &str = "tree.txt";

/// The file of a snapshot directory that holds the sections of the repositories.
pub(super) const GIT_FILE: &str = "git.txt";

/// The directory of a snapshot directory that holds the content of the changed files.
pub(super) const FILES_DIRECTORY: &str = "files";

/// The entries of the root that are no state of a scenario, left out of the snapshot as a whole.
const EXCLUDED: [&str; 5] = ["bin", "calls", "git-template", "cache", "tmp"];

/// The name of a directory an interpreter keeps compiled modules in, left out wherever it is.
const COMPILED_MODULES: &str = "__pycache__";

/// The ending of the name of a bare repository under the remotes directory.
const BARE_SUFFIX: &str = ".git";

/// What the line of a `.git` file starts with, before the git directory it names.
const GITFILE_PREFIX: &str = "gitdir: ";

/// What starts each line of a block of `git.txt`, so that no line of a block reads as a label.
const INDENT: &str = "    ";

/// The label of the block that opens a section of `git.txt`.
const REPOSITORY_LABEL: &str = "repository";

/// What the second line of that block starts with, before the kind.
const KIND_PREFIX: &str = "kind: ";

/// What stands between the path of a link and its target, in `tree.txt` and in a `dot-git` block.
const LINK_ARROW: &str = " -> ";

/// What a git directory holds while an operation is in progress; a name that ends in `/` is a directory.
const IN_PROGRESS: [&str; 7] = [
    "MERGE_HEAD",
    "REBASE_HEAD",
    "rebase-merge/",
    "rebase-apply/",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
];

/// The state of a tree at one moment.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Snapshot {
    /// Every entry under the root that is not left out, sorted by the bytes of its path, each path once.
    pub tree: Vec<Entry>,
    /// One section for every repository, sorted by the bytes of its path.
    pub git: Vec<Repository>,
    /// The stored content of every file of [`Snapshot::tree`] that the snapshot before lists with another
    /// content or not as a file, by its path. Empty for a snapshot that has none before it.
    pub files: BTreeMap<String, Vec<u8>>,
}

/// One entry of a tree.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// Relative to the root of the tree, its segments joined by `/`.
    pub path: String,
    pub kind: EntryKind,
}

/// What an entry of a tree is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EntryKind {
    Directory,
    File {
        /// Whether any execute bit is set.
        executable: bool,
        /// The SHA-1, in lowercase hexadecimal, of the content as it is stored: with the root replaced
        /// when the content is valid UTF-8, and of the bytes as they are otherwise.
        hash: String,
    },
    Link {
        /// Where the link points, with the root replaced. It is never followed.
        target: String,
    },
}

/// The section of one repository.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Repository {
    /// Relative to the root of the tree; `.` for the root itself.
    pub path: String,
    pub kind: RepositoryKind,
    /// The blocks after the one that names the repository, in the order they are written.
    pub blocks: Vec<Block>,
}

/// What kind of repository a section describes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RepositoryKind {
    /// A work tree whose git directory is not the common directory of its repository.
    Worktree,
    /// A work tree whose git directory is the common directory of its repository.
    Main,
    /// A repository without a work tree, under the remotes directory.
    Bare,
}

impl RepositoryKind {
    /// The kind as `git.txt` spells it.
    pub fn name(self) -> &'static str {
        match self {
            Self::Worktree => "worktree",
            Self::Main => "main",
            Self::Bare => "bare",
        }
    }

    fn from_name(name: &str) -> Option<Self> {
        [Self::Worktree, Self::Main, Self::Bare]
            .into_iter()
            .find(|kind| kind.name() == name)
    }
}

/// One labelled part of a section.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Block {
    pub label: String,
    /// The lines of the block, each without its line end, with the root replaced.
    pub lines: Vec<String>,
}

/// Why a snapshot was not taken, written, or read.
#[derive(Debug)]
pub enum SnapshotError {
    /// An entry of the tree, or a file or directory of a stored snapshot, could not be read or written.
    Io { path: PathBuf, error: io::Error },
    /// Git did not answer for a repository of the tree, or the repository does not stay in the tree: its
    /// git directory or common directory leaves it, or its work tree is not its own directory.
    Git(GitError),
    /// An entry has no line form: its name is not UTF-8 or holds a line break, its line would not read
    /// back as the entry, as that of a link does not when the arrow written between its path and its
    /// target is not the first of the line, it is neither a file, a directory, nor a link, or the path of
    /// a stored file does not lead downward. Or a repository has no section: what git, its `.git` entry,
    /// or its exclude file says is not UTF-8, or it lies at or beneath a git directory of the tree.
    Unrepresentable { path: PathBuf, reason: &'static str },
    /// The directory holds an entry `.git`, and git answers for it with a git directory that is not that
    /// entry's: the facts git would give are those of another repository.
    Foreign {
        path: PathBuf,
        git_directory: PathBuf,
    },
    /// A stored snapshot lacks the content of a file its `tree.txt` implies was stored.
    Absent {
        /// The path of the file in the tree.
        path: String,
        /// Where the content was looked for.
        file: PathBuf,
    },
    /// The content of a stored file does not hash to the value on its line of `tree.txt`.
    Altered {
        /// The path of the file in the tree.
        path: String,
        /// The stored file whose content was hashed.
        file: PathBuf,
        /// The hash on the line of `tree.txt`.
        listed: String,
        /// The hash of the content as it is stored.
        found: String,
    },
    /// A stored snapshot holds, under `files/`, a file that nothing implies was stored.
    Unimplied {
        /// The path of the file in the tree.
        path: String,
        /// The stored file.
        file: PathBuf,
        /// Why nothing implies it: `tree.txt` lists no file at the path, the snapshot before lists the
        /// file with the same hash, or there is no snapshot before.
        reason: &'static str,
    },
    /// A stored snapshot does not read as one: a line of `tree.txt` or `git.txt`, the place of a line of
    /// `tree.txt` among the others, a name under `files/`, or the kind of an entry of the snapshot
    /// directory is not what a snapshot is written as.
    Stored {
        file: PathBuf,
        /// The line that does not read, counted from 1; 0 when the file as a whole does not.
        line: usize,
        reason: String,
    },
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, error } => write!(f, "{}: {error}", path.display()),
            Self::Git(error) => write!(f, "no snapshot of a repository: {error}"),
            Self::Unrepresentable { path, reason } => {
                write!(f, "{} has no place in a snapshot: {reason}", path.display())
            }
            Self::Foreign {
                path,
                git_directory,
            } => write!(
                f,
                "{} holds a .git that is no repository of its own: git answers with {}",
                path.display(),
                git_directory.display()
            ),
            Self::Absent { path, file } => write!(
                f,
                "the stored snapshot lacks the content of {path}: {} is absent",
                file.display()
            ),
            Self::Altered {
                path,
                file,
                listed,
                found,
            } => write!(
                f,
                "the stored content of {path} is not what was stored: {} hashes to {found}, and tree.txt lists {listed}",
                file.display()
            ),
            Self::Unimplied { path, file, reason } => write!(
                f,
                "the stored snapshot holds content of {path} that nothing implies: {reason}, and {} is stored",
                file.display()
            ),
            Self::Stored { file, line, reason } => {
                write!(f, "{}, line {line}: {reason}", file.display())
            }
        }
    }
}

impl std::error::Error for SnapshotError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { error, .. } => Some(error),
            Self::Git(error) => Some(error),
            Self::Unrepresentable { .. }
            | Self::Foreign { .. }
            | Self::Absent { .. }
            | Self::Altered { .. }
            | Self::Unimplied { .. }
            | Self::Stored { .. } => None,
        }
    }
}

impl From<GitError> for SnapshotError {
    fn from(error: GitError) -> Self {
        Self::Git(error)
    }
}

/// Takes the snapshot of `tree`.
///
/// `compare` gives the keys that are left out of the `config` block of every repository. `previous` is the
/// snapshot taken before this one in the same tree: the content of a file is kept when `previous` lists
/// the file with another hash, or does not list it as a file. With no `previous`, no content is kept.
pub fn take(
    tree: &TempTree,
    compare: &Compare,
    previous: Option<&Snapshot>,
) -> Result<Snapshot, SnapshotError> {
    let root = tree.root();
    let mut found = Vec::new();
    discover(root, Path::new(""), &mut found)?;
    // The sections are in the order of their paths, the root, which is written `.`, at its place.
    found.sort_by(|(a, _), (b, _)| written_path(a).cmp(written_path(b)));
    let helper = match found.is_empty() {
        true => None,
        false => Some(Git::new(tree)?),
    };

    // Git names the git directory and the common directory of every repository, and both stay in the tree,
    // before an entry is listed: what lies at or beneath one of them is left out by where it is.
    let mut located = Vec::new();
    let mut git_directories = BTreeSet::new();
    for (helper, (relative, bare)) in helper.iter().cycle().zip(found) {
        let directory = beneath(root, &relative);
        let (git_directory, common_directory) = helper.git_directories(&directory)?;
        for named in [&git_directory, &common_directory] {
            let resolved = named.canonicalize().map_err(|error| SnapshotError::Io {
                path: named.clone(),
                error,
            })?;
            git_directories.insert(resolved);
        }
        located.push(Located {
            directory,
            relative,
            bare,
            git_directory,
            common_directory,
        });
    }
    // A repository inside a git directory would be read by git and left out of the listing at once.
    for repository in &located {
        let directory = &repository.directory;
        let inside = git_directories.iter().any(|git_directory| {
            directory.starts_with(git_directory) && !(repository.bare && directory == git_directory)
        });
        if inside {
            return Err(SnapshotError::Unrepresentable {
                path: directory.clone(),
                reason: "it is a repository at or beneath a git directory of the tree",
            });
        }
    }

    let mut seen = Seen {
        git_directories,
        ..Seen::default()
    };
    seen.directory(root, "")?;
    seen.entries.sort_by(|a, b| a.path.cmp(&b.path));

    let mut git = Vec::new();
    if let Some(helper) = &helper {
        for repository in &located {
            git.push(section(tree, helper, repository, compare)?);
        }
    }

    let mut files = BTreeMap::new();
    for path in implied(&seen.entries, previous) {
        if let Some((_, content)) = seen.contents.remove(path) {
            files.insert(path.to_owned(), content);
        }
    }
    Ok(Snapshot {
        tree: seen.entries,
        git,
        files,
    })
}

/// The paths of the files of `tree` whose content a snapshot stores, given the snapshot `previous` before
/// it: every file `previous` lists with another hash, or does not list as a file. A file that differs in
/// its execute bit alone is none of them, and with no `previous` there is none.
pub(super) fn implied<'t>(tree: &'t [Entry], previous: Option<&Snapshot>) -> Vec<&'t str> {
    let Some(previous) = previous else {
        return Vec::new();
    };
    let known: BTreeMap<&str, &str> = previous.tree.iter().filter_map(file_of).collect();
    tree.iter()
        .filter_map(file_of)
        .filter(|(path, hash)| known.get(path) != Some(hash))
        .map(|(path, _)| path)
        .collect()
}

/// The path and the hash of `entry`, when it is a file.
fn file_of(entry: &Entry) -> Option<(&str, &str)> {
    match &entry.kind {
        EntryKind::File { hash, .. } => Some((entry.path.as_str(), hash.as_str())),
        _ => None,
    }
}

/// What stands for a `%` in a stored segment.
const STORED_PERCENT: &str = "%25";

/// What stands for the leading dot of a stored segment whose name in the tree begins with [`GIT_ENTRY`].
const STORED_DOT: &str = "%2E";

/// The stored form of `path`, a path of the tree: segment by segment, every `%` becomes `%25`, and then a
/// segment that began with `.git` has its leading dot written as `%2E`. Nothing else changes.
pub fn stored_path(path: &str) -> String {
    let segments: Vec<String> = path
        .split('/')
        .map(|segment| {
            let escaped = segment.replace('%', STORED_PERCENT);
            match escaped.strip_prefix(GIT_ENTRY) {
                Some(rest) => format!("{STORED_DOT}{}{rest}", &GIT_ENTRY[1..]),
                None => escaped,
            }
        })
        .collect();
    segments.join("/")
}

/// The path of the tree that `stored`, a path in the stored form, stands for: exactly the reverse of
/// [`stored_path`]. `None` when a segment is not in the stored form: it begins with `.git`, or holds a
/// `%` that neither rule of the form accounts for.
pub fn tree_path(stored: &str) -> Option<String> {
    let segments: Option<Vec<String>> = stored.split('/').map(tree_segment).collect();
    Some(segments?.join("/"))
}

/// The segment of a path of the tree that the stored segment `stored` stands for.
fn tree_segment(stored: &str) -> Option<String> {
    if stored.starts_with(GIT_ENTRY) {
        return None;
    }
    let (dot, rest) = match stored.strip_prefix(STORED_DOT) {
        Some(rest) if rest.starts_with(&GIT_ENTRY[1..]) => (".", rest),
        _ => ("", stored),
    };
    let mut segment = dot.to_owned();
    let mut parts = rest.split('%');
    segment.push_str(parts.next()?);
    for part in parts {
        segment.push('%');
        segment.push_str(part.strip_prefix(&STORED_PERCENT[1..])?);
    }
    Some(segment)
}

impl Snapshot {
    /// The text of `tree.txt`: one line for every entry, `d <path>` for a directory,
    /// `f <x|-> <hash> <path>` for a file, and `l <path> -> <target>` for a link.
    pub fn tree_text(&self) -> String {
        let mut text = String::new();
        for entry in &self.tree {
            text.push_str(&line_of(entry));
            text.push('\n');
        }
        text
    }

    /// The text of `git.txt`: the sections, an empty line between two of them. A section is its blocks,
    /// and a block is a line with its label followed by its lines, each after four spaces. The first block
    /// is `repository`, with the path and `kind: <kind>`.
    pub fn git_text(&self) -> String {
        let mut text = String::new();
        for (index, repository) in self.git.iter().enumerate() {
            if index > 0 {
                text.push('\n');
            }
            let named = Block {
                label: REPOSITORY_LABEL.to_owned(),
                lines: vec![
                    repository.path.clone(),
                    format!("{KIND_PREFIX}{}", repository.kind.name()),
                ],
            };
            for block in std::iter::once(&named).chain(&repository.blocks) {
                text.push_str(&block.label);
                text.push('\n');
                for line in &block.lines {
                    text.push_str(INDENT);
                    text.push_str(line);
                    text.push('\n');
                }
            }
        }
        text
    }

    /// Writes the snapshot to `directory`, which is absent or holds nothing: `tree.txt`, `git.txt`, and
    /// each kept content at `files/<path>`, the path in its stored form. The directories that lead to
    /// `directory` are created.
    pub fn write(&self, directory: &Path) -> Result<(), SnapshotError> {
        for path in self.files.keys() {
            if path.is_empty()
                || path.contains('\0')
                || path
                    .split('/')
                    .any(|segment| matches!(segment, "" | "." | ".."))
            {
                return Err(SnapshotError::Unrepresentable {
                    path: path.into(),
                    reason: "the path of a stored file leads downward from the root",
                });
            }
        }
        let failed = |path: &Path| {
            let path = path.to_path_buf();
            move |error| SnapshotError::Io { path, error }
        };
        std::fs::create_dir_all(directory).map_err(failed(directory))?;
        let occupied = std::fs::read_dir(directory)
            .map_err(failed(directory))?
            .next()
            .is_some();
        if occupied {
            return Err(SnapshotError::Io {
                path: directory.to_path_buf(),
                error: io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "a snapshot is written to a directory that holds nothing",
                ),
            });
        }
        let tree = directory.join(TREE_FILE);
        std::fs::write(&tree, self.tree_text()).map_err(failed(&tree))?;
        let git = directory.join(GIT_FILE);
        std::fs::write(&git, self.git_text()).map_err(failed(&git))?;
        for (path, content) in &self.files {
            let file = directory.join(FILES_DIRECTORY).join(stored_path(path));
            if let Some(parent) = file.parent() {
                std::fs::create_dir_all(parent).map_err(failed(parent))?;
            }
            std::fs::write(&file, content).map_err(failed(&file))?;
        }
        Ok(())
    }

    /// Reads the snapshot [`Snapshot::write`] wrote to `directory`, together with `previous`, the snapshot
    /// before it of the same scenario; `None` for the one taken before the first step.
    ///
    /// The content under `files/` is answered under the paths of the tree. Which files were stored follows
    /// from the two `tree.txt`, by the rule [`take`] stores content by, and the read holds `files/` to it,
    /// naming the path of the tree and the stored file in each failure:
    ///
    /// - [`SnapshotError::Absent`] when a file that was stored is absent;
    /// - [`SnapshotError::Unimplied`] when a file is there that was not stored: `tree.txt` lists no file
    ///   at its path, `previous` lists the file with the same hash, or there is no `previous`;
    /// - [`SnapshotError::Altered`] when the content of a stored file does not hash to the value on its
    ///   line of `tree.txt`.
    ///
    /// An absent file is reported before a file nothing implies, and that before an altered one; among
    /// several of a kind, the one first by the bytes of its path.
    ///
    /// It fails with [`SnapshotError::Stored`], naming the entry, when `tree.txt` or `git.txt` is not a
    /// regular file or `files` is not a directory, a symbolic link in the place of any of the three
    /// included, which is not followed; for a line of `tree.txt` that is no entry, or whose path does not
    /// come after the path of the line before it by its bytes, naming the line, so that a listing with a
    /// path twice or out of order does not read; for a name under `files/` that is not in the stored
    /// form; and for an entry there that is neither a directory nor a regular file. A `tree.txt` or `git.txt` that is
    /// absent fails with [`SnapshotError::Io`]. A directory without `files` holds no content, and reads
    /// when none is implied.
    pub fn read(directory: &Path, previous: Option<&Self>) -> Result<Self, SnapshotError> {
        let unread = |path: &Path| {
            let path = path.to_path_buf();
            move |error| SnapshotError::Io { path, error }
        };
        let text = |name: &str| {
            let file = directory.join(name);
            if !file.symlink_metadata().map_err(unread(&file))?.is_file() {
                return Err(stored(&file, 0, "not a regular file"));
            }
            let content = std::fs::read(&file).map_err(unread(&file))?;
            match String::from_utf8(content) {
                Ok(text) => Ok((file, text)),
                Err(_) => Err(stored(&file, 0, "the file is not UTF-8")),
            }
        };
        let (file, tree) = text(TREE_FILE)?;
        let tree = parse_tree(&file, &tree)?;
        let (file, git) = text(GIT_FILE)?;
        let git = parse_git(&file, &git)?;

        let mut files = BTreeMap::new();
        let stored_directory = directory.join(FILES_DIRECTORY);
        match stored_directory.symlink_metadata() {
            Ok(metadata) if metadata.is_dir() => stored_files(&stored_directory, "", &mut files)?,
            Ok(_) => return Err(stored(&stored_directory, 0, "not a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(unread(&stored_directory)(error)),
        }

        let location = |path: &str| stored_directory.join(stored_path(path));
        held(&tree, &files, previous, &location)?;
        Ok(Self { tree, git, files })
    }
}

/// Holds `files`, the stored content of a snapshot by the paths of the tree, to what `tree`, its listing,
/// and `previous`, the snapshot before it, imply was stored: the rule [`Snapshot::read`] reads a stored
/// snapshot by, with its order of failures. `location` answers where the content of a path is, or would
/// be, stored, for the failure to name.
pub(super) fn held(
    tree: &[Entry],
    files: &BTreeMap<String, Vec<u8>>,
    previous: Option<&Snapshot>,
    location: &dyn Fn(&str) -> PathBuf,
) -> Result<(), SnapshotError> {
    let implied = implied(tree, previous);
    if let Some(path) = implied.iter().find(|path| !files.contains_key(**path)) {
        return Err(SnapshotError::Absent {
            path: (*path).to_owned(),
            file: location(path),
        });
    }
    let listed: BTreeMap<&str, &str> = tree.iter().filter_map(file_of).collect();
    for path in files.keys() {
        let reason = if !listed.contains_key(path.as_str()) {
            "tree.txt lists no file at the path"
        } else if previous.is_none() {
            "a snapshot that has none before it stores no content"
        } else if !implied.contains(&path.as_str()) {
            "the snapshot before lists the file with the same hash"
        } else {
            continue;
        };
        return Err(SnapshotError::Unimplied {
            path: path.clone(),
            file: location(path),
            reason,
        });
    }
    for (path, content) in files {
        let found = hash_of(content);
        let listed = listed[path.as_str()];
        if found != listed {
            return Err(SnapshotError::Altered {
                path: path.clone(),
                file: location(path),
                listed: listed.to_owned(),
                found,
            });
        }
    }
    Ok(())
}

/// A repository of a tree, and the two directories git names for it.
struct Located {
    /// The directory of the repository: the root of the tree followed by `relative`.
    directory: PathBuf,
    /// Relative to the root of the tree; empty for the root itself.
    relative: PathBuf,
    bare: bool,
    /// The git directory and the common directory, as git answered them.
    git_directory: PathBuf,
    common_directory: PathBuf,
}

/// The bytes of the path a section gives the repository at `relative` under the root.
fn written_path(relative: &Path) -> &[u8] {
    match relative.as_os_str().as_bytes() {
        [] => b".",
        bytes => bytes,
    }
}

/// The directory `relative` under `root`, which is `root` itself when `relative` is empty.
fn beneath(root: &Path, relative: &Path) -> PathBuf {
    if relative.as_os_str().is_empty() {
        root.to_path_buf()
    } else {
        root.join(relative)
    }
}

/// Whether the entry `name` of the directory `relative` under the root is left out by its name, with all
/// it holds: an excluded entry of the root, or a directory of compiled modules.
fn left_out(relative: &Path, name: &OsStr, is_directory: bool) -> bool {
    let of_root = relative.as_os_str().is_empty();
    of_root && EXCLUDED.iter().any(|excluded| name == *excluded)
        || is_directory && name == COMPILED_MODULES
}

/// Whether the directory `name` in the directory `relative` under the root is a bare repository.
fn is_bare(relative: &Path, name: &OsStr) -> bool {
    relative == Path::new(REMOTES_DIRECTORY)
        && name != GIT_ENTRY
        && name.as_encoded_bytes().ends_with(BARE_SUFFIX.as_bytes())
}

/// Adds to `found` every repository at or under the directory `relative` of `root`, as its path under the
/// root and whether it is a bare one: each directory that holds an entry `.git`, and each bare repository
/// under the remotes directory. Nothing but the names and the kinds of entries is read, no link is
/// followed, and what [`Seen::directory`] leaves out by its name is not entered.
fn discover(
    root: &Path,
    relative: &Path,
    found: &mut Vec<(PathBuf, bool)>,
) -> Result<(), SnapshotError> {
    let directory = beneath(root, relative);
    let unread = |path: &Path| {
        let path = path.to_path_buf();
        move |error| SnapshotError::Io { path, error }
    };
    for entry in std::fs::read_dir(&directory).map_err(unread(&directory))? {
        let entry = entry.map_err(unread(&directory))?;
        let name = entry.file_name();
        let is_directory = entry.file_type().map_err(unread(&entry.path()))?.is_dir();
        if left_out(relative, &name, is_directory) {
            continue;
        }
        if name == GIT_ENTRY {
            found.push((relative.to_path_buf(), false));
        } else if is_directory {
            let path = relative.join(&name);
            if is_bare(relative, &name) {
                found.push((path, true));
            } else {
                discover(root, &path, found)?;
            }
        }
    }
    Ok(())
}

/// The line of `tree.txt` that `entry` is written as, without its line end.
pub(super) fn line_of(entry: &Entry) -> String {
    let path = &entry.path;
    match &entry.kind {
        EntryKind::Directory => format!("d {path}"),
        EntryKind::File { executable, hash } => {
            let mode = if *executable { 'x' } else { '-' };
            format!("f {mode} {hash} {path}")
        }
        EntryKind::Link { target } => format!("l {path}{LINK_ARROW}{target}"),
    }
}

/// What a walk of a tree has found.
#[derive(Default)]
struct Seen {
    entries: Vec<Entry>,
    /// The hash and the stored content of every file, by its path.
    contents: BTreeMap<String, (String, Vec<u8>)>,
    /// Every directory git names as the git directory or the common directory of a repository of the
    /// tree, every link of its path resolved. None is entered.
    git_directories: BTreeSet<PathBuf>,
}

impl Seen {
    /// Lists the entries of `directory`, which is `relative` under the root of the tree, and of every
    /// directory under it that is not left out. No link is followed.
    fn directory(&mut self, root: &Path, relative: &str) -> Result<(), SnapshotError> {
        let directory = if relative.is_empty() {
            root.to_path_buf()
        } else {
            root.join(relative)
        };
        let unread = |path: &Path| {
            let path = path.to_path_buf();
            move |error| SnapshotError::Io { path, error }
        };
        let entries = std::fs::read_dir(&directory)
            .and_then(|entries| entries.collect::<io::Result<Vec<_>>>())
            .map_err(unread(&directory))?;
        for entry in entries {
            let from = entry.path();
            let unrepresentable = |reason| SnapshotError::Unrepresentable {
                path: from.clone(),
                reason,
            };
            let file_name = entry.file_name();
            let name = file_name
                .to_str()
                .filter(|name| !name.contains(['\n', '\r']))
                .ok_or_else(|| unrepresentable("its name is not UTF-8 or holds a line break"))?;
            let kind = entry.file_type().map_err(unread(&from))?;
            if left_out(Path::new(relative), OsStr::new(name), kind.is_dir()) {
                continue;
            }
            let path = if relative.is_empty() {
                name.to_owned()
            } else {
                format!("{relative}/{name}")
            };
            let kind = if kind.is_symlink() {
                let target = std::fs::read_link(&from).map_err(unread(&from))?;
                let target = normalize::root(target.as_os_str().as_bytes(), root);
                let target = String::from_utf8(target)
                    .ok()
                    .filter(|target| !target.contains(['\n', '\r']))
                    .ok_or_else(|| {
                        unrepresentable("its target is not UTF-8 or holds a line break")
                    })?;
                EntryKind::Link { target }
            } else if kind.is_dir() {
                // The path is the root followed by names of directories, so no link is left in it to
                // resolve, and it is a git directory exactly when it is one of the resolved ones.
                let git_directory = name == GIT_ENTRY
                    || is_bare(Path::new(relative), OsStr::new(name))
                    || self.git_directories.contains(&from);
                if !git_directory {
                    self.directory(root, &path)?;
                }
                EntryKind::Directory
            } else if kind.is_file() {
                let content = std::fs::read(&from).map_err(unread(&from))?;
                let executable = entry
                    .metadata()
                    .map_err(unread(&from))?
                    .permissions()
                    .mode()
                    & 0o111
                    != 0;
                let content = normalize::stored(&content, root);
                let hash = hash_of(&content);
                self.contents.insert(path.clone(), (hash.clone(), content));
                EntryKind::File { executable, hash }
            } else {
                return Err(unrepresentable(
                    "it is no file, directory, or symbolic link",
                ));
            };
            let entry = Entry { path, kind };
            // A line is read by its form alone, and that of a link at its first arrow, which is the one
            // written only when none begins in the path or where the path ends.
            if parse_entry(&line_of(&entry)).as_ref() != Some(&entry) {
                return Err(unrepresentable(
                    "its line does not read back as the entry it is written from",
                ));
            }
            self.entries.push(entry);
        }
        Ok(())
    }
}

/// The SHA-1 of `content`, in lowercase hexadecimal.
pub(super) fn hash_of(content: &[u8]) -> String {
    Sha1::digest(content)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The section of `repository`, a repository of `tree`.
fn section(
    tree: &TempTree,
    git: &Git<'_>,
    repository: &Located,
    compare: &Compare,
) -> Result<Repository, SnapshotError> {
    let root = tree.root();
    let Located {
        directory,
        relative,
        bare,
        git_directory,
        common_directory,
    } = repository;
    let (directory, bare) = (directory.clone(), *bare);
    let path = std::str::from_utf8(written_path(relative));
    let path = path.map_err(|_| SnapshotError::Unrepresentable {
        path: directory.clone(),
        reason: "its name is not UTF-8 or holds a line break",
    })?;
    let unread = |path: &Path| {
        let path = path.to_path_buf();
        move |error| SnapshotError::Io { path, error }
    };
    let text = |bytes: &[u8]| -> Result<String, SnapshotError> {
        String::from_utf8(normalize::root(bytes, root)).map_err(|_| {
            SnapshotError::Unrepresentable {
                path: directory.clone(),
                reason: "what git, its .git entry, or its exclude file says of the repository is not UTF-8",
            }
        })
    };
    let lines = |output: &[u8]| -> Result<Vec<String>, SnapshotError> {
        Ok(text(output)?.lines().map(str::to_owned).collect())
    };
    // The `.git` entry as its block shows it, and the git directory that is the directory's own: the entry
    // or the directory its file names, and for a bare repository the directory itself.
    let (dot_git, own) = if bare {
        (None, Some(directory.clone()))
    } else {
        let entry = directory.join(GIT_ENTRY);
        let metadata = entry.symlink_metadata().map_err(unread(&entry))?;
        if metadata.is_symlink() {
            let target = std::fs::read_link(&entry).map_err(unread(&entry))?;
            let line = format!("link{LINK_ARROW}{}", text(target.as_os_str().as_bytes())?);
            (Some(line), Some(entry))
        } else if metadata.is_dir() {
            (Some("directory".to_owned()), Some(entry))
        } else {
            let content = std::fs::read(&entry).map_err(unread(&entry))?;
            let named = content
                .split(|byte| *byte == b'\n')
                .next()
                .and_then(|line| line.strip_prefix(GITFILE_PREFIX.as_bytes()))
                .map(|path| directory.join(Path::new(std::ffi::OsStr::from_bytes(path))));
            let line = lines(&content)?.into_iter().next().unwrap_or_default();
            (Some(line), named)
        }
    };
    let same = own
        .and_then(|own| own.canonicalize().ok())
        .zip(git_directory.canonicalize().ok())
        .is_some_and(|(own, named)| own == named);
    if !same {
        return Err(SnapshotError::Foreign {
            path: directory,
            git_directory: git_directory.clone(),
        });
    }
    // The two commands that read a work tree read the one git reports, so it is held to the directory of
    // the repository before either runs.
    if !bare {
        git.work_tree_top(&directory)?;
    }

    let kind = if bare {
        RepositoryKind::Bare
    } else if git_directory == common_directory {
        RepositoryKind::Main
    } else {
        RepositoryKind::Worktree
    };
    let run = |args: &[&str]| -> Result<Vec<String>, SnapshotError> {
        lines(&git.run_bytes(&directory, args)?)
    };
    // What git answers, or `otherwise` when it ends with 1, as it does for a question whose answer is no.
    let run_or = |args: &[&str], otherwise: &str| -> Result<Vec<String>, SnapshotError> {
        match git.run_bytes(&directory, args) {
            Ok(output) => lines(&output),
            Err(GitError::Failed {
                exit: Exit::Code(1),
                ..
            }) => Ok(vec![otherwise.to_owned()]),
            Err(error) => Err(error.into()),
        }
    };
    let mut blocks = Vec::new();
    let mut block = |label: &str, lines: Vec<String>| {
        blocks.push(Block {
            label: label.to_owned(),
            lines,
        });
    };

    if let Some(line) = dot_git {
        block("dot-git", vec![line]);
    }

    let mut head = run_or(&["rev-parse", "--verify", "--quiet", "HEAD"], "unborn")?;
    head.extend(run_or(&["symbolic-ref", "--quiet", "HEAD"], "detached")?);
    block("head", head);
    block(
        "refs",
        run(&[
            "for-each-ref",
            "--format=%(refname) %(objectname) %(upstream) %(symref)",
        ])?,
    );
    if kind != RepositoryKind::Worktree {
        block("worktrees", run(&["worktree", "list", "--porcelain"])?);
    }
    if !bare {
        // Without the option, git would write the index back after refreshing it.
        block(
            "status",
            run(&[
                "--no-optional-locks",
                "status",
                "--porcelain=v2",
                "--branch",
                "--untracked-files=all",
            ])?,
        );
        // Git lists the stash only where there is a work tree.
        block("stash", run(&["stash", "list", "--format=%gd %H"])?);
    }

    let mut config = run(&["config", "--local", "--list"])?;
    config.retain(|line| {
        let key = line.split_once('=').map_or(line.as_str(), |(key, _)| key);
        !compare
            .git_config_ignore
            .iter()
            .any(|ignored| ignored == key)
    });
    config.sort();
    block("config", config);

    let mut in_progress = Vec::new();
    for name in IN_PROGRESS {
        let (entry, is_directory) = match name.strip_suffix('/') {
            Some(entry) => (entry, true),
            None => (name, false),
        };
        let found = git_directory
            .join(entry)
            .symlink_metadata()
            .is_ok_and(|metadata| !is_directory || metadata.is_dir());
        if found {
            in_progress.push(name.to_owned());
        }
    }
    block("in-progress", in_progress);

    let exclude = common_directory.join("info/exclude");
    let lands = tree
        .landing(&exclude, Use::Base)
        .ok_or_else(|| GitError::Unconfined {
            path: exclude.clone(),
        })?;
    let content = match std::fs::read(&lands) {
        Ok(content) => content,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(unread(&exclude)(error)),
    };
    block("exclude", lines(&content)?);

    let mut log = Vec::new();
    for branch in run(&["for-each-ref", "--format=%(refname)", "refs/heads"])? {
        let entries = run(&[
            "log",
            "--format=%H %P %an %ae %at %cn %ce %ct %s",
            &branch,
            "--",
        ])?;
        log.push(branch);
        log.extend(entries);
    }
    block("log", log);

    Ok(Repository {
        path: path.to_owned(),
        kind,
        blocks,
    })
}

fn stored(file: &Path, line: usize, reason: impl Into<String>) -> SnapshotError {
    SnapshotError::Stored {
        file: file.to_path_buf(),
        line,
        reason: reason.into(),
    }
}

/// The entries `text`, the content of the `tree.txt` at `file`, lists: each line an entry, and each path
/// after the one of the line before it by its bytes, so that none is listed twice.
pub(super) fn parse_tree(file: &Path, text: &str) -> Result<Vec<Entry>, SnapshotError> {
    let mut entries: Vec<Entry> = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let entry =
            parse_entry(line).ok_or_else(|| stored(file, index + 1, "not the line of an entry"))?;
        if entries.last().is_some_and(|last| last.path >= entry.path) {
            return Err(stored(
                file,
                index + 1,
                "its path does not come after the path of the line before it",
            ));
        }
        entries.push(entry);
    }
    Ok(entries)
}

/// The entry `line` of a `tree.txt` is, when it has one of the three forms.
fn parse_entry(line: &str) -> Option<Entry> {
    let (kind, rest) = line.split_once(' ')?;
    let (path, kind) = match kind {
        "d" => (rest, EntryKind::Directory),
        "f" => {
            let mut parts = rest.splitn(3, ' ');
            let executable = match parts.next()? {
                "x" => true,
                "-" => false,
                _ => return None,
            };
            let hash = parts.next()?;
            let is_hash = hash.len() == 40
                && hash
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
            if !is_hash {
                return None;
            }
            let kind = EntryKind::File {
                executable,
                hash: hash.to_owned(),
            };
            (parts.next()?, kind)
        }
        "l" => {
            let (path, target) = rest.split_once(LINK_ARROW)?;
            let kind = EntryKind::Link {
                target: target.to_owned(),
            };
            (path, kind)
        }
        _ => return None,
    };
    (!path.is_empty()).then(|| Entry {
        path: path.to_owned(),
        kind,
    })
}

/// The sections `text`, the content of the `git.txt` at `file`, holds.
pub(super) fn parse_git(file: &Path, text: &str) -> Result<Vec<Repository>, SnapshotError> {
    let mut repositories = Vec::new();
    let mut blocks: Vec<Block> = Vec::new();
    // An empty line after the last one ends the last section.
    for (index, line) in text.lines().chain([""]).enumerate() {
        if let Some(content) = line.strip_prefix(INDENT) {
            blocks
                .last_mut()
                .ok_or_else(|| stored(file, index + 1, "a line of a block before any label"))?
                .lines
                .push(content.to_owned());
        } else if !line.is_empty() {
            blocks.push(Block {
                label: line.to_owned(),
                lines: Vec::new(),
            });
        } else if !blocks.is_empty() {
            let mut blocks = std::mem::take(&mut blocks).into_iter();
            let named = blocks
                .next()
                .filter(|block| block.label == REPOSITORY_LABEL);
            let repository = named.as_ref().and_then(|block| match &block.lines[..] {
                [path, kind] => Some((
                    path.clone(),
                    RepositoryKind::from_name(kind.strip_prefix(KIND_PREFIX)?)?,
                )),
                _ => None,
            });
            let (path, kind) = repository.ok_or_else(|| {
                stored(
                    file,
                    index + 1,
                    "the section before this line does not start by naming a repository and its kind",
                )
            })?;
            repositories.push(Repository {
                path,
                kind,
                blocks: blocks.collect(),
            });
        }
    }
    Ok(repositories)
}

/// Adds the content of every regular file under `directory`, the directory of a `files/` that holds the
/// path `relative` of the tree, to `files`, each under its path of the tree. No link under `directory` is
/// followed; `directory` itself is one its caller found to be a directory.
fn stored_files(
    directory: &Path,
    relative: &str,
    files: &mut BTreeMap<String, Vec<u8>>,
) -> Result<(), SnapshotError> {
    let unread = |path: &Path| {
        let path = path.to_path_buf();
        move |error| SnapshotError::Io { path, error }
    };
    let entries = std::fs::read_dir(directory).map_err(unread(directory))?;
    for entry in entries {
        let entry = entry.map_err(unread(directory))?;
        let from = entry.path();
        let file_name = entry.file_name();
        let name = file_name
            .to_str()
            .ok_or_else(|| stored(&from, 0, "the name of a stored file is not UTF-8"))?;
        let name = tree_segment(name)
            .ok_or_else(|| stored(&from, 0, "the name is not in the stored form of a path"))?;
        let path = if relative.is_empty() {
            name
        } else {
            format!("{relative}/{name}")
        };
        let kind = entry.file_type().map_err(unread(&from))?;
        if kind.is_dir() {
            stored_files(&from, &path, files)?;
        } else if kind.is_file() {
            files.insert(path, std::fs::read(&from).map_err(unread(&from))?);
        } else {
            return Err(stored(&from, 0, "neither a directory nor a regular file"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(path: &str, kind: EntryKind) -> Entry {
        Entry {
            path: path.to_owned(),
            kind,
        }
    }

    #[test]
    fn a_line_of_the_tree_reads_back_as_the_entry_it_was_written_from() {
        let hash = "da39a3ee5e6b4b0d3255bfef95601890afd80709".to_owned();
        let snapshot = Snapshot {
            tree: vec![
                entry(
                    "a -> b",
                    EntryKind::File {
                        executable: false,
                        hash: hash.clone(),
                    },
                ),
                entry("a b", EntryKind::Directory),
                entry(
                    "a b/run me",
                    EntryKind::File {
                        executable: true,
                        hash: hash.clone(),
                    },
                ),
                entry(
                    "link",
                    EntryKind::Link {
                        target: "<ROOT>/ws/a -> b".to_owned(),
                    },
                ),
            ],
            ..Snapshot::default()
        };
        let text = snapshot.tree_text();

        assert_eq!(
            text,
            format!(
                "f - {hash} a -> b\nd a b\nf x {hash} a b/run me\nl link -> <ROOT>/ws/a -> b\n"
            )
        );
        assert_eq!(
            parse_tree(Path::new("tree.txt"), &text).expect("the text reads"),
            snapshot.tree
        );
    }

    #[test]
    fn a_line_that_is_no_entry_is_named_by_its_number() {
        let hash = "da39a3ee5e6b4b0d3255bfef95601890afd80709";
        for line in [
            "",
            "d",
            "d ",
            "x path",
            "f - path",
            &format!("f r {hash} path"),
            &format!("f - {} path", &hash[1..]),
            &format!("f - {} path", hash.to_uppercase()),
            &format!("f - {hash}"),
            "l path",
        ] {
            let text = format!("d first\n{line}\n");
            let error = parse_tree(Path::new("tree.txt"), &text).expect_err("no entry");
            assert!(
                matches!(error, SnapshotError::Stored { line: 2, .. }),
                "{line:?}: {error}"
            );
        }
    }

    #[test]
    fn a_line_whose_path_does_not_come_after_the_one_before_it_is_named_by_its_number() {
        let hash = "da39a3ee5e6b4b0d3255bfef95601890afd80709";
        for (text, line) in [
            ("d b\nd a\n", 2),
            ("d a\nd a\n", 2),
            ("d a\nd a/b\nl a -> x\n", 3),
            (&format!("d a\nd b\nf - {hash} b\n"), 3),
            ("d a\nd a.c\nd a/b\nd a.c\n", 4),
        ] {
            let error = parse_tree(Path::new("tree.txt"), text).expect_err("no listing");
            assert!(
                matches!(&error, SnapshotError::Stored { line: named, reason, .. }
                    if *named == line && reason.contains("does not come after")),
                "{text:?}: {error}"
            );
        }
        // By the bytes of the paths, not by their segments.
        let read = parse_tree(Path::new("tree.txt"), "d a\nd a.c\nd a/b\n").expect("it reads");
        assert_eq!(read.len(), 3);
    }

    #[test]
    fn the_sections_of_the_repositories_read_back_as_they_were_written() {
        let block = |label: &str, lines: &[&str]| Block {
            label: label.to_owned(),
            lines: lines.iter().map(|line| (*line).to_owned()).collect(),
        };
        let snapshot = Snapshot {
            git: vec![
                Repository {
                    path: "remotes/alpha.git".to_owned(),
                    kind: RepositoryKind::Bare,
                    blocks: vec![block("head", &["unborn", "refs/heads/main"])],
                },
                Repository {
                    path: "ws/projects/alpha".to_owned(),
                    kind: RepositoryKind::Main,
                    blocks: vec![
                        block("worktrees", &["worktree <ROOT>/ws", "", "bare"]),
                        block("in-progress", &[]),
                        block("exclude", &["", "    indented", "head", "trailing  "]),
                    ],
                },
                Repository {
                    path: "ws/feature/alpha".to_owned(),
                    kind: RepositoryKind::Worktree,
                    blocks: Vec::new(),
                },
            ],
            ..Snapshot::default()
        };
        let text = snapshot.git_text();

        assert!(
            text.starts_with(
                "repository\n    remotes/alpha.git\n    kind: bare\nhead\n    unborn\n    refs/heads/main\n\nrepository\n"
            ),
            "{text}"
        );
        assert_eq!(
            parse_git(Path::new("git.txt"), &text).expect("the text reads"),
            snapshot.git
        );
        assert_eq!(parse_git(Path::new("git.txt"), "").expect("no section"), []);
    }

    #[test]
    fn a_section_that_names_no_repository_is_refused() {
        for text in [
            "    a line\n",
            "head\n    unborn\n",
            "repository\n    ws\n",
            "repository\n    ws\n    kind: other\n",
            "repository\n    ws\n    main\n",
        ] {
            let error = parse_git(Path::new("git.txt"), text).expect_err("no section");
            assert!(matches!(error, SnapshotError::Stored { .. }), "{text:?}");
        }
    }

    #[test]
    fn a_path_is_stored_in_a_form_no_repository_reads_as_its_own_and_read_back() {
        for (path, stored) in [
            ("ws/feature-one/alpha/.git", "ws/feature-one/alpha/%2Egit"),
            ("ws/.gitignore", "ws/%2Egitignore"),
            ("ws/docs/.gitattributes", "ws/docs/%2Egitattributes"),
            ("ws/50%.txt", "ws/50%25.txt"),
            ("ws/%2Egit", "ws/%252Egit"),
            ("ws/.github/x", "ws/%2Egithub/x"),
            ("ws/a.git/x", "ws/a.git/x"),
            (".git/.git", "%2Egit/%2Egit"),
            (".git%", "%2Egit%25"),
            ("%25/100%", "%2525/100%25"),
            (".gi/.hidden/git", ".gi/.hidden/git"),
            ("plain/file.txt", "plain/file.txt"),
        ] {
            assert_eq!(stored_path(path), stored);
            assert_eq!(tree_path(stored).as_deref(), Some(path), "{stored}");
        }
    }

    #[test]
    fn a_segment_that_is_not_in_the_stored_form_stands_for_no_path() {
        for stored in [
            "ws/.git",
            ".gitignore/x",
            "ws/.github",
            "ws/50%.txt",
            "ws/%",
            "ws/%2",
            "ws/a%2Egit",
            "ws/%2egit",
            "ws/%2Efoo",
            "ws/%2E",
            "ws/%24",
            "ws/x%25%",
        ] {
            assert_eq!(tree_path(stored), None, "{stored}");
        }
    }
}
