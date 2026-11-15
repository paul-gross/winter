//! `ws index`: the index of a feature env name, looked up and never recorded.

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::env_index::{IndexAnswer, IndexSource};
use crate::index_lookup;
use crate::output;
use crate::registry::Registration;
use crate::termination::{ParseFault, Termination, UsageError};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const NAME: &str = "NAME";

/// What the arguments ask for.
#[derive(Debug, PartialEq, Eq)]
struct Params<'m> {
    name: &'m str,
    json: bool,
}

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Print the port-offset index for a feature env name")
        .arg(
            Arg::new("name")
                .value_name(NAME)
                .required(true)
                .help("The feature env name; it need not name an existing environment"),
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
        name: matches.get_one::<String>("name")?,
        json: matches.get_flag("json"),
    })
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let Some(params) = params(matches) else {
        return Termination::Usage(UsageError::Parse {
            command_path: vec!["ws".to_owned(), "index".to_owned()],
            fault: ParseFault::MissingArgument {
                metavar: NAME.to_owned(),
            },
        });
    };
    let answer = match index_lookup::answer(cx.files, cx.cwd, params.name) {
        Ok(answer) => answer,
        Err(error) => return Termination::Config(error),
    };
    if params.json {
        output::document(cx.out, &document(params.name, &answer))
    } else {
        output::line(cx.out, &line(&answer))
    }
}

fn source_name(source: IndexSource) -> &'static str {
    match source {
        IndexSource::Registry => "registry",
        IndexSource::Suggested => "suggested",
    }
}

fn document(name: &str, answer: &IndexAnswer) -> serde_json::Value {
    serde_json::json!({
        "name": name,
        "index": answer.index,
        "source": source_name(answer.source),
    })
}

/// The integer first on the line. A mark follows it only for the hash slot of a name that nothing records
/// and that is no alias.
fn line(answer: &IndexAnswer) -> String {
    match (answer.source, answer.alias) {
        (IndexSource::Suggested, false) => format!("{} (suggested)", answer.index),
        _ => answer.index.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("index").no_binary_name(true)).try_get_matches_from(arguments)
    }

    fn answer(index: i64, source: IndexSource, alias: bool) -> IndexAnswer {
        IndexAnswer {
            index,
            source,
            alias,
        }
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("index")).debug_assert();
    }

    #[test]
    fn a_name_alone_asks_for_the_human_line() {
        let matches = parsed(&["feature-x"]).expect("accepted arguments");

        assert_eq!(
            params(&matches),
            Some(Params {
                name: "feature-x",
                json: false
            })
        );
    }

    #[test]
    fn the_json_flag_is_read_on_either_side_of_the_name() {
        for arguments in [["feature-x", "--json"], ["--json", "feature-x"]] {
            let matches = parsed(&arguments).expect("accepted arguments");

            assert_eq!(
                params(&matches),
                Some(Params {
                    name: "feature-x",
                    json: true
                }),
                "{arguments:?}"
            );
        }
    }

    #[test]
    fn a_name_is_taken_as_given() {
        for name in ["", "with space", "quo\"te\\slash", "UPPER/slash"] {
            let matches = parsed(&[name]).expect("accepted arguments");

            assert_eq!(params(&matches).map(|params| params.name), Some(name));
        }
    }

    #[test]
    fn a_missing_name_a_second_name_and_a_repeated_flag_are_refused() {
        assert!(parsed(&[]).is_err());
        assert!(parsed(&["--json"]).is_err());
        assert!(parsed(&["one", "two"]).is_err());
        assert!(parsed(&["one", "--json", "--json"]).is_err());
    }

    #[test]
    fn the_document_holds_the_name_the_index_and_the_source() {
        assert_eq!(
            document("feature-x", &answer(40, IndexSource::Suggested, false)),
            serde_json::json!({"name": "feature-x", "index": 40, "source": "suggested"})
        );
        assert_eq!(
            document("far", &answer(5_000_000_000, IndexSource::Registry, false)),
            serde_json::json!({"name": "far", "index": 5_000_000_000_i64, "source": "registry"})
        );
    }

    #[test]
    fn only_the_hash_slot_of_a_name_that_is_no_alias_is_marked() {
        assert_eq!(
            line(&answer(40, IndexSource::Suggested, false)),
            "40 (suggested)"
        );
        assert_eq!(line(&answer(3, IndexSource::Suggested, true)), "3");
        assert_eq!(line(&answer(15, IndexSource::Registry, false)), "15");
        assert_eq!(line(&answer(31, IndexSource::Registry, true)), "31");
    }
}
