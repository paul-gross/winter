//! Command discovery: which files under the commands directory are commands, and the registry generated from
//! them. Pure functions: the build script does the directory walk and the file reads and hands the results in.

use std::fmt;

/// The commands directory, as messages and the registry spell it: relative to the repository root.
pub const COMMANDS_DIR: &str = "crates/core/src/commands";

/// The directories a command file may sit in below the commands directory.
pub const GROUPS: [&str; 4] = ["ext", "repo", "service", "ws"];

/// One discovered command file.
pub struct CommandFile {
    /// The file's path relative to the commands directory, `/`-separated: `ws/index.rs`.
    pub relative: String,
    /// The command path the file registers: `["ws", "index"]`.
    pub path: Vec<String>,
}

/// A path rule that an entry of the commands directory breaks. Each variant carries the entry's path relative
/// to the commands directory.
#[derive(Debug, PartialEq, Eq)]
pub enum DiscoverError {
    /// The file stem is not `[a-z][a-z0-9_]*`.
    Stem { path: String },
    /// The entry sits deeper than one directory below the commands directory.
    Depth { path: String },
    /// The entry is, or sits in, a directory that is not one of the groups.
    Directory { path: String },
    /// The file is a `mod.rs`.
    ModFile { path: String },
    /// The file is not a `.rs` file.
    NotRust { path: String },
    /// The file's text does not contain `REGISTRATION`.
    NoRegistration { path: String },
}

impl fmt::Display for DiscoverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (path, rule) = match self {
            Self::Stem { path } => (
                path,
                "a command file is named [a-z][a-z0-9_]*.rs".to_owned(),
            ),
            Self::Depth { path } => (
                path,
                "a command file sits in the commands directory or in one group directory, nothing deeper"
                    .to_owned(),
            ),
            Self::Directory { path } => (
                path,
                format!("the only directories of commands are {}", GROUPS.join(", ")),
            ),
            Self::ModFile { path } => (
                path,
                "a group is declared by its directory alone, not by a mod.rs".to_owned(),
            ),
            Self::NotRust { path } => (
                path,
                "the commands directory holds .rs command files only".to_owned(),
            ),
            Self::NoRegistration { path } => {
                (path, "a command file defines REGISTRATION".to_owned())
            }
        };
        write!(f, "{COMMANDS_DIR}/{path}: {rule}")
    }
}

/// Applies the path rules to every entry found below the commands directory.
///
/// `relative_paths` are `/`-separated paths relative to the commands directory; a directory is spelled with a
/// trailing `/`, so that an empty directory is still seen. `contents` returns the text of the file at such a
/// path. The first entry that breaks a rule ends the classification.
pub fn classify(
    relative_paths: &[String],
    contents: &dyn Fn(&str) -> String,
) -> Result<Vec<CommandFile>, DiscoverError> {
    let mut files = Vec::new();
    for relative in relative_paths {
        let path = || relative.clone();
        if let Some(directory) = relative.strip_suffix('/') {
            if directory.contains('/') {
                return Err(DiscoverError::Depth { path: path() });
            }
            if !GROUPS.contains(&directory) {
                return Err(DiscoverError::Directory { path: path() });
            }
            continue;
        }

        let (group, file_name) = match relative.split('/').collect::<Vec<_>>().as_slice() {
            [file_name] => (None, *file_name),
            [group, file_name] => (Some(*group), *file_name),
            _ => return Err(DiscoverError::Depth { path: path() }),
        };
        if group.is_some_and(|group| !GROUPS.contains(&group)) {
            return Err(DiscoverError::Directory { path: path() });
        }
        let Some(stem) = file_name.strip_suffix(".rs") else {
            return Err(DiscoverError::NotRust { path: path() });
        };
        if stem == "mod" {
            return Err(DiscoverError::ModFile { path: path() });
        }
        if !is_command_stem(stem) {
            return Err(DiscoverError::Stem { path: path() });
        }
        if !contents(relative).contains("REGISTRATION") {
            return Err(DiscoverError::NoRegistration { path: path() });
        }

        let name = stem.replace('_', "-");
        files.push(CommandFile {
            relative: relative.clone(),
            path: group.map(str::to_owned).into_iter().chain([name]).collect(),
        });
    }
    Ok(files)
}

fn is_command_stem(stem: &str) -> bool {
    let mut chars = stem.chars();
    chars.next().is_some_and(|first| first.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// The source of the generated registry: one module declaration per command file, and `REGISTRY`, sorted by
/// command path so that neither help order nor dispatch depends on the order the directory was read in.
pub fn generate(files: &[CommandFile], commands_dir_absolute: &str) -> String {
    let mut sorted: Vec<&CommandFile> = files.iter().collect();
    sorted.sort_by(|a, b| a.path.cmp(&b.path));

    let mut source = String::new();
    for (index, file) in sorted.iter().enumerate() {
        let absolute = format!("{commands_dir_absolute}/{}", file.relative);
        source.push_str(&format!("#[path = {absolute:?}]\nmod command_{index};\n"));
    }
    source.push_str("pub(crate) static REGISTRY: &[Entry] = &[\n");
    for (index, file) in sorted.iter().enumerate() {
        let tracked = format!("{COMMANDS_DIR}/{}", file.relative);
        source.push_str(&format!(
            "    Entry {{ path: &{:?}, file: {tracked:?}, registration: &command_{index}::REGISTRATION }},\n",
            file.path
        ));
    }
    source.push_str("];\n");
    source
}

#[cfg(test)]
mod tests {
    use super::*;

    fn paths(relative: &[&str]) -> Vec<String> {
        relative.iter().map(|path| (*path).to_owned()).collect()
    }

    fn registering(_: &str) -> String {
        "pub(crate) const REGISTRATION: Registration = todo!();".to_owned()
    }

    fn refused(relative: &str, contents: &dyn Fn(&str) -> String) -> DiscoverError {
        match classify(&paths(&[relative]), contents) {
            Ok(_) => panic!("{relative} was accepted"),
            Err(error) => error,
        }
    }

    fn assert_starts_with_path(error: &DiscoverError, relative: &str) {
        let text = error.to_string();
        let tracked = format!("crates/core/src/commands/{relative}");
        assert!(text.starts_with(&tracked), "{text}");
        assert!(text.len() > tracked.len(), "{text}");
    }

    #[test]
    fn an_upper_case_stem_is_refused() {
        let error = refused("ws/Bad.rs", &registering);

        assert_eq!(
            error,
            DiscoverError::Stem {
                path: "ws/Bad.rs".to_owned()
            }
        );
        assert_starts_with_path(&error, "ws/Bad.rs");
    }

    #[test]
    fn a_stem_that_starts_with_a_digit_or_holds_a_dash_is_refused() {
        for relative in ["2fa.rs", "ws/dry-run.rs", "_x.rs", ".rs"] {
            assert_eq!(
                refused(relative, &registering),
                DiscoverError::Stem {
                    path: relative.to_owned()
                }
            );
        }
    }

    #[test]
    fn a_file_two_directories_deep_is_refused() {
        let error = refused("ws/deep/index.rs", &registering);

        assert_eq!(
            error,
            DiscoverError::Depth {
                path: "ws/deep/index.rs".to_owned()
            }
        );
        assert_starts_with_path(&error, "ws/deep/index.rs");
    }

    #[test]
    fn a_directory_inside_a_group_is_refused_even_when_empty() {
        assert_eq!(
            refused("ws/deep/", &registering),
            DiscoverError::Depth {
                path: "ws/deep/".to_owned()
            }
        );
    }

    #[test]
    fn a_directory_outside_the_groups_is_refused() {
        let error = refused("misc/index.rs", &registering);

        assert_eq!(
            error,
            DiscoverError::Directory {
                path: "misc/index.rs".to_owned()
            }
        );
        assert_starts_with_path(&error, "misc/index.rs");
    }

    #[test]
    fn an_empty_directory_outside_the_groups_is_refused() {
        let error = refused("misc/", &registering);

        assert_eq!(
            error,
            DiscoverError::Directory {
                path: "misc/".to_owned()
            }
        );
        assert_starts_with_path(&error, "misc/");
    }

    #[test]
    fn a_mod_file_is_refused() {
        let error = refused("ws/mod.rs", &registering);

        assert_eq!(
            error,
            DiscoverError::ModFile {
                path: "ws/mod.rs".to_owned()
            }
        );
        assert_starts_with_path(&error, "ws/mod.rs");
    }

    #[test]
    fn a_file_that_is_not_rust_is_refused() {
        let error = refused("ws/notes.md", &registering);

        assert_eq!(
            error,
            DiscoverError::NotRust {
                path: "ws/notes.md".to_owned()
            }
        );
        assert_starts_with_path(&error, "ws/notes.md");
    }

    #[test]
    fn a_file_without_registration_is_refused() {
        let error = refused("agents.rs", &|_| "fn run() {}".to_owned());

        assert_eq!(
            error,
            DiscoverError::NoRegistration {
                path: "agents.rs".to_owned()
            }
        );
        assert_starts_with_path(&error, "agents.rs");
    }

    #[test]
    fn contents_are_asked_for_by_relative_path() {
        let only_index = |relative: &str| {
            if relative == "ws/index.rs" {
                registering(relative)
            } else {
                String::new()
            }
        };

        assert!(classify(&paths(&["ws/index.rs"]), &only_index).is_ok());
        assert!(classify(&paths(&["ws/other.rs"]), &only_index).is_err());
    }

    #[test]
    fn group_directories_and_no_entry_at_all_yield_no_command() {
        let files = classify(&paths(&["ext/", "repo/", "service/", "ws/"]), &registering)
            .expect("the four groups are allowed");
        assert!(files.is_empty());

        let files = classify(&[], &registering).expect("no entry breaks no rule");
        assert!(files.is_empty());
    }

    #[test]
    fn command_paths_come_from_file_paths() {
        let files = classify(
            &paths(&["ws/", "ws/index.rs", "agents.rs", "service/ext_services.rs"]),
            &registering,
        )
        .expect("every path follows the rules");

        let found: Vec<(&str, Vec<&str>)> = files
            .iter()
            .map(|file| {
                (
                    file.relative.as_str(),
                    file.path.iter().map(String::as_str).collect(),
                )
            })
            .collect();
        assert_eq!(
            found,
            [
                ("ws/index.rs", vec!["ws", "index"]),
                ("agents.rs", vec!["agents"]),
                ("service/ext_services.rs", vec!["service", "ext-services"]),
            ]
        );
    }

    #[test]
    fn the_registry_is_sorted_by_command_path() {
        let files = classify(&paths(&["ws/index.rs", "agents.rs"]), &registering)
            .expect("both paths follow the rules");

        let source = generate(&files, "/checkout/crates/core/src/commands");

        assert_eq!(
            source,
            concat!(
                "#[path = \"/checkout/crates/core/src/commands/agents.rs\"]\n",
                "mod command_0;\n",
                "#[path = \"/checkout/crates/core/src/commands/ws/index.rs\"]\n",
                "mod command_1;\n",
                "pub(crate) static REGISTRY: &[Entry] = &[\n",
                "    Entry { path: &[\"agents\"], file: \"crates/core/src/commands/agents.rs\", ",
                "registration: &command_0::REGISTRATION },\n",
                "    Entry { path: &[\"ws\", \"index\"], file: \"crates/core/src/commands/ws/index.rs\", ",
                "registration: &command_1::REGISTRATION },\n",
                "];\n",
            )
        );
    }

    #[test]
    fn an_underscore_in_the_stem_is_a_dash_in_the_command_name() {
        let files = classify(&paths(&["service/ext_services.rs"]), &registering)
            .expect("the path follows the rules");

        let source = generate(&files, "/checkout/crates/core/src/commands");

        assert!(
            source.contains("path: &[\"service\", \"ext-services\"]"),
            "{source}"
        );
        assert!(
            source.contains("file: \"crates/core/src/commands/service/ext_services.rs\""),
            "{source}"
        );
    }

    #[test]
    fn no_command_file_yields_an_empty_registry() {
        assert_eq!(
            generate(&[], "/checkout/crates/core/src/commands"),
            "pub(crate) static REGISTRY: &[Entry] = &[\n];\n"
        );
    }
}
