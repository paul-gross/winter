//! The env index registry of a workspace, read from its machine-local state file.

use std::collections::BTreeMap;
use std::io::{Error, ErrorKind};
use std::path::Path;

use crate::files::ReadFiles;
use crate::termination::{ConfigError, FileCause, TomlParseError};
use crate::workspace_files::STATE_FILE;

/// The table of the state file that records one index per env name.
const TABLE: &str = "env_index";

/// The index recorded for each env name of the workspace at `root`.
///
/// Nothing at the path of the state file gives an empty registry. A path that exists and cannot be read is
/// [`ConfigError::FileRead`] with the `Io` cause, and so is a file that is not UTF-8, whose cause is of the
/// kind "invalid data" and carries the decoding error. A UTF-8 text that does not parse as TOML is
/// [`ConfigError::FileRead`] with the `Toml` cause, whatever the parser refuses it for: the parser's error is
/// carried and never read.
///
/// In a text that parses, an `env_index` that is not a table gives an empty registry. A recorded integer is
/// kept as it stands, whatever its sign or size; `true` is the index 1 and `false` the index 0; an entry with
/// any other value is left out.
pub(crate) fn read(
    files: &dyn ReadFiles,
    root: &Path,
) -> Result<BTreeMap<String, i64>, ConfigError> {
    let path = root.join(STATE_FILE);
    let file_read = |cause: FileCause| ConfigError::FileRead {
        path: path.clone(),
        cause,
    };
    let bytes = match files.read(&path) {
        Ok(Some(bytes)) => bytes,
        Ok(None) => return Ok(BTreeMap::new()),
        Err(error) => return Err(file_read(FileCause::Io(error))),
    };
    let text = String::from_utf8(bytes)
        .map_err(|error| file_read(FileCause::Io(Error::new(ErrorKind::InvalidData, error))))?;
    let document = toml::from_str::<toml::Table>(&text)
        .map_err(|error| file_read(FileCause::Toml(TomlParseError::from(error))))?;
    let Some(toml::Value::Table(recorded)) = document.get(TABLE) else {
        return Ok(BTreeMap::new());
    };
    Ok(recorded
        .iter()
        .filter_map(|(name, value)| Some((name.clone(), index_of(value)?)))
        .collect())
}

/// The index a value of the registry table records, if it records one.
fn index_of(value: &toml::Value) -> Option<i64> {
    match value {
        toml::Value::Integer(index) => Some(*index),
        toml::Value::Boolean(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use std::string::FromUtf8Error;

    use super::*;
    use crate::files::MemoryFiles;

    const STATE: &str = "/ws/.winter/state.toml";

    fn registry_of(state: &[u8]) -> Result<BTreeMap<String, i64>, ConfigError> {
        let files = MemoryFiles::default().with(STATE, state);
        read(&files, Path::new("/ws"))
    }

    fn entries(pairs: &[(&str, i64)]) -> BTreeMap<String, i64> {
        pairs
            .iter()
            .map(|(name, index)| ((*name).to_owned(), *index))
            .collect()
    }

    #[test]
    fn every_integer_entry_is_read_as_recorded() {
        let registry = registry_of(
            b"[env_index]\nalpha = 1\nfeature-x = 14\nbelow = -3\nzero = 0\nfar = 5000000000\n",
        )
        .expect("a registry");

        assert_eq!(
            registry,
            entries(&[
                ("alpha", 1),
                ("feature-x", 14),
                ("below", -3),
                ("zero", 0),
                ("far", 5_000_000_000),
            ])
        );
    }

    #[test]
    fn a_missing_file_is_an_empty_registry() {
        let registry = read(&MemoryFiles::default(), Path::new("/ws")).expect("a registry");

        assert!(registry.is_empty());
    }

    #[test]
    fn a_file_that_cannot_be_read_is_a_file_read_error_with_the_io_cause() {
        let files = MemoryFiles::default().unreadable(STATE, ErrorKind::PermissionDenied);

        let error = read(&files, Path::new("/ws")).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Io(cause) }
                    if path == Path::new(STATE) && cause.kind() == ErrorKind::PermissionDenied
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_file_that_is_not_utf_8_is_a_file_read_error_with_an_invalid_data_cause() {
        let error = registry_of(&[0xff, 0xfe]).expect_err("a refusal");

        let ConfigError::FileRead {
            path,
            cause: FileCause::Io(cause),
        } = &error
        else {
            panic!("expected a file read error with the Io cause, got {error:?}");
        };
        assert_eq!(path, Path::new(STATE));
        assert_eq!(cause.kind(), ErrorKind::InvalidData);
        let decoding = cause
            .get_ref()
            .and_then(|inner| inner.downcast_ref::<FromUtf8Error>())
            .expect("the decoding error");
        assert_eq!(decoding.utf8_error().valid_up_to(), 0);
    }

    #[test]
    fn a_text_that_does_not_parse_as_toml_is_a_file_read_error_with_the_toml_cause() {
        let texts: [(&str, &str); 9] = [
            ("a table header left open", "[env_index\nalpha = 1\n"),
            (
                "a syntax error before the entry asked for",
                "[env_index\nfeature-x = 15\n",
            ),
            (
                "a key given twice in the registry table",
                "[env_index]\nfeature-x = 14\nfeature-x = 15\n",
            ),
            (
                "a key given twice in an inline table",
                "env_index = { feature-x = 14, feature-x = 15 }\n",
            ),
            (
                "a key given twice above the first table header",
                "a = 1\na = 2\n\n[env_index]\nfeature-x = 15\n",
            ),
            (
                "a table header given twice",
                "[env_index]\nfeature-x = 15\n\n[env_index]\nfeature-x = 16\n",
            ),
            (
                "a child table header over a key that holds a value",
                "[env_index]\nfeature-x = 15\n\n[env_index.feature-x]\n",
            ),
            (
                "a dotted key over a key that holds a value",
                "[env_index]\na = 1\na.b = 2\n",
            ),
            (
                "a dotted key given twice",
                "[env_index]\na.b = 1\na.b = 2\n",
            ),
        ];

        for (case, text) in texts {
            let error = registry_of(text.as_bytes()).expect_err(case);

            assert!(
                matches!(
                    &error,
                    ConfigError::FileRead { path, cause: FileCause::Toml(_) }
                        if path == Path::new(STATE)
                ),
                "{case}: {error:?}"
            );
        }
    }

    #[test]
    fn a_boolean_entry_is_the_index_one_or_zero_and_any_other_non_integer_is_left_out() {
        let registry = registry_of(
            b"[env_index]\nkept = 7\ntext = \"8\"\nflag = true\nreal = 9.0\nlist = [1]\n",
        )
        .expect("a registry");

        assert_eq!(registry, entries(&[("kept", 7), ("flag", 1)]));
    }

    #[test]
    fn a_false_entry_is_the_index_zero() {
        let registry = registry_of(b"[env_index]\non = true\noff = false\n").expect("a registry");

        assert_eq!(registry, entries(&[("on", 1), ("off", 0)]));
    }

    #[test]
    fn an_env_index_that_is_not_a_table_is_an_empty_registry() {
        assert!(
            registry_of(b"env_index = 3\n")
                .expect("a registry")
                .is_empty()
        );
        assert!(registry_of(b"other = 1\n").expect("a registry").is_empty());
    }

    #[test]
    fn other_top_level_keys_do_not_reach_the_registry() {
        let registry = registry_of(b"note = 5\n\n[env_index]\nalpha = 1\n\n[more]\nbeta = 2\n")
            .expect("a registry");

        assert_eq!(registry, entries(&[("alpha", 1)]));
    }
}
