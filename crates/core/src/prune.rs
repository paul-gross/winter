//! Pruning: the material the workspace once managed and no configuration declares now, what of it could hold
//! work and stays, and the removal of the rest.
//!
//! A scan only reads: the exclude file is located without a write of git configuration, and nothing is
//! removed. A removal checks its candidate again first, because the tree may have changed since the scan.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use crate::bootstrap::{self, BootstrapError, InitEvent};
use crate::config_load;
use crate::config_model::Workspace;
use crate::diagnostics::{self, Diagnostics};
use crate::git::{self, GitFailure, OwnRepository};
use crate::managed_block::blocks;
use crate::ports::Ports;
use crate::termination::{ConfigError, Termination};
use crate::workspace_exclude::{self, Finalized, Located};
use crate::workspace_files::{PROJECTS_DIR, standalone_location};

/// The directories whose dangling links are candidates, each relative to the root.
const LINK_DIRECTORIES: [&str; 3] = [".claude/skills", ".claude/agents", ".codex/skills"];

/// The vendor agent directories whose copies a block may name, each relative to the root.
const AGENT_DIRECTORIES: [&str; 3] = [".claude/agents", ".codex/agents", ".opencode/agent"];

/// What a candidate is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrphanKind {
    /// A directory under `projects/` that no declared project names.
    ProjectClone,
    /// A path that the exclude block of an extension no longer declared names.
    StandaloneClone,
    /// A link in a vendor directory whose target does not exist.
    BrokenSymlink,
    /// An agent file in a vendor directory that such a block names by its prefix.
    OrphanAgentCopy,
}

impl OrphanKind {
    /// The kind in the JSON document and in the preview.
    pub fn wire_name(self) -> &'static str {
        match self {
            Self::ProjectClone => "project_clone",
            Self::StandaloneClone => "standalone_clone",
            Self::BrokenSymlink => "broken_symlink",
            Self::OrphanAgentCopy => "orphan_agent_copy",
        }
    }

    fn is_clone(self) -> bool {
        matches!(self, Self::ProjectClone | Self::StandaloneClone)
    }
}

/// One candidate for removal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Orphan {
    pub kind: OrphanKind,
    /// Absolute. A project clone's is its entry under `projects/` as it stands, not resolved.
    pub path: PathBuf,
    /// The exclude block that named it; `None` for a project clone and for a dangling link.
    pub block: Option<String>,
    /// Why it stays; `None` when it is safe to remove.
    pub retained: Option<Retained>,
    /// What a removal compares the tree against.
    pub scanned: Scanned,
}

/// What the scan saw of a candidate's place.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Scanned {
    /// The directory that holds the candidate, resolved.
    pub parent: PathBuf,
    pub entry: Entry,
}

/// The type of an entry, with a symbolic link at the last component not followed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Entry {
    Directory,
    Link,
    File,
}

/// Why a clone stays. The first that holds, in this order, is the reason.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Retained {
    OutsideRoot,
    NotAClone,
    LinkedWorktree,
    HasWorktrees,
    /// Also a status that cannot be read.
    Dirty,
    /// Also a history that cannot be read.
    LocalOnly,
    /// Also a stash list that cannot be read.
    Stashes,
}

impl Retained {
    /// The reason as the preview, a skipped row, and the JSON document give it.
    pub fn notes(self) -> &'static str {
        match self {
            Self::OutsideRoot => "resolves outside the workspace",
            Self::NotAClone => "not a git clone (delete by hand if intentional)",
            Self::LinkedWorktree => "is a linked worktree (.git is a file)",
            Self::HasWorktrees => "has linked worktrees",
            Self::Dirty => "uncommitted or untracked changes",
            Self::LocalOnly => "has commits no remote-tracking branch holds",
            Self::Stashes => "has stashes",
        }
    }
}

/// What a scan found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Scan {
    pub orphans: Vec<Orphan>,
    pub evidence: Evidence,
}

/// Whether the blocks of the exclude file were read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Evidence {
    /// Read, or the root is no repository and holds none.
    Read,
    /// The exclude file could not be located or read: no candidate comes from a block.
    Unreadable,
}

/// Why a removal left its candidate in place.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Skipped {
    /// A retention reason, found by the scan or by the check before the removal.
    Retained(Retained),
    /// The candidate's place is not what the scan saw.
    Changed,
}

impl Skipped {
    pub fn notes(self) -> &'static str {
        match self {
            Self::Retained(reason) => reason.notes(),
            Self::Changed => "changed since the scan",
        }
    }
}

/// What a removal did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Removal {
    Removed,
    Skipped(Skipped),
    /// The cause; the path may be partly removed and is left as it is.
    Error(String),
}

/// What an invocation asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Request {
    pub dry_run: bool,
    pub force: bool,
    pub json: bool,
}

/// Why no scan was made.
#[derive(Debug, thiserror::Error)]
pub enum SurveyError {
    #[error("{0}")]
    Config(#[source] Box<ConfigError>),
    #[error("{0}")]
    Refused(#[source] BootstrapError),
}

/// A workspace and the scan of it.
#[derive(Debug)]
pub struct Survey {
    pub workspace: Workspace,
    pub scan: Scan,
}

/// What a removal run reports as it happens.
#[derive(Debug)]
pub enum PruneEvent<'e> {
    /// One candidate was attempted; `relative` is its path from the root, or its whole path outside it.
    Attempted {
        relative: &'e Path,
        removal: &'e Removal,
    },
    /// What the refresh of the exclude blocks after a removal reports.
    Reaggregated(&'e InitEvent),
}

/// Called once per event, in order. An `Err` stops the run: the candidates not yet reached stay as they are.
pub type PruneSink<'s> =
    dyn FnMut(&PruneEvent<'_>, &mut Diagnostics<'_>) -> Result<(), Termination> + 's;

/// What a completed removal run did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pruned {
    /// One per candidate of the scan, in its order.
    pub removals: Vec<Removal>,
    /// Whether the exclude blocks were refreshed without a failure.
    pub excludes_updated: bool,
}

/// How a removal run ended.
#[derive(Debug)]
pub enum PruneEnd {
    Completed(Pruned),
    /// The sink refused an event; this is the ending it gave.
    Stopped(Termination),
}

/// Finds the workspace of `start`, loads its configuration, and scans it. A request for JSON that names
/// neither `force` nor `dry_run` is refused once the configuration has loaded, before anything else is read.
pub fn survey(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    start: &Path,
    request: Request,
) -> Result<Survey, SurveyError> {
    let workspace = config_load::load(ports.files, start)
        .map_err(|error| SurveyError::Config(Box::new(error)))?;
    if request.json && !request.force && !request.dry_run {
        return Err(SurveyError::Refused(BootstrapError::JsonNeedsForce));
    }
    let scan = scan(ports, diagnostics, &workspace);
    Ok(Survey { workspace, scan })
}

/// Removes each candidate of `survey` in scan order, reporting each to `sink`, and then, when anything was
/// removed and the exclude blocks were read, refreshes those blocks.
pub fn prune(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    survey: &Survey,
    sink: &mut PruneSink<'_>,
) -> PruneEnd {
    match run(ports, diagnostics, survey, sink) {
        Ok(pruned) => PruneEnd::Completed(pruned),
        Err(ending) => PruneEnd::Stopped(ending),
    }
}

fn run(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    survey: &Survey,
    sink: &mut PruneSink<'_>,
) -> Result<Pruned, Termination> {
    let workspace = &survey.workspace;
    let resolved_root = ports.files.resolve(&workspace.root);
    let mut removals = Vec::with_capacity(survey.scan.orphans.len());
    let mut retained_blocks: Vec<String> = Vec::new();
    for orphan in &survey.scan.orphans {
        let removal = remove(ports, workspace, orphan);
        let relative = orphan
            .path
            .strip_prefix(&workspace.root)
            .or_else(|_| orphan.path.strip_prefix(&resolved_root))
            .unwrap_or(&orphan.path);
        let event = PruneEvent::Attempted {
            relative,
            removal: &removal,
        };
        let reported = sink(&event, diagnostics);
        if removal != Removal::Removed
            && let Some(block) = &orphan.block
            && !retained_blocks.contains(block)
        {
            retained_blocks.push(block.clone());
        }
        removals.push(removal);
        reported?;
    }
    let mut excludes_updated = false;
    if survey.scan.evidence == Evidence::Read && removals.contains(&Removal::Removed) {
        let finalized = reaggregate(ports, workspace, &retained_blocks);
        excludes_updated = finalized.is_ok();
        if let Some(event) = bootstrap::finalized_event(finalized) {
            sink(&PruneEvent::Reaggregated(&event), diagnostics)?;
        }
    }
    Ok(Pruned {
        removals,
        excludes_updated,
    })
}

/// The candidates of `workspace`, each with why it stays. Reads only.
///
/// Like [`remove`] and [`reaggregate`], it runs git without the variables that would have it answer for a
/// repository other than the one it is asked about.
pub fn scan(ports: &Ports<'_>, diagnostics: &mut Diagnostics<'_>, workspace: &Workspace) -> Scan {
    let programs = OwnRepository(ports.programs);
    let ports = &Ports {
        programs: &programs,
        ..*ports
    };
    let root = &workspace.root;
    let mut found = Found {
        ports,
        workspace,
        orphans: Vec::new(),
    };

    let projects = root.join(PROJECTS_DIR);
    for name in sorted_entries(ports, &projects) {
        let declared = workspace
            .config
            .project_repositories
            .iter()
            .any(|project| name == project.common.name.as_str());
        let path = projects.join(&name);
        if !declared && ports.files.is_dir(&path) {
            found.add(OrphanKind::ProjectClone, path, None);
        }
    }

    let (stale, evidence) = stale_blocks(ports, diagnostics, workspace);
    for (name, lines) in &stale {
        for group in lines.iter().filter_map(|line| clone_line(line)) {
            let path = ports.files.resolve(&root.join(group));
            if ports.files.exists(&path) {
                found.add(OrphanKind::StandaloneClone, path, Some(name));
            }
        }
    }

    for directory in LINK_DIRECTORIES {
        let directory = root.join(directory);
        for name in sorted_entries(ports, &directory) {
            let path = directory.join(name);
            if entry(ports, &path) == Some(Entry::Link) && !ports.files.exists(&path) {
                found.add(OrphanKind::BrokenSymlink, path, None);
            }
        }
    }

    for (name, lines) in &stale {
        for (directory, prefix) in lines.iter().filter_map(|line| copy_line(line)) {
            let directory = root.join(directory);
            let prefix = format!("{prefix}-");
            for file in sorted_entries(ports, &directory) {
                let path = directory.join(&file);
                let named = file.as_encoded_bytes().starts_with(prefix.as_bytes());
                if named && entry(ports, &path) == Some(Entry::File) {
                    found.add(OrphanKind::OrphanAgentCopy, path, Some(name));
                }
            }
        }
    }

    Scan {
        orphans: found.orphans,
        evidence,
    }
}

/// The candidates of a scan under way.
struct Found<'a> {
    ports: &'a Ports<'a>,
    workspace: &'a Workspace,
    orphans: Vec<Orphan>,
}

impl Found<'_> {
    /// Adds the candidate at `path`, unless it is one already, its entry cannot be told, or it is a clone
    /// whose removal would take a declared checkout with it.
    fn add(&mut self, kind: OrphanKind, path: PathBuf, block: Option<&String>) {
        let files = self.ports.files;
        let known = self
            .orphans
            .iter()
            .any(|orphan| orphan.path == path || files.resolve(&orphan.path) == path);
        let Some(entry) = entry(self.ports, &path) else {
            return;
        };
        if known || (kind.is_clone() && entry == Entry::Directory && self.declared(&path)) {
            return;
        }
        let parent = files.resolve(path.parent().unwrap_or(&path));
        let retained = kind
            .is_clone()
            .then(|| retained(self.ports, self.workspace, &path))
            .flatten();
        self.orphans.push(Orphan {
            kind,
            path,
            block: block.cloned(),
            retained,
            scanned: Scanned { parent, entry },
        });
    }

    /// Whether the directory at `path`, inside the root, is the checkout of a declared repository or holds
    /// one. A block can name such a place: a project that stops being an extension leaves its block behind.
    fn declared(&self, path: &Path) -> bool {
        let files = self.ports.files;
        let root = &self.workspace.root;
        let config = &self.workspace.config;
        let candidate = files.resolve(path);
        if candidate == files.resolve(root) || !candidate.starts_with(files.resolve(root)) {
            return false;
        }
        let projects = config
            .project_repositories
            .iter()
            .map(|project| root.join(PROJECTS_DIR).join(project.common.name.as_str()));
        let standalones = config
            .standalone_repositories
            .iter()
            .map(|standalone| standalone_location(root, &standalone.common.name, &standalone.path));
        projects
            .chain(standalones)
            .any(|location| files.resolve(&location).starts_with(&candidate))
    }
}

/// The names of the entries of `directory`, sorted; none when it cannot be listed.
fn sorted_entries(ports: &Ports<'_>, directory: &Path) -> Vec<OsString> {
    let mut names = ports.files.entries(directory).unwrap_or_default();
    names.sort();
    names
}

/// The type of the entry at `path`, its last component not followed; `None` when there is none or it cannot
/// be told.
fn entry(ports: &Ports<'_>, path: &Path) -> Option<Entry> {
    match ports.files.read_link(path) {
        Ok(Some(_)) => Some(Entry::Link),
        Ok(None) if ports.files.is_dir(path) => Some(Entry::Directory),
        Ok(None) if ports.files.is_file(path) => Some(Entry::File),
        _ => None,
    }
}

/// The blocks of the exclude file that belong to an extension no configuration declares now, in file order,
/// and whether the file was read. A failure is reported as a warning and leaves no block.
fn stale_blocks(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    workspace: &Workspace,
) -> (Vec<(String, Vec<String>)>, Evidence) {
    let root = &workspace.root;
    let mut unreadable = |cause: String| {
        diagnostics::notice(
            diagnostics.err,
            &format!(
                "warning: the workspace exclude file could not be read — {cause}; \
                 nothing it names is a candidate"
            ),
        );
        (Vec::new(), Evidence::Unreadable)
    };
    let file = match workspace_exclude::locate(ports.programs, ports.tracer, ports.files, root) {
        Ok(Located::NotARepository) => return (Vec::new(), Evidence::Read),
        Ok(Located::File(file)) => file,
        Err(cause) => return unreadable(bootstrap::git_text(cause)),
    };
    let content = match ports.files.read(&file) {
        Ok(None) => return (Vec::new(), Evidence::Read),
        Ok(Some(bytes)) => match String::from_utf8(bytes) {
            Ok(content) => content,
            Err(_) => return unreadable(format!("{} is not UTF-8", file.display())),
        },
        Err(cause) => return unreadable(format!("{}: {cause}", file.display())),
    };
    let config = &workspace.config;
    let eligible = |name: &str| {
        config
            .standalone_repositories
            .iter()
            .any(|standalone| standalone.common.name == name)
            || config.project_repositories.iter().any(|project| {
                project.common.name == name && bootstrap::extension(ports, root, project)
            })
    };
    let stale = blocks(&content)
        .into_iter()
        .filter(|(name, _)| !name.contains('/') && !eligible(name))
        .collect();
    (stale, Evidence::Read)
}

/// The path a block line names as a directory of the root: the line, trimmed, is `/<path>` with at most one
/// `/` after it. `projects` and anything under `.claude/` are not named this way.
fn clone_line(line: &str) -> Option<&str> {
    let rest = line.trim().strip_prefix('/')?;
    let group = match rest.strip_suffix('/') {
        Some(shorter) if !shorter.is_empty() => shorter,
        _ => rest,
    };
    (!group.is_empty() && group != "projects" && !group.starts_with(".claude/")).then_some(group)
}

/// The vendor agent directory and the name prefix a block line names: the line, trimmed, is
/// `<directory>/<prefix>-*`, the directory one of [`AGENT_DIRECTORIES`].
fn copy_line(line: &str) -> Option<(&str, &str)> {
    let named = line.trim().strip_suffix("-*")?;
    AGENT_DIRECTORIES.into_iter().find_map(|directory| {
        let prefix = named.strip_prefix(directory)?.strip_prefix('/')?;
        (!prefix.is_empty()).then_some((directory, prefix))
    })
}

/// Why the clone at `path` stays, or `None` when nothing in it can hold work.
fn retained(ports: &Ports<'_>, workspace: &Workspace, path: &Path) -> Option<Retained> {
    let files = ports.files;
    let root = files.resolve(&workspace.root);
    let resolved = files.resolve(path);
    if resolved == root || !resolved.starts_with(&root) {
        return Some(Retained::OutsideRoot);
    }
    let dot_git = path.join(".git");
    if !files.exists(&dot_git) {
        return Some(Retained::NotAClone);
    }
    if files.is_file(&dot_git) {
        return Some(Retained::LinkedWorktree);
    }
    let worktrees = dot_git.join("worktrees");
    if files.is_dir(&worktrees)
        && files
            .entries(&worktrees)
            .map_or(true, |held| !held.is_empty())
    {
        return Some(Retained::HasWorktrees);
    }
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (programs, tracer) = (ports.programs, ports.tracer);
    // A `.git` that is no repository would have git answer for a repository above the path: the workspace's
    // own. The answers below count only when git takes the path itself for the top of a work tree.
    match git::toplevel(programs, tracer, &name, path) {
        Ok(Some(top)) if top == resolved => {}
        Ok(_) => return Some(Retained::NotAClone),
        Err(_) => return Some(Retained::Dirty),
    }
    if !git::clean(programs, tracer, &name, path).unwrap_or(false) {
        return Some(Retained::Dirty);
    }
    if git::local_only(programs, tracer, &name, path).unwrap_or(true) {
        return Some(Retained::LocalOnly);
    }
    if git::stashed(programs, tracer, &name, path).unwrap_or(true) {
        return Some(Retained::Stashes);
    }
    None
}

/// Removes `orphan`, unless it was retained, its place is no longer what the scan saw, or, for a clone, a
/// retention reason holds now.
pub fn remove(ports: &Ports<'_>, workspace: &Workspace, orphan: &Orphan) -> Removal {
    let programs = OwnRepository(ports.programs);
    let ports = &Ports {
        programs: &programs,
        ..*ports
    };
    if let Some(reason) = orphan.retained {
        return Removal::Skipped(Skipped::Retained(reason));
    }
    if !unchanged(ports, workspace, orphan) {
        return Removal::Skipped(Skipped::Changed);
    }
    if orphan.kind.is_clone()
        && let Some(reason) = retained(ports, workspace, &orphan.path)
    {
        return Removal::Skipped(Skipped::Retained(reason));
    }
    match ports.writer.remove(&orphan.path) {
        Ok(()) => Removal::Removed,
        Err(cause) => Removal::Error(cause.to_string()),
    }
}

/// Whether the place of `orphan` is what the scan saw: its parent resolves to the same directory, inside the
/// root, and the entry itself is of the type its kind requires.
fn unchanged(ports: &Ports<'_>, workspace: &Workspace, orphan: &Orphan) -> bool {
    let files = ports.files;
    let Some(parent) = orphan.path.parent() else {
        return false;
    };
    let parent = files.resolve(parent);
    if parent != orphan.scanned.parent || !parent.starts_with(files.resolve(&workspace.root)) {
        return false;
    }
    let now = entry(ports, &orphan.path);
    match orphan.kind {
        OrphanKind::ProjectClone | OrphanKind::StandaloneClone => now == Some(orphan.scanned.entry),
        OrphanKind::BrokenSymlink => now == Some(Entry::Link) && !files.exists(&orphan.path),
        OrphanKind::OrphanAgentCopy => now == Some(Entry::File),
    }
}

/// Refreshes the exclude blocks of the extensions for the current configuration, keeping each block of
/// `retained_blocks` and its lines as they are: a block stays with the material it names.
pub fn reaggregate(
    ports: &Ports<'_>,
    workspace: &Workspace,
    retained_blocks: &[String],
) -> Result<Finalized, GitFailure> {
    let programs = OwnRepository(ports.programs);
    let ports = &Ports {
        programs: &programs,
        ..*ports
    };
    workspace_exclude::finalize(
        ports,
        &workspace.root,
        workspace.config.adopt_extensions,
        &bootstrap::eligible(ports, workspace),
        retained_blocks,
    )
}

#[cfg(test)]
mod tests {
    use std::io;
    use std::sync::Mutex;

    use super::*;
    use crate::files::{Edit, MemoryFiles, WriteFiles};
    use crate::programs::{Call, Finished, RunPrograms};
    use crate::trace::NoTrace;

    /// A writer that records each removal it is asked for and removes nothing.
    #[derive(Default)]
    struct Writer(Mutex<Vec<PathBuf>>);

    impl WriteFiles for Writer {
        fn create_dir_all(&self, _: &Path) -> io::Result<()> {
            unreachable!()
        }
        fn remove(&self, path: &Path) -> io::Result<()> {
            self.0.lock().expect("the record").push(path.to_path_buf());
            Ok(())
        }
        fn update(&self, _: &Path, _: &mut Edit<'_>) -> io::Result<bool> {
            unreachable!()
        }
    }

    /// No case here reaches git: each ends at a check of the files.
    struct NoGit;

    impl RunPrograms for NoGit {
        fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
            panic!("git was started: {:?}", call.args)
        }
    }

    fn workspace() -> Workspace {
        let files = MemoryFiles::default().with("/ws/.winter/config.toml", b"");
        config_load::load(&files, Path::new("/ws")).expect("a workspace")
    }

    /// What `remove` answers for `orphan` over `files`, and the removals it asked for.
    fn removed(files: &MemoryFiles, orphan: &Orphan) -> (Removal, Vec<PathBuf>) {
        let writer = Writer::default();
        let ports = Ports {
            files,
            writer: &writer,
            programs: &NoGit,
            tracer: &NoTrace,
        };
        let removal = remove(&ports, &workspace(), orphan);
        (removal, writer.0.into_inner().expect("the record"))
    }

    /// A project clone the scan found safe, as a directory of `/ws/projects`.
    fn safe_clone() -> Orphan {
        Orphan {
            kind: OrphanKind::ProjectClone,
            path: PathBuf::from("/ws/projects/old"),
            block: None,
            retained: None,
            scanned: Scanned {
                parent: PathBuf::from("/ws/projects"),
                entry: Entry::Directory,
            },
        }
    }

    const CHANGED: Removal = Removal::Skipped(Skipped::Changed);

    #[test]
    fn a_clone_that_became_a_link_since_the_scan_is_skipped_and_nothing_is_removed() {
        let files = MemoryFiles::default()
            .dir("/ws/projects")
            .dir("/elsewhere/work/.git")
            .link("/ws/projects/old", "/elsewhere/work");

        let (removal, asked) = removed(&files, &safe_clone());

        assert_eq!(removal, CHANGED);
        assert_eq!(Skipped::Changed.notes(), "changed since the scan");
        assert_eq!(asked, Vec::<PathBuf>::new());
    }

    #[test]
    fn a_clone_whose_parent_became_a_link_since_the_scan_is_skipped_and_nothing_is_removed() {
        for target in ["/elsewhere", "/ws/other"] {
            let files = MemoryFiles::default()
                .dir("/ws")
                .dir(&format!("{target}/old/.git"))
                .link("/ws/projects", target);

            let (removal, asked) = removed(&files, &safe_clone());

            assert_eq!(removal, CHANGED, "{target}");
            assert_eq!(asked, Vec::<PathBuf>::new(), "{target}");
        }
    }

    #[test]
    fn a_candidate_that_is_gone_or_of_another_type_is_skipped() {
        let gone = MemoryFiles::default().dir("/ws/projects");
        assert_eq!(removed(&gone, &safe_clone()).0, CHANGED);

        let link = Orphan {
            kind: OrphanKind::BrokenSymlink,
            path: PathBuf::from("/ws/.claude/skills/old"),
            block: None,
            retained: None,
            scanned: Scanned {
                parent: PathBuf::from("/ws/.claude/skills"),
                entry: Entry::Link,
            },
        };
        let mended = MemoryFiles::default()
            .with("/ws/skill", b"")
            .link("/ws/.claude/skills/old", "/ws/skill");
        let (removal, asked) = removed(&mended, &link);
        assert_eq!(removal, CHANGED, "a link whose target exists now");
        assert_eq!(asked, Vec::<PathBuf>::new());

        let copy = Orphan {
            kind: OrphanKind::OrphanAgentCopy,
            path: PathBuf::from("/ws/.claude/agents/ext-a.md"),
            block: Some("ext".to_owned()),
            retained: None,
            scanned: Scanned {
                parent: PathBuf::from("/ws/.claude/agents"),
                entry: Entry::File,
            },
        };
        let linked = MemoryFiles::default()
            .with("/ws/kept.md", b"")
            .link("/ws/.claude/agents/ext-a.md", "/ws/kept.md");
        let (removal, asked) = removed(&linked, &copy);
        assert_eq!(removal, CHANGED, "a copy that is a link now");
        assert_eq!(asked, Vec::<PathBuf>::new());
    }

    #[test]
    fn a_dangling_link_and_a_copy_that_stand_as_scanned_are_removed() {
        let files = MemoryFiles::default()
            .link("/ws/.claude/skills/old", "/ws/gone")
            .with("/ws/.claude/agents/ext-a.md", b"");
        for (kind, path, entry) in [
            (
                OrphanKind::BrokenSymlink,
                "/ws/.claude/skills/old",
                Entry::Link,
            ),
            (
                OrphanKind::OrphanAgentCopy,
                "/ws/.claude/agents/ext-a.md",
                Entry::File,
            ),
        ] {
            let orphan = Orphan {
                kind,
                path: PathBuf::from(path),
                block: None,
                retained: None,
                scanned: Scanned {
                    parent: PathBuf::from(path).parent().expect("a parent").into(),
                    entry,
                },
            };

            let (removal, asked) = removed(&files, &orphan);

            assert_eq!(removal, Removal::Removed, "{path}");
            assert_eq!(asked, [PathBuf::from(path)]);
        }
    }

    #[test]
    fn a_clone_safe_at_the_scan_whose_retention_reason_holds_now_is_skipped_with_it() {
        let no_git = MemoryFiles::default().dir("/ws/projects/old/src");
        let (removal, asked) = removed(&no_git, &safe_clone());
        assert_eq!(
            removal,
            Removal::Skipped(Skipped::Retained(Retained::NotAClone))
        );
        assert_eq!(asked, Vec::<PathBuf>::new());

        let worktree = MemoryFiles::default().with("/ws/projects/old/.git", b"gitdir: /x\n");
        assert_eq!(
            removed(&worktree, &safe_clone()).0,
            Removal::Skipped(Skipped::Retained(Retained::LinkedWorktree))
        );

        let has_worktrees = MemoryFiles::default().dir("/ws/projects/old/.git/worktrees/alpha");
        assert_eq!(
            removed(&has_worktrees, &safe_clone()).0,
            Removal::Skipped(Skipped::Retained(Retained::HasWorktrees))
        );
    }

    #[test]
    fn a_candidate_retained_at_the_scan_is_skipped_with_its_reason_whatever_the_tree_holds() {
        let orphan = Orphan {
            retained: Some(Retained::Dirty),
            ..safe_clone()
        };

        let (removal, asked) = removed(&MemoryFiles::default(), &orphan);

        assert_eq!(
            removal,
            Removal::Skipped(Skipped::Retained(Retained::Dirty))
        );
        assert_eq!(asked, Vec::<PathBuf>::new());
    }

    #[test]
    fn a_block_line_names_a_directory_of_the_root_with_at_most_one_slash_after_it() {
        for (line, group) in [
            ("/lib/", Some("lib")),
            ("/lib", Some("lib")),
            ("  /vendor/lib/ \t", Some("vendor/lib")),
            ("/lib//", Some("lib/")),
            ("//", Some("/")),
            ("/../out/", Some("../out")),
            ("/.claude", Some(".claude")),
            ("/projects-old/", Some("projects-old")),
            ("/", None),
            ("", None),
            ("lib/", None),
            ("/projects/", None),
            ("/projects", None),
            ("/.claude/skills/x/", None),
            (".claude/agents/ext-*", None),
        ] {
            assert_eq!(clone_line(line), group, "{line:?}");
        }
    }

    #[test]
    fn a_block_line_names_agent_copies_by_a_vendor_directory_and_a_prefix() {
        for (line, named) in [
            (".claude/agents/ext-*", Some((".claude/agents", "ext"))),
            (
                " .codex/agents/my-ext-* ",
                Some((".codex/agents", "my-ext")),
            ),
            (".opencode/agent/a/b-*", Some((".opencode/agent", "a/b"))),
            (".claude/skills/ext-*", None),
            (".claude/agents/-*", None),
            (".claude/agents/ext", None),
            (".claude/agents/ext-*x", None),
            ("/.claude/agents/ext-*", None),
            (".claude/agentsx/ext-*", None),
        ] {
            assert_eq!(copy_line(line), named, "{line:?}");
        }
    }

    #[test]
    fn each_kind_and_each_reason_has_its_text() {
        assert_eq!(
            [
                OrphanKind::ProjectClone,
                OrphanKind::StandaloneClone,
                OrphanKind::BrokenSymlink,
                OrphanKind::OrphanAgentCopy,
            ]
            .map(OrphanKind::wire_name),
            [
                "project_clone",
                "standalone_clone",
                "broken_symlink",
                "orphan_agent_copy"
            ]
        );
        assert_eq!(
            [
                Retained::OutsideRoot,
                Retained::NotAClone,
                Retained::LinkedWorktree,
                Retained::HasWorktrees,
                Retained::Dirty,
                Retained::LocalOnly,
                Retained::Stashes,
            ]
            .map(Retained::notes),
            [
                "resolves outside the workspace",
                "not a git clone (delete by hand if intentional)",
                "is a linked worktree (.git is a file)",
                "has linked worktrees",
                "uncommitted or untracked changes",
                "has commits no remote-tracking branch holds",
                "has stashes",
            ]
        );
    }
}
