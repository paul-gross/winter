//! The git counts of a checkout location: the rule that tells a repository from any other directory, and the
//! probe of a worktree.

use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path, PathBuf};

use super::Counts;
use crate::files::ReadFiles;
use crate::programs::{Call, Exit, Finished, Program, RunPrograms};
use crate::termination::{GitOperation, InternalError, RepoError, RepoMessage};

/// Why a probe gave no counts.
#[derive(Debug)]
pub(super) enum Failure {
    /// A git call failed.
    Repo(Box<RepoError>),
    /// Git could not be started, or wrote what does not read as the result of the call.
    Internal(InternalError),
}

/// What git wrote on a call that succeeded, when it does not read as the result of that call.
#[derive(Debug, thiserror::Error)]
#[error("git {subcommand} wrote what does not read as its result: {output:?}")]
struct UnreadOutput {
    subcommand: &'static str,
    output: String,
}

impl UnreadOutput {
    fn failure(subcommand: &'static str, output: &[u8]) -> Failure {
        Failure::Internal(InternalError::unexpected(Self {
            subcommand,
            output: String::from_utf8_lossy(output).into_owned(),
        }))
    }
}

/// How many files with a `gitdir: ` line are read one after the other before the last is taken to name
/// nothing.
const GITDIR_FILES_READ: usize = 40;

/// Whether `location` is a repository, with symbolic links followed and no directory above it searched.
///
/// It is one when it is a git directory itself, when its `.git` is one, when its `.git` is a file that
/// starts `gitdir: ` and names such a directory or another such file, and when its `.git` is a regular file
/// whose every line reads `<key>: <value>` with one key `gitdir`, whatever that names.
pub(super) fn is_repository(files: &dyn ReadFiles, location: &Path) -> bool {
    let dot_git = location.join(".git");
    is_git_directory(files, location)
        || leads_to_a_git_directory(files, &dot_git, GITDIR_FILES_READ)
        || names_a_gitdir(files, &dot_git)
}

/// Whether `directory` holds the directories `objects` and `refs`, and `HEAD` as a file or as a symbolic
/// link whose target text starts with `refs`.
fn is_git_directory(files: &dyn ReadFiles, directory: &Path) -> bool {
    let head = directory.join("HEAD");
    files.is_dir(directory)
        && files.is_dir(&directory.join("objects"))
        && files.is_dir(&directory.join("refs"))
        && (files.is_file(&head)
            || matches!(
                files.read_link(&head),
                Ok(Some(target)) if target.as_os_str().as_bytes().starts_with(b"refs")
            ))
}

/// Whether `path` is a git directory, or a file that starts `gitdir: ` whose path, read from the directory
/// of the file, is in turn such a directory or such a file. At most `files_left` files are read.
fn leads_to_a_git_directory(files: &dyn ReadFiles, path: &Path, files_left: usize) -> bool {
    if is_git_directory(files, path) {
        return true;
    }
    if files_left == 0 {
        return false;
    }
    let Ok(Some(content)) = files.read(path) else {
        return false;
    };
    let Ok(content) = String::from_utf8(content) else {
        return false;
    };
    let Some(named) = content.trim_end().strip_prefix("gitdir: ") else {
        return false;
    };
    let named = Path::new(named);
    let next = match path.parent() {
        Some(above) if named.is_relative() => without_dot_parts(&above.join(named)),
        _ => named.to_path_buf(),
    };
    leads_to_a_git_directory(files, &next, files_left - 1)
}

/// `path` with each `.` part dropped and each `..` part taken with the part before it, as text and with no
/// link followed.
fn without_dot_parts(path: &Path) -> PathBuf {
    let mut kept = PathBuf::new();
    for part in path.components() {
        match part {
            Component::CurDir => {}
            Component::ParentDir => {
                if matches!(kept.components().next_back(), Some(Component::Normal(_))) {
                    kept.pop();
                } else if !kept.has_root() {
                    kept.push("..");
                }
            }
            other => kept.push(other),
        }
    }
    kept
}

/// Whether `path` is a regular file whose every line reads `<key>: <value>` and one of whose keys is
/// `gitdir`.
fn names_a_gitdir(files: &dyn ReadFiles, path: &Path) -> bool {
    if !files.is_file(path) {
        return false;
    }
    let Ok(Some(content)) = files.read(path) else {
        return false;
    };
    let Ok(content) = String::from_utf8(content) else {
        return false;
    };
    let mut named = false;
    for line in content.lines() {
        let parts: Vec<&str> = line.trim().split(": ").collect();
        let [key, _] = parts.as_slice() else {
            return false;
        };
        named |= *key == "gitdir";
    }
    named
}

/// The counts of the worktree at `location` of the repository `name`, against the branch `main` of its
/// remote `origin`.
///
/// A location that is no repository has the counts 0, 0, 0, and git is not started for it. Otherwise one
/// `git status` gives the dirty count and one `git rev-list` the commits behind and ahead. When that
/// `rev-list` fails and `origin/<main>` does not resolve, the worktree is neither ahead nor behind.
pub(super) fn worktree(
    files: &dyn ReadFiles,
    programs: &dyn RunPrograms,
    location: &Path,
    name: &str,
    main: &str,
) -> Result<Counts, Failure> {
    if !is_repository(files, location) {
        return Ok(Counts {
            ahead: Some(0),
            behind: Some(0),
            dirty: Some(0),
        });
    }

    let status = [
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=all",
        "-z",
    ];
    let finished = git(programs, location, &status)?;
    if finished.exit != Exit::Code(0) {
        return Err(failed(
            GitOperation::StatusProbe,
            name,
            location,
            &status,
            &finished,
        ));
    }
    let dirty =
        dirty(&finished.stdout).ok_or_else(|| UnreadOutput::failure("status", &finished.stdout))?;

    let main_ref = format!("origin/{main}");
    let range = format!("{main_ref}...HEAD");
    let rev_list = ["rev-list", "--left-right", "--count", &range];
    let counted = git(programs, location, &rev_list)?;
    let (behind, ahead) = if counted.exit == Exit::Code(0) {
        behind_and_ahead(&counted.stdout)
            .ok_or_else(|| UnreadOutput::failure("rev-list", &counted.stdout))?
    } else {
        let resolved = git(
            programs,
            location,
            &["rev-parse", "--verify", "--quiet", &main_ref],
        )?;
        if resolved.exit == Exit::Code(0) {
            return Err(failed(
                GitOperation::MainAheadBehindProbe,
                name,
                location,
                &rev_list,
                &counted,
            ));
        }
        (0, 0)
    };

    Ok(Counts {
        ahead: Some(ahead),
        behind: Some(behind),
        dirty: Some(dirty),
    })
}

/// Runs git with `args` in `cwd`. A git that cannot be started is an internal failure.
fn git(programs: &dyn RunPrograms, cwd: &Path, args: &[&str]) -> Result<Finished, Failure> {
    let args: Vec<&OsStr> = args.iter().map(OsStr::new).collect();
    programs
        .run(&Call {
            program: Program::Git,
            args: &args,
            cwd,
        })
        .map_err(|error| Failure::Internal(InternalError::unexpected(error)))
}

/// The repo error of the git call `args` in `cwd` that ended as `finished`, made for `operation` on the
/// repository `name`.
fn failed(
    operation: GitOperation,
    name: &str,
    cwd: &Path,
    args: &[&str],
    finished: &Finished,
) -> Failure {
    let stderr = String::from_utf8_lossy(&finished.stderr);
    let stderr = stderr.trim();
    Failure::Repo(Box::new(RepoError {
        message: RepoMessage::GitFailure {
            operation,
            repo: name.to_owned(),
        },
        program: Some(Program::Git.name().to_owned()),
        subcommand: args.first().map(|subcommand| (*subcommand).to_owned()),
        args: args.iter().skip(1).map(|arg| (*arg).to_owned()).collect(),
        cwd: Some(cwd.to_path_buf()),
        exit: Some(finished.exit.number()),
        stderr: (!stderr.is_empty()).then(|| stderr.to_owned()),
    }))
}

/// The dirty count of what `git status --porcelain=v2 -z` wrote, or `None` when a record of it does not
/// read: the number of distinct paths of the changed and the unmerged entries, a renamed entry counted by
/// its new path, plus the number of untracked files. Headers and ignored files count nothing.
fn dirty(output: &[u8]) -> Option<u64> {
    let mut records = output
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty());
    let mut changed: BTreeSet<&[u8]> = BTreeSet::new();
    let mut untracked: u64 = 0;
    while let Some(record) = records.next() {
        match record[0] {
            b'?' => untracked += 1,
            b'1' => {
                if is_changed(record)? {
                    changed.insert(field(record, 8)?);
                }
            }
            b'2' => {
                // The path the entry was renamed or copied from is a record of its own.
                records.next()?;
                if is_changed(record)? {
                    changed.insert(field(record, 9)?);
                }
            }
            b'u' => {
                changed.insert(field(record, 10)?);
            }
            _ => {}
        }
    }
    u64::try_from(changed.len()).ok()?.checked_add(untracked)
}

/// Field `index` of a record whose fields are separated by one space, the last of them the rest of the
/// record: a path, which may hold spaces.
fn field(record: &[u8], index: usize) -> Option<&[u8]> {
    record.splitn(index + 1, |byte| *byte == b' ').nth(index)
}

/// Whether the entry of `record` differs in the index or in the working tree: its second field holds a
/// state for each, and `.` is the state of no change.
fn is_changed(record: &[u8]) -> Option<bool> {
    match record.split(|byte| *byte == b' ').nth(1)? {
        [index, tree] => Some(*index != b'.' || *tree != b'.'),
        _ => None,
    }
}

/// The two counts `git rev-list --left-right --count <main>...HEAD` wrote: the commits behind, then the
/// commits ahead. `None` when the output is not two numbers.
fn behind_and_ahead(output: &[u8]) -> Option<(u64, u64)> {
    let text = std::str::from_utf8(output).ok()?;
    let mut counts = text.split_whitespace().map(str::parse::<u64>);
    match (counts.next(), counts.next(), counts.next()) {
        (Some(Ok(behind)), Some(Ok(ahead)), None) => Some((behind, ahead)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::MemoryFiles;

    fn repository(files: &MemoryFiles) -> bool {
        is_repository(files, Path::new("/ws/alpha/api"))
    }

    /// A git directory at `directory`, with `HEAD` a file.
    fn git_directory(files: MemoryFiles, directory: &str) -> MemoryFiles {
        files
            .dir(&format!("{directory}/objects"))
            .dir(&format!("{directory}/refs"))
            .with(&format!("{directory}/HEAD"), b"ref: refs/heads/main\n")
    }

    #[test]
    fn a_location_that_is_a_git_directory_is_a_repository() {
        assert!(repository(&git_directory(
            MemoryFiles::default(),
            "/ws/alpha/api"
        )));
    }

    #[test]
    fn a_location_whose_dot_git_is_a_git_directory_is_a_repository() {
        assert!(repository(&git_directory(
            MemoryFiles::default(),
            "/ws/alpha/api/.git"
        )));
    }

    #[test]
    fn a_git_directory_needs_objects_refs_and_head() {
        let without_objects = MemoryFiles::default()
            .dir("/ws/alpha/api/.git/refs")
            .with("/ws/alpha/api/.git/HEAD", b"");
        let without_refs = MemoryFiles::default()
            .dir("/ws/alpha/api/.git/objects")
            .with("/ws/alpha/api/.git/HEAD", b"");
        let without_head = MemoryFiles::default()
            .dir("/ws/alpha/api/.git/objects")
            .dir("/ws/alpha/api/.git/refs");
        let objects_a_file = MemoryFiles::default()
            .with("/ws/alpha/api/.git/objects", b"")
            .dir("/ws/alpha/api/.git/refs")
            .with("/ws/alpha/api/.git/HEAD", b"");

        for (files, case) in [
            (without_objects, "no objects"),
            (without_refs, "no refs"),
            (without_head, "no HEAD"),
            (objects_a_file, "objects a file"),
        ] {
            assert!(!repository(&files), "{case}");
        }
    }

    #[test]
    fn a_head_link_counts_when_its_target_starts_with_refs_or_is_a_file() {
        let head = |target: &str| {
            MemoryFiles::default()
                .dir("/ws/alpha/api/.git/objects")
                .dir("/ws/alpha/api/.git/refs")
                .with("/ws/elsewhere/HEAD", b"ref: refs/heads/main\n")
                .link("/ws/alpha/api/.git/HEAD", target)
        };

        assert!(repository(&head("refs/heads/main")), "a target under refs");
        assert!(!repository(&head("heads/main")), "a target that is gone");
        assert!(
            repository(&head("/ws/elsewhere/HEAD")),
            "a target that is a file"
        );
    }

    #[test]
    fn a_gitdir_file_that_names_a_git_directory_is_a_repository() {
        let relative = git_directory(MemoryFiles::default(), "/ws/projects/api/.git")
            .with("/ws/alpha/api/.git", b"gitdir: ../../projects/api/.git\n");
        let absolute = git_directory(MemoryFiles::default(), "/ws/projects/api/.git")
            .with("/ws/alpha/api/.git", b"gitdir: /ws/projects/api/.git\n");

        assert!(repository(&relative), "a path from the location");
        assert!(repository(&absolute), "an absolute path");
    }

    #[test]
    fn a_gitdir_file_that_names_another_gitdir_file_is_followed() {
        let files = git_directory(MemoryFiles::default(), "/ws/real")
            .with("/ws/alpha/api/.git", b"gitdir: ../hop\n")
            .with("/ws/alpha/hop", b"gitdir: ../real\n");

        assert!(leads_to_a_git_directory(
            &files,
            Path::new("/ws/alpha/api/.git"),
            GITDIR_FILES_READ
        ));
        assert!(
            !leads_to_a_git_directory(&files, Path::new("/ws/alpha/api/.git"), 1),
            "the second file is not read"
        );
        assert!(repository(&files));
    }

    #[test]
    fn gitdir_files_that_name_each_other_lead_to_no_git_directory() {
        let files = MemoryFiles::default()
            .with("/ws/alpha/api/.git", b"gitdir: other\n")
            .with("/ws/alpha/api/other", b"gitdir: .git\n");

        assert!(!leads_to_a_git_directory(
            &files,
            Path::new("/ws/alpha/api/.git"),
            GITDIR_FILES_READ
        ));
    }

    #[test]
    fn a_file_of_pairs_with_a_gitdir_key_is_a_repository_whatever_it_names() {
        let nothing = MemoryFiles::default().with("/ws/alpha/api/.git", b"gitdir: /nowhere\n");
        let among_others = MemoryFiles::default().with(
            "/ws/alpha/api/.git",
            b"worktree: /ws/alpha/api\n  gitdir: /nowhere  \n",
        );

        assert!(repository(&nothing));
        assert!(repository(&among_others));
    }

    #[test]
    fn a_location_without_a_dot_git_entry_is_none() {
        let files = MemoryFiles::default().with("/ws/alpha/api/README.md", b"");

        assert!(!repository(&files));
        assert!(!repository(&MemoryFiles::default()), "no location at all");
    }

    #[test]
    fn an_empty_dot_git_directory_is_none() {
        assert!(!repository(
            &MemoryFiles::default().dir("/ws/alpha/api/.git")
        ));
    }

    #[test]
    fn a_dot_git_file_of_another_form_is_none() {
        for (content, case) in [
            (&b""[..], "empty"),
            (b"worktree: /ws/alpha/api\n", "no gitdir line"),
            (b"gitdir: /nowhere\nnot a pair\n", "a line that is no pair"),
            (b"gitdir: /nowhere\n\n", "a blank line"),
            (b"gitdir: a: b\n", "a line of three parts"),
            (b"gitdir:/nowhere\n", "no space after the key"),
            (b"gitdir: \xff\n", "not UTF-8"),
        ] {
            let files = MemoryFiles::default().with("/ws/alpha/api/.git", content);

            assert!(!repository(&files), "{case}");
        }
    }

    #[test]
    fn dot_parts_are_dropped_as_text() {
        for (path, expected) in [
            (
                "/ws/alpha/api/../../projects/api/.git",
                "/ws/projects/api/.git",
            ),
            ("/ws/./a/./b", "/ws/a/b"),
            ("/../a", "/a"),
            ("a/../../b", "../b"),
        ] {
            assert_eq!(without_dot_parts(Path::new(path)), Path::new(expected));
        }
    }

    #[test]
    fn the_dirty_count_is_the_distinct_changed_paths_plus_the_untracked_files() {
        let output = [
            &b"# branch.oid 1234"[..],
            b"# branch.head main",
            b"1 M. N... 100644 100644 100644 aaaa bbbb staged.txt",
            b"1 .M N... 100644 100644 100644 aaaa aaaa un staged.txt",
            b"1 MM N... 100644 100644 100644 aaaa bbbb both.txt",
            b"2 R. N... 100644 100644 100644 aaaa aaaa R100 new name.txt",
            b"old name.txt",
            b"u UU N... 100644 100644 100644 100644 aaaa bbbb cccc conflict.txt",
            b"? untracked.txt",
            b"? dir/one",
            b"? dir/two",
            b"! ignored.log",
        ]
        .join(&0u8);

        assert_eq!(dirty(&output), Some(8));
        assert_eq!(dirty(b""), Some(0));
        assert_eq!(dirty(b"# branch.head main\0"), Some(0));
    }

    #[test]
    fn an_entry_without_a_change_counts_nothing_and_a_path_is_counted_once() {
        let output = [
            &b"1 .. N... 100644 100644 100644 aaaa aaaa same.txt"[..],
            b"1 M. N... 100644 100644 100644 aaaa bbbb twice.txt",
            b"u UU N... 100644 100644 100644 100644 aaaa bbbb cccc twice.txt",
        ]
        .join(&0u8);

        assert_eq!(dirty(&output), Some(1));
    }

    #[test]
    fn a_status_record_that_does_not_read_gives_no_count() {
        for record in [
            &b"1 M."[..],
            b"1 M N... 100644 100644 100644 aaaa bbbb short-state.txt",
            b"2 R. N... 100644 100644 100644 aaaa aaaa R100 new.txt",
            b"u UU N... 100644",
        ] {
            assert_eq!(dirty(record), None, "{}", String::from_utf8_lossy(record));
        }
    }

    #[test]
    fn the_counts_of_rev_list_are_behind_and_then_ahead() {
        assert_eq!(behind_and_ahead(b"3\t5\n"), Some((3, 5)));
        assert_eq!(behind_and_ahead(b"0\t0\n"), Some((0, 0)));
        for output in [
            &b""[..],
            b"3\n",
            b"3\tx\n",
            b"1\t2\t3\n",
            b"-1\t2\n",
            b"\xff",
        ] {
            assert_eq!(behind_and_ahead(output), None, "{output:?}");
        }
    }
}
