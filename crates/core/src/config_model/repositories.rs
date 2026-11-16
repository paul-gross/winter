//! The repository entries of a configuration, and the check of the workspace prefix against the prefixes of
//! the standalone repositories.

use toml::{Table, Value};

use super::{
    LoadMode, Nested, ProjectRepository, REPOSITORY_ARRAYS, RepositoryCommon, StandaloneRepository,
    config_value, is_empty_form, string, strings,
};
use crate::termination::{
    ConfigError, EntryIdent, NestedExcludedKey, NestedOnlyKey, PathField, RepoKind, RepoRef,
    RepositoryFault, TypedKey,
};

/// The project and the standalone repositories of `merged`. `main_branch` is the branch of the workspace,
/// which an entry without a branch of its own takes.
///
/// The project entries are read first, then the standalone entries, each array in the order of the document,
/// and the first refusal met ends the read. An element that is not a table is passed over, and so is an
/// entry whose `name` and `url` are each absent or an empty string, whatever else it holds. A key whose
/// value is not an array holds no entry here.
///
/// Of one entry the checks run in this order: the types of `name` and of `url`; for a standalone entry, its
/// name against the names of the standalone entries before it; `main_branch`, `git_excludes`, `cmd`,
/// `extension`, `load`, and `entry`; `nested` with `envs` and `inherit_local`; and for a standalone entry
/// `ref`, `path`, `prefix`, and `config_dir`.
pub(super) fn repositories(
    merged: &Table,
    main_branch: &str,
) -> Result<(Vec<ProjectRepository>, Vec<StandaloneRepository>), ConfigError> {
    let mut projects = Vec::new();
    let mut standalones = Vec::new();
    let mut seen: Vec<(String, EntryIdent)> = Vec::new();

    for (kind, key) in REPOSITORY_ARRAYS {
        let elements = merged
            .get(key)
            .and_then(Value::as_array)
            .map_or(&[][..], Vec::as_slice);
        for (index, element) in elements.iter().enumerate() {
            let Some(entry) = Entry::declared(kind, index + 1, element)? else {
                continue;
            };
            match kind {
                RepoKind::Project => projects.push(entry.project(main_branch)?),
                RepoKind::Standalone => {
                    entry.unique_among(&mut seen)?;
                    standalones.push(entry.standalone(main_branch)?);
                }
            }
        }
    }
    Ok((projects, standalones))
}

/// One element of a repository array that declares a repository: a table with a name or a URL.
struct Entry<'t> {
    table: &'t Table,
    owner: RepoRef,
    /// `name`, when it is not empty.
    name: Option<&'t str>,
    /// `url`, when it is not empty.
    url: Option<&'t str>,
}

impl<'t> Entry<'t> {
    /// The entry `element` declares at `position` of the array of `kind`, counted from one; none when the
    /// element is not a table, or when its `name` and its `url` are each absent or an empty string.
    ///
    /// A `name`, then a `url`, that is not a string is [`ConfigError::ValueType`]. The label of the entry is
    /// its name when that is a non-empty string, else its URL when that is one, else `#` and the position.
    fn declared(
        kind: RepoKind,
        position: usize,
        element: &'t Value,
    ) -> Result<Option<Self>, ConfigError> {
        let Value::Table(table) = element else {
            return Ok(None);
        };
        let text = |field: &str| {
            table
                .get(field)
                .and_then(Value::as_str)
                .filter(|text| !text.is_empty())
        };
        let owner = RepoRef {
            kind,
            label: text("name")
                .or_else(|| text("url"))
                .map_or_else(|| format!("#{position}"), str::to_owned),
        };
        for (field, key) in [("name", TypedKey::Name), ("url", TypedKey::Url)] {
            if let Some(value) = table.get(field) {
                string(value, key, Some(&owner))?;
            }
        }
        let (name, url) = (text("name"), text("url"));
        Ok((name.is_some() || url.is_some()).then_some(Self {
            table,
            owner,
            name,
            url,
        }))
    }

    /// The name of the repository: `name`, and without one the name its URL gives.
    fn resolved_name(&self) -> &'t str {
        self.name
            .unwrap_or_else(|| name_from_url(self.url.unwrap_or_default()))
    }

    /// Records the name of this standalone entry in `seen`, and refuses it with
    /// [`ConfigError::DuplicateStandaloneName`] when an entry before it has the same name. Each of the two
    /// entries is told by its `name`, or by its URL when it has none.
    fn unique_among(&self, seen: &mut Vec<(String, EntryIdent)>) -> Result<(), ConfigError> {
        let name = self.resolved_name();
        let ident = match (self.name, self.url) {
            (Some(name), _) => EntryIdent::Name(name.to_owned()),
            (None, url) => EntryIdent::Url(url.unwrap_or_default().to_owned()),
        };
        if let Some((_, first)) = seen.iter().find(|(taken, _)| taken == name) {
            return Err(ConfigError::DuplicateStandaloneName {
                name: name.to_owned(),
                first: first.clone(),
                second: ident,
            });
        }
        seen.push((name.to_owned(), ident));
        Ok(())
    }

    /// The entry as a project repository. `pinned` is true for every value that is not an empty form, and a
    /// nested entry is no extension.
    fn project(&self, main_branch: &str) -> Result<ProjectRepository, ConfigError> {
        let mut common = self.common(main_branch)?;
        let nested = self.nested()?;
        if nested.is_some() {
            common.extension = false;
        }
        Ok(ProjectRepository {
            common,
            pinned: self
                .table
                .get("pinned")
                .is_some_and(|value| !is_empty_form(value)),
            nested,
        })
    }

    /// The entry as a standalone repository. An empty `path` or `config_dir` is the default of the key; an
    /// empty `ref` or `prefix` is kept as written.
    fn standalone(&self, main_branch: &str) -> Result<StandaloneRepository, ConfigError> {
        let common = self.common(main_branch)?;
        self.nested()?;
        let reference = self.string("ref", TypedKey::Ref)?.map(str::to_owned);
        let path = self.path("path", TypedKey::Path, PathField::Path)?;
        let prefix = self.string("prefix", TypedKey::Prefix)?.map(str::to_owned);
        let config_dir = self.path("config_dir", TypedKey::ConfigDir, PathField::ConfigDir)?;
        Ok(StandaloneRepository {
            reference,
            path: path.unwrap_or(&common.name).to_owned(),
            prefix,
            config_dir: config_dir
                .map_or_else(|| format!(".winter/config/{}", common.name), str::to_owned),
            common,
        })
    }

    /// The fields both kinds of entry have. An absent or empty `main_branch` is the branch of the workspace.
    fn common(&self, main_branch: &str) -> Result<RepositoryCommon, ConfigError> {
        let main_branch = self
            .string("main_branch", TypedKey::MainBranch)?
            .filter(|branch| !branch.is_empty())
            .unwrap_or(main_branch);
        let git_excludes = self.strings("git_excludes", TypedKey::GitExcludes)?;
        let cmd = self.strings("cmd", TypedKey::Cmd)?;
        let extension = match self.table.get("extension") {
            None => true,
            Some(Value::Boolean(extension)) => *extension,
            Some(value) => {
                return Err(self.fault(RepositoryFault::Extension {
                    value: config_value(value),
                }));
            }
        };
        let load = match self.table.get("load") {
            None => None,
            Some(value) => Some(match value.as_str() {
                Some("eager") => LoadMode::Eager,
                Some("lazy") => LoadMode::Lazy,
                Some("none") => LoadMode::None,
                _ => {
                    return Err(self.fault(RepositoryFault::Load {
                        value: config_value(value),
                    }));
                }
            }),
        };
        Ok(RepositoryCommon {
            name: self.resolved_name().to_owned(),
            url: self.url.map(str::to_owned),
            main_branch: main_branch.to_owned(),
            git_excludes,
            cmd,
            extension,
            load,
            entry: self.entry_points()?,
        })
    }

    /// `entry`, when the entry holds the key: a non-empty array of non-empty strings, each a relative path
    /// with no `..` segment.
    fn entry_points(&self) -> Result<Option<Vec<String>>, ConfigError> {
        let Some(value) = self.table.get("entry") else {
            return Ok(None);
        };
        let candidates = match value {
            Value::Array(candidates) if !candidates.is_empty() => candidates,
            _ => {
                return Err(self.fault(RepositoryFault::EntryList {
                    value: config_value(value),
                }));
            }
        };
        candidates
            .iter()
            .map(|candidate| match candidate.as_str() {
                Some(path) if leaves_its_base(path) => {
                    Err(self.fault(RepositoryFault::EntryPathEscapes {
                        value: path.to_owned(),
                    }))
                }
                Some(path) if !path.is_empty() => Ok(path.to_owned()),
                _ => Err(self.fault(RepositoryFault::EntryPathEmpty {
                    value: config_value(candidate),
                })),
            })
            .collect::<Result<_, _>>()
            .map(Some)
    }

    /// What the entry declares as a nested workspace; none when `nested` is absent or false.
    ///
    /// A `nested` that is not a boolean is refused, and so are `envs` and `inherit_local` on an entry that is
    /// not nested. A nested entry is refused when it is a standalone one, when it holds `load` or `entry`,
    /// and when its `extension` is true. Its `envs` is an integer of at least one, and its `inherit_local` an
    /// array of strings, each non-empty and without a `.`.
    fn nested(&self) -> Result<Option<Nested>, ConfigError> {
        let nested = match self.table.get("nested") {
            None => false,
            Some(Value::Boolean(nested)) => *nested,
            Some(value) => {
                return Err(self.fault(RepositoryFault::Nested {
                    value: config_value(value),
                }));
            }
        };
        if !nested {
            for (field, key) in [
                ("envs", NestedOnlyKey::Envs),
                ("inherit_local", NestedOnlyKey::InheritLocal),
            ] {
                if self.table.contains_key(field) {
                    return Err(self.fault(RepositoryFault::NestedOnly { key }));
                }
            }
            return Ok(None);
        }
        if self.owner.kind == RepoKind::Standalone {
            return Err(self.fault(RepositoryFault::StandaloneNested));
        }
        for (field, key) in [
            ("load", NestedExcludedKey::Load),
            ("entry", NestedExcludedKey::Entry),
        ] {
            if self.table.contains_key(field) {
                return Err(self.fault(RepositoryFault::NestedWith { key }));
            }
        }
        if self.table.get("extension").and_then(Value::as_bool) == Some(true) {
            return Err(self.fault(RepositoryFault::NestedWithExtensionTrue));
        }
        let envs = match self.table.get("envs") {
            None => None,
            Some(Value::Integer(envs)) if *envs >= 1 => Some(*envs),
            Some(value) => {
                return Err(self.fault(RepositoryFault::Envs {
                    value: config_value(value),
                }));
            }
        };
        let inherit_local = match self.table.get("inherit_local") {
            None => None,
            Some(value) => Some(inherited_keys(value).ok_or_else(|| {
                self.fault(RepositoryFault::InheritLocal {
                    value: config_value(value),
                })
            })?),
        };
        Ok(Some(Nested {
            envs,
            inherit_local,
        }))
    }

    /// The path under `field`, when the entry holds a non-empty one. An absolute path and one with a `..`
    /// segment are refused, and a value that is not a string is [`ConfigError::ValueType`] for `key`.
    fn path(
        &self,
        field: &str,
        key: TypedKey,
        fault: PathField,
    ) -> Result<Option<&'t str>, ConfigError> {
        match self.string(field, key)? {
            Some(path) if leaves_its_base(path) => Err(self.fault(RepositoryFault::Path {
                field: fault,
                value: path.to_owned(),
            })),
            path => Ok(path.filter(|path| !path.is_empty())),
        }
    }

    /// The string under `field`, when the entry holds the key. A value of another type is
    /// [`ConfigError::ValueType`] for `key`, naming the entry.
    fn string(&self, field: &str, key: TypedKey) -> Result<Option<&'t str>, ConfigError> {
        self.table
            .get(field)
            .map(|value| string(value, key, Some(&self.owner)))
            .transpose()
    }

    /// The strings of the array under `field`, and no string when the entry does not hold the key. A value that is
    /// not an array of strings is [`ConfigError::ValueType`] for `key`, naming the entry.
    fn strings(&self, field: &str, key: TypedKey) -> Result<Vec<String>, ConfigError> {
        match self.table.get(field) {
            Some(value) => strings(value, key, Some(&self.owner)),
            None => Ok(Vec::new()),
        }
    }

    /// The refusal of this entry for `fault`.
    fn fault(&self, fault: RepositoryFault) -> ConfigError {
        ConfigError::Repository {
            kind: self.owner.kind,
            label: self.owner.label.clone(),
            fault,
        }
    }
}

/// The name a repository at `url` takes when its entry gives none: the trailing `/` characters and then one
/// trailing `.git` are removed, and the name is what follows the last `/` or `:` that is left.
fn name_from_url(url: &str) -> &str {
    let trimmed = url.trim_end_matches('/');
    let trimmed = trimmed.strip_suffix(".git").unwrap_or(trimmed);
    trimmed
        .rfind(['/', ':'])
        .map_or(trimmed, |cut| &trimmed[cut + 1..])
}

/// Whether `path` is absolute or has a `..` segment.
fn leaves_its_base(path: &str) -> bool {
    path.starts_with('/') || path.split('/').any(|segment| segment == "..")
}

/// The names `value` lists, when it is an array of strings that are each non-empty and without a `.`.
fn inherited_keys(value: &Value) -> Option<Vec<String>> {
    value
        .as_array()?
        .iter()
        .map(|name| {
            name.as_str()
                .filter(|name| !name.is_empty() && !name.contains('.'))
                .map(str::to_owned)
        })
        .collect()
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
    use super::super::table;
    use super::*;
    use crate::config_merge::merge;
    use crate::termination::{ConfigValue, ValueType};

    type Repositories = (Vec<ProjectRepository>, Vec<StandaloneRepository>);

    /// The repositories of `text`, in a workspace whose branch is `trunk`.
    fn read(text: &str) -> Repositories {
        repositories(&table(text), "trunk").expect("the repositories")
    }

    fn refused(text: &str) -> ConfigError {
        repositories(&table(text), "trunk").expect_err("a refusal")
    }

    fn owned(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    fn value(text: &str, found: ValueType) -> ConfigValue {
        ConfigValue {
            text: text.to_owned(),
            found,
        }
    }

    #[test]
    fn a_project_entry_that_declares_only_its_name_takes_the_defaults() {
        let (projects, standalones) = read("[[project_repository]]\nname = \"api\"\n");

        assert_eq!(
            projects,
            [ProjectRepository {
                common: RepositoryCommon {
                    name: "api".to_owned(),
                    url: None,
                    main_branch: "trunk".to_owned(),
                    git_excludes: Vec::new(),
                    cmd: Vec::new(),
                    extension: true,
                    load: None,
                    entry: None,
                },
                pinned: false,
                nested: None,
            }]
        );
        assert!(standalones.is_empty());
    }

    #[test]
    fn the_declared_fields_of_a_project_entry_are_read() {
        let (projects, _) = read(
            "[[project_repository]]\nname = \"api\"\nurl = \"https://fixture.invalid/api.git\"\n\
             main_branch = \"develop\"\ngit_excludes = [\"target/\"]\ncmd = [\"make\", \"\"]\n\
             extension = false\nload = \"lazy\"\nentry = [\"docs/index.md\", \"./a/b\"]\n\
             pinned = true\nnested = false\n",
        );

        assert_eq!(
            projects,
            [ProjectRepository {
                common: RepositoryCommon {
                    name: "api".to_owned(),
                    url: Some("https://fixture.invalid/api.git".to_owned()),
                    main_branch: "develop".to_owned(),
                    git_excludes: owned(&["target/"]),
                    cmd: owned(&["make", ""]),
                    extension: false,
                    load: Some(LoadMode::Lazy),
                    entry: Some(owned(&["docs/index.md", "./a/b"])),
                },
                pinned: true,
                nested: None,
            }]
        );
    }

    #[test]
    fn a_standalone_entry_that_declares_only_its_url_takes_the_defaults_under_the_name_of_the_url()
    {
        let (projects, standalones) =
            read("[[standalone_repository]]\nurl = \"https://fixture.invalid/tools.git\"\n");

        assert!(projects.is_empty());
        assert_eq!(
            standalones,
            [StandaloneRepository {
                common: RepositoryCommon {
                    name: "tools".to_owned(),
                    url: Some("https://fixture.invalid/tools.git".to_owned()),
                    main_branch: "trunk".to_owned(),
                    git_excludes: Vec::new(),
                    cmd: Vec::new(),
                    extension: true,
                    load: None,
                    entry: None,
                },
                reference: None,
                path: "tools".to_owned(),
                prefix: None,
                config_dir: ".winter/config/tools".to_owned(),
            }]
        );
    }

    #[test]
    fn the_declared_fields_of_a_standalone_entry_are_read() {
        let (_, standalones) = read(
            "[[standalone_repository]]\nname = \"kit\"\nurl = \"https://fixture.invalid/tools.git\"\n\
             main_branch = \"develop\"\ngit_excludes = [\"out/\"]\ncmd = [\"make\"]\n\
             extension = false\nload = \"none\"\nentry = [\"index.md\"]\nref = \"v1\"\n\
             path = \"extensions/kit\"\nprefix = \"kt\"\nconfig_dir = \"settings/kit\"\n\
             pinned = true\nnested = false\n",
        );

        assert_eq!(
            standalones,
            [StandaloneRepository {
                common: RepositoryCommon {
                    name: "kit".to_owned(),
                    url: Some("https://fixture.invalid/tools.git".to_owned()),
                    main_branch: "develop".to_owned(),
                    git_excludes: owned(&["out/"]),
                    cmd: owned(&["make"]),
                    extension: false,
                    load: Some(LoadMode::None),
                    entry: Some(owned(&["index.md"])),
                },
                reference: Some("v1".to_owned()),
                path: "extensions/kit".to_owned(),
                prefix: Some("kt".to_owned()),
                config_dir: "settings/kit".to_owned(),
            }]
        );
    }

    #[test]
    fn an_empty_string_is_the_default_of_a_branch_or_a_path_and_is_kept_for_a_ref_or_a_prefix() {
        let (_, standalones) = read(
            "[[standalone_repository]]\nname = \"kit\"\nurl = \"\"\nmain_branch = \"\"\n\
             ref = \"\"\npath = \"\"\nprefix = \"\"\nconfig_dir = \"\"\n",
        );

        let [kit] = standalones.as_slice() else {
            panic!("one entry: {standalones:?}");
        };
        assert_eq!(kit.common.url, None);
        assert_eq!(kit.common.main_branch, "trunk");
        assert_eq!(kit.reference.as_deref(), Some(""));
        assert_eq!(kit.path, "kit");
        assert_eq!(kit.prefix.as_deref(), Some(""));
        assert_eq!(kit.config_dir, ".winter/config/kit");
    }

    #[test]
    fn pinned_is_true_for_every_value_that_is_not_an_empty_form() {
        let pinned = |value: &str| {
            let (projects, _) = read(&format!(
                "[[project_repository]]\nname = \"api\"\npinned = {value}\n"
            ));
            projects[0].pinned
        };

        for value in ["true", "1", "0.5", "\"no\"", "[false]", "{ a = 0 }"] {
            assert!(pinned(value), "{value}");
        }
        for value in ["false", "0", "0.0", "\"\"", "[]", "{}"] {
            assert!(!pinned(value), "{value}");
        }
    }

    #[test]
    fn a_nested_project_entry_is_no_extension_and_carries_its_envs_and_inherited_keys() {
        let nested = |fields: &str| {
            let (projects, _) = read(&format!(
                "[[project_repository]]\nname = \"inner\"\nnested = true\n{fields}"
            ));
            let [inner] = projects.as_slice() else {
                panic!("one entry: {projects:?}");
            };
            assert!(!inner.common.extension, "{fields}");
            inner.nested.clone().expect("a nested entry")
        };

        assert_eq!(
            nested(""),
            Nested {
                envs: None,
                inherit_local: None
            }
        );
        assert_eq!(
            nested("extension = false\nenvs = 1\ninherit_local = []\n"),
            Nested {
                envs: Some(1),
                inherit_local: Some(Vec::new())
            }
        );
        assert_eq!(
            nested("envs = 3\ninherit_local = [\"git\", \"env\"]\n"),
            Nested {
                envs: Some(3),
                inherit_local: Some(owned(&["git", "env"]))
            }
        );
    }

    #[test]
    fn each_fault_of_an_entry_is_refused_with_its_kind_its_label_and_its_values() {
        let project = RepoKind::Project;
        let standalone = RepoKind::Standalone;
        let cases = [
            (
                standalone,
                "path = \"/abs\"",
                RepositoryFault::Path {
                    field: PathField::Path,
                    value: "/abs".to_owned(),
                },
            ),
            (
                standalone,
                "path = \"a/../b\"",
                RepositoryFault::Path {
                    field: PathField::Path,
                    value: "a/../b".to_owned(),
                },
            ),
            (
                standalone,
                "config_dir = \"../outside\"",
                RepositoryFault::Path {
                    field: PathField::ConfigDir,
                    value: "../outside".to_owned(),
                },
            ),
            (
                standalone,
                "nested = true",
                RepositoryFault::StandaloneNested,
            ),
            (
                project,
                "envs = 2",
                RepositoryFault::NestedOnly {
                    key: NestedOnlyKey::Envs,
                },
            ),
            (
                project,
                "nested = false\ninherit_local = [\"git\"]",
                RepositoryFault::NestedOnly {
                    key: NestedOnlyKey::InheritLocal,
                },
            ),
            (
                standalone,
                "envs = 2",
                RepositoryFault::NestedOnly {
                    key: NestedOnlyKey::Envs,
                },
            ),
            (
                standalone,
                "inherit_local = []",
                RepositoryFault::NestedOnly {
                    key: NestedOnlyKey::InheritLocal,
                },
            ),
            (
                project,
                "nested = true\nload = \"lazy\"",
                RepositoryFault::NestedWith {
                    key: NestedExcludedKey::Load,
                },
            ),
            (
                project,
                "nested = true\nentry = [\"index.md\"]",
                RepositoryFault::NestedWith {
                    key: NestedExcludedKey::Entry,
                },
            ),
            (
                project,
                "nested = true\nextension = true",
                RepositoryFault::NestedWithExtensionTrue,
            ),
            (
                project,
                "extension = \"yes\"",
                RepositoryFault::Extension {
                    value: value("\"yes\"", ValueType::String),
                },
            ),
            (
                standalone,
                "extension = 1",
                RepositoryFault::Extension {
                    value: value("1", ValueType::Integer),
                },
            ),
            (
                project,
                "nested = 1",
                RepositoryFault::Nested {
                    value: value("1", ValueType::Integer),
                },
            ),
            (
                standalone,
                "nested = \"true\"",
                RepositoryFault::Nested {
                    value: value("\"true\"", ValueType::String),
                },
            ),
            (
                project,
                "load = \"always\"",
                RepositoryFault::Load {
                    value: value("\"always\"", ValueType::String),
                },
            ),
            (
                standalone,
                "load = true",
                RepositoryFault::Load {
                    value: value("true", ValueType::Boolean),
                },
            ),
            (
                project,
                "entry = []",
                RepositoryFault::EntryList {
                    value: value("[]", ValueType::Array),
                },
            ),
            (
                standalone,
                "entry = \"index.md\"",
                RepositoryFault::EntryList {
                    value: value("\"index.md\"", ValueType::String),
                },
            ),
            (
                project,
                "entry = [\"index.md\", \"\"]",
                RepositoryFault::EntryPathEmpty {
                    value: value("\"\"", ValueType::String),
                },
            ),
            (
                project,
                "entry = [7]",
                RepositoryFault::EntryPathEmpty {
                    value: value("7", ValueType::Integer),
                },
            ),
            (
                project,
                "entry = [\"/etc/index.md\"]",
                RepositoryFault::EntryPathEscapes {
                    value: "/etc/index.md".to_owned(),
                },
            ),
            (
                standalone,
                "entry = [\"index.md\", \"docs/../../index.md\"]",
                RepositoryFault::EntryPathEscapes {
                    value: "docs/../../index.md".to_owned(),
                },
            ),
            (
                project,
                "nested = true\nenvs = 0",
                RepositoryFault::Envs {
                    value: value("0", ValueType::Integer),
                },
            ),
            (
                project,
                "nested = true\nenvs = true",
                RepositoryFault::Envs {
                    value: value("true", ValueType::Boolean),
                },
            ),
            (
                project,
                "nested = true\ninherit_local = \"git\"",
                RepositoryFault::InheritLocal {
                    value: value("\"git\"", ValueType::String),
                },
            ),
            (
                project,
                "nested = true\ninherit_local = [\"git\", \"env.feature\"]",
                RepositoryFault::InheritLocal {
                    value: value("[\"git\", \"env.feature\"]", ValueType::Array),
                },
            ),
            (
                project,
                "nested = true\ninherit_local = [\"\"]",
                RepositoryFault::InheritLocal {
                    value: value("[\"\"]", ValueType::Array),
                },
            ),
            (
                project,
                "nested = true\ninherit_local = [1]",
                RepositoryFault::InheritLocal {
                    value: value("[1]", ValueType::Array),
                },
            ),
        ];

        for (expected_kind, fields, expected) in cases {
            let error = refused(&format!(
                "[[{expected_kind}_repository]]\nname = \"subject\"\n{fields}\n"
            ));

            assert!(
                matches!(
                    &error,
                    ConfigError::Repository { kind, label, fault }
                        if *kind == expected_kind && label == "subject" && *fault == expected
                ),
                "{expected_kind} {fields}: {error:?}"
            );
        }
    }

    #[test]
    fn a_fault_of_an_entry_without_a_name_is_labelled_with_its_url() {
        let error = refused(
            "[[project_repository]]\nurl = \"https://fixture.invalid/api.git\"\nextension = 0\n",
        );

        assert!(
            matches!(
                &error,
                ConfigError::Repository { kind: RepoKind::Project, label, fault: RepositoryFault::Extension { .. } }
                    if label == "https://fixture.invalid/api.git"
            ),
            "{error:?}"
        );
    }

    #[test]
    fn two_standalone_entries_of_one_name_are_refused_naming_both() {
        let error = refused(
            "[[standalone_repository]]\nname = \"other\"\n\n\
             [[standalone_repository]]\nname = \"tools\"\nurl = \"https://fixture.invalid/a.git\"\n\n\
             [[standalone_repository]]\nname = \"tools\"\n",
        );

        assert!(
            matches!(
                &error,
                ConfigError::DuplicateStandaloneName { name, first, second }
                    if name == "tools"
                        && *first == EntryIdent::Name("tools".to_owned())
                        && *second == EntryIdent::Name("tools".to_owned())
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_standalone_entry_whose_url_gives_the_name_of_another_is_refused_naming_the_url() {
        let by_url_second = refused(
            "[[standalone_repository]]\nname = \"tools\"\n\n\
             [[standalone_repository]]\nurl = \"https://fixture.invalid/tools.git\"\n",
        );
        assert!(
            matches!(
                &by_url_second,
                ConfigError::DuplicateStandaloneName { name, first, second }
                    if name == "tools"
                        && *first == EntryIdent::Name("tools".to_owned())
                        && *second == EntryIdent::Url("https://fixture.invalid/tools.git".to_owned())
            ),
            "{by_url_second:?}"
        );

        let both_by_url = refused(
            "[[standalone_repository]]\nurl = \"git@fixture.invalid:team/tools.git\"\n\n\
             [[standalone_repository]]\nurl = \"https://fixture.invalid/tools/\"\n",
        );
        assert!(
            matches!(
                &both_by_url,
                ConfigError::DuplicateStandaloneName { name, first, second }
                    if name == "tools"
                        && *first == EntryIdent::Url("git@fixture.invalid:team/tools.git".to_owned())
                        && *second == EntryIdent::Url("https://fixture.invalid/tools/".to_owned())
            ),
            "{both_by_url:?}"
        );
    }

    #[test]
    fn two_project_entries_of_one_name_are_both_read_and_a_standalone_may_share_that_name() {
        let (projects, standalones) = read(
            "[[project_repository]]\nname = \"api\"\n\n\
             [[project_repository]]\nurl = \"https://fixture.invalid/api.git\"\n\n\
             [[standalone_repository]]\nname = \"api\"\n",
        );

        let names: Vec<&str> = projects
            .iter()
            .map(|repository| repository.common.name.as_str())
            .collect();
        assert_eq!(names, ["api", "api"]);
        assert_eq!(standalones.len(), 1);
    }

    #[test]
    fn a_value_of_the_wrong_type_is_refused_for_each_typed_field_naming_the_entry() {
        let project = RepoKind::Project;
        let standalone = RepoKind::Standalone;
        let cases = [
            (
                project,
                "name = 7\nurl = \"u\"",
                TypedKey::Name,
                "u",
                ValueType::Integer,
            ),
            (
                project,
                "name = \"api\"\nurl = true",
                TypedKey::Url,
                "api",
                ValueType::Boolean,
            ),
            (
                project,
                "name = \"api\"\nmain_branch = 1.5",
                TypedKey::MainBranch,
                "api",
                ValueType::Float,
            ),
            (
                project,
                "name = \"api\"\ngit_excludes = \"abc\"",
                TypedKey::GitExcludes,
                "api",
                ValueType::String,
            ),
            (
                project,
                "name = \"api\"\ngit_excludes = [\"a\", 1]",
                TypedKey::GitExcludes,
                "api",
                ValueType::Integer,
            ),
            (
                project,
                "name = \"api\"\ncmd = \"make\"",
                TypedKey::Cmd,
                "api",
                ValueType::String,
            ),
            (
                project,
                "name = \"api\"\ncmd = {}",
                TypedKey::Cmd,
                "api",
                ValueType::Table,
            ),
            (
                project,
                "name = \"api\"\ncmd = false",
                TypedKey::Cmd,
                "api",
                ValueType::Boolean,
            ),
            (
                standalone,
                "name = [\"kit\"]\nurl = \"u\"",
                TypedKey::Name,
                "u",
                ValueType::Array,
            ),
            (
                standalone,
                "name = \"kit\"\nurl = 1979-05-27",
                TypedKey::Url,
                "kit",
                ValueType::Datetime,
            ),
            (
                standalone,
                "name = \"kit\"\nmain_branch = []",
                TypedKey::MainBranch,
                "kit",
                ValueType::Array,
            ),
            (
                standalone,
                "name = \"kit\"\ngit_excludes = 0",
                TypedKey::GitExcludes,
                "kit",
                ValueType::Integer,
            ),
            (
                standalone,
                "name = \"kit\"\ncmd = [[\"make\"]]",
                TypedKey::Cmd,
                "kit",
                ValueType::Array,
            ),
            (
                standalone,
                "name = \"kit\"\nref = 1",
                TypedKey::Ref,
                "kit",
                ValueType::Integer,
            ),
            (
                standalone,
                "name = \"kit\"\npath = {}",
                TypedKey::Path,
                "kit",
                ValueType::Table,
            ),
            (
                standalone,
                "name = \"kit\"\nprefix = 0.0",
                TypedKey::Prefix,
                "kit",
                ValueType::Float,
            ),
            (
                standalone,
                "name = \"kit\"\nconfig_dir = false",
                TypedKey::ConfigDir,
                "kit",
                ValueType::Boolean,
            ),
        ];

        for (kind, fields, expected_key, label, expected_found) in cases {
            let error = refused(&format!("[[{kind}_repository]]\n{fields}\n"));

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key, owner: Some(owner), found }
                        if *key == expected_key
                            && *owner == RepoRef { kind, label: label.to_owned() }
                            && *found == expected_found
                ),
                "{kind} {fields}: {error:?}"
            );
        }
    }

    #[test]
    fn an_empty_form_that_is_no_string_is_refused_for_a_string_field() {
        let empty_forms = [
            ("false", ValueType::Boolean),
            ("0", ValueType::Integer),
            ("0.0", ValueType::Float),
            ("[]", ValueType::Array),
            ("{}", ValueType::Table),
        ];
        let mut cases = Vec::new();
        for (form, found) in empty_forms {
            cases.push((
                format!("name = {form}\nurl = \"u\""),
                TypedKey::Name,
                "u",
                found,
            ));
            cases.push((format!("name = {form}"), TypedKey::Name, "#1", found));
            cases.push((
                format!("name = {form}\nurl = \"\""),
                TypedKey::Name,
                "#1",
                found,
            ));
            cases.push((format!("url = {form}"), TypedKey::Url, "#1", found));
            cases.push((
                format!("name = \"api\"\nurl = {form}"),
                TypedKey::Url,
                "api",
                found,
            ));
        }
        cases.push((
            "name = 0\nurl = 0".to_owned(),
            TypedKey::Name,
            "#1",
            ValueType::Integer,
        ));
        cases.push((
            "name = \"api\"\nmain_branch = 0".to_owned(),
            TypedKey::MainBranch,
            "api",
            ValueType::Integer,
        ));

        for (fields, expected_key, label, expected_found) in cases {
            for kind in [RepoKind::Project, RepoKind::Standalone] {
                let error = refused(&format!("[[{kind}_repository]]\n{fields}\n"));

                assert!(
                    matches!(
                        &error,
                        ConfigError::ValueType { key, owner: Some(owner), found }
                            if *key == expected_key
                                && *owner == RepoRef { kind, label: label.to_owned() }
                                && *found == expected_found
                    ),
                    "{kind} {fields}: {error:?}"
                );
            }
        }

        let error = refused("[[standalone_repository]]\nname = \"kit\"\nprefix = false\n");
        assert!(
            matches!(
                &error,
                ConfigError::ValueType { key: TypedKey::Prefix, owner: Some(owner), found: ValueType::Boolean }
                    if *owner == RepoRef { kind: RepoKind::Standalone, label: "kit".to_owned() }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn an_entry_without_a_name_or_a_url_string_is_labelled_with_its_position_in_its_array() {
        let error = refused(
            "project_repository = [\"skipped\", { name = \"api\" }, { name = 0 }]\n\
             standalone_repository = [{ name = 0 }]\n",
        );

        assert!(
            matches!(
                &error,
                ConfigError::ValueType { key: TypedKey::Name, owner: Some(owner), found: ValueType::Integer }
                    if *owner == RepoRef { kind: RepoKind::Project, label: "#3".to_owned() }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn what_declares_no_repository_is_passed_over_without_a_refusal() {
        let (projects, standalones) = read(
            "project_repository = [\n  \"a string\",\n  7,\n  [\"an\", \"array\"],\n\
             \x20 { main_branch = \"develop\", cmd = [\"make\"] },\n\
             \x20 { name = \"\", url = \"\", main_branch = 0 },\n\
             \x20 { name = \"\", nested = 1, cmd = \"make\" },\n\
             \x20 { name = \"kept\" },\n]\n\
             standalone_repository = [\n  \"a string\",\n  { path = \"/abs\", nested = true },\n\
             \x20 { name = \"\", url = \"\", main_branch = 0, ref = 0 },\n]\n",
        );

        let names: Vec<&str> = projects
            .iter()
            .map(|repository| repository.common.name.as_str())
            .collect();
        assert_eq!(names, ["kept"]);
        assert!(standalones.is_empty());
    }

    #[test]
    fn a_key_that_is_absent_or_holds_no_array_declares_no_repository() {
        for text in [
            "",
            "project_repository = []\nstandalone_repository = []\n",
            "project_repository = \"api\"\nstandalone_repository = 7\n",
        ] {
            assert_eq!(read(text), (Vec::new(), Vec::new()), "{text}");
        }
    }

    #[test]
    fn the_name_of_an_entry_without_one_is_the_last_segment_of_its_url() {
        for (url, name) in [
            ("https://fixture.invalid/team/api.git", "api"),
            ("https://fixture.invalid/team/api", "api"),
            ("https://fixture.invalid/team/api/", "api"),
            ("https://fixture.invalid/team/api///", "api"),
            ("https://fixture.invalid/team/api.git/", "api"),
            ("git@fixture.invalid:api.git", "api"),
            ("git@fixture.invalid:team/api.git", "api"),
            ("fixture.invalid:api", "api"),
            ("api.git", "api"),
            ("api", "api"),
            ("https://fixture.invalid/api.git.git", "api.git"),
            ("https://fixture.invalid/api.github", "api.github"),
        ] {
            assert_eq!(name_from_url(url), name, "{url}");

            let (projects, standalones) = read(&format!(
                "[[project_repository]]\nurl = \"{url}\"\n\n[[standalone_repository]]\nurl = \"{url}\"\n"
            ));
            assert_eq!(projects[0].common.name, name, "{url}");
            assert_eq!(standalones[0].common.name, name, "{url}");
            assert_eq!(standalones[0].path, name, "{url}");
        }
    }

    #[test]
    fn a_name_is_taken_before_the_name_of_the_url() {
        let (projects, _) = read(
            "[[project_repository]]\nname = \"alias\"\nurl = \"https://fixture.invalid/api.git\"\n",
        );

        assert_eq!(projects[0].common.name, "alias");
    }

    #[test]
    fn the_first_refusal_in_document_order_is_reported_and_projects_come_before_standalones() {
        let error = refused(
            "[[standalone_repository]]\nname = \"kit\"\nnested = true\n\n\
             [[project_repository]]\nname = \"api\"\n\n\
             [[project_repository]]\nname = \"web\"\nload = \"always\"\n\n\
             [[project_repository]]\nname = \"docs\"\nextension = 1\n",
        );

        assert!(
            matches!(
                &error,
                ConfigError::Repository { kind: RepoKind::Project, label, fault: RepositoryFault::Load { .. } }
                    if label == "web"
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_entries_of_the_local_file_follow_those_of_the_shared_file() {
        let merged = merge(
            table(
                "[[project_repository]]\nname = \"api\"\n\n[[standalone_repository]]\nname = \"tools\"\n",
            ),
            table(
                "[[project_repository]]\nname = \"web\"\n\n[[standalone_repository]]\nname = \"kit\"\n",
            ),
        )
        .expect("a merged table");

        let (projects, standalones) = repositories(&merged, "trunk").expect("the repositories");

        let projects: Vec<&str> = projects
            .iter()
            .map(|repository| repository.common.name.as_str())
            .collect();
        let standalones: Vec<&str> = standalones
            .iter()
            .map(|repository| repository.common.name.as_str())
            .collect();
        assert_eq!(projects, ["api", "web"]);
        assert_eq!(standalones, ["tools", "kit"]);
    }

    #[test]
    fn a_local_standalone_entry_with_the_name_of_a_shared_one_is_refused() {
        let merged = merge(
            table("[[standalone_repository]]\nname = \"tools\"\n"),
            table("[[standalone_repository]]\nurl = \"https://fixture.invalid/tools.git\"\n"),
        )
        .expect("a merged table");

        let error = repositories(&merged, "trunk").expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::DuplicateStandaloneName { name, first, second }
                    if name == "tools"
                        && *first == EntryIdent::Name("tools".to_owned())
                        && *second == EntryIdent::Url("https://fixture.invalid/tools.git".to_owned())
            ),
            "{error:?}"
        );
    }

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
