//! The creation of a workspace in a directory.

use std::path::{Path, PathBuf};

use super::BootstrapError;
use crate::agents_md::{self, Placement, Shim};
use crate::files::{ReadFiles, WriteFiles};
use crate::termination::ConfigError;

pub struct Created {
    pub root: PathBuf,
    pub agents_md: Placement,
    pub claude_md: Shim,
}

#[derive(Debug, thiserror::Error)]
pub enum CreateError {
    #[error("{0}")]
    Bootstrap(#[from] BootstrapError),
    #[error("{0}")]
    Config(#[source] Box<ConfigError>),
}

pub fn create(
    writer: &dyn WriteFiles,
    files: &dyn ReadFiles,
    cwd: &Path,
    version: &str,
) -> Result<Created, CreateError> {
    let path = cwd.join(".winter/config.toml");
    let conflict = || BootstrapError::AlreadyInitialized { path: path.clone() };
    if files.exists(&path) || files.read_link(&path).ok().flatten().is_some() {
        return Err(conflict().into());
    }
    let write_error = |path: PathBuf, cause| {
        CreateError::Config(Box::new(ConfigError::FileWrite { path, cause }))
    };
    writer
        .create_dir_all(&cwd.join(".winter"))
        .map_err(|e| write_error(path.clone(), e))?;
    let content = format!(
        "[versions]\nworkspace = {}\n",
        toml::Value::String(version.to_owned())
    );
    let mut appeared = false;
    writer
        .update(&path, &mut |current| {
            if current.is_some() {
                appeared = true;
                None
            } else {
                Some(content.as_bytes().to_vec())
            }
        })
        .map_err(|e| write_error(path.clone(), e))?;
    if appeared {
        return Err(conflict().into());
    }
    let agents_md =
        agents_md::place(writer, cwd).map_err(|e| write_error(cwd.join("AGENTS.md"), e))?;
    let claude_md =
        agents_md::shim(writer, cwd).map_err(|e| write_error(cwd.join("CLAUDE.md"), e))?;
    Ok(Created {
        root: cwd.to_owned(),
        agents_md,
        claude_md,
    })
}
