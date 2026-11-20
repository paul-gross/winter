//! The checkout locations of a workspace: the worktrees of each environment present, the standalone
//! repositories, and the workspace itself, and on request the git counts of each.

mod probe;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::config_load;
use crate::config_model::ProjectRepository;
use crate::diagnostics::{self, Diagnostics, Record};
use crate::environments;
use crate::files::ReadFiles;
use crate::logging::Level;
use crate::programs::RunPrograms;
use crate::state_registry;
use crate::termination::{ConfigError, InternalError, RepoError};
use crate::workspace_files::location;

/// What a location is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LocationKind {
    Worktree,
    Standalone,
    Workspace,
}

/// One checkout location. Its path is absolute, and exists as a directory.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Location {
    pub kind: LocationKind,
    /// The environment of a worktree.
    pub env: Option<String>,
    /// The resolved project name of a worktree.
    pub repo: Option<String>,
    /// The resolved name of a standalone, or the base name of the root for the workspace.
    pub name: Option<String>,
    pub label: String,
    pub path: PathBuf,
    /// The git counts of the location: `Some` exactly when the counts were asked for.
    pub status: Option<Counts>,
}

/// The git counts of a location, each `None` when it was not counted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Counts {
    /// The commits of the checkout its base lacks.
    pub ahead: Option<u64>,
    /// The commits of the base the checkout lacks.
    pub behind: Option<u64>,
    /// The paths that are changed, staged, or untracked.
    pub dirty: Option<u64>,
}

impl Counts {
    /// The counts of a location that is not counted.
    const UNCOUNTED: Self = Self {
        ahead: None,
        behind: None,
        dirty: None,
    };
}

/// Why the locations were not listed.
#[derive(Debug, thiserror::Error)]
pub(crate) enum ListError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    /// A git call of a worktree probe failed.
    #[error(transparent)]
    Repo(Box<RepoError>),
    #[error(transparent)]
    Internal(#[from] InternalError),
}

impl From<probe::Failure> for ListError {
    fn from(failure: probe::Failure) -> Self {
        match failure {
            probe::Failure::Repo(error) => Self::Repo(error),
            probe::Failure::Internal(error) => Self::Internal(error),
        }
    }
}

/// The origin of the records this module writes.
const ORIGIN: &str = "locations";

/// The locations of the workspace of `start`: for each environment present, in order, a worktree for each
/// project entry in merged order (pinned entries and entries of one name included); then a location for each
/// standalone entry, at its `path` or else its name; then the workspace root, always.
///
/// A location other than the workspace is listed only when it is a directory, symbolic links followed. The
/// state file is read after the load. Paths are joined and never resolved.
///
/// With `with_status` every location carries its counts, and git is started through `programs`; without it
/// none carries any and no program is started. The worktrees of an environment are probed before the next
/// environment, for every project entry, whether or not its location exists; a worktree takes the counts of
/// the probe of its name, and of two entries with one name the later probe is the one both show. A git call
/// that fails is written as a record through `diagnostics`, every probe of that environment still runs, and
/// the first failure in project order then ends the listing: no later environment is probed. A standalone
/// is not counted. The workspace is probed last, against the upstream of its branch, and a git call of that
/// probe that fails is no failure and writes no record: the workspace then carries no count.
pub(crate) fn list(
    files: &dyn ReadFiles,
    programs: &dyn RunPrograms,
    diagnostics: &mut Diagnostics<'_>,
    start: &Path,
    with_status: bool,
) -> Result<Vec<Location>, ListError> {
    let workspace = config_load::load(files, start)?;
    let root = workspace.root;
    let config = workspace.config;
    let registry = state_registry::read_in_order(files, &root)?;
    let environments = environments::present(files, &root, &config, &registry)?;
    let uncounted = with_status.then_some(Counts::UNCOUNTED);

    let mut listed = Vec::new();
    for env in &environments {
        let counted = if with_status {
            let probed = probe_worktrees(
                files,
                programs,
                diagnostics,
                &root,
                env,
                &config.project_repositories,
            )?;
            Some(probed)
        } else {
            None
        };
        for project in &config.project_repositories {
            let name = &project.common.name;
            let path = location(&root, &Path::new(env).join(name));
            if files.is_dir(&path) {
                listed.push(Location {
                    kind: LocationKind::Worktree,
                    env: Some(env.clone()),
                    repo: Some(name.clone()),
                    name: None,
                    label: format!("{env}/{name}"),
                    path,
                    status: counted
                        .as_ref()
                        .and_then(|counted| counted.get(name.as_str()).copied()),
                });
            }
        }
    }
    for standalone in &config.standalone_repositories {
        let name = &standalone.common.name;
        let relative = if standalone.path.is_empty() {
            name
        } else {
            &standalone.path
        };
        let path = location(&root, Path::new(relative));
        if files.is_dir(&path) {
            listed.push(Location {
                kind: LocationKind::Standalone,
                env: None,
                repo: None,
                name: Some(name.clone()),
                label: name.clone(),
                path,
                status: uncounted,
            });
        }
    }
    let workspace_counts = if with_status {
        Some(probe::workspace(files, programs, &root)?)
    } else {
        None
    };
    listed.push(Location {
        kind: LocationKind::Workspace,
        env: None,
        repo: None,
        name: root
            .file_name()
            .map(|base| base.to_string_lossy().into_owned()),
        label: "<workspace>".to_owned(),
        path: root,
        status: workspace_counts,
    });
    Ok(listed)
}

/// The counts of the worktrees of `env`, by project name: one probe for each entry of `projects`, in order,
/// the counts of a later entry replacing those of an earlier one of the same name.
///
/// Every probe runs. Each git call that failed is written as a record when it is met, and the first failure
/// in the order of `projects` is the one returned.
fn probe_worktrees<'p>(
    files: &dyn ReadFiles,
    programs: &dyn RunPrograms,
    diagnostics: &mut Diagnostics<'_>,
    root: &Path,
    env: &str,
    projects: &'p [ProjectRepository],
) -> Result<BTreeMap<&'p str, Counts>, ListError> {
    let mut counted = BTreeMap::new();
    let mut first_failure = None;
    for project in projects {
        let name = project.common.name.as_str();
        let path = location(root, &Path::new(env).join(name));
        match probe::worktree(files, programs, &path, name, &project.common.main_branch) {
            Ok(counts) => {
                counted.insert(name, counts);
            }
            Err(failure) => {
                if let probe::Failure::Repo(error) = &failure {
                    diagnostics::record(
                        diagnostics.err,
                        diagnostics.log,
                        &Record {
                            level: Level::Error,
                            origin: ORIGIN,
                            message: &error.to_string(),
                            only_when_switched: true,
                        },
                    );
                }
                first_failure.get_or_insert(failure);
            }
        }
    }
    match first_failure {
        Some(failure) => Err(failure.into()),
        None => Ok(counted),
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::io;

    use super::*;
    use crate::files::MemoryFiles;
    use crate::logging::Log;
    use crate::programs::{Call, Exit, Finished};
    use crate::termination::{GitOperation, RepoMessage};

    const CONFIG: &str = "/ws/.winter/config.toml";
    const STATE: &str = "/ws/.winter/state.toml";

    /// The arguments and the working directory of one call.
    type Called = (Vec<String>, PathBuf);

    /// A program port that keeps each call and answers it as the test says.
    struct Scripted<A> {
        calls: RefCell<Vec<Called>>,
        answer: A,
    }

    impl<A: Fn(&[String], &Path) -> io::Result<Finished>> Scripted<A> {
        fn new(answer: A) -> Self {
            Self {
                calls: RefCell::new(Vec::new()),
                answer,
            }
        }

        fn calls(&self) -> Vec<Called> {
            self.calls.borrow().clone()
        }

        /// The subcommand of each call with the last part of its working directory.
        fn summary(&self) -> Vec<String> {
            self.calls()
                .iter()
                .map(|(args, cwd)| {
                    let below: Vec<_> = cwd.iter().skip(2).map(|p| p.to_string_lossy()).collect();
                    format!("{} {}", below.join("/"), args[0])
                })
                .collect()
        }
    }

    impl<A: Fn(&[String], &Path) -> io::Result<Finished>> RunPrograms for Scripted<A> {
        fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
            let args: Vec<String> = call
                .args
                .iter()
                .map(|arg| arg.to_string_lossy().into_owned())
                .collect();
            self.calls
                .borrow_mut()
                .push((args.clone(), call.cwd.to_path_buf()));
            (self.answer)(&args, call.cwd)
        }
    }

    fn ended(code: i32, stdout: &[u8], stderr: &[u8]) -> io::Result<Finished> {
        Ok(Finished {
            exit: Exit::Code(code),
            stdout: stdout.to_vec(),
            stderr: stderr.to_vec(),
        })
    }

    /// A git whose every working tree is clean and level with its main branch.
    fn clean(args: &[String], _: &Path) -> io::Result<Finished> {
        match args[0].as_str() {
            "rev-list" => ended(0, b"0\t0\n", b""),
            _ => ended(0, b"", b""),
        }
    }

    /// A port no listing may call.
    fn never() -> Scripted<impl Fn(&[String], &Path) -> io::Result<Finished>> {
        Scripted::new(|args: &[String], _: &Path| panic!("git was started: {args:?}"))
    }

    /// What a listing answered and what it wrote to stderr, under a log whose format is switched or not.
    fn listing(
        files: &MemoryFiles,
        programs: &dyn RunPrograms,
        with_status: bool,
        switched: bool,
    ) -> (Result<Vec<Location>, ListError>, String) {
        let mut err = Vec::new();
        let mut diagnostics = Diagnostics {
            err: &mut err,
            log: Log {
                threshold: Level::Warning,
                switched,
            },
        };
        let listed = list(
            files,
            programs,
            &mut diagnostics,
            Path::new("/ws"),
            with_status,
        );
        (listed, String::from_utf8(err).expect("UTF-8 on stderr"))
    }

    fn labels(files: &MemoryFiles) -> Vec<String> {
        listing(files, &never(), false, false)
            .0
            .expect("locations")
            .into_iter()
            .map(|location| location.label)
            .collect()
    }

    /// The label of a location with its counts: ahead, behind, and dirty.
    type Counted<'l> = (&'l str, Option<u64>, Option<u64>, Option<u64>);

    /// The counts of each location of a listing with its label.
    fn counts(listed: &[Location]) -> Vec<Counted<'_>> {
        listed
            .iter()
            .map(|location| {
                let status = location.status.expect("counts");
                (
                    location.label.as_str(),
                    status.ahead,
                    status.behind,
                    status.dirty,
                )
            })
            .collect()
    }

    /// A workspace with the projects `api` and `web`, each a repository in the environments `alpha` and
    /// `beta`, and the standalone `ctx`.
    fn two_environments() -> MemoryFiles {
        MemoryFiles::default()
            .with(
                CONFIG,
                b"[[project_repository]]\nname = \"api\"\n\n\
                  [[project_repository]]\nname = \"web\"\nmain_branch = \"dev\"\n\n\
                  [[standalone_repository]]\nname = \"ctx\"\n",
            )
            .with(STATE, b"[env_index]\nalpha = 1\nbeta = 2\n")
            .with("/ws/alpha/api/.git", b"gitdir: /nowhere\n")
            .with("/ws/alpha/web/.git", b"gitdir: /nowhere\n")
            .with("/ws/beta/api/.git", b"gitdir: /nowhere\n")
            .with("/ws/beta/web/.git", b"gitdir: /nowhere\n")
            .with("/ws/ctx/.git", b"gitdir: /nowhere\n")
    }

    #[test]
    fn a_workspace_with_nothing_lists_only_itself() {
        let files = MemoryFiles::default().with(CONFIG, b"");

        assert_eq!(labels(&files), ["<workspace>"]);
    }

    #[test]
    fn worktrees_come_first_then_standalones_then_the_workspace() {
        let files = MemoryFiles::default()
            .with(
                CONFIG,
                b"[[project_repository]]\nname = \"api\"\n\n[[project_repository]]\nname = \"web\"\n\n\
                  [[standalone_repository]]\nname = \"ctx\"\npath = \".winter/ext/context\"\n\n\
                  [[standalone_repository]]\nname = \"gone\"\n",
            )
            .dir("/ws/alpha/api")
            .dir("/ws/.winter/ext/context");

        assert_eq!(labels(&files), ["alpha/api", "ctx", "<workspace>"]);
    }

    #[test]
    fn an_alias_whose_project_entry_is_a_link_to_a_directory_is_listed() {
        let files = MemoryFiles::default()
            .with(CONFIG, b"[[project_repository]]\nname = \"api\"\n")
            .dir("/elsewhere")
            .link("/ws/alpha/api", "/elsewhere");

        assert_eq!(labels(&files), ["alpha/api", "<workspace>"]);
    }

    #[test]
    fn without_the_counts_no_program_is_started_and_no_location_carries_any() {
        let (listed, stderr) = listing(&two_environments(), &never(), false, true);

        let listed = listed.expect("locations");
        assert_eq!(listed.len(), 6);
        assert!(listed.iter().all(|location| location.status.is_none()));
        assert_eq!(stderr, "");
    }

    #[test]
    fn a_worktree_is_counted_by_one_status_and_one_rev_list_against_its_main_branch() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "status" if cwd.ends_with("alpha/api") => ended(0, b"? new.txt\0? other.txt\0", b""),
            "rev-list" if cwd.ends_with("alpha/api") => ended(0, b"2\t3\n", b""),
            _ => clean(args, cwd),
        });

        let (listed, stderr) = listing(&two_environments(), &programs, true, true);

        assert_eq!(
            counts(&listed.expect("locations")),
            [
                ("alpha/api", Some(3), Some(2), Some(2)),
                ("alpha/web", Some(0), Some(0), Some(0)),
                ("beta/api", Some(0), Some(0), Some(0)),
                ("beta/web", Some(0), Some(0), Some(0)),
                ("ctx", None, None, None),
                ("<workspace>", None, None, None),
            ]
        );
        assert_eq!(stderr, "");
        let strings = |args: &[&str]| -> Vec<String> { args.iter().map(|a| (*a).into()).collect() };
        assert_eq!(
            programs.calls()[..4],
            [
                (
                    strings(&[
                        "status",
                        "--porcelain=v2",
                        "--branch",
                        "--untracked-files=all",
                        "-z"
                    ]),
                    PathBuf::from("/ws/alpha/api")
                ),
                (
                    strings(&["rev-list", "--left-right", "--count", "origin/main...HEAD"]),
                    PathBuf::from("/ws/alpha/api")
                ),
                (
                    strings(&[
                        "status",
                        "--porcelain=v2",
                        "--branch",
                        "--untracked-files=all",
                        "-z"
                    ]),
                    PathBuf::from("/ws/alpha/web")
                ),
                (
                    strings(&["rev-list", "--left-right", "--count", "origin/dev...HEAD"]),
                    PathBuf::from("/ws/alpha/web")
                ),
            ]
        );
        assert_eq!(programs.calls().len(), 8, "no call for a standalone");
    }

    #[test]
    fn a_location_that_is_no_repository_counts_zero_and_starts_no_program() {
        let files = MemoryFiles::default()
            .with(
                CONFIG,
                b"[[project_repository]]\nname = \"api\"\n\n[[project_repository]]\nname = \"web\"\n",
            )
            .with("/ws/alpha/api/README.md", b"")
            .dir("/ws/alpha/web/.git");

        let (listed, _) = listing(&files, &never(), true, false);

        assert_eq!(
            counts(&listed.expect("locations")),
            [
                ("alpha/api", Some(0), Some(0), Some(0)),
                ("alpha/web", Some(0), Some(0), Some(0)),
                ("<workspace>", None, None, None),
            ]
        );
    }

    #[test]
    fn a_status_that_fails_ends_as_the_repo_error_of_that_call() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "status" if cwd.ends_with("alpha/api") => {
                ended(128, b"", b"fatal: not a git repository\n")
            }
            _ => clean(args, cwd),
        });

        let (listed, _) = listing(&two_environments(), &programs, true, false);

        let Err(ListError::Repo(error)) = listed else {
            panic!("{listed:?}");
        };
        assert_eq!(
            *error,
            RepoError {
                message: RepoMessage::GitFailure {
                    operation: GitOperation::StatusProbe,
                    repo: "api".to_owned(),
                },
                program: Some("git".to_owned()),
                subcommand: Some("status".to_owned()),
                args: vec![
                    "--porcelain=v2".to_owned(),
                    "--branch".to_owned(),
                    "--untracked-files=all".to_owned(),
                    "-z".to_owned(),
                ],
                cwd: Some(PathBuf::from("/ws/alpha/api")),
                exit: Some(128),
                stderr: Some("fatal: not a git repository".to_owned()),
            }
        );
    }

    #[test]
    fn a_program_ended_by_a_signal_is_reported_as_minus_its_number_and_without_a_stderr() {
        let programs = Scripted::new(|_: &[String], _: &Path| {
            Ok(Finished {
                exit: Exit::Signal(9),
                stdout: Vec::new(),
                stderr: b" \n".to_vec(),
            })
        });

        let (listed, _) = listing(&two_environments(), &programs, true, false);

        let Err(ListError::Repo(error)) = listed else {
            panic!("{listed:?}");
        };
        assert_eq!(error.exit, Some(-9));
        assert_eq!(error.stderr, None);
    }

    #[test]
    fn every_probe_of_the_environment_that_fails_runs_and_none_of_the_next() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "status" if cwd.ends_with("alpha/api") => ended(128, b"", b"fatal: api\n"),
            _ => clean(args, cwd),
        });

        let (listed, _) = listing(&two_environments(), &programs, true, false);

        assert!(matches!(listed, Err(ListError::Repo(_))), "{listed:?}");
        assert_eq!(
            programs.summary(),
            ["alpha/api status", "alpha/web status", "alpha/web rev-list"]
        );
    }

    #[test]
    fn the_first_failure_in_project_order_is_the_ending_and_each_is_a_record_when_switched() {
        let failing = |args: &[String], cwd: &Path| match args[0].as_str() {
            "status" if cwd.starts_with("/ws/alpha") => ended(128, b"", b"fatal\n"),
            _ => clean(args, cwd),
        };

        let (listed, stderr) = listing(&two_environments(), &Scripted::new(failing), true, true);

        let Err(ListError::Repo(error)) = listed else {
            panic!("{listed:?}");
        };
        assert_eq!(error.to_string(), "status probe failed for api");
        assert_eq!(
            stderr,
            "ERROR locations: status probe failed for api\n\
             ERROR locations: status probe failed for web\n"
        );

        let (_, stderr) = listing(&two_environments(), &Scripted::new(failing), true, false);
        assert_eq!(stderr, "", "the ending alone carries it");
    }

    #[test]
    fn a_program_that_cannot_be_started_is_an_internal_failure_and_no_record() {
        let programs = Scripted::new(|_: &[String], _: &Path| {
            Err(io::Error::new(io::ErrorKind::NotFound, "no git"))
        });

        let (listed, stderr) = listing(&two_environments(), &programs, true, true);

        let Err(ListError::Internal(error)) = listed else {
            panic!("{listed:?}");
        };
        assert_eq!(error.to_string(), "no git");
        assert_eq!(stderr, "");
    }

    #[test]
    fn a_rev_list_that_fails_without_the_main_branch_of_the_remote_counts_zero() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-list" => ended(128, b"", b"fatal: bad revision\n"),
            "rev-parse" => ended(1, b"", b""),
            _ => clean(args, cwd),
        });

        let (listed, stderr) = listing(&two_environments(), &programs, true, true);

        let listed = listed.expect("locations");
        assert_eq!(counts(&listed)[0], ("alpha/api", Some(0), Some(0), Some(0)));
        assert_eq!(stderr, "");
        assert_eq!(
            programs.calls()[2].0,
            ["rev-parse", "--verify", "--quiet", "origin/main"]
        );
    }

    #[test]
    fn a_rev_list_that_fails_with_the_main_branch_present_is_the_repo_error_of_the_rev_list() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-list" => ended(129, b"", b"usage: git rev-list\n"),
            "rev-parse" => ended(0, b"1234\n", b""),
            _ => clean(args, cwd),
        });

        let (listed, _) = listing(&two_environments(), &programs, true, false);

        let Err(ListError::Repo(error)) = listed else {
            panic!("{listed:?}");
        };
        assert_eq!(
            error.message,
            RepoMessage::GitFailure {
                operation: GitOperation::MainAheadBehindProbe,
                repo: "api".to_owned(),
            }
        );
        assert_eq!(error.subcommand.as_deref(), Some("rev-list"));
        assert_eq!(
            error.args,
            ["--left-right", "--count", "origin/main...HEAD"]
        );
        assert_eq!(error.exit, Some(129));
        assert_eq!(error.stderr.as_deref(), Some("usage: git rev-list"));
    }

    #[test]
    fn output_of_git_that_does_not_read_is_an_internal_failure() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-list" => ended(0, b"many\n", b""),
            _ => clean(args, cwd),
        });

        let (listed, _) = listing(&two_environments(), &programs, true, false);

        assert!(matches!(listed, Err(ListError::Internal(_))), "{listed:?}");
    }

    #[test]
    fn two_entries_of_one_name_both_show_the_counts_of_the_later_probe() {
        let files = MemoryFiles::default()
            .with(
                CONFIG,
                b"[[project_repository]]\nname = \"api\"\n\n\
                  [[project_repository]]\nname = \"api\"\nmain_branch = \"dev\"\n",
            )
            .with("/ws/alpha/api/.git", b"gitdir: /nowhere\n");
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-list" if args[3] == "origin/dev...HEAD" => ended(0, b"4\t7\n", b""),
            _ => clean(args, cwd),
        });

        let (listed, _) = listing(&files, &programs, true, false);

        assert_eq!(
            counts(&listed.expect("locations"))[..2],
            [
                ("alpha/api", Some(7), Some(4), Some(0)),
                ("alpha/api", Some(7), Some(4), Some(0)),
            ]
        );
        assert_eq!(programs.calls().len(), 4, "each entry is probed");
    }

    /// A workspace with no project whose root is a repository.
    fn root_repository() -> MemoryFiles {
        MemoryFiles::default()
            .with(CONFIG, b"")
            .with("/ws/.git", b"gitdir: /nowhere\n")
    }

    /// A git whose repository is on the branch `main`, which follows `origin/main`: two paths staged, two
    /// changed, one of them both, three files untracked, three commits ahead and one behind.
    fn following(args: &[String], _: &Path) -> io::Result<Finished> {
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        match args.as_slice() {
            ["symbolic-ref", ..] => ended(0, b"main\n", b""),
            ["rev-parse", ..] => ended(0, b"1234\n", b""),
            ["diff", "--cached", ..] => ended(0, b"both.txt\0new name.txt\0", b""),
            ["diff", ..] => ended(0, b"both.txt\0changed.txt\0", b""),
            ["ls-files", ..] => ended(0, b"new.txt\0dir/one\0dir/two\0", b""),
            ["config", "--get", "branch.main.remote"] => ended(0, b"origin\n", b""),
            ["config", "--get", "branch.main.merge"] => ended(0, b"refs/heads/main\n", b""),
            ["rev-list", "--count", "origin/main..HEAD"] => ended(0, b"3\n", b""),
            ["rev-list", "--count", "HEAD..origin/main"] => ended(0, b"1\n", b""),
            other => panic!("a call the probe does not make: {other:?}"),
        }
    }

    /// The calls of a port, each as one line.
    fn lines(calls: &[Called]) -> Vec<String> {
        calls.iter().map(|(args, _)| args.join(" ")).collect()
    }

    /// The counts of the workspace of a listing that succeeded and wrote nothing to stderr, under a log
    /// whose format is switched.
    fn workspace_counts(programs: &dyn RunPrograms) -> (Option<u64>, Option<u64>, Option<u64>) {
        let (listed, stderr) = listing(&root_repository(), programs, true, true);
        let listed = listed.expect("locations");
        assert_eq!(stderr, "");
        let [("<workspace>", ahead, behind, dirty)] = counts(&listed)[..] else {
            panic!("{listed:?}");
        };
        (ahead, behind, dirty)
    }

    #[test]
    fn the_workspace_is_counted_by_its_calls_in_order_against_the_upstream_of_its_branch() {
        let programs = Scripted::new(following);

        assert_eq!(workspace_counts(&programs), (Some(3), Some(1), Some(6)));
        assert_eq!(
            lines(&programs.calls()),
            [
                "symbolic-ref --quiet --short HEAD",
                "rev-parse --verify --quiet HEAD",
                "diff --cached --name-only -z -M HEAD",
                "diff --name-only -z",
                "ls-files --others --exclude-standard -z",
                "config --get branch.main.remote",
                "config --get branch.main.merge",
                "rev-list --count origin/main..HEAD",
                "rev-list --count HEAD..origin/main",
            ]
        );
        assert!(
            programs
                .calls()
                .iter()
                .all(|(_, cwd)| cwd == Path::new("/ws"))
        );
    }

    #[test]
    fn the_workspace_is_probed_after_every_worktree() {
        let files = two_environments().with("/ws/.git", b"gitdir: /nowhere\n");
        let programs = Scripted::new(|args: &[String], cwd: &Path| {
            if cwd == Path::new("/ws") {
                following(args, cwd)
            } else {
                clean(args, cwd)
            }
        });

        let (listed, _) = listing(&files, &programs, true, false);

        assert_eq!(
            counts(&listed.expect("locations"))[4..],
            [
                ("ctx", None, None, None),
                ("<workspace>", Some(3), Some(1), Some(6)),
            ]
        );
        let calls = programs.calls();
        assert_eq!(calls.len(), 17);
        assert!(calls[..8].iter().all(|(_, cwd)| cwd != Path::new("/ws")));
    }

    #[test]
    fn a_workspace_call_that_fails_gives_no_count_writes_no_record_and_ends_the_probe() {
        for failing in 0..9 {
            let made = RefCell::new(0);
            let programs = Scripted::new(|args: &[String], cwd: &Path| {
                let call = made.replace_with(|made| *made + 1);
                if call == failing {
                    ended(128, b"", b"fatal: the call fails\n")
                } else {
                    following(args, cwd)
                }
            });

            assert_eq!(
                workspace_counts(&programs),
                (None, None, None),
                "call {failing}"
            );
            assert_eq!(programs.calls().len(), failing + 1, "call {failing}");
        }
    }

    #[test]
    fn a_workspace_call_ended_by_a_signal_gives_no_count() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "ls-files" => Ok(Finished {
                exit: Exit::Signal(9),
                stdout: Vec::new(),
                stderr: Vec::new(),
            }),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (None, None, None));
    }

    #[test]
    fn a_detached_head_gives_the_workspace_no_count_after_one_call() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "symbolic-ref" => ended(1, b"", b""),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (None, None, None));
        assert_eq!(
            lines(&programs.calls()),
            ["symbolic-ref --quiet --short HEAD"]
        );
    }

    #[test]
    fn a_head_without_a_commit_skips_the_staged_call_and_stages_no_path() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-parse" => ended(1, b"", b""),
            "diff" if args[1] == "--cached" => panic!("the staged call was made"),
            "config" => ended(1, b"", b""),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (Some(0), Some(0), Some(5)));
        assert_eq!(
            lines(&programs.calls()),
            [
                "symbolic-ref --quiet --short HEAD",
                "rev-parse --verify --quiet HEAD",
                "diff --name-only -z",
                "ls-files --others --exclude-standard -z",
                "config --get branch.main.remote",
            ]
        );
    }

    #[test]
    fn a_branch_without_a_merge_setting_is_level_and_no_rev_list_is_made() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "config" if args[2] == "branch.main.merge" => ended(1, b"", b""),
            "rev-list" => panic!("rev-list was made"),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (Some(0), Some(0), Some(6)));
        assert_eq!(
            lines(&programs.calls())[5..],
            [
                "config --get branch.main.remote",
                "config --get branch.main.merge"
            ]
        );
    }

    #[test]
    fn a_branch_without_a_remote_setting_is_level_and_its_merge_setting_is_not_read() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "config" if args[2] == "branch.main.remote" => ended(1, b"", b""),
            "config" => panic!("the merge setting was read"),
            "rev-list" => panic!("rev-list was made"),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (Some(0), Some(0), Some(6)));
    }

    #[test]
    fn a_local_upstream_is_counted_against_an_operand_git_refuses_and_gives_no_count() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "symbolic-ref" => ended(0, b"topic\n", b""),
            "config" if args[2] == "branch.topic.remote" => ended(0, b".\n", b""),
            "config" if args[2] == "branch.topic.merge" => ended(0, b"refs/heads/main\n", b""),
            "rev-list" => ended(128, b"", b"fatal: ambiguous argument\n"),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (None, None, None));
        assert_eq!(
            lines(&programs.calls())[7..],
            ["rev-list --count ./main..HEAD"]
        );
    }

    #[test]
    fn a_merge_setting_outside_the_branch_namespace_is_the_operand_as_written() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "config" if args[2] == "branch.main.merge" => ended(0, b"refs/tags/v1\n", b""),
            "rev-list" => ended(0, b"0\n", b""),
            _ => following(args, cwd),
        });

        assert_eq!(workspace_counts(&programs), (Some(0), Some(0), Some(6)));
        assert_eq!(
            lines(&programs.calls())[7..],
            [
                "rev-list --count origin/refs/tags/v1..HEAD",
                "rev-list --count HEAD..origin/refs/tags/v1"
            ]
        );
    }

    #[test]
    fn a_workspace_count_that_does_not_read_is_an_internal_failure() {
        let programs = Scripted::new(|args: &[String], cwd: &Path| match args[0].as_str() {
            "rev-list" => ended(0, b"many\n", b""),
            _ => following(args, cwd),
        });

        let (listed, stderr) = listing(&root_repository(), &programs, true, true);

        assert!(matches!(listed, Err(ListError::Internal(_))), "{listed:?}");
        assert_eq!(stderr, "");
    }

    #[test]
    fn a_program_that_cannot_be_started_for_the_workspace_is_an_internal_failure() {
        let programs = Scripted::new(|_: &[String], _: &Path| {
            Err(io::Error::new(io::ErrorKind::NotFound, "no git"))
        });

        let (listed, _) = listing(&root_repository(), &programs, true, false);

        assert!(matches!(listed, Err(ListError::Internal(_))), "{listed:?}");
    }
}
