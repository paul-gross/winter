//! The model tiers and the model overrides of agents.

use toml::Table;

use super::{AgentOverride, ModelTier};
use crate::termination::ConfigError;

/// The model tiers of `merged`.
///
/// No tier is read yet and no tier is built in: the list is empty, whatever the table holds.
pub(super) fn model_tiers(_merged: &Table) -> Result<Vec<ModelTier>, ConfigError> {
    Ok(Vec::new())
}

/// The model override of each agent of `merged`. `tiers` are the tiers an override may name by label.
///
/// No override is read yet: the list is empty, whatever the table holds.
pub(super) fn agent_model_overrides(
    _merged: &Table,
    _tiers: &[ModelTier],
) -> Result<Vec<(String, AgentOverride)>, ConfigError> {
    Ok(Vec::new())
}
