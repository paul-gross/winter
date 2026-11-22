//! Resolving a declared ref to the kind of thing it names and the commit it stands for.

use std::path::Path;

use crate::config_lock::RefKind;
use crate::git::{self, GitFailure, REPO_ATTRIBUTE, REPO_ERROR_TYPE, REV_PARSE_SPAN, Sha};
use crate::pins::Ports;
use crate::termination::{RepoError, RepoMessage};

/// What a ref resolved to: what it names, and the id it records.
///
/// For an annotated tag, `commit` is the id of the tag object, as `rev-parse --verify refs/tags/<tag>` answers
/// it: it never equals `HEAD`, and a comparison with `HEAD` reads it as moved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolution {
    pub kind: RefKind,
    pub commit: Sha,
}

/// Resolves `ref` in the checkout at `path`, the checkout of the repository `repo`, without the network.
///
/// The first match wins: the remote branch `origin/<ref>`, then the tag `<ref>`, then a commit. A ref that
/// names none is a [`RepoMessage::UnresolvableRef`] error. One `git rev-parse` span covers every verify.
pub fn resolve(
    ports: &Ports<'_>,
    repo: &str,
    path: &Path,
    r#ref: &str,
) -> Result<Resolution, GitFailure> {
    let mut span = ports
        .tracer
        .operation(REV_PARSE_SPAN, &[(REPO_ATTRIBUTE, repo)]);
    let candidates = [
        (RefKind::Branch, format!("refs/remotes/origin/{}", r#ref)),
        (RefKind::Tag, format!("refs/tags/{}", r#ref)),
        (RefKind::Commit, format!("{}^{{commit}}", r#ref)),
    ];
    for (kind, candidate) in candidates {
        if let Some(commit) = git::verified(ports.programs, path, &candidate)? {
            return Ok(Resolution { kind, commit });
        }
    }
    span.failed(REPO_ERROR_TYPE);
    Err(GitFailure::Repo(Box::new(RepoError {
        message: RepoMessage::UnresolvableRef {
            r#ref: r#ref.to_owned(),
            path: path.to_path_buf(),
        },
        program: None,
        subcommand: None,
        args: Vec::new(),
        cwd: Some(path.to_path_buf()),
        exit: None,
        stderr: None,
    })))
}
