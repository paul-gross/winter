//! `repo remove`: removes one repository block from a configuration file of the workspace.

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::output;
use crate::registry::Registration;
use crate::repository_catalog::{self, RemoveError, Removed};
use crate::termination::{CommandError, ParseFault, Termination, UsageError};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const TARGET: &str = "TARGET";

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
    let removed = match repository_catalog::remove(
        cx.files,
        cx.file_writer,
        cx.cwd,
        params.target,
        params.local,
    ) {
        Ok(removed) => removed,
        Err(RemoveError::Catalog(error)) => {
            return Termination::Command(CommandError::Catalog(error));
        }
        Err(RemoveError::Config(error)) => return Termination::Config(error),
    };
    if params.json {
        output::document(cx.out, &document(&removed))
    } else {
        output::line(cx.out, &line(&removed))
    }
}

fn document(removed: &Removed) -> serde_json::Value {
    serde_json::json!({
        "removed": true,
        "kind": removed.kind.to_string(),
        "name": removed.name,
    })
}

fn line(removed: &Removed) -> String {
    format!("Removed {} repository {}", removed.kind, removed.name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::RepositoryKind;

    fn removed(kind: RepositoryKind, name: &str) -> Removed {
        Removed {
            kind,
            name: name.to_owned(),
        }
    }

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
    fn the_document_holds_the_two_parts_of_the_target() {
        assert_eq!(
            document(&removed(RepositoryKind::Project, "a/b")),
            serde_json::json!({"removed": true, "kind": "project", "name": "a/b"})
        );
        assert_eq!(
            document(&removed(RepositoryKind::Standalone, "tools")),
            serde_json::json!({"removed": true, "kind": "standalone", "name": "tools"})
        );
    }

    #[test]
    fn the_human_line_names_the_type_and_the_name() {
        let line = line(&removed(RepositoryKind::Project, "docs"));

        assert!(line.contains("project") && line.contains("docs"), "{line}");
    }
}
