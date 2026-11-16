//! The model tiers and the model overrides of agents: `[model_tiers]` laid over the built-in tier table, and
//! `[agent_model_overrides]` read against the result.

use toml::{Table, Value};

use super::{AgentOverride, ModelTier, Vendor, VendorModel, value_type};
use crate::termination::{AgentOverrideFault, ConfigError, ModelTierFault};

/// The built-in tiers: each label with its model id for claude, codex, and opencode, in that order.
const BUILT_IN_TIERS: [(&str, [&str; 3]); 4] = [
    (
        "fable",
        ["fable", "gpt-6-astra", "anthropic/claude-fable-5-1"],
    ),
    ("opus", ["opus", "gpt-6.1-sol", "anthropic/claude-opus-5-5"]),
    (
        "sonnet",
        ["sonnet", "gpt-6.1-terra", "anthropic/claude-sonnet-5-5"],
    ),
    (
        "haiku",
        ["haiku", "gpt-6-luna", "anthropic/claude-haiku-4-5"],
    ),
];

const PROFILE_MODEL: &str = "model";
const PROFILE_EFFORT: &str = "effort";

/// The effective model tiers of `merged`: the built-in tiers with `[model_tiers]` laid over them.
///
/// A label of the table that is a built-in one replaces the model id of each vendor it names and keeps the
/// others; any other label adds a tier that holds the vendors it names alone. The built-in tiers come first,
/// then the added ones in the order of the table. A `model_tiers` that is absent or not a table gives the
/// built-in tiers alone.
///
/// A label is refused with [`ConfigError::ModelTier`] when its value is not a table, when it names no vendor,
/// and at the first of its vendors, in the order of the table, that is not a vendor or whose model id is not
/// a non-empty string. A model id made of white space alone is accepted.
pub(super) fn model_tiers(merged: &Table) -> Result<Vec<ModelTier>, ConfigError> {
    let mut tiers = built_in_tiers();
    let Some(Value::Table(declared)) = merged.get("model_tiers") else {
        return Ok(tiers);
    };
    for (label, value) in declared {
        let refused = |fault| ConfigError::ModelTier {
            label: label.clone(),
            fault,
        };
        let Value::Table(vendors) = value else {
            return Err(refused(ModelTierFault::NotVendorTable {
                found: value_type(value),
            }));
        };
        if vendors.is_empty() {
            return Err(refused(ModelTierFault::NoVendor));
        }
        let at = match tiers.iter().position(|tier| tier.label == *label) {
            Some(at) => at,
            None => {
                tiers.push(ModelTier {
                    label: label.clone(),
                    claude: None,
                    codex: None,
                    opencode: None,
                });
                tiers.len() - 1
            }
        };
        let tier = &mut tiers[at];
        for (name, model_id) in vendors {
            let Some(vendor) = vendor(name) else {
                return Err(refused(ModelTierFault::UnknownVendor {
                    vendor: name.clone(),
                }));
            };
            let model_id = match model_id {
                Value::String(model_id) if !model_id.is_empty() => model_id.clone(),
                _ => {
                    return Err(refused(ModelTierFault::ModelIdNotString {
                        vendor: name.clone(),
                        found: value_type(model_id),
                    }));
                }
            };
            *match vendor {
                Vendor::Claude => &mut tier.claude,
                Vendor::Codex => &mut tier.codex,
                Vendor::Opencode => &mut tier.opencode,
            } = Some(model_id);
        }
    }
    Ok(tiers)
}

/// The model override of each agent of `merged`, in the order of `[agent_model_overrides]`. `tiers` are the
/// tiers an override may name by label.
///
/// An override is a string, the label of one of `tiers`, or a table that gives each vendor it names a model
/// id or a profile, a table of `model` and `effort`. A value that is not a table gives no override here; the
/// load refuses one in the file that holds it, before the two files are merged.
///
/// An agent is refused with [`ConfigError::AgentOverride`] at the first fault of its value: a blank string,
/// a label no tier has, a value that is neither a string nor a table, or a table that names no vendor; and,
/// at the first vendor at fault in the order of the table, a name that is not a vendor, a blank model id, a
/// value that is neither a string nor a table, or a profile with a key other than its two, with a `model` or
/// an `effort` that is not a string or is blank, or with neither. Blank is what [`is_blank`] says.
pub(super) fn agent_model_overrides(
    merged: &Table,
    tiers: &[ModelTier],
) -> Result<Vec<(String, AgentOverride)>, ConfigError> {
    let Some(Value::Table(declared)) = merged.get("agent_model_overrides") else {
        return Ok(Vec::new());
    };
    declared
        .iter()
        .map(|(agent, value)| match agent_override(value, tiers) {
            Ok(read) => Ok((agent.clone(), read)),
            Err(fault) => Err(ConfigError::AgentOverride {
                agent: agent.clone(),
                fault,
            }),
        })
        .collect()
}

/// The override `value` declares for one agent.
fn agent_override(value: &Value, tiers: &[ModelTier]) -> Result<AgentOverride, AgentOverrideFault> {
    match value {
        Value::String(label) if is_blank(label) => Err(AgentOverrideFault::EmptyValue),
        Value::String(label) if tiers.iter().any(|tier| tier.label == *label) => {
            Ok(AgentOverride::Tier(label.clone()))
        }
        Value::String(label) => Err(AgentOverrideFault::UnknownTier {
            label: label.clone(),
            known: tiers.iter().map(|tier| tier.label.clone()).collect(),
        }),
        Value::Table(vendors) if vendors.is_empty() => Err(AgentOverrideFault::NoVendor),
        Value::Table(vendors) => vendors
            .iter()
            .map(|(name, model)| Ok((known_vendor(name)?, vendor_model(name, model)?)))
            .collect::<Result<_, _>>()
            .map(AgentOverride::Vendors),
        _ => Err(AgentOverrideFault::WrongType {
            found: value_type(value),
        }),
    }
}

/// The vendor `name` names in an override, and a refusal that carries the name when it names none.
fn known_vendor(name: &str) -> Result<Vendor, AgentOverrideFault> {
    vendor(name).ok_or_else(|| AgentOverrideFault::UnknownVendor {
        vendor: name.to_owned(),
    })
}

/// The model and the effort `value` gives the vendor named `name` in an override: a string is the model id
/// alone, and a table is a profile.
fn vendor_model(name: &str, value: &Value) -> Result<VendorModel, AgentOverrideFault> {
    let vendor = || name.to_owned();
    match value {
        Value::String(model_id) if is_blank(model_id) => {
            Err(AgentOverrideFault::VendorModelId { vendor: vendor() })
        }
        Value::String(model_id) => Ok(VendorModel {
            model: Some(model_id.clone()),
            effort: None,
        }),
        Value::Table(profile) => {
            let mut unknown: Vec<String> = profile
                .keys()
                .filter(|key| *key != PROFILE_MODEL && *key != PROFILE_EFFORT)
                .cloned()
                .collect();
            if !unknown.is_empty() {
                unknown.sort();
                return Err(AgentOverrideFault::UnknownProfileKeys {
                    vendor: vendor(),
                    keys: unknown,
                });
            }
            let model = profile_text(profile, PROFILE_MODEL)
                .map_err(|()| AgentOverrideFault::ProfileModel { vendor: vendor() })?;
            let effort = profile_text(profile, PROFILE_EFFORT)
                .map_err(|()| AgentOverrideFault::ProfileEffort { vendor: vendor() })?;
            if model.is_none() && effort.is_none() {
                return Err(AgentOverrideFault::ProfileEmpty { vendor: vendor() });
            }
            Ok(VendorModel { model, effort })
        }
        _ => Err(AgentOverrideFault::VendorValueType {
            vendor: vendor(),
            found: value_type(value),
        }),
    }
}

/// The string `profile` holds under `key`, and none when the key is absent. A value that is not a string, or
/// is a blank one, is the error.
fn profile_text(profile: &Table, key: &str) -> Result<Option<String>, ()> {
    match profile.get(key) {
        None => Ok(None),
        Some(Value::String(text)) if !is_blank(text) => Ok(Some(text.clone())),
        Some(_) => Err(()),
    }
}

/// The vendor `name` names, if any: `claude`, `codex`, or `opencode`, in exactly that spelling.
fn vendor(name: &str) -> Option<Vendor> {
    match name {
        "claude" => Some(Vendor::Claude),
        "codex" => Some(Vendor::Codex),
        "opencode" => Some(Vendor::Opencode),
        _ => None,
    }
}

/// Whether `text` is empty or made of white space alone. White space is what Unicode marks as such and, with
/// it, the four separator controls U+001C to U+001F.
fn is_blank(text: &str) -> bool {
    text.chars()
        .all(|character| character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character))
}

fn built_in_tiers() -> Vec<ModelTier> {
    BUILT_IN_TIERS
        .iter()
        .map(|(label, [claude, codex, opencode])| ModelTier {
            label: (*label).to_owned(),
            claude: Some((*claude).to_owned()),
            codex: Some((*codex).to_owned()),
            opencode: Some((*opencode).to_owned()),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::super::table;
    use super::*;
    use crate::config_merge;
    use crate::termination::ValueType;

    fn tier(
        label: &str,
        claude: Option<&str>,
        codex: Option<&str>,
        opencode: Option<&str>,
    ) -> ModelTier {
        ModelTier {
            label: label.to_owned(),
            claude: claude.map(str::to_owned),
            codex: codex.map(str::to_owned),
            opencode: opencode.map(str::to_owned),
        }
    }

    /// The twelve cells of the built-in table, written out.
    fn built_in() -> Vec<ModelTier> {
        vec![
            tier(
                "fable",
                Some("fable"),
                Some("gpt-6-astra"),
                Some("anthropic/claude-fable-5-1"),
            ),
            tier(
                "opus",
                Some("opus"),
                Some("gpt-6.1-sol"),
                Some("anthropic/claude-opus-5-5"),
            ),
            tier(
                "sonnet",
                Some("sonnet"),
                Some("gpt-6.1-terra"),
                Some("anthropic/claude-sonnet-5-5"),
            ),
            tier(
                "haiku",
                Some("haiku"),
                Some("gpt-6-luna"),
                Some("anthropic/claude-haiku-4-5"),
            ),
        ]
    }

    fn tiers_of(text: &str) -> Vec<ModelTier> {
        model_tiers(&table(text)).expect("the tiers")
    }

    /// The fault of the label `deep`, the one label each refused document of these tests declares.
    fn tier_fault(text: &str) -> ModelTierFault {
        match model_tiers(&table(text)).expect_err("a refusal") {
            ConfigError::ModelTier { label, fault } if label == "deep" => fault,
            other => panic!("{text}: {other:?}"),
        }
    }

    fn overrides_of(text: &str) -> Vec<(String, AgentOverride)> {
        let merged = table(text);
        let tiers = model_tiers(&merged).expect("the tiers");
        agent_model_overrides(&merged, &tiers).expect("the overrides")
    }

    /// The fault of the agent `reviewer`, the one agent each refused document of these tests declares.
    fn override_fault(text: &str) -> AgentOverrideFault {
        let merged = table(text);
        let tiers = model_tiers(&merged).expect("the tiers");
        match agent_model_overrides(&merged, &tiers).expect_err("a refusal") {
            ConfigError::AgentOverride { agent, fault } if agent == "reviewer" => fault,
            other => panic!("{text}: {other:?}"),
        }
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    #[test]
    fn a_table_that_declares_no_tier_gives_the_twelve_built_in_cells() {
        assert_eq!(tiers_of("# nothing declared\n"), built_in());
        assert_eq!(tiers_of("[model_tiers]\n"), built_in());
    }

    #[test]
    fn model_tiers_that_is_no_table_gives_the_built_in_table_and_no_error() {
        for text in [
            "model_tiers = \"x\"",
            "model_tiers = 7",
            "model_tiers = true",
            "model_tiers = [\"x\"]",
        ] {
            assert_eq!(tiers_of(text), built_in(), "{text}");
        }
    }

    #[test]
    fn a_built_in_label_takes_the_cells_declared_and_keeps_the_others() {
        let mut expected = built_in();
        expected[1].codex = Some("gpt-next".to_owned());

        assert_eq!(
            tiers_of("[model_tiers.opus]\ncodex = \"gpt-next\"\n"),
            expected
        );
    }

    #[test]
    fn added_labels_follow_the_built_in_ones_in_document_order_with_their_own_cells_alone() {
        let mut expected = built_in();
        expected.push(tier("zeal", None, Some("z-1"), None));
        expected.push(tier("deep", Some("d-1"), None, Some("d-3")));

        assert_eq!(
            tiers_of(
                "[model_tiers.zeal]\ncodex = \"z-1\"\n\n\
                 [model_tiers.deep]\nopencode = \"d-3\"\nclaude = \"d-1\"\n"
            ),
            expected
        );
    }

    #[test]
    fn a_local_label_replaces_the_shared_label_whole() {
        let merged = config_merge::merge(
            table(
                "[model_tiers.opus]\nclaude = \"shared-a\"\ncodex = \"shared-b\"\n\n\
                 [model_tiers.deep]\nclaude = \"shared-c\"\ncodex = \"shared-d\"\n\n\
                 [model_tiers.wide]\nclaude = \"shared-e\"\n",
            ),
            table(
                "[model_tiers.opus]\nclaude = \"local-a\"\n\n\
                 [model_tiers.deep]\nopencode = \"local-f\"\n",
            ),
        )
        .expect("a merged table");

        // The shared codex cells of both labels are gone: `opus` falls back to its built-in cell, and
        // `deep`, which has none, holds the local cell alone.
        let mut expected = built_in();
        expected[1].claude = Some("local-a".to_owned());
        expected.push(tier("deep", None, None, Some("local-f")));
        expected.push(tier("wide", Some("shared-e"), None, None));
        assert_eq!(model_tiers(&merged).expect("the tiers"), expected);
    }

    #[test]
    fn a_tier_that_is_no_table_is_refused_with_its_type() {
        for (text, found) in [
            ("[model_tiers]\ndeep = \"opus\"\n", ValueType::String),
            ("[model_tiers]\ndeep = 3\n", ValueType::Integer),
            ("[model_tiers]\ndeep = [\"opus\"]\n", ValueType::Array),
        ] {
            assert_eq!(
                tier_fault(text),
                ModelTierFault::NotVendorTable { found },
                "{text}"
            );
        }
    }

    #[test]
    fn a_tier_with_an_unknown_vendor_is_refused_with_the_vendor() {
        for vendor in ["lantern", "Claude", "claude-code", ""] {
            let text = format!("[model_tiers.deep]\nclaude = \"a\"\n\"{vendor}\" = \"b\"\n");

            assert_eq!(
                tier_fault(&text),
                ModelTierFault::UnknownVendor {
                    vendor: vendor.to_owned()
                },
                "{text}"
            );
        }
    }

    #[test]
    fn a_model_id_that_is_no_string_or_is_empty_is_refused_with_its_vendor_and_type() {
        for (text, vendor, found) in [
            (
                "[model_tiers.deep]\ncodex = 5.1\n",
                "codex",
                ValueType::Float,
            ),
            (
                "[model_tiers.deep]\nclaude = \"\"\n",
                "claude",
                ValueType::String,
            ),
            (
                "[model_tiers.deep]\nclaude = \"a\"\nopencode = { model = \"b\" }\n",
                "opencode",
                ValueType::Table,
            ),
            (
                "[model_tiers.opus]\nclaude = false\n",
                "claude",
                ValueType::Boolean,
            ),
        ] {
            let error = model_tiers(&table(text)).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ModelTier {
                        fault: ModelTierFault::ModelIdNotString { vendor: named, found: seen },
                        ..
                    } if named == vendor && *seen == found
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn a_tier_that_names_no_vendor_is_refused() {
        assert_eq!(tier_fault("[model_tiers.deep]\n"), ModelTierFault::NoVendor);
        assert_eq!(
            tier_fault("[model_tiers]\ndeep = {}\n"),
            ModelTierFault::NoVendor
        );
    }

    #[test]
    fn the_first_vendor_at_fault_in_document_order_is_the_one_reported() {
        assert_eq!(
            tier_fault("[model_tiers.deep]\ncodex = 1\nlantern = \"a\"\n"),
            ModelTierFault::ModelIdNotString {
                vendor: "codex".to_owned(),
                found: ValueType::Integer
            }
        );
        assert_eq!(
            tier_fault("[model_tiers.deep]\nlantern = \"a\"\ncodex = 1\n"),
            ModelTierFault::UnknownVendor {
                vendor: "lantern".to_owned()
            }
        );
    }

    #[test]
    fn a_model_id_of_white_space_alone_passes_in_a_tier() {
        let mut expected = built_in();
        expected.push(tier("deep", Some(" \t"), None, None));

        assert_eq!(
            tiers_of("[model_tiers.deep]\nclaude = \" \\t\"\n"),
            expected
        );
    }

    #[test]
    fn no_table_of_overrides_gives_no_override() {
        assert!(overrides_of("# nothing declared\n").is_empty());
        assert!(overrides_of("[agent_model_overrides]\n").is_empty());
    }

    #[test]
    fn overrides_are_read_in_document_order_as_a_tier_or_as_models_by_vendor() {
        assert_eq!(
            overrides_of(
                "[model_tiers.deep]\nclaude = \"d-1\"\n\n\
                 [agent_model_overrides]\n\
                 writer = \"deep\"\n\
                 reviewer = \"haiku\"\n\
                 builder = { opencode = \"o-1\", claude = { model = \"c-1\", effort = \"high\" } }\n\
                 planner = { codex = { effort = \"low\" }, claude = { model = \"c-2\" } }\n"
            ),
            [
                ("writer".to_owned(), AgentOverride::Tier("deep".to_owned())),
                (
                    "reviewer".to_owned(),
                    AgentOverride::Tier("haiku".to_owned())
                ),
                (
                    "builder".to_owned(),
                    AgentOverride::Vendors(vec![
                        (
                            Vendor::Opencode,
                            VendorModel {
                                model: Some("o-1".to_owned()),
                                effort: None
                            }
                        ),
                        (
                            Vendor::Claude,
                            VendorModel {
                                model: Some("c-1".to_owned()),
                                effort: Some("high".to_owned())
                            }
                        ),
                    ])
                ),
                (
                    "planner".to_owned(),
                    AgentOverride::Vendors(vec![
                        (
                            Vendor::Codex,
                            VendorModel {
                                model: None,
                                effort: Some("low".to_owned())
                            }
                        ),
                        (
                            Vendor::Claude,
                            VendorModel {
                                model: Some("c-2".to_owned()),
                                effort: None
                            }
                        ),
                    ])
                ),
            ]
        );
    }

    #[test]
    fn a_label_no_tier_has_is_refused_with_the_labels_of_the_effective_table() {
        assert_eq!(
            override_fault(
                "[model_tiers.deep]\nclaude = \"d-1\"\n\n[agent_model_overrides]\nreviewer = \"huge\"\n"
            ),
            AgentOverrideFault::UnknownTier {
                label: "huge".to_owned(),
                known: strings(&["fable", "opus", "sonnet", "haiku", "deep"]),
            }
        );
        // A label is matched as written: neither its case nor white space around it is forgiven.
        for label in ["Opus", " opus", "gpt-6.1-sol"] {
            let text = format!("[agent_model_overrides]\nreviewer = \"{label}\"\n");

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::UnknownTier {
                    label: label.to_owned(),
                    known: strings(&["fable", "opus", "sonnet", "haiku"]),
                },
                "{text}"
            );
        }
    }

    #[test]
    fn a_blank_override_is_refused_as_empty() {
        for value in ["", " ", "\\t\\n", "\\u001F", "\\u00A0\\u3000"] {
            let text = format!("[agent_model_overrides]\nreviewer = \"{value}\"\n");

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::EmptyValue,
                "{text}"
            );
        }
    }

    #[test]
    fn an_override_that_is_neither_a_string_nor_a_table_is_refused_with_its_type() {
        for (value, found) in [
            ("7", ValueType::Integer),
            ("true", ValueType::Boolean),
            ("[\"opus\"]", ValueType::Array),
            ("1979-05-27", ValueType::Datetime),
        ] {
            let text = format!("[agent_model_overrides]\nreviewer = {value}\n");

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::WrongType { found },
                "{text}"
            );
        }
    }

    #[test]
    fn an_override_with_an_unknown_vendor_is_refused_with_the_vendor() {
        assert_eq!(
            override_fault(
                "[agent_model_overrides]\nreviewer = { claude = \"a\", lantern = \"b\" }\n"
            ),
            AgentOverrideFault::UnknownVendor {
                vendor: "lantern".to_owned()
            }
        );
    }

    #[test]
    fn an_override_table_that_names_no_vendor_is_refused() {
        assert_eq!(
            override_fault("[agent_model_overrides]\nreviewer = {}\n"),
            AgentOverrideFault::NoVendor
        );
    }

    #[test]
    fn a_blank_model_id_of_a_vendor_is_refused_in_an_override() {
        for value in ["", " \\t", "\\u001C"] {
            let text = format!("[agent_model_overrides]\nreviewer = {{ codex = \"{value}\" }}\n");

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::VendorModelId {
                    vendor: "codex".to_owned()
                },
                "{text}"
            );
        }
    }

    #[test]
    fn a_vendor_value_that_is_neither_a_string_nor_a_table_is_refused_with_its_type() {
        for (value, found) in [
            ("7", ValueType::Integer),
            ("false", ValueType::Boolean),
            ("[\"a\"]", ValueType::Array),
        ] {
            let text = format!("[agent_model_overrides]\nreviewer = {{ codex = {value} }}\n");

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::VendorValueType {
                    vendor: "codex".to_owned(),
                    found
                },
                "{text}"
            );
        }
    }

    #[test]
    fn unknown_profile_keys_are_refused_together_and_sorted_before_the_known_keys_are_read() {
        assert_eq!(
            override_fault(
                "[agent_model_overrides.reviewer]\n\
                 opencode = { speed = \"fast\", model = 7, depth = 2, Model = \"a\" }\n"
            ),
            AgentOverrideFault::UnknownProfileKeys {
                vendor: "opencode".to_owned(),
                keys: strings(&["Model", "depth", "speed"]),
            }
        );
    }

    #[test]
    fn a_profile_model_that_is_no_string_or_is_blank_is_refused_before_its_effort() {
        for model in ["7", "\"\"", "\" \"", "[\"a\"]"] {
            let text = format!(
                "[agent_model_overrides.reviewer]\nclaude = {{ effort = 3, model = {model} }}\n"
            );

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::ProfileModel {
                    vendor: "claude".to_owned()
                },
                "{text}"
            );
        }
    }

    #[test]
    fn a_profile_effort_that_is_no_string_or_is_blank_is_refused() {
        for effort in ["3", "\"\"", "\"\\t\"", "true"] {
            let text = format!(
                "[agent_model_overrides.reviewer]\nclaude = {{ model = \"a\", effort = {effort} }}\n"
            );

            assert_eq!(
                override_fault(&text),
                AgentOverrideFault::ProfileEffort {
                    vendor: "claude".to_owned()
                },
                "{text}"
            );
        }
    }

    #[test]
    fn a_profile_with_neither_model_nor_effort_is_refused() {
        assert_eq!(
            override_fault("[agent_model_overrides.reviewer]\ncodex = {}\n"),
            AgentOverrideFault::ProfileEmpty {
                vendor: "codex".to_owned()
            }
        );
    }

    #[test]
    fn the_first_agent_at_fault_in_document_order_is_the_one_reported() {
        let merged =
            table("[agent_model_overrides]\nwriter = \"opus\"\nplanner = 7\nreviewer = \"huge\"\n");
        let tiers = model_tiers(&merged).expect("the tiers");

        let error = agent_model_overrides(&merged, &tiers).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::AgentOverride {
                    agent,
                    fault: AgentOverrideFault::WrongType { found: ValueType::Integer },
                } if agent == "planner"
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_model_id_of_white_space_alone_that_passes_in_a_tier_is_refused_in_an_override() {
        let merged = table(
            "[model_tiers.deep]\nclaude = \" \"\n\n[agent_model_overrides]\nreviewer = { claude = \" \" }\n",
        );

        let tiers = model_tiers(&merged).expect("the tiers");
        let error = agent_model_overrides(&merged, &tiers).expect_err("a refusal");

        assert_eq!(tiers.last(), Some(&tier("deep", Some(" "), None, None)));
        assert!(
            matches!(
                &error,
                ConfigError::AgentOverride {
                    fault: AgentOverrideFault::VendorModelId { vendor },
                    ..
                } if vendor == "claude"
            ),
            "{error:?}"
        );
    }
}
