//! The repository entries of a configuration, and the check of the workspace prefix against the prefixes of
//! the standalone repositories.

use toml::Table;

use super::{ProjectRepository, StandaloneRepository};
use crate::termination::ConfigError;

/// The project and the standalone repositories of `merged`. `main_branch` is the branch of the workspace,
/// which an entry without a branch of its own takes.
///
/// No entry is read yet: both lists are empty, whatever the table holds.
pub(super) fn repositories(
    _merged: &Table,
    _main_branch: &str,
) -> Result<(Vec<ProjectRepository>, Vec<StandaloneRepository>), ConfigError> {
    Ok((Vec::new(), Vec::new()))
}

/// Refuses a workspace `prefix` that is also the prefix of one of the `standalone` repositories, with
/// [`ConfigError::PrefixCollision`] naming the first such repository.
pub(super) fn prefix_collision(
    prefix: &str,
    standalone: &[StandaloneRepository],
) -> Result<(), ConfigError> {
    match standalone
        .iter()
        .find(|repository| repository.prefix.as_deref() == Some(prefix))
    {
        Some(repository) => Err(ConfigError::PrefixCollision {
            prefix: prefix.to_owned(),
            standalone: repository.common.name.clone(),
        }),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::super::RepositoryCommon;
    use super::*;

    fn standalone(name: &str, prefix: Option<&str>) -> StandaloneRepository {
        StandaloneRepository {
            common: RepositoryCommon {
                name: name.to_owned(),
                url: None,
                main_branch: "main".to_owned(),
                git_excludes: Vec::new(),
                cmd: Vec::new(),
                extension: true,
                load: None,
                entry: None,
            },
            reference: None,
            path: name.to_owned(),
            prefix: prefix.map(str::to_owned),
            config_dir: format!(".winter/config/{name}"),
        }
    }

    #[test]
    fn a_workspace_prefix_no_standalone_repository_has_is_accepted() {
        let repositories = [
            standalone("plain", None),
            standalone("tools", Some("tl")),
            standalone("wide", Some("wsx")),
        ];

        assert!(prefix_collision("ws", &repositories).is_ok());
        assert!(prefix_collision("ws", &[]).is_ok());
    }

    #[test]
    fn a_workspace_prefix_a_standalone_repository_has_is_refused_naming_the_first_such_repository()
    {
        let repositories = [
            standalone("plain", None),
            standalone("tools", Some("ws")),
            standalone("more", Some("ws")),
        ];

        let error = prefix_collision("ws", &repositories).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::PrefixCollision { prefix, standalone }
                    if prefix == "ws" && standalone == "tools"
            ),
            "{error:?}"
        );
    }
}
