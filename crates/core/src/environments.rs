//! The environments present in a workspace: the directories under its root that hold a project's worktree.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::path::Path;

use crate::config_model::WorkspaceConfig;
use crate::env_index;
use crate::files::ReadFiles;
use crate::termination::InternalError;

/// The names of the environments present under `root`, by ascending index.
///
/// The candidates are the aliases of `config` in their order, then the names of `registry` (the env index
/// registry in the order of the state file); a name already a candidate is not repeated. A candidate is
/// present when `<root>/<name>` is a directory and one of its entries is a directory named as a loaded
/// project entry, symbolic links followed. The order is by the index [`env_index::lookup`] answers for the
/// name; names of equal index keep their candidate order.
///
/// A candidate directory that exists and cannot be listed is [`InternalError::Unexpected`].
pub(crate) fn present(
    files: &dyn ReadFiles,
    root: &Path,
    config: &WorkspaceConfig,
    registry: &[(String, i64)],
) -> Result<Vec<String>, InternalError> {
    let projects: BTreeSet<OsString> = config
        .project_repositories
        .iter()
        .map(|project| OsString::from(&project.common.name))
        .collect();
    let mut seen = BTreeSet::new();
    let candidates = config
        .ports
        .env_aliases
        .iter()
        .chain(registry.iter().map(|(name, _)| name))
        .filter(|name| seen.insert(name.as_str()));

    let recorded: BTreeMap<String, i64> = registry.iter().cloned().collect();
    let slots = env_index::slot_count(config.ports.envs_per_workspace);
    let mut found = Vec::new();
    for name in candidates {
        let directory = root.join(name);
        if !files.is_dir(&directory) {
            continue;
        }
        let entries = files
            .entries(&directory)
            .map_err(InternalError::unexpected)?;
        let holds_project = entries
            .iter()
            .any(|entry| projects.contains(entry) && files.is_dir(&directory.join(entry)));
        if holds_project {
            let index = env_index::lookup(name, &config.ports.env_aliases, slots, &recorded).index;
            found.push((index, name.clone()));
        }
    }
    found.sort_by_key(|(index, _)| *index);
    Ok(found.into_iter().map(|(_, name)| name).collect())
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::files::MemoryFiles;

    fn config() -> WorkspaceConfig {
        let files = MemoryFiles::default().with(
            "/ws/.winter/config.toml",
            b"env_aliases = [\"alpha\", \"beta\"]\n\n[[project_repository]]\nname = \"api\"\n",
        );
        crate::config_load::load(&files, Path::new("/ws"))
            .expect("a workspace")
            .config
    }

    fn registry(pairs: &[(&str, i64)]) -> Vec<(String, i64)> {
        pairs.iter().map(|(n, i)| ((*n).to_owned(), *i)).collect()
    }

    fn found(files: &MemoryFiles, pairs: &[(&str, i64)]) -> Result<Vec<String>, InternalError> {
        present(files, Path::new("/ws"), &config(), &registry(pairs))
    }

    #[test]
    fn an_alias_directory_holding_a_project_is_present_and_one_holding_another_is_not() {
        let files = MemoryFiles::default()
            .dir("/ws/alpha/api")
            .dir("/ws/beta/other");

        assert_eq!(found(&files, &[]).expect("names"), ["alpha"]);
    }

    #[test]
    fn a_directory_neither_aliased_nor_recorded_is_no_candidate() {
        let files = MemoryFiles::default().dir("/ws/lambda/api");

        assert!(found(&files, &[]).expect("names").is_empty());
    }

    #[test]
    fn a_recorded_name_orders_by_its_index_among_aliases() {
        let files = MemoryFiles::default()
            .dir("/ws/alpha/api")
            .dir("/ws/beta/api")
            .dir("/ws/mine/api");

        assert_eq!(
            found(&files, &[("mine", 0)]).expect("names"),
            ["mine", "alpha", "beta"].map(String::from)
        );
        assert_eq!(
            found(&files, &[("mine", 9)]).expect("names"),
            ["alpha", "beta", "mine"].map(String::from)
        );
    }

    #[test]
    fn equal_indexes_keep_alias_then_registry_order() {
        let files = MemoryFiles::default()
            .dir("/ws/alpha/api")
            .dir("/ws/one/api")
            .dir("/ws/two/api");

        assert_eq!(
            found(&files, &[("two", 1), ("one", 1)]).expect("names"),
            ["alpha", "two", "one"].map(String::from)
        );
    }

    #[test]
    fn a_file_named_as_an_alias_is_not_present() {
        let files = MemoryFiles::default().with("/ws/alpha", b"");

        assert!(found(&files, &[]).expect("names").is_empty());
    }

    #[test]
    fn a_candidate_that_cannot_be_listed_is_an_unexpected_failure() {
        let files = MemoryFiles::default().failing_dir("/ws/alpha", ErrorKind::PermissionDenied);

        let error = found(&files, &[]).expect_err("a failure");

        assert!(
            matches!(error, InternalError::Unexpected { .. }),
            "{error:?}"
        );
    }
}
