//! The workspace builder: a fixture workspace under the workspace directory of a tree, from literal inputs.

use std::path::PathBuf;

use crate::git::{CommitSpec, Git, GitError};
use crate::tree::TempTree;

/// The default branch of every remote the builder makes, and the branch of the workspace root repository.
const DEFAULT_BRANCH: &str = "main";

/// What an env worktree's branch starts from.
const WORKTREE_START: &str = "origin/main";

/// The message of the one commit of the workspace root repository.
const ROOT_COMMIT_MESSAGE: &str = "Workspace root";

/// Collects what a fixture workspace holds; [`WorkspaceBuilder::build`] makes it.
#[derive(Debug)]
pub struct WorkspaceBuilder<'t> {
    tree: &'t TempTree,
    /// Path relative to the workspace directory and content, in the order given.
    files: Vec<(String, Vec<u8>)>,
    remotes: Vec<(String, Vec<CommitSpec>)>,
    projects: Vec<String>,
    /// Env and project.
    env_worktrees: Vec<(String, String)>,
    gitignore: Option<String>,
}

/// A built fixture workspace.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Workspace {
    /// The workspace directory.
    pub root: PathBuf,
}

impl<'t> WorkspaceBuilder<'t> {
    /// A builder of an empty workspace in the workspace directory of `tree`.
    pub fn new(tree: &'t TempTree) -> Self {
        Self {
            tree,
            files: Vec::new(),
            remotes: Vec::new(),
            projects: Vec::new(),
            env_worktrees: Vec::new(),
            gitignore: None,
        }
    }

    /// `.winter/config.toml`, verbatim.
    pub fn config(self, toml: &str) -> Self {
        self.file(".winter/config.toml", toml.as_bytes())
    }

    /// `.winter/config.local.toml`, verbatim.
    pub fn local_config(self, toml: &str) -> Self {
        self.file(".winter/config.local.toml", toml.as_bytes())
    }

    /// `.winter/state.toml`, verbatim.
    pub fn state(self, toml: &str) -> Self {
        self.file(".winter/state.toml", toml.as_bytes())
    }

    /// `.winter/config.lock`, verbatim.
    pub fn lock(self, toml: &str) -> Self {
        self.file(".winter/config.lock", toml.as_bytes())
    }

    /// The file `relative`, a path from the workspace directory, with `content`. Of two files given for one
    /// path the later is the one the workspace holds.
    pub fn file(mut self, relative: &str, content: &[u8]) -> Self {
        self.files.push((relative.to_owned(), content.to_vec()));
        self
    }

    /// The bare remote `name` of the tree, built from `commits`, with the default branch `main`.
    pub fn remote(mut self, name: &str, commits: Vec<CommitSpec>) -> Self {
        self.remotes.push((name.to_owned(), commits));
        self
    }

    /// A clone of the remote `name` at `projects/<name>`.
    pub fn project(mut self, name: &str) -> Self {
        self.projects.push(name.to_owned());
        self
    }

    /// The worktree `<env>/<project>` of the project `project`, on the new branch `env` made from
    /// `origin/main`.
    pub fn env_worktree(mut self, env: &str, project: &str) -> Self {
        self.env_worktrees
            .push((env.to_owned(), project.to_owned()));
        self
    }

    /// Makes the workspace directory a git repository with one commit, which holds `.gitignore` with the
    /// content `gitignore` and every file of the workspace that it does not exclude. A repository inside the
    /// workspace that it does not exclude, a project clone or an env worktree, is recorded as git records a
    /// nested repository: one entry that names the commit its `HEAD` is on, and none of its files.
    pub fn root_repository(mut self, gitignore: &str) -> Self {
        self.gitignore = Some(gitignore.to_owned());
        self
    }

    /// Builds the workspace: the workspace root repository, then remotes, then files, then projects, then
    /// env worktrees, then the commit of the workspace root repository. The repository is created first,
    /// while the workspace directory holds nothing, as a place a repository is created in must. Nothing the
    /// builder does resolves a host name.
    pub fn build(self) -> Result<Workspace, GitError> {
        let git = Git::new(self.tree)?;
        let root = self.tree.workspace_dir();

        if self.gitignore.is_some() {
            git.init(&root, DEFAULT_BRANCH)?;
        }
        for (name, commits) in &self.remotes {
            git.bare_remote(name, DEFAULT_BRANCH, commits)?;
        }
        for (relative, content) in &self.files {
            git.write_file(&root, relative, content)?;
        }
        for name in &self.projects {
            git.clone_remote(name, &root.join("projects").join(name))?;
        }
        for (env, project) in &self.env_worktrees {
            let repository = root.join("projects").join(project);
            let worktree = root.join(env).join(project);
            git.add_worktree(&repository, &worktree, env, WORKTREE_START)?;
        }
        if let Some(gitignore) = self.gitignore {
            let spec = CommitSpec {
                message: ROOT_COMMIT_MESSAGE.to_owned(),
                files: vec![(".gitignore".to_owned(), gitignore.into_bytes())],
                ..CommitSpec::default()
            };
            git.commit(&root, &spec)?;
        }
        Ok(Workspace { root })
    }
}
