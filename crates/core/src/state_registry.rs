//! The env index registry of a workspace, read from its machine-local state file.

use std::collections::BTreeMap;
use std::path::Path;

use crate::files::ReadFiles;
use crate::workspace_files::STATE_FILE;

/// The table of the state file that records one index per env name.
const TABLE: &str = "env_index";

/// The index recorded for each env name of the workspace at `root`.
///
/// The read never fails. A state file that is missing, cannot be read, or is not valid TOML gives an empty
/// registry, and so does an `env_index` that is not a table; an entry whose value is not an integer is left
/// out. A recorded integer is kept as it stands, whatever its sign or size.
pub(crate) fn read(files: &dyn ReadFiles, root: &Path) -> BTreeMap<String, i64> {
    let Ok(Some(bytes)) = files.read(&root.join(STATE_FILE)) else {
        return BTreeMap::new();
    };
    let Ok(document) = toml::from_slice::<toml::Table>(&bytes) else {
        return BTreeMap::new();
    };
    let Some(toml::Value::Table(recorded)) = document.get(TABLE) else {
        return BTreeMap::new();
    };
    recorded
        .iter()
        .filter_map(|(name, value)| Some((name.clone(), value.as_integer()?)))
        .collect()
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::files::MemoryFiles;

    fn registry_of(state: &[u8]) -> BTreeMap<String, i64> {
        let files = MemoryFiles::default().with("/ws/.winter/state.toml", state);
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
        );

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
        let registry = read(&MemoryFiles::default(), Path::new("/ws"));

        assert!(registry.is_empty());
    }

    #[test]
    fn a_file_that_cannot_be_read_is_an_empty_registry() {
        let files = MemoryFiles::default()
            .unreadable("/ws/.winter/state.toml", ErrorKind::PermissionDenied);

        assert!(read(&files, Path::new("/ws")).is_empty());
    }

    #[test]
    fn a_file_that_is_not_valid_toml_is_an_empty_registry() {
        assert!(registry_of(b"[env_index\nalpha = 1\n").is_empty());
        assert!(registry_of(&[0xff, 0xfe]).is_empty());
    }

    #[test]
    fn an_entry_that_is_not_an_integer_is_left_out() {
        let registry = registry_of(
            b"[env_index]\nkept = 7\ntext = \"8\"\nflag = true\nreal = 9.0\nlist = [1]\n",
        );

        assert_eq!(registry, entries(&[("kept", 7)]));
    }

    #[test]
    fn an_env_index_that_is_not_a_table_is_an_empty_registry() {
        assert!(registry_of(b"env_index = 3\n").is_empty());
        assert!(registry_of(b"other = 1\n").is_empty());
    }

    #[test]
    fn other_top_level_keys_do_not_reach_the_registry() {
        let registry = registry_of(b"note = 5\n\n[env_index]\nalpha = 1\n\n[more]\nbeta = 2\n");

        assert_eq!(registry, entries(&[("alpha", 1)]));
    }
}
