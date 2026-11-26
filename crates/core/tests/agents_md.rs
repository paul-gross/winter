//! The eager safety block of `AGENTS.md`, and the `CLAUDE.md` shim.

use std::fs;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use winter_workspace_core::{
    Conflict, OsFiles, Placement, SAFETY_BLOCK, Shim, eager_text, place, refresh, shim,
};
use winter_workspace_test_support::TempTree;

const BEGIN: &str = "<!-- >>> winter-workspace/safety (managed by winter) -->";
const END: &str = "<!-- <<< winter-workspace/safety -->";

type Entry = fn(&OsFiles, &Path) -> io::Result<Placement>;

const PLACE: Entry = |files, root| place(files, root);
const REFRESH: Entry = |files, root| refresh(files, root);

fn current_block() -> String {
    format!("{BEGIN}\n{}{END}\n", eager_text())
}

/// A tree whose workspace holds `name` with `content`.
fn holding(name: &str, content: &[u8]) -> (TempTree, PathBuf) {
    let tree = TempTree::new().expect("a tree");
    let path = tree.workspace_dir().join(name);
    fs::write(&path, content).expect("a file");
    (tree, path)
}

/// Runs `act` on a workspace whose `name` holds `content`, and asserts that the file was not written: its
/// bytes are the same, and it is the same file, which a replacement would not be.
fn untouched<T>(name: &str, content: &[u8], act: impl FnOnce(&Path) -> T) -> (T, PathBuf) {
    let (tree, path) = holding(name, content);
    let before = fs::metadata(&path).expect("the file").ino();

    let answer = act(&tree.workspace_dir());

    assert_eq!(fs::read(&path).expect("the file"), content);
    assert_eq!(fs::metadata(&path).expect("the file").ino(), before);
    (answer, path)
}

/// What `entry` answers over an `AGENTS.md` holding `content`, and what the file holds afterwards.
fn written(entry: Entry, content: &str) -> (Placement, String) {
    let (tree, path) = holding("AGENTS.md", content.as_bytes());
    let placement = entry(&OsFiles, &tree.workspace_dir()).expect("an update");
    (placement, fs::read_to_string(path).expect("the file"))
}

#[test]
fn the_block_is_named_and_its_body_is_the_eager_text_line_for_line() {
    let tree = TempTree::new().expect("a tree");

    place(&OsFiles, &tree.workspace_dir()).expect("an update");

    let text = fs::read_to_string(tree.workspace_dir().join("AGENTS.md")).expect("the file");
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(SAFETY_BLOCK, "winter-workspace/safety");
    assert_eq!(lines.first(), Some(&BEGIN));
    assert_eq!(lines.last(), Some(&END));
    assert_eq!(
        lines[1..lines.len() - 1],
        eager_text().lines().collect::<Vec<_>>()
    );
}

#[test]
fn place_in_an_empty_directory_writes_the_block_alone() {
    let tree = TempTree::new().expect("a tree");

    let placement = place(&OsFiles, &tree.workspace_dir()).expect("an update");

    assert_eq!(placement, Placement::Created);
    assert_eq!(
        fs::read_to_string(tree.workspace_dir().join("AGENTS.md")).expect("the file"),
        current_block()
    );
}

#[test]
fn place_prepends_the_block_and_one_blank_line_and_keeps_every_other_byte() {
    let own = "# Mine\r\n\n  indented \t\n\n\nlast, with no newline";

    let (placement, text) = written(PLACE, own);

    assert_eq!(placement, Placement::Updated);
    // The final newline is the one thing added after the user's text.
    assert_eq!(text, format!("{}\n{own}\n", current_block()));
}

#[test]
fn a_current_block_is_not_written() {
    let content = format!("above\n\n{}\nbelow\n", current_block());

    for entry in [PLACE, REFRESH] {
        let (placement, _) = untouched("AGENTS.md", content.as_bytes(), |root| {
            entry(&OsFiles, root).expect("an update")
        });

        assert_eq!(placement, Placement::Unchanged);
    }
}

#[test]
fn an_outdated_block_is_replaced_alone() {
    let content = format!("above\r\n\n{BEGIN}\nold rule\n{END}\n\n\nbelow\t\n");

    for entry in [PLACE, REFRESH] {
        let (placement, text) = written(entry, &content);

        assert_eq!(placement, Placement::Updated);
        assert_eq!(text, format!("above\r\n\n{}\n\nbelow\t\n", current_block()));
    }
}

#[test]
fn a_malformed_block_is_a_conflict_and_is_not_written() {
    let block = current_block();
    for content in [
        format!("above\n{BEGIN}\nrule\nbelow\n"),
        format!("{END}\n{BEGIN}\n"),
        format!("{block}\n{block}"),
    ] {
        for entry in [PLACE, REFRESH] {
            let (placement, path) = untouched("AGENTS.md", content.as_bytes(), |root| {
                entry(&OsFiles, root).expect("an update")
            });

            assert_eq!(
                placement,
                Placement::Conflict(Conflict::MalformedBlock { path }),
                "{content:?}"
            );
        }
    }
}

#[test]
fn content_that_is_not_utf_8_is_a_conflict_and_is_not_written() {
    let content = [b"\xff\n".as_slice(), current_block().as_bytes()].concat();

    for entry in [PLACE, REFRESH] {
        let (placement, path) = untouched("AGENTS.md", &content, |root| {
            entry(&OsFiles, root).expect("an update")
        });

        assert_eq!(placement, Placement::Conflict(Conflict::NotUtf8 { path }));
    }

    let (placement, path) = untouched("AGENTS.md", b"\xff\n", |root| {
        place(&OsFiles, root).expect("an update")
    });
    assert_eq!(placement, Placement::Conflict(Conflict::NotUtf8 { path }));
}

#[test]
fn refresh_creates_no_agents_file() {
    let tree = TempTree::new().expect("a tree");

    let placement = refresh(&OsFiles, &tree.workspace_dir()).expect("an update");

    assert_eq!(placement, Placement::Absent);
    assert!(!tree.workspace_dir().join("AGENTS.md").exists());
}

#[test]
fn refresh_leaves_a_file_without_the_begin_marker_line() {
    for content in [
        b"".as_slice(),
        b"# Mine\n",
        b"\xff not text\n",
        b"<!-- <<< winter-workspace/safety -->\n",
        b"The block starts at `<!-- >>> winter-workspace/safety (managed by winter) -->`.\n",
    ] {
        let (placement, _) = untouched("AGENTS.md", content, |root| {
            refresh(&OsFiles, root).expect("an update")
        });

        assert_eq!(placement, Placement::Absent, "{content:?}");
    }
}

#[test]
fn shim_writes_the_include_when_there_is_no_file() {
    let tree = TempTree::new().expect("a tree");

    let outcome = shim(&OsFiles, &tree.workspace_dir()).expect("an update");

    assert_eq!(outcome, Shim::Created);
    assert_eq!(
        fs::read(tree.workspace_dir().join("CLAUDE.md")).expect("the file"),
        b"@AGENTS.md\n"
    );
}

#[test]
fn shim_leaves_a_file_that_holds_the_include_line() {
    for content in [
        b"@AGENTS.md\n".as_slice(),
        b"@AGENTS.md",
        b"# Mine\n\n@AGENTS.md\nmore\n",
    ] {
        let (outcome, _) = untouched("CLAUDE.md", content, |root| {
            shim(&OsFiles, root).expect("an update")
        });

        assert_eq!(outcome, Shim::Present, "{content:?}");
    }
}

#[test]
fn shim_leaves_any_other_file_as_a_conflict() {
    for content in [
        b"".as_slice(),
        b"# Mine\n",
        b" @AGENTS.md\n",
        b"@AGENTS.md \n",
        b"See @AGENTS.md\n",
        b"\xff\n",
    ] {
        let (outcome, path) = untouched("CLAUDE.md", content, |root| {
            shim(&OsFiles, root).expect("an update")
        });

        assert_eq!(outcome, Shim::Conflict { path }, "{content:?}");
    }
}
