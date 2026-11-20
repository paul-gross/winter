//! Reading a parsed argument that the parser guarantees.
//!
//! The parser has already refused a command line that lacks a required argument, so a command file reads such
//! a value through [`required`] and never builds a usage error for its absence.

use clap::ArgMatches;

use crate::termination::{InternalError, Termination};

/// The parsed arguments hold no value of the asked type under `id`.
#[derive(Debug, thiserror::Error)]
#[error("the parsed arguments hold no value for '{id}'")]
struct MissingArgument {
    id: String,
}

/// The value of the argument `id`, which the node's parser requires or defaults.
///
/// Fails as an internal failure when the id is absent, unknown to the node, or of another type: the fault is
/// then the command's own parser, not the command line.
pub(crate) fn required<'m, T>(matches: &'m ArgMatches, id: &str) -> Result<&'m T, Termination>
where
    T: Clone + Send + Sync + 'static,
{
    match matches.try_get_one::<T>(id) {
        Ok(Some(value)) => Ok(value),
        Ok(None) | Err(_) => Err(Termination::Internal(Box::new(InternalError::unexpected(
            MissingArgument { id: id.to_owned() },
        )))),
    }
}

/// The value of the option `id` when it was given, with `None` for an option that was not.
///
/// Fails as an internal failure when `id` is unknown to the node or of another type.
pub(crate) fn optional<'m, T>(
    matches: &'m ArgMatches,
    id: &str,
) -> Result<Option<&'m T>, Termination>
where
    T: Clone + Send + Sync + 'static,
{
    matches.try_get_one::<T>(id).map_err(|_| {
        Termination::Internal(Box::new(InternalError::unexpected(MissingArgument {
            id: id.to_owned(),
        })))
    })
}

/// The values of the option `id` in command-line order, none when it was not given.
///
/// Fails as an internal failure when `id` is unknown to the node or of another type.
pub(crate) fn repeated<'m, T>(matches: &'m ArgMatches, id: &str) -> Result<Vec<&'m T>, Termination>
where
    T: Clone + Send + Sync + 'static,
{
    matches
        .try_get_many::<T>(id)
        .map(|values| values.map(Iterator::collect).unwrap_or_default())
        .map_err(|_| {
            Termination::Internal(Box::new(InternalError::unexpected(MissingArgument {
                id: id.to_owned(),
            })))
        })
}

#[cfg(test)]
mod tests {
    use clap::Arg;

    use super::*;

    fn matches(arguments: &[&str]) -> ArgMatches {
        clap::Command::new("index")
            .no_binary_name(true)
            .arg(Arg::new("name"))
            .arg(Arg::new("tag").long("tag").action(clap::ArgAction::Append))
            .arg(
                Arg::new("count")
                    .long("count")
                    .value_parser(clap::value_parser!(u32)),
            )
            .try_get_matches_from(arguments)
            .expect("accepted arguments")
    }

    fn internal(ended: Result<&String, Termination>) -> String {
        let termination = ended.expect_err("no value to read");
        assert!(
            matches!(
                &termination,
                Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
            ),
            "{termination:?}"
        );
        assert_eq!(termination.exit_code(), 1);
        termination.diagnostic().expect("a diagnostic")
    }

    #[test]
    fn a_value_the_matches_hold_is_returned() {
        let matches = matches(&["feature-x"]);

        let name = required::<String>(&matches, "name").expect("a value");

        assert_eq!(name, "feature-x");
    }

    #[test]
    fn an_id_the_matches_do_not_hold_is_an_internal_failure_naming_it() {
        let matches = matches(&[]);

        let diagnostic = internal(required::<String>(&matches, "name"));

        assert!(diagnostic.contains("name"), "{diagnostic}");
    }

    #[test]
    fn an_id_unknown_to_the_node_is_an_internal_failure() {
        let matches = matches(&["feature-x"]);

        let diagnostic = internal(required::<String>(&matches, "target"));

        assert!(diagnostic.contains("target"), "{diagnostic}");
    }

    #[test]
    fn a_value_of_another_type_is_an_internal_failure() {
        let matches = matches(&["feature-x", "--count", "3"]);

        let diagnostic = internal(required::<String>(&matches, "count"));

        assert!(diagnostic.contains("count"), "{diagnostic}");
    }

    #[test]
    fn an_optional_value_is_none_when_absent_and_present_when_empty() {
        assert!(
            optional::<String>(&matches(&[]), "count")
                .expect("read")
                .is_none()
        );
        let given = matches(&["--tag", ""]);
        assert_eq!(
            optional::<String>(&given, "tag").expect("read"),
            Some(&String::new())
        );
        let absent = matches(&[]);
        assert!(optional::<String>(&absent, "tag").expect("read").is_none());
    }

    #[test]
    fn an_optional_of_an_unknown_id_or_another_type_is_an_internal_failure() {
        let matches = matches(&["--count", "3"]);

        assert!(matches!(
            optional::<String>(&matches, "count"),
            Err(Termination::Internal(_))
        ));
        assert!(matches!(
            optional::<String>(&matches, "nope"),
            Err(Termination::Internal(_))
        ));
    }

    #[test]
    fn repeated_values_come_in_command_line_order() {
        let given = matches(&["--tag", "b", "--tag", "a", "--tag", ""]);

        let values = repeated::<String>(&given, "tag").expect("values");

        assert_eq!(values, ["b", "a", ""]);
        assert!(
            repeated::<String>(&matches(&[]), "tag")
                .expect("values")
                .is_empty()
        );
        assert!(matches!(
            repeated::<String>(&given, "nope"),
            Err(Termination::Internal(_))
        ));
    }
}
