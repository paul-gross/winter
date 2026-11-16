//! The environment variables of `[env]`, by band: `[env.workspace.vars]`, `[env.feature.vars]`, and the
//! `vars` of every other sub-table, which is the band of the env of that name. An entry is read and
//! validated here and nothing more: no command of an entry is started.

use toml::{Table, Value};

use super::{EnvBands, EnvCommand, EnvFormat, EnvValue, EnvVar, config_value, value_type};
use crate::termination::{ConfigError, EnvVarFault, RemovedKey};

const WORKSPACE_BAND: &str = "workspace";
const FEATURE_BAND: &str = "feature";

/// The key of the removed table `[env.vars]`.
const REMOVED_VARS: &str = "vars";

/// The start of the names the tool sets itself, which no command entry may be declared under.
const MANAGED_PREFIX: &str = "WINTER_";

/// The fields a command entry may hold.
const COMMAND_FIELDS: [&str; 4] = ["command", "format", "shell", "exports"];

/// The env bands of `merged`.
///
/// An `env` that is not a table holds no band, and neither a band nor its `vars` that is not a table holds a
/// variable; none of the three is refused. A named band without a variable is left out.
///
/// An `env` that holds the key `vars` is [`RemovedKey::EnvVars`] with the keys of that table, or with none
/// when the value is not a table, whatever else `env` holds. Otherwise the workspace band is read, then the
/// feature band, then the named bands in the order of the document, each in the order of its entries, and
/// the first entry at fault is [`ConfigError::EnvVar`], which names the band and the key and carries neither
/// the value of the entry nor its command.
pub(super) fn env_bands(merged: &Table) -> Result<EnvBands, ConfigError> {
    let Some(Value::Table(env)) = merged.get("env") else {
        return Ok(EnvBands {
            workspace: Vec::new(),
            feature: Vec::new(),
            named: Vec::new(),
        });
    };

    if let Some(removed) = env.get(REMOVED_VARS) {
        let keys = removed
            .as_table()
            .map(|vars| vars.keys().cloned().collect())
            .unwrap_or_default();
        return Err(ConfigError::RemovedKey(RemovedKey::EnvVars { keys }));
    }

    let workspace = band(env, WORKSPACE_BAND)?;
    let feature = band(env, FEATURE_BAND)?;
    let mut named = Vec::new();
    for name in env.keys() {
        if name == WORKSPACE_BAND || name == FEATURE_BAND {
            continue;
        }
        let vars = band(env, name)?;
        if !vars.is_empty() {
            named.push((name.clone(), vars));
        }
    }
    Ok(EnvBands {
        workspace,
        feature,
        named,
    })
}

/// The variables of the band `name` of `env`, in the order of the document; none when the band or its `vars`
/// is absent or not a table.
fn band(env: &Table, name: &str) -> Result<Vec<EnvVar>, ConfigError> {
    let Some(Value::Table(vars)) = env.get(name).and_then(|band| band.get("vars")) else {
        return Ok(Vec::new());
    };
    vars.iter()
        .map(|(key, value)| match env_value(key, value) {
            Ok(value) => Ok(EnvVar {
                key: key.clone(),
                value,
            }),
            Err(fault) => Err(ConfigError::EnvVar {
                band: name.to_owned(),
                key: key.clone(),
                fault,
            }),
        })
        .collect()
}

/// The value of the entry `key`: a string is a literal as written, an integer and a float are the literal of
/// their decimal string, and a table is a command entry. A value of any other type is refused with its type.
fn env_value(key: &str, value: &Value) -> Result<EnvValue, EnvVarFault> {
    match value {
        Value::String(text) => Ok(EnvValue::Literal(text.clone())),
        Value::Integer(number) => Ok(EnvValue::Literal(number.to_string())),
        Value::Float(number) => Ok(EnvValue::Literal(float_text(*number))),
        Value::Table(entry) => command_entry(key, entry).map(EnvValue::Command),
        Value::Boolean(_) | Value::Datetime(_) | Value::Array(_) => {
            Err(EnvVarFault::UnsupportedType {
                found: value_type(value),
            })
        }
    }
}

/// The command entry `entry` declares under `key`.
///
/// The faults are looked for in this order, and the first found is the one reported: a key that starts with
/// `WINTER_`; fields outside the closed set, listed sorted by name; a `command` that is absent, empty, or not
/// a string; a `format` that is none of `raw`, `dotenv`, and `json`; a `shell` that is not a boolean; an
/// `exports` that is not an array of strings. An absent `format` is `raw`, an absent `shell` is `false`, and
/// an absent `exports` is none.
fn command_entry(key: &str, entry: &Table) -> Result<EnvCommand, EnvVarFault> {
    if key.starts_with(MANAGED_PREFIX) {
        return Err(EnvVarFault::ManagedName);
    }

    let mut unknown: Vec<String> = entry
        .keys()
        .filter(|field| !COMMAND_FIELDS.contains(&field.as_str()))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(EnvVarFault::UnknownFields { fields: unknown });
    }

    let command = match entry.get("command") {
        Some(Value::String(command)) if !command.is_empty() => command.clone(),
        _ => return Err(EnvVarFault::MissingCommand),
    };

    let format = match entry.get("format") {
        None => EnvFormat::Raw,
        Some(value) => match value.as_str() {
            Some("raw") => EnvFormat::Raw,
            Some("dotenv") => EnvFormat::Dotenv,
            Some("json") => EnvFormat::Json,
            _ => {
                return Err(EnvVarFault::InvalidFormat {
                    value: config_value(value),
                });
            }
        },
    };

    let shell = match entry.get("shell") {
        None => false,
        Some(Value::Boolean(shell)) => *shell,
        Some(value) => {
            return Err(EnvVarFault::ShellNotBoolean {
                found: value_type(value),
            });
        }
    };

    let exports = match entry.get("exports") {
        None => None,
        Some(value) => Some(
            value
                .as_array()
                .and_then(|items| {
                    items
                        .iter()
                        .map(|item| item.as_str().map(str::to_owned))
                        .collect::<Option<Vec<String>>>()
                })
                .ok_or(EnvVarFault::ExportsNotStringList)?,
        ),
    };

    Ok(EnvCommand {
        command,
        format,
        shell,
        exports,
    })
}

/// The decimal string of a float: the shortest digits that read back as the same value.
///
/// An integral value ends in `.0`. A value whose decimal exponent is below -4 or at least 16 is written in
/// exponent form, with the sign of the exponent and at least two digits of it: `1e+16`, `1e-05`. The
/// infinities are `inf` and `-inf`, and a value that is not a number is `nan` whatever its sign.
fn float_text(number: f64) -> String {
    if number.is_nan() {
        return "nan".to_owned();
    }
    let sign = if number.is_sign_negative() { "-" } else { "" };
    if number.is_infinite() {
        return format!("{sign}inf");
    }

    // The formatter writes the shortest digits that read back as the value, as `d[.ddd]e[-]n`.
    let scientific = format!("{:e}", number.abs());
    let Some((mantissa, exponent)) = scientific.split_once('e') else {
        return format!("{sign}{scientific}");
    };
    let Ok(exponent) = exponent.parse::<i32>() else {
        return format!("{sign}{scientific}");
    };
    let digits = mantissa.replace('.', "");

    if !(-4..16).contains(&exponent) {
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        return format!(
            "{sign}{mantissa}e{exponent_sign}{:02}",
            exponent.unsigned_abs()
        );
    }
    match usize::try_from(exponent) {
        // The decimal point stands after the digit at `exponent`; digits the value lacks up to there are
        // zeros.
        Ok(point) => {
            let whole_digits = point + 1;
            if digits.len() <= whole_digits {
                format!("{sign}{digits:0<whole_digits$}.0")
            } else {
                let (whole, fraction) = digits.split_at(whole_digits);
                format!("{sign}{whole}.{fraction}")
            }
        }
        Err(_) => {
            let zeros = "0".repeat(exponent.unsigned_abs() as usize - 1);
            format!("{sign}0.{zeros}{digits}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::table;
    use super::*;
    use crate::termination::{ConfigValue, ValueType};

    fn bands_of(text: &str) -> EnvBands {
        env_bands(&table(text)).expect("the env bands")
    }

    fn refusal_of(text: &str) -> ConfigError {
        env_bands(&table(text)).expect_err("a refusal")
    }

    fn literal(key: &str, value: &str) -> EnvVar {
        EnvVar {
            key: key.to_owned(),
            value: EnvValue::Literal(value.to_owned()),
        }
    }

    fn no_bands() -> EnvBands {
        EnvBands {
            workspace: Vec::new(),
            feature: Vec::new(),
            named: Vec::new(),
        }
    }

    /// The fault of the entry `TOKEN` of the feature band, which `entry` is the value of.
    fn fault_of(entry: &str) -> EnvVarFault {
        let error = refusal_of(&format!("[env.feature.vars]\nTOKEN = {entry}\n"));
        match error {
            ConfigError::EnvVar { band, key, fault } => {
                assert_eq!(
                    (band.as_str(), key.as_str()),
                    ("feature", "TOKEN"),
                    "{entry}"
                );
                fault
            }
            other => panic!("{entry}: {other:?}"),
        }
    }

    #[test]
    fn a_configuration_without_env_has_no_band() {
        assert_eq!(bands_of("# nothing declared\n"), no_bands());
    }

    #[test]
    fn the_workspace_and_feature_bands_are_read_in_the_order_of_their_entries() {
        let bands = bands_of(
            "[env.workspace.vars]\nB = \"two\"\nA = \"one\"\n\n[env.feature.vars]\nC = \"{three}\"\n",
        );

        assert_eq!(
            bands,
            EnvBands {
                workspace: vec![literal("B", "two"), literal("A", "one")],
                feature: vec![literal("C", "{three}")],
                named: Vec::new(),
            }
        );
    }

    #[test]
    fn a_named_band_is_any_sub_table_other_than_workspace_and_feature() {
        let bands = bands_of(
            "[env.zeta.vars]\nA = \"1\"\n\n[env.workspace.vars]\nB = \"2\"\n\n\
             [env.\"no such env\".vars]\nC = \"3\"\n\n[env.feature.vars]\nD = \"4\"\n\n\
             [env.alpha.vars]\nE = \"5\"\n",
        );

        assert_eq!(
            bands,
            EnvBands {
                workspace: vec![literal("B", "2")],
                feature: vec![literal("D", "4")],
                named: vec![
                    ("zeta".to_owned(), vec![literal("A", "1")]),
                    ("no such env".to_owned(), vec![literal("C", "3")]),
                    ("alpha".to_owned(), vec![literal("E", "5")]),
                ],
            }
        );
    }

    #[test]
    fn a_named_band_without_a_variable_is_left_out() {
        for text in [
            "[env.alpha]\n",
            "[env.alpha.vars]\n",
            "[env.alpha]\nother = 1\n",
            "[env]\nalpha = \"x\"\n",
            "[env.alpha]\nvars = \"x\"\n",
        ] {
            assert_eq!(bands_of(text), no_bands(), "{text}");
        }
    }

    #[test]
    fn an_env_a_band_or_a_vars_that_is_not_a_table_is_read_as_absent() {
        for text in [
            "env = \"x\"\n",
            "env = 7\n",
            "env = [\"x\"]\n",
            "[env]\nfeature = \"x\"\n",
            "[env]\nworkspace = true\n",
            "[env.feature]\nvars = \"x\"\n",
            "[env.workspace]\nvars = [\"x\"]\n",
        ] {
            assert_eq!(bands_of(text), no_bands(), "{text}");
        }
    }

    #[test]
    fn a_band_that_is_not_a_table_leaves_the_other_bands_read() {
        let bands = bands_of("[env]\nfeature = \"x\"\n\n[env.workspace.vars]\nA = \"1\"\n");

        assert_eq!(bands.workspace, [literal("A", "1")]);
        assert_eq!(bands.feature, []);
    }

    #[test]
    fn a_string_is_a_literal_as_written() {
        let bands = bands_of(
            "[env.feature.vars]\nEMPTY = \"\"\nSPACED = \" a b \"\nWINTER_HOME = \"kept\"\n",
        );

        assert_eq!(
            bands.feature,
            [
                literal("EMPTY", ""),
                literal("SPACED", " a b "),
                literal("WINTER_HOME", "kept"),
            ]
        );
    }

    #[test]
    fn a_number_is_the_literal_of_its_decimal_string() {
        let cases = [
            ("1.0", "1.0"),
            ("-0.0", "-0.0"),
            ("0.0", "0.0"),
            ("0.1", "0.1"),
            ("0.5", "0.5"),
            ("-2.5", "-2.5"),
            ("123.456", "123.456"),
            ("1e16", "1e+16"),
            ("1e15", "1000000000000000.0"),
            ("9999999999999998.0", "9999999999999998.0"),
            ("123456789012345680.0", "1.2345678901234568e+17"),
            ("0.0001", "0.0001"),
            ("0.00012", "0.00012"),
            ("0.00001", "1e-05"),
            ("0.000012345", "1.2345e-05"),
            ("-0.00001", "-1e-05"),
            ("1.5e300", "1.5e+300"),
            ("-1.5e300", "-1.5e+300"),
            ("1e100", "1e+100"),
            ("1e-100", "1e-100"),
            ("5e-324", "5e-324"),
            ("1.7976931348623157e308", "1.7976931348623157e+308"),
            ("inf", "inf"),
            ("+inf", "inf"),
            ("-inf", "-inf"),
            ("nan", "nan"),
            ("-nan", "nan"),
            ("0", "0"),
            ("-7", "-7"),
            ("0x10", "16"),
            ("1_000", "1000"),
            ("9223372036854775807", "9223372036854775807"),
            ("-9223372036854775808", "-9223372036854775808"),
        ];

        for (written, expected) in cases {
            let bands = bands_of(&format!("[env.workspace.vars]\nN = {written}\n"));

            assert_eq!(bands.workspace, [literal("N", expected)], "{written}");
        }
    }

    #[test]
    fn a_table_with_a_command_alone_is_a_command_entry_with_the_defaults() {
        let bands = bands_of("[env.feature.vars]\nTOKEN = { command = \"print-token\" }\n");

        assert_eq!(
            bands.feature,
            [EnvVar {
                key: "TOKEN".to_owned(),
                value: EnvValue::Command(EnvCommand {
                    command: "print-token".to_owned(),
                    format: EnvFormat::Raw,
                    shell: false,
                    exports: None,
                }),
            }]
        );
    }

    #[test]
    fn a_command_entry_reads_each_of_its_fields() {
        let entry = |fields: &str| {
            let text =
                format!("[env.alpha.vars]\nTOKEN = {{ command = \"print-token\", {fields} }}\n");
            match bands_of(&text).named.as_slice() {
                [(name, vars)] if name == "alpha" && vars.len() == 1 => match &vars[0].value {
                    EnvValue::Command(command) => command.clone(),
                    other => panic!("{fields}: {other:?}"),
                },
                other => panic!("{fields}: {other:?}"),
            }
        };

        assert_eq!(entry("format = \"raw\"").format, EnvFormat::Raw);
        assert_eq!(entry("format = \"dotenv\"").format, EnvFormat::Dotenv);
        assert_eq!(entry("format = \"json\"").format, EnvFormat::Json);
        assert!(entry("shell = true").shell);
        assert!(!entry("shell = false").shell);
        assert_eq!(entry("exports = []").exports, Some(Vec::new()));
        assert_eq!(
            entry("exports = [\"B\", \"A\", \"\"]").exports,
            Some(vec!["B".to_owned(), "A".to_owned(), String::new()])
        );
    }

    #[test]
    fn a_boolean_a_datetime_or_an_array_is_an_unsupported_type() {
        for (written, found) in [
            ("true", ValueType::Boolean),
            ("false", ValueType::Boolean),
            ("1979-05-27T07:32:00Z", ValueType::Datetime),
            ("1979-05-27", ValueType::Datetime),
            ("[\"a\"]", ValueType::Array),
            ("[]", ValueType::Array),
        ] {
            assert_eq!(
                fault_of(written),
                EnvVarFault::UnsupportedType { found },
                "{written}"
            );
        }
    }

    #[test]
    fn fields_outside_the_closed_set_are_unknown_fields_sorted_by_name() {
        assert_eq!(
            fault_of("{ command = \"print-token\", timeout = 5, cwd = \".\", Shell = true }"),
            EnvVarFault::UnknownFields {
                fields: vec!["Shell".to_owned(), "cwd".to_owned(), "timeout".to_owned()],
            }
        );
    }

    #[test]
    fn a_command_entry_under_a_winter_key_is_a_managed_name() {
        for key in ["WINTER_TOKEN", "WINTER_"] {
            let error = refusal_of(&format!(
                "[env.workspace.vars]\n{key} = {{ command = \"print-token\" }}\n"
            ));

            assert!(
                matches!(
                    &error,
                    ConfigError::EnvVar { band, key: held, fault: EnvVarFault::ManagedName }
                        if band == "workspace" && held == key
                ),
                "{key}: {error:?}"
            );
        }
    }

    #[test]
    fn a_command_entry_under_a_key_that_only_resembles_a_winter_key_is_read() {
        let bands = bands_of(
            "[env.workspace.vars]\nwinter_token = { command = \"a\" }\nWINTER = { command = \"b\" }\n\
             MY_WINTER_TOKEN = { command = \"c\" }\n",
        );

        assert_eq!(bands.workspace.len(), 3);
    }

    #[test]
    fn a_command_that_is_absent_empty_or_not_a_string_is_a_missing_command() {
        for written in [
            "{}",
            "{ shell = true }",
            "{ command = \"\" }",
            "{ command = 7 }",
            "{ command = [\"print-token\"] }",
        ] {
            assert_eq!(fault_of(written), EnvVarFault::MissingCommand, "{written}");
        }
    }

    #[test]
    fn a_format_that_is_no_choice_is_an_invalid_format_carrying_the_value_of_the_field() {
        for (written, text, found) in [
            ("\"yaml\"", "\"yaml\"", ValueType::String),
            ("\"RAW\"", "\"RAW\"", ValueType::String),
            ("\"\"", "\"\"", ValueType::String),
            ("1", "1", ValueType::Integer),
            ("true", "true", ValueType::Boolean),
        ] {
            assert_eq!(
                fault_of(&format!(
                    "{{ command = \"print-token\", format = {written} }}"
                )),
                EnvVarFault::InvalidFormat {
                    value: ConfigValue {
                        text: text.to_owned(),
                        found,
                    },
                },
                "{written}"
            );
        }
    }

    #[test]
    fn a_shell_that_is_not_a_boolean_is_refused_with_its_type() {
        for (written, found) in [
            ("\"true\"", ValueType::String),
            ("1", ValueType::Integer),
            ("[true]", ValueType::Array),
        ] {
            assert_eq!(
                fault_of(&format!(
                    "{{ command = \"print-token\", shell = {written} }}"
                )),
                EnvVarFault::ShellNotBoolean { found },
                "{written}"
            );
        }
    }

    #[test]
    fn an_exports_that_is_not_an_array_of_strings_is_refused() {
        for written in ["\"A\"", "7", "{ a = \"A\" }", "[\"A\", 1]", "[[\"A\"]]"] {
            assert_eq!(
                fault_of(&format!(
                    "{{ command = \"print-token\", exports = {written} }}"
                )),
                EnvVarFault::ExportsNotStringList,
                "{written}"
            );
        }
    }

    #[test]
    fn the_faults_of_one_command_entry_are_reported_in_a_fixed_order() {
        let all = "{ command = \"\", format = \"yaml\", shell = 1, exports = 7, timeout = 5 }";
        let error = refusal_of(&format!("[env.feature.vars]\nWINTER_TOKEN = {all}\n"));
        assert!(
            matches!(
                error,
                ConfigError::EnvVar {
                    fault: EnvVarFault::ManagedName,
                    ..
                }
            ),
            "{error:?}"
        );

        assert!(matches!(fault_of(all), EnvVarFault::UnknownFields { .. }));
        assert_eq!(
            fault_of("{ command = \"\", format = \"yaml\", shell = 1, exports = 7 }"),
            EnvVarFault::MissingCommand
        );
        assert!(matches!(
            fault_of("{ command = \"print-token\", format = \"yaml\", shell = 1, exports = 7 }"),
            EnvVarFault::InvalidFormat { .. }
        ));
        assert!(matches!(
            fault_of("{ command = \"print-token\", shell = 1, exports = 7 }"),
            EnvVarFault::ShellNotBoolean { .. }
        ));
    }

    #[test]
    fn the_first_entry_at_fault_is_reported_workspace_then_feature_then_named_bands() {
        let band_at_fault = |text: &str| match refusal_of(text) {
            ConfigError::EnvVar { band, key, .. } => (band, key),
            other => panic!("{text}: {other:?}"),
        };
        let pair = |band: &str, key: &str| (band.to_owned(), key.to_owned());

        assert_eq!(
            band_at_fault(
                "[env.alpha.vars]\nA = true\n\n[env.feature.vars]\nB = true\n\n\
                 [env.workspace.vars]\nGOOD = \"1\"\nC = true\nD = true\n"
            ),
            pair("workspace", "C")
        );
        assert_eq!(
            band_at_fault("[env.alpha.vars]\nA = true\n\n[env.feature.vars]\nB = true\n"),
            pair("feature", "B")
        );
        assert_eq!(
            band_at_fault("[env.beta.vars]\nA = true\n\n[env.alpha.vars]\nB = true\n"),
            pair("beta", "A")
        );
    }

    #[test]
    fn the_removed_env_vars_is_refused_with_the_keys_found_in_the_order_of_the_document() {
        let error = refusal_of("[env.vars]\nB = \"2\"\nA = true\n");

        assert!(
            matches!(
                &error,
                ConfigError::RemovedKey(RemovedKey::EnvVars { keys }) if keys == &["B", "A"]
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_removed_env_vars_is_refused_without_keys_when_it_is_empty_or_not_a_table() {
        for text in ["[env.vars]\n", "[env]\nvars = \"x\"\n", "[env]\nvars = 7\n"] {
            let error = refusal_of(text);

            assert!(
                matches!(
                    &error,
                    ConfigError::RemovedKey(RemovedKey::EnvVars { keys }) if keys.is_empty()
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn the_removed_env_vars_is_reported_before_an_entry_at_fault() {
        let error = refusal_of("[env.workspace.vars]\nA = true\n\n[env.vars]\nB = \"2\"\n");

        assert!(
            matches!(error, ConfigError::RemovedKey(RemovedKey::EnvVars { .. })),
            "{error:?}"
        );
    }

    #[test]
    fn no_refusal_of_an_entry_holds_its_value_or_its_command() {
        for entry in [
            "[\"hidden-value\"]",
            "{ command = \"hidden-value\", timeout = \"hidden-value\" }",
            "{ format = \"raw\", shell = \"hidden-value\" }",
            "{ command = \"hidden-value\", format = \"yaml\" }",
            "{ command = \"hidden-value\", shell = \"hidden-value\" }",
            "{ command = \"hidden-value\", exports = \"hidden-value\" }",
            "{ command = \"hidden-value\", exports = [\"hidden-value\", 1] }",
        ] {
            let error = refusal_of(&format!("[env.feature.vars]\nTOKEN = {entry}\n"));

            assert!(
                !format!("{error:?}").contains("hidden"),
                "{entry}: {error:?}"
            );
            assert!(!error.to_string().contains("hidden"), "{entry}: {error}");
        }
        let managed =
            refusal_of("[env.feature.vars]\nWINTER_TOKEN = { command = \"hidden-value\" }\n");
        assert!(!format!("{managed:?}").contains("hidden"), "{managed:?}");
    }
}
