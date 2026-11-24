//! `context`: one topic of the guidance compiled into the binary. It reads no workspace, no file, and no
//! environment, so it runs the same in any directory.

use clap::{Arg, ArgAction, ArgMatches};

use crate::arguments;
use crate::context::Context;
use crate::output;
use crate::registry::Registration;
use crate::termination::{CommandError, Termination};
use crate::topics::{self, Topic};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const TOPIC: &str = "TOPIC";

/// What the arguments ask for.
#[derive(Debug, PartialEq, Eq)]
struct Params {
    topic: Option<String>,
    json: bool,
}

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Print one topic of this version's guidance; the root when no TOPIC is given")
        .arg(
            Arg::new(TOPIC)
                .value_name(TOPIC)
                .help("The id of the topic, as `context topics` lists it"),
        )
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print the topic as one JSON document"),
        )
}

fn params(matches: &ArgMatches) -> Result<Params, Termination> {
    Ok(Params {
        topic: arguments::optional::<String>(matches, TOPIC)?.cloned(),
        json: matches.get_flag("json"),
    })
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let params = match params(matches) {
        Ok(params) => params,
        Err(termination) => return termination,
    };
    let found = match &params.topic {
        None => Ok(topics::root()),
        Some(id) => topics::find(id),
    };
    let topic = match found {
        Ok(topic) => topic,
        Err(error) => return Termination::Command(Box::new(CommandError::Context(error))),
    };
    if params.json {
        output::document(cx.out, &document(topic))
    } else {
        output::text(cx.out, &text(topic))
    }
}

/// The title, a blank line, and the text as served, then a line break unless the text ends with one.
fn text(topic: &Topic) -> String {
    let mut printed = format!("{}\n\n{}", topic.title, topic.text);
    if !printed.ends_with('\n') {
        printed.push('\n');
    }
    printed
}

fn document(topic: &Topic) -> serde_json::Value {
    serde_json::json!({
        "id": topic.id.0,
        "title": topic.title,
        "text": topic.text,
        "next": topic.next.iter().map(|id| id.0).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("context").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("context")).debug_assert();
    }

    #[test]
    fn the_topic_and_the_flag_are_optional() {
        let bare = params(&parsed(&[]).unwrap()).unwrap();
        let both = params(&parsed(&["ws/init", "--json"]).unwrap()).unwrap();

        assert_eq!(
            bare,
            Params {
                topic: None,
                json: false
            }
        );
        assert_eq!(
            both,
            Params {
                topic: Some("ws/init".to_owned()),
                json: true
            }
        );
        assert!(parsed(&["a", "b"]).is_err());
    }

    fn topic_with(text: &'static str) -> Topic {
        Topic {
            id: topics::TopicId("x"),
            title: "Title",
            text,
            next: &[],
        }
    }

    #[test]
    fn a_text_without_a_final_line_break_gains_one_and_one_with_it_gains_none() {
        assert_eq!(text(&topic_with("body")), "Title\n\nbody\n");
        assert_eq!(text(&topic_with("body\n")), "Title\n\nbody\n");
    }
}
