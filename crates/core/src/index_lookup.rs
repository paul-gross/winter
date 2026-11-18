//! The lookup of an env name in a workspace: its root found, its configuration and registry read, and the
//! index answered. Nothing is written.

use std::path::Path;

use crate::config;
use crate::env_index::{self, IndexAnswer};
use crate::files::ReadFiles;
use crate::state_registry;
use crate::termination::ConfigError;
use crate::workspace_root;

/// The index of `name` in the workspace of `start`. Reads only.
pub(crate) fn answer(
    files: &dyn ReadFiles,
    start: &Path,
    name: &str,
) -> Result<IndexAnswer, ConfigError> {
    let root = workspace_root::find(files, start)?;
    let config = config::load(files, &root)?;
    let registry = state_registry::read(files, &root);
    // A band wider than the 65536 values of the hash gives every name the slot a still wider band gives it,
    // so a count beyond `u32` is looked up as the largest `u32`.
    let envs_per_workspace = u32::try_from(config.envs_per_workspace).unwrap_or(u32::MAX);
    Ok(env_index::lookup(
        name,
        &config.env_aliases,
        envs_per_workspace,
        &registry,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::env_index::IndexSource;
    use crate::files::MemoryFiles;

    const CONFIG: &str = "/ws/.winter/config.toml";
    const STATE: &str = "/ws/.winter/state.toml";

    fn answer_in(files: &MemoryFiles, name: &str) -> IndexAnswer {
        answer(files, Path::new("/ws/alpha/project"), name).expect("an answer")
    }

    #[test]
    fn a_recorded_name_answers_from_the_registry() {
        let files = MemoryFiles::default()
            .with(CONFIG, b"")
            .with(STATE, b"[env_index]\nfeature-x = 15\n");

        assert_eq!(
            answer_in(&files, "feature-x"),
            IndexAnswer {
                index: 15,
                source: IndexSource::Registry,
                alias: false,
            }
        );
    }

    #[test]
    fn an_unrecorded_name_answers_its_hash_slot() {
        let files = MemoryFiles::default()
            .with(CONFIG, b"")
            .with(STATE, b"[env_index]\nother = 15\n");

        assert_eq!(
            answer_in(&files, "feature-x"),
            IndexAnswer {
                index: 40,
                source: IndexSource::Suggested,
                alias: false,
            }
        );
    }

    #[test]
    fn an_envs_per_workspace_beyond_32_bits_answers_as_the_largest_32_bit_count_does() {
        let beyond = MemoryFiles::default().with(CONFIG, b"envs_per_workspace = 5000000000\n");
        let largest = MemoryFiles::default().with(CONFIG, b"envs_per_workspace = 4294967295\n");

        let answer = answer_in(&beyond, "feature-x");

        assert_eq!(answer, answer_in(&largest, "feature-x"));
        // The first two bytes of the SHA-1 of `feature-x` are 0xcbc1, 52161; the band starts at 12.
        assert_eq!(answer.index, 12 + 52161);
    }

    #[test]
    fn a_count_beyond_32_bits_is_not_cut_to_its_low_32_bits() {
        let largest = MemoryFiles::default().with(CONFIG, b"envs_per_workspace = 4294967295\n");
        let expected = answer_in(&largest, "feature-x");

        // The low 32 bits of these are 0, 20, and 4294967295: no band, a narrow band, and the widest one.
        for count in [4_294_967_296_i64, 4_294_967_316, i64::MAX] {
            let config = format!("envs_per_workspace = {count}\n");
            let beyond = MemoryFiles::default().with(CONFIG, config.as_bytes());

            assert_eq!(answer_in(&beyond, "feature-x"), expected, "{count}");
        }
    }

    #[test]
    fn no_workspace_above_the_start_is_the_missing_root() {
        let files = MemoryFiles::default().with(CONFIG, b"");

        let error = answer(&files, Path::new("/elsewhere"), "feature-x").expect_err("no root");

        assert!(
            matches!(&error, ConfigError::RootNotFound { start } if start == Path::new("/elsewhere")),
            "{error:?}"
        );
    }
}
