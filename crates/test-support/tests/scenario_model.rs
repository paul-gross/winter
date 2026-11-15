//! The scenario model: what a description parses to, what makes one malformed, which paths it confines, what
//! discovery reports, and what the description digest covers.

use std::path::{Path, PathBuf};

use winter_workspace_test_support::TempTree;
use winter_workspace_test_support::scenario::{
    Facility, FileMode, Malformed, Meta, Reference, Scenario, State, StdoutMode, discover,
};

/// The id of the scenario most tests write.
const ID: &str = "ws-index/basic";

/// The smallest description that parses.
const MINIMAL: &str =
    "summary = \"lists one environment\"\n\n[[step]]\nargs = [\"ws\", \"index\"]\n";

/// A description that uses every key of the format.
const EVERY_KEY: &str = r#"
summary = "one sentence: the behaviour this scenario pins"
needs = ["pty", "clock"]

[fixture]
workspace_git = true
gitignore = "target/\n"

[[fixture.remote]]
name = "alpha"
default_branch = "trunk"
[[fixture.remote.commit]]
message = "initial"
files = { "README.md" = "alpha\n", "src/lib.rs" = "" }
delete = ["old.txt"]
branch = "side"
tag = "v1"

[[fixture.checkout]]
remote = "alpha"
path = "projects/alpha"

[[fixture.worktree]]
checkout = "projects/alpha"
path = "feature-one/alpha"
branch = "feature-one"
start = "origin/main"

[[fixture.git]]
cwd = "feature-one/alpha"
args = ["commit", "--allow-empty", "-m", "local"]

[[step]]
args = ["ws", "index", "feature-one", "--json"]
cwd = "feature-one"
stdin = "stdin"
stdout = "json"
env = { WINTER_LOG_LEVEL = "debug" }

[compare]
message_text = ["/warnings/*"]
git_config_ignore = ["core.fsmonitor"]

[[compare.fan_out]]
types = ["progress", "result"]
key = "/repo"

[[compare.file]]
path = ".winter/state.toml"
mode = "toml"
reason = "the writer's key order is not function"

[[stub]]
name = "docker"
exit = 3
stdout = "done\n"
env = ["WINTER_ENV", "WINTER_WORKSPACE_ROOT"]

[deviation]
decision = "decision:example/git-failure-rendering"
"#;

/// A scenarios root inside `tree`, created empty.
fn scenarios_root(tree: &TempTree) -> PathBuf {
    let root = tree.root().join("tmp/scenarios");
    std::fs::create_dir_all(&root).expect("the scenarios root is created");
    root
}

/// Writes the scenario `id` with the description `description` under `root`, and answers its directory.
fn write_scenario(root: &Path, id: &str, description: &str) -> PathBuf {
    let dir = root.join(id);
    std::fs::create_dir_all(&dir).expect("the scenario directory is created");
    std::fs::write(dir.join("scenario.toml"), description).expect("the description is written");
    dir
}

/// Writes `content` to `path` of the directory `dir`, with the directories on the way.
fn write_file(dir: &Path, path: &str, content: &str) {
    let file = dir.join(path);
    std::fs::create_dir_all(file.parent().expect("a file has a parent"))
        .expect("the directories are created");
    std::fs::write(file, content).expect("the file is written");
}

/// Parses `description` as the scenario [`ID`] of a fresh tree.
fn parse(description: &str) -> Result<Scenario, Malformed> {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), ID, description);
    Scenario::load(&dir)
}

/// Asserts that `description` is malformed and that the error names the scenario [`ID`] and `key`.
#[track_caller]
fn assert_malformed(description: &str, key: &str) {
    let malformed = parse(description).expect_err("the description is malformed");
    assert_names(&malformed, ID, key);
}

/// Asserts that `malformed` names the scenario `id` and `key`, as fields and in its message.
#[track_caller]
fn assert_names(malformed: &Malformed, id: &str, key: &str) {
    assert_eq!(malformed.scenario, id);
    assert_eq!(malformed.key, key, "{malformed}");
    let message = malformed.to_string();
    assert!(message.contains(id), "{message}");
    assert!(message.contains(key), "{message}");
}

/// [`MINIMAL`] with `lines` in its one step.
fn step_with(lines: &str) -> String {
    format!("{MINIMAL}{lines}\n")
}

/// [`MINIMAL`] with `tables` before its step.
fn fixture_with(tables: &str) -> String {
    format!("summary = \"s\"\n\n{tables}\n\n[[step]]\nargs = [\"doctor\"]\n")
}

#[test]
fn a_description_with_every_key_parses_to_its_values() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), ID, EVERY_KEY);
    write_file(&dir, "stdin", "input\n");
    std::fs::create_dir(dir.join("expected")).expect("expected/ is created");

    let scenario = Scenario::load(&dir).expect("the description parses");

    assert_eq!(scenario.id, ID);
    assert_eq!(scenario.dir, dir);
    assert_eq!(
        scenario.summary,
        "one sentence: the behaviour this scenario pins"
    );
    assert_eq!(scenario.needs, [Facility::Pty, Facility::Clock]);

    let fixture = &scenario.fixture;
    assert!(fixture.workspace_git);
    assert_eq!(fixture.gitignore.as_deref(), Some("target/\n"));
    let [remote] = &fixture.remotes[..] else {
        panic!("one remote: {:?}", fixture.remotes)
    };
    assert_eq!(remote.name, "alpha");
    assert_eq!(remote.default_branch, "trunk");
    let [commit] = &remote.commits[..] else {
        panic!("one commit: {:?}", remote.commits)
    };
    assert_eq!(commit.message, "initial");
    assert_eq!(
        commit.files.iter().collect::<Vec<_>>(),
        [
            (&"README.md".to_owned(), &"alpha\n".to_owned()),
            (&"src/lib.rs".to_owned(), &String::new()),
        ]
    );
    assert_eq!(commit.delete, ["old.txt"]);
    assert_eq!(commit.branch.as_deref(), Some("side"));
    assert_eq!(commit.tag.as_deref(), Some("v1"));
    let [checkout] = &fixture.checkouts[..] else {
        panic!("one checkout: {:?}", fixture.checkouts)
    };
    assert_eq!(checkout.remote, "alpha");
    assert_eq!(checkout.path, "projects/alpha");
    let [worktree] = &fixture.worktrees[..] else {
        panic!("one worktree: {:?}", fixture.worktrees)
    };
    assert_eq!(worktree.checkout, "projects/alpha");
    assert_eq!(worktree.path, "feature-one/alpha");
    assert_eq!(worktree.branch, "feature-one");
    assert_eq!(worktree.start, "origin/main");
    let [call] = &fixture.git[..] else {
        panic!("one git call: {:?}", fixture.git)
    };
    assert_eq!(call.cwd, "feature-one/alpha");
    assert_eq!(call.args, ["commit", "--allow-empty", "-m", "local"]);

    let [step] = &scenario.steps[..] else {
        panic!("one step: {:?}", scenario.steps)
    };
    assert_eq!(step.args, ["ws", "index", "feature-one", "--json"]);
    assert_eq!(step.cwd, "feature-one");
    assert_eq!(step.stdin.as_deref(), Some("stdin"));
    assert_eq!(step.stdout, StdoutMode::Json);
    assert_eq!(
        step.env.iter().collect::<Vec<_>>(),
        [(&"WINTER_LOG_LEVEL".to_owned(), &"debug".to_owned())]
    );

    let compare = &scenario.compare;
    assert_eq!(compare.message_text, ["/warnings/*"]);
    assert_eq!(compare.git_config_ignore, ["core.fsmonitor"]);
    let [fan_out] = &compare.fan_out[..] else {
        panic!("one fan-out: {:?}", compare.fan_out)
    };
    assert_eq!(fan_out.types, ["progress", "result"]);
    assert_eq!(fan_out.key, "/repo");
    let [file] = &compare.files[..] else {
        panic!("one file rule: {:?}", compare.files)
    };
    assert_eq!(file.path, ".winter/state.toml");
    assert_eq!(file.mode, FileMode::Toml);
    assert_eq!(file.reason, "the writer's key order is not function");

    let [stub] = &scenario.stubs[..] else {
        panic!("one stub: {:?}", scenario.stubs)
    };
    assert_eq!(stub.name, "docker");
    assert_eq!(stub.exit, 3);
    assert_eq!(stub.stdout.as_deref(), Some("done\n"));
    assert_eq!(stub.env, ["WINTER_ENV", "WINTER_WORKSPACE_ROOT"]);

    assert_eq!(
        scenario.deviation.map(|deviation| deviation.decision),
        Some("decision:example/git-failure-rendering".to_owned())
    );
}

#[test]
fn a_minimal_description_parses_to_the_defaults() {
    let scenario = parse(MINIMAL).expect("the description parses");

    assert_eq!(scenario.needs, []);
    assert!(!scenario.fixture.workspace_git);
    assert_eq!(scenario.fixture.gitignore, None);
    assert!(scenario.fixture.remotes.is_empty());
    let [step] = &scenario.steps[..] else {
        panic!("one step: {:?}", scenario.steps)
    };
    assert_eq!(step.cwd, ".");
    assert_eq!(step.stdin, None);
    assert_eq!(step.stdout, StdoutMode::Text);
    assert!(step.env.is_empty());
    assert!(scenario.compare.files.is_empty());
    assert!(scenario.stubs.is_empty());
    assert_eq!(scenario.deviation, None);
}

#[test]
fn a_remote_without_a_default_branch_has_main() {
    let scenario = parse(&fixture_with("[[fixture.remote]]\nname = \"alpha\""))
        .expect("the description parses");

    assert_eq!(scenario.fixture.remotes[0].default_branch, "main");
}

#[test]
fn an_unknown_key_is_malformed_at_every_level() {
    let cases = [
        ("bogus = 1\n".to_owned() + MINIMAL, "bogus"),
        (fixture_with("[fixture]\nbogus = 1"), "bogus"),
        (
            fixture_with("[[fixture.remote]]\nname = \"a\"\nurl = \"x\""),
            "url",
        ),
        (
            fixture_with(
                "[[fixture.remote]]\nname = \"a\"\n[[fixture.remote.commit]]\nmessage = \"m\"\nauthor = \"x\"",
            ),
            "author",
        ),
        (
            fixture_with("[[fixture.checkout]]\nremote = \"a\"\npath = \"p\"\ndepth = \"1\""),
            "depth",
        ),
        (
            fixture_with(
                "[[fixture.worktree]]\ncheckout = \"p\"\npath = \"q\"\nbranch = \"b\"\nstart = \"s\"\nforce = true",
            ),
            "force",
        ),
        (
            fixture_with("[[fixture.git]]\ncwd = \".\"\nargs = [\"status\"]\nenv = {}"),
            "env",
        ),
        (step_with("timeout = 5"), "timeout"),
        (step_with("[compare]\nstdout = \"text\""), "stdout"),
        (
            step_with("[[compare.fan_out]]\ntypes = [\"a\"]\nkey = \"/k\"\norder = \"any\""),
            "order",
        ),
        (
            step_with(
                "[[compare.file]]\npath = \"a\"\nmode = \"bytes\"\nreason = \"r\"\nglob = true",
            ),
            "glob",
        ),
        (
            step_with("[[stub]]\nname = \"docker\"\nexit = 0\nstderr = \"\""),
            "stderr",
        ),
        (
            step_with("[deviation]\ndecision = \"d\"\nnote = \"n\""),
            "note",
        ),
    ];
    for (description, key) in cases {
        let malformed = parse(&description).expect_err("the description is malformed");
        assert_names(&malformed, ID, key);
    }
}

#[test]
fn a_stub_entry_is_validated_like_every_other_table() {
    assert_malformed(&step_with("[[stub]]\nname = \"docker\""), "exit");
    assert_malformed(
        &step_with("[[stub]]\nname = \"docker\"\nexit = 256"),
        "exit",
    );
    assert_malformed(
        &step_with("[[stub]]\nname = \"bin/docker\"\nexit = 0"),
        "name",
    );
    assert_malformed(
        &step_with("[[stub]]\nname = \"docker\"\nexit = 0\nenv = \"WINTER_ENV\""),
        "env",
    );
}

#[test]
fn an_unknown_facility_is_malformed() {
    let malformed = parse(&("needs = [\"pty\", \"telepathy\"]\n".to_owned() + MINIMAL))
        .expect_err("the description is malformed");

    assert_names(&malformed, ID, "needs");
    assert!(malformed.reason.contains("telepathy"), "{malformed}");
}

#[test]
fn an_invalid_scenario_name_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), "ws-index/Bad_Name", MINIMAL);

    let malformed = Scenario::load(&dir).expect_err("the name is invalid");

    assert_names(&malformed, "ws-index/Bad_Name", "Bad_Name");
}

#[test]
fn an_invalid_command_directory_name_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    let dir = write_scenario(&root, "ws_index/basic", MINIMAL);

    let malformed = Scenario::load(&dir).expect_err("the name is invalid");
    assert_names(&malformed, "ws_index/basic", "ws_index");

    let found = discover(&root).expect("the root is read");
    let [entry] = &found[..] else {
        panic!("one entry: {found:?}")
    };
    assert_eq!(entry.id, "ws_index");
    let State::Malformed(malformed) = &entry.state else {
        panic!("malformed: {entry:?}")
    };
    assert_names(malformed, "ws_index", "ws_index");
}

#[test]
fn a_description_without_a_step_is_malformed() {
    assert_malformed("summary = \"s\"\n", "step");
    assert_malformed("summary = \"s\"\nstep = []\n", "step");
}

#[test]
fn an_expectation_without_a_deviation_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), ID, MINIMAL);
    std::fs::create_dir(dir.join("expected")).expect("expected/ is created");

    let malformed = Scenario::load(&dir).expect_err("the description is malformed");

    assert_names(&malformed, ID, "expected");
}

#[test]
fn a_deviation_without_an_expectation_is_malformed() {
    assert_malformed(&step_with("[deviation]\ndecision = \"d\""), "deviation");
}

#[test]
fn a_description_that_is_absent_or_not_toml_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = scenarios_root(&tree).join(ID);
    std::fs::create_dir_all(&dir).expect("the scenario directory is created");

    let malformed = Scenario::load(&dir).expect_err("no description is there");
    assert_names(&malformed, ID, "scenario.toml");

    assert_malformed("summary = \n", "scenario.toml");
}

#[test]
fn a_missing_required_key_or_a_value_of_another_type_is_malformed() {
    assert_malformed("[[step]]\nargs = []\n", "summary");
    assert_malformed("summary = \"s\"\n[[step]]\ncwd = \".\"\n", "args");
    assert_malformed("summary = 1\n[[step]]\nargs = []\n", "summary");
    assert_malformed(&step_with("stdout = \"yaml\""), "stdout");
    assert_malformed(
        &step_with("[[compare.file]]\npath = \"a\"\nmode = \"fuzzy\"\nreason = \"r\""),
        "mode",
    );
}

#[test]
fn an_absolute_step_cwd_is_malformed() {
    assert_malformed(&step_with("cwd = \"/tmp/x\""), "cwd");
}

#[test]
fn a_step_cwd_of_the_parent_directory_is_malformed() {
    assert_malformed(&step_with("cwd = \"..\""), "cwd");
}

#[test]
fn a_step_cwd_beside_the_workspace_directory_is_malformed() {
    assert_malformed(&step_with("cwd = \"../ws\""), "cwd");
}

#[test]
fn a_step_cwd_that_goes_down_and_then_above_the_workspace_directory_is_malformed() {
    assert_malformed(&step_with("cwd = \"a/../../b\""), "cwd");
}

#[test]
fn a_checkout_path_that_leaves_the_workspace_directory_is_malformed() {
    assert_malformed(
        &fixture_with("[[fixture.checkout]]\nremote = \"alpha\"\npath = \"../outside\""),
        "path",
    );
}

#[test]
fn an_absolute_worktree_path_is_malformed() {
    assert_malformed(
        &fixture_with(
            "[[fixture.worktree]]\ncheckout = \"projects/alpha\"\npath = \"/tmp/x\"\nbranch = \"b\"\nstart = \"s\"",
        ),
        "path",
    );
}

#[test]
fn a_worktree_checkout_that_leaves_the_workspace_directory_is_malformed() {
    assert_malformed(
        &fixture_with(
            "[[fixture.worktree]]\ncheckout = \"../alpha\"\npath = \"x\"\nbranch = \"b\"\nstart = \"s\"",
        ),
        "checkout",
    );
}

#[test]
fn a_fixture_git_cwd_that_leaves_the_workspace_directory_is_malformed() {
    assert_malformed(
        &fixture_with("[[fixture.git]]\ncwd = \"../..\"\nargs = [\"status\"]"),
        "cwd",
    );
}

#[test]
fn a_commit_file_that_leaves_the_repository_is_malformed() {
    assert_malformed(
        &fixture_with(
            "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"m\"\nfiles = { \"../x\" = \"\" }",
        ),
        "files",
    );
}

#[test]
fn a_commit_deletion_that_leaves_the_repository_is_malformed() {
    assert_malformed(
        &fixture_with(
            "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"m\"\ndelete = [\"../x\"]",
        ),
        "delete",
    );
}

#[test]
fn a_remote_name_of_more_than_one_segment_is_malformed() {
    assert_malformed(&fixture_with("[[fixture.remote]]\nname = \"../x\""), "name");
}

#[test]
fn a_stdin_that_leaves_the_scenario_directory_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    write_file(&root, "ws-index/other/stdin", "input\n");
    let dir = write_scenario(&root, ID, &step_with("stdin = \"../other/stdin\""));

    let malformed = Scenario::load(&dir).expect_err("the description is malformed");

    assert_names(&malformed, ID, "stdin");
}

#[test]
fn a_stdin_that_links_to_a_file_outside_the_scenario_directory_is_malformed() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    let outside = tree.root().join("tmp/outside");
    std::fs::write(&outside, "input\n").expect("the outside file is written");
    let dir = write_scenario(&root, ID, &step_with("stdin = \"stdin\""));
    std::os::unix::fs::symlink(&outside, dir.join("stdin")).expect("the link is made");

    let malformed = Scenario::load(&dir).expect_err("the description is malformed");

    assert_names(&malformed, ID, "stdin");
}

#[test]
fn a_stdin_that_is_absent_or_a_directory_is_malformed() {
    assert_malformed(&step_with("stdin = \"stdin\""), "stdin");

    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), ID, &step_with("stdin = \"input\""));
    std::fs::create_dir(dir.join("input")).expect("the directory is created");
    let malformed = Scenario::load(&dir).expect_err("the description is malformed");
    assert_names(&malformed, ID, "stdin");
}

#[test]
fn fixture_git_arguments_that_start_with_a_global_option_are_malformed() {
    assert_malformed(
        &fixture_with("[[fixture.git]]\ncwd = \".\"\nargs = [\"-C\", \"x\", \"status\"]"),
        "args",
    );
}

#[test]
fn fixture_git_arguments_that_hold_a_git_directory_option_are_malformed() {
    assert_malformed(
        &fixture_with("[[fixture.git]]\ncwd = \".\"\nargs = [\"status\", \"--git-dir=/tmp/x\"]"),
        "args",
    );
}

#[test]
fn a_step_argument_that_starts_with_a_slash_is_malformed() {
    assert_malformed(
        "summary = \"s\"\n[[step]]\nargs = [\"ws\", \"index\", \"/tmp/x\"]\n",
        "args",
    );
}

#[test]
fn a_step_environment_value_that_starts_with_a_slash_is_malformed() {
    assert_malformed(&step_with("env = { WINTER_HOME = \"/tmp/x\" }"), "env");
}

#[test]
fn a_step_cwd_that_goes_up_and_down_again_parses() {
    let scenario = parse(&step_with("cwd = \"a/../b\"")).expect("the description parses");

    assert_eq!(scenario.steps[0].cwd, "a/../b");
}

#[test]
fn a_step_environment_value_under_the_root_token_parses() {
    let scenario = parse(&step_with("env = { WINTER_HOME = \"<ROOT>/ws/x\" }"))
        .expect("the description parses");

    assert_eq!(
        scenario.steps[0].env.get("WINTER_HOME").map(String::as_str),
        Some("<ROOT>/ws/x")
    );
}

#[test]
fn discovery_returns_the_scenario_ids_in_byte_order_and_ignores_the_reference() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    write_scenario(&root, "ws-index/b", MINIMAL);
    write_scenario(&root, "ws-index/a-b", MINIMAL);
    write_scenario(&root, "repo-remove/basic", MINIMAL);
    write_file(
        &root,
        "reference.toml",
        "commit = \"0123456789abcdef0123456789abcdef01234567\"\n",
    );

    let found = discover(&root).expect("the root is read");

    let ids: Vec<&str> = found.iter().map(|entry| entry.id.as_str()).collect();
    assert_eq!(ids, ["repo-remove/basic", "ws-index/a-b", "ws-index/b"]);
    for entry in &found {
        assert_eq!(entry.state, State::Unrecorded, "{}", entry.id);
        assert_eq!(entry.dir, root.join(&entry.id));
    }
}

#[test]
fn discovery_tells_recorded_unrecorded_and_malformed_apart() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    let recorded = write_scenario(&root, "doctor/recorded", MINIMAL);
    std::fs::create_dir(recorded.join("record")).expect("record/ is created");
    write_scenario(&root, "doctor/unrecorded", MINIMAL);
    write_scenario(&root, "doctor/broken", &step_with("cwd = \"..\""));
    write_scenario(&root, "doctor/Bad", MINIMAL);

    let found = discover(&root).expect("the root is read");

    let ids: Vec<&str> = found.iter().map(|entry| entry.id.as_str()).collect();
    assert_eq!(
        ids,
        [
            "doctor/Bad",
            "doctor/broken",
            "doctor/recorded",
            "doctor/unrecorded"
        ]
    );
    let State::Malformed(name) = &found[0].state else {
        panic!("malformed: {:?}", found[0])
    };
    assert_names(name, "doctor/Bad", "Bad");
    let State::Malformed(cwd) = &found[1].state else {
        panic!("malformed: {:?}", found[1])
    };
    assert_names(cwd, "doctor/broken", "cwd");
    assert_eq!(found[2].state, State::Recorded);
    assert_eq!(found[3].state, State::Unrecorded);
}

#[test]
fn discovery_reports_what_is_not_where_a_scenario_may_be() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    std::fs::create_dir(root.join("empty")).expect("the directory is created");
    write_file(&root, "notes.txt", "stray\n");

    let found = discover(&root).expect("the root is read");

    let ids: Vec<&str> = found.iter().map(|entry| entry.id.as_str()).collect();
    assert_eq!(ids, ["empty", "notes.txt"]);
    for entry in &found {
        let State::Malformed(malformed) = &entry.state else {
            panic!("malformed: {entry:?}")
        };
        assert_names(malformed, &entry.id, &entry.id);
    }
}

#[test]
fn discovery_reports_an_entry_of_a_command_directory_that_is_no_directory() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    write_scenario(&root, "ws-index/lists", MINIMAL);
    write_file(&root, "ws-index/notes.txt", "stray\n");
    std::os::unix::fs::symlink("lists", root.join("ws-index/linked")).expect("the link is made");
    write_file(&root, "only-a-file/scenario.toml", MINIMAL);

    let found = discover(&root).expect("the root is read");

    let ids: Vec<&str> = found.iter().map(|entry| entry.id.as_str()).collect();
    assert_eq!(
        ids,
        [
            "only-a-file",
            "only-a-file/scenario.toml",
            "ws-index/linked",
            "ws-index/lists",
            "ws-index/notes.txt"
        ]
    );
    assert_eq!(found[3].state, State::Unrecorded);
    for (entry, key) in [
        (&found[1], "scenario.toml"),
        (&found[2], "linked"),
        (&found[4], "notes.txt"),
    ] {
        let State::Malformed(malformed) = &entry.state else {
            panic!("malformed: {entry:?}")
        };
        assert_names(malformed, &entry.id, key);
        assert_eq!(entry.scenario, None);
    }
}

/// Writes a scenario with a step that reads `stdin`, a file under `files/`, and one under `dirty/`.
fn write_digested(root: &Path, id: &str) -> PathBuf {
    let dir = write_scenario(root, id, &step_with("stdin = \"stdin\""));
    write_file(&dir, "stdin", "input\n");
    write_file(&dir, "files/.winter/config.toml", "[projects]\n");
    write_file(&dir, "dirty/notes.txt", "untracked\n");
    dir
}

/// The description digest of the scenario directory `dir`.
fn digest(dir: &Path) -> String {
    Scenario::load(dir)
        .expect("the description parses")
        .description_digest()
        .expect("the scenario is read")
}

#[test]
fn the_digest_is_equal_for_two_copies_of_a_scenario() {
    let tree = TempTree::new().expect("a tree is created");
    let root = scenarios_root(&tree);
    let one = write_digested(&root, "ws-index/one");
    let other = write_digested(&root, "ws-index/other");

    let digest = digest(&one);

    assert_eq!(digest, self::digest(&other));
    assert_eq!(digest.len(), 40);
    assert!(digest.bytes().all(|byte| byte.is_ascii_hexdigit()));
}

#[test]
fn the_digest_is_the_sha1_of_the_description_and_each_file_in_order() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_digested(&scenarios_root(&tree), ID);

    // The SHA-1 of the description, then `files/.winter/config.toml`, `dirty/notes.txt`, and `stdin`, each as
    // its path, a NUL, its length, a NUL, and its content.
    assert_eq!(digest(&dir), "f08ef7d9a765273a62bb0223d06599fa383c2014");
}

#[test]
fn the_digest_takes_each_group_of_files_in_the_byte_order_of_their_paths() {
    let tree = TempTree::new().expect("a tree is created");
    let description = "summary = \"s\"\n\n[[step]]\nargs = [\"a\"]\nstdin = \"in/two\"\n\n\
                       [[step]]\nargs = [\"b\"]\nstdin = \"in/one\"\n\n\
                       [[step]]\nargs = [\"c\"]\nstdin = \"in/two\"\n";
    let dir = write_scenario(&scenarios_root(&tree), ID, description);
    for (path, content) in [
        ("in/two", "two\n"),
        ("in/one", "one\n"),
        ("dirty/y", "y\n"),
        ("dirty/x/w", "w\n"),
        ("files/c", ""),
        ("files/b", "b\n"),
        ("files/a/z", "z\n"),
        ("files/a.txt", "a\n"),
        ("files/a-b", "-\n"),
        ("files/B", "B\n"),
    ] {
        write_file(&dir, path, content);
    }

    // The SHA-1 of the description, then `files/B`, `files/a-b`, `files/a.txt`, `files/a/z`, `files/b`,
    // `files/c`, then `dirty/x/w`, `dirty/y`, then `in/one`, `in/two` once: each as its path, a NUL, its
    // length, a NUL, and its content.
    assert_eq!(digest(&dir), "92c700b426b6d1a89f786fc020c62de974794ee3");
}

#[test]
fn the_digest_changes_with_one_byte_of_a_file() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_digested(&scenarios_root(&tree), ID);
    let before = digest(&dir);

    write_file(&dir, "files/.winter/config.toml", "[projectz]\n");

    assert_ne!(digest(&dir), before);
}

#[test]
fn the_digest_changes_when_a_file_is_renamed() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_digested(&scenarios_root(&tree), ID);
    let before = digest(&dir);

    std::fs::rename(
        dir.join("files/.winter/config.toml"),
        dir.join("files/.winter/config.local.toml"),
    )
    .expect("the file is renamed");

    assert_ne!(digest(&dir), before);
}

#[test]
fn the_digest_changes_with_the_description_a_dirty_file_and_a_stdin_file() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_digested(&scenarios_root(&tree), ID);
    let mut seen = vec![digest(&dir)];

    write_file(&dir, "dirty/notes.txt", "untracked!\n");
    seen.push(digest(&dir));
    write_file(&dir, "stdin", "other\n");
    seen.push(digest(&dir));
    write_file(
        &dir,
        "scenario.toml",
        &step_with("stdin = \"stdin\"\nstdout = \"json\""),
    );
    seen.push(digest(&dir));

    seen.sort();
    seen.dedup();
    assert_eq!(seen.len(), 4);
}

#[test]
fn the_reference_of_the_checkout_is_the_pinned_commit() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scenarios");

    let reference = Reference::load(&root).expect("reference.toml is read");

    assert_eq!(reference.commit, "b57b020e99e10a04d8bb621d0b03ddb2c983a090");
    assert_eq!(reference.commit.len(), 40);
}

#[test]
fn a_reference_with_another_key_or_no_commit_id_is_refused() {
    let unknown =
        Reference::parse("commit = \"b57b020e99e10a04d8bb621d0b03ddb2c983a090\"\nbranch = \"m\"\n")
            .expect_err("the key is unknown");
    assert_eq!(unknown.key, "branch");

    let short = Reference::parse("commit = \"b57b020\"\n").expect_err("the id is short");
    assert_eq!(short.key, "commit");

    let absent = Reference::parse("").expect_err("the key is required");
    assert_eq!(absent.key, "commit");
}

/// What a record's `meta.toml` holds.
fn meta() -> Meta {
    Meta {
        reference_commit: "b57b020e99e10a04d8bb621d0b03ddb2c983a090".to_owned(),
        git_version: "2.47.1".to_owned(),
        reference_runtime: "winter 1.0 \"quoted\"".to_owned(),
        environment: "f9432b7bd6bfdb84a8badef0f46cffad82b76a81".to_owned(),
        description: "0123456789abcdef0123456789abcdef01234567".to_owned(),
    }
}

#[test]
fn a_meta_file_parses_to_its_values_and_is_written_back() {
    let text = "format = 1\n\
                reference_commit = \"b57b020e99e10a04d8bb621d0b03ddb2c983a090\"\n\
                git_version = \"2.47.1\"\n\
                reference_runtime = 'winter 1.0 \"quoted\"'\n\
                environment = \"f9432b7bd6bfdb84a8badef0f46cffad82b76a81\"\n\
                description = \"0123456789abcdef0123456789abcdef01234567\"\n";

    assert_eq!(Meta::parse(text), Ok(meta()));
    assert_eq!(Meta::parse(&meta().to_toml()), Ok(meta()));
}

#[test]
fn a_meta_file_is_read_from_the_record_of_a_scenario() {
    let tree = TempTree::new().expect("a tree is created");
    let dir = write_scenario(&scenarios_root(&tree), ID, MINIMAL);
    write_file(&dir, "record/meta.toml", &meta().to_toml());

    assert_eq!(Meta::load(&dir).expect("meta.toml is read"), meta());
}

#[test]
fn a_meta_file_of_another_format_or_with_a_bad_key_is_refused() {
    let written = meta().to_toml();

    let format = Meta::parse(&written.replace("format = 1", "format = 2"))
        .expect_err("the format is unknown");
    assert_eq!(format.key, "format");

    let unknown = Meta::parse(&format!("{written}host = \"h\"\n")).expect_err("the key is unknown");
    assert_eq!(unknown.key, "host");

    let digest = Meta::parse(&written.replace("f9432b7b", "F9432B7B"))
        .expect_err("the digest is not lowercase hexadecimal");
    assert_eq!(digest.key, "environment");

    let absent = Meta::parse(&written.replace("git_version = \"2.47.1\"\n", ""))
        .expect_err("the key is required");
    assert_eq!(absent.key, "git_version");
}
