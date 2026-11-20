//! The repository catalog of a workspace: the addition and the removal of one repository block in a
//! configuration file, and the list of the repositories a workspace holds.

use std::path::{Path, PathBuf};

pub(crate) use crate::config_edit::NewBlock;
use crate::config_edit::{self, BlockRemoval, CatalogError, RepositoryKind, StandaloneOption};
use crate::config_load;
use crate::diagnostics::{self, Diagnostics};
use crate::drift;
use crate::files::{ReadFiles, WriteFiles};
use crate::termination::{ConfigError, FileCause, InternalError, TomlParseError};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE, PROJECTS_DIR, location};

/// The directory of the implicit repository `product`, relative to a workspace root.
const PRODUCT_DIR: &str = "product";

/// The directory of the implicit repository `harness`, relative to a workspace root.
const HARNESS_DIR: &str = "context/harness";

/// One repository of a workspace, as the list gives it. Every location is absolute, under the workspace root
/// unless a name or a path of the configuration leads out of it.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Listed {
    /// An implicit repository, `product` or `harness`: one the workspace holds without declaring it.
    Singleton { name: &'static str, path: PathBuf },
    Standalone {
        name: String,
        url: Option<String>,
        path: PathBuf,
    },
    Project {
        name: String,
        url: Option<String>,
        main_path: PathBuf,
        pinned: bool,
    },
}

/// The list of the repositories of a workspace, with the root their locations are under.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Listing {
    pub root: PathBuf,
    /// The implicit repositories, then the standalone ones, then the projects.
    pub entries: Vec<Listed>,
}

/// Why a list was not made.
#[derive(Debug, thiserror::Error)]
pub(crate) enum ListError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Internal(#[from] InternalError),
}

/// The repositories of the workspace of `start`, and its root.
///
/// The workspace is loaded first. Then come the implicit repositories: `product` when `<root>/product` is a
/// directory, and `harness` when `<root>/context/harness/.git` exists, each with symbolic links followed. Then
/// the standalone repositories, and then the project repositories, each in the order the load gives them: two
/// project entries of one name are both listed.
///
/// A standalone repository is at its `path` under the root, or at its name when it declares none, and a
/// project at its name under `<root>/projects`. A location is joined and never resolved: a repeated `/`, a `.`
/// part, and a trailing `/` are dropped, a `..` part is kept, and no symbolic link is followed.
///
/// When the declared projects and the directories under `<root>/projects` disagree, the notice of that is
/// written through `diagnostics` before the list is returned. Nothing else is written, and no file is changed.
pub(crate) fn list(
    files: &dyn ReadFiles,
    start: &Path,
    diagnostics: &mut Diagnostics<'_>,
) -> Result<Listing, ListError> {
    let workspace = config_load::load(files, start)?;
    let root = workspace.root;
    let config = workspace.config;

    let mut listed = Vec::new();
    if files.is_dir(&root.join(PRODUCT_DIR)) {
        listed.push(Listed::Singleton {
            name: "product",
            path: location(&root, Path::new(PRODUCT_DIR)),
        });
    }
    if files.exists(&root.join(HARNESS_DIR).join(".git")) {
        listed.push(Listed::Singleton {
            name: "harness",
            path: location(&root, Path::new(HARNESS_DIR)),
        });
    }

    if let Some(line) =
        drift::notice_line(&drift::detect(files, &root, &config.project_repositories)?)
    {
        diagnostics::notice(diagnostics.err, &line);
    }

    for standalone in config.standalone_repositories {
        let relative = if standalone.path.is_empty() {
            &standalone.common.name
        } else {
            &standalone.path
        };
        listed.push(Listed::Standalone {
            path: location(&root, Path::new(relative)),
            name: standalone.common.name,
            url: standalone.common.url,
        });
    }
    for project in config.project_repositories {
        listed.push(Listed::Project {
            main_path: location(&root, &Path::new(PROJECTS_DIR).join(&project.common.name)),
            name: project.common.name,
            url: project.common.url,
            pinned: project.pinned,
        });
    }
    Ok(Listing {
        root,
        entries: listed,
    })
}

/// The repository a removal took out of a configuration file: the two parts of its target.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Removed {
    pub kind: RepositoryKind,
    pub name: String,
}

/// Why a removal did not happen.
#[derive(Debug, thiserror::Error)]
pub(crate) enum RemoveError {
    #[error(transparent)]
    Catalog(#[from] CatalogError),
    #[error(transparent)]
    Config(#[from] ConfigError),
}

/// Removes the repository `target`, spelled `<type>/<name>`, from a configuration file of the workspace of
/// `start`: the machine-local file when `local` is set, else the shared one.
///
/// A target without `/` is refused before any file is read. Then the workspace is loaded, so a configuration
/// the load refuses ends the removal with both files as they were, whichever of the two is at fault. Then the
/// type is checked to be one of the two words, and then the first block of that type whose effective name is
/// the name is removed from the one file. Only that file is searched, and it is replaced only when a block was
/// removed.
///
/// The edit is made on the bytes the writer hands it, not on what the load read, so an entry another writer
/// added to the file since the load is kept. A file the writer cannot replace is
/// [`ConfigError::FileWrite`], and one that is no longer valid TOML when the writer reads it is
/// [`ConfigError::FileRead`].
pub(crate) fn remove(
    files: &dyn ReadFiles,
    writer: &dyn WriteFiles,
    start: &Path,
    target: &str,
    local: bool,
) -> Result<Removed, RemoveError> {
    let Some((word, name)) = target.split_once('/') else {
        return Err(CatalogError::TargetShape {
            target: target.to_owned(),
        }
        .into());
    };
    let root = config_load::load(files, start)?.root;
    let Some(kind) = repository_kind(word) else {
        return Err(CatalogError::UnknownKind {
            kind: word.to_owned(),
        }
        .into());
    };
    let file = root.join(if local {
        LOCAL_CONFIG_FILE
    } else {
        CONFIG_FILE
    });
    match remove_block(writer, &file, kind, name) {
        Ok(Removal::Done) => Ok(Removed {
            kind,
            name: name.to_owned(),
        }),
        Ok(Removal::NotFound) => Err(CatalogError::NotFound {
            kind,
            name: name.to_owned(),
        }
        .into()),
        Ok(Removal::NotArrayOfTables) => {
            Err(CatalogError::NotArrayOfTables { kind, path: file }.into())
        }
        Ok(Removal::Entangled) => Err(CatalogError::Entangled {
            kind,
            name: name.to_owned(),
        }
        .into()),
        Err(Unedited::Invalid(error)) => Err(ConfigError::FileRead {
            path: file,
            cause: FileCause::Toml(error),
        }
        .into()),
        Err(Unedited::Unwritten(cause)) => Err(ConfigError::FileWrite { path: file, cause }.into()),
    }
}

/// The repository an addition wrote to a configuration file.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Added {
    pub kind: RepositoryKind,
    pub url: String,
}

/// Why an addition did not happen.
#[derive(Debug, thiserror::Error)]
pub(crate) enum AddError {
    #[error(transparent)]
    Catalog(#[from] CatalogError),
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Internal(#[from] InternalError),
}

/// The edited file does not exist and is the shared one.
#[derive(Debug, thiserror::Error)]
#[error("the configuration file {} does not exist", .path.display())]
struct SharedFileMissing {
    path: PathBuf,
}

/// Adds the repository `request` to a configuration file of the workspace of `start`: the machine-local file
/// when `local` is set, else the shared one.
///
/// The workspace is loaded first, so a configuration the load refuses ends the addition with both files as
/// they were. Then the checks run in order: the shape of the URL, the options that do not fit the kind, the
/// `--path` rule, and a declared repository of the kind with the URL or with the name, the URL test first of
/// each entry. Then the one file is replaced through the writer with its text and the block appended; only
/// that file is read for the edit, and it is replaced once. The local file is created when absent. The shared
/// file must exist.
pub(crate) fn add(
    files: &dyn ReadFiles,
    writer: &dyn WriteFiles,
    start: &Path,
    request: &NewBlock<'_>,
    local: bool,
) -> Result<Added, AddError> {
    let workspace = config_load::load(files, start)?;
    if !url_shape_accepted(request.url) {
        return Err(CatalogError::InvalidUrl {
            url: request.url.to_owned(),
        }
        .into());
    }
    if request.kind == RepositoryKind::Standalone {
        if request.pinned {
            return Err(CatalogError::PinnedStandalone.into());
        }
    } else if let Some(option) = [
        (request.path, StandaloneOption::Path),
        (request.prefix, StandaloneOption::Prefix),
        (request.r#ref, StandaloneOption::Ref),
    ]
    .into_iter()
    .find_map(|(given, option)| given.map(|_| option))
    {
        return Err(CatalogError::StandaloneOnly { option }.into());
    }
    if let Some(path) = request.path
        && !path_is_safe(path)
    {
        return Err(CatalogError::UnsafePath {
            path: path.to_owned(),
        }
        .into());
    }
    let name = match request.name {
        Some(name) if !name.is_empty() => name,
        _ => config_edit::name_from_url(request.url),
    };
    let existing: Vec<&crate::config_model::RepositoryCommon> = match request.kind {
        RepositoryKind::Project => workspace
            .config
            .project_repositories
            .iter()
            .map(|entry| &entry.common)
            .collect(),
        RepositoryKind::Standalone => workspace
            .config
            .standalone_repositories
            .iter()
            .map(|entry| &entry.common)
            .collect(),
    };
    for entry in existing {
        if entry.url.as_deref() == Some(request.url) {
            return Err(CatalogError::DuplicateUrl {
                kind: request.kind,
                url: request.url.to_owned(),
            }
            .into());
        }
        if entry.name == name {
            return Err(CatalogError::DuplicateName {
                kind: request.kind,
                name: name.to_owned(),
            }
            .into());
        }
    }

    let file = workspace.root.join(if local {
        LOCAL_CONFIG_FILE
    } else {
        CONFIG_FILE
    });
    write_block(writer, &file, request, local)?;
    Ok(Added {
        kind: request.kind,
        url: request.url.to_owned(),
    })
}

/// How the one update of an addition ended, when it ran.
enum Appended {
    Done,
    Missing,
    Invalid(TomlParseError),
    NotArrayOfTables,
}

/// Replaces `file` with its text and the block of `request` appended.
fn write_block(
    writer: &dyn WriteFiles,
    file: &Path,
    request: &NewBlock<'_>,
    local: bool,
) -> Result<(), AddError> {
    let mut ended = Appended::Done;
    let updated = writer.update(file, &mut |current| {
        let text = match current {
            None if local => "",
            None => {
                ended = Appended::Missing;
                return None;
            }
            Some(bytes) => {
                if let Err(error) = toml::from_slice::<toml::Table>(bytes) {
                    ended = Appended::Invalid(TomlParseError::from(error));
                    return None;
                }
                let Ok(text) = std::str::from_utf8(bytes) else {
                    return None;
                };
                text
            }
        };
        match config_edit::append_repository_block(text, request) {
            Ok(edited) => Some(edited.into_bytes()),
            Err(config_edit::NotArrayOfTables) => {
                ended = Appended::NotArrayOfTables;
                None
            }
        }
    });
    let write_failed = |cause| ConfigError::FileWrite {
        path: file.to_path_buf(),
        cause,
    };
    updated.map_err(write_failed)?;
    match ended {
        Appended::Done => Ok(()),
        Appended::Missing => Err(InternalError::unexpected(SharedFileMissing {
            path: file.to_path_buf(),
        })
        .into()),
        Appended::Invalid(error) => Err(ConfigError::FileRead {
            path: file.to_path_buf(),
            cause: FileCause::Toml(error),
        }
        .into()),
        Appended::NotArrayOfTables => Err(CatalogError::NotArrayOfTables {
            kind: request.kind,
            path: file.to_path_buf(),
        }
        .into()),
    }
}

/// A character of a word, as Python's `\w`: a letter or digit of any script, or `_`.
fn is_word(c: char) -> bool {
    c == '_' || c.is_alphanumeric()
}

/// Whether `url` is a git URL of the accepted shapes: `(http|https|ssh|git)://H[:D][/P][.git][/]` and
/// `H@H:P[.git]`, the match ending at the end of the text or before one final newline.
fn url_shape_accepted(url: &str) -> bool {
    let body = url.strip_suffix('\n').unwrap_or(url);
    scheme_form(body) || scp_form(body)
}

/// A host: one or more word characters, `.`, `+`, or `-`.
fn is_host(text: &str) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|c| is_word(c) || matches!(c, '.' | '+' | '-'))
}

/// A path of the scp form: one or more word characters, `.`, `/`, `+`, `~`, or `-`.
fn is_scp_path(text: &str) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|c| is_word(c) || matches!(c, '.' | '/' | '+' | '~' | '-'))
}

fn scheme_form(body: &str) -> bool {
    let Some(rest) = ["http://", "https://", "ssh://", "git://"]
        .iter()
        .find_map(|scheme| body.strip_prefix(scheme))
    else {
        return false;
    };
    // After `H[:D][/P]` come an optional `.git` and an optional `/`: try each way the tail can be present.
    let without_slash = rest.strip_suffix('/');
    [Some(rest), without_slash]
        .into_iter()
        .flatten()
        .flat_map(|text| [Some(text), text.strip_suffix(".git")])
        .flatten()
        .any(host_port_path)
}

/// `H[:D][/P]` with the characters of a host, decimal digits, and the characters of a path.
fn host_port_path(text: &str) -> bool {
    let (authority, path) = match text.split_once('/') {
        Some((authority, path)) => (authority, Some(path)),
        None => (text, None),
    };
    let host = match authority.split_once(':') {
        Some((host, port)) => {
            if port.is_empty() || !port.chars().all(char::is_numeric) {
                return false;
            }
            host
        }
        None => authority,
    };
    is_host(host) && path.is_none_or(is_scp_path)
}

fn scp_form(body: &str) -> bool {
    let Some((user, rest)) = body.split_once('@') else {
        return false;
    };
    let Some((host, path)) = rest.split_once(':') else {
        return false;
    };
    is_host(user) && is_host(host) && is_scp_path(path)
}

/// Whether a `--path` value is relative and has no `..` component.
fn path_is_safe(path: &str) -> bool {
    !path.starts_with('/') && !path.split('/').any(|component| component == "..")
}

/// The repository kind a type word of a target names.
fn repository_kind(word: &str) -> Option<RepositoryKind> {
    match word {
        "project" => Some(RepositoryKind::Project),
        "standalone" => Some(RepositoryKind::Standalone),
        _ => None,
    }
}

/// How an attempt to remove a block ended, when the file could be read.
#[derive(Debug, PartialEq, Eq)]
enum Removal {
    /// The file was replaced.
    Done,
    /// The file does not exist or holds no such block.
    NotFound,
    /// The key exists, but its value is not written as repository blocks.
    NotArrayOfTables,
    /// The text without the block is not the document less the block that was matched.
    Entangled,
}

/// Why an attempt to remove a block came to no answer about the block.
#[derive(Debug)]
enum Unedited {
    /// The bytes the writer handed the edit are not valid TOML.
    Invalid(TomlParseError),
    /// The writer failed: at its lock, at its read of the file, or at the replacement.
    Unwritten(std::io::Error),
}

/// What takes a block of a kind and a name out of a text: [`config_edit::remove_repository_block`].
type BlockEdit = fn(&str, RepositoryKind, &str) -> Option<BlockRemoval>;

/// Replaces `file` with its text less the first block of `kind` named `name`, and says how that ended.
///
/// A file that does not exist, and one that holds no such block, is left as it is. So is a file that is not
/// valid TOML, which is an error, as a failure of the writer is.
fn remove_block(
    writer: &dyn WriteFiles,
    file: &Path,
    kind: RepositoryKind,
    name: &str,
) -> Result<Removal, Unedited> {
    remove_block_with(
        writer,
        file,
        kind,
        name,
        config_edit::remove_repository_block,
    )
}

/// [`remove_block`] with the edit given. The edited text is handed to `writer` only when it parses to the
/// document less the block of `kind` at the position the edit names; otherwise the file is left as it is.
fn remove_block_with(
    writer: &dyn WriteFiles,
    file: &Path,
    kind: RepositoryKind,
    name: &str,
    edit: BlockEdit,
) -> Result<Removal, Unedited> {
    let mut invalid = None;
    let mut entangled = false;
    let mut not_array_of_tables = false;
    let updated = writer.update(file, &mut |current| {
        let bytes = current?;
        if let Err(error) = toml::from_slice::<toml::Table>(bytes) {
            invalid = Some(TomlParseError::from(error));
            return None;
        }
        let text = std::str::from_utf8(bytes).ok()?;
        let document = toml_edit::Document::parse(text).ok()?;
        let repositories = document.as_table().get(kind.array_key())?;
        if !repositories.is_array_of_tables() {
            not_array_of_tables = true;
            return None;
        }
        let edited = edit(text, kind, name)?;
        if !config_edit::lacks_one_block(text, &edited.text, kind, edited.index) {
            entangled = true;
            return None;
        }
        Some(edited.text.into_bytes())
    });
    let replaced = updated.map_err(Unedited::Unwritten)?;
    match invalid {
        Some(error) => Err(Unedited::Invalid(error)),
        None if not_array_of_tables => Ok(Removal::NotArrayOfTables),
        None if entangled => Ok(Removal::Entangled),
        None if replaced => Ok(Removal::Done),
        None => Ok(Removal::NotFound),
    }
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    use super::*;
    use crate::files::{Edit, MemoryFiles};
    #[test]
    fn only_the_two_types_are_repository_kinds() {
        assert_eq!(repository_kind("project"), Some(RepositoryKind::Project));
        assert_eq!(
            repository_kind("standalone"),
            Some(RepositoryKind::Standalone)
        );
        for other in ["", "Project", "projects", "other"] {
            assert_eq!(repository_kind(other), None, "{other:?}");
        }
    }

    /// One file held in memory, and how often it was replaced.
    struct Held {
        content: RefCell<Vec<u8>>,
        replacements: Cell<u32>,
    }

    impl WriteFiles for Held {
        fn update(&self, _path: &Path, edit: &mut Edit<'_>) -> std::io::Result<bool> {
            let replacement = edit(Some(&self.content.borrow()));
            Ok(replacement.is_some_and(|replacement| {
                self.content.replace(replacement);
                self.replacements.set(self.replacements.get() + 1);
                true
            }))
        }
    }

    const TWO_BLOCKS: &str = "\
[[project_repository]]
name = \"api\"

[[project_repository]]
name = \"web\"
";

    fn held() -> Held {
        held_with(TWO_BLOCKS)
    }

    fn held_with(content: &str) -> Held {
        Held {
            content: RefCell::new(content.as_bytes().to_vec()),
            replacements: Cell::new(0),
        }
    }

    /// Asserts that removing the project block `name` from `content` replaces it with `expected`.
    fn assert_removed(content: &str, name: &str, expected: &str) {
        let file = held_with(content);

        let removal = remove_block(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            name,
        );

        assert!(matches!(removal, Ok(Removal::Done)), "{removal:?}");
        assert_eq!(
            String::from_utf8_lossy(&file.content.into_inner()),
            expected
        );
        assert_eq!(file.replacements.get(), 1);
    }

    const API: &str = "[[project_repository]]\nname = \"api\"\n";
    const WEB: &str = "[[project_repository]]\nname = \"web\"\n";

    #[test]
    fn a_nan_in_another_table_does_not_keep_a_block_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let tool = format!("[tool]\nx = {nan}\ny = [1.5, {nan}]\n");

            assert_removed(&[&tool, API, WEB].concat(), "api", &[&tool, WEB].concat());
            assert_removed(&[API, &tool].concat(), "api", &tool);
        }
    }

    #[test]
    fn a_nan_in_a_kept_block_before_the_removed_one_does_not_keep_it_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let kept = format!("{WEB}weight = {nan}\n");

            assert_removed(&[&kept, API, WEB].concat(), "api", &[&kept, WEB].concat());
        }
    }

    #[test]
    fn a_nan_in_a_kept_block_after_the_removed_one_does_not_keep_it_from_being_removed() {
        for nan in ["nan", "+nan", "-nan"] {
            let kept = format!("{WEB}weight = {nan}\n");

            assert_removed(&[WEB, API, &kept].concat(), "api", &[WEB, &kept].concat());
        }
    }

    #[test]
    fn infinities_dates_and_nested_blocks_in_kept_blocks_do_not_keep_a_block_from_being_removed() {
        let before = [
            WEB,
            "high = inf\nlow = -inf\nzero = -0.0\nsince = 1979-05-27T07:32:00.5-08:00\n",
            "[[project_repository.mirror]]\nurl = \"h/one.git\"\nweight = nan\n",
            "[[project_repository.mirror]]\nurl = \"h/two.git\"\n",
        ]
        .concat();
        let after = [
            "[[project_repository]]\nname = \"docs\"\n",
            "day = 1979-05-27\nat = 07:32:00\nlocal = 1979-05-27T07:32:00\n",
            "[[project_repository.mirror]]\nurl = \"h/three.git\"\n",
            "[[project_repository.mirror.tag]]\nlow = -inf\n",
            "[[project_repository.mirror.tag]]\nhigh = +inf\n",
        ]
        .concat();

        assert_removed(
            &[&before, API, &after].concat(),
            "api",
            &[before.as_str(), after.as_str()].concat(),
        );
    }

    #[test]
    fn an_edit_that_removes_another_block_than_the_one_at_the_index_it_reports_is_refused() {
        let file = held();

        let removal = remove_block_with(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            "api",
            |text, kind, _| {
                let other = config_edit::remove_repository_block(text, kind, "web")?;
                Some(BlockRemoval {
                    text: other.text,
                    index: 0,
                })
            },
        );

        assert!(matches!(removal, Ok(Removal::Entangled)), "{removal:?}");
        assert_eq!(file.content.into_inner(), TWO_BLOCKS.as_bytes());
        assert_eq!(file.replacements.get(), 0);
    }

    #[test]
    fn an_edit_that_removes_the_one_block_replaces_the_file() {
        let file = held();

        let removal = remove_block(
            &file,
            Path::new("config.toml"),
            RepositoryKind::Project,
            "api",
        );

        assert!(matches!(removal, Ok(Removal::Done)), "{removal:?}");
        assert_eq!(
            file.content.into_inner(),
            b"[[project_repository]]\nname = \"web\"\n"
        );
        assert_eq!(file.replacements.get(), 1);
    }

    #[test]
    fn an_edit_that_would_change_another_entry_is_refused_and_nothing_is_written() {
        let edits: [BlockEdit; 3] = [
            |_, _, _| {
                Some(BlockRemoval {
                    text: String::new(),
                    index: 0,
                })
            },
            |text, _, _| {
                Some(BlockRemoval {
                    text: text
                        .replace("web", "docs")
                        .replacen("name = \"api\"", "", 1),
                    index: 0,
                })
            },
            |_, _, _| {
                Some(BlockRemoval {
                    text: "[[project_repository\n".to_owned(),
                    index: 0,
                })
            },
        ];

        for edit in edits {
            let file = held();

            let removal = remove_block_with(
                &file,
                Path::new("config.toml"),
                RepositoryKind::Project,
                "api",
                edit,
            );

            assert!(matches!(removal, Ok(Removal::Entangled)), "{removal:?}");
            assert_eq!(file.content.into_inner(), TWO_BLOCKS.as_bytes());
            assert_eq!(file.replacements.get(), 0);
        }
    }

    /// The files of a workspace held in memory by path, read and replaced through the two sides of the port.
    #[derive(Default)]
    struct Stored(RefCell<BTreeMap<PathBuf, Vec<u8>>>);

    impl Stored {
        fn with(self, path: &str, content: &str) -> Self {
            self.0
                .borrow_mut()
                .insert(path.into(), content.as_bytes().to_vec());
            self
        }

        /// The text of the file at `path`, or `None` when no such file is held.
        fn text(&self, path: &str) -> Option<String> {
            let files = self.0.borrow();
            let bytes = files.get(Path::new(path))?;
            Some(String::from_utf8_lossy(bytes).into_owned())
        }
    }

    impl ReadFiles for Stored {
        fn read(&self, path: &Path) -> std::io::Result<Option<Vec<u8>>> {
            Ok(self.0.borrow().get(path).cloned())
        }

        fn is_file(&self, path: &Path) -> bool {
            self.0.borrow().contains_key(path)
        }

        fn exists(&self, path: &Path) -> bool {
            self.is_file(path)
        }

        fn is_dir(&self, _path: &Path) -> bool {
            false
        }

        fn entries(&self, _path: &Path) -> std::io::Result<Vec<std::ffi::OsString>> {
            Err(std::io::ErrorKind::NotADirectory.into())
        }

        fn read_link(&self, _path: &Path) -> std::io::Result<Option<std::path::PathBuf>> {
            Ok(None)
        }
    }

    impl WriteFiles for Stored {
        fn update(&self, path: &Path, edit: &mut Edit<'_>) -> std::io::Result<bool> {
            let current = self.0.borrow().get(path).cloned();
            Ok(edit(current.as_deref()).is_some_and(|replacement| {
                self.0.borrow_mut().insert(path.to_path_buf(), replacement);
                true
            }))
        }
    }

    const SHARED: &str = "/ws/.winter/config.toml";
    const LOCAL: &str = "/ws/.winter/config.local.toml";

    /// A workspace at `/ws` whose shared and local files both hold [`TWO_BLOCKS`].
    fn workspace() -> Stored {
        Stored::default()
            .with(SHARED, TWO_BLOCKS)
            .with(LOCAL, TWO_BLOCKS)
    }

    fn remove_in(files: &Stored, target: &str, local: bool) -> Result<Removed, RemoveError> {
        remove(files, files, Path::new("/ws/alpha"), target, local)
    }

    #[test]
    fn a_target_without_a_slash_is_refused_with_no_workspace_above_the_start() {
        let files = MemoryFiles::default();
        let writer = held();

        let ended = remove(&files, &writer, Path::new("/elsewhere"), "nope", false);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Catalog(CatalogError::TargetShape { target })) if target == "nope"
            ),
            "{ended:?}"
        );
        assert_eq!(writer.replacements.get(), 0);
    }

    #[test]
    fn an_unknown_type_word_with_no_workspace_above_the_start_ends_on_the_missing_root() {
        let files = MemoryFiles::default();
        let writer = held();

        let ended = remove(&files, &writer, Path::new("/elsewhere"), "other/api", false);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Config(ConfigError::RootNotFound { start }))
                    if start == Path::new("/elsewhere")
            ),
            "{ended:?}"
        );
        assert_eq!(writer.replacements.get(), 0);
    }

    #[test]
    fn an_unknown_type_word_inside_a_workspace_is_refused_and_nothing_is_written() {
        let files = workspace();

        let ended = remove_in(&files, "other/api", false);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Catalog(CatalogError::UnknownKind { kind })) if kind == "other"
            ),
            "{ended:?}"
        );
        assert_eq!(files.text(SHARED).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn local_edits_the_local_file_and_leaves_the_shared_one() {
        let files = workspace();

        let ended = remove_in(&files, "project/api", true);

        assert!(ended.is_ok(), "{ended:?}");
        assert_eq!(files.text(LOCAL).as_deref(), Some(WEB));
        assert_eq!(files.text(SHARED).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn without_local_the_shared_file_is_edited_and_the_local_one_is_left() {
        let files = workspace();

        let ended = remove_in(&files, "project/api", false);

        assert!(ended.is_ok(), "{ended:?}");
        assert_eq!(files.text(SHARED).as_deref(), Some(WEB));
        assert_eq!(files.text(LOCAL).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn a_name_no_block_has_is_not_found_and_nothing_is_written() {
        let files = workspace();

        let ended = remove_in(&files, "project/docs", false);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Catalog(CatalogError::NotFound {
                    kind: RepositoryKind::Project,
                    name
                })) if name == "docs"
            ),
            "{ended:?}"
        );
        assert_eq!(files.text(SHARED).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn a_local_file_that_does_not_exist_is_not_found_and_is_not_created() {
        let files = Stored::default().with(SHARED, TWO_BLOCKS);

        let ended = remove_in(&files, "project/api", true);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Catalog(CatalogError::NotFound { .. }))
            ),
            "{ended:?}"
        );
        assert_eq!(files.text(LOCAL), None);
        assert_eq!(files.text(SHARED).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn a_file_that_is_not_valid_toml_is_a_file_read_error_on_the_edited_file() {
        let files = Stored::default().with(SHARED, "[[project_repository\n");

        let ended = remove_in(&files, "project/api", false);

        assert!(
            matches!(
                &ended,
                Err(RemoveError::Config(ConfigError::FileRead {
                    path,
                    cause: FileCause::Toml(_)
                })) if path == Path::new(SHARED)
            ),
            "{ended:?}"
        );
        assert_eq!(
            files.text(SHARED).as_deref(),
            Some("[[project_repository\n")
        );
    }

    #[test]
    fn a_file_that_is_no_longer_valid_toml_under_the_lock_is_a_file_read_error_on_the_edited_file()
    {
        for (local, edited) in [(false, SHARED), (true, LOCAL)] {
            let files = workspace();
            let under_the_lock = held_with("[[project_repository\n");

            let ended = remove(
                &files,
                &under_the_lock,
                Path::new("/ws/alpha"),
                "project/api",
                local,
            );

            assert!(
                matches!(
                    &ended,
                    Err(RemoveError::Config(ConfigError::FileRead {
                        path,
                        cause: FileCause::Toml(_)
                    })) if path == Path::new(edited)
                ),
                "{edited}: {ended:?}"
            );
            assert_eq!(under_the_lock.replacements.get(), 0, "{edited}");
            assert_eq!(
                String::from_utf8_lossy(&under_the_lock.content.into_inner()),
                "[[project_repository\n",
                "{edited}"
            );
        }
    }

    #[test]
    fn a_non_block_value_written_since_the_load_is_refused_without_replacement() {
        for (local, path) in [(false, SHARED), (true, LOCAL)] {
            for kind in [RepositoryKind::Project, RepositoryKind::Standalone] {
                let key = kind.array_key();
                for text in [
                    format!("{key} = \"x\"\n"),
                    format!("{key} = true\n"),
                    format!("{key} = 1\n"),
                    format!("{key} = {{ url = \"x\" }}\n"),
                    format!("[{key}]\nurl = \"x\"\n"),
                ] {
                    let writer = held_with(&text);
                    let ended = remove(
                        &workspace(),
                        &writer,
                        Path::new("/ws/alpha"),
                        &format!("{kind}/x"),
                        local,
                    );
                    assert!(
                        matches!(
                            &ended,
                            Err(RemoveError::Catalog(CatalogError::NotArrayOfTables { kind: actual, path: actual_path }))
                                if *actual == kind && actual_path == Path::new(path)
                        ),
                        "{text}: {ended:?}"
                    );
                    let diagnostic = ended.unwrap_err().to_string();
                    assert!(diagnostic.contains(&kind.to_string()), "{diagnostic}");
                    assert!(diagnostic.contains(path), "{diagnostic}");
                    assert_eq!(writer.replacements.get(), 0);
                    assert_eq!(*writer.content.borrow(), text.as_bytes());
                }
            }
        }
    }

    /// A writer whose every update fails with a kind, before the edit is asked anything.
    struct Failing(std::io::ErrorKind);

    impl WriteFiles for Failing {
        fn update(&self, _path: &Path, _edit: &mut Edit<'_>) -> std::io::Result<bool> {
            Err(std::io::Error::from(self.0))
        }
    }

    #[test]
    fn a_writer_that_fails_is_a_file_write_error_on_the_edited_file() {
        for (local, edited) in [(false, SHARED), (true, LOCAL)] {
            let files = workspace();
            let writer = Failing(std::io::ErrorKind::PermissionDenied);

            let ended = remove(
                &files,
                &writer,
                Path::new("/ws/alpha"),
                "project/api",
                local,
            );

            assert!(
                matches!(
                    &ended,
                    Err(RemoveError::Config(ConfigError::FileWrite { path, cause }))
                        if path == Path::new(edited)
                            && cause.kind() == std::io::ErrorKind::PermissionDenied
                ),
                "{edited}: {ended:?}"
            );
            assert_eq!(files.text(SHARED).as_deref(), Some(TWO_BLOCKS), "{edited}");
            assert_eq!(files.text(LOCAL).as_deref(), Some(TWO_BLOCKS), "{edited}");
        }
    }

    #[test]
    fn a_configuration_the_load_refuses_ends_the_removal_before_the_type_word_and_the_writer() {
        // The unknown type word would be the ending had it been checked first, and the writer's error had
        // the removal of a known type reached the writer.
        for (target, local) in [
            ("other/api", false),
            ("project/api", false),
            ("project/api", true),
        ] {
            let files = Stored::default()
                .with(SHARED, TWO_BLOCKS)
                .with(LOCAL, &["envs_per_workspace = 1\n", TWO_BLOCKS].concat());
            let writer = Failing(std::io::ErrorKind::PermissionDenied);

            let ended = remove(&files, &writer, Path::new("/ws/alpha"), target, local);

            assert!(
                matches!(
                    &ended,
                    Err(RemoveError::Config(
                        ConfigError::EnvsPerWorkspaceTooSmall { .. }
                    ))
                ),
                "{target} {local}: {ended:?}"
            );
        }
    }

    #[test]
    fn an_entry_the_file_gained_since_the_load_is_kept_by_the_removal() {
        const DOCS: &str = "\n[[project_repository]]\nname = \"docs\"\n";
        let loaded = Stored::default().with(SHARED, TWO_BLOCKS);
        let under_the_lock = held_with(&[TWO_BLOCKS, DOCS].concat());

        let ended = remove(
            &loaded,
            &under_the_lock,
            Path::new("/ws/alpha"),
            "project/api",
            false,
        );

        assert!(ended.is_ok(), "{ended:?}");
        assert_eq!(
            String::from_utf8_lossy(&under_the_lock.content.into_inner()),
            [WEB, DOCS].concat()
        );
        assert_eq!(under_the_lock.replacements.get(), 1);
        assert_eq!(loaded.text(SHARED).as_deref(), Some(TWO_BLOCKS));
    }

    #[test]
    fn success_returns_the_kind_and_the_name_of_the_target() {
        let files =
            Stored::default().with(SHARED, "[[standalone_repository]]\nname = \"tools/lint\"\n");

        let ended = remove_in(&files, "standalone/tools/lint", false);

        assert!(
            matches!(
                &ended,
                Ok(Removed {
                    kind: RepositoryKind::Standalone,
                    name
                }) if name == "tools/lint"
            ),
            "{ended:?}"
        );
        assert_eq!(files.text(SHARED).as_deref(), Some(""));
    }

    /// The list of the repositories of a workspace.
    mod listing {
        use super::super::*;
        use crate::files::MemoryFiles;
        use crate::logging::{Level, Log};

        const SHARED: &str = "/ws/.winter/config.toml";
        const LOCAL: &str = "/ws/.winter/config.local.toml";

        /// What `list` answers from `start`, and what it wrote through its diagnostics under `log`.
        fn listed_under(
            files: &MemoryFiles,
            start: &str,
            log: Log,
        ) -> (Result<Listing, ListError>, String) {
            let mut err = Vec::new();
            let mut diagnostics = Diagnostics { err: &mut err, log };
            let listing = list(files, Path::new(start), &mut diagnostics);
            (listing, String::from_utf8(err).expect("UTF-8"))
        }

        /// The entries `list` answers for the workspace `/ws`, which must have written nothing.
        fn entries(files: &MemoryFiles) -> Vec<Listed> {
            let (listing, written) = listed_under(files, "/ws", Log::DEFAULT);
            assert_eq!(written, "");
            let listing = listing.expect("a list");
            assert_eq!(listing.root, Path::new("/ws"));
            listing.entries
        }

        /// The files of the workspace `/ws` with the two configuration texts and a directory for each of
        /// `projects`.
        fn workspace(shared: &str, local: &str, projects: &[&str]) -> MemoryFiles {
            let files = MemoryFiles::default()
                .with(SHARED, shared.as_bytes())
                .with(LOCAL, local.as_bytes());
            projects.iter().fold(files, |files, name| {
                files.dir(&format!("/ws/projects/{name}"))
            })
        }

        fn project(name: &str, url: Option<&str>, pinned: bool) -> Listed {
            Listed::Project {
                name: name.to_owned(),
                url: url.map(str::to_owned),
                main_path: format!("/ws/projects/{name}").into(),
                pinned,
            }
        }

        fn standalone(name: &str, url: Option<&str>, path: &str) -> Listed {
            Listed::Standalone {
                name: name.to_owned(),
                url: url.map(str::to_owned),
                path: path.into(),
            }
        }

        #[test]
        fn a_workspace_that_declares_and_holds_nothing_lists_nothing() {
            assert_eq!(entries(&workspace("", "", &[])), []);
        }

        #[test]
        fn the_projects_of_the_shared_file_come_before_those_of_the_local_file_each_in_file_order()
        {
            let files = workspace(
                "[[project_repository]]\nname = \"web\"\n\n[[project_repository]]\nname = \"api\"\n",
                "[[project_repository]]\nname = \"mine\"\n",
                &["api", "mine", "web"],
            );

            assert_eq!(
                entries(&files),
                [
                    project("web", None, false),
                    project("api", None, false),
                    project("mine", None, false),
                ]
            );
        }

        #[test]
        fn an_entry_with_neither_a_name_nor_a_url_is_absent() {
            let files = workspace(
                "[[project_repository]]\npinned = true\n\n[[project_repository]]\nname = \"api\"\n\n\
                 [[standalone_repository]]\npath = \"tools\"\n",
                "",
                &["api"],
            );

            assert_eq!(entries(&files), [project("api", None, false)]);
        }

        #[test]
        fn an_empty_name_with_a_url_is_listed_by_the_name_of_the_url() {
            let files = workspace(
                "[[project_repository]]\nname = \"\"\nurl = \"https://fixture.invalid/api.git\"\n",
                "",
                &["api"],
            );

            assert_eq!(
                entries(&files),
                [project(
                    "api",
                    Some("https://fixture.invalid/api.git"),
                    false
                )]
            );
        }

        #[test]
        fn two_projects_of_one_resolved_name_are_both_listed() {
            let files = workspace(
                "[[project_repository]]\nname = \"api\"\n\n\
                 [[project_repository]]\nurl = \"https://fixture.invalid/api.git\"\n",
                "",
                &["api"],
            );

            assert_eq!(
                entries(&files),
                [
                    project("api", None, false),
                    project("api", Some("https://fixture.invalid/api.git"), false),
                ]
            );
        }

        #[test]
        fn a_pinned_value_is_listed_as_the_load_coerces_it() {
            let files = workspace(
                "[[project_repository]]\nname = \"one\"\npinned = 1\n\n\
                 [[project_repository]]\nname = \"zero\"\npinned = 0\n\n\
                 [[project_repository]]\nname = \"yes\"\npinned = true\n",
                "",
                &["one", "yes", "zero"],
            );

            assert_eq!(
                entries(&files),
                [
                    project("one", None, true),
                    project("zero", None, false),
                    project("yes", None, true),
                ]
            );
        }

        #[test]
        fn a_standalone_is_at_its_path_and_one_with_an_empty_path_at_its_name() {
            let files = workspace(
                "[[standalone_repository]]\nname = \"context\"\npath = \".winter/ext/context\"\n\
                 url = \"https://fixture.invalid/ctx.git\"\n\n\
                 [[standalone_repository]]\nname = \"tools\"\npath = \"\"\n\n\
                 [[standalone_repository]]\nurl = \"https://fixture.invalid/kit.git\"\n",
                "",
                &[],
            );

            assert_eq!(
                entries(&files),
                [
                    standalone(
                        "context",
                        Some("https://fixture.invalid/ctx.git"),
                        "/ws/.winter/ext/context"
                    ),
                    standalone("tools", None, "/ws/tools"),
                    standalone("kit", Some("https://fixture.invalid/kit.git"), "/ws/kit"),
                ]
            );
        }

        #[test]
        fn the_implicit_repositories_come_first_then_the_standalones_then_the_projects() {
            let files = workspace(
                "[[project_repository]]\nname = \"api\"\n\n[[standalone_repository]]\nname = \"tools\"\n",
                "",
                &["api"],
            )
            .dir("/ws/product")
            .dir("/ws/context/harness/.git");

            assert_eq!(
                entries(&files),
                [
                    Listed::Singleton {
                        name: "product",
                        path: "/ws/product".into()
                    },
                    Listed::Singleton {
                        name: "harness",
                        path: "/ws/context/harness".into()
                    },
                    standalone("tools", None, "/ws/tools"),
                    project("api", None, false),
                ]
            );
        }

        #[test]
        fn product_is_listed_for_a_directory_or_a_link_to_one_and_for_nothing_else() {
            let singletons = |files: MemoryFiles| -> Vec<&'static str> {
                entries(&files)
                    .into_iter()
                    .filter_map(|entry| match entry {
                        Listed::Singleton { name, .. } => Some(name),
                        _ => None,
                    })
                    .collect()
            };
            let bare = || workspace("", "", &[]);

            assert_eq!(singletons(bare().dir("/ws/product")), ["product"]);
            assert_eq!(
                singletons(bare().dir("/held").link("/ws/product", "/held")),
                ["product"]
            );
            assert_eq!(singletons(bare().with("/ws/product", b"")), [] as [&str; 0]);
            assert_eq!(
                singletons(bare().link("/ws/product", "/gone")),
                [] as [&str; 0]
            );
        }

        #[test]
        fn harness_is_listed_when_its_git_entry_exists_as_a_file_or_a_directory() {
            let singletons = |files: MemoryFiles| -> Vec<&'static str> {
                entries(&files)
                    .into_iter()
                    .filter_map(|entry| match entry {
                        Listed::Singleton { name, .. } => Some(name),
                        _ => None,
                    })
                    .collect()
            };
            let bare = || workspace("", "", &[]);

            assert_eq!(
                singletons(bare().dir("/ws/context/harness/.git")),
                ["harness"]
            );
            assert_eq!(
                singletons(bare().with("/ws/context/harness/.git", b"gitdir: elsewhere\n")),
                ["harness"]
            );
            assert_eq!(
                singletons(bare().dir("/ws/context/harness")),
                [] as [&str; 0]
            );
            assert_eq!(
                singletons(bare().link("/ws/context/harness/.git", "/gone")),
                [] as [&str; 0]
            );
        }

        #[test]
        fn a_location_is_joined_and_never_resolved() {
            let root = Path::new("/ws");

            for (relative, expected) in [
                ("a//b", "/ws/a/b"),
                ("./a/./b/", "/ws/a/b"),
                ("a/../b", "/ws/a/../b"),
                ("..", "/ws/.."),
                ("x/", "/ws/x"),
                ("/abs", "/abs"),
            ] {
                assert_eq!(
                    location(root, Path::new(relative)),
                    Path::new(expected),
                    "{relative}"
                );
                assert_eq!(
                    location(root, Path::new(relative)).as_os_str(),
                    expected,
                    "{relative}"
                );
            }
        }

        #[test]
        fn a_list_is_made_from_a_directory_below_the_root() {
            let files = workspace("[[project_repository]]\nname = \"api\"\n", "", &["api"]);

            let (listing, written) = listed_under(&files, "/ws/projects/api/src", Log::DEFAULT);

            assert_eq!(
                listing.expect("a list"),
                Listing {
                    root: "/ws".into(),
                    entries: vec![project("api", None, false)],
                }
            );
            assert_eq!(written, "");
        }

        #[test]
        fn with_drift_the_notice_and_nothing_else_is_written_whatever_the_log_asks_for() {
            let files = workspace(
                "[[project_repository]]\nname = \"web\"\n\n[[project_repository]]\nname = \"api\"\n",
                "",
                &["old", ".hidden"],
            );

            for log in [
                Log::DEFAULT,
                Log {
                    threshold: Level::Debug,
                    switched: true,
                },
                Log {
                    threshold: Level::Critical,
                    switched: true,
                },
            ] {
                let (listing, written) = listed_under(&files, "/ws", log);

                assert_eq!(
                    listing.expect("a list").entries,
                    [project("web", None, false), project("api", None, false)],
                    "{log:?}"
                );
                assert_eq!(
                    written,
                    "warning: Config and projects/ have drifted — missing from projects/: api, web; \
                     undeclared in config: old\n",
                    "{log:?}"
                );
            }
        }

        #[test]
        fn no_workspace_above_the_start_and_a_refused_configuration_are_the_load_s_errors() {
            let (outside, written) =
                listed_under(&workspace("", "", &[]), "/elsewhere", Log::DEFAULT);
            assert!(
                matches!(
                    &outside,
                    Err(ListError::Config(ConfigError::RootNotFound { start })) if start == Path::new("/elsewhere")
                ),
                "{outside:?}"
            );
            assert_eq!(written, "");

            let refusing = workspace("project_repository = 1\n", "", &["old"]);
            let (refused, written) = listed_under(&refusing, "/ws", Log::DEFAULT);
            assert!(
                matches!(
                    &refused,
                    Err(ListError::Config(ConfigError::RepositoriesNotArray { .. }))
                ),
                "{refused:?}"
            );
            assert_eq!(written, "");
        }

        #[test]
        fn a_projects_directory_that_cannot_be_listed_is_an_internal_failure_with_nothing_written()
        {
            let files = workspace("", "", &[])
                .failing_dir("/ws/projects", std::io::ErrorKind::PermissionDenied);

            let (listing, written) = listed_under(&files, "/ws", Log::DEFAULT);

            assert!(
                matches!(
                    &listing,
                    Err(ListError::Internal(InternalError::Unexpected { .. }))
                ),
                "{listing:?}"
            );
            assert_eq!(written, "");
        }
    }

    mod addition {
        use super::*;
        use crate::config_edit::{NewBlock, StandaloneOption};

        fn request(url: &str) -> NewBlock<'_> {
            NewBlock {
                kind: RepositoryKind::Project,
                name: None,
                url,
                main_branch: None,
                pinned: false,
                path: None,
                prefix: None,
                r#ref: None,
                git_excludes: &[],
                cmd: &[],
            }
        }

        fn add_in(files: &Stored, block: &NewBlock<'_>, local: bool) -> Result<Added, AddError> {
            add(files, files, Path::new("/ws/alpha"), block, local)
        }

        fn catalog(ended: Result<Added, AddError>) -> CatalogError {
            match ended {
                Err(AddError::Catalog(error)) => error,
                other => panic!("expected a catalog error, got {other:?}"),
            }
        }

        #[test]
        fn the_url_shape_accepts_the_spec_forms() {
            for url in [
                "https://h.invalid/a/b.git",
                "http://h.invalid",
                "ssh://h.invalid:22/a",
                "git://h.invalid/a.git/",
                "https://h.invalid:8080",
                "https://h.invalid:8080.git",
                "git@h.invalid:a/b.git",
                "a@b:c",
                "https://h\u{e9}st.invalid/a",
                "https://h.invalid/a.git\n",
                "https://h.invalid/a/",
            ] {
                assert!(url_shape_accepted(url), "{url:?}");
            }
        }

        #[test]
        fn the_url_shape_refuses_everything_else() {
            for url in [
                "",
                "ssh://user@h.invalid/path",
                "https://h.invalid/a b",
                "https://h.invalid/a\n\n",
                "ftp://h.invalid/a",
                "https://",
                "https://h.invalid:/a",
                "https://h.invalid:x/a",
                "h.invalid:a",
                "a@b:",
                " https://h.invalid/a",
            ] {
                assert!(!url_shape_accepted(url), "{url:?}");
            }
        }

        #[test]
        fn the_path_rule_refuses_absolute_and_dot_dot_components() {
            for path in ["/x", "a/../b", "a/..", ".."] {
                assert!(!path_is_safe(path), "{path:?}");
            }
            for path in ["", "..a", "a..b/c", "a/b", "./a"] {
                assert!(path_is_safe(path), "{path:?}");
            }
        }

        #[test]
        fn a_project_is_appended_to_the_shared_file_only() {
            let files = workspace();

            let added = add_in(&files, &request("https://h.invalid/new.git"), false);

            assert_eq!(
                added.expect("added"),
                Added {
                    kind: RepositoryKind::Project,
                    url: "https://h.invalid/new.git".to_owned()
                }
            );
            assert!(files.text(SHARED).expect("file").contains("new.git"));
            assert_eq!(files.text(LOCAL).as_deref(), Some(TWO_BLOCKS));
        }

        #[test]
        fn the_checks_run_in_order() {
            let files = workspace();
            let mut block = request("nope");
            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::InvalidUrl { .. }
            ));

            block = request("https://h.invalid/new.git");
            block.kind = RepositoryKind::Standalone;
            block.pinned = true;
            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::PinnedStandalone
            ));

            let mut block = request("https://h.invalid/new.git");
            block.path = Some("/x");
            block.r#ref = Some("r");
            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::StandaloneOnly {
                    option: StandaloneOption::Path
                }
            ));
            block.path = None;
            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::StandaloneOnly {
                    option: StandaloneOption::Ref
                }
            ));

            let mut block = request("https://h.invalid/new.git");
            block.kind = RepositoryKind::Standalone;
            block.path = Some("../x");
            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::UnsafePath { .. }
            ));
        }

        #[test]
        fn a_declared_name_is_refused_and_the_url_test_comes_first() {
            let files = Stored::default()
                .with(
                    SHARED,
                    "[[project_repository]]\nurl = \"https://h.invalid/api.git\"\n",
                )
                .with(LOCAL, "");

            let by_name = add_in(&files, &request("https://other.invalid/api"), false);
            let by_url = add_in(&files, &request("https://h.invalid/api.git"), false);
            let named = NewBlock {
                name: Some("api"),
                ..request("https://h.invalid/api.git")
            };
            let both = add_in(&files, &named, false);
            let other_kind = NewBlock {
                kind: RepositoryKind::Standalone,
                ..request("https://h.invalid/api.git")
            };

            assert!(matches!(
                catalog(by_name),
                CatalogError::DuplicateName { .. }
            ));
            assert!(matches!(catalog(by_url), CatalogError::DuplicateUrl { .. }));
            assert!(matches!(catalog(both), CatalogError::DuplicateUrl { .. }));
            assert!(add_in(&files, &other_kind, false).is_ok());
        }

        #[test]
        fn an_empty_name_falls_back_to_the_derived_one_for_the_duplicate_test() {
            let files = workspace();
            let block = NewBlock {
                name: Some(""),
                ..request("https://h.invalid/api")
            };

            assert!(matches!(
                catalog(add_in(&files, &block, false)),
                CatalogError::DuplicateName { name, .. } if name == "api"
            ));
        }

        #[test]
        fn the_local_file_is_created_when_absent_and_the_shared_one_must_exist() {
            let files = Stored::default().with(SHARED, "");
            assert!(add_in(&files, &request("https://h.invalid/a"), true).is_ok());
            assert!(
                files
                    .text(LOCAL)
                    .expect("created")
                    .contains("[[project_repository]]")
            );
            assert_eq!(files.text(SHARED).as_deref(), Some(""));

            let held = Stored::default().with(SHARED, "");
            let vanished = Held {
                content: RefCell::new(Vec::new()),
                replacements: Cell::new(0),
            };
            struct Gone(Held);
            impl WriteFiles for Gone {
                fn update(&self, _: &Path, edit: &mut Edit<'_>) -> std::io::Result<bool> {
                    Ok(edit(None).is_some_and(|_| {
                        self.0.replacements.set(1);
                        true
                    }))
                }
            }
            let gone = Gone(vanished);

            let ended = add(
                &held,
                &gone,
                Path::new("/ws/alpha"),
                &request("https://h.invalid/a"),
                false,
            );

            assert!(
                matches!(
                    &ended,
                    Err(AddError::Internal(InternalError::Unexpected { .. }))
                ),
                "{ended:?}"
            );
            assert_eq!(gone.0.replacements.get(), 0);
        }

        #[test]
        fn a_writer_that_fails_is_a_file_write_error_naming_the_file() {
            let files = workspace();
            let writer = Failing(std::io::ErrorKind::PermissionDenied);

            let ended = add(
                &files,
                &writer,
                Path::new("/ws/alpha"),
                &request("https://h.invalid/a"),
                true,
            );

            assert!(
                matches!(&ended, Err(AddError::Config(ConfigError::FileWrite { path, .. }))
                    if path == Path::new(LOCAL)),
                "{ended:?}"
            );
        }

        #[test]
        fn an_inline_array_is_refused_with_the_file_unchanged() {
            let files = Stored::default()
                .with(SHARED, "project_repository = []\n")
                .with(LOCAL, "");

            let ended = add_in(&files, &request("https://h.invalid/a"), false);

            assert!(matches!(
                catalog(ended),
                CatalogError::NotArrayOfTables { .. }
            ));
            assert_eq!(
                files.text(SHARED).as_deref(),
                Some("project_repository = []\n")
            );
        }
    }
}
