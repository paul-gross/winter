//! The environment variables of `[env]`, by band.

use toml::Table;

use super::EnvBands;
use crate::termination::ConfigError;

/// The env bands of `merged`.
///
/// No band is read yet: every band is empty, whatever the table holds.
pub(super) fn env_bands(_merged: &Table) -> Result<EnvBands, ConfigError> {
    Ok(EnvBands {
        workspace: Vec::new(),
        feature: Vec::new(),
        named: Vec::new(),
    })
}
