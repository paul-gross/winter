//! The text of a managed block: a range of whole lines between two markers, in a file shared with the user.
//!
//! Every function is pure and splits its text on `"\n"`. A marker matches a whole line exactly, with no
//! trimming, so a line outside a block is kept byte for byte.

use std::ops::Range;

const EXCLUDE_BEGIN: &str = "# >>> ";
const EXCLUDE_END: &str = "# <<< ";
const OWNER: &str = " (managed by winter)";

/// The begin and end marker lines of one block.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Markers {
    pub begin: String,
    pub end: String,
}

impl Markers {
    /// The markers of the block `name` in an exclude file.
    pub fn exclude(name: &str) -> Self {
        Self {
            begin: format!("{EXCLUDE_BEGIN}{name}{OWNER}"),
            end: format!("{EXCLUDE_END}{name}"),
        }
    }

    /// The markers of the block `name` in a Markdown file.
    pub fn markdown(name: &str) -> Self {
        Self {
            begin: format!("<!-- >>> {name}{OWNER} -->"),
            end: format!("<!-- <<< {name} -->"),
        }
    }
}

/// Where a block that the text does not hold yet goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Position {
    Append,
    Prepend,
}

/// What a text holds of one block.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Shape {
    /// No begin marker.
    Absent,
    /// One begin marker, with its end after it.
    One,
    /// A begin marker without its end, or more than one begin marker.
    Malformed,
}

/// `content` with the block of `markers` holding `body`, ending in `"\n"`.
///
/// A block that is there is replaced where it stands, to the end of the text when it has no end marker. An
/// absent one goes at `position`, with a blank line between it and the other lines unless the line next to
/// it is blank already.
pub fn replace_or_insert(
    content: &str,
    markers: &Markers,
    body: &[&str],
    position: Position,
) -> String {
    let mut lines = lines(content);
    let mut block = vec![markers.begin.as_str()];
    block.extend_from_slice(body);
    block.push(&markers.end);

    match (find(&lines, markers), position) {
        (Some(found), _) => {
            lines.splice(found, block);
        }
        (None, Position::Append) => {
            if lines.last().is_some_and(|line| !blank(line)) {
                lines.push("");
            }
            lines.append(&mut block);
        }
        (None, Position::Prepend) => {
            if lines.first().is_some_and(|line| !blank(line)) {
                block.push("");
            }
            lines.splice(..0, block);
        }
    }
    terminated(lines.join("\n"))
}

/// `content` without the block of `markers` and without the one blank line directly above it.
///
/// A block with no end marker is removed to the end of the text. Content with no begin marker is answered
/// unchanged, and a text left empty stays empty.
pub fn strip(content: &str, markers: &Markers) -> String {
    let mut lines = lines(content);
    let Some(mut found) = find(&lines, markers) else {
        return content.to_owned();
    };
    if found.start > 0 && blank(lines[found.start - 1]) {
        found.start -= 1;
    }
    lines.drain(found);
    let rest = lines.join("\n");
    if rest.is_empty() {
        rest
    } else {
        terminated(rest)
    }
}

/// The blocks of an exclude file in file order, each with its name and the lines between its markers.
///
/// A block with no end marker runs to the end of the text. A begin marker among the lines of a block is one
/// of those lines and starts no block of its own.
pub fn blocks(content: &str) -> Vec<(String, Vec<String>)> {
    let mut lines = lines(content).into_iter();
    let mut found = Vec::new();
    while let Some(line) = lines.next() {
        let Some(name) = line
            .strip_prefix(EXCLUDE_BEGIN)
            .and_then(|rest| rest.strip_suffix(OWNER))
            .filter(|name| !name.is_empty())
        else {
            continue;
        };
        let end = Markers::exclude(name).end;
        let body = lines
            .by_ref()
            .take_while(|line| *line != end)
            .map(str::to_owned)
            .collect();
        found.push((name.to_owned(), body));
    }
    found
}

/// What `content` holds of the block of `markers`.
pub fn shape(content: &str, markers: &Markers) -> Shape {
    let lines = lines(content);
    let begins = lines.iter().filter(|line| **line == markers.begin).count();
    match (begins, find(&lines, markers)) {
        (0, _) => Shape::Absent,
        (1, Some(found)) if lines[found.end - 1] == markers.end => Shape::One,
        _ => Shape::Malformed,
    }
}

/// The lines of `content`, none for an empty text. A final `"\n"` leaves a last, empty line.
fn lines(content: &str) -> Vec<&str> {
    if content.is_empty() {
        Vec::new()
    } else {
        content.split('\n').collect()
    }
}

/// The lines of the first block of `markers`, both markers included, or all from its begin marker on when no
/// end marker follows.
fn find(lines: &[&str], markers: &Markers) -> Option<Range<usize>> {
    let start = lines.iter().position(|line| *line == markers.begin)?;
    let end = lines[start + 1..]
        .iter()
        .position(|line| *line == markers.end)
        .map_or(lines.len(), |offset| start + offset + 2);
    Some(start..end)
}

fn blank(line: &str) -> bool {
    line.trim().is_empty()
}

fn terminated(mut text: String) -> String {
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text
}
