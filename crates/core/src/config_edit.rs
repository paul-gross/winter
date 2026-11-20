//! Removal of one repository block from the text of a configuration file.
//!
//! A pure text edit: the block is located in the parsed document and removed as ranges of whole lines of
//! the original text, so every byte outside those ranges is kept. No file is read or written here.

use std::fmt;
use std::ops::Range;
use std::path::PathBuf;

use toml_edit::{Document, Item, Table, Value};

/// The array a repository block belongs to, named as a `TARGET` names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepositoryKind {
    Project,
    Standalone,
}

impl RepositoryKind {
    /// The key of the array of tables that holds the blocks of this kind.
    pub(crate) fn array_key(self) -> &'static str {
        match self {
            Self::Project => "project_repository",
            Self::Standalone => "standalone_repository",
        }
    }
}

impl fmt::Display for RepositoryKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Project => "project",
            Self::Standalone => "standalone",
        })
    }
}

/// A rule of the repository catalog that an invocation violates.
#[derive(Debug, thiserror::Error)]
pub enum CatalogError {
    #[error("{target}: a repository is named <type>/<name>")]
    TargetShape { target: String },
    #[error("{kind}: the repository type is project or standalone")]
    UnknownKind { kind: String },
    #[error("no {kind} repository named {name}")]
    NotFound { kind: RepositoryKind, name: String },
    #[error("the {kind} repositories of {} are not written as blocks", .path.display())]
    NotArrayOfTables { kind: RepositoryKind, path: PathBuf },
    #[error("the {kind} repository {name} cannot be removed without changing other entries")]
    Entangled { kind: RepositoryKind, name: String },
}

/// A text less one block, and which block that is.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct BlockRemoval {
    /// The text without the block.
    pub(crate) text: String,
    /// The position of the block among the blocks of its kind in the text it was taken from, from zero.
    pub(crate) index: usize,
}

/// The document without the first block of `kind` whose effective name is `name`, or `None` when no block
/// matches.
///
/// What is removed is whole lines. The main range starts at the block's own comment, the unbroken run of
/// comment lines that ends directly above its header, or at the header when it has none. It ends before the
/// own comment of the next header that is no sub-table of the block, before that header when it has none, and
/// at the end of the text when no header follows. A sub-table of the block whose header stands beyond the main
/// range is removed too, from its header line, with the comment lines above it left in place, to the same kind
/// of end. A document that does not parse has no block.
pub(crate) fn remove_repository_block(
    document: &str,
    kind: RepositoryKind,
    name: &str,
) -> Option<BlockRemoval> {
    let parsed = Document::parse(document).ok()?;
    let blocks = parsed.as_table().get(kind.array_key())?;
    let (index, block) = blocks
        .as_array_of_tables()?
        .iter()
        .enumerate()
        .find(|(_, block)| effective_name(block).as_deref() == Some(name))?;
    let header = block.span()?.start;

    let mut whole = Layout::default();
    whole.collect(parsed.as_table());
    let mut own = Layout::default();
    own.collect(block);
    own.headers.sort_unstable();
    let lines = Lines::of(document, &whole.values);
    // Where what belongs to the header at `start` ends: before the own comment of the next header that is not
    // the block's, or with the text.
    let end_after = |start: usize| {
        whole
            .headers
            .iter()
            .copied()
            .filter(|other| *other > start && !own.headers.contains(other))
            .min()
            .map_or(document.len(), |next| lines.own_comment_start(next))
    };
    let mut removed: Vec<Range<usize>> = Vec::new();
    for start in std::iter::once(header).chain(own.headers.iter().copied()) {
        if !removed.iter().any(|range| range.contains(&start)) {
            let from = if start == header {
                lines.own_comment_start(start)
            } else {
                lines.line_start(start)
            };
            removed.push(from..end_after(start));
        }
    }

    let mut rest = String::with_capacity(document.len());
    let mut kept_from = 0;
    for range in removed {
        rest.push_str(document.get(kept_from..range.start)?);
        kept_from = range.end;
    }
    rest.push_str(document.get(kept_from..)?);
    Some(BlockRemoval { text: rest, index })
}

/// Whether `edited` is `original` less the block of `kind` at `index`: both parse, every other key holds the
/// same value, and the blocks of `kind` are those of `original` in their order with the one at `index` left
/// out. The array is absent from `edited` when its only block is the one left out.
///
/// Two values are the same when they have one type and one content, tables whatever the order of their keys
/// and arrays element by element. Floats are the same when their bits are: a NaN is the same as that NaN, and
/// `-0.0` is not the same as `0.0`.
pub(crate) fn lacks_one_block(
    original: &str,
    edited: &str,
    kind: RepositoryKind,
    index: usize,
) -> bool {
    let (Ok(mut before), Ok(mut after)) = (
        toml::from_str::<toml::Table>(original),
        toml::from_str::<toml::Table>(edited),
    ) else {
        return false;
    };
    let Some(toml::Value::Array(blocks)) = before.remove(kind.array_key()) else {
        return false;
    };
    let rest = match after.remove(kind.array_key()) {
        Some(toml::Value::Array(rest)) if !rest.is_empty() => rest,
        None => Vec::new(),
        Some(_) => return false,
    };
    let kept = blocks
        .iter()
        .enumerate()
        .filter(|(position, _)| *position != index)
        .map(|(_, block)| block);
    same_table(&before, &after)
        && index < blocks.len()
        && rest.len() + 1 == blocks.len()
        && kept.zip(&rest).all(|(block, kept)| same_value(block, kept))
}

/// Whether two tables hold the same keys with the same values.
fn same_table(left: &toml::Table, right: &toml::Table) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .all(|(key, value)| right.get(key).is_some_and(|other| same_value(value, other)))
}

/// Whether two values are the same: equal, but for floats, which are compared by their bits.
fn same_value(left: &toml::Value, right: &toml::Value) -> bool {
    use toml::Value as V;
    match (left, right) {
        (V::Float(left), V::Float(right)) => left.to_bits() == right.to_bits(),
        (V::Array(left), V::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| same_value(left, right))
        }
        (V::Table(left), V::Table(right)) => same_table(left, right),
        (V::String(left), V::String(right)) => left == right,
        (V::Integer(left), V::Integer(right)) => left == right,
        (V::Boolean(left), V::Boolean(right)) => left == right,
        (V::Datetime(left), V::Datetime(right)) => left == right,
        _ => false,
    }
}

/// The name a block is matched by, or `None` for a block that never matches.
///
/// A `name` key decides alone: a string is the name as it is, an integer is its decimal digits, and a value
/// of any other type gives the block no name. Without a `name` key, a `url` that is a string and not empty
/// gives the name derived from it.
fn effective_name(block: &Table) -> Option<String> {
    match block.get("name") {
        Some(Item::Value(Value::String(name))) => Some(name.value().clone()),
        Some(Item::Value(Value::Integer(name))) => Some(name.value().to_string()),
        Some(_) => None,
        None => match block.get("url")?.as_str()? {
            "" => None,
            url => Some(name_from_url(url).to_owned()),
        },
    }
}

/// The repository name a URL ends with: what follows the last `/` or `:`, whichever is later, once every
/// trailing `/` is gone, and without one trailing `.git`.
fn name_from_url(url: &str) -> &str {
    let trimmed = url.trim_end_matches('/');
    let last = trimmed
        .rsplit_once(['/', ':'])
        .map_or(trimmed, |(_, last)| last);
    last.strip_suffix(".git").unwrap_or(last)
}

/// Where the table headers and the values of a table and of everything below it start and lie.
#[derive(Default)]
struct Layout {
    /// The offset of each table header.
    headers: Vec<usize>,
    /// The range of each value of a key.
    values: Vec<Range<usize>>,
}

impl Layout {
    /// Adds what `table` holds, not the header of `table` itself.
    fn collect(&mut self, table: &Table) {
        for (_, item) in table.iter() {
            match item {
                Item::Value(value) => self.values.extend(value.span()),
                Item::Table(child) => self.collect_table(child),
                Item::ArrayOfTables(children) => {
                    for child in children.iter() {
                        self.collect_table(child);
                    }
                }
                Item::None => {}
            }
        }
    }

    /// Adds `table` and what it holds. A table that dotted keys make, or that only the header of a table
    /// below it implies, has no header of its own.
    fn collect_table(&mut self, table: &Table) {
        if !table.is_dotted() && !table.is_implicit() {
            self.headers.extend(table.span().map(|span| span.start));
        }
        self.collect(table);
    }
}

/// The lines of a text, and which of them are comment lines.
struct Lines<'a> {
    text: &'a str,
    /// The offset each line starts at, in order.
    starts: Vec<usize>,
    values: &'a [Range<usize>],
}

impl<'a> Lines<'a> {
    fn of(text: &'a str, values: &'a [Range<usize>]) -> Self {
        // A byte-order mark is no part of the first line: the line starts after it, and the mark is kept.
        let first = text
            .strip_prefix('\u{feff}')
            .map_or(0, |rest| text.len() - rest.len());
        let starts = std::iter::once(first)
            .chain(text.match_indices('\n').map(|(newline, _)| newline + 1))
            .filter(|start| *start < text.len())
            .collect();
        Self {
            text,
            starts,
            values,
        }
    }

    /// Whether the line that starts at `start` is a comment line: its first character after spaces and tabs
    /// is `#`. A line that continues a value, as inside a string of several lines, is none.
    fn is_comment(&self, start: usize) -> bool {
        let inside_a_value = self
            .values
            .iter()
            .any(|value| value.start < start && start < value.end);
        !inside_a_value
            && self
                .text
                .get(start..)
                .is_some_and(|rest| rest.trim_start_matches([' ', '\t']).starts_with('#'))
    }

    /// The offset at which the line that holds `offset` starts.
    fn line_start(&self, offset: usize) -> usize {
        let line = self.starts.partition_point(|start| *start <= offset);
        self.starts
            .get(line.saturating_sub(1))
            .copied()
            .unwrap_or(offset)
    }

    /// The offset at which the own comment of the header at `header` starts: the start of the first line of
    /// the unbroken run of comment lines directly above the header's line, or of that line without one.
    fn own_comment_start(&self, header: usize) -> usize {
        let line = self.starts.partition_point(|start| *start <= header);
        let above = self.starts.get(..line.saturating_sub(1)).unwrap_or(&[]);
        let comments = above
            .iter()
            .rev()
            .take_while(|start| self.is_comment(**start))
            .count();
        self.starts
            .get(line.saturating_sub(1) - comments)
            .copied()
            .unwrap_or(header)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PLAIN: &str = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"

[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

    const COMMENTED: &str = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
# after api

# second: web
[[project_repository]]
url = \"https://fixture.invalid/web.git\"
# after web

# third: docs
[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

    fn project(document: &str, name: &str) -> Option<String> {
        remove_repository_block(document, RepositoryKind::Project, name).map(|removal| removal.text)
    }

    /// Whether the one block of a document with `block_keys` under a project header is matched by `name`.
    fn matches(block_keys: &str, name: &str) -> bool {
        let document = format!("[[project_repository]]\n{block_keys}\n");
        match project(&document, name) {
            Some(rest) => {
                assert_eq!(rest, "", "{block_keys}: {name}");
                true
            }
            None => false,
        }
    }

    #[test]
    fn the_kinds_display_as_a_target_names_them() {
        assert_eq!(RepositoryKind::Project.to_string(), "project");
        assert_eq!(RepositoryKind::Standalone.to_string(), "standalone");
    }

    #[test]
    fn each_error_names_the_value_at_fault() {
        let shape = CatalogError::TargetShape {
            target: "nope".to_owned(),
        };
        let unknown = CatalogError::UnknownKind {
            kind: "other".to_owned(),
        };
        let not_found = CatalogError::NotFound {
            kind: RepositoryKind::Standalone,
            name: "tools".to_owned(),
        };
        let entangled = CatalogError::Entangled {
            kind: RepositoryKind::Project,
            name: "api".to_owned(),
        };

        assert!(shape.to_string().contains("nope"), "{shape}");
        assert!(unknown.to_string().contains("other"), "{unknown}");
        let text = entangled.to_string();
        assert!(text.contains("project") && text.contains("api"), "{text}");
        let text = not_found.to_string();
        assert!(
            text.contains("standalone") && text.contains("tools"),
            "{text}"
        );
    }

    #[test]
    fn a_middle_block_goes_with_its_own_comment_and_leaves_the_next_block_s() {
        let expected = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
# after api

# third: docs
[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

        assert_eq!(project(COMMENTED, "web").as_deref(), Some(expected));
    }

    #[test]
    fn the_last_commented_block_goes_from_its_own_comment_to_the_end_of_the_file() {
        let expected = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
# after api

# second: web
[[project_repository]]
url = \"https://fixture.invalid/web.git\"
# after web

";

        assert_eq!(project(COMMENTED, "docs").as_deref(), Some(expected));
    }

    #[test]
    fn the_first_commented_block_goes_with_what_follows_it_up_to_the_next_block_s_comment() {
        let expected = "\
# workspace repositories

# second: web
[[project_repository]]
url = \"https://fixture.invalid/web.git\"
# after web

# third: docs
[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

        assert_eq!(project(COMMENTED, "api").as_deref(), Some(expected));
    }

    #[test]
    fn the_last_plain_block_leaves_the_blank_line_that_stood_above_it() {
        let expected = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"

";

        assert_eq!(project(PLAIN, "docs").as_deref(), Some(expected));
    }

    #[test]
    fn a_comment_run_directly_above_the_header_is_removed_with_the_block() {
        let document = "\
top = 1

# about b
# more about b
[[project_repository]]
name = \"b\"
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("top = 1\n\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn a_comment_run_a_blank_line_above_the_header_is_kept_with_that_blank_line() {
        let document = "\
top = 1
# not about b

[[project_repository]]
name = \"b\"
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("top = 1\n# not about b\n\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn comment_lines_after_the_last_key_and_above_a_blank_line_are_removed() {
        let document = "\
[[project_repository]]
name = \"b\"
# trailing
# more trailing

[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn of_two_comment_runs_after_the_last_key_the_one_above_the_next_header_is_kept() {
        let document = "\
[[project_repository]]
name = \"b\"
# trailing

# about c
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("# about c\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn comment_lines_between_the_last_key_and_the_next_header_are_the_next_header_s() {
        let document = "\
[[project_repository]]
name = \"b\"
# about c
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("# about c\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn without_comments_the_block_goes_from_its_header_to_the_next_header() {
        let document = "\
top = 1
[[project_repository]]
name = \"b\"
[other]
key = 2
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("top = 1\n[other]\nkey = 2\n")
        );
    }

    #[test]
    fn two_blank_lines_above_the_own_comment_are_both_kept() {
        let document = "\
top = 1


# about b
[[project_repository]]
name = \"b\"
";

        assert_eq!(project(document, "b").as_deref(), Some("top = 1\n\n\n"));
    }

    #[test]
    fn two_blank_lines_between_the_last_key_and_the_next_header_s_comment_are_both_removed() {
        let document = "\
[[project_repository]]
name = \"b\"


# about c
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("# about c\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn the_only_block_of_a_file_leaves_what_stood_above_it() {
        let document = "\
env_aliases = [\"one\"]

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
";

        assert_eq!(
            project(document, "api").as_deref(),
            Some("env_aliases = [\"one\"]\n\n")
        );
    }

    #[test]
    fn a_block_is_matched_by_its_explicit_name_and_not_by_the_name_of_its_url() {
        let keys = "name = \"docs\"\nurl = \"https://fixture.invalid/manual.git\"";

        assert!(matches(keys, "docs"));
        assert!(!matches(keys, "manual"));
    }

    #[test]
    fn a_block_with_neither_name_nor_url_never_matches() {
        for name in ["", "branch", "main"] {
            assert!(!matches("branch = \"main\"", name), "{name:?}");
        }
    }

    /// Asserts that a block with only `url` is matched by `name` and by none of `others`.
    fn assert_url_name(url: &str, name: &str, others: &[&str]) {
        let keys = format!("url = \"{url}\"");

        assert!(matches(&keys, name), "{url}: {name}");
        for other in others {
            assert!(!matches(&keys, other), "{url}: {other}");
        }
    }

    #[test]
    fn a_url_that_ends_in_dot_git_gives_its_last_segment_without_it() {
        assert_url_name(
            "https://host/org/api.git",
            "api",
            &["api.git", "org/api", "org", "host", ""],
        );
    }

    #[test]
    fn a_url_that_ends_in_a_slash_gives_the_segment_before_it() {
        assert_url_name("https://host/org/api/", "api", &["", "api/", "org", "host"]);
    }

    #[test]
    fn a_url_with_only_a_colon_gives_what_follows_the_colon() {
        assert_url_name("host:api.git", "api", &["host", "host:api", "api.git", ""]);
    }

    #[test]
    fn a_url_with_a_colon_and_then_a_slash_gives_what_follows_the_slash() {
        assert_url_name(
            "git@host:org/api.git",
            "api",
            &["org/api", "org", "git@host", "api.git", ""],
        );
    }

    #[test]
    fn a_url_with_a_slash_and_then_a_colon_gives_what_follows_the_colon() {
        assert_url_name("host/a:b.git", "b", &["a:b", "a", "host", "b.git", ""]);
    }

    #[test]
    fn a_url_of_a_host_alone_gives_the_host() {
        assert_url_name("https://host/", "host", &["", "https", "//host", "host/"]);
    }

    #[test]
    fn only_one_trailing_dot_git_is_dropped() {
        assert_url_name("https://host/api.git.git", "api.git", &["api"]);
    }

    #[test]
    fn an_empty_name_is_the_name_and_the_url_is_not_consulted() {
        let keys = "name = \"\"\nurl = \"https://host/org/api.git\"";

        assert!(!matches(keys, "api"));
        assert!(matches(keys, ""));
    }

    #[test]
    fn an_integer_name_is_matched_by_its_decimal_digits() {
        assert!(matches("name = 7", "7"));
        assert!(!matches("name = 7", "07"));
        assert!(matches("name = 0x10\nurl = \"https://host/api.git\"", "16"));
        assert!(!matches(
            "name = 0x10\nurl = \"https://host/api.git\"",
            "api"
        ));
    }

    #[test]
    fn an_empty_url_without_a_name_never_matches() {
        for name in ["", "url"] {
            assert!(!matches("url = \"\"", name), "{name:?}");
        }
    }

    #[test]
    fn a_name_or_a_url_of_another_type_gives_the_block_no_name() {
        let url = "url = \"https://host/api.git\"";
        for name in ["true", "1.5", "[\"api\"]", "{ a = 1 }", "1979-05-27"] {
            let keys = format!("name = {name}\n{url}");
            for target in ["api", name, "", "a"] {
                assert!(!matches(&keys, target), "{keys}: {target}");
            }
        }
        for target in ["7", "", "url"] {
            assert!(!matches("url = 7", target), "{target}");
        }
    }

    #[test]
    fn of_two_blocks_with_one_name_only_the_first_is_removed() {
        let document = "\
[[project_repository]]
name = \"twin\"
branch = \"first\"

[[project_repository]]
name = \"twin\"
branch = \"second\"
";

        assert_eq!(
            project(document, "twin").as_deref(),
            Some("[[project_repository]]\nname = \"twin\"\nbranch = \"second\"\n")
        );
    }

    #[test]
    fn a_standalone_target_leaves_a_project_block_of_the_same_name_alone() {
        let document = "\
[[project_repository]]
name = \"tools\"

[[standalone_repository]]
name = \"tools\"
";

        assert_eq!(
            remove_repository_block(document, RepositoryKind::Standalone, "tools"),
            Some(BlockRemoval {
                text: "[[project_repository]]\nname = \"tools\"\n\n".to_owned(),
                index: 0,
            })
        );
        assert_eq!(
            project(document, "tools").as_deref(),
            Some("[[standalone_repository]]\nname = \"tools\"\n")
        );
    }

    #[test]
    fn a_file_without_a_final_newline_keeps_that() {
        let document = "\
[[project_repository]]
name = \"b\"

[[project_repository]]
name = \"c\"";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("[[project_repository]]\nname = \"c\"")
        );
        assert_eq!(
            project(document, "c").as_deref(),
            Some("[[project_repository]]\nname = \"b\"\n\n")
        );
    }

    #[test]
    fn a_comment_line_indented_with_a_tab_is_a_comment_line() {
        let document = "\
top = 1
\t# about b
 \t # more about b
[[project_repository]]
name = \"b\"
\t# about c
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("top = 1\n\t# about c\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn no_matching_block_is_none() {
        assert_eq!(project(COMMENTED, "nope"), None);
        assert_eq!(project(COMMENTED, ""), None);
        assert_eq!(project(COMMENTED, "Docs"), None);
        assert_eq!(
            remove_repository_block(COMMENTED, RepositoryKind::Standalone, "docs"),
            None
        );
        assert_eq!(project("", "docs"), None);
        assert_eq!(project("# nothing declared\n", "docs"), None);
    }

    #[test]
    fn a_repository_array_written_inline_holds_no_block() {
        let document = "project_repository = [{ name = \"docs\" }]\n";

        assert_eq!(project(document, "docs"), None);
    }

    #[test]
    fn a_text_that_is_not_toml_holds_no_block() {
        assert_eq!(project("[[project_repository]]\nname = \n", "docs"), None);
    }

    #[test]
    fn a_sub_table_of_the_block_is_removed_with_it() {
        let document = "\
[[project_repository]]
name = \"b\"
# about the options
[project_repository.options]
depth = 1

[[project_repository.mirror]]
url = \"https://host/mirror.git\"

# about c
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("# about c\n[[project_repository]]\nname = \"c\"\n")
        );
    }

    #[test]
    fn a_sub_table_of_the_block_before_the_range_s_end_is_not_the_next_header() {
        let document = "\
[[project_repository]]
name = \"b\"
[project_repository.options]
depth = 1
[other]
key = 2
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("[other]\nkey = 2\n")
        );
    }

    #[test]
    fn a_line_of_a_string_that_starts_with_a_hash_is_no_comment_line() {
        let document = "\
[[project_repository]]
name = \"a\"
note = \"\"\"
# inside the string of a\"\"\"
[[project_repository]]
name = \"b\"
note = \"\"\"
# inside the string of b\"\"\"
[[project_repository]]
name = \"c\"
";

        assert_eq!(
            project(document, "b").as_deref(),
            Some(
                "[[project_repository]]\nname = \"a\"\nnote = \"\"\"\n# inside the string of a\"\"\"\n\
                 [[project_repository]]\nname = \"c\"\n"
            )
        );
    }

    #[test]
    fn lines_that_end_in_a_carriage_return_are_kept_as_they_are() {
        let document = "top = 1\r\n# about b\r\n[[project_repository]]\r\nname = \"b\"\r\n\r\n\
                        # about c\r\n[[project_repository]]\r\nname = \"c\"\r\n";

        assert_eq!(
            project(document, "b").as_deref(),
            Some("top = 1\r\n# about c\r\n[[project_repository]]\r\nname = \"c\"\r\n")
        );
    }

    const API: &str = "[[project_repository]]\nurl = \"h/api.git\"\n";
    const WEB: &str = "[[project_repository]]\nurl = \"h/web.git\"\n";
    const TOOL: &str = "[tool]\nx = 1\n";
    const EXTRA: &str = "[project_repository.extra]\nk = 1\n";

    #[test]
    fn a_sub_table_of_the_block_after_another_table_is_removed_with_it() {
        let document = [API, TOOL, EXTRA, WEB].concat();

        assert_eq!(project(&document, "api"), Some([TOOL, WEB].concat()));
    }

    #[test]
    fn a_detached_sub_table_does_not_pass_to_the_block_before_the_removed_one() {
        let document = [WEB, API, TOOL, EXTRA].concat();

        assert_eq!(project(&document, "api"), Some([WEB, TOOL].concat()));
    }

    #[test]
    fn a_detached_sub_table_of_the_only_block_leaves_no_table_of_the_array_s_name() {
        let document = [API, TOOL, EXTRA].concat();

        assert_eq!(project(&document, "api").as_deref(), Some(TOOL));
    }

    #[test]
    fn two_detached_sub_tables_are_both_removed() {
        let document = [
            API,
            TOOL,
            EXTRA,
            "[other]\ny = 2\n",
            "[[project_repository.mirror]]\nurl = \"m\"\n",
            "[last]\nz = 3\n",
        ]
        .concat();

        assert_eq!(
            project(&document, "api"),
            Some([TOOL, "[other]\ny = 2\n", "[last]\nz = 3\n"].concat())
        );
    }

    #[test]
    fn a_detached_sub_table_ends_before_the_own_comment_of_the_next_block() {
        let document = [
            API,
            TOOL,
            "# above extra\n",
            EXTRA,
            "# after extra\n\n# about web\n",
            WEB,
        ]
        .concat();

        assert_eq!(
            project(&document, "api"),
            Some([TOOL, "# above extra\n", "# about web\n", WEB].concat())
        );
    }

    #[test]
    fn a_byte_order_mark_before_a_header_on_the_first_line_is_kept() {
        let document = ["\u{feff}", API, "\n", WEB].concat();

        assert_eq!(project(&document, "api"), Some(["\u{feff}", WEB].concat()));
    }

    #[test]
    fn a_comment_on_the_first_line_after_a_byte_order_mark_is_the_block_s_own() {
        let document = ["\u{feff}# about api\n", API, "\n", WEB].concat();

        assert_eq!(project(&document, "api"), Some(["\u{feff}", WEB].concat()));
    }

    #[test]
    fn characters_of_several_bytes_before_and_inside_the_block_are_counted_as_bytes() {
        let document = "\
title = \"Büro – 事務所\"

# zweites: wéb ☃
[[project_repository]]
name = \"wéb\"
note = \"雪 ❄\"

# drittes: ünï
[[project_repository]]
name = \"ünï\"
";

        assert_eq!(
            project(document, "wéb").as_deref(),
            Some(
                "title = \"Büro – 事務所\"\n\n# drittes: ünï\n[[project_repository]]\nname = \"ünï\"\n"
            )
        );
    }

    #[test]
    fn the_removal_names_the_position_of_the_block_among_those_of_its_kind() {
        let document = [API, "[[standalone_repository]]\nurl = \"h/web.git\"\n", WEB].concat();
        let index = |kind, name| {
            remove_repository_block(&document, kind, name).map(|removal| removal.index)
        };

        assert_eq!(index(RepositoryKind::Project, "api"), Some(0));
        assert_eq!(index(RepositoryKind::Project, "web"), Some(1));
        assert_eq!(index(RepositoryKind::Standalone, "web"), Some(0));
    }

    #[test]
    fn a_text_less_the_block_at_the_index_lacks_that_block() {
        let kind = RepositoryKind::Project;
        let both = [TOOL, API, WEB].concat();

        assert!(lacks_one_block(&both, &[TOOL, WEB].concat(), kind, 0));
        assert!(lacks_one_block(&both, &[TOOL, API].concat(), kind, 1));
        assert!(lacks_one_block(&[TOOL, API].concat(), TOOL, kind, 0));
        assert!(lacks_one_block(&[API, API].concat(), API, kind, 0));
        assert!(lacks_one_block(&[API, API].concat(), API, kind, 1));
    }

    #[test]
    fn a_text_less_another_block_than_the_one_at_the_index_does_not_lack_that_block() {
        let kind = RepositoryKind::Project;
        let both = [TOOL, API, WEB].concat();

        assert!(!lacks_one_block(&both, &[TOOL, WEB].concat(), kind, 1));
        assert!(!lacks_one_block(&both, &[TOOL, API].concat(), kind, 0));
        assert!(!lacks_one_block(&both, &[TOOL, WEB].concat(), kind, 2));
        assert!(!lacks_one_block(&[TOOL, API].concat(), TOOL, kind, 1));
        assert!(!lacks_one_block(&both, &[TOOL, API].concat(), kind, 5));
    }

    #[test]
    fn floats_are_the_same_by_their_bits() {
        let kind = RepositoryKind::Project;
        let with = |float: &str| format!("[tool]\nx = {float}\n{API}");
        let without = |float: &str| format!("[tool]\nx = {float}\n");

        for float in ["nan", "+nan", "-nan", "inf", "-inf", "0.0", "-0.0", "1.5"] {
            assert!(
                lacks_one_block(&with(float), &without(float), kind, 0),
                "{float}"
            );
        }
        for (before, after) in [
            ("0.0", "-0.0"),
            ("inf", "-inf"),
            ("1.5", "1.25"),
            ("1.0", "1"),
        ] {
            assert!(
                !lacks_one_block(&with(before), &without(after), kind, 0),
                "{before} {after}"
            );
        }
    }

    #[test]
    fn a_difference_inside_a_nested_table_or_array_is_a_difference() {
        let kind = RepositoryKind::Project;
        let with = |value: &str| format!("[tool]\nx = {value}\n{API}");
        let without = |value: &str| format!("[tool]\nx = {value}\n");

        for value in [
            "[1, [2.5, nan], { a = [-0.0] }]",
            "{ a = { b = [nan] } }",
            "1979-05-27T07:32:00Z",
        ] {
            assert!(
                lacks_one_block(&with(value), &without(value), kind, 0),
                "{value}"
            );
        }
        for (before, after) in [
            ("[1, 2]", "[1, 3]"),
            ("[1, 2]", "[1]"),
            ("[1]", "[1, 2]"),
            ("[[1.5, nan]]", "[[1.5, 0.0]]"),
            ("[{ a = [0.0] }]", "[{ a = [-0.0] }]"),
            ("{ a = { b = 1 } }", "{ a = { b = 2 } }"),
            ("{ a = 1 }", "{ b = 1 }"),
            ("1979-05-27", "1979-05-28"),
            ("1979-05-27T07:32:00Z", "1979-05-27T07:32:00"),
        ] {
            assert!(
                !lacks_one_block(&with(before), &without(after), kind, 0),
                "{before} {after}"
            );
        }

        let kept = |value: u8| format!("{WEB}[[project_repository.items]]\nv = {value}\n");
        assert!(lacks_one_block(
            &[API, &kept(1)].concat(),
            &kept(1),
            kind,
            0
        ));
        assert!(!lacks_one_block(
            &[API, &kept(1)].concat(),
            &kept(2),
            kind,
            0
        ));
    }

    #[test]
    fn a_text_that_differs_otherwise_does_not_lack_one_block() {
        let kind = RepositoryKind::Project;
        let both = [TOOL, API, WEB].concat();
        let cases = [
            ("the same text", both.clone()),
            ("both blocks gone", TOOL.to_owned()),
            ("another key gone", [API].concat()),
            ("another key changed", ["[tool]\nx = 2\n", WEB].concat()),
            ("another key added", [TOOL, "y = 1\n", WEB].concat()),
            ("the other block changed", [TOOL, WEB, "k = 1\n"].concat()),
            ("a block more", [TOOL, WEB, API, WEB].concat()),
            ("no TOML", [TOOL, "[[project_repository\n"].concat()),
            (
                "an empty array",
                ["project_repository = []\n", TOOL].concat(),
            ),
            ("a table of the array's name", [TOOL, EXTRA].concat()),
        ];

        for (case, edited) in cases {
            assert!(!lacks_one_block(&both, &edited, kind, 0), "{case}");
        }
        assert!(!lacks_one_block("[[project_repository\n", TOOL, kind, 0));
        assert!(!lacks_one_block(TOOL, TOOL, kind, 0), "no array before");
        assert!(
            !lacks_one_block(&both, &[TOOL, API].concat(), RepositoryKind::Standalone, 1),
            "the other array"
        );
    }
}
