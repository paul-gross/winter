//! `resolve` against the host's git in fixture repositories.

use std::path::PathBuf;

use winter_workspace_core::{
    GitFailure, OsFiles, OsPrograms, PinPorts, ProcessEnv, RefKind, RepoMessage, resolve,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree};

#[path = "support/spans.rs"]
#[allow(dead_code)]
mod spans;
use spans::Spans;

struct Fixture {
    tree: TempTree,
    programs: OsPrograms,
    clone: PathBuf,
}

fn fixture() -> Fixture {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote(
        "origin",
        "main",
        &[CommitSpec {
            message: "one".to_owned(),
            files: vec![("a.txt".to_owned(), b"a\n".to_vec())],
            tag: Some("v1".to_owned()),
            ..CommitSpec::default()
        }],
    )
    .expect("a remote");
    let clone = tree.root().join("clone");
    git.clone_remote("origin", &clone).expect("a clone");
    let programs = OsPrograms::new(&ProcessEnv::from_vars(
        tree.env()
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned())),
    ));
    Fixture {
        tree,
        programs,
        clone,
    }
}

impl Fixture {
    fn out(&self, args: &[&str]) -> String {
        let git = Git::new(&self.tree).expect("git");
        git.run(&self.clone, args)
            .expect("git output")
            .trim()
            .to_owned()
    }

    fn resolve(&self, spans: &Spans, r#ref: &str) -> Result<(RefKind, String), GitFailure> {
        let ports = PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &self.programs,
            tracer: spans,
        };
        resolve(&ports, "api", &self.clone, r#ref).map(|found| (found.kind, found.commit))
    }
}

#[test]
fn a_name_that_is_a_branch_and_a_tag_resolves_as_the_branch() {
    let f = fixture();
    let head = f.out(&["rev-parse", "HEAD"]);
    f.out(&["tag", "main", &head]);

    let (kind, commit) = f.resolve(&Spans::default(), "main").expect("resolved");

    assert_eq!(kind, RefKind::Branch);
    assert_eq!(commit, head);
}

#[test]
fn a_lightweight_tag_resolves_to_its_commit() {
    let f = fixture();

    let (kind, commit) = f.resolve(&Spans::default(), "v1").expect("resolved");

    assert_eq!(kind, RefKind::Tag);
    assert_eq!(commit, f.out(&["rev-parse", "HEAD"]));
}

#[test]
fn an_annotated_tag_resolves_to_its_tag_object() {
    let f = fixture();
    f.out(&["tag", "-a", "v2", "-m", "release"]);

    let (kind, commit) = f.resolve(&Spans::default(), "v2").expect("resolved");

    assert_eq!(kind, RefKind::Tag);
    assert_eq!(commit, f.out(&["rev-parse", "refs/tags/v2"]));
    assert_ne!(commit, f.out(&["rev-parse", "HEAD"]));
}

#[test]
fn a_short_sha_resolves_to_the_full_commit() {
    let f = fixture();
    let head = f.out(&["rev-parse", "HEAD"]);

    let (kind, commit) = f.resolve(&Spans::default(), &head[..8]).expect("resolved");

    assert_eq!(kind, RefKind::Commit);
    assert_eq!(commit, head);
}

#[test]
fn an_unknown_name_is_unresolvable() {
    let f = fixture();

    let answer = f.resolve(&Spans::default(), "nowhere");

    match answer {
        Err(GitFailure::Repo(error)) => {
            assert!(matches!(error.message, RepoMessage::UnresolvableRef { .. }));
            assert_eq!(error.cwd, Some(f.clone.clone()));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_commit_resolution_records_one_rev_parse_span_with_the_repo_alone() {
    let f = fixture();
    let head = f.out(&["rev-parse", "HEAD"]);
    let spans = Spans::default();

    f.resolve(&spans, &head).expect("resolved");

    assert_eq!(
        *spans.0.borrow(),
        vec![(
            "git rev-parse".to_owned(),
            vec![("winter.repo".to_owned(), "api".to_owned())]
        )]
    );
}
