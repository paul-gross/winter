//! The workspace root of a directory: the nearest directory, from it upward, that holds the shared
//! configuration file.

use std::path::{Path, PathBuf};

use crate::files::ReadFiles;
use crate::termination::ConfigError;

/// The shared configuration file, relative to a workspace root.
pub(crate) const CONFIG_FILE: &str = ".winter/config.toml";

/// The workspace root of `start`: `start` itself or its nearest ancestor in which [`CONFIG_FILE`] is a
/// regular file. A directory that holds a bare `.winter/` does not qualify.
pub(crate) fn find(files: &dyn ReadFiles, start: &Path) -> Result<PathBuf, ConfigError> {
    start
        .ancestors()
        .find(|directory| files.is_file(&directory.join(CONFIG_FILE)))
        .map(Path::to_path_buf)
        .ok_or_else(|| ConfigError::RootNotFound {
            start: start.to_path_buf(),
        })
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::files::MemoryFiles;

    #[test]
    fn the_start_directory_is_the_root_when_it_holds_the_configuration_file() {
        let files = MemoryFiles::default().with("/ws/.winter/config.toml", b"");

        let root = find(&files, Path::new("/ws")).expect("a root");

        assert_eq!(root, Path::new("/ws"));
    }

    #[test]
    fn an_ancestor_that_holds_the_configuration_file_is_the_root() {
        let files = MemoryFiles::default().with("/ws/.winter/config.toml", b"");

        let root = find(&files, Path::new("/ws/alpha/project/src")).expect("a root");

        assert_eq!(root, Path::new("/ws"));
    }

    #[test]
    fn the_nearest_qualifying_ancestor_wins() {
        let files = MemoryFiles::default()
            .with("/ws/.winter/config.toml", b"")
            .with("/ws/alpha/nested/.winter/config.toml", b"");

        let root = find(&files, Path::new("/ws/alpha/nested/src")).expect("a root");

        assert_eq!(root, Path::new("/ws/alpha/nested"));
    }

    #[test]
    fn a_configuration_path_that_is_no_regular_file_does_not_qualify() {
        let files = MemoryFiles::default()
            .with("/ws/.winter/config.toml", b"")
            .unreadable("/ws/alpha/.winter/config.toml", ErrorKind::IsADirectory);

        let root = find(&files, Path::new("/ws/alpha")).expect("a root");

        assert_eq!(root, Path::new("/ws"));
    }

    #[test]
    fn no_configuration_file_at_or_above_the_start_names_the_start() {
        let files = MemoryFiles::default().with("/ws/.winter/config.toml", b"");

        let error = find(&files, Path::new("/elsewhere/below")).expect_err("no root");

        assert!(
            matches!(&error, ConfigError::RootNotFound { start } if start == Path::new("/elsewhere/below")),
            "{error:?}"
        );
    }
}
