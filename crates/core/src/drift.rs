//! Drift: the disagreement between the declared project repositories and the directories under `projects/`.
//!
//! Projects alone are compared; a standalone or an implicit repository never drifts. Nothing is written here:
//! [`detect`] reads one directory through the file port, and [`notice_line`] gives the line a domain entry
//! writes through `diagnostics::notice`.

use std::ffi::OsString;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use crate::config_model::ProjectRepository;
use crate::files::ReadFiles;
use crate::termination::InternalError;
use crate::workspace_files::PROJECTS_DIR;

/// What the declared projects and the directories under `projects/` disagree on.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct Drift {
    /// The name of every declared project that has no directory, sorted; a name declared twice is held twice.
    pub missing: Vec<String>,
    /// The name of every directory no project declares, sorted.
    pub undeclared: Vec<String>,
}

/// The drift of the workspace at `root`, whose configuration declares `projects`.
///
/// The names on disk are those of the entries of `<root>/projects` that are directories and do not start with
/// `.`, with symbolic links followed; when `<root>/projects` is no directory there is none. Names are compared
/// and sorted as bytes, so a directory name that is not UTF-8 equals no declared name and is undeclared, and it
/// is held with each such byte sequence replaced by U+FFFD.
///
/// A `<root>/projects` that is a directory and cannot be listed is an error.
pub(crate) fn detect(
    files: &dyn ReadFiles,
    root: &Path,
    projects: &[ProjectRepository],
) -> Result<Drift, InternalError> {
    let directory = root.join(PROJECTS_DIR);
    let mut on_disk: Vec<OsString> = if files.is_dir(&directory) {
        files
            .entries(&directory)
            .map_err(InternalError::unexpected)?
            .into_iter()
            .filter(|name| {
                !name.as_bytes().starts_with(b".") && files.is_dir(&directory.join(name))
            })
            .collect()
    } else {
        Vec::new()
    };
    on_disk.sort();
    on_disk.dedup();

    let mut missing: Vec<String> = projects
        .iter()
        .map(|project| &project.common.name)
        .filter(|name| {
            !on_disk
                .iter()
                .any(|held| held.as_bytes() == name.as_bytes())
        })
        .cloned()
        .collect();
    missing.sort();
    let undeclared = on_disk
        .iter()
        .filter(|held| {
            !projects
                .iter()
                .any(|project| project.common.name.as_bytes() == held.as_bytes())
        })
        .map(|held| held.to_string_lossy().into_owned())
        .collect();
    Ok(Drift {
        missing,
        undeclared,
    })
}

/// The notice line of `drift`, without a newline, or `None` when both of its lists are empty.
///
/// The line names each missing name in order when there is any, and then each undeclared name in order when
/// there is any.
pub(crate) fn notice_line(drift: &Drift) -> Option<String> {
    let mut parts = Vec::new();
    if !drift.missing.is_empty() {
        parts.push(format!(
            "missing from projects/: {}",
            drift.missing.join(", ")
        ));
    }
    if !drift.undeclared.is_empty() {
        parts.push(format!(
            "undeclared in config: {}",
            drift.undeclared.join(", ")
        ));
    }
    if parts.is_empty() {
        return None;
    }
    Some(format!(
        "warning: Config and projects/ have drifted — {}",
        parts.join("; ")
    ))
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::config_load;
    use crate::files::MemoryFiles;

    const CONFIG: &str = "/ws/.winter/config.toml";

    /// The project entries a shared configuration file that declares `names`, in that order, loads as.
    fn declared(names: &[&str]) -> Vec<ProjectRepository> {
        let text: String = names
            .iter()
            .map(|name| format!("[[project_repository]]\nname = \"{name}\"\n"))
            .collect();
        let files = MemoryFiles::default().with(CONFIG, text.as_bytes());
        config_load::load(&files, Path::new("/ws"))
            .expect("a workspace")
            .config
            .project_repositories
    }

    fn detected(files: &MemoryFiles, names: &[&str]) -> Drift {
        detect(files, Path::new("/ws"), &declared(names)).expect("a listing")
    }

    fn drift(missing: &[&str], undeclared: &[&str]) -> Drift {
        Drift {
            missing: missing.iter().map(|&name| name.to_owned()).collect(),
            undeclared: undeclared.iter().map(|&name| name.to_owned()).collect(),
        }
    }

    #[test]
    fn a_directory_for_every_declared_project_and_no_other_is_no_drift() {
        let files = MemoryFiles::default()
            .dir("/ws/projects/api")
            .dir("/ws/projects/web");

        assert_eq!(detected(&files, &["web", "api"]), Drift::default());
    }

    #[test]
    fn no_projects_directory_gives_no_name_on_disk() {
        let files = MemoryFiles::default().dir("/ws");

        assert_eq!(detected(&files, &[]), Drift::default());
        assert_eq!(detected(&files, &["api"]), drift(&["api"], &[]));
    }

    #[test]
    fn a_projects_path_that_is_no_directory_gives_no_name_on_disk() {
        for files in [
            MemoryFiles::default().with("/ws/projects", b""),
            MemoryFiles::default().unreadable("/ws/projects", ErrorKind::PermissionDenied),
        ] {
            assert_eq!(detected(&files, &["api"]), drift(&["api"], &[]));
        }
    }

    #[test]
    fn a_file_under_projects_is_not_a_directory() {
        let files = MemoryFiles::default()
            .with("/ws/projects/api", b"")
            .with("/ws/projects/notes.txt", b"")
            .unreadable("/ws/projects/odd", ErrorKind::PermissionDenied);

        assert_eq!(detected(&files, &["api"]), drift(&["api"], &[]));
    }

    #[test]
    fn a_name_that_starts_with_a_dot_is_left_out() {
        let files = MemoryFiles::default()
            .dir("/ws/projects/.hidden")
            .dir("/ws/projects/api");

        assert_eq!(detected(&files, &["api"]), Drift::default());
        assert_eq!(
            detected(&files, &["api", ".hidden"]),
            drift(&[".hidden"], &[])
        );
    }

    #[test]
    fn a_link_to_a_directory_is_a_directory_and_a_link_that_leads_nowhere_is_not() {
        let files = MemoryFiles::default()
            .dir("/ws/elsewhere/api")
            .link("/ws/projects/api", "../elsewhere/api")
            .link("/ws/projects/web", "../elsewhere/gone");

        assert_eq!(detected(&files, &["api", "web"]), drift(&["web"], &[]));
    }

    #[test]
    fn a_projects_directory_that_is_a_link_is_listed_through_it() {
        let files = MemoryFiles::default()
            .dir("/held/api")
            .dir("/held/old")
            .link("/ws/projects", "/held");

        assert_eq!(detected(&files, &["api"]), drift(&[], &["old"]));
    }

    #[test]
    fn a_projects_directory_that_cannot_be_listed_is_an_unexpected_failure() {
        let files = MemoryFiles::default().failing_dir("/ws/projects", ErrorKind::PermissionDenied);

        let error = detect(&files, Path::new("/ws"), &declared(&["api"])).expect_err("a failure");

        assert!(
            matches!(error, InternalError::Unexpected { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn both_lists_are_sorted_and_a_name_declared_twice_is_missing_twice() {
        let files = MemoryFiles::default()
            .dir("/ws/projects/zeta")
            .dir("/ws/projects/c")
            .dir("/ws/projects/kept");

        assert_eq!(
            detected(&files, &["b", "kept", "a", "b"]),
            drift(&["a", "b", "b"], &["c", "zeta"])
        );
    }

    #[test]
    fn names_sort_by_code_point_and_not_by_letter() {
        let files = MemoryFiles::default()
            .dir("/ws/projects/b")
            .dir("/ws/projects/B")
            .dir("/ws/projects/é");

        assert_eq!(
            detected(&files, &["a", "Z", "ä"]),
            drift(&["Z", "a", "ä"], &["B", "b", "é"])
        );
    }

    /// A workspace whose every path is a directory, and whose every directory holds the same names.
    struct Named(Vec<OsString>);

    impl ReadFiles for Named {
        fn read(&self, _path: &Path) -> std::io::Result<Option<Vec<u8>>> {
            Err(ErrorKind::IsADirectory.into())
        }

        fn is_file(&self, _path: &Path) -> bool {
            false
        }

        fn exists(&self, _path: &Path) -> bool {
            true
        }

        fn is_dir(&self, _path: &Path) -> bool {
            true
        }

        fn entries(&self, _path: &Path) -> std::io::Result<Vec<OsString>> {
            Ok(self.0.clone())
        }

        fn missing(&self, _: &std::path::Path) -> bool {
            unreachable!()
        }

        fn read_link(&self, _path: &Path) -> std::io::Result<Option<std::path::PathBuf>> {
            Ok(None)
        }
    }

    #[test]
    fn a_directory_name_that_is_not_utf_8_is_undeclared_and_held_lossily() {
        use std::os::unix::ffi::OsStringExt;

        let files = Named(vec![
            OsString::from_vec(b"ap\xff".to_vec()),
            OsString::from("api"),
        ]);

        let found =
            detect(&files, Path::new("/ws"), &declared(&["api", "ap\u{fffd}"])).expect("a listing");

        assert_eq!(found, drift(&["ap\u{fffd}"], &["ap\u{fffd}"]));
    }

    #[test]
    fn the_line_holds_the_missing_names_in_order_and_then_the_undeclared_ones() {
        let files = MemoryFiles::default().dir("/ws/projects/c");

        let line = notice_line(&detected(&files, &["b", "a"])).expect("a line");

        assert_eq!(
            line,
            "warning: Config and projects/ have drifted — missing from projects/: a, b; undeclared in config: c"
        );
    }

    #[test]
    fn the_line_holds_only_the_list_that_is_not_empty() {
        assert_eq!(
            notice_line(&drift(&["api", "web"], &[])).as_deref(),
            Some("warning: Config and projects/ have drifted — missing from projects/: api, web")
        );
        assert_eq!(
            notice_line(&drift(&[], &["old"])).as_deref(),
            Some("warning: Config and projects/ have drifted — undeclared in config: old")
        );
    }

    #[test]
    fn no_drift_gives_no_line() {
        assert_eq!(notice_line(&Drift::default()), None);
    }
}
