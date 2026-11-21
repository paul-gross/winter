//! The errors of reading the lock file.

use std::path::PathBuf;

use crate::termination::{ConfigValue, FileCause};

/// A failure to read the lock file, carried as `ConfigError::Lock`.
#[derive(Debug, thiserror::Error)]
pub enum LockError {
    /// The file is unreadable, or not valid TOML.
    #[error("cannot load {}: {cause}", .path.display())]
    Load {
        path: PathBuf,
        #[source]
        cause: FileCause,
    },
    #[error("{}: unsupported lock file version", .path.display())]
    UnsupportedVersion {
        path: PathBuf,
        found: Option<ConfigValue>,
    },
    #[error("{}: malformed [[standalone]] entry", .path.display())]
    MalformedEntry {
        path: PathBuf,
        fault: LockEntryFault,
    },
}

/// What is wrong with a `[[standalone]]` entry of the lock.
#[derive(Debug)]
pub enum LockEntryFault {
    /// A field the entry lacks: `name`, `ref`, `kind`, or `commit`.
    MissingField {
        field: &'static str,
    },
    InvalidKind {
        value: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unsupported_version_names_the_file() {
        let error = LockError::UnsupportedVersion {
            path: PathBuf::from("/ws/.winter/config.lock"),
            found: None,
        };
        assert_eq!(
            error.to_string(),
            "/ws/.winter/config.lock: unsupported lock file version"
        );
    }

    #[test]
    fn a_malformed_entry_names_the_file() {
        let error = LockError::MalformedEntry {
            path: PathBuf::from("/ws/.winter/config.lock"),
            fault: LockEntryFault::MissingField { field: "commit" },
        };
        assert_eq!(
            error.to_string(),
            "/ws/.winter/config.lock: malformed [[standalone]] entry"
        );
    }
}
