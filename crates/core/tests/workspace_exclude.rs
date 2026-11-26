//! The workspace exclude file: locating it, writing a block, and finalizing, against the host's git in fixture
//! repositories.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use winter_workspace_core::{
    AdoptExtensions, Call, Eligible, Exit, Finalized, Finished, GitFailure, GitOperation, Located,
    NoTrace, OsFiles, OsPrograms, PinPorts, ProcessEnv, ReadFiles, RepoMessage, RunPrograms,
    finalize, locate, locate_for_write, write_block,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree, snapshot};

#[path = "support/spans.rs"]
#[allow(dead_code)]
mod spans;
use spans::Spans;

const USER_LINE: &str = "*.swp  \n";
const CONFIG_BLOCK: &str =
    "# >>> winter-config (managed by winter)\n.winter/config/**/*.local.*\n# <<< winter-config\n";

/// The programs of the operating system, keeping the arguments of every call.
struct Recording {
    programs: OsPrograms,
    calls: Mutex<Vec<Vec<String>>>,
    /// The key whose `git config --get` fails in place of running.
    refused: Option<&'static str>,
}

impl RunPrograms for Recording {
    fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
        let args: Vec<String> = call
            .args
            .iter()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();
        let refused = self.refused.is_some_and(|key| {
            args.iter().any(|arg| arg == "--get") && args.last().is_some_and(|last| last == key)
        });
        self.calls.lock().expect("the record").push(args);
        if refused {
            return Ok(Finished {
                exit: Exit::Code(128),
                stdout: Vec::new(),
                stderr: b"refused\n".to_vec(),
            });
        }
        self.programs.run(call)
    }
}

impl Recording {
    fn taken(&self) -> Vec<Vec<String>> {
        std::mem::take(&mut self.calls.lock().expect("the record"))
    }
}

/// The `git config` calls among `calls` that are not reads.
fn config_writes(calls: &[Vec<String>]) -> Vec<&Vec<String>> {
    calls
        .iter()
        .filter(|args| args[0] == "config" && !args.iter().any(|arg| arg == "--get"))
        .collect()
}

struct Fixture {
    tree: TempTree,
    programs: Recording,
}

fn fixture() -> Fixture {
    let tree = TempTree::new().expect("an isolated tree");
    let programs = Recording {
        programs: OsPrograms::new(
            &ProcessEnv::from_vars(
                tree.env()
                    .variables()
                    .map(|(name, value)| (name.to_owned(), value.to_owned())),
            ),
            false,
        ),
        calls: Mutex::default(),
        refused: None,
    };
    Fixture { tree, programs }
}

impl Fixture {
    fn git(&self) -> Git<'_> {
        Git::new(&self.tree).expect("git")
    }

    fn out(&self, cwd: &Path, args: &[&str]) -> String {
        let answer = self.git().run(cwd, args).expect("git output");
        answer.trim_end_matches('\n').to_owned()
    }

    fn remote(&self) -> PathBuf {
        let spec = CommitSpec {
            message: "one".to_owned(),
            files: vec![("a.txt".to_owned(), b"a\n".to_vec())],
            ..CommitSpec::default()
        };
        self.git()
            .bare_remote("origin", "main", &[spec])
            .expect("a remote")
    }

    /// A plain clone at `<tree>/<name>`.
    fn clone_at(&self, name: &str) -> PathBuf {
        let path = self.tree.root().join(name);
        self.git()
            .clone_remote("origin", &path)
            .expect("a clone of the remote");
        path
    }

    fn plain(&self) -> PathBuf {
        self.remote();
        self.clone_at("clone")
    }

    fn worktree(&self, repo: &Path, name: &str) -> PathBuf {
        let path = self.tree.root().join(name);
        self.git()
            .add_worktree(repo, &path, name, "main")
            .expect("a worktree");
        path
    }

    /// `info/exclude` of the git directory of the checkout at `root`, resolved.
    fn exclude_of(&self, root: &Path) -> PathBuf {
        let directory = self.out(root, &["rev-parse", "--absolute-git-dir"]);
        OsFiles.resolve(Path::new(&directory)).join("info/exclude")
    }

    fn ports<'p>(&'p self) -> PinPorts<'p> {
        PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &self.programs,
            tracer: &NoTrace,
        }
    }

    fn locate(&self, root: &Path) -> Result<Located, GitFailure> {
        locate(&self.programs, &NoTrace, &OsFiles, root)
    }

    fn locate_for_write(&self, root: &Path) -> Result<Located, GitFailure> {
        locate_for_write(&self.programs, &NoTrace, &OsFiles, root)
    }

    fn write_block(&self, root: &Path, name: &str, lines: &[&str]) -> Option<PathBuf> {
        let lines: Vec<String> = lines.iter().map(|line| (*line).to_owned()).collect();
        write_block(&self.ports(), root, name, &lines).expect("a written block")
    }

    fn finalize(
        &self,
        root: &Path,
        mode: AdoptExtensions,
        eligible: &[Eligible],
        keep: &[&str],
    ) -> Finalized {
        let keep: Vec<String> = keep.iter().map(|name| (*name).to_owned()).collect();
        finalize(&self.ports(), root, mode, eligible, &keep).expect("a finalized file")
    }
}

fn eligible(name: &str, extra: &[&str]) -> Eligible {
    Eligible {
        name: name.to_owned(),
        relative: format!(".winter/ext/{name}"),
        extra: extra.iter().map(|line| (*line).to_owned()).collect(),
    }
}

/// Writes the exclude file before the subject does: a fixture clone has no `info` directory.
fn seed(file: &Path, content: impl AsRef<[u8]>) {
    std::fs::create_dir_all(file.parent().expect("a directory")).expect("a directory");
    std::fs::write(file, content).expect("an exclude file");
}

fn read(path: &Path) -> String {
    std::fs::read_to_string(path).expect("the exclude file")
}

fn block(name: &str, lines: &[&str]) -> String {
    let mut text = format!("# >>> {name} (managed by winter)\n");
    for line in lines {
        text.push_str(line);
        text.push('\n');
    }
    text.push_str(&format!("# <<< {name}\n"));
    text
}

fn standalone(name: &str) -> String {
    block(name, &[&format!("/.winter/ext/{name}/")])
}

fn updated(file: &Path, names: &[&str]) -> Finalized {
    Finalized::Updated {
        file: file.to_path_buf(),
        names: names.iter().map(|name| (*name).to_owned()).collect(),
    }
}

fn repo_failure(answer: Result<Located, GitFailure>) -> (GitOperation, String) {
    let Err(GitFailure::Repo(error)) = answer else {
        panic!("a repo error: {answer:?}");
    };
    let RepoMessage::GitFailure { operation, repo } = error.message else {
        panic!("a git failure: {:?}", error.message);
    };
    (operation, repo)
}

#[test]
fn the_file_of_a_plain_clone_is_under_its_git_directory() {
    let f = fixture();
    let root = f.plain();
    let file = OsFiles.resolve(&root.join(".git/info/exclude"));

    assert_eq!(
        f.locate(&root).expect("located"),
        Located::File(file.clone())
    );
    assert_eq!(
        f.locate_for_write(&root).expect("located"),
        Located::File(file)
    );
    assert_eq!(
        config_writes(&f.programs.taken()),
        Vec::<&Vec<String>>::new()
    );
}

#[test]
fn a_root_without_a_git_entry_is_no_repository_and_nothing_runs_or_is_written() {
    let f = fixture();
    f.remote();
    let above = f.clone_at("clone");
    let root = above.join("plain");
    std::fs::create_dir(&root).expect("a directory");
    let before = snapshot(f.tree.root());

    assert_eq!(f.locate(&root).expect("located"), Located::NotARepository);
    assert_eq!(
        f.locate_for_write(&root).expect("located"),
        Located::NotARepository
    );
    assert_eq!(
        f.write_block(&root, "winter-dir/projects", &["/projects/"]),
        None
    );
    assert_eq!(
        f.finalize(&root, AdoptExtensions::All, &[eligible("alpha", &[])], &[]),
        Finalized::NotARepository
    );

    assert_eq!(f.programs.taken(), Vec::<Vec<String>>::new());
    assert!(snapshot(f.tree.root()) == before, "the tree changed");
}

/// What `locate_for_write` leaves of the linked worktree `root` of the common directory `common`.
fn assert_configured(f: &Fixture, root: &Path, common: &Path) {
    let file = f.exclude_of(root);
    assert_eq!(
        f.locate_for_write(root).expect("located"),
        Located::File(file.clone())
    );
    assert_eq!(
        f.out(
            root,
            &[
                "config",
                "--type=bool",
                "--get",
                "extensions.worktreeConfig"
            ]
        ),
        "true"
    );
    assert_eq!(
        f.out(
            root,
            &["config", "--worktree", "--get", "core.excludesFile"]
        ),
        file.to_str().expect("a UTF-8 path")
    );
    let shared = common.join("config");
    let shared = shared.to_str().expect("a UTF-8 path");
    for key in ["core.bare", "core.worktree"] {
        let found = f
            .git()
            .run(root, &["config", "--file", shared, "--get", key]);
        assert!(found.is_err(), "{key} is still shared: {found:?}");
    }
}

fn moved(f: &Fixture, common: &Path, key: &str) -> String {
    let own = common.join("config.worktree");
    let own = own.to_str().expect("a UTF-8 path");
    f.out(common, &["config", "--file", own, "--get", key])
}

#[test]
fn a_linked_worktree_of_a_clone_is_given_settings_of_its_own() {
    let f = fixture();
    let main = f.plain();
    let main_text = main.to_str().expect("a UTF-8 path");
    f.out(&main, &["config", "core.worktree", main_text]);
    let linked = f.worktree(&main, "linked");
    let common = main.join(".git");

    assert_configured(&f, &linked, &common);

    assert_eq!(moved(&f, &common, "core.bare"), "false");
    assert_eq!(moved(&f, &common, "core.worktree"), main_text);
    assert_eq!(f.out(&main, &["status", "--porcelain"]), "");
}

#[test]
fn a_linked_worktree_of_a_bare_directory_is_given_settings_of_its_own() {
    let f = fixture();
    let bare = f.remote();
    let first = f.worktree(&bare, "first");
    let second = f.worktree(&bare, "second");

    assert_configured(&f, &first, &bare);

    assert_eq!(moved(&f, &bare, "core.bare"), "true");
    assert_eq!(f.out(&second, &["status", "--porcelain"]), "");
    assert_eq!(f.out(&bare, &["rev-parse", "--is-bare-repository"]), "true");
}

#[test]
fn a_block_of_a_linked_worktree_does_not_ignore_the_path_in_its_sibling() {
    let f = fixture();
    let main = f.plain();
    let linked = f.worktree(&main, "linked");

    let written = f.write_block(&linked, "junk", &["/junk/"]);

    assert_eq!(written, Some(f.exclude_of(&linked)));
    for root in [&main, &linked] {
        std::fs::create_dir(root.join("junk")).expect("a directory");
        std::fs::write(root.join("junk/file"), "x").expect("a file");
    }
    assert_eq!(f.out(&linked, &["status", "--porcelain"]), "");
    assert_eq!(f.out(&main, &["status", "--porcelain"]), "?? junk/");
}

#[test]
fn a_configured_worktree_is_written_no_configuration_again_and_locate_writes_none() {
    let f = fixture();
    let main = f.plain();
    let linked = f.worktree(&main, "linked");

    f.locate(&linked).expect("located");
    assert_eq!(
        config_writes(&f.programs.taken()),
        Vec::<&Vec<String>>::new()
    );
    f.locate_for_write(&linked).expect("located");
    assert!(!config_writes(&f.programs.taken()).is_empty());

    f.locate_for_write(&linked).expect("located");
    let calls = f.programs.taken();
    assert!(calls.iter().any(|args| args[0] == "config"), "{calls:?}");
    assert_eq!(config_writes(&calls), Vec::<&Vec<String>>::new());
    f.locate(&linked).expect("located");
    assert_eq!(
        config_writes(&f.programs.taken()),
        Vec::<&Vec<String>>::new()
    );
}

#[test]
fn each_linked_worktree_of_one_repository_is_given_an_exclude_file_of_its_own() {
    let f = fixture();
    let main = f.plain();
    let first = f.worktree(&main, "first");
    let second = f.worktree(&main, "second");
    f.locate_for_write(&first).expect("located");
    f.programs.taken();

    f.locate_for_write(&second).expect("located");

    let own = f.exclude_of(&second);
    let own = own.to_str().expect("a UTF-8 path");
    let write = ["config", "--worktree", "core.excludesFile", own].map(str::to_owned);
    assert_eq!(config_writes(&f.programs.taken()), [&write.to_vec()]);
    f.write_block(&first, "one", &["/one/"]);
    f.write_block(&second, "two", &["/two/"]);
    for root in [&first, &second] {
        for name in ["one", "two"] {
            std::fs::create_dir(root.join(name)).expect("a directory");
            std::fs::write(root.join(name).join("file"), "x").expect("a file");
        }
    }
    assert_eq!(f.out(&first, &["status", "--porcelain"]), "?? two/");
    assert_eq!(f.out(&second, &["status", "--porcelain"]), "?? one/");
}

#[test]
fn a_worktree_whose_excludes_file_is_any_other_text_is_given_its_own_again() {
    let f = fixture();
    let main = f.plain();
    let linked = f.worktree(&main, "linked");
    f.locate_for_write(&linked).expect("located");
    let file = f.exclude_of(&linked);
    let file = file.to_str().expect("a UTF-8 path");

    // git reads the last two as another file, though each has the components of the right one.
    for other in [
        "/elsewhere".to_owned(),
        format!("{file}/"),
        format!("{file}/."),
    ] {
        f.out(
            &linked,
            &["config", "--worktree", "core.excludesFile", &other],
        );

        f.locate_for_write(&linked).expect("located");

        let args = ["config", "--worktree", "--get", "core.excludesFile"];
        assert_eq!(f.out(&linked, &args), file, "{other}");
    }
}

#[test]
fn finalize_replaces_the_block_of_an_eligible_extension_that_changed_where_it_stands() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    let beta = eligible("beta", &[]);
    f.finalize(
        &root,
        AdoptExtensions::All,
        &[eligible("alpha", &["/old"]), beta.clone()],
        &[],
    );
    let mut alpha = eligible("alpha", &["/new"]);

    for relative in [".winter/ext/alpha", "elsewhere/alpha"] {
        alpha.relative = relative.to_owned();

        let answer = f.finalize(
            &root,
            AdoptExtensions::All,
            &[alpha.clone(), beta.clone()],
            &[],
        );

        assert_eq!(answer, updated(&file, &["alpha", "beta"]), "{relative}");
        let alpha = block("alpha", &[&format!("/{relative}/"), "/new"]);
        let beta = standalone("beta");
        assert_eq!(read(&file), format!("{CONFIG_BLOCK}\n{alpha}\n{beta}"));
    }
}

#[test]
fn finalize_strips_the_block_of_an_extension_that_is_no_longer_eligible() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    seed(&file, USER_LINE);
    f.write_block(&root, "winter-dir/projects", &["/projects/"]);
    let projects = block("winter-dir/projects", &["/projects/"]);
    let both = [eligible("beta", &["/two", "/one"]), eligible("alpha", &[])];
    let beta = block("beta", &["/.winter/ext/beta/", "/two", "/one"]);
    let alpha = standalone("alpha");

    let first = f.finalize(&root, AdoptExtensions::All, &both, &[]);

    assert_eq!(first, updated(&file, &["alpha", "beta"]));
    assert_eq!(
        read(&file),
        format!("{USER_LINE}\n{projects}\n{CONFIG_BLOCK}\n{beta}\n{alpha}")
    );
    assert_eq!(
        f.finalize(&root, AdoptExtensions::All, &both, &[]),
        Finalized::Unchanged
    );

    let one = f.finalize(&root, AdoptExtensions::Winter, &both[1..], &[]);

    assert_eq!(one, updated(&file, &["alpha"]));
    assert_eq!(
        read(&file),
        format!("{USER_LINE}\n{projects}\n{CONFIG_BLOCK}\n{alpha}")
    );

    let none = f.finalize(&root, AdoptExtensions::All, &[], &[]);

    assert_eq!(none, updated(&file, &[]));
    assert_eq!(
        read(&file),
        format!("{USER_LINE}\n{projects}\n{CONFIG_BLOCK}")
    );
}

#[test]
fn finalize_leaves_a_kept_block_as_it_is() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    seed(&file, "");
    f.write_block(&root, "held", &["/held/"]);
    f.write_block(&root, "gone", &["/gone/"]);

    let answer = f.finalize(&root, AdoptExtensions::All, &[], &["held"]);

    assert_eq!(answer, updated(&file, &[]));
    let held = block("held", &["/held/"]);
    assert_eq!(read(&file), format!("{held}\n{CONFIG_BLOCK}"));
}

#[test]
fn finalize_under_no_adoption_writes_the_config_block_alone() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    seed(&file, "");
    f.write_block(&root, "other", &["/other/"]);

    let answer = f.finalize(&root, AdoptExtensions::None, &[eligible("alpha", &[])], &[]);

    assert_eq!(answer, Finalized::Unchanged);
    let other = block("other", &["/other/"]);
    assert_eq!(read(&file), format!("{other}\n{CONFIG_BLOCK}"));
}

#[test]
fn finalize_that_only_adds_the_config_block_is_unchanged() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    seed(&file, USER_LINE);

    let answer = f.finalize(&root, AdoptExtensions::All, &[], &[]);

    assert_eq!(answer, Finalized::Unchanged);
    assert_eq!(read(&file), format!("{USER_LINE}\n{CONFIG_BLOCK}"));
}

#[test]
fn write_block_answers_the_file_once_and_none_when_the_block_holds_its_lines() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);

    let first = f.write_block(&root, "winter-dir/projects", &["/projects/"]);
    let written = snapshot(&root.join(".git/info"));
    let modified = std::fs::metadata(&file).and_then(|data| data.modified());
    let second = f.write_block(&root, "winter-dir/projects", &["/projects/"]);

    assert_eq!(first, Some(file.clone()));
    assert_eq!(second, None);
    assert!(snapshot(&root.join(".git/info")) == written);
    assert_eq!(
        std::fs::metadata(&file)
            .and_then(|data| data.modified())
            .expect("a time"),
        modified.expect("a time")
    );
}

#[test]
fn a_git_file_that_names_no_git_directory_fails_the_lookup() {
    let f = fixture();
    let root = f.tree.root().join("broken");
    std::fs::create_dir(&root).expect("a directory");
    let nowhere = f.tree.root().join("nowhere");
    std::fs::write(
        root.join(".git"),
        format!("gitdir: {}\n", nowhere.display()),
    )
    .expect("a git file");
    let before = snapshot(f.tree.root());

    let answer = f.locate(&root);

    assert_eq!(
        repo_failure(answer),
        (GitOperation::ExcludeLocate, "winter".to_owned())
    );
    assert!(snapshot(f.tree.root()) == before, "the tree changed");
}

#[test]
fn a_refused_configuration_write_is_a_config_failure() {
    let f = fixture();
    let main = f.plain();
    let linked = f.worktree(&main, "linked");
    std::fs::write(main.join(".git/config.lock"), "").expect("a lock");

    let answer = f.locate_for_write(&linked);

    assert_eq!(
        repo_failure(answer),
        (GitOperation::Config, "winter".to_owned())
    );
}

#[test]
fn a_configuration_read_that_fails_is_a_config_failure_and_not_an_unset_key() {
    for key in [
        "extensions.worktreeConfig",
        "core.bare",
        "core.worktree",
        "core.excludesFile",
    ] {
        let mut f = fixture();
        let main = f.plain();
        let linked = f.worktree(&main, "linked");
        f.programs.refused = Some(key);

        let answer = f.locate_for_write(&linked);

        assert_eq!(
            repo_failure(answer),
            (GitOperation::Config, "winter".to_owned()),
            "{key}"
        );
        let calls = f.programs.taken();
        let last = calls.last().expect("a call");
        assert_eq!(last.last().map(String::as_str), Some(key), "{calls:?}");
    }
}

#[test]
fn each_git_call_has_an_operation_span_of_the_repository_winter() {
    let f = fixture();
    let main = f.plain();
    let linked = f.worktree(&main, "linked");
    let spans = Spans::default();

    locate_for_write(&f.programs, &spans, &OsFiles, &linked).expect("located");

    let calls = f.programs.taken();
    let spans = spans.0.into_inner().expect("span mutex is not poisoned");
    let expected: Vec<_> = calls
        .iter()
        .map(|args| {
            (
                format!("git {}", args[0]),
                vec![("winter.repo".to_owned(), "winter".to_owned())],
            )
        })
        .collect();
    assert_eq!(spans, expected);
    for name in ["git rev-parse", "git config"] {
        assert!(spans.iter().any(|span| span.0 == name), "{name}");
    }
}

#[test]
fn a_file_that_is_not_utf8_is_refused_and_keeps_its_bytes() {
    let f = fixture();
    let root = f.plain();
    let file = f.exclude_of(&root);
    let bytes: &[u8] = b"user \xff\n";
    seed(&file, bytes);

    let written = write_block(&f.ports(), &root, "name", &["/a/".to_owned()]);
    let finalized = finalize(&f.ports(), &root, AdoptExtensions::All, &[], &[]);

    for failure in [written.map(drop), finalized.map(drop)] {
        let Err(GitFailure::Internal(error)) = failure else {
            panic!("an internal failure: {failure:?}");
        };
        let message = error.to_string();
        let expected = format!("the exclude file {} is not UTF-8", file.display());
        assert!(message.contains(&expected), "{message}");
    }
    assert_eq!(std::fs::read(&file).expect("the file"), bytes);
}
