//! The content fingerprint of a workspace definition: the tree of the tracked content of the workspace
//! repository and of each standalone, and the digest of those trees and the standalones' names.
//!
//! Untracked files, history, and where a repository was cloned from are outside it, so two checkouts of one
//! definition have one digest.

use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::config_load;
use crate::files::{ReadFiles, ScratchFiles};
use crate::git::{self, GitFailure, Sha};
use crate::programs::RunPrograms;
use crate::termination::{ConfigError, InternalError, RepoError, RepoMessage};
use crate::trace::CommandTracer;
use crate::workspace_files::location;

/// What one repository contributes: its commit, the tree of its tracked content, and whether a tracked file
/// differs from `HEAD`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RepoFingerprint {
    pub name: String,
    pub commit: Sha,
    pub tree: Sha,
    pub dirty: bool,
}

/// The digest of a workspace and the parts it was made from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WorkspaceFingerprint {
    pub digest: String,
    pub workspace: RepoFingerprint,
    /// In order of name.
    pub standalones: Vec<RepoFingerprint>,
}

/// Why the fingerprint was not computed.
#[derive(Debug, thiserror::Error)]
pub(crate) enum FingerprintError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    /// A checkout is missing, or a git call failed.
    #[error(transparent)]
    Repo(Box<RepoError>),
    #[error(transparent)]
    Internal(Box<InternalError>),
}

impl From<GitFailure> for FingerprintError {
    fn from(failure: GitFailure) -> Self {
        match failure {
            GitFailure::Repo(error) => Self::Repo(error),
            GitFailure::Internal(error) => Self::Internal(error),
        }
    }
}

/// The fingerprint of the workspace of `start`: the workspace repository first, then each standalone in
/// order of name, each through `HEAD`, the tracked-content tree, and the tracked status. Scratch indexes are
/// made under `temp`.
pub(crate) fn compute(
    files: &dyn ReadFiles,
    scratch: &dyn ScratchFiles,
    programs: &dyn RunPrograms,
    tracer: &dyn CommandTracer,
    temp: &Path,
    start: &Path,
) -> Result<WorkspaceFingerprint, FingerprintError> {
    let loaded = config_load::load(files, start)?;
    let root = loaded.root;
    let name = root
        .file_name()
        .map_or_else(String::new, |name| name.to_string_lossy().into_owned());
    let observe = |name: &str, path: &Path| -> Result<RepoFingerprint, FingerprintError> {
        if !files.is_dir(path) {
            return Err(FingerprintError::Repo(Box::new(RepoError {
                message: RepoMessage::MissingCheckout {
                    name: name.to_owned(),
                    path: path.to_path_buf(),
                },
                program: None,
                subcommand: None,
                args: Vec::new(),
                cwd: Some(path.to_path_buf()),
                exit: None,
                stderr: None,
            })));
        }
        let commit = git::head(programs, tracer, name, path)?;
        let tree = git::tree(files, scratch, programs, tracer, temp, name, path)?;
        let dirty = git::dirty(programs, tracer, name, path)?;
        Ok(RepoFingerprint {
            name: name.to_owned(),
            commit,
            tree,
            dirty,
        })
    };

    let workspace = observe(&name, &root)?;
    let mut declared: Vec<(&str, PathBuf)> = loaded
        .config
        .standalone_repositories
        .iter()
        .map(|standalone| {
            let name = standalone.common.name.as_str();
            let relative = if standalone.path.is_empty() {
                name
            } else {
                standalone.path.as_str()
            };
            (name, location(&root, Path::new(relative)))
        })
        .collect();
    declared.sort_by(|left, right| left.0.cmp(right.0));
    let standalones = declared
        .iter()
        .map(|(name, path)| observe(name, path))
        .collect::<Result<Vec<_>, _>>()?;
    let digest = digest(&workspace.tree, &standalones);
    Ok(WorkspaceFingerprint {
        digest,
        workspace,
        standalones,
    })
}

/// The digest of the tree of the workspace repository and of the standalones, which are in order of name.
///
/// The SHA-256 of the canonical preimage
/// `{"standalones":[{"name":..,"tree":..},..],"version":1,"workspace":..}`: keys in order, no whitespace,
/// strings escaped as an ASCII-only JSON encoder escapes them. Written by hand because the escaping of a
/// general JSON encoder, and the order of its keys, are the encoder's to change.
pub(crate) fn digest(workspace_tree: &str, standalones: &[RepoFingerprint]) -> String {
    let preimage = preimage(workspace_tree, standalones);
    let hashed = Sha256::digest(preimage.as_bytes());
    hashed.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn preimage(workspace_tree: &str, standalones: &[RepoFingerprint]) -> String {
    let mut text = String::from("{\"standalones\":[");
    for (position, standalone) in standalones.iter().enumerate() {
        if position > 0 {
            text.push(',');
        }
        text.push_str("{\"name\":");
        quote(&mut text, &standalone.name);
        text.push_str(",\"tree\":");
        quote(&mut text, &standalone.tree);
        text.push('}');
    }
    text.push_str("],\"version\":1,\"workspace\":");
    quote(&mut text, workspace_tree);
    text.push('}');
    text
}

/// Appends `value` as a JSON string with every character outside `0x20..=0x7e` escaped: the short form
/// where JSON has one, else `\uXXXX` in lowercase hex, a character beyond the basic plane as its surrogate pair.
fn quote(text: &mut String, value: &str) {
    text.push('"');
    for character in value.chars() {
        match character {
            '"' => text.push_str("\\\""),
            '\\' => text.push_str("\\\\"),
            '\n' => text.push_str("\\n"),
            '\r' => text.push_str("\\r"),
            '\t' => text.push_str("\\t"),
            '\u{8}' => text.push_str("\\b"),
            '\u{c}' => text.push_str("\\f"),
            ' '..='~' => text.push(character),
            _ => {
                let mut units = [0u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    text.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    text.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    const WORKSPACE: &str = "1111111111111111111111111111111111111111";
    const TREE: &str = "2222222222222222222222222222222222222222";

    fn standalone(name: &str, tree: &str) -> RepoFingerprint {
        RepoFingerprint {
            name: name.to_owned(),
            commit: String::new(),
            tree: tree.to_owned(),
            dirty: false,
        }
    }

    #[test]
    fn the_digest_of_an_ascii_name_is_the_one_the_reference_encoder_gives() {
        let standalones = [standalone("api", TREE)];

        assert_eq!(
            digest(WORKSPACE, &standalones),
            "feadf5d3f1d68b6eb6377a96c4d8c427217632b0f69543a4edd1e150c3520c45"
        );
    }

    #[test]
    fn the_digest_of_a_name_in_the_basic_plane_is_the_one_the_reference_encoder_gives() {
        let standalones = [standalone("é", TREE)];

        assert_eq!(
            preimage(WORKSPACE, &standalones),
            format!(
                "{{\"standalones\":[{{\"name\":\"\\u00e9\",\"tree\":\"{TREE}\"}}],\"version\":1,\"workspace\":\"{WORKSPACE}\"}}"
            )
        );
        assert_eq!(
            digest(WORKSPACE, &standalones),
            "27abbae9adb07db0bf4abf8a29ccb7b1c73d6957a181bfc4519a7f7e588365be"
        );
    }

    #[test]
    fn the_digest_of_a_name_beyond_the_basic_plane_is_the_one_the_reference_encoder_gives() {
        let standalones = [standalone("\u{1d11e}", TREE)];

        assert_eq!(
            preimage(WORKSPACE, &standalones),
            format!(
                "{{\"standalones\":[{{\"name\":\"\\ud834\\udd1e\",\"tree\":\"{TREE}\"}}],\"version\":1,\"workspace\":\"{WORKSPACE}\"}}"
            )
        );
        assert_eq!(
            digest(WORKSPACE, &standalones),
            "462446e8ef1f24cf66b64e1c8ba526c5856288c715d23ce74ff03c8a82a2e906"
        );
    }

    #[test]
    fn the_digest_of_no_standalone_is_the_one_the_reference_encoder_gives() {
        assert_eq!(
            digest(WORKSPACE, &[]),
            "e43d3b18fc7db042c648992fd331a91af18ec0328c4d52e60ac5d1f5f63144c7"
        );
    }

    #[test]
    fn a_quote_a_backslash_a_newline_and_a_delete_are_escaped() {
        let standalones = [standalone("a\"b\\c\n\u{7f}", &"3".repeat(40))];

        assert_eq!(
            digest(WORKSPACE, &standalones),
            "befef2afe6f6170adc5de2f184237c207fb7f202bbc21bc2a2ed7ebc4eb69ad6"
        );
    }
}
