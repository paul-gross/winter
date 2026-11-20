//! `repo list`: the repositories of the workspace, the implicit ones first, then the standalone ones, then
//! the projects.

use std::path::Path;

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::diagnostics::Diagnostics;
use crate::output;
use crate::registry::Registration;
use crate::repository_catalog::{self, ListError, Listed, Listing};
use crate::termination::Termination;

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

/// What the arguments ask for.
#[derive(Debug, PartialEq, Eq)]
struct Params {
    json: bool,
}

fn args(command: clap::Command) -> clap::Command {
    command.about("List the repositories of the workspace").arg(
        Arg::new("json")
            .long("json")
            .action(ArgAction::SetTrue)
            .help("Print the result as one JSON document"),
    )
}

fn params(matches: &ArgMatches) -> Params {
    Params {
        json: matches.get_flag("json"),
    }
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let params = params(matches);
    let mut diagnostics = Diagnostics {
        err: &mut *cx.err,
        log: cx.log,
    };
    let listing = match repository_catalog::list(cx.files, cx.cwd, &mut diagnostics) {
        Ok(listing) => listing,
        Err(ListError::Config(error)) => return Termination::Config(Box::new(error)),
        Err(ListError::Internal(error)) => return Termination::Internal(Box::new(error)),
    };
    if params.json {
        output::document(cx.out, &document(&listing))
    } else {
        output::text(cx.out, &table(&listing))
    }
}

/// One object per repository, in the order of the list. A location is absolute.
fn document(listing: &Listing) -> serde_json::Value {
    listing
        .entries
        .iter()
        .map(|entry| match entry {
            Listed::Singleton { name, path } => serde_json::json!({
                "name": name,
                "type": "singleton",
                "path": path.to_string_lossy(),
            }),
            Listed::Standalone { name, path, .. } => serde_json::json!({
                "name": name,
                "type": "standalone",
                "path": path.to_string_lossy(),
            }),
            Listed::Project {
                name,
                main_path,
                pinned,
                ..
            } => serde_json::json!({
                "name": name,
                "type": "project",
                "main_path": main_path.to_string_lossy(),
                "pinned": pinned,
            }),
        })
        .collect()
}

/// One line per repository, in the order of the list, each ended by a newline: the source URL or `-`, the
/// type, the location relative to the root, and a mark on a pinned project. The columns are aligned, and no
/// line ends in a space. An empty list is an empty text.
fn table(listing: &Listing) -> String {
    let rows: Vec<Vec<String>> = listing
        .entries
        .iter()
        .map(|entry| row(&listing.root, entry))
        .collect();
    output::table(&rows)
}

/// The cells of one repository. The mark of a pinned project is a fourth cell, which no other row has.
fn row(root: &Path, entry: &Listed) -> Vec<String> {
    let source = |url: &Option<String>| url.clone().unwrap_or_else(|| "-".to_owned());
    match entry {
        Listed::Singleton { path, .. } => {
            vec!["-".to_owned(), "singleton".to_owned(), relative(root, path)]
        }
        Listed::Standalone { url, path, .. } => {
            vec![source(url), "standalone".to_owned(), relative(root, path)]
        }
        Listed::Project {
            url,
            main_path,
            pinned,
            ..
        } => {
            let mut cells = vec![source(url), "project".to_owned(), relative(root, main_path)];
            if *pinned {
                cells.push("[pinned]".to_owned());
            }
            cells
        }
    }
}

/// `location` as a path from `root`: `.` for the root itself, and the location as it stands when it does not
/// lie under the root.
fn relative(root: &Path, location: &Path) -> String {
    match location.strip_prefix(root) {
        Ok(below) if below.as_os_str().is_empty() => ".".to_owned(),
        Ok(below) => below.to_string_lossy().into_owned(),
        Err(_) => location.to_string_lossy().into_owned(),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("list").no_binary_name(true)).try_get_matches_from(arguments)
    }

    fn singleton(name: &'static str, path: &str) -> Listed {
        Listed::Singleton {
            name,
            path: path.into(),
        }
    }

    fn standalone(name: &str, url: Option<&str>, path: &str) -> Listed {
        Listed::Standalone {
            name: name.to_owned(),
            url: url.map(str::to_owned),
            path: path.into(),
        }
    }

    fn project(name: &str, url: Option<&str>, main_path: &str, pinned: bool) -> Listed {
        Listed::Project {
            name: name.to_owned(),
            url: url.map(str::to_owned),
            main_path: main_path.into(),
            pinned,
        }
    }

    fn listing(entries: Vec<Listed>) -> Listing {
        Listing {
            root: PathBuf::from("/ws"),
            entries,
        }
    }

    /// One repository of each type, and a second project that is pinned.
    fn one_of_each() -> Listing {
        listing(vec![
            singleton("product", "/ws/product"),
            standalone(
                "context",
                Some("https://fixture.invalid/ctx.git"),
                "/ws/.winter/ext/context",
            ),
            project("web", None, "/ws/projects/web", false),
            project(
                "api",
                Some("https://fixture.invalid/api.git"),
                "/ws/projects/api",
                true,
            ),
        ])
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("list")).debug_assert();
    }

    #[test]
    fn no_argument_asks_for_the_human_table_and_the_flag_for_the_document() {
        let bare = parsed(&[]).expect("accepted arguments");
        let flagged = parsed(&["--json"]).expect("accepted arguments");

        assert_eq!(params(&bare), Params { json: false });
        assert_eq!(params(&flagged), Params { json: true });
    }

    #[test]
    fn a_positional_and_a_repeated_flag_are_refused() {
        assert!(parsed(&["project/api"]).is_err());
        assert!(parsed(&["--json", "--json"]).is_err());
        assert!(parsed(&["--local"]).is_err());
    }

    #[test]
    fn the_document_holds_one_object_per_repository_with_the_keys_of_its_type() {
        assert_eq!(
            document(&one_of_each()),
            serde_json::json!([
                {"name": "product", "type": "singleton", "path": "/ws/product"},
                {"name": "context", "type": "standalone", "path": "/ws/.winter/ext/context"},
                {"name": "web", "type": "project", "main_path": "/ws/projects/web", "pinned": false},
                {"name": "api", "type": "project", "main_path": "/ws/projects/api", "pinned": true},
            ])
        );
    }

    #[test]
    fn the_document_of_an_empty_list_is_an_empty_array() {
        assert_eq!(document(&listing(Vec::new())), serde_json::json!([]));
    }

    #[test]
    fn the_table_aligns_its_columns_and_marks_a_pinned_project() {
        assert_eq!(
            table(&one_of_each()),
            "\
-                                singleton   product
https://fixture.invalid/ctx.git  standalone  .winter/ext/context
-                                project     projects/web
https://fixture.invalid/api.git  project     projects/api         [pinned]
"
        );
    }

    #[test]
    fn the_table_of_an_empty_list_is_empty() {
        assert_eq!(table(&listing(Vec::new())), "");
    }

    #[test]
    fn a_column_is_as_wide_as_its_widest_cell_counted_in_characters() {
        let listing = listing(vec![
            standalone("é", Some("https://fixture.invalid/ééé"), "/ws/é"),
            standalone("b", Some("https://fixture.invalid/b"), "/ws/b"),
        ]);

        assert_eq!(
            table(&listing),
            "\
https://fixture.invalid/ééé  standalone  é
https://fixture.invalid/b    standalone  b
"
        );
    }

    #[test]
    fn a_location_is_written_from_the_root_and_one_outside_it_as_it_stands() {
        let root = Path::new("/ws");

        for (location, expected) in [
            ("/ws/projects/api", "projects/api"),
            ("/ws/projects/..", "projects/.."),
            ("/ws", "."),
            ("/elsewhere/api", "/elsewhere/api"),
            ("/wsx/api", "/wsx/api"),
        ] {
            assert_eq!(relative(root, Path::new(location)), expected, "{location}");
        }
    }
}
