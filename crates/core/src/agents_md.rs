//! The eager safety block of the workspace's `AGENTS.md`, and the `CLAUDE.md` shim.
//!
//! Both files are shared with the user: nothing outside the block is rewritten, and a conflict writes nothing.
//! Each entry is one update of one file through the file port.

use std::io;
use std::path::{Path, PathBuf};

use crate::files::WriteFiles;
use crate::managed_block::{Markers, Position, Shape, replace_or_insert, shape};
use crate::topics::eager_text;

/// The name of the managed block that holds the eager safety text.
pub const SAFETY_BLOCK: &str = "winter-workspace/safety";

const AGENTS_FILE: &str = "AGENTS.md";
const SHIM_FILE: &str = "CLAUDE.md";
/// The line of `CLAUDE.md` that includes `AGENTS.md`.
const SHIM_LINE: &str = "@AGENTS.md";

/// What placing or refreshing the safety block did to `AGENTS.md`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Placement {
    /// There was no file: it now holds the block alone.
    Created,
    /// The file was there, and the block was added to it or replaced.
    Updated,
    /// The file holds the current block already.
    Unchanged,
    /// There is no block to refresh.
    Absent,
    /// The file cannot take the block.
    Conflict(Conflict),
}

/// Why `AGENTS.md` is left as it is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Conflict {
    /// A begin marker without its end, or more than one begin marker.
    MalformedBlock {
        path: PathBuf,
    },
    NotUtf8 {
        path: PathBuf,
    },
}

/// What [`shim`] found or did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Shim {
    Created,
    /// The file includes `AGENTS.md` already.
    Present,
    /// The file is there without the line that includes `AGENTS.md`.
    Conflict {
        path: PathBuf,
    },
}

/// Whether an `AGENTS.md` without the block gets it.
#[derive(Clone, Copy)]
enum Missing {
    Place,
    Leave,
}

/// Puts the current safety block into `<root>/AGENTS.md`: a block that is there is replaced where it stands,
/// and otherwise it goes first in the file, which is created when there is none.
pub fn place(writer: &dyn WriteFiles, root: &Path) -> io::Result<Placement> {
    write_block(writer, root, Missing::Place)
}

/// Replaces the safety block `<root>/AGENTS.md` holds with the current one. A file without the block is left
/// alone, whatever else it holds, and none is created.
pub fn refresh(writer: &dyn WriteFiles, root: &Path) -> io::Result<Placement> {
    write_block(writer, root, Missing::Leave)
}

/// Writes `<root>/CLAUDE.md` as the include of `AGENTS.md` when there is no such file.
pub fn shim(writer: &dyn WriteFiles, root: &Path) -> io::Result<Shim> {
    let path = root.join(SHIM_FILE);
    let mut outcome = Shim::Present;
    writer.update(&path, &mut |current| {
        let Some(bytes) = current else {
            outcome = Shim::Created;
            return Some(format!("{SHIM_LINE}\n").into_bytes());
        };
        if !bytes
            .split(|byte| *byte == b'\n')
            .any(|line| line == SHIM_LINE.as_bytes())
        {
            outcome = Shim::Conflict { path: path.clone() };
        }
        None
    })?;
    Ok(outcome)
}

fn write_block(writer: &dyn WriteFiles, root: &Path, missing: Missing) -> io::Result<Placement> {
    let path = root.join(AGENTS_FILE);
    let mut placement = Placement::Absent;
    writer.update(&path, &mut |current| {
        let (decided, replacement) = decide(&path, current, missing);
        placement = decided;
        replacement
    })?;
    Ok(placement)
}

/// What becomes of the `AGENTS.md` at `path` holding `current`, and its new bytes when it is to be written.
fn decide(path: &Path, current: Option<&[u8]>, missing: Missing) -> (Placement, Option<Vec<u8>>) {
    let markers = Markers::markdown(SAFETY_BLOCK);
    let body: Vec<&str> = eager_text().split_terminator('\n').collect();
    let block_in = |content| replace_or_insert(content, &markers, &body, Position::Prepend);
    let conflict = |conflict| (Placement::Conflict(conflict), None);

    // Asked of the bytes first, so that a refresh finds no conflict in a file that cannot hold the block.
    let begin = markers.begin.as_bytes();
    if matches!(missing, Missing::Leave)
        && !current.is_some_and(|bytes| bytes.windows(begin.len()).any(|part| part == begin))
    {
        return (Placement::Absent, None);
    }
    let Some(bytes) = current else {
        return (Placement::Created, Some(block_in("").into_bytes()));
    };
    let Ok(content) = std::str::from_utf8(bytes) else {
        return conflict(Conflict::NotUtf8 { path: path.into() });
    };
    match (shape(content, &markers), missing) {
        (Shape::Malformed, _) => conflict(Conflict::MalformedBlock { path: path.into() }),
        // The marker's text is in the file, but on no line of its own.
        (Shape::Absent, Missing::Leave) => (Placement::Absent, None),
        (Shape::Absent, Missing::Place) | (Shape::One, _) => {
            let replacement = block_in(content);
            if replacement == content {
                (Placement::Unchanged, None)
            } else {
                (Placement::Updated, Some(replacement.into_bytes()))
            }
        }
    }
}
