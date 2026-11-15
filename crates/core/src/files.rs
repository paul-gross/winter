//! The file port: how the library reads files, and its one adapter over the operating system.
//!
//! Reading is its own authority: a module that takes only [`ReadFiles`] cannot change a file.

use std::io;
use std::path::Path;

/// Reads files.
pub(crate) trait ReadFiles {
    /// The bytes of the file at `path`, or `Ok(None)` when no such file exists.
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>>;

    /// Whether `path` is a regular file, with symbolic links followed.
    fn is_file(&self, path: &Path) -> bool;
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

/// Files held in memory, for the unit tests of the modules that read through the port.
#[cfg(test)]
#[derive(Default)]
pub(crate) struct MemoryFiles(
    std::collections::BTreeMap<std::path::PathBuf, Result<Vec<u8>, io::ErrorKind>>,
);

#[cfg(test)]
impl MemoryFiles {
    /// Adds the regular file `path` with `content`.
    pub(crate) fn with(mut self, path: &str, content: &[u8]) -> Self {
        self.0.insert(path.into(), Ok(content.to_vec()));
        self
    }

    /// Adds an entry at `path` that exists, is no regular file, and fails every read with `kind`.
    pub(crate) fn unreadable(mut self, path: &str, kind: io::ErrorKind) -> Self {
        self.0.insert(path.into(), Err(kind));
        self
    }
}

#[cfg(test)]
impl ReadFiles for MemoryFiles {
    fn read(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        match self.0.get(path) {
            Some(Ok(bytes)) => Ok(Some(bytes.clone())),
            Some(Err(kind)) => Err(io::Error::from(*kind)),
            None => Ok(None),
        }
    }

    fn is_file(&self, path: &Path) -> bool {
        matches!(self.0.get(path), Some(Ok(_)))
    }
}
