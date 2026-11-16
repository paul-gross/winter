//! The file port: how the library reads and writes files, and its one adapter over the operating system.
//!
//! Reading and writing are separate authorities: a module that takes only [`ReadFiles`] cannot change a file.

use std::ffi::OsString;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// Reads files.
pub(crate) trait ReadFiles {
    /// The bytes of the file at `path`, or `Ok(None)` when no such file exists.
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>>;

    /// Whether `path` is a regular file, with symbolic links followed.
    fn is_file(&self, path: &Path) -> bool;
}

/// What one update does to a file: it gets the current bytes, `None` when no such file exists, and returns the
/// new bytes, or `None` to leave the file as it is.
pub(crate) type Edit<'a> = dyn FnMut(Option<&[u8]>) -> Option<Vec<u8>> + 'a;

/// Writes files.
pub(crate) trait WriteFiles {
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

/// The files of the operating system.
pub(crate) struct OsFiles;

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
}

impl WriteFiles for OsFiles {
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
#[cfg(test)]
#[derive(Default)]
pub(crate) struct MemoryFiles(std::collections::BTreeMap<std::path::PathBuf, MemoryEntry>);

/// One entry of [`MemoryFiles`].
#[cfg(test)]
enum MemoryEntry {
    /// A regular file with its content.
    File(Vec<u8>),
    /// A regular file whose every read fails.
    FailingFile(io::ErrorKind),
    /// An entry that is no regular file, and whose every read fails.
    NotAFile(io::ErrorKind),
}

#[cfg(test)]
impl MemoryFiles {
    /// Adds the regular file `path` with `content`.
    pub(crate) fn with(mut self, path: &str, content: &[u8]) -> Self {
        self.0
            .insert(path.into(), MemoryEntry::File(content.to_vec()));
        self
    }

    /// Adds the regular file `path`, which fails every read with `kind`.
    pub(crate) fn failing(mut self, path: &str, kind: io::ErrorKind) -> Self {
        self.0.insert(path.into(), MemoryEntry::FailingFile(kind));
        self
    }

    /// Adds an entry at `path` that exists, is no regular file, and fails every read with `kind`.
    pub(crate) fn unreadable(mut self, path: &str, kind: io::ErrorKind) -> Self {
        self.0.insert(path.into(), MemoryEntry::NotAFile(kind));
        self
    }
}

#[cfg(test)]
impl ReadFiles for MemoryFiles {
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        match self.0.get(path) {
            Some(MemoryEntry::File(bytes)) => Ok(Some(bytes.clone())),
            Some(MemoryEntry::FailingFile(kind) | MemoryEntry::NotAFile(kind)) => {
                Err(io::Error::from(*kind))
            }
            None => Ok(None),
        }
    }

    fn is_file(&self, path: &Path) -> bool {
        matches!(
            self.0.get(path),
            Some(MemoryEntry::File(_) | MemoryEntry::FailingFile(_))
        )
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use winter_workspace_test_support::TempTree;

    use super::*;

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
}
