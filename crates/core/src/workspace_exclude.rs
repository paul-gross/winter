//! The workspace exclude file: where it is, and the blocks written into it.
//!
//! The file is `info/exclude` under the git directory of the workspace root, so each linked worktree used as a
//! root has its own. A block is one managed block of exclude patterns; the rest of the file is the user's.

use std::ffi::OsStr;
use std::io;
use std::path::{Path, PathBuf};

use crate::config_model::AdoptExtensions;
use crate::files::{ReadFiles, WriteFiles};
use crate::git::{self, GitFailure};
use crate::managed_block::{Markers, Position, blocks, replace_or_insert, strip};
use crate::ports::Ports;
use crate::programs::RunPrograms;
use crate::termination::InternalError;
use crate::trace::CommandTracer;

/// The repository a git call made here is reported for.
const REPO: &str = "winter";

/// The block of the local configuration files, written whatever the adoption mode and never stripped.
const CONFIG_BLOCK: &str = "winter-config";
const CONFIG_LINE: &str = ".winter/config/**/*.local.*";

/// The exclude file of a workspace root.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Located {
    /// The root holds no `.git`.
    NotARepository,
    File(PathBuf),
}

/// An extension that gets a block of its own.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Eligible {
    /// The name of its block, which holds no `/`.
    pub name: String,
    /// Its directory, relative to the root.
    pub relative: String,
    /// The lines of the block after the one of its directory.
    pub extra: Vec<String>,
}

/// What [`finalize`] did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Finalized {
    NotARepository,
    /// No block of an extension changed. A change to the `winter-config` block alone is among these.
    Unchanged,
    /// The blocks of the extensions changed; `names` are those of the eligible ones, sorted.
    Updated {
        file: PathBuf,
        names: Vec<String>,
    },
}

/// The exclude file of the workspace at `root`, an absolute path. The root itself is looked at, and no
/// directory above it. Git configuration is not written.
pub fn locate(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    files: &dyn ReadFiles,
    root: &Path,
) -> Result<Located, GitFailure> {
    Ok(match git_directory(programs, tracer, files, root)? {
        Some(directory) => Located::File(exclude_file(&directory)),
        None => Located::NotARepository,
    })
}

/// [`locate`], for a caller about to write the file: a root that is a linked worktree is first given an
/// exclude file that its git reads and no other worktree does.
///
/// That takes `extensions.worktreeConfig`, before which `core.bare` and `core.worktree` move to the
/// `config.worktree` of the common directory, and a `core.excludesFile` of the worktree. Each setting is read
/// before it is written, so a run that finds them in place writes no configuration.
pub fn locate_for_write(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    files: &dyn ReadFiles,
    root: &Path,
) -> Result<Located, GitFailure> {
    let Some(directory) = git_directory(programs, tracer, files, root)? else {
        return Ok(Located::NotARepository);
    };
    let file = exclude_file(&directory);
    let common = files.resolve(&root.join(git::common_dir(programs, tracer, REPO, root)?));
    if directory != common {
        enable_worktree_config(programs, tracer, root, &common)?;
        let key = "core.excludesFile";
        let current = git::config_get(programs, tracer, REPO, root, &["--worktree", "--get", key])?;
        if current.as_deref() != Some(file.as_os_str()) {
            let args = ["--worktree".as_ref(), key.as_ref(), file.as_os_str()];
            git::config_write(programs, tracer, REPO, root, &args)?;
        }
    }
    Ok(Located::File(file))
}

/// Writes the block `name` holding `lines` into the exclude file of the workspace at `root`, and answers the
/// file when it changed. `None` when the block already holds its lines, or the root is no repository.
pub fn write_block(
    ports: &Ports<'_>,
    root: &Path,
    name: &str,
    lines: &[String],
) -> Result<Option<PathBuf>, GitFailure> {
    let Located::File(file) = locate_for_write(ports.programs, ports.tracer, ports.files, root)?
    else {
        return Ok(None);
    };
    let body: Vec<&str> = lines.iter().map(String::as_str).collect();
    let written = rewrite(ports.writer, &file, &mut |content| {
        replace_or_insert(content, &Markers::exclude(name), &body, Position::Append)
    })?;
    Ok(written.then_some(file))
}

/// Brings the blocks of the extensions in the exclude file of the workspace at `root` to those of
/// `eligible`, in one update of the file, and writes the `winter-config` block.
///
/// A block whose name holds no `/` is the block of an extension: it is stripped unless its name is
/// `winter-config`, one of `eligible`, or one of `keep`. Under [`AdoptExtensions::None`] the `winter-config`
/// block is written and nothing else is touched.
pub fn finalize(
    ports: &Ports<'_>,
    root: &Path,
    mode: AdoptExtensions,
    eligible: &[Eligible],
    keep: &[String],
) -> Result<Finalized, GitFailure> {
    let Located::File(file) = locate_for_write(ports.programs, ports.tracer, ports.files, root)?
    else {
        return Ok(Finalized::NotARepository);
    };
    let mut updated = false;
    rewrite(ports.writer, &file, &mut |existing| {
        let base = replace_or_insert(
            existing,
            &Markers::exclude(CONFIG_BLOCK),
            &[CONFIG_LINE],
            Position::Append,
        );
        if mode == AdoptExtensions::None {
            return base;
        }
        let content = extension_blocks(&base, eligible, keep);
        updated = content != base;
        content
    })?;
    if !updated {
        return Ok(Finalized::Unchanged);
    }
    let mut names: Vec<String> = eligible.iter().map(|entry| entry.name.clone()).collect();
    names.sort();
    Ok(Finalized::Updated { file, names })
}

/// `content` without the blocks of extensions that are neither eligible nor kept, and with the block of each
/// eligible one.
fn extension_blocks(content: &str, eligible: &[Eligible], keep: &[String]) -> String {
    let mut result = content.to_owned();
    for (name, _) in blocks(content) {
        let stays = name.contains('/')
            || name == CONFIG_BLOCK
            || keep.contains(&name)
            || eligible.iter().any(|entry| entry.name == name);
        if !stays {
            result = strip(&result, &Markers::exclude(&name));
        }
    }
    for entry in eligible {
        let directory = format!("/{}/", entry.relative);
        let body: Vec<&str> = std::iter::once(directory.as_str())
            .chain(entry.extra.iter().map(String::as_str))
            .collect();
        let markers = Markers::exclude(&entry.name);
        result = replace_or_insert(&result, &markers, &body, Position::Append);
    }
    result
}

/// The git directory of the root, resolved; `None` when the root holds no `.git`.
fn git_directory(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    files: &dyn ReadFiles,
    root: &Path,
) -> Result<Option<PathBuf>, GitFailure> {
    let dot_git = root.join(".git");
    if !files.is_file(&dot_git) && !files.is_dir(&dot_git) {
        return Ok(None);
    }
    let named = git::absolute_git_dir(programs, tracer, REPO, root)?;
    Ok(Some(files.resolve(&named)))
}

fn exclude_file(git_directory: &Path) -> PathBuf {
    git_directory.join("info").join("exclude")
}

/// Turns `extensions.worktreeConfig` on where it is off. The settings that would then apply to every worktree
/// from the common `config` move to the common `config.worktree` first.
fn enable_worktree_config(
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    root: &Path,
    common: &Path,
) -> Result<(), GitFailure> {
    let extension = "extensions.worktreeConfig";
    let args = ["--local", "--type=bool", "--get", extension];
    let enabled = git::config_get(programs, tracer, REPO, root, &args)?;
    if enabled.as_deref() == Some(OsStr::new("true")) {
        return Ok(());
    }
    let shared = common.join("config");
    let own = common.join("config.worktree");
    let file = OsStr::new("--file");
    for key in ["core.bare", "core.worktree"] {
        let key = OsStr::new(key);
        let args = [file, shared.as_os_str(), OsStr::new("--get"), key];
        let Some(value) = git::config_get(programs, tracer, REPO, root, &args)? else {
            continue;
        };
        let args = [file, own.as_os_str(), key, &value];
        git::config_write(programs, tracer, REPO, root, &args)?;
        let args = [file, shared.as_os_str(), OsStr::new("--unset"), key];
        git::config_write(programs, tracer, REPO, root, &args)?;
    }
    git::config_write(programs, tracer, REPO, root, &[extension, "true"])
}

/// One update of the exclude file to what `edit` makes of its text, and whether it was written.
///
/// A file that is not UTF-8 is an error and keeps its bytes.
fn rewrite(
    writer: &dyn WriteFiles,
    file: &Path,
    edit: &mut dyn FnMut(&str) -> String,
) -> Result<bool, GitFailure> {
    let mut unreadable = false;
    let written = writer
        .update(file, &mut |current| {
            let Ok(content) = std::str::from_utf8(current.unwrap_or_default()) else {
                unreadable = true;
                return None;
            };
            let next = edit(content);
            (next != content).then(|| next.into_bytes())
        })
        .map_err(internal)?;
    if unreadable {
        let message = format!("{} is not UTF-8", file.display());
        return Err(internal(io::Error::new(
            io::ErrorKind::InvalidData,
            message,
        )));
    }
    Ok(written)
}

fn internal(error: io::Error) -> GitFailure {
    GitFailure::Internal(Box::new(InternalError::unexpected(error)))
}
