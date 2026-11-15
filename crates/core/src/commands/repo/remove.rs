//! `repo remove`: removes one repository block from a configuration file of the workspace.

use std::path::Path;

use clap::{Arg, ArgAction, ArgMatches};

use crate::config_edit::{self, BlockRemoval, CatalogError, RepositoryKind};
use crate::context::Context;
use crate::files::WriteFiles;
use crate::output;
use crate::registry::Registration;
use crate::termination::{
    CommandError, ConfigError, FileCause, ParseFault, Termination, TomlParseError, UsageError,
};
use crate::workspace_root::{self, CONFIG_FILE};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const TARGET: &str = "TARGET";

/// The machine-local configuration file, relative to a workspace root.
const LOCAL_CONFIG_FILE: &str = ".winter/config.local.toml";

/// What the arguments ask for.
#[derive(Debug, PartialEq, Eq)]
struct Params<'m> {
    target: &'m str,
    local: bool,
    json: bool,
}

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Remove a repository from the workspace configuration")
        .arg(
            Arg::new("target")
                .value_name(TARGET)
                .required(true)
                .help("The repository, as <type>/<name>; the type is project or standalone"),
        )
        .arg(
            Arg::new("local")
                .long("local")
                .action(ArgAction::SetTrue)
                .help("Edit the machine-local configuration file instead of the shared one"),
        )
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print the result as one JSON document"),
        )
}

fn params(matches: &ArgMatches) -> Option<Params<'_>> {
    Some(Params {
        target: matches.get_one::<String>("target")?,
        local: matches.get_flag("local"),
        json: matches.get_flag("json"),
    })
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let Some(params) = params(matches) else {
        return Termination::Usage(UsageError::Parse {
            command_path: vec!["repo".to_owned(), "remove".to_owned()],
            fault: ParseFault::MissingArgument {
                metavar: TARGET.to_owned(),
            },
        });
    };
    let Some((kind, name)) = params.target.split_once('/') else {
        return catalog(CatalogError::TargetShape {
            target: params.target.to_owned(),
        });
    };
    let root = match workspace_root::find(cx.files, cx.cwd) {
        Ok(root) => root,
        Err(error) => return Termination::Config(error),
    };
    let Some(repository_kind) = repository_kind(kind) else {
        return catalog(CatalogError::UnknownKind {
            kind: kind.to_owned(),
        });
    };
    let file = root.join(if params.local {
        LOCAL_CONFIG_FILE
    } else {
        CONFIG_FILE
    });
    match remove(cx.file_writer, &file, repository_kind, name) {
        Ok(Removal::Done) => {}
        Ok(Removal::NotFound) => {
            return catalog(CatalogError::NotFound {
                kind: repository_kind,
                name: name.to_owned(),
            });
        }
        Ok(Removal::Entangled) => {
            return catalog(CatalogError::Entangled {
                kind: repository_kind,
                name: name.to_owned(),
            });
        }
        Err(cause) => return Termination::Config(ConfigError::FileRead { path: file, cause }),
    }
    if params.json {
        output::document(cx.out, &document(kind, name))
    } else {
        output::line(cx.out, &line(kind, name))
    }
}

fn catalog(error: CatalogError) -> Termination {
    Termination::Command(CommandError::Catalog(error))
}

fn repository_kind(kind: &str) -> Option<RepositoryKind> {
    match kind {
        "project" => Some(RepositoryKind::Project),
        "standalone" => Some(RepositoryKind::Standalone),
        _ => None,
    }
}

/// How an attempt to remove a block ended, when the file could be read.
#[derive(Debug, PartialEq, Eq)]
enum Removal {
    /// The file was replaced.
    Done,
    /// The file does not exist or holds no such block.
    NotFound,
    /// The text without the block is not the document less the block that was matched.
    Entangled,
}

/// What takes a block of a kind and a name out of a text: [`config_edit::remove_repository_block`].
type BlockEdit = fn(&str, RepositoryKind, &str) -> Option<BlockRemoval>;

/// Replaces `file` with its text less the first block of `kind` named `name`, and says how that ended.
///
/// A file that does not exist, and one that holds no such block, is left as it is. So is a file that is not
/// valid TOML, which is the error.
fn remove(
    writer: &dyn WriteFiles,
    file: &Path,
    kind: RepositoryKind,
    name: &str,
) -> Result<Removal, FileCause> {
    remove_with(
        writer,
        file,
        kind,
        name,
        config_edit::remove_repository_block,
    )
}

/// [`remove`] with the edit given. The edited text is handed to `writer` only when it parses to the document
/// less the block of `kind` at the position the edit names; otherwise the file is left as it is.
fn remove_with(
    writer: &dyn WriteFiles,
    file: &Path,
    kind: RepositoryKind,
    name: &str,
    edit: BlockEdit,
) -> Result<Removal, FileCause> {
    let mut invalid = None;
    let mut entangled = false;
    let replaced = writer.update(file, &mut |current| {
        let bytes = current?;
        if let Err(error) = toml::from_slice::<toml::Table>(bytes) {
            invalid = Some(TomlParseError::from(error));
            return None;
        }
        let text = std::str::from_utf8(bytes).ok()?;
        let edited = edit(text, kind, name)?;
        if !config_edit::lacks_one_block(text, &edited.text, kind, edited.index) {
            entangled = true;
            return None;
        }
        Some(edited.text.into_bytes())
    })?;
    match invalid {
        Some(error) => Err(FileCause::Toml(error)),
        None if entangled => Ok(Removal::Entangled),
        None if replaced => Ok(Removal::Done),
        None => Ok(Removal::NotFound),
    }
}

fn document(kind: &str, name: &str) -> serde_json::Value {
    serde_json::json!({
        "removed": true,
        "kind": kind,
        "name": name,
    })
}

fn line(kind: &str, name: &str) -> String {
    format!("Removed {kind} repository {name}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("remove").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("remove")).debug_assert();
    }

    #[test]
    fn a_target_alone_asks_for_the_shared_file_and_the_human_line() {
        let matches = parsed(&["project/docs"]).expect("accepted arguments");

        assert_eq!(
            params(&matches),
            Some(Params {
                target: "project/docs",
                local: false,
                json: false
            })
        );
    }

    #[test]
    fn the_flags_are_read_on_either_side_of_the_target() {
        for arguments in [
            ["project/docs", "--local", "--json"],
            ["--json", "--local", "project/docs"],
            ["--local", "project/docs", "--json"],
        ] {
            let matches = parsed(&arguments).expect("accepted arguments");

            assert_eq!(
                params(&matches),
                Some(Params {
                    target: "project/docs",
                    local: true,
                    json: true
                }),
                "{arguments:?}"
            );
        }
    }

    #[test]
    fn a_target_is_taken_as_given() {
        for target in ["", "nope", "project/a/b", "other/x", "project/"] {
            let matches = parsed(&[target]).expect("accepted arguments");

            assert_eq!(params(&matches).map(|params| params.target), Some(target));
        }
    }

    #[test]
    fn a_missing_target_a_second_target_and_a_repeated_flag_are_refused() {
        assert!(parsed(&[]).is_err());
        assert!(parsed(&["--local"]).is_err());
        assert!(parsed(&["project/a", "project/b"]).is_err());
        assert!(parsed(&["project/a", "--json", "--json"]).is_err());
        assert!(parsed(&["project/a", "--local", "--local"]).is_err());
    }

    #[test]
    fn only_the_two_types_are_repository_kinds() {
        assert_eq!(repository_kind("project"), Some(RepositoryKind::Project));
        assert_eq!(
            repository_kind("standalone"),
            Some(RepositoryKind::Standalone)
        );
        for other in ["", "Project", "projects", "other"] {
            assert_eq!(repository_kind(other), None, "{other:?}");
        }
    }

    #[test]
    fn the_document_holds_the_two_parts_of_the_target() {
        assert_eq!(
            document("project", "a/b"),
            serde_json::json!({"removed": true, "kind": "project", "name": "a/b"})
        );
    }

    #[test]
    fn the_human_line_names_the_type_and_the_name() {
        let line = line("project", "docs");

        assert!(line.contains("project") && line.contains("docs"), "{line}");
    }

    /// One file held in memory, and how often it was replaced.
    struct Held {
        content: std::cell::RefCell<Vec<u8>>,
        replacements: std::cell::Cell<u32>,
    }

    impl WriteFiles for Held {
        fn update(&self, _path: &Path, edit: &mut crate::files::Edit<'_>) -> std::io::Result<bool> {
            let replacement = edit(Some(&self.content.borrow()));
            Ok(replacement.is_some_and(|replacement| {
                self.content.replace(replacement);
                self.replacements.set(self.replacements.get() + 1);
                true
            }))
        }
    }

    const TWO_BLOCKS: &str = "\
[[project_repository]]
name = \"api\"

[[project_repository]]
name = \"web\"
";

    fn held() -> Held {
        held_with(TWO_BLOCKS)
    }

    fn held_with(content: &str) -> Held {
        Held {
            content: std::cell::RefCell::new(content.as_bytes().to_vec()),
            replacements: std::cell::Cell::new(0),
        }
    }

    /// Asserts that removing the project block `name` from `content` replaces it with `expected`.
    fn assert_removed(content: &str, name: &str, expected: &str) {
        let file = held_with(content);

        let removal = remove(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            name,
        );

        assert!(matches!(removal, Ok(Removal::Done)), "{removal:?}");
        assert_eq!(
            String::from_utf8_lossy(&file.content.into_inner()),
            expected
        );
        assert_eq!(file.replacements.get(), 1);
    }

    const API: &str = "[[project_repository]]\nname = \"api\"\n";
    const WEB: &str = "[[project_repository]]\nname = \"web\"\n";

    #[test]
    fn a_nan_in_another_table_does_not_keep_a_block_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let tool = format!("[tool]\nx = {nan}\ny = [1.5, {nan}]\n");

            assert_removed(&[&tool, API, WEB].concat(), "api", &[&tool, WEB].concat());
            assert_removed(&[API, &tool].concat(), "api", &tool);
        }
    }

    #[test]
    fn a_nan_in_a_kept_block_before_the_removed_one_does_not_keep_it_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let kept = format!("{WEB}weight = {nan}\n");

            assert_removed(&[&kept, API, WEB].concat(), "api", &[&kept, WEB].concat());
        }
    }

    #[test]
    fn a_nan_in_a_kept_block_after_the_removed_one_does_not_keep_it_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let kept = format!("{WEB}weight = {nan}\n");

            assert_removed(&[WEB, API, &kept].concat(), "api", &[WEB, &kept].concat());
        }
    }

    #[test]
    fn infinities_dates_and_nested_blocks_in_kept_blocks_do_not_keep_a_block_from_being_removed() {
        let before = [
            WEB,
            "high = inf\nlow = -inf\nzero = -0.0\nsince = 1979-05-27T07:32:00.5-08:00\n",
            "[[project_repository.mirror]]\nurl = \"h/one.git\"\nweight = nan\n",
            "[[project_repository.mirror]]\nurl = \"h/two.git\"\n",
        ]
        .concat();
        let after = [
            "[[project_repository]]\nname = \"docs\"\n",
            "day = 1979-05-27\nat = 07:32:00\nlocal = 1979-05-27T07:32:00\n",
            "[[project_repository.mirror]]\nurl = \"h/three.git\"\n",
            "[[project_repository.mirror.tag]]\nlow = -inf\n",
            "[[project_repository.mirror.tag]]\nhigh = +inf\n",
        ]
        .concat();

        assert_removed(
            &[&before, API, &after].concat(),
            "api",
            &[before.as_str(), after.as_str()].concat(),
        );
    }

    #[test]
    fn an_edit_that_removes_another_block_than_the_one_at_the_index_it_reports_is_refused() {
        let file = held();

        let removal = remove_with(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            "api",
            |text, kind, _| {
                let other = config_edit::remove_repository_block(text, kind, "web")?;
                Some(BlockRemoval {
                    text: other.text,
                    index: 0,
                })
            },
        );

        assert!(matches!(removal, Ok(Removal::Entangled)), "{removal:?}");
        assert_eq!(file.content.into_inner(), TWO_BLOCKS.as_bytes());
        assert_eq!(file.replacements.get(), 0);
    }

    #[test]
    fn an_edit_that_removes_the_one_block_replaces_the_file() {
        let file = held();

        let removal = remove(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            "api",
        );

        assert!(matches!(removal, Ok(Removal::Done)), "{removal:?}");
        assert_eq!(
            file.content.into_inner(),
            b"[[project_repository]]\nname = \"web\"\n"
        );
        assert_eq!(file.replacements.get(), 1);
    }

    #[test]
    fn an_edit_that_would_change_another_entry_is_refused_and_nothing_is_written() {
        let edits: [BlockEdit; 3] = [
            |_, _, _| {
                Some(BlockRemoval {
                    text: String::new(),
                    index: 0,
                })
            },
            |text, _, _| {
                Some(BlockRemoval {
                    text: text
                        .replace("web", "docs")
                        .replacen("name = \"api\"", "", 1),
                    index: 0,
                })
            },
            |_, _, _| {
                Some(BlockRemoval {
                    text: "[[project_repository\n".to_owned(),
                    index: 0,
                })
            },
        ];

        for edit in edits {
            let file = held();

            let removal = remove_with(
                &file,
                Path::new("config.toml"),
                RepositoryKind::Project,
                "api",
                edit,
            );

            assert!(matches!(removal, Ok(Removal::Entangled)), "{removal:?}");
            assert_eq!(file.content.into_inner(), TWO_BLOCKS.as_bytes());
            assert_eq!(file.replacements.get(), 0);
        }
    }
}
