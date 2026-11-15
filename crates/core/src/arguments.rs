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

#[cfg(test)]
mod tests {
    use clap::Arg;

    use super::*;

    fn matches(arguments: &[&str]) -> ArgMatches {
        clap::Command::new("index")
            .no_binary_name(true)
            .arg(Arg::new("name"))
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
}
