//! `repo add`: appends one repository block to a configuration file of the workspace.

use clap::{Arg, ArgAction, ArgMatches};

use crate::RepositoryKind;
use crate::arguments;
use crate::context::Context;
use crate::output;
use crate::registry::Registration;
use crate::repository_catalog::{self, AddError, Added, NewBlock};
use crate::termination::{CommandError, Termination};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const URL: &str = "URL";

/// What the arguments ask for. Each text option is `Some` when it was given, the empty string included.
#[derive(Debug, PartialEq, Eq)]
struct Params<'m> {
    url: &'m str,
    standalone: bool,
    name: Option<&'m str>,
    main_branch: Option<&'m str>,
    reference: Option<&'m str>,
    git_excludes: Vec<String>,
    cmd: Vec<String>,
    pinned: bool,
    path: Option<&'m str>,
    prefix: Option<&'m str>,
    local: bool,
    json: bool,
}

fn text(id: &'static str, long: &'static str, help: &'static str) -> Arg {
    Arg::new(id)
        .long(long)
        .value_name("TEXT")
        .allow_hyphen_values(true)
        .help(help)
}

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Add a repository to the workspace configuration")
        .arg(
            Arg::new("url")
                .value_name(URL)
                .required(true)
                .help("The git URL of the repository"),
        )
        .arg(
            Arg::new("standalone")
                .long("standalone")
                .action(ArgAction::SetTrue)
                .help("Add a standalone repository instead of a project"),
        )
        .arg(text("name", "name", "The name of the repository"))
        .arg(text(
            "main_branch",
            "main-branch",
            "The main branch of the repository",
        ))
        .arg(text("ref", "ref", "The ref to check out (standalone only)"))
        .arg(
            text(
                "git_exclude",
                "git-exclude",
                "A git exclude pattern; may be repeated",
            )
            .action(ArgAction::Append),
        )
        .arg(text("cmd", "cmd", "A setup command; may be repeated").action(ArgAction::Append))
        .arg(
            Arg::new("pinned")
                .long("pinned")
                .action(ArgAction::SetTrue)
                .help("Pin the project repository (project only)"),
        )
        .arg(text(
            "path",
            "path",
            "The location under the workspace (standalone only)",
        ))
        .arg(text(
            "prefix",
            "prefix",
            "The prefix of the repository (standalone only)",
        ))
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

fn params(matches: &ArgMatches) -> Result<Params<'_>, Termination> {
    let optional = |id: &str| -> Result<Option<&str>, Termination> {
        Ok(arguments::optional::<String>(matches, id)?.map(String::as_str))
    };
    let repeated = |id: &str| -> Result<Vec<String>, Termination> {
        Ok(arguments::repeated::<String>(matches, id)?
            .into_iter()
            .cloned()
            .collect())
    };
    Ok(Params {
        url: arguments::required::<String>(matches, "url")?,
        standalone: matches.get_flag("standalone"),
        name: optional("name")?,
        main_branch: optional("main_branch")?,
        reference: optional("ref")?,
        git_excludes: repeated("git_exclude")?,
        cmd: repeated("cmd")?,
        pinned: matches.get_flag("pinned"),
        path: optional("path")?,
        prefix: optional("prefix")?,
        local: matches.get_flag("local"),
        json: matches.get_flag("json"),
    })
}

fn block<'p>(params: &'p Params<'_>) -> NewBlock<'p> {
    NewBlock {
        kind: if params.standalone {
            RepositoryKind::Standalone
        } else {
            RepositoryKind::Project
        },
        name: params.name,
        url: params.url,
        main_branch: params.main_branch,
        pinned: params.pinned,
        path: params.path,
        prefix: params.prefix,
        r#ref: params.reference,
        git_excludes: &params.git_excludes,
        cmd: &params.cmd,
    }
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let params = match params(matches) {
        Ok(params) => params,
        Err(ended) => return ended,
    };
    let added = match repository_catalog::add(
        cx.files,
        cx.file_writer,
        cx.cwd,
        &block(&params),
        params.local,
    ) {
        Ok(added) => added,
        Err(AddError::Catalog(error)) => {
            return Termination::Command(Box::new(CommandError::Catalog(error)));
        }
        Err(AddError::Config(error)) => return Termination::Config(Box::new(error)),
        Err(AddError::Internal(error)) => return Termination::Internal(Box::new(error)),
    };
    if params.json {
        output::document(cx.out, &document(&added))
    } else {
        output::line(cx.out, &line(&added))
    }
}

fn document(added: &Added) -> serde_json::Value {
    serde_json::json!({
        "added": true,
        "kind": added.kind.to_string(),
        "url": added.url,
    })
}

fn line(added: &Added) -> String {
    format!("Added {} repository {}", added.kind, added.url)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("add").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("add")).debug_assert();
    }

    #[test]
    fn a_url_alone_gives_a_project_with_nothing_else() {
        let matches = parsed(&["https://h.invalid/a.git"]).expect("accepted arguments");

        let params = params(&matches).expect("parameters");

        assert_eq!(
            params,
            Params {
                url: "https://h.invalid/a.git",
                standalone: false,
                name: None,
                main_branch: None,
                reference: None,
                git_excludes: vec![],
                cmd: vec![],
                pinned: false,
                path: None,
                prefix: None,
                local: false,
                json: false,
            }
        );
        assert_eq!(block(&params).kind, RepositoryKind::Project);
    }

    #[test]
    fn every_option_is_read_and_the_repeated_ones_keep_their_order() {
        let matches = parsed(&[
            "--standalone",
            "--name",
            "",
            "u",
            "--main-branch",
            "-dev",
            "--ref",
            "r",
            "--git-exclude",
            "b",
            "--git-exclude",
            "a",
            "--cmd",
            "x",
            "--cmd",
            "--y",
            "--path",
            "",
            "--prefix",
            "p",
            "--pinned",
            "--local",
            "--json",
        ])
        .expect("accepted arguments");

        let params = params(&matches).expect("parameters");

        assert_eq!(params.name, Some(""));
        assert_eq!(params.main_branch, Some("-dev"));
        assert_eq!(params.reference, Some("r"));
        assert_eq!(params.git_excludes, ["b", "a"]);
        assert_eq!(params.cmd, ["x", "--y"]);
        assert_eq!(params.path, Some(""));
        assert_eq!(params.prefix, Some("p"));
        assert!(params.standalone && params.pinned && params.local && params.json);
        assert_eq!(block(&params).kind, RepositoryKind::Standalone);
    }

    #[test]
    fn a_missing_url_a_second_url_and_a_repeated_single_option_are_refused() {
        assert!(parsed(&[]).is_err());
        assert!(parsed(&["a", "b"]).is_err());
        assert!(parsed(&["a", "--name", "x", "--name", "y"]).is_err());
        assert!(parsed(&["a", "--local", "--local"]).is_err());
    }

    #[test]
    fn the_document_holds_the_kind_and_the_url_as_given() {
        let added = Added {
            kind: RepositoryKind::Standalone,
            url: "git@h.invalid:a/b.git".to_owned(),
        };

        assert_eq!(
            document(&added),
            serde_json::json!({"added": true, "kind": "standalone", "url": "git@h.invalid:a/b.git"})
        );
        assert!(
            line(&added).contains("standalone") && line(&added).contains("git@h.invalid:a/b.git")
        );
    }
}
