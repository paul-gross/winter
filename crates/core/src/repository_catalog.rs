//! The repository catalog of a workspace: the removal of one repository block from a configuration file.

use std::path::Path;

use crate::config;
use crate::config_edit::{self, BlockRemoval, CatalogError, RepositoryKind};
use crate::files::{ReadFiles, WriteFiles};
use crate::termination::{ConfigError, FileCause, TomlParseError};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE};
use crate::workspace_root;

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
/// A target without `/` is refused before any file is read. Then the workspace root is found, then the
/// configuration is loaded and its first refusal ends the removal, then the type is checked to be one of the
/// two words, and then the first block of that type whose effective name is the name is removed from the
/// one file. Only that file is searched, and it is replaced only when a block was removed.
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
    let root = workspace_root::find(files, start)?;
    config::load(files, &root)?;
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
        Ok(Removal::Entangled) => Err(CatalogError::Entangled {
            kind,
            name: name.to_owned(),
        }
        .into()),
        Err(cause) => Err(ConfigError::FileRead { path: file, cause }.into()),
    }
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
    /// The text without the block is not the document less the block that was matched.
    Entangled,
}

/// What takes a block of a kind and a name out of a text: [`config_edit::remove_repository_block`].
type BlockEdit = fn(&str, RepositoryKind, &str) -> Option<BlockRemoval>;

/// Replaces `file` with its text less the first block of `kind` named `name`, and says how that ended.
///
/// A file that does not exist, and one that holds no such block, is left as it is. So is a file that is not
/// valid TOML, which is the error.
fn remove_block(
    writer: &dyn WriteFiles,
    file: &Path,
    kind: RepositoryKind,
    name: &str,
) -> Result<Removal, FileCause> {
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
) -> Result<Removal, FileCause> {
    let mut invalid = None;
    let mut entangled = false;
    let replaced = writer.update(file, &mut |current| {
        let bytes = current?;
        if let Err(error) = toml::from_slice::<toml::Table>(bytes) {
            invalid = Some(TomlParseError::from(error));
            return None;
        }
        let text = std::str::from_utf8(bytes).ok()?;
        let edited = edit(text, kind, name)?;
        if !config_edit::lacks_one_block(text, &edited.text, kind, edited.index) {
            entangled = true;
            return None;
        }
        Some(edited.text.into_bytes())
    })?;
    match invalid {
        Some(error) => Err(FileCause::Toml(error)),
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
}
