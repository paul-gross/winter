//! The topic map compiled into the binary: the guidance the `context` command serves, and the eager safety text.
//! The build script generates the map from the sources under `topics/`; nothing here reads a file at run time.

/// A topic id: one or more segments joined by `/`, each `[a-z][a-z0-9-]*`. An id names a topic, not a file.
#[allow(
    dead_code,
    reason = "as Topic: the lookup of the context command reads it"
)]
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct TopicId(&'static str);

/// One topic of the compiled map.
#[allow(
    dead_code,
    reason = "the lookup of the context command reads the fields eager_text does not"
)]
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

/// The eager safety text: the body of the topic whose id is `safety`, byte for byte.
pub fn eager_text() -> &'static str {
    SAFETY.text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text_of(id: &str) -> &'static str {
        TOPICS
            .iter()
            .find(|topic| topic.id.0 == id)
            .unwrap_or_else(|| panic!("no topic {id}"))
            .text
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
}
