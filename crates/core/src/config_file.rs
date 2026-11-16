//! One configuration file of a workspace, read into a TOML table.

use std::io::ErrorKind;
use std::path::Path;

use toml::Table;

use crate::files::ReadFiles;
use crate::termination::{ConfigError, FileCause, TomlParseError};

/// The table of the configuration file at `path`, with the keys of every table in the order of the file.
///
/// A path that is not a regular file holds no key: nothing there, a directory, a link that leads nowhere. A
/// regular file that cannot be read is [`ConfigError::FileRead`] with the `Io` cause, and so is one that is
/// gone when it is read. One that is not UTF-8 or not valid TOML is [`ConfigError::FileRead`] with the `Toml`
/// cause.
pub(crate) fn read(files: &dyn ReadFiles, path: &Path) -> Result<Table, ConfigError> {
    let file_read = |cause: FileCause| ConfigError::FileRead {
        path: path.to_path_buf(),
        cause,
    };
    if !files.is_file(path) {
        return Ok(Table::new());
    }
    match files.read(path) {
        Ok(Some(bytes)) => toml::from_slice(&bytes)
            .map_err(|error| file_read(FileCause::Toml(TomlParseError::from(error)))),
        // The file went away between the two calls of the port.
        Ok(None) => Err(file_read(FileCause::Io(ErrorKind::NotFound.into()))),
        Err(error) => Err(file_read(FileCause::Io(error))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::MemoryFiles;

    const PATH: &str = "/ws/.winter/config.local.toml";

    fn read_from(files: &MemoryFiles) -> Result<Table, ConfigError> {
        read(files, Path::new(PATH))
    }

    /// Files in which every path is a regular file that is gone by the time it is read.
    struct Vanished;

    impl ReadFiles for Vanished {
        fn read(&self, _path: &Path) -> std::io::Result<Option<Vec<u8>>> {
            Ok(None)
        }

        fn is_file(&self, _path: &Path) -> bool {
            true
        }
    }

    #[test]
    fn a_regular_file_that_is_gone_when_it_is_read_is_a_file_read_error_with_the_not_found_cause() {
        let error = read(&Vanished, Path::new(PATH)).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Io(cause) }
                    if path == Path::new(PATH) && cause.kind() == ErrorKind::NotFound
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_keys_of_a_table_keep_the_order_of_the_file() {
        let files = MemoryFiles::default().with(
            PATH,
            b"zeta = 1\nalpha = 2\n\n[middle]\nlast = 1\nfirst = { y = 1, x = 2 }\n\n[beta]\n",
        );

        let table = read_from(&files).expect("a table");

        let keys = |table: &Table| table.keys().cloned().collect::<Vec<_>>();
        assert_eq!(keys(&table), ["zeta", "alpha", "middle", "beta"]);
        let middle = table["middle"].as_table().expect("a table");
        assert_eq!(keys(middle), ["last", "first"]);
        assert_eq!(
            keys(middle["first"].as_table().expect("a table")),
            ["y", "x"]
        );
    }

    #[test]
    fn a_file_is_read_into_its_table() {
        let files = MemoryFiles::default().with(PATH, b"base_port = 5000\n\n[git]\nuser = 1\n");

        let table = read_from(&files).expect("a table");

        assert_eq!(table.get("base_port"), Some(&toml::Value::Integer(5000)));
        assert!(table.get("git").is_some_and(toml::Value::is_table));
        assert_eq!(table.len(), 2);
    }

    #[test]
    fn a_missing_file_holds_no_key() {
        let table = read_from(&MemoryFiles::default()).expect("a table");

        assert!(table.is_empty());
    }

    #[test]
    fn a_directory_at_the_path_loads_as_if_absent() {
        let files = MemoryFiles::default().unreadable(PATH, ErrorKind::IsADirectory);

        let table = read_from(&files).expect("a table");

        assert!(table.is_empty());
    }

    #[test]
    fn a_file_that_is_not_valid_toml_is_a_file_read_error_with_the_toml_cause_and_the_path() {
        let files = MemoryFiles::default().with(PATH, b"env_aliases = [\n");

        let error = read_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Toml(_) } if path == Path::new(PATH)
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_file_that_is_not_utf_8_is_a_file_read_error_with_the_toml_cause_and_the_path() {
        let files = MemoryFiles::default().with(PATH, &[0xff, 0xfe, b'\n']);

        let error = read_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Toml(_) } if path == Path::new(PATH)
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_regular_file_whose_read_fails_is_a_file_read_error_with_the_io_cause() {
        let files = MemoryFiles::default().failing(PATH, ErrorKind::PermissionDenied);

        let error = read_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Io(cause) }
                    if path == Path::new(PATH) && cause.kind() == ErrorKind::PermissionDenied
            ),
            "{error:?}"
        );
    }

    /// Texts on which TOML parsers differ, each with whether this parser accepts it.
    #[test]
    fn the_parser_reads_the_texts_on_which_toml_parsers_differ_as_recorded_here() {
        let cases = [
            (
                "an integer above the signed 64-bit range",
                "big = 123456789012345678901234567890\n",
                false,
            ),
            (
                "an integer below the signed 64-bit range",
                "big = -123456789012345678901234567890\n",
                false,
            ),
            (
                "a newline inside an inline table",
                "point = { x = 1,\n  y = 2 }\n",
                true,
            ),
            (
                "a trailing comma inside an inline table",
                "point = { x = 1, y = 2, }\n",
                true,
            ),
            ("the \\e string escape", "escape = \"\\e[0m\"\n", true),
            ("the \\xHH string escape", "escape = \"\\x41\"\n", true),
            ("a time without seconds", "at = 07:32\n", true),
            (
                "a date and time without seconds",
                "at = 1979-05-27T07:32\n",
                true,
            ),
        ];

        for (subject, text, accepted) in cases {
            let files = MemoryFiles::default().with(PATH, text.as_bytes());

            let read = read_from(&files);

            assert_eq!(read.is_ok(), accepted, "{subject}: {read:?}");
        }
    }
}
