//! The architecture rules of the repository: which packages the workspace has and how they may depend on one
//! another, what a library source may name, and what the CI workflow and the Cargo aliases hold.
//!
//! Each rule is a function over inputs its caller passes in and answers the violations it finds; an empty answer
//! is a pass. Nothing here reads a file or starts a process: the integration test of this package gathers the
//! inputs from the checkout.

use std::collections::BTreeSet;
use std::fmt;

use cargo_metadata::{DependencyKind, Metadata};

/// The composition root: the package of the binary.
pub const CLI: &str = "winter-workspace";
/// The library: parsing, commands, domain, and adapters.
pub const CORE: &str = "winter-workspace-core";
/// Test support for the other packages.
pub const TEST_SUPPORT: &str = "winter-workspace-test-support";
/// The development driver, which holds these rules.
pub const DEV: &str = "winter-workspace-dev";

/// The packages the workspace has.
const MEMBERS: [&str; 4] = [CLI, CORE, DEV, TEST_SUPPORT];

/// The edges workspace packages may have between them: from, to, kind.
const ALLOWED_EDGES: [(&str, &str, EdgeKind); 4] = [
    (CLI, CORE, EdgeKind::Normal),
    (CLI, TEST_SUPPORT, EdgeKind::Dev),
    (CORE, TEST_SUPPORT, EdgeKind::Dev),
    (DEV, TEST_SUPPORT, EdgeKind::Normal),
];

/// The packages that are never published.
const UNPUBLISHED: [&str; 2] = [DEV, TEST_SUPPORT];

/// What no library source may name. The library returns values: streams, the process environment, and the exit
/// belong to the binary.
const FORBIDDEN_TOKENS: [&str; 15] = [
    "std::io::stdout",
    "std::io::stderr",
    "io::stdout(",
    "io::stderr(",
    "println!",
    "eprintln!",
    "print!",
    "eprint!",
    "dbg!",
    "std::process::exit",
    "process::exit(",
    "std::env::var",
    "std::env::vars",
    "std::env::args",
    "std::env::current_dir",
];

/// Forbidden in every library source but [`FILE_PORT`].
const FILE_SYSTEM_TOKEN: &str = "std::fs";

/// The one library source that may name [`FILE_SYSTEM_TOKEN`]: the file port and its adapter.
const FILE_PORT: &str = "crates/core/src/files.rs";

/// The values of the workflow's `run:` lines, in order.
const WORKFLOW_RUNS: [&str; 3] = [
    "rustup toolchain install",
    "cargo fetch --locked",
    "cargo dev gate full",
];

/// The values of the workflow's `uses:` lines, in order: one action, at a full commit id.
const WORKFLOW_USES: [&str; 1] = ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"];

/// The aliases `.cargo/config.toml` defines.
const ALIASES: [&str; 2] = ["dev", "scenarios"];

/// The kind of a dependency between two workspace packages.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EdgeKind {
    Normal,
    Dev,
    Build,
    /// A kind Cargo reports that this module does not know. No rule allows it.
    Unknown,
}

impl fmt::Display for EdgeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Normal => "normal",
            Self::Dev => "dev",
            Self::Build => "build",
            Self::Unknown => "unknown",
        })
    }
}

/// A dependency of one workspace package on another.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edge {
    /// The name of the package depended on.
    pub to: String,
    pub kind: EdgeKind,
}

/// A workspace package, as far as the rules read it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Package {
    pub name: String,
    /// False when the manifest declares `publish = false`.
    pub publishable: bool,
    /// Its dependencies on workspace packages; third-party dependencies are not edges.
    pub edges: Vec<Edge>,
}

/// One breach of a rule, carrying the offender.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Violation {
    /// The workspace has a package the layout does not list.
    UnlistedMember { package: String },
    /// The workspace lacks a package the layout lists.
    MissingMember { package: String },
    /// A workspace package depends on another in a way the layout does not list.
    UnlistedEdge {
        from: String,
        to: String,
        kind: EdgeKind,
    },
    /// A workspace package build-depends on another.
    BuildEdge { from: String, to: String },
    /// A package that is never published does not declare `publish = false`.
    Publishable { package: String },
    /// A library source names what the library may not name. `line` counts from 1.
    ForbiddenToken {
        file: String,
        line: usize,
        token: &'static str,
    },
    /// A library module is spelled `mod.rs`.
    ModFile { file: String },
    /// A `run:` or `uses:` line of the workflow is not the line expected at its position; `expected` is `None`
    /// when no line is expected there. `line` counts from 1.
    WorkflowLine {
        key: &'static str,
        line: usize,
        found: String,
        expected: Option<&'static str>,
    },
    /// The workflow lacks a `run:` or `uses:` line.
    MissingWorkflowLine {
        key: &'static str,
        expected: &'static str,
    },
    /// The Cargo configuration defines an alias that is not expected.
    UnlistedAlias { alias: String },
    /// The Cargo configuration lacks an expected alias.
    MissingAlias { alias: &'static str },
}

impl fmt::Display for Violation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnlistedMember { package } => {
                write!(
                    f,
                    "workspace member `{package}` is not a package of the layout"
                )
            }
            Self::MissingMember { package } => {
                write!(f, "the workspace has no member `{package}`")
            }
            Self::UnlistedEdge { from, to, kind } => {
                write!(f, "`{from}` may not depend on `{to}` ({kind} dependency)")
            }
            Self::BuildEdge { from, to } => {
                write!(
                    f,
                    "`{from}` build-depends on `{to}`; no workspace package may build-depend on another"
                )
            }
            Self::Publishable { package } => {
                write!(f, "`{package}` does not declare `publish = false`")
            }
            Self::ForbiddenToken { file, line, token } => {
                write!(f, "{file}:{line}: `{token}` is forbidden in the library")
            }
            Self::ModFile { file } => {
                write!(
                    f,
                    "{file}: a module `x` is `x.rs` with its children in `x/`, never `mod.rs`"
                )
            }
            Self::WorkflowLine {
                key,
                line,
                found,
                expected: Some(expected),
            } => {
                write!(
                    f,
                    "workflow line {line}: `{key} {found}` where `{key} {expected}` is expected"
                )
            }
            Self::WorkflowLine {
                key,
                line,
                found,
                expected: None,
            } => {
                write!(
                    f,
                    "workflow line {line}: `{key} {found}` where no further `{key}` line is expected"
                )
            }
            Self::MissingWorkflowLine { key, expected } => {
                write!(f, "the workflow has no line `{key} {expected}`")
            }
            Self::UnlistedAlias { alias } => {
                write!(
                    f,
                    "the Cargo configuration defines an unexpected alias `{alias}`"
                )
            }
            Self::MissingAlias { alias } => {
                write!(
                    f,
                    "the Cargo configuration does not define the alias `{alias}`"
                )
            }
        }
    }
}

/// The workspace packages of `metadata`, each with its edges to the others.
pub fn workspace_packages(metadata: &Metadata) -> Vec<Package> {
    let members = metadata.workspace_packages();
    members
        .iter()
        .map(|package| Package {
            name: package.name.to_string(),
            publishable: package
                .publish
                .as_ref()
                .is_none_or(|registries| !registries.is_empty()),
            edges: package
                .dependencies
                .iter()
                .filter(|dependency| {
                    members
                        .iter()
                        .any(|member| member.name.as_str() == dependency.name)
                })
                .map(|dependency| Edge {
                    to: dependency.name.clone(),
                    kind: match dependency.kind {
                        DependencyKind::Normal => EdgeKind::Normal,
                        DependencyKind::Development => EdgeKind::Dev,
                        DependencyKind::Build => EdgeKind::Build,
                        _ => EdgeKind::Unknown,
                    },
                })
                .collect(),
        })
        .collect()
}

/// The member set is exactly the packages of the layout: an extra member fails, and so does a missing one.
pub fn member_set(packages: &[Package]) -> Vec<Violation> {
    let unlisted = packages
        .iter()
        .filter(|package| !MEMBERS.contains(&package.name.as_str()))
        .map(|package| Violation::UnlistedMember {
            package: package.name.clone(),
        });
    let missing = MEMBERS
        .iter()
        .filter(|member| !packages.iter().any(|package| package.name == **member))
        .map(|member| Violation::MissingMember {
            package: (*member).to_owned(),
        });
    unlisted.chain(missing).collect()
}

/// Every edge between workspace packages is one the layout lists, with its kind, and none is a build edge.
pub fn edges(packages: &[Package]) -> Vec<Violation> {
    let mut violations = Vec::new();
    for package in packages {
        for edge in &package.edges {
            let from = package.name.clone();
            let to = edge.to.clone();
            if edge.kind == EdgeKind::Build {
                violations.push(Violation::BuildEdge { from, to });
            } else if !ALLOWED_EDGES.contains(&(package.name.as_str(), edge.to.as_str(), edge.kind))
            {
                violations.push(Violation::UnlistedEdge {
                    from,
                    to,
                    kind: edge.kind,
                });
            }
        }
    }
    violations
}

/// The packages that are never published declare `publish = false`.
pub fn unpublished(packages: &[Package]) -> Vec<Violation> {
    packages
        .iter()
        .filter(|package| UNPUBLISHED.contains(&package.name.as_str()) && package.publishable)
        .map(|package| Violation::Publishable {
            package: package.name.clone(),
        })
        .collect()
}

/// The forbidden tokens of one library source. `file` is its path from the repository root, with `/` between
/// components, and `source` its text.
///
/// The scan is over the text as it stands: a test module is checked like the rest, and so is a comment. A token
/// that overlaps one reported before it on its line is the same offence and is not reported again; of two that
/// start at the same place the longer is the one reported.
pub fn forbidden_tokens(file: &str, source: &str) -> Vec<Violation> {
    let file_system = (file != FILE_PORT).then_some(FILE_SYSTEM_TOKEN);
    let mut violations = Vec::new();
    for (index, text) in source.lines().enumerate() {
        let mut found: Vec<(usize, &'static str)> = FORBIDDEN_TOKENS
            .into_iter()
            .chain(file_system)
            .flat_map(|token| {
                text.match_indices(token)
                    .map(move |(start, _)| (start, token))
            })
            .collect();
        found.sort_by_key(|(start, token)| (*start, std::cmp::Reverse(token.len())));

        let mut covered = 0;
        for (start, token) in found {
            let end = start + token.len();
            if start >= covered {
                violations.push(Violation::ForbiddenToken {
                    file: file.to_owned(),
                    line: index + 1,
                    token,
                });
            }
            covered = covered.max(end);
        }
    }
    violations
}

/// No library module is spelled `mod.rs`. `file` is a path as [`forbidden_tokens`] takes it.
pub fn module_file(file: &str) -> Vec<Violation> {
    if file.rsplit('/').next() == Some("mod.rs") {
        vec![Violation::ModFile {
            file: file.to_owned(),
        }]
    } else {
        Vec::new()
    }
}

/// The `run:` lines of the CI workflow are the bootstrap and the full gate, in order, and its one `uses:` line
/// is the checkout action at its full commit id. `workflow` is the text of the workflow file.
///
/// The text is read by lines, not as YAML: a `run:` or `uses:` line is one that starts with that key after its
/// leading spaces and an optional `- `, and its value ends before a ` #` comment.
pub fn workflow(workflow: &str) -> Vec<Violation> {
    let mut violations = workflow_lines(workflow, "run:", &WORKFLOW_RUNS);
    violations.extend(workflow_lines(workflow, "uses:", &WORKFLOW_USES));
    violations
}

fn workflow_lines(workflow: &str, key: &'static str, expected: &[&'static str]) -> Vec<Violation> {
    let found: Vec<(usize, &str)> = workflow
        .lines()
        .enumerate()
        .filter_map(|(index, text)| {
            let text = text.trim_start_matches(' ');
            let value = text.strip_prefix("- ").unwrap_or(text).strip_prefix(key)?;
            let value = value.split(" #").next().unwrap_or(value);
            Some((index + 1, value.trim()))
        })
        .collect();

    let mut violations = Vec::new();
    for position in 0..found.len().max(expected.len()) {
        match (found.get(position), expected.get(position).copied()) {
            (Some((_, value)), Some(expected)) if *value == expected => {}
            (Some((line, value)), expected) => violations.push(Violation::WorkflowLine {
                key,
                line: *line,
                found: (*value).to_owned(),
                expected,
            }),
            (None, Some(expected)) => {
                violations.push(Violation::MissingWorkflowLine { key, expected });
            }
            (None, None) => {}
        }
    }
    violations
}

/// The names the `[alias]` table of a Cargo configuration defines. `config` is the text of the file; a file
/// without the table defines none.
pub fn alias_names(config: &str) -> Result<BTreeSet<String>, toml::de::Error> {
    let config: toml::Table = toml::from_str(config)?;
    Ok(config
        .get("alias")
        .and_then(toml::Value::as_table)
        .map(|aliases| aliases.keys().cloned().collect())
        .unwrap_or_default())
}

/// The Cargo configuration defines exactly the expected aliases. `names` is what [`alias_names`] answers.
pub fn aliases(names: &BTreeSet<String>) -> Vec<Violation> {
    let unlisted = names
        .iter()
        .filter(|name| !ALIASES.contains(&name.as_str()))
        .map(|name| Violation::UnlistedAlias {
            alias: name.clone(),
        });
    let missing = ALIASES
        .into_iter()
        .filter(|alias| !names.contains(*alias))
        .map(|alias| Violation::MissingAlias { alias });
    unlisted.chain(missing).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn package(name: &str, publishable: bool, edges: &[(&str, EdgeKind)]) -> Package {
        Package {
            name: name.to_owned(),
            publishable,
            edges: edges
                .iter()
                .map(|(to, kind)| Edge {
                    to: (*to).to_owned(),
                    kind: *kind,
                })
                .collect(),
        }
    }

    /// The workspace with every edge the layout allows.
    fn layout() -> Vec<Package> {
        vec![
            package(
                CLI,
                true,
                &[(CORE, EdgeKind::Normal), (TEST_SUPPORT, EdgeKind::Dev)],
            ),
            package(CORE, true, &[(TEST_SUPPORT, EdgeKind::Dev)]),
            package(DEV, false, &[(TEST_SUPPORT, EdgeKind::Normal)]),
            package(TEST_SUPPORT, false, &[]),
        ]
    }

    fn rendered(violations: &[Violation]) -> Vec<String> {
        violations.iter().map(Violation::to_string).collect()
    }

    const WORKFLOW: &str = "\
name: check
on: [push, pull_request]
jobs:
  gate:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: rustup toolchain install          # reads rust-toolchain.toml
      - run: cargo fetch --locked
      - run: cargo dev gate full
";

    #[test]
    fn the_layout_with_every_allowed_edge_passes_the_package_rules() {
        let packages = layout();

        assert_eq!(member_set(&packages), []);
        assert_eq!(edges(&packages), []);
        assert_eq!(unpublished(&packages), []);
    }

    #[test]
    fn a_fifth_member_is_named() {
        let mut packages = layout();
        packages.push(package("winter-workspace-extra", true, &[]));

        let violations = member_set(&packages);

        assert_eq!(
            violations,
            [Violation::UnlistedMember {
                package: "winter-workspace-extra".to_owned()
            }]
        );
        assert!(rendered(&violations)[0].contains("winter-workspace-extra"));
    }

    #[test]
    fn a_missing_member_is_named() {
        let mut packages = layout();
        packages.retain(|package| package.name != DEV);

        assert_eq!(
            member_set(&packages),
            [Violation::MissingMember {
                package: DEV.to_owned()
            }]
        );
    }

    #[test]
    fn a_normal_edge_from_the_library_to_test_support_is_named() {
        let mut packages = layout();
        packages[1].edges = vec![Edge {
            to: TEST_SUPPORT.to_owned(),
            kind: EdgeKind::Normal,
        }];

        let violations = edges(&packages);

        assert_eq!(
            violations,
            [Violation::UnlistedEdge {
                from: CORE.to_owned(),
                to: TEST_SUPPORT.to_owned(),
                kind: EdgeKind::Normal,
            }]
        );
        assert_eq!(
            rendered(&violations),
            [
                "`winter-workspace-core` may not depend on `winter-workspace-test-support` (normal dependency)"
            ]
        );
    }

    #[test]
    fn an_edge_to_a_package_outside_the_layout_is_named() {
        let mut packages = layout();
        packages[0].edges.push(Edge {
            to: "winter-workspace-extra".to_owned(),
            kind: EdgeKind::Normal,
        });

        assert_eq!(
            edges(&packages),
            [Violation::UnlistedEdge {
                from: CLI.to_owned(),
                to: "winter-workspace-extra".to_owned(),
                kind: EdgeKind::Normal,
            }]
        );
    }

    #[test]
    fn a_build_edge_is_named_even_between_packages_that_may_depend_otherwise() {
        let mut packages = layout();
        packages[0].edges.push(Edge {
            to: CORE.to_owned(),
            kind: EdgeKind::Build,
        });

        let violations = edges(&packages);

        assert_eq!(
            violations,
            [Violation::BuildEdge {
                from: CLI.to_owned(),
                to: CORE.to_owned(),
            }]
        );
        let message = &rendered(&violations)[0];
        assert!(message.contains("`winter-workspace` build-depends on `winter-workspace-core`"));
    }

    #[test]
    fn a_publishable_support_package_is_named() {
        let mut packages = layout();
        packages[3].publishable = true;

        let violations = unpublished(&packages);

        assert_eq!(
            violations,
            [Violation::Publishable {
                package: TEST_SUPPORT.to_owned()
            }]
        );
        assert!(rendered(&violations)[0].contains(TEST_SUPPORT));
    }

    #[test]
    fn the_product_packages_may_be_publishable() {
        assert_eq!(unpublished(&layout()), []);
    }

    #[test]
    fn workspace_packages_reads_members_edges_kinds_and_publish_from_cargo_metadata() {
        let dependency = |name: &str, kind: &str, path: &str| {
            format!(
                r#"{{"name":"{name}","source":null,"req":"*","kind":{kind},"rename":null,"optional":false,
                    "uses_default_features":true,"features":[],"target":null{path}}}"#
            )
        };
        let member = |name: &str, publish: &str, dependencies: &[String]| {
            format!(
                r#"{{"name":"{name}","version":"0.1.0","id":"path+file:///w/{name}#0.1.0","source":null,
                    "dependencies":[{}],"targets":[],"features":{{}},"manifest_path":"/w/{name}/Cargo.toml",
                    "publish":{publish},"edition":"2024"}}"#,
                dependencies.join(",")
            )
        };
        let local = r#","path":"/w/x""#;
        let json = format!(
            r#"{{"packages":[{},{},{}],
                "workspace_members":["path+file:///w/a#0.1.0","path+file:///w/b#0.1.0","path+file:///w/c#0.1.0"],
                "resolve":null,"target_directory":"/w/target","version":1,"workspace_root":"/w"}}"#,
            member(
                "a",
                "null",
                &[
                    dependency("b", "null", local),
                    dependency("c", r#""dev""#, local),
                    dependency("c", r#""build""#, local),
                    dependency("third-party", "null", ""),
                ]
            ),
            member("b", "[]", &[]),
            member("c", r#"["a-registry"]"#, &[]),
        );
        let metadata = cargo_metadata::MetadataCommand::parse(json).unwrap();

        assert_eq!(
            workspace_packages(&metadata),
            [
                package(
                    "a",
                    true,
                    &[
                        ("b", EdgeKind::Normal),
                        ("c", EdgeKind::Dev),
                        ("c", EdgeKind::Build)
                    ]
                ),
                package("b", false, &[]),
                package("c", true, &[]),
            ]
        );
    }

    #[test]
    fn a_print_macro_in_a_library_source_is_named_with_file_and_line() {
        let source = "fn greet() {\n    println!(\"hello\");\n}\n";

        let violations = forbidden_tokens("crates/core/src/greet.rs", source);

        assert_eq!(
            violations,
            [Violation::ForbiddenToken {
                file: "crates/core/src/greet.rs".to_owned(),
                line: 2,
                token: "println!",
            }]
        );
        assert_eq!(
            rendered(&violations),
            ["crates/core/src/greet.rs:2: `println!` is forbidden in the library"]
        );
    }

    #[test]
    fn an_environment_read_in_a_library_source_is_named() {
        let source = "\n\nlet home = std::env::var(\"HOME\");\n";

        assert_eq!(
            forbidden_tokens("crates/core/src/home.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/home.rs".to_owned(),
                line: 3,
                token: "std::env::var",
            }]
        );
    }

    #[test]
    fn the_file_system_in_a_library_source_other_than_the_file_port_is_named() {
        let source = "use std::fs;\n";

        assert_eq!(
            forbidden_tokens("crates/core/src/config.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/config.rs".to_owned(),
                line: 1,
                token: "std::fs",
            }]
        );
        assert_eq!(
            forbidden_tokens("crates/core/src/config/files.rs", source).len(),
            1,
            "only the file port itself is exempt, not a file that shares its name"
        );
    }

    #[test]
    fn the_file_system_in_the_file_port_is_allowed() {
        let source = "use std::fs;\nfn read() { let _ = std::fs::read(\"x\"); }\n";

        assert_eq!(forbidden_tokens("crates/core/src/files.rs", source), []);
    }

    #[test]
    fn the_file_port_is_held_to_every_other_token() {
        let source = "use std::fs;\nfn read() { eprintln!(\"reading\"); }\n";

        assert_eq!(
            forbidden_tokens("crates/core/src/files.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/files.rs".to_owned(),
                line: 2,
                token: "eprintln!",
            }]
        );
    }

    #[test]
    fn a_token_inside_a_test_module_of_the_library_is_reported() {
        let source = "\
pub fn answer() -> u8 {
    42
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_answers() {
        dbg!(super::answer());
    }
}
";

        assert_eq!(
            forbidden_tokens("crates/core/src/answer.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/answer.rs".to_owned(),
                line: 9,
                token: "dbg!",
            }]
        );
    }

    #[test]
    fn every_forbidden_token_is_found() {
        for token in FORBIDDEN_TOKENS.into_iter().chain([FILE_SYSTEM_TOKEN]) {
            let source = format!("let _ = {token};\n");

            let found: Vec<&str> = forbidden_tokens("crates/core/src/any.rs", &source)
                .into_iter()
                .map(|violation| match violation {
                    Violation::ForbiddenToken { token, .. } => token,
                    other => panic!("{other:?}"),
                })
                .collect();

            assert!(found.contains(&token), "{token}: {found:?}");
        }
    }

    #[test]
    fn overlapping_tokens_are_reported_once_and_separate_ones_on_a_line_are_each_reported() {
        let source = "std::io::stdout().flush(); std::process::exit(std::env::vars().count());\n";

        let tokens: Vec<&str> = forbidden_tokens("crates/core/src/any.rs", source)
            .into_iter()
            .map(|violation| match violation {
                Violation::ForbiddenToken { token, .. } => token,
                other => panic!("{other:?}"),
            })
            .collect();

        assert_eq!(
            tokens,
            ["std::io::stdout", "std::process::exit", "std::env::vars"]
        );
    }

    #[test]
    fn a_source_without_a_forbidden_token_passes() {
        let source =
            "use std::io::Write;\nfn write(out: &mut dyn Write) { let _ = writeln!(out); }\n";

        assert_eq!(forbidden_tokens("crates/core/src/write.rs", source), []);
    }

    #[test]
    fn a_mod_file_is_named() {
        let violations = module_file("crates/core/src/config/mod.rs");

        assert_eq!(
            violations,
            [Violation::ModFile {
                file: "crates/core/src/config/mod.rs".to_owned()
            }]
        );
        assert!(rendered(&violations)[0].starts_with("crates/core/src/config/mod.rs: "));
    }

    #[test]
    fn a_module_named_after_itself_passes() {
        assert_eq!(module_file("crates/core/src/config.rs"), []);
        assert_eq!(module_file("crates/core/src/config/model.rs"), []);
        assert_eq!(module_file("crates/core/src/commands/remod.rs"), []);
    }

    #[test]
    fn the_expected_workflow_passes() {
        assert_eq!(workflow(WORKFLOW), []);
    }

    #[test]
    fn a_fourth_run_line_is_named() {
        let text = format!("{WORKFLOW}      - run: cargo publish   # never\n");

        let violations = workflow(&text);

        assert_eq!(
            violations,
            [Violation::WorkflowLine {
                key: "run:",
                line: 11,
                found: "cargo publish".to_owned(),
                expected: None,
            }]
        );
        assert_eq!(
            rendered(&violations),
            ["workflow line 11: `run: cargo publish` where no further `run:` line is expected"]
        );
    }

    #[test]
    fn a_changed_and_a_missing_run_line_are_named() {
        let text = WORKFLOW
            .replace("cargo dev gate full", "cargo dev gate slice")
            .replace("      - run: cargo fetch --locked\n", "");

        assert_eq!(
            workflow(&text),
            [
                Violation::WorkflowLine {
                    key: "run:",
                    line: 9,
                    found: "cargo dev gate slice".to_owned(),
                    expected: Some("cargo fetch --locked"),
                },
                Violation::MissingWorkflowLine {
                    key: "run:",
                    expected: "cargo dev gate full",
                },
            ]
        );
    }

    #[test]
    fn a_uses_line_that_names_a_tag_is_named() {
        let text = WORKFLOW.replace(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            "actions/checkout@v7",
        );

        let violations = workflow(&text);

        assert_eq!(
            violations,
            [Violation::WorkflowLine {
                key: "uses:",
                line: 7,
                found: "actions/checkout@v7".to_owned(),
                expected: Some("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"),
            }]
        );
        assert!(rendered(&violations)[0].contains("`uses: actions/checkout@v7`"));
    }

    #[test]
    fn a_second_uses_line_is_named() {
        let text = format!("{WORKFLOW}      - uses: actions/cache@v5\n");

        assert_eq!(
            workflow(&text),
            [Violation::WorkflowLine {
                key: "uses:",
                line: 11,
                found: "actions/cache@v5".to_owned(),
                expected: None,
            }]
        );
    }

    #[test]
    fn a_workflow_without_a_uses_line_names_the_one_it_lacks() {
        let text: String = WORKFLOW
            .lines()
            .filter(|line| !line.contains("uses:"))
            .map(|line| format!("{line}\n"))
            .collect();

        assert_eq!(
            workflow(&text),
            [Violation::MissingWorkflowLine {
                key: "uses:",
                expected: "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            }]
        );
    }

    #[test]
    fn a_key_line_is_read_with_or_without_the_list_dash() {
        let text = "\
steps:
  - name: checkout
    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
  - name: toolchain
    run: rustup toolchain install
  - run: cargo fetch --locked
  - run: cargo dev gate full
";

        assert_eq!(workflow(text), []);
    }

    #[test]
    fn the_expected_aliases_pass() {
        let names = alias_names("[alias]\ndev = \"run\"\nscenarios = \"test\"\n").unwrap();

        assert_eq!(aliases(&names), []);
    }

    #[test]
    fn a_third_alias_is_named() {
        let config = "[alias]\ndev = \"run\"\nscenarios = \"test\"\nship = \"publish\"\n";

        let violations = aliases(&alias_names(config).unwrap());

        assert_eq!(
            violations,
            [Violation::UnlistedAlias {
                alias: "ship".to_owned()
            }]
        );
        assert!(rendered(&violations)[0].contains("`ship`"));
    }

    #[test]
    fn a_missing_alias_is_named() {
        let names = alias_names("[alias]\ndev = \"run\"\n").unwrap();

        assert_eq!(
            aliases(&names),
            [Violation::MissingAlias { alias: "scenarios" }]
        );
    }

    #[test]
    fn a_configuration_without_the_alias_table_defines_none() {
        let names = alias_names("[build]\njobs = 2\n").unwrap();

        assert!(names.is_empty());
        assert_eq!(
            aliases(&names),
            [
                Violation::MissingAlias { alias: "dev" },
                Violation::MissingAlias { alias: "scenarios" },
            ]
        );
    }

    #[test]
    fn a_configuration_that_is_not_toml_is_an_error() {
        assert!(alias_names("[alias\n").is_err());
    }
}
