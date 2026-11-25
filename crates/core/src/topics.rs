//! The topic map compiled into the binary: the guidance the `context` command serves, and the eager safety text.
//! The build script generates the map from the sources under `topics/`; nothing here reads a file at run time.

/// A topic id: one or more segments joined by `/`, each `[a-z][a-z0-9-]*`. An id names a topic, not a file.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct TopicId(pub(crate) &'static str);

/// One topic of the compiled map.
#[derive(Debug)]
pub(crate) struct Topic {
    pub id: TopicId,
    /// One line, from the source's front matter.
    pub title: &'static str,
    /// The served text: the source's body, unchanged; for `root`, the body of `safety`, a line break, and the body
    /// of `root`.
    pub text: &'static str,
    /// The ids the text references, in order of first reference, without repeats or the topic's own id.
    pub next: &'static [TopicId],
}

include!(concat!(env!("OUT_DIR"), "/topics.rs"));

/// A request for a topic that the map does not hold.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum ContextError {
    #[error("no topic {id:?}; `winter-workspace context topics` lists every topic")]
    UnknownTopic { id: String },
}

/// The topic served when no id is asked for.
pub(crate) fn root() -> &'static Topic {
    find("root").unwrap_or_else(|_| unreachable!("the build requires the topic `root`"))
}

/// The topic whose id is exactly `id`: the whole string, with no normalisation and no near match.
pub(crate) fn find(id: &str) -> Result<&'static Topic, ContextError> {
    TOPICS
        .iter()
        .find(|topic| topic.id.0 == id)
        .ok_or_else(|| ContextError::UnknownTopic { id: id.to_owned() })
}

/// The eager safety text: the body of the topic whose id is `safety`, byte for byte.
pub fn eager_text() -> &'static str {
    SAFETY.text
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::ffi::OsString;

    use clap::ArgMatches;
    use pulldown_cmark::{Event, Parser};

    use super::*;
    use crate::context::Context;
    use crate::parse::{self, Outcome};
    use crate::process::Identity;
    use crate::registry::{Entry, REGISTRY, Registration};
    use crate::termination::Termination;

    fn text_of(id: &str) -> &'static str {
        TOPICS
            .iter()
            .find(|topic| topic.id.0 == id)
            .unwrap_or_else(|| panic!("no topic {id}"))
            .text
    }

    #[test]
    fn every_reference_of_every_topic_resolves() {
        for topic in TOPICS {
            for next in topic.next {
                assert!(find(next.0).is_ok(), "{} -> {}", topic.id.0, next.0);
            }
        }
    }

    #[test]
    fn find_matches_the_whole_id_only() {
        assert_eq!(find("safety").unwrap().id.0, "safety");
        for id in [
            "Context",
            "ROOT",
            "context/",
            "../x",
            "",
            "workspace/context",
        ] {
            assert_eq!(
                find(id).unwrap_err(),
                ContextError::UnknownTopic { id: id.to_owned() }
            );
        }
        assert_eq!(root().id.0, "root");
    }

    #[test]
    fn the_served_root_starts_with_the_eager_text() {
        assert!(text_of("root").starts_with(eager_text()));
    }

    #[test]
    fn the_eager_text_is_the_body_of_the_safety_topic() {
        assert_eq!(eager_text(), text_of("safety"));
    }

    #[test]
    fn the_topics_are_sorted_by_id_and_hold_the_generated_listing() {
        let ids: Vec<&str> = TOPICS.iter().map(|topic| topic.id.0).collect();
        let mut sorted = ids.clone();
        sorted.sort_unstable();

        assert_eq!(ids, sorted);
        assert!(ids.contains(&"topics"));
    }

    #[test]
    fn the_safety_text_holds_the_provision_rule_the_destructive_rule_and_the_topic_listing() {
        let text = eager_text();

        assert!(
            text.contains("Never run or exercise an environment that has not been provisioned")
        );
        assert!(text.contains("Never run a destructive command against a live environment"));
        assert!(text.contains("`winter-workspace context topics`"));
    }

    /// The ids `entries` require that `ids` lacks: the path of each visible command joined by `/`, and the name of
    /// each group that holds one. An alias requires none.
    fn uncovered(entries: &[Entry], ids: &[&str]) -> Vec<String> {
        let mut required = BTreeSet::new();
        for entry in entries {
            if let Registration::Command { hidden: false, .. } = entry.registration {
                required.insert(entry.path.join("/"));
                if let [group, _] = entry.path {
                    required.insert((*group).to_owned());
                }
            }
        }
        required.retain(|id| !ids.contains(&id.as_str()));
        required.into_iter().collect()
    }

    /// Each inline code span of an authored body that starts `winter-workspace ` and is no command of `entries` to
    /// run, with the id of its topic. `topics` are ids with their served texts. The body of `safety` states the
    /// rules for commands a build may lack, so neither it nor its copy at the head of `root` is checked.
    fn unrunnable(entries: &[Entry], topics: &[(&str, &str)]) -> Vec<(String, String)> {
        let identity = Identity {
            program: "winter-workspace",
            version: "0.1.0",
        };
        let mut found = Vec::new();
        for &(id, text) in topics {
            let body = match id {
                "safety" | "topics" => continue,
                "root" => text.strip_prefix(eager_text()).unwrap_or(text),
                _ => text,
            };
            for event in Parser::new(body) {
                let Event::Code(span) = event else { continue };
                let Some(rest) = span.strip_prefix("winter-workspace ") else {
                    continue;
                };
                let args: Vec<OsString> = rest.split_whitespace().map(OsString::from).collect();
                let parsed = parse::parse(entries, &identity, &args);
                if !matches!(parsed.outcome, Outcome::Dispatch { .. }) {
                    found.push((id.to_owned(), span.to_string()));
                }
            }
        }
        found
    }

    fn ids() -> Vec<&'static str> {
        TOPICS.iter().map(|topic| topic.id.0).collect()
    }

    fn no_args(command: clap::Command) -> clap::Command {
        command
    }

    fn no_run(_: &ArgMatches, _: &mut Context<'_>) -> Termination {
        Termination::Success
    }

    static VISIBLE: Registration = Registration::Command {
        args: no_args,
        run: no_run,
        hidden: false,
    };
    static HIDDEN: Registration = Registration::Command {
        args: no_args,
        run: no_run,
        hidden: true,
    };
    static ALIAS: Registration = Registration::Alias { of: "restack" };

    fn entry(path: &'static [&'static str], registration: &'static Registration) -> Entry {
        Entry {
            path,
            file: "no file",
            registration,
        }
    }

    #[test]
    fn every_visible_command_and_its_group_have_a_topic() {
        let missing = uncovered(REGISTRY, &ids());

        assert!(
            missing.is_empty(),
            "add a topic under crates/core/topics/ for each of: {missing:?}"
        );
    }

    #[test]
    fn a_removed_command_topic_or_group_topic_is_named() {
        for removed in ["ws/index", "ws", "context"] {
            let mut kept = ids();
            kept.retain(|id| *id != removed);

            assert_eq!(uncovered(REGISTRY, &kept), [removed]);
        }
    }

    #[test]
    fn an_added_command_needs_its_topic_and_its_group_s_and_a_hidden_one_or_an_alias_none() {
        let added = [entry(&["service", "restack"], &VISIBLE)];
        let exempt = [
            entry(&["service", "secret"], &HIDDEN),
            entry(&["service", "again"], &ALIAS),
        ];

        assert_eq!(uncovered(&added, &[]), ["service", "service/restack"]);
        assert_eq!(uncovered(&exempt, &[]), [] as [&str; 0]);
    }

    #[test]
    fn every_command_span_outside_safety_is_a_command_to_run() {
        let topics: Vec<(&str, &str)> = TOPICS
            .iter()
            .map(|topic| (topic.id.0, topic.text))
            .collect();

        let found = unrunnable(REGISTRY, &topics);

        assert!(
            found.is_empty(),
            "(topic, span) the build cannot run: {found:?}"
        );
    }

    #[test]
    fn a_span_with_an_option_or_a_command_the_build_lacks_is_named_except_in_safety() {
        for span in [
            "winter-workspace ws index --cut",
            "winter-workspace ws restack",
        ] {
            let text = format!("Run `{span}`.\n");
            let root = format!("{}\n{text}", eager_text());

            for id in ["ws", "root"] {
                let served = if id == "root" { &root } else { &text };

                assert_eq!(
                    unrunnable(REGISTRY, &[(id, served)]),
                    [(id.to_owned(), span.to_owned())]
                );
            }
            assert_eq!(unrunnable(REGISTRY, &[("safety", &text)]), []);
        }
    }

    #[test]
    fn a_bare_group_and_a_help_request_are_no_command_to_run_and_a_code_block_is_no_span() {
        let text = "`winter-workspace ws` `winter-workspace ws index --help`\n\n```text\nwinter-workspace ws restack\n```\n";

        let found = unrunnable(REGISTRY, &[("x", text)]);

        let spans: Vec<&str> = found.iter().map(|(_, span)| span.as_str()).collect();
        assert_eq!(
            spans,
            ["winter-workspace ws", "winter-workspace ws index --help"]
        );
    }
}
