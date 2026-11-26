//! The architecture rules of the repository: which packages the workspace has and how they may depend on one
//! another, what a library source may name, what a command file may name, which files may not name the reference
//! tool, what the CI workflow and the Cargo aliases hold, and what the contributor documents link to, contain,
//! and tell a reader to run.
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

/// The tokens each forbidden in every library source but the one named beside it: the file system, the start of
/// a process, and the clock, the sleep, the random draw, and the operating-system calls that the program port
/// alone reads, waits on, and makes.
const PORT_TOKENS: [(&str, &str); 6] = [
    ("std::fs", "crates/core/src/files.rs"),
    ("process::Command", "crates/core/src/programs.rs"),
    ("Instant::now", "crates/core/src/programs.rs"),
    ("thread::sleep", "crates/core/src/programs.rs"),
    ("fastrand", "crates/core/src/programs.rs"),
    ("rustix", "crates/core/src/programs.rs"),
];

/// The directory of the command files, from the repository root.
const COMMAND_FILES: &str = "crates/core/src/commands/";

/// What no command file may name. A command file holds its arguments, one call into a domain module, and the
/// mapping of that call's outcome; these are the names of what such a call stands on.
///
/// A deny-list: the change that adds a module only a domain entry may call adds its name here.
const COMMAND_FILE_NAMES: [&str; 26] = [
    // The file port.
    "ReadFiles",
    "WriteFiles",
    "OsFiles",
    "ScratchFiles",
    // The program port.
    "RunPrograms",
    "OsPrograms",
    // The modules a domain entry stands on.
    "workspace_root",
    "workspace_files",
    "config_file",
    "config_load",
    "config_lock",
    "config_merge",
    "config_model",
    "config_edit",
    "state_registry",
    "environments",
    "drift",
    "workspace_exclude",
    "git::",
    // The file formats.
    "toml::",
    "toml_edit",
    // Writing, and how a failed write ends.
    "writeln!",
    "write!(",
    "to_writer",
    "InternalError",
    "Backtrace",
];

/// The library sources that the compiled topic map is read through, from the repository root. They hold no
/// file read and no environment access: the map is embedded and the topics are the same in every run.
const TOPIC_FILES: [&str; 2] = [
    "crates/core/src/topics.rs",
    "crates/core/src/commands/context.rs",
];

/// What a topic file names besides [`COMMAND_FILE_NAMES`].
const TOPIC_FILE_EXTRA_NAMES: [&str; 2] = ["std::fs", "std::env"];

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

/// The entry point of the contributor documents.
const ENTRY_POINT: &str = "AGENTS.md";

/// The directory of the context tree, with its separator.
const CONTEXT_TREE: &str = "context/";

/// The name of the document that routes to the others of its directory.
const INDEX: &str = "index.md";

/// What no contributor document may contain, matched as substrings without regard to ASCII case. The documents
/// describe how to build this repository and nothing else.
const SCOPE_TOKENS: [&str; 8] = [
    "python",
    "uv run",
    "uv sync",
    "pytest",
    "pip install",
    "tools/winter-cli",
    ".venv",
    "skills/",
];

/// The one document of the context tree the scope rule does not read.
const SCOPE_EXEMPT: &str = "context/verification/scenarios.md";

/// The variable that carries the path of the reference tool. Only the development driver knows it.
const REFERENCE_VARIABLE: &str = "WINTER_REFERENCE_BIN";

/// The directories no file of which may contain [`REFERENCE_VARIABLE`], each with its separator.
const WITHOUT_REFERENCE_VARIABLE: [&str; 3] =
    ["crates/cli/", "crates/core/", "crates/test-support/"];

/// The language of the reference tool, matched without regard to ASCII case. The code that builds fixtures,
/// runs steps, records, and replays cannot name it.
const REFERENCE_LANGUAGE: &str = "python";

/// The directory no file of which may contain [`REFERENCE_LANGUAGE`], with its separator, and the one file
/// outside it that may not either.
const WITHOUT_REFERENCE_LANGUAGE: (&str, &str) =
    ("crates/test-support/", "crates/cli/tests/scenarios.rs");

/// The commands a contributor document may tell a reader to run, beside a `cargo test -p` call on a workspace
/// package. `cargo scenarios` also takes one filter, and `cargo dev record` is named without its arguments.
const DOCUMENTED_COMMANDS: [&str; 6] = [
    "cargo dev gate slice",
    "cargo dev gate full",
    "cargo dev fmt",
    "cargo dev record",
    "cargo scenarios",
    "cargo fetch --locked",
];

/// The headings of the three parts of the verifiability matrix.
const COMMANDS_PART: &str = "Commands";
const MANUAL_PART: &str = "Manual testing";
const TOOLS_PART: &str = "Tools";

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
    /// A command file names what only a domain module may name. `line` counts from 1.
    CommandFileName {
        file: String,
        line: usize,
        name: &'static str,
    },
    /// A topic file names what the embedded map never needs. `line` counts from 1.
    TopicFileName {
        file: String,
        line: usize,
        name: &'static str,
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
    /// A contributor document is not linked from the index that routes to it.
    Unlinked { file: String, index: String },
    /// A relative link of a contributor document names no file of the checkout. `line` counts from 1.
    DeadLink {
        file: String,
        line: usize,
        target: String,
    },
    /// A contributor document contains a scope token. `line` counts from 1.
    ScopeToken {
        file: String,
        line: usize,
        token: &'static str,
    },
    /// A file that may not name the reference tool's variable or its language contains one of the two. `line`
    /// counts from 1.
    ReferenceName {
        file: String,
        line: usize,
        token: &'static str,
    },
    /// A contributor document tells a reader to run a command that is not a documented one. `line` counts
    /// from 1.
    UnknownCommand {
        file: String,
        line: usize,
        command: String,
    },
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
            Self::CommandFileName { file, line, name } => {
                write!(
                    f,
                    "{file}:{line}: `{name}` is not named in a command file; it belongs to a domain module"
                )
            }
            Self::TopicFileName { file, line, name } => {
                write!(
                    f,
                    "{file}:{line}: `{name}` is not named in a topic file; the topics are embedded and read nothing at run time"
                )
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
            Self::Unlinked { file, index } => {
                write!(f, "{file}: not linked from {index}")
            }
            Self::DeadLink { file, line, target } => {
                write!(
                    f,
                    "{file}:{line}: the link to `{target}` names no file of the checkout"
                )
            }
            Self::ScopeToken { file, line, token } => {
                write!(
                    f,
                    "{file}:{line}: `{token}` is outside what a contributor document describes"
                )
            }
            Self::ReferenceName { file, line, token } => {
                write!(
                    f,
                    "{file}:{line}: `{token}` names the reference tool, which no file here may"
                )
            }
            Self::UnknownCommand {
                file,
                line,
                command,
            } => {
                write!(
                    f,
                    "{file}:{line}: `{command}` is neither a gate command nor a `cargo test -p` call on a workspace package"
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
    let ports = PORT_TOKENS
        .into_iter()
        .filter(|(_, allowed)| file != *allowed)
        .map(|(token, _)| token);
    let mut violations = Vec::new();
    for (index, text) in source.lines().enumerate() {
        let mut found: Vec<(usize, &'static str)> = FORBIDDEN_TOKENS
            .into_iter()
            .chain(ports.clone())
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

/// The names of [`COMMAND_FILE_NAMES`] that one command file holds: one violation for each line and each name
/// the line holds, however often it holds it. `file` and `source` are as [`forbidden_tokens`] takes them; a
/// file that is not under the directory of the command files has no violation.
///
/// The scan is over the text as it stands: a test module is checked like the rest, and so is a comment. It finds
/// the listed names and nothing else. A command file that holds none of them may still carry logic of its own or
/// call a module the list does not hold. It may also reach what the names stand for: an item of a listed module
/// through a re-export, by name or through a glob import of the crate root; a crate under another name, as after
/// `use toml as t;`; and the port through the handles of the context, whose methods are called with no type of
/// the port named. None of that is seen here.
pub fn command_file_names(file: &str, source: &str) -> Vec<Violation> {
    if !file.starts_with(COMMAND_FILES) {
        return Vec::new();
    }
    let mut violations = Vec::new();
    for (index, text) in source.lines().enumerate() {
        for name in COMMAND_FILE_NAMES {
            if text.contains(name) {
                violations.push(Violation::CommandFileName {
                    file: file.to_owned(),
                    line: index + 1,
                    name,
                });
            }
        }
    }
    violations
}

/// A topic file names none of the names of a command file, no file access, and no environment access. `file` is
/// a path as [`forbidden_tokens`] takes it; any other file has no violation.
pub fn topic_file_names(file: &str, source: &str) -> Vec<Violation> {
    if !TOPIC_FILES.contains(&file) {
        return Vec::new();
    }
    let mut violations = Vec::new();
    for (index, text) in source.lines().enumerate() {
        for name in COMMAND_FILE_NAMES.into_iter().chain(TOPIC_FILE_EXTRA_NAMES) {
            if text.contains(name) {
                violations.push(Violation::TopicFileName {
                    file: file.to_owned(),
                    line: index + 1,
                    name,
                });
            }
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

/// Whether `file`, a path from the repository root, is a document the context rules read: the entry point, or
/// a Markdown file of the context tree.
pub fn is_context_document(file: &str) -> bool {
    file == ENTRY_POINT || (file.starts_with(CONTEXT_TREE) && file.ends_with(".md"))
}

/// The document that routes to `file`, a document of the context tree: the index of its directory, for an
/// index the index of the directory above, and for the index of the tree the entry point.
fn routed_from(file: &str) -> String {
    let (directory, name) = file.rsplit_once('/').unwrap_or(("", file));
    if name != INDEX {
        return format!("{directory}/{INDEX}");
    }
    match directory.rsplit_once('/') {
        Some((above, _)) => format!("{above}/{INDEX}"),
        None => ENTRY_POINT.to_owned(),
    }
}

/// The lines of `text` outside fenced code blocks, each with its number from 1.
fn prose_lines(text: &str) -> Vec<(usize, &str)> {
    let mut fenced = false;
    let mut lines = Vec::new();
    for (index, line) in text.lines().enumerate() {
        if is_fence(line) {
            fenced = !fenced;
        } else if !fenced {
            lines.push((index + 1, line));
        }
    }
    lines
}

fn is_fence(line: &str) -> bool {
    let line = line.trim_start();
    line.starts_with("```") || line.starts_with("~~~")
}

/// The code spans of one line: the text between each pair of backticks.
fn code_spans(line: &str) -> Vec<&str> {
    line.split('`').skip(1).step_by(2).collect()
}

/// `line` with its code spans left out.
fn without_code_spans(line: &str) -> String {
    line.split('`').step_by(2).collect::<Vec<_>>().join(" ")
}

/// The targets of the inline links of `text`, each with the number of its line from 1. Fenced code blocks and
/// code spans hold no link.
pub fn link_targets(text: &str) -> Vec<(usize, String)> {
    let mut targets = Vec::new();
    for (line, content) in prose_lines(text) {
        let content = without_code_spans(content);
        for (start, _) in content.match_indices("](") {
            let rest = &content[start + 2..];
            let Some(end) = rest.find(')') else { continue };
            let target = rest[..end].split_whitespace().next().unwrap_or_default();
            let target = target.trim_start_matches('<').trim_end_matches('>');
            targets.push((line, target.to_owned()));
        }
    }
    targets
}

/// Where a link target leads.
enum Destination {
    /// Out of the checkout's files: another site, or a place in the document itself. Not checked.
    Elsewhere,
    /// A path from the repository root.
    File(String),
    /// Above the repository root.
    Outside,
}

/// Where `target`, a link target of the document `file`, leads. A fragment is not part of the path.
fn destination(file: &str, target: &str) -> Destination {
    let path = target.split('#').next().unwrap_or_default();
    if path.is_empty() || target.contains("://") || target.starts_with("mailto:") {
        return Destination::Elsewhere;
    }
    let mut parts: Vec<&str> = if path.starts_with('/') {
        Vec::new()
    } else {
        file.split('/').collect()
    };
    parts.pop();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.pop().is_none() {
                    return Destination::Outside;
                }
            }
            part => parts.push(part),
        }
    }
    Destination::File(parts.join("/"))
}

/// Whether the document `text`, at `file`, links to the file `to`.
fn links_to(file: &str, text: &str, to: &str) -> bool {
    link_targets(text).iter().any(
        |(_, target)| matches!(destination(file, target), Destination::File(path) if path == to),
    )
}

/// Every document of the context tree is linked from the index that routes to it: a document from the index
/// of its directory, an index from the index of the directory above, and the index of the tree from the entry
/// point. `documents` are the documents [`is_context_document`] accepts, each as its path from the repository
/// root and its text.
pub fn reachable(documents: &[(String, String)]) -> Vec<Violation> {
    documents
        .iter()
        .filter(|(file, _)| file != ENTRY_POINT)
        .filter_map(|(file, _)| {
            let index = routed_from(file);
            let linked = documents
                .iter()
                .any(|(from, text)| *from == index && links_to(from, text, file));
            (!linked).then(|| Violation::Unlinked {
                file: file.clone(),
                index,
            })
        })
        .collect()
}

/// Every relative link of the document `text`, at `file`, names a file of the checkout. `files` are the
/// checkout's files as paths from the repository root.
pub fn links_resolve(file: &str, text: &str, files: &BTreeSet<String>) -> Vec<Violation> {
    link_targets(text)
        .into_iter()
        .filter(|(_, target)| match destination(file, target) {
            Destination::Elsewhere => false,
            Destination::File(path) => !files.contains(&path),
            Destination::Outside => true,
        })
        .map(|(line, target)| Violation::DeadLink {
            file: file.to_owned(),
            line,
            target,
        })
        .collect()
}

/// The scope tokens of one contributor document. The recorded-scenario document alone is not read: it
/// describes the tool the scenarios are recorded from.
pub fn scope_tokens(file: &str, text: &str) -> Vec<Violation> {
    if file == SCOPE_EXEMPT {
        return Vec::new();
    }
    let mut violations = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let line = line.to_ascii_lowercase();
        for token in SCOPE_TOKENS {
            if line.contains(token) {
                violations.push(Violation::ScopeToken {
                    file: file.to_owned(),
                    line: index + 1,
                    token,
                });
            }
        }
    }
    violations
}

/// What `text`, the text of the file `file`, holds of the two names that keep the reference tool apart: the
/// variable that carries its path, in a file under the binary's, the library's, or the support package's
/// directory, and its language, in any letter case, in a file under the support package's directory or in
/// the replay target. `file` is a path from the repository root, with `/` between components. A file
/// elsewhere is not read.
///
/// The scan is over the text as it stands, source, test, comment, or data alike, and a line is reported once
/// for each name it holds.
pub fn reference_names(file: &str, text: &str) -> Vec<Violation> {
    let variable = WITHOUT_REFERENCE_VARIABLE
        .iter()
        .any(|directory| file.starts_with(directory));
    let (directory, target) = WITHOUT_REFERENCE_LANGUAGE;
    let language = file.starts_with(directory) || file == target;
    let mut violations = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let found = [
            (variable && line.contains(REFERENCE_VARIABLE)).then_some(REFERENCE_VARIABLE),
            (language && line.to_ascii_lowercase().contains(REFERENCE_LANGUAGE))
                .then_some(REFERENCE_LANGUAGE),
        ];
        for token in found.into_iter().flatten() {
            violations.push(Violation::ReferenceName {
                file: file.to_owned(),
                line: index + 1,
                token,
            });
        }
    }
    violations
}

/// The lines inside the fenced code blocks of `text` that are not blank, trimmed, each with its number from 1.
pub fn fenced_lines(text: &str) -> Vec<(usize, String)> {
    let mut fenced = false;
    let mut lines = Vec::new();
    for (index, line) in text.lines().enumerate() {
        if is_fence(line) {
            fenced = !fenced;
        } else if fenced && !line.trim().is_empty() {
            lines.push((index + 1, line.trim().to_owned()));
        }
    }
    lines
}

/// The lines of the part of `text` under the second-level heading `heading`, up to the next heading of that
/// level, each with its number from 1.
fn part<'a>(text: &'a str, heading: &str) -> Vec<(usize, &'a str)> {
    text.lines()
        .enumerate()
        .skip_while(|(_, line)| line.strip_prefix("## ").map(str::trim) != Some(heading))
        .skip(1)
        .take_while(|(_, line)| !line.starts_with("## "))
        .map(|(index, line)| (index + 1, line))
        .collect()
}

/// The cells of a table line, or `None` for a line that is no table line.
fn cells(line: &str) -> Option<Vec<&str>> {
    let inner = line.trim().strip_prefix('|')?;
    let inner = inner.strip_suffix('|').unwrap_or(inner);
    Some(inner.split('|').map(str::trim).collect())
}

/// The rows of the tables among `lines`, each as its cells: a table line that is neither the line under a
/// header nor the header above it.
fn table_rows<'a>(lines: &[(usize, &'a str)]) -> Vec<(usize, Vec<&'a str>)> {
    let is_rule = |cells: &[&str]| {
        cells
            .iter()
            .all(|cell| !cell.is_empty() && cell.chars().all(|c| matches!(c, '-' | ':')))
    };
    let tabled: Vec<(usize, Vec<&str>)> = lines
        .iter()
        .filter_map(|(line, text)| Some((*line, cells(text)?)))
        .collect();
    let mut rows = Vec::new();
    for (position, (line, row)) in tabled.iter().enumerate() {
        let heads = tabled
            .get(position + 1)
            .is_some_and(|(next, cells)| *next == line + 1 && is_rule(cells));
        if !heads && !is_rule(row) {
            rows.push((*line, row.clone()));
        }
    }
    rows
}

/// `cell` without the backticks of a code span that is all it holds.
fn unquoted(cell: &str) -> &str {
    cell.strip_prefix('`')
        .and_then(|cell| cell.strip_suffix('`'))
        .unwrap_or(cell)
}

/// The commands of the Commands and Tools parts of the verifiability matrix `text`, each with the number of
/// its line from 1: the whole second cell of every row of the Commands part, and every code span of either
/// part that starts with the word `cargo`.
pub fn matrix_commands(text: &str) -> Vec<(usize, String)> {
    let commands_part = part(text, COMMANDS_PART);
    let rows = table_rows(&commands_part);
    let mut commands: Vec<(usize, String)> = rows
        .iter()
        .map(|(line, cells)| {
            let command = cells.get(1).copied().unwrap_or_default();
            (*line, unquoted(command).to_owned())
        })
        .collect();

    let in_row = |line: usize| rows.iter().any(|(row, _)| *row == line);
    for (line, content) in commands_part.into_iter().chain(part(text, TOOLS_PART)) {
        if in_row(line) {
            continue;
        }
        for span in code_spans(content) {
            if span.split_whitespace().next() == Some("cargo") {
                commands.push((line, span.to_owned()));
            }
        }
    }
    commands.sort();
    commands
}

/// The method ids of the Commands part of the verifiability matrix `text`, in order: the first cell of every
/// row.
pub fn matrix_command_ids(text: &str) -> Vec<String> {
    first_cells(text, COMMANDS_PART)
}

/// The tool ids of the Tools part of the verifiability matrix `text`, in order: the first cell of every row.
pub fn matrix_tool_ids(text: &str) -> Vec<String> {
    first_cells(text, TOOLS_PART)
}

fn first_cells(text: &str, heading: &str) -> Vec<String> {
    table_rows(&part(text, heading))
        .into_iter()
        .map(|(_, cells)| unquoted(cells.first().copied().unwrap_or_default()).to_owned())
        .collect()
}

/// The entries of the Manual testing part of the verifiability matrix `text`, in order: each third-level
/// heading of the part gives an entry, whose id is the first code span of the heading and whose text is what
/// follows it up to the next heading.
pub fn matrix_manual_methods(text: &str) -> Vec<(String, String)> {
    let mut methods: Vec<(String, String)> = Vec::new();
    for (_, line) in part(text, MANUAL_PART) {
        if let Some(heading) = line.strip_prefix("### ") {
            let id = code_spans(heading).first().copied().unwrap_or(heading);
            methods.push((id.to_owned(), String::new()));
        } else if let Some((_, body)) = methods.last_mut() {
            body.push_str(line);
            body.push('\n');
        }
    }
    methods
}

/// Whether `command` is one a contributor document may tell a reader to run: a gate command, the record command
/// without arguments, `cargo scenarios` with or without one filter, or a `cargo test -p` call that names a
/// workspace package.
fn is_documented_command(command: &str) -> bool {
    let words: Vec<&str> = command.split_whitespace().collect();
    DOCUMENTED_COMMANDS.contains(&words.join(" ").as_str())
        || matches!(words.as_slice(), ["cargo", "scenarios", filter] if !filter.starts_with('-'))
        || matches!(words.as_slice(), ["cargo", "test", "-p", package, ..] if MEMBERS.contains(package))
}

/// Every command a contributor document tells a reader to run is a documented one. `commands` are the command
/// lines of the document `file`, as [`fenced_lines`] or [`matrix_commands`] answers them.
pub fn documented_commands(file: &str, commands: &[(usize, String)]) -> Vec<Violation> {
    commands
        .iter()
        .filter(|(_, command)| !is_documented_command(command))
        .map(|(line, command)| Violation::UnknownCommand {
            file: file.to_owned(),
            line: *line,
            command: command.clone(),
        })
        .collect()
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
    fn the_start_of_a_process_in_a_library_source_other_than_the_program_port_is_named() {
        let source = "use std::io;\nfn run() { let _ = std::process::Command::new(\"git\"); }\n";

        for file in [
            "crates/core/src/locations.rs",
            "crates/core/src/files.rs",
            "crates/core/src/locations/programs.rs",
        ] {
            assert_eq!(
                forbidden_tokens(file, source),
                [Violation::ForbiddenToken {
                    file: file.to_owned(),
                    line: 2,
                    token: "process::Command",
                }]
            );
        }
        assert_eq!(
            forbidden_tokens("crates/core/src/git.rs", "use process::Command;\n").len(),
            1,
            "the name is found however it is reached"
        );
    }

    #[test]
    fn the_start_of_a_process_in_the_program_port_is_allowed() {
        let source = "fn run() { let _ = std::process::Command::new(\"git\"); }\n";

        assert_eq!(forbidden_tokens("crates/core/src/programs.rs", source), []);
    }

    #[test]
    fn the_program_port_is_held_to_every_other_token_the_file_system_included() {
        let source =
            "use std::process::Command;\nfn find() { let _ = std::fs::metadata(\"git\"); }\n";

        assert_eq!(
            forbidden_tokens("crates/core/src/programs.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/programs.rs".to_owned(),
                line: 2,
                token: "std::fs",
            }]
        );
    }

    #[test]
    fn a_sleep_outside_the_program_port_is_reported_and_inside_it_is_allowed() {
        let source = "fn wait() { std::thread::sleep(d); }\n";

        assert_eq!(
            forbidden_tokens("crates/core/src/git.rs", source),
            [Violation::ForbiddenToken {
                file: "crates/core/src/git.rs".to_owned(),
                line: 1,
                token: "thread::sleep",
            }]
        );
        assert_eq!(forbidden_tokens("crates/core/src/programs.rs", source), []);
    }

    #[test]
    fn the_process_id_the_file_port_reads_is_no_start_of_a_process() {
        let source = "fn name() -> String { format!(\".{}.tmp\", std::process::id()) }\n";

        assert_eq!(forbidden_tokens("crates/core/src/files.rs", source), []);
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
        for token in FORBIDDEN_TOKENS
            .into_iter()
            .chain(PORT_TOKENS.map(|(token, _)| token))
        {
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

    const COMMAND_FILE: &str = "crates/core/src/commands/repo/add.rs";

    #[test]
    fn the_names_no_command_file_may_hold_are_these_twenty_six() {
        assert_eq!(
            COMMAND_FILE_NAMES,
            [
                "ReadFiles",
                "WriteFiles",
                "OsFiles",
                "ScratchFiles",
                "RunPrograms",
                "OsPrograms",
                "workspace_root",
                "workspace_files",
                "config_file",
                "config_load",
                "config_lock",
                "config_merge",
                "config_model",
                "config_edit",
                "state_registry",
                "environments",
                "drift",
                "workspace_exclude",
                "git::",
                "toml::",
                "toml_edit",
                "writeln!",
                "write!(",
                "to_writer",
                "InternalError",
                "Backtrace",
            ]
        );
    }

    #[test]
    fn each_listed_name_in_a_command_file_is_named_with_file_line_and_name() {
        for name in COMMAND_FILE_NAMES {
            let source = format!("fn run() {{\n    let _ = {name};\n}}\n");

            let violations = command_file_names(COMMAND_FILE, &source);

            assert_eq!(
                violations,
                [Violation::CommandFileName {
                    file: COMMAND_FILE.to_owned(),
                    line: 2,
                    name,
                }]
            );
            assert_eq!(
                rendered(&violations),
                [format!(
                    "{COMMAND_FILE}:2: `{name}` is not named in a command file; it belongs to a domain module"
                )]
            );
        }
    }

    #[test]
    fn a_listed_name_in_a_library_source_that_is_no_command_file_is_not_named() {
        for name in COMMAND_FILE_NAMES {
            let source = format!("fn run() {{\n    let _ = {name};\n}}\n");

            for file in [
                "crates/core/src/parse.rs",
                "crates/core/src/commands.rs",
                "crates/dev/src/commands/add.rs",
            ] {
                assert_eq!(command_file_names(file, &source), [], "{name} in {file}");
            }
        }
    }

    #[test]
    fn a_listed_name_inside_a_test_module_and_one_inside_a_comment_are_both_reported() {
        let source = "\
// The entry reads through ReadFiles.
fn run() {}

#[cfg(test)]
mod tests {
    use crate::config_edit::RepositoryKind;
}
";

        assert_eq!(
            command_file_names(COMMAND_FILE, source),
            [
                Violation::CommandFileName {
                    file: COMMAND_FILE.to_owned(),
                    line: 1,
                    name: "ReadFiles",
                },
                Violation::CommandFileName {
                    file: COMMAND_FILE.to_owned(),
                    line: 6,
                    name: "config_edit",
                },
            ]
        );
    }

    #[test]
    fn a_line_gives_one_violation_for_each_listed_name_it_holds() {
        let source = "use crate::files::{ReadFiles, WriteFiles}; fn f(a: &dyn ReadFiles) {}\n";

        let names: Vec<&str> = command_file_names(COMMAND_FILE, source)
            .into_iter()
            .map(|violation| match violation {
                Violation::CommandFileName { name, .. } => name,
                other => panic!("{other:?}"),
            })
            .collect();

        assert_eq!(names, ["ReadFiles", "WriteFiles"]);
    }

    #[test]
    fn a_command_file_that_names_what_a_command_file_holds_passes() {
        let source = "\
use crate::context::Context;
use crate::env_index::{IndexAnswer, IndexSource};
use crate::output;
use crate::registry::Registration;
use crate::termination::{ParseFault, Termination, UsageError};

fn run(cx: &mut Context<'_>) -> Termination {
    output::line(cx.out, \"done\")
}
";

        assert_eq!(command_file_names(COMMAND_FILE, source), []);
    }

    const TOPIC_FILE: &str = "crates/core/src/topics.rs";

    #[test]
    fn a_topic_file_that_names_the_file_port_is_reported_with_file_line_and_name() {
        let source = "fn eager() {\n    let _ = OsFiles;\n}\n";

        let violations = topic_file_names(TOPIC_FILE, source);

        assert_eq!(
            violations,
            [Violation::TopicFileName {
                file: TOPIC_FILE.to_owned(),
                line: 2,
                name: "OsFiles",
            }]
        );
        assert_eq!(
            rendered(&violations),
            [format!(
                "{TOPIC_FILE}:2: `OsFiles` is not named in a topic file; the topics are embedded and read nothing at run time"
            )]
        );
    }

    #[test]
    fn a_topic_file_that_names_the_temporary_directory_is_reported_as_environment_access() {
        let source = "let _ = std::env::temp_dir();\n";

        let names: Vec<&str> = topic_file_names(TOPIC_FILE, source)
            .into_iter()
            .map(|violation| match violation {
                Violation::TopicFileName { name, .. } => name,
                other => panic!("{other:?}"),
            })
            .collect();

        assert_eq!(names, ["std::env"]);
    }

    #[test]
    fn the_context_command_file_that_names_the_temporary_directory_is_reported() {
        let file = "crates/core/src/commands/context.rs";
        let source = "let _ = std::env::temp_dir();\n";

        let names: Vec<&str> = topic_file_names(file, source)
            .into_iter()
            .map(|violation| match violation {
                Violation::TopicFileName { name, .. } => name,
                other => panic!("{other:?}"),
            })
            .collect();

        assert_eq!(names, ["std::env"]);
        assert!(command_file_names(file, source).is_empty());
    }

    #[test]
    fn a_topic_file_that_names_the_file_system_is_reported() {
        assert_eq!(topic_file_names(TOPIC_FILE, "use std::fs;\n").len(), 1);
    }

    #[test]
    fn every_command_file_name_is_refused_in_a_topic_file() {
        for name in COMMAND_FILE_NAMES {
            let source = format!("let _ = {name};\n");

            assert!(!topic_file_names(TOPIC_FILE, &source).is_empty(), "{name}");
        }
    }

    #[test]
    fn a_file_that_is_no_topic_file_may_name_them() {
        let source = "let _ = OsFiles; std::env::temp_dir();\n";

        assert_eq!(topic_file_names("crates/core/src/files.rs", source), []);
    }

    #[test]
    fn a_topic_file_that_names_none_of_them_passes() {
        let source = "pub fn eager_text() -> &'static str {\n    SAFETY.text\n}\n";

        assert_eq!(topic_file_names(TOPIC_FILE, source), []);
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

    fn documents(documents: &[(&str, &str)]) -> Vec<(String, String)> {
        documents
            .iter()
            .map(|(file, text)| ((*file).to_owned(), (*text).to_owned()))
            .collect()
    }

    /// A context tree in which every document is linked from the index that routes to it.
    fn context_tree() -> Vec<(String, String)> {
        documents(&[
            ("AGENTS.md", "Start at [the context](./context/index.md).\n"),
            (
                "context/index.md",
                "| [tooling](./tooling/index.md) | You run a gate. |\n",
            ),
            (
                "context/tooling/index.md",
                "| [development.md](./development.md) | You run a gate. |\n",
            ),
            ("context/tooling/development.md", "Run the gate.\n"),
        ])
    }

    fn files(files: &[&str]) -> BTreeSet<String> {
        files.iter().map(|file| (*file).to_owned()).collect()
    }

    const MATRIX: &str = "\
# Verifiability matrix

## Commands

| Method                | Command                                     | Proves       |
| --------------------- | ------------------------------------------- | ------------ |
| `workspace:unit-test` | `cargo test -p winter-workspace-core --lib` | A pure rule. |
| `workspace:slice-gate` | `cargo dev gate slice`                     | Mergeable.   |

A narrower run names one file with `--test <subject>`.

## Manual testing

### `workspace:manual-gates`

Runs in a scratch copy of the checkout.

**Setup.** Inject one fault with `cargo build`.

### `workspace:manual-registration`

**Success.** Each run ends as stated.

## Tools

| Tool             | Use                                           |
| ---------------- | --------------------------------------------- |
| `tool:format`    | `cargo dev fmt` rewrites formatting.          |
| `tool:keep-tree` | `WINTER_TEST_KEEP=1` on any test command.     |
";

    #[test]
    fn a_tree_whose_every_document_is_linked_from_its_index_passes() {
        assert_eq!(reachable(&context_tree()), []);
    }

    #[test]
    fn a_document_no_index_links_is_named() {
        let mut tree = context_tree();
        tree.push((
            "context/tooling/orphan.md".to_owned(),
            "Nobody routes here.\n".to_owned(),
        ));

        let violations = reachable(&tree);

        assert_eq!(
            violations,
            [Violation::Unlinked {
                file: "context/tooling/orphan.md".to_owned(),
                index: "context/tooling/index.md".to_owned(),
            }]
        );
        assert_eq!(
            rendered(&violations),
            ["context/tooling/orphan.md: not linked from context/tooling/index.md"]
        );
    }

    #[test]
    fn a_document_linked_only_from_another_index_is_named() {
        let mut tree = context_tree();
        tree[0]
            .1
            .push_str("[direct](./context/tooling/development.md)\n");
        tree[2].1 = "No row.\n".to_owned();

        assert_eq!(
            reachable(&tree),
            [Violation::Unlinked {
                file: "context/tooling/development.md".to_owned(),
                index: "context/tooling/index.md".to_owned(),
            }]
        );
    }

    #[test]
    fn an_area_index_the_tree_index_does_not_link_and_a_tree_index_the_entry_point_does_not_link_are_named()
     {
        let mut tree = context_tree();
        tree[0].1 = "No routing.\n".to_owned();
        tree[1].1 = "No areas.\n".to_owned();

        assert_eq!(
            reachable(&tree),
            [
                Violation::Unlinked {
                    file: "context/index.md".to_owned(),
                    index: "AGENTS.md".to_owned(),
                },
                Violation::Unlinked {
                    file: "context/tooling/index.md".to_owned(),
                    index: "context/index.md".to_owned(),
                },
            ]
        );
    }

    #[test]
    fn a_document_whose_directory_has_no_index_is_named() {
        let mut tree = context_tree();
        tree.push(("context/extra/notes.md".to_owned(), "Notes.\n".to_owned()));

        assert_eq!(
            reachable(&tree),
            [Violation::Unlinked {
                file: "context/extra/notes.md".to_owned(),
                index: "context/extra/index.md".to_owned(),
            }]
        );
    }

    #[test]
    fn a_link_inside_a_code_span_or_a_fenced_block_links_nothing() {
        let mut tree = context_tree();
        tree[2].1 = "`[development.md](./development.md)`\n\n```text\n[development.md](./development.md)\n```\n"
            .to_owned();

        assert_eq!(reachable(&tree).len(), 1);
    }

    #[test]
    fn the_documents_the_context_rules_read_are_the_entry_point_and_the_markdown_of_the_tree() {
        assert!(is_context_document("AGENTS.md"));
        assert!(is_context_document("context/index.md"));
        assert!(is_context_document("context/standards/rust.md"));
        assert!(!is_context_document("README.md"));
        assert!(!is_context_document("context/standards/example.rs"));
        assert!(!is_context_document("crates/core/context/index.md"));
    }

    #[test]
    fn a_link_to_a_missing_file_is_named_with_file_and_line() {
        let text =
            "# Tooling\n\nSee [the gates](./gates.md) and [development](./development.md#gates).\n";
        let present = files(&["context/tooling/index.md", "context/tooling/development.md"]);

        let violations = links_resolve("context/tooling/index.md", text, &present);

        assert_eq!(
            violations,
            [Violation::DeadLink {
                file: "context/tooling/index.md".to_owned(),
                line: 3,
                target: "./gates.md".to_owned(),
            }]
        );
        assert_eq!(
            rendered(&violations),
            ["context/tooling/index.md:3: the link to `./gates.md` names no file of the checkout"]
        );
    }

    #[test]
    fn links_resolve_against_the_directory_of_the_document() {
        let text =
            "[up](../index.md) [root](../../AGENTS.md) [source](../../crates/dev/src/lib.rs)\n";
        let present = files(&["context/index.md", "AGENTS.md", "crates/dev/src/lib.rs"]);

        assert_eq!(
            links_resolve("context/tooling/development.md", text, &present),
            []
        );
        assert_eq!(
            links_resolve("context/development.md", text, &present).len(),
            3
        );
    }

    #[test]
    fn a_link_that_leaves_the_repository_is_named() {
        let violations = links_resolve("AGENTS.md", "[out](../elsewhere.md)\n", &files(&[]));

        assert_eq!(
            violations,
            [Violation::DeadLink {
                file: "AGENTS.md".to_owned(),
                line: 1,
                target: "../elsewhere.md".to_owned(),
            }]
        );
    }

    #[test]
    fn a_link_to_another_site_or_to_a_place_in_the_document_is_not_checked() {
        let text = "[site](https://example.invalid/a.md) [mail](mailto:a@example.invalid) [here](#gates)\n";

        assert_eq!(links_resolve("AGENTS.md", text, &files(&[])), []);
    }

    #[test]
    fn a_document_that_names_the_test_runner_of_another_language_is_named() {
        let text = "# Testing\n\nRun PyTest first.\n";

        let violations = scope_tokens("context/standards/testing.md", text);

        assert_eq!(
            violations,
            [Violation::ScopeToken {
                file: "context/standards/testing.md".to_owned(),
                line: 3,
                token: "pytest",
            }]
        );
        assert!(rendered(&violations)[0].starts_with("context/standards/testing.md:3: `pytest`"));
    }

    #[test]
    fn an_entry_point_that_names_another_tool_runner_is_named() {
        let text = "## Standing rules\n\n- Run `uv run check` before handing a change over.\n";

        assert_eq!(
            scope_tokens("AGENTS.md", text),
            [Violation::ScopeToken {
                file: "AGENTS.md".to_owned(),
                line: 3,
                token: "uv run",
            }]
        );
    }

    #[test]
    fn the_recorded_scenario_document_alone_is_not_read_for_scope() {
        let text = "The reference tool is written in python.\n";

        assert_eq!(scope_tokens("context/verification/scenarios.md", text), []);
        assert_eq!(
            scope_tokens("context/verification/matrix.md", text).len(),
            1
        );
        assert_eq!(scope_tokens("context/scenarios.md", text).len(), 1);
    }

    #[test]
    fn every_scope_token_is_found_in_any_letter_case() {
        for token in SCOPE_TOKENS {
            let text = format!("Before {} after.\n", token.to_ascii_uppercase());

            let found: Vec<&str> = scope_tokens("README.md", &text)
                .into_iter()
                .map(|violation| match violation {
                    Violation::ScopeToken { token, .. } => token,
                    other => panic!("{other:?}"),
                })
                .collect();

            assert!(found.contains(&token), "{token}: {found:?}");
        }
    }

    #[test]
    fn a_document_without_a_scope_token_passes() {
        assert_eq!(scope_tokens("README.md", "Build with `cargo build`.\n"), []);
    }

    #[test]
    fn the_variable_of_the_reference_is_named_under_the_library_the_binary_and_the_support_package()
    {
        let text = "fn f() {}\n// reads WINTER_REFERENCE_BIN\n";

        for file in [
            "crates/core/src/process.rs",
            "crates/cli/tests/root.rs",
            "crates/test-support/src/scenario/record.rs",
        ] {
            let violations = reference_names(file, text);

            assert_eq!(
                violations,
                [Violation::ReferenceName {
                    file: file.to_owned(),
                    line: 2,
                    token: "WINTER_REFERENCE_BIN",
                }]
            );
            assert!(rendered(&violations)[0].starts_with(&format!("{file}:2: ")));
        }
    }

    #[test]
    fn the_language_of_the_reference_is_named_under_the_support_package_and_in_the_replay_target() {
        let text = "fn f() {}\n\nconst SEED: &str = \"PYTHONHASHSEED\";\n";

        for file in [
            "crates/test-support/src/env.rs",
            "crates/cli/tests/scenarios.rs",
        ] {
            let violations = reference_names(file, text);

            assert_eq!(
                violations,
                [Violation::ReferenceName {
                    file: file.to_owned(),
                    line: 3,
                    token: "python",
                }]
            );
            assert!(rendered(&violations)[0].starts_with(&format!("{file}:3: `python`")));
        }
    }

    #[test]
    fn the_language_of_the_reference_is_found_in_any_letter_case_and_in_data() {
        for text in ["python", "Python 3", "a PyThOn b"] {
            assert_eq!(
                reference_names("crates/test-support/Cargo.toml", text).len(),
                1,
                "{text}"
            );
        }
    }

    #[test]
    fn the_product_may_name_the_language_of_the_reference_where_a_contract_of_its_own_does() {
        let text = "env.remove(\"PYTHONPATH\");\n";

        for file in [
            "crates/core/src/process.rs",
            "crates/core/src/commands/doctor.rs",
            "crates/cli/src/main.rs",
            "crates/cli/tests/root.rs",
        ] {
            assert_eq!(reference_names(file, text), [], "{file}");
        }
    }

    #[test]
    fn the_driver_may_name_both() {
        let text = "const A: &str = \"WINTER_REFERENCE_BIN\"; // python\n";

        assert_eq!(reference_names("crates/dev/src/record.rs", text), []);
        assert_eq!(reference_names("AGENTS.md", text), []);
    }

    #[test]
    fn a_line_that_holds_both_names_is_reported_for_each() {
        let text = "WINTER_REFERENCE_BIN=/x/python\n";

        let violations = reference_names("crates/test-support/tests/scenario_record.rs", text);

        assert_eq!(violations.len(), 2, "{violations:?}");
    }

    #[test]
    fn the_lines_of_fenced_blocks_are_read_without_the_fences_and_blank_lines() {
        let text = "Bootstrap:\n\n```bash\ncargo fetch --locked\n\n  cargo dev gate slice\n```\n\nThen `cargo build`.\n\n~~~\ncargo dev fmt\n~~~\n";

        assert_eq!(
            fenced_lines(text),
            [
                (4, "cargo fetch --locked".to_owned()),
                (6, "cargo dev gate slice".to_owned()),
                (12, "cargo dev fmt".to_owned()),
            ]
        );
    }

    #[test]
    fn the_gate_commands_and_package_tests_are_documented_commands() {
        for command in [
            "cargo dev gate slice",
            "cargo dev gate full",
            "cargo dev fmt",
            "cargo dev record",
            "cargo scenarios",
            "cargo scenarios some-filter",
            "cargo fetch --locked",
            "cargo test -p winter-workspace",
            "cargo test -p winter-workspace-core --lib",
            "cargo test -p winter-workspace-core --test '*'",
            "cargo test -p winter-workspace-dev",
            "cargo test -p winter-workspace-test-support --test tree",
        ] {
            let lines = [(1, command.to_owned())];

            assert_eq!(documented_commands("AGENTS.md", &lines), [], "{command}");
        }
    }

    #[test]
    fn a_command_that_is_no_gate_command_and_no_package_test_is_named() {
        for command in [
            "cargo build",
            "cargo test",
            "cargo test --workspace",
            "cargo test -p winter-workspace-extra",
            "cargo dev gate",
            "cargo dev record ws-index/alias-suggested",
            "cargo dev record --all-missing",
            "cargo dev fmt --check",
            "cargo scenarios --list",
            "cargo fetch",
            "git status",
            "# a comment",
        ] {
            let lines = [(7, command.to_owned())];

            let violations = documented_commands("context/tooling/development.md", &lines);

            assert_eq!(
                violations,
                [Violation::UnknownCommand {
                    file: "context/tooling/development.md".to_owned(),
                    line: 7,
                    command: command.to_owned(),
                }],
                "{command}"
            );
            assert!(
                rendered(&violations)[0].starts_with("context/tooling/development.md:7: `"),
                "{command}"
            );
        }
    }

    #[test]
    fn the_matrix_commands_are_the_command_cells_and_the_cargo_spans_of_the_commands_and_tools_parts()
     {
        assert_eq!(
            matrix_commands(MATRIX),
            [
                (7, "cargo test -p winter-workspace-core --lib".to_owned()),
                (8, "cargo dev gate slice".to_owned()),
                (28, "cargo dev fmt".to_owned()),
            ]
        );
    }

    #[test]
    fn a_command_cell_that_holds_no_command_is_still_read_as_one() {
        let text = MATRIX.replace(
            "`cargo dev gate slice`                    ",
            "see the gates",
        );

        assert!(matrix_commands(&text).contains(&(8, "see the gates".to_owned())));
    }

    #[test]
    fn a_cargo_span_under_the_commands_table_is_read_and_one_of_the_manual_part_is_not() {
        let text = MATRIX.replace(
            "A narrower run names one file with `--test <subject>`.",
            "A narrower run is `cargo test --workspace`.",
        );

        let commands = matrix_commands(&text);

        assert!(commands.contains(&(10, "cargo test --workspace".to_owned())));
        assert!(!commands.iter().any(|(_, command)| command == "cargo build"));
    }

    #[test]
    fn the_matrix_ids_are_read_from_each_part() {
        assert_eq!(
            matrix_command_ids(MATRIX),
            ["workspace:unit-test", "workspace:slice-gate"]
        );
        assert_eq!(matrix_tool_ids(MATRIX), ["tool:format", "tool:keep-tree"]);

        let manual = matrix_manual_methods(MATRIX);
        let ids: Vec<&str> = manual.iter().map(|(id, _)| id.as_str()).collect();
        assert_eq!(
            ids,
            ["workspace:manual-gates", "workspace:manual-registration"]
        );
        assert!(manual[0].1.contains("scratch copy"));
        assert!(manual[0].1.contains("**Setup.**"));
        assert!(!manual[0].1.contains("**Success.**"));
        assert!(manual[1].1.contains("**Success.**"));
    }

    #[test]
    fn a_matrix_without_a_part_has_nothing_in_it() {
        let text = "# Verifiability matrix\n\n## Commands\n\nNone.\n";

        assert!(matrix_commands(text).is_empty());
        assert!(matrix_command_ids(text).is_empty());
        assert!(matrix_manual_methods(text).is_empty());
        assert!(matrix_tool_ids(text).is_empty());
    }
}
