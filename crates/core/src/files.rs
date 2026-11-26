//! The file port: how the library reads and writes files, and its one adapter over the operating system.
//!
//! Reading and writing are separate authorities: a module that takes only [`ReadFiles`] cannot change a file.

use std::ffi::OsString;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// Reads files.
pub trait ReadFiles: Sync {
    /// The bytes of the file at `path`, or `Ok(None)` when no such file exists.
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>>;

    /// Whether `path` is a regular file, with symbolic links followed.
    fn is_file(&self, path: &Path) -> bool;

    /// Whether there is an entry at `path`, with symbolic links followed: a link whose target is gone does
    /// not exist.
    fn exists(&self, path: &Path) -> bool;

    /// Whether `path` is a directory, with symbolic links followed.
    fn is_dir(&self, path: &Path) -> bool;

    /// The names of the entries of the directory `path`, with symbolic links followed, in no fixed order. A
    /// name that starts with `.` is among them; `.` and `..` are not. A `path` that is no directory, or one
    /// that cannot be listed, is an error.
    fn entries(&self, path: &Path) -> io::Result<Vec<OsString>>;

    /// The target text of the symbolic link at `path`, as it was written and not followed: `Ok(None)` when
    /// `path` is no symbolic link or does not exist. The directories above `path` are followed.
    fn read_link(&self, path: &Path) -> io::Result<Option<PathBuf>>;

    /// Where the absolute `path` leads with every symbolic link followed: the `resolve` rule of
    /// repository-addition.md, Duplicates.
    ///
    /// The walk is not strict. A component that does not exist, or whose link cannot be read, is kept as
    /// text, and the rest of the path is taken as text after it. `.` is dropped and `..` removes the last
    /// component, lexically, as `os.path.realpath` does. A link met again while it is still being resolved
    /// is a loop: the path reached at that link is kept, and nothing after it is resolved.
    fn resolve(&self, path: &Path) -> PathBuf {
        let mut seen = std::collections::HashMap::new();
        resolve_walk(self, PathBuf::from("/"), path, &mut seen).0
    }
}

/// One walk of [`ReadFiles::resolve`]: `at` followed by the parts of `rest`, and whether every link met
/// was resolved. `seen` maps each link met to its resolution, which is `None` while it is under way.
fn resolve_walk<F: ReadFiles + ?Sized>(
    files: &F,
    mut at: PathBuf,
    rest: &Path,
    seen: &mut std::collections::HashMap<PathBuf, Option<PathBuf>>,
) -> (PathBuf, bool) {
    use std::path::Component;

    let mut parts = rest.components();
    while let Some(part) = parts.next() {
        let name = match part {
            Component::RootDir | Component::Prefix(_) => {
                at = PathBuf::from("/");
                continue;
            }
            Component::CurDir => continue,
            Component::ParentDir => {
                at.pop();
                continue;
            }
            Component::Normal(name) => name,
        };
        let reached = at.join(name);
        let Some(target) = files.read_link(&reached).ok().flatten() else {
            at = reached;
            continue;
        };
        if let Some(resolution) = seen.get(&reached) {
            match resolution {
                Some(resolution) => {
                    at = resolution.clone();
                    continue;
                }
                None => return (reached.join(parts.as_path()), false),
            }
        }
        seen.insert(reached.clone(), None);
        let (resolved, whole) = resolve_walk(files, at, &target, seen);
        if !whole {
            return (resolved.join(parts.as_path()), false);
        }
        seen.insert(reached, Some(resolved.clone()));
        at = resolved;
    }
    (at, true)
}

/// What one update does to a file: it gets the current bytes, `None` when no such file exists, and returns the
/// new bytes, or `None` to leave the file as it is.
pub type Edit<'a> = dyn FnMut(Option<&[u8]>) -> Option<Vec<u8>> + 'a;

/// Writes files.
pub trait WriteFiles: Sync {
    /// Creates `path` and any missing parent directories; an existing directory succeeds.
    fn create_dir_all(&self, path: &Path) -> io::Result<()>;

    /// Removes an entry, recursively for a directory, without following symbolic links in the tree.
    /// A symbolic link is itself removed, even when dangling. A missing entry succeeds.
    /// Ancestor directories are followed; this operation provides neither confinement nor rollback.
    fn remove(&self, path: &Path) -> io::Result<()>;

    /// One read-modify-write of the file at `path`, and whether the file was replaced.
    ///
    /// `edit` is given the current bytes and decides the new ones. An exclusive advisory lock on the directory
    /// that holds `path` is held from before the read until the call returns, and a caller that cannot take it
    /// waits. The new bytes are written to a temporary file beside the target, which is then renamed over it,
    /// so a reader sees the old file or the new one; when the write fails the temporary file is removed. A
    /// target that exists keeps its permission bits. A target that is a symbolic link stays one: the file it
    /// points to is the one replaced.
    ///
    /// When `edit` returns new bytes, a target that exists and cannot be opened for writing is an error, and so
    /// is one that is a symbolic link to a file that does not exist; neither is replaced. When `edit` returns
    /// `None`, neither is an error: nothing is to be written, and the call reports that the file was not
    /// replaced.
    fn update(&self, path: &Path, edit: &mut Edit<'_>) -> io::Result<bool>;
}

/// Makes scratch files: a file in a directory of its own, which is removed with its handle.
pub trait ScratchFiles {
    /// A copy of the file at `source` in a new directory under `temp`, with the bytes and the modification time
    /// of the source, to the nanosecond.
    fn scratch_copy(&self, temp: &Path, source: &Path) -> io::Result<ScratchFile>;

    /// The path of a file `name` that does not exist, in a new directory under `temp`.
    fn scratch_path(&self, temp: &Path, name: &str) -> io::Result<ScratchFile>;
}

/// A path in a scratch directory. Dropping the handle removes the directory and what it holds, and ignores a
/// failure to do so.
#[derive(Debug)]
pub struct ScratchFile {
    directory: PathBuf,
    path: PathBuf,
}

impl ScratchFile {
    /// The path of the scratch file.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for ScratchFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

/// Creates a directory that did not exist under `temp`, named for this process and a sequence number, and
/// returns the handle of `name` in it.
fn scratch_directory(temp: &Path, name: &str) -> io::Result<ScratchFile> {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    loop {
        let directory = temp.join(format!(
            "winter-fingerprint.{}.{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        match std::fs::create_dir(&directory) {
            Ok(()) => {
                let path = directory.join(name);
                return Ok(ScratchFile { directory, path });
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
}

impl ScratchFiles for OsFiles {
    fn scratch_copy(&self, temp: &Path, source: &Path) -> io::Result<ScratchFile> {
        let name = source
            .file_name()
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        let scratch = scratch_directory(temp, &name.to_string_lossy())?;
        let modified = std::fs::metadata(source)?.modified()?;
        std::fs::copy(source, scratch.path())?;
        std::fs::File::options()
            .write(true)
            .open(scratch.path())?
            .set_modified(modified)?;
        Ok(scratch)
    }

    fn scratch_path(&self, temp: &Path, name: &str) -> io::Result<ScratchFile> {
        scratch_directory(temp, name)
    }
}

/// The files of the operating system.
pub struct OsFiles;

impl ReadFiles for OsFiles {
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        match std::fs::read(path) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn is_file(&self, path: &Path) -> bool {
        std::fs::metadata(path).is_ok_and(|metadata| metadata.is_file())
    }

    fn exists(&self, path: &Path) -> bool {
        std::fs::metadata(path).is_ok()
    }

    fn is_dir(&self, path: &Path) -> bool {
        std::fs::metadata(path).is_ok_and(|metadata| metadata.is_dir())
    }

    fn entries(&self, path: &Path) -> io::Result<Vec<OsString>> {
        std::fs::read_dir(path)?
            .map(|entry| entry.map(|entry| entry.file_name()))
            .collect()
    }

    fn read_link(&self, path: &Path) -> io::Result<Option<PathBuf>> {
        match std::fs::read_link(path) {
            Ok(target) => Ok(Some(target)),
            // An entry that is no link, no entry at all, and a path through something that is no directory.
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::InvalidInput
                        | io::ErrorKind::NotFound
                        | io::ErrorKind::NotADirectory
                ) =>
            {
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }
}

impl WriteFiles for OsFiles {
    fn create_dir_all(&self, path: &Path) -> io::Result<()> {
        std::fs::create_dir_all(path)
    }

    fn remove(&self, path: &Path) -> io::Result<()> {
        let metadata = match std::fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        let result = if metadata.is_dir() {
            std::fs::remove_dir_all(path)
        } else {
            std::fs::remove_file(path)
        };
        match result {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            result => result,
        }
    }

    fn update(&self, path: &Path, edit: &mut Edit<'_>) -> io::Result<bool> {
        let directory = path
            .parent()
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        std::fs::create_dir_all(directory)?;
        // The lock ends when this handle closes, which is when the function returns.
        let lock = std::fs::File::open(directory)?;
        lock_exclusive(&lock)?;

        let resolved = match std::fs::canonicalize(path) {
            Ok(resolved) => Some(resolved),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let current = match &resolved {
            Some(resolved) => Some(std::fs::read(resolved)?),
            None => None,
        };
        // Asked of the file just read, and answered only when there is something to write.
        let kept_mode = resolved.as_deref().map(writable_mode);
        let Some(replacement) = edit(current.as_deref()) else {
            return Ok(false);
        };
        match resolved {
            Some(target) => replace(&target, kept_mode.transpose()?, &replacement)?,
            // Nothing resolves, yet an entry is there: a symbolic link to a file that does not exist. Renaming
            // over it would replace the link itself.
            None if std::fs::symlink_metadata(path).is_ok() => {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "a symbolic link to a file that does not exist",
                ));
            }
            None => replace(path, None, &replacement)?,
        }
        Ok(true)
    }
}

/// The permission bits of `target`, which must be a file that can be opened for writing.
///
/// A rename asks only the directory for permission, so without this a file that refuses a plain write would
/// be replaced all the same.
fn writable_mode(target: &Path) -> io::Result<u32> {
    let file = std::fs::OpenOptions::new().write(true).open(target)?;
    Ok(file.metadata()?.permissions().mode() & 0o7777)
}

/// Takes the exclusive advisory lock of `directory`, waiting for as long as another handle holds it.
fn lock_exclusive(directory: &std::fs::File) -> io::Result<()> {
    loop {
        match directory.lock() {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            result => return result,
        }
    }
}

/// Replaces `target` with `content` through a temporary file beside it, which does not outlive the call.
/// The new file gets the permission bits `mode`, or those of a plain create without one.
fn replace(target: &Path, mode: Option<u32>, content: &[u8]) -> io::Result<()> {
    let (temporary, file) = create_beside(target)?;
    let replaced =
        write_whole(file, mode, content).and_then(|()| std::fs::rename(&temporary, target));
    if replaced.is_err() {
        // The failure of the write is the one reported; the removal has nothing to add to it.
        let _ = std::fs::remove_file(&temporary);
    }
    replaced
}

/// Creates a file that did not exist in the directory of `target`, under a name derived from the target's.
fn create_beside(target: &Path) -> io::Result<(PathBuf, std::fs::File)> {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    let name = target
        .file_name()
        .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    loop {
        let mut temporary_name = OsString::from(".");
        temporary_name.push(name);
        temporary_name.push(format!(
            ".{}.{}.tmp",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let temporary = target.with_file_name(temporary_name);
        match std::fs::File::create_new(&temporary) {
            Ok(file) => return Ok((temporary, file)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
}

/// Gives `file` the permission bits `mode` when there is one, writes `content`, and flushes it to the device.
fn write_whole(mut file: std::fs::File, mode: Option<u32>, content: &[u8]) -> io::Result<()> {
    if let Some(mode) = mode {
        file.set_permissions(std::fs::Permissions::from_mode(mode))?;
    }
    file.write_all(content)?;
    file.sync_all()
}

/// Files held in memory, for the unit tests of the modules that read through the port.
///
/// Each path holds one entry. Every directory above an added path is a directory, unless an entry was added
/// there. The reads follow a symbolic link, at any part of a path, as [`OsFiles`] does: its target text is read
/// from the directory that holds the link.
#[cfg(test)]
#[derive(Default)]
pub(crate) struct MemoryFiles(std::collections::BTreeMap<PathBuf, MemoryEntry>);

/// One entry of [`MemoryFiles`].
#[cfg(test)]
enum MemoryEntry {
    /// A regular file with its content.
    File(Vec<u8>),
    /// A regular file whose every read fails.
    FailingFile(io::ErrorKind),
    /// An entry that is no regular file and no directory, and whose every read fails.
    NotAFile(io::ErrorKind),
    /// A directory.
    Dir,
    /// A directory whose listing fails.
    FailingDir(io::ErrorKind),
    /// A symbolic link with its target text.
    Link(PathBuf),
}

#[cfg(test)]
impl MemoryFiles {
    /// How many symbolic links one read follows before it takes the path for one that leads nowhere.
    const LINKS_FOLLOWED: usize = 40;

    /// Adds the regular file `path` with `content`.
    pub(crate) fn with(self, path: &str, content: &[u8]) -> Self {
        self.holding(path, MemoryEntry::File(content.to_vec()))
    }

    /// Adds the regular file `path`, which fails every read with `kind`.
    pub(crate) fn failing(self, path: &str, kind: io::ErrorKind) -> Self {
        self.holding(path, MemoryEntry::FailingFile(kind))
    }

    /// Adds an entry at `path` that exists, is no regular file and no directory, and fails every read with
    /// `kind`.
    pub(crate) fn unreadable(self, path: &str, kind: io::ErrorKind) -> Self {
        self.holding(path, MemoryEntry::NotAFile(kind))
    }

    /// Adds the directory `path`.
    pub(crate) fn dir(self, path: &str) -> Self {
        self.holding(path, MemoryEntry::Dir)
    }

    /// Adds the directory `path`, whose listing fails with `kind`.
    pub(crate) fn failing_dir(self, path: &str, kind: io::ErrorKind) -> Self {
        self.holding(path, MemoryEntry::FailingDir(kind))
    }

    /// Adds the symbolic link `path` with the target text `target`.
    pub(crate) fn link(self, path: &str, target: &str) -> Self {
        self.holding(path, MemoryEntry::Link(target.into()))
    }

    /// Puts `entry` at `path`, and a directory at every path above it that holds no entry yet.
    fn holding(mut self, path: &str, entry: MemoryEntry) -> Self {
        let path = PathBuf::from(path);
        for above in path.ancestors().skip(1) {
            if !above.as_os_str().is_empty() {
                self.0
                    .entry(above.to_path_buf())
                    .or_insert(MemoryEntry::Dir);
            }
        }
        self.0.insert(path, entry);
        self
    }

    /// Where `path` leads with every symbolic link followed, or `None` when it follows more links than a read
    /// does. The path answered need not hold an entry.
    fn followed(&self, path: &Path) -> Option<PathBuf> {
        let mut ahead: std::collections::VecDeque<PathBuf> = parts(path).collect();
        let mut at = PathBuf::new();
        let mut links = 0;
        while let Some(part) = ahead.pop_front() {
            if part == Path::new("..") {
                at.pop();
                continue;
            }
            // A root replaces what was walked so far, which is how an absolute target leaves its directory.
            at.push(part);
            if let Some(MemoryEntry::Link(target)) = self.0.get(&at) {
                links += 1;
                if links > Self::LINKS_FOLLOWED {
                    return None;
                }
                at.pop();
                for part in parts(target).rev() {
                    ahead.push_front(part);
                }
            }
        }
        Some(at)
    }

    /// The entry `path` leads to with every symbolic link followed, and the path that holds it.
    fn entry(&self, path: &Path) -> Option<(PathBuf, &MemoryEntry)> {
        let at = self.followed(path)?;
        let entry = self.0.get(&at)?;
        Some((at, entry))
    }
}

/// The parts of `path` a walk takes one at a time: its root, its names, and each `..`.
#[cfg(test)]
fn parts(path: &Path) -> impl DoubleEndedIterator<Item = PathBuf> + '_ {
    path.components()
        .filter(|part| !matches!(part, std::path::Component::CurDir))
        .map(|part| PathBuf::from(part.as_os_str()))
}

#[cfg(test)]
impl ReadFiles for MemoryFiles {
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        match self.entry(path) {
            Some((_, MemoryEntry::File(bytes))) => Ok(Some(bytes.clone())),
            Some((_, MemoryEntry::FailingFile(kind) | MemoryEntry::NotAFile(kind))) => {
                Err(io::Error::from(*kind))
            }
            Some((_, MemoryEntry::Dir | MemoryEntry::FailingDir(_))) => {
                Err(io::Error::from(io::ErrorKind::IsADirectory))
            }
            Some((_, MemoryEntry::Link(_))) | None => Ok(None),
        }
    }

    fn is_file(&self, path: &Path) -> bool {
        matches!(
            self.entry(path),
            Some((_, MemoryEntry::File(_) | MemoryEntry::FailingFile(_)))
        )
    }

    fn exists(&self, path: &Path) -> bool {
        self.entry(path).is_some()
    }

    fn is_dir(&self, path: &Path) -> bool {
        matches!(
            self.entry(path),
            Some((_, MemoryEntry::Dir | MemoryEntry::FailingDir(_)))
        )
    }

    fn entries(&self, path: &Path) -> io::Result<Vec<OsString>> {
        match self.entry(path) {
            Some((directory, MemoryEntry::Dir)) => Ok(self
                .0
                .keys()
                .filter(|held| held.parent() == Some(directory.as_path()))
                .filter_map(|held| held.file_name().map(OsString::from))
                .collect()),
            Some((_, MemoryEntry::FailingDir(kind))) => Err(io::Error::from(*kind)),
            Some(_) => Err(io::Error::from(io::ErrorKind::NotADirectory)),
            None => Err(io::Error::from(io::ErrorKind::NotFound)),
        }
    }

    fn read_link(&self, path: &Path) -> io::Result<Option<PathBuf>> {
        let (Some(above), Some(name)) = (path.parent(), path.file_name()) else {
            return Ok(None);
        };
        let held = self.followed(above).map(|above| above.join(name));
        match held.and_then(|held| self.0.get(&held)) {
            Some(MemoryEntry::Link(target)) => Ok(Some(target.clone())),
            _ => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use winter_workspace_test_support::TempTree;

    use super::*;

    #[test]
    fn directories_are_created_recursively_and_an_existing_directory_succeeds() {
        let tree = TempTree::new().expect("an isolated tree");
        let path = tree.root().join("created/child");
        OsFiles.create_dir_all(&path).expect("create parents");
        OsFiles.create_dir_all(&path).expect("existing directory");
        assert!(path.is_dir());
        let file = path.join("file");
        std::fs::write(&file, b"kept").expect("file");
        assert!(OsFiles.create_dir_all(&file).is_err());
        assert!(OsFiles.create_dir_all(&file.join("child")).is_err());
        assert_eq!(std::fs::read(file).expect("unchanged file"), b"kept");
    }

    #[test]
    fn removal_handles_files_directories_and_missing_entries() {
        let tree = TempTree::new().expect("an isolated tree");
        let path = tree.root().join("removed");
        OsFiles.remove(&path).expect("missing entry");
        std::fs::write(&path, b"bytes").expect("file");
        OsFiles.remove(&path).expect("remove file");
        assert!(!path.exists());
        std::fs::create_dir_all(path.join("nested")).expect("directories");
        std::fs::write(path.join("nested/file"), b"bytes").expect("nested file");
        OsFiles.remove(&path).expect("remove whole directory");
        assert!(!path.exists());
        OsFiles.remove(&path).expect("already removed");
    }

    #[test]
    fn removal_unlinks_live_and_dangling_links_without_touching_their_targets() {
        let tree = TempTree::new().expect("an isolated tree");
        let target = tree.root().join("kept");
        std::fs::create_dir(&target).expect("target directory");
        std::fs::write(target.join("file"), b"kept").expect("target file");
        for target in [target.clone(), target.join("file"), target.join("missing")] {
            let link = tree.root().join("link");
            std::os::unix::fs::symlink(&target, &link).expect("link");
            OsFiles.remove(&link).expect("unlink");
            assert!(std::fs::symlink_metadata(link).is_err());
        }
        assert!(target.is_dir());
        assert_eq!(
            std::fs::read(target.join("file")).expect("kept file"),
            b"kept"
        );

        let removed = tree.root().join("removed");
        std::fs::create_dir_all(removed.join("nested")).expect("subtree");
        for (name, destination) in [
            ("directory", target.clone()),
            ("file", target.join("file")),
            ("dangling", target.join("missing")),
        ] {
            std::os::unix::fs::symlink(destination, removed.join("nested").join(name))
                .expect("nested link");
        }
        OsFiles
            .remove(&removed)
            .expect("remove subtree without following links");
        assert!(!removed.exists());
        assert!(target.is_dir());
        assert_eq!(
            std::fs::read(target.join("file")).expect("kept file"),
            b"kept"
        );
    }

    fn names(directory: &Path) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(directory)
            .expect("a readable directory")
            .map(|entry| {
                let name = entry.expect("a directory entry").file_name();
                name.to_string_lossy().into_owned()
            })
            .collect();
        names.sort();
        names
    }

    #[test]
    fn a_scratch_copy_keeps_the_bytes_and_the_modification_time_and_goes_with_its_handle() {
        let tree = TempTree::new().expect("an isolated tree");
        let temp = tree.root().join("temp");
        std::fs::create_dir(&temp).expect("a temp directory");
        let source = tree.root().join("index");
        std::fs::write(&source, b"bytes").expect("a source");
        let when = std::time::SystemTime::UNIX_EPOCH
            + std::time::Duration::new(1_700_000_000, 123_456_789);
        std::fs::File::options()
            .write(true)
            .open(&source)
            .expect("the source")
            .set_modified(when)
            .expect("a set time");

        let copy = OsFiles.scratch_copy(&temp, &source).expect("a copy");

        assert_eq!(std::fs::read(copy.path()).expect("the copy"), b"bytes");
        let modified = std::fs::metadata(copy.path())
            .expect("the copy")
            .modified()
            .expect("a time");
        assert_eq!(modified, when);
        assert_eq!(
            copy.path().parent().and_then(Path::parent),
            Some(temp.as_path())
        );
        let directory = copy.path().parent().expect("a directory").to_path_buf();
        drop(copy);
        assert!(!directory.exists());
        assert!(names(&temp).is_empty());
    }

    #[test]
    fn a_scratch_path_names_nothing_and_its_directory_goes_with_its_handle() {
        let tree = TempTree::new().expect("an isolated tree");
        let path = OsFiles
            .scratch_path(tree.root(), "index")
            .expect("a scratch path");

        assert!(!path.path().exists());
        assert!(path.path().parent().expect("a directory").is_dir());
        let directory = path.path().parent().expect("a directory").to_path_buf();
        drop(path);
        assert!(!directory.exists());
    }

    #[test]
    fn two_scratch_handles_in_one_directory_get_distinct_directories() {
        let tree = TempTree::new().expect("an isolated tree");
        let first = OsFiles.scratch_path(tree.root(), "index").expect("one");
        let second = OsFiles.scratch_path(tree.root(), "index").expect("two");

        assert_ne!(first.path(), second.path());
    }

    #[test]
    fn an_absent_directory_and_target_are_created_with_the_mode_of_a_plain_create() {
        let tree = TempTree::new().expect("an isolated tree");
        let target = tree.root().join("made/deeper/state.toml");
        let plain = tree.root().join("plain");
        std::fs::write(&plain, b"").expect("a plain file");
        let mut seen = Vec::new();

        let replaced = OsFiles.update(&target, &mut |current| {
            seen.push(current.map(<[u8]>::to_vec));
            Some(b"a = 1\n".to_vec())
        });

        assert!(matches!(replaced, Ok(true)), "{replaced:?}");
        assert_eq!(seen, [None]);
        assert_eq!(std::fs::read(&target).expect("the target"), b"a = 1\n");
        assert_eq!(names(&tree.root().join("made/deeper")), ["state.toml"]);
        let mode = |path: &Path| {
            std::fs::metadata(path)
                .expect("metadata")
                .permissions()
                .mode()
        };
        assert_eq!(mode(&target), mode(&plain));
    }

    #[test]
    fn an_absent_target_the_edit_leaves_alone_is_not_created() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");

        let replaced = OsFiles.update(&directory.join("state.toml"), &mut |_| None);

        assert!(matches!(replaced, Ok(false)), "{replaced:?}");
        assert_eq!(names(&directory), [] as [&str; 0]);
    }

    #[test]
    fn a_link_to_a_file_that_does_not_exist_is_not_written_through_and_stays() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let link = directory.join("config.toml");
        std::os::unix::fs::symlink("gone/config.toml", &link).expect("a link");
        let mut seen = Vec::new();

        let replaced = OsFiles.update(&link, &mut |current| {
            seen.push(current.map(<[u8]>::to_vec));
            Some(b"a = 1\n".to_vec())
        });

        assert_eq!(
            replaced.map_err(|error| error.kind()),
            Err(io::ErrorKind::NotFound)
        );
        assert_eq!(seen, [None]);
        assert_eq!(names(&directory), ["config.toml"]);
        assert_eq!(
            std::fs::read_link(&link).expect("still a link"),
            Path::new("gone/config.toml")
        );
        assert!(
            OsFiles.update(&link, &mut |_| None).is_ok(),
            "the lock is free"
        );
    }

    #[test]
    fn the_temporary_file_is_removed_when_the_rename_fails() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let target = directory.join("config.toml");
        std::fs::write(&target, b"a = 1\n").expect("the target");

        // The edit puts a directory that is not empty where the target was: no file can be renamed over it.
        let replaced = OsFiles.update(&target, &mut |_| {
            std::fs::remove_file(&target).expect("the target removed");
            std::fs::create_dir(&target).expect("a directory in its place");
            std::fs::write(target.join("child"), b"").expect("a child");
            Some(b"a = 2\n".to_vec())
        });

        assert!(replaced.is_err(), "{replaced:?}");
        assert_eq!(names(&directory), ["config.toml"]);
        assert_eq!(names(&target), ["child"]);
        assert!(OsFiles.update(&target.join("child"), &mut |_| None).is_ok());
    }

    #[test]
    fn a_target_that_cannot_be_opened_for_writing_is_not_replaced() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let target = directory.join("config.toml");
        std::fs::write(&target, b"a = 1\n").expect("the target");
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o444)).expect("a mode");
        if std::fs::OpenOptions::new()
            .write(true)
            .open(&target)
            .is_ok()
        {
            // This user may write any file, and then no mode refuses a plain write either.
            return;
        }

        let replaced = OsFiles.update(&target, &mut |_| Some(b"a = 2\n".to_vec()));

        assert_eq!(
            replaced.map_err(|error| error.kind()),
            Err(io::ErrorKind::PermissionDenied)
        );
        assert_eq!(std::fs::read(&target).expect("the target"), b"a = 1\n");
        assert_eq!(names(&directory), ["config.toml"]);
        let mode = std::fs::metadata(&target)
            .expect("metadata")
            .permissions()
            .mode();
        assert_eq!(mode & 0o7777, 0o444);
    }

    #[test]
    fn a_second_hard_link_of_the_target_keeps_the_old_content() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let target = directory.join("config.toml");
        let other = directory.join("other.toml");
        std::fs::write(&target, b"a = 1\n").expect("the target");
        std::fs::hard_link(&target, &other).expect("a second link");

        let replaced = OsFiles.update(&target, &mut |_| Some(b"a = 2\n".to_vec()));

        assert!(matches!(replaced, Ok(true)), "{replaced:?}");
        assert_eq!(std::fs::read(&target).expect("the target"), b"a = 2\n");
        assert_eq!(std::fs::read(&other).expect("the other name"), b"a = 1\n");
        assert_eq!(names(&directory), ["config.toml", "other.toml"]);
    }

    #[test]
    fn a_target_in_a_directory_that_takes_no_new_file_is_refused_and_left_as_it_was() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let target = directory.join("config.toml");
        std::fs::write(&target, b"a = 1\n").expect("the target");
        let mode =
            |mode| std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(mode));
        mode(0o555).expect("a mode");
        let probe = directory.join("probe");
        if std::fs::File::create_new(&probe).is_ok() {
            // This user may write any file, and then no mode keeps a file out of a directory either.
            std::fs::remove_file(&probe).expect("the probe removed");
            mode(0o755).expect("the mode restored");
            return;
        }
        let mut seen = Vec::new();

        let replaced = OsFiles.update(&target, &mut |current| {
            seen.push(current.map(<[u8]>::to_vec));
            Some(b"a = 2\n".to_vec())
        });
        mode(0o755).expect("the mode restored");

        assert_eq!(
            replaced.map_err(|error| error.kind()),
            Err(io::ErrorKind::PermissionDenied)
        );
        assert_eq!(seen, [Some(b"a = 1\n".to_vec())]);
        assert_eq!(std::fs::read(&target).expect("the target"), b"a = 1\n");
        assert_eq!(names(&directory), ["config.toml"]);
    }

    #[test]
    fn a_target_in_a_directory_that_cannot_be_read_is_refused_and_left_as_it_was() {
        let tree = TempTree::new().expect("an isolated tree");
        let directory = tree.root().join("held");
        std::fs::create_dir(&directory).expect("a directory");
        let target = directory.join("config.toml");
        std::fs::write(&target, b"a = 1\n").expect("the target");
        let mode =
            |mode| std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(mode));
        // Written and searched, and not read.
        mode(0o333).expect("a mode");
        if std::fs::File::open(&directory).is_ok() {
            // This user may read any file, and then no mode keeps a directory from being opened either.
            mode(0o755).expect("the mode restored");
            return;
        }
        let mut asked = 0;

        let replaced = OsFiles.update(&target, &mut |_| {
            asked += 1;
            Some(b"a = 2\n".to_vec())
        });
        mode(0o755).expect("the mode restored");

        assert_eq!(
            replaced.map_err(|error| error.kind()),
            Err(io::ErrorKind::PermissionDenied)
        );
        assert_eq!(asked, 0, "the edit was asked before the lock was taken");
        assert_eq!(std::fs::read(&target).expect("the target"), b"a = 1\n");
        assert_eq!(names(&directory), ["config.toml"]);
    }

    /// A tree of each kind of entry the reads tell apart, on the file system, and its root.
    fn kinds_on_disk(tree: &TempTree) -> PathBuf {
        let root = tree.root().join("kinds");
        std::fs::create_dir_all(root.join("dir/inner")).expect("directories");
        std::fs::write(root.join("file"), b"content").expect("a file");
        std::fs::write(root.join("dir/plain"), b"").expect("a file");
        std::fs::write(root.join("dir/.hidden"), b"").expect("a dot-entry");
        for (link, target) in [("to-file", "file"), ("to-dir", "dir"), ("dangling", "gone")] {
            std::os::unix::fs::symlink(target, root.join(link)).expect("a link");
        }
        root
    }

    /// The same tree held in memory.
    fn kinds_in_memory() -> (MemoryFiles, PathBuf) {
        let files = MemoryFiles::default()
            .with("/kinds/file", b"content")
            .dir("/kinds/dir/inner")
            .with("/kinds/dir/plain", b"")
            .with("/kinds/dir/.hidden", b"")
            .link("/kinds/to-file", "file")
            .link("/kinds/to-dir", "dir")
            .link("/kinds/dangling", "gone");
        (files, PathBuf::from("/kinds"))
    }

    /// Asserts what the reads of `files` answer for each kind of entry under `root`.
    fn assert_the_reads_tell_the_kinds_apart(files: &dyn ReadFiles, root: &Path) {
        for (name, exists, is_file, is_dir) in [
            ("file", true, true, false),
            ("dir", true, false, true),
            ("to-file", true, true, false),
            ("to-dir", true, false, true),
            ("dangling", false, false, false),
            ("absent", false, false, false),
        ] {
            let path = root.join(name);
            assert_eq!(files.exists(&path), exists, "exists: {name}");
            assert_eq!(files.is_file(&path), is_file, "is_file: {name}");
            assert_eq!(files.is_dir(&path), is_dir, "is_dir: {name}");
        }
        for directory in ["dir", "to-dir"] {
            let mut entries = files
                .entries(&root.join(directory))
                .expect("a listed directory");
            entries.sort();
            assert_eq!(entries, [".hidden", "inner", "plain"], "{directory}");
        }
        for (name, kind) in [
            ("absent", io::ErrorKind::NotFound),
            ("dangling", io::ErrorKind::NotFound),
            ("file", io::ErrorKind::NotADirectory),
        ] {
            assert_eq!(
                files
                    .entries(&root.join(name))
                    .map_err(|error| error.kind()),
                Err(kind),
                "{name}"
            );
        }
        assert!(files.is_file(&root.join("to-dir/plain")), "through a link");
        assert_eq!(
            files
                .read(&root.join("to-file"))
                .expect("a read through a link"),
            Some(b"content".to_vec())
        );
        assert!(matches!(files.read(&root.join("dangling")), Ok(None)));
    }

    #[test]
    fn the_reads_of_the_file_system_tell_a_file_a_directory_and_a_link_to_each_apart() {
        let tree = TempTree::new().expect("an isolated tree");
        let root = kinds_on_disk(&tree);

        assert_the_reads_tell_the_kinds_apart(&OsFiles, &root);
    }

    #[test]
    fn the_reads_of_the_memory_files_tell_a_file_a_directory_and_a_link_to_each_apart() {
        let (files, root) = kinds_in_memory();

        assert_the_reads_tell_the_kinds_apart(&files, &root);
    }

    /// Asserts what `read_link` of `files` answers for each kind of entry under `root`, which holds the tree of
    /// the kinds and the link `absolute`, whose target text is `/somewhere/else`.
    fn assert_a_link_gives_its_target_as_written_and_nothing_else_gives_one(
        files: &dyn ReadFiles,
        root: &Path,
    ) {
        for (name, target) in [
            ("to-file", Some("file")),
            ("to-dir", Some("dir")),
            ("dangling", Some("gone")),
            ("absolute", Some("/somewhere/else")),
            ("to-dir/plain", None),
            ("file", None),
            ("dir", None),
            ("absent", None),
            ("file/below", None),
        ] {
            assert_eq!(
                files
                    .read_link(&root.join(name))
                    .map_err(|error| error.kind()),
                Ok(target.map(PathBuf::from)),
                "{name}"
            );
        }
    }

    #[test]
    fn a_link_of_the_file_system_gives_its_target_as_written_and_nothing_else_gives_one() {
        let tree = TempTree::new().expect("an isolated tree");
        let root = kinds_on_disk(&tree);
        std::os::unix::fs::symlink("/somewhere/else", root.join("absolute")).expect("a link");

        assert_a_link_gives_its_target_as_written_and_nothing_else_gives_one(&OsFiles, &root);
    }

    #[test]
    fn a_link_of_the_memory_files_gives_its_target_as_written_and_nothing_else_gives_one() {
        let (files, root) = kinds_in_memory();
        let files = files.link("/kinds/absolute", "/somewhere/else");

        assert_a_link_gives_its_target_as_written_and_nothing_else_gives_one(&files, &root);
    }

    /// Asserts what `resolve` of `files` answers for paths under `root`, which holds the links `via -> real`,
    /// `up -> ..`, `abs -> <root>/real`, `a -> b`, `b -> a`, `c -> a`, and the directory `real`.
    fn assert_resolve_follows_links_and_keeps_the_rest_as_text(files: &dyn ReadFiles, root: &Path) {
        let at = |name: &str| root.join(name);
        for (given, resolved) in [
            ("real", at("real")),
            ("real/", at("real")),
            ("via", at("real")),
            ("via/x/y", at("real/x/y")),
            ("abs", at("real")),
            ("absent/deeper", at("absent/deeper")),
            ("absent/../real", at("real")),
            ("./real/./x", at("real/x")),
            ("real/..", root.to_path_buf()),
            ("up", root.parent().expect("a parent").to_path_buf()),
            ("c", at("a")),
            ("b", at("b")),
            ("a", at("a")),
        ] {
            assert_eq!(files.resolve(&root.join(given)), resolved, "{given}");
        }
    }

    #[test]
    fn resolve_of_the_file_system_follows_links_and_keeps_the_rest_as_text() {
        let tree = TempTree::new().expect("an isolated tree");
        let root = tree.root().join("resolving");
        std::fs::create_dir_all(root.join("real")).expect("a directory");
        for (link, target) in [
            ("via", "real".into()),
            ("up", "..".into()),
            ("abs", root.join("real")),
            ("a", "b".into()),
            ("b", "a".into()),
            ("c", "a".into()),
        ] {
            std::os::unix::fs::symlink(target, root.join(link)).expect("a link");
        }

        assert_resolve_follows_links_and_keeps_the_rest_as_text(&OsFiles, &root);
    }

    #[test]
    fn resolve_of_the_memory_files_follows_links_and_keeps_the_rest_as_text() {
        let files = MemoryFiles::default()
            .dir("/resolving/real")
            .link("/resolving/via", "real")
            .link("/resolving/up", "..")
            .link("/resolving/abs", "/resolving/real")
            .link("/resolving/a", "b")
            .link("/resolving/b", "a")
            .link("/resolving/c", "a");

        assert_resolve_follows_links_and_keeps_the_rest_as_text(&files, Path::new("/resolving"));
    }

    #[test]
    fn a_directory_above_an_added_path_is_a_directory_of_the_memory_files() {
        let files = MemoryFiles::default().with("/ws/.winter/config.toml", b"");

        for above in ["/", "/ws", "/ws/.winter"] {
            assert!(files.is_dir(Path::new(above)), "{above}");
            assert!(!files.is_file(Path::new(above)), "{above}");
        }
        assert_eq!(
            files.entries(Path::new("/ws")).expect("a listing"),
            [".winter"]
        );
        assert_eq!(
            files
                .read(Path::new("/ws/.winter"))
                .map_err(|error| error.kind()),
            Err(io::ErrorKind::IsADirectory)
        );
    }

    #[test]
    fn an_entry_that_fails_reads_is_no_directory_and_a_failing_directory_is_one() {
        let files = MemoryFiles::default()
            .unreadable("/ws/odd", io::ErrorKind::PermissionDenied)
            .failing("/ws/file", io::ErrorKind::PermissionDenied)
            .failing_dir("/ws/shut", io::ErrorKind::PermissionDenied);

        for (path, exists, is_file, is_dir, listing) in [
            ("/ws/odd", true, false, false, io::ErrorKind::NotADirectory),
            ("/ws/file", true, true, false, io::ErrorKind::NotADirectory),
            (
                "/ws/shut",
                true,
                false,
                true,
                io::ErrorKind::PermissionDenied,
            ),
        ] {
            let path = Path::new(path);
            assert_eq!(files.exists(path), exists, "{path:?}");
            assert_eq!(files.is_file(path), is_file, "{path:?}");
            assert_eq!(files.is_dir(path), is_dir, "{path:?}");
            assert_eq!(
                files.entries(path).map_err(|error| error.kind()),
                Err(listing),
                "{path:?}"
            );
        }
    }

    #[test]
    fn a_link_of_the_memory_files_is_followed_from_its_own_directory_and_a_loop_leads_nowhere() {
        let files = MemoryFiles::default()
            .with("/ws/real/file", b"x")
            .link("/ws/links/up", "../real")
            .link("/ws/links/absolute", "/ws/real/file")
            .link("/ws/links/again", "up/file")
            .link("/ws/links/loop", "loop");

        for path in [
            "/ws/links/up/file",
            "/ws/links/absolute",
            "/ws/links/again",
            "/ws/links/./up/../real/file",
        ] {
            assert!(files.is_file(Path::new(path)), "{path}");
        }
        // `..` after a link is the directory above the link's target, not the one that holds the link.
        assert!(!files.exists(Path::new("/ws/links/up/../up")));
        assert!(!files.exists(Path::new("/ws/links/loop")));
        let mut entries = files
            .entries(Path::new("/ws/links"))
            .expect("a listed directory");
        entries.sort();
        assert_eq!(entries, ["absolute", "again", "loop", "up"]);
    }
}
