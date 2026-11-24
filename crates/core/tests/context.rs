//! `context`: one topic of the compiled guidance, printed without a workspace.

use winter_workspace_core::{CommandError, ContextError, Termination, eager_text};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

fn printed(args: &[&str]) -> String {
    let ended = InProcess::new(&[&["context"], args].concat()).run();
    assert!(
        matches!(ended.termination, Termination::Success),
        "{args:?}"
    );
    assert_eq!(ended.stderr, "", "{args:?}");
    ended.stdout
}

#[test]
fn no_topic_prints_the_title_of_the_root_a_blank_line_and_the_served_text() {
    let stdout = printed(&[]);

    let body = stdout
        .strip_prefix("Workspace guidance\n\n")
        .expect("the title of root, then a blank line");
    assert!(body.starts_with(eager_text()), "{body}");
    assert!(body.ends_with('\n') && !body.ends_with("\n\n"));
}

#[test]
fn the_topics_topic_prints_the_generated_listing() {
    let stdout = printed(&["topics"]);

    assert!(stdout.starts_with("Workspace topics\n\n"), "{stdout}");
    assert!(
        stdout.contains("- `winter-workspace context context` — "),
        "{stdout}"
    );
    assert!(
        stdout.contains("- `winter-workspace context topics` — "),
        "{stdout}"
    );
}

#[test]
fn the_context_topic_prints_only_itself() {
    let stdout = printed(&["context"]);

    assert!(stdout.starts_with("The context command\n\n"), "{stdout}");
    assert!(stdout.contains("winter:/workspace/<id>"), "{stdout}");
    assert!(!stdout.contains("Workspace safety"), "{stdout}");
}

#[test]
fn a_text_that_ends_with_a_line_break_is_followed_by_no_second_one() {
    let stdout = printed(&["safety"]);

    assert_eq!(
        stdout,
        format!("Workspace safety rules\n\n{}", eager_text())
    );
    assert!(!stdout.ends_with("\n\n"));
}

#[test]
fn json_is_one_document_whose_text_is_the_served_text_and_whose_next_is_the_references_in_order() {
    let document = InProcess::new(&["context", "safety", "--json"])
        .run()
        .document("safety");

    assert_eq!(document["id"], "safety");
    assert_eq!(document["title"], "Workspace safety rules");
    assert_eq!(document["text"], eager_text());
    assert_eq!(document["next"], serde_json::json!(["root", "topics"]));

    let root = InProcess::new(&["context", "--json"])
        .run()
        .document("root");
    assert_eq!(root["id"], "root");
    let next: Vec<&str> = root["next"]
        .as_array()
        .expect("an array")
        .iter()
        .map(|id| id.as_str().expect("a string"))
        .collect();
    assert_eq!(next[..2], ["topics", "context"]);
    let human = printed(&[]);
    assert_eq!(
        human,
        format!("Workspace guidance\n\n{}", root["text"].as_str().unwrap())
    );
}

#[test]
fn an_unknown_topic_ends_as_a_context_error_with_nothing_on_stdout() {
    for id in [
        "Context",
        "ROOT",
        "context/",
        "../x",
        "",
        "workspace/context",
    ] {
        for json in [false, true] {
            let mut args = vec!["context", id];
            if json {
                args.push("--json");
            }
            let ended = InProcess::new(&args).run();

            assert_eq!(ended.stdout, "", "{args:?}");
            assert_eq!(ended.termination.exit_code(), 1, "{args:?}");
            let Termination::Command(error) = ended.termination else {
                panic!("{args:?}: not a command error");
            };
            let CommandError::Context(ContextError::UnknownTopic { id: named }) = *error else {
                panic!("{args:?}: not a context error");
            };
            assert_eq!(named, id);
            let message =
                CommandError::Context(ContextError::UnknownTopic { id: named }).to_string();
            assert!(message.contains(&format!("{id:?}")), "{message}");
            assert!(
                message.contains("`winter-workspace context topics`"),
                "{message}"
            );
        }
    }
}

#[test]
fn a_workspace_whose_configuration_does_not_parse_changes_nothing_and_prints_the_same_text() {
    let outside = printed(&["safety"]);
    let tree = TempTree::new().expect("a tree");
    let root = WorkspaceBuilder::new(&tree)
        .config("this is = = not toml\n[")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let ended = InProcess::new(&["context", "safety"]).cwd(&root).run();

    assert!(matches!(ended.termination, Termination::Success));
    assert_eq!(ended.stdout, outside);
    assert_eq!(ended.stderr, "");
    assert_eq!(snapshot(&root), before);
}
