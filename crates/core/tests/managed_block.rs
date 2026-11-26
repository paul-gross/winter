//! The text functions of a managed block: replacing, inserting, stripping, listing, and classifying.

use winter_workspace_core::{Markers, Position, Shape, blocks, replace_or_insert, shape, strip};

const BEGIN: &str = "# >>> api (managed by winter)";
const END: &str = "# <<< api";

fn api() -> Markers {
    Markers::exclude("api")
}

fn put(content: &str, position: Position) -> String {
    replace_or_insert(content, &api(), &["/new/"], position)
}

#[test]
fn the_markers_of_each_file_kind() {
    assert_eq!(
        api(),
        Markers {
            begin: BEGIN.to_owned(),
            end: END.to_owned(),
        }
    );
    assert_eq!(
        Markers::markdown("winter-workspace/safety"),
        Markers {
            begin: "<!-- >>> winter-workspace/safety (managed by winter) -->".to_owned(),
            end: "<!-- <<< winter-workspace/safety -->".to_owned(),
        }
    );
}

#[test]
fn an_existing_block_is_replaced_in_place_and_the_lines_around_it_are_unchanged() {
    let content = format!("  above \r\n\n\n{BEGIN}\n/old/\n/older/\n{END}\n\nbelow\t\n");

    for position in [Position::Append, Position::Prepend] {
        assert_eq!(
            put(&content, position),
            format!("  above \r\n\n\n{BEGIN}\n/new/\n{END}\n\nbelow\t\n"),
            "{position:?}"
        );
    }
}

#[test]
fn an_absent_block_is_appended_after_one_blank_line() {
    let block = format!("{BEGIN}\n/new/\n{END}\n");

    assert_eq!(put("", Position::Append), block);
    assert_eq!(put("a", Position::Append), format!("a\n\n{block}"));
    // The final newline of the content is the blank line.
    assert_eq!(put("a\n", Position::Append), format!("a\n\n{block}"));
    assert_eq!(put("a\n  ", Position::Append), format!("a\n  \n{block}"));
}

#[test]
fn an_absent_block_is_prepended_before_one_blank_line() {
    let block = format!("{BEGIN}\n/new/\n{END}\n");

    assert_eq!(put("", Position::Prepend), block);
    assert_eq!(put("a\n", Position::Prepend), format!("{block}\na\n"));
    assert_eq!(put("a", Position::Prepend), format!("{block}\na\n"));
    assert_eq!(put("\na\n", Position::Prepend), format!("{block}\na\n"));
}

#[test]
fn a_begin_without_its_end_is_replaced_to_the_end_of_the_file() {
    let content = format!("above\n{BEGIN}\n/old/\nbelow\n");

    assert_eq!(
        put(&content, Position::Append),
        format!("above\n{BEGIN}\n/new/\n{END}\n")
    );
}

#[test]
fn an_end_before_the_begin_does_not_close_it() {
    let content = format!("{END}\n{BEGIN}\n/old/\n");

    assert_eq!(
        put(&content, Position::Append),
        format!("{END}\n{BEGIN}\n/new/\n{END}\n")
    );
}

#[test]
fn a_marker_matches_a_whole_line_alone() {
    let content = format!(" {BEGIN}\n{BEGIN} \n");

    assert_eq!(shape(&content, &api()), Shape::Absent);
    assert_eq!(strip(&content, &api()), content);
    assert_eq!(
        put(&content, Position::Append),
        format!("{content}\n{BEGIN}\n/new/\n{END}\n")
    );
}

#[test]
fn the_result_of_a_replacement_ends_with_a_newline() {
    let content = format!("{BEGIN}\n/old/\n{END}");

    assert_eq!(
        put(&content, Position::Append),
        format!("{BEGIN}\n/new/\n{END}\n")
    );
}

#[test]
fn strip_removes_the_block_and_the_one_blank_line_above_it() {
    let content = format!("above\n\n\n{BEGIN}\n/old/\n{END}\n\nbelow\n");

    assert_eq!(strip(&content, &api()), "above\n\n\nbelow\n");
    assert_eq!(strip(&format!("above\n{BEGIN}\n{END}"), &api()), "above\n");
    assert_eq!(strip(&format!("{BEGIN}\n{END}\n"), &api()), "");
}

#[test]
fn strip_takes_a_begin_without_its_end_to_the_end_of_the_file() {
    let content = format!("above\n\n{BEGIN}\n/old/\nbelow\n");

    assert_eq!(strip(&content, &api()), "above\n");
}

#[test]
fn strip_returns_content_without_the_begin_marker_unchanged() {
    for content in ["", "a", "a\n\n", "a\n\n# <<< api\n"] {
        assert_eq!(strip(content, &api()), content, "{content:?}");
    }
}

#[test]
fn blocks_are_listed_in_file_order_with_their_lines() {
    let content = "user\n\
        # >>> web (managed by winter)\n\
        /web/\n\
        .claude/skills/w-*\n\
        # <<< web\n\
        between\n\
        # >>>  (managed by winter)\n\
        # >>> winter-dir/projects (managed by winter)\n\
        # <<< winter-dir/projects\n\
        # >>> api (managed by winter)\n\
        /api/\n\
        # <<< api\n";

    assert_eq!(
        blocks(content),
        [
            listed("web", &["/web/", ".claude/skills/w-*"]),
            listed("winter-dir/projects", &[]),
            listed("api", &["/api/"]),
        ]
    );
    assert_eq!(blocks(""), []);
}

#[test]
fn a_listed_block_without_its_end_runs_to_the_end_of_the_file() {
    let content = format!("{BEGIN}\n/api/\n# >>> web (managed by winter)");

    assert_eq!(
        blocks(&content),
        [listed("api", &["/api/", "# >>> web (managed by winter)"])]
    );
}

#[test]
fn the_shape_of_a_block() {
    let other = Markers::exclude("web");
    let block = format!("{BEGIN}\n/api/\n{END}\n");
    let cases = [
        (String::new(), Shape::Absent),
        (format!("{END}\n"), Shape::Absent),
        (block.clone(), Shape::One),
        (format!("{BEGIN}\n{END}"), Shape::One),
        (format!("{BEGIN}\n/api/\n"), Shape::Malformed),
        (format!("{END}\n{BEGIN}\n"), Shape::Malformed),
        (BEGIN.to_owned(), Shape::Malformed),
        (format!("{block}{block}"), Shape::Malformed),
        (format!("{BEGIN}\n{block}"), Shape::Malformed),
    ];

    for (content, expected) in cases {
        assert_eq!(shape(&content, &api()), expected, "{content:?}");
        assert_eq!(shape(&content, &other), Shape::Absent, "{content:?}");
    }
}

fn listed(name: &str, lines: &[&str]) -> (String, Vec<String>) {
    (
        name.to_owned(),
        lines.iter().map(|line| (*line).to_owned()).collect(),
    )
}
