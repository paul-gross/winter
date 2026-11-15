//! The scenario description as types: `scenario.toml`, `reference.toml`, and `record/meta.toml`, the discovery
//! of scenario directories, and the description digest. Nothing here starts a process or creates a directory.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use sha1::{Digest, Sha1};

/// The description file of a scenario directory.
const DESCRIPTION: &str = "scenario.toml";
/// The file of the scenarios root that pins the reference.
const REFERENCE: &str = "reference.toml";
/// The directory of a scenario that holds its record.
const RECORD: &str = "record";
/// The file of a record that says what it was made with, relative to the scenario directory.
const META: &str = "record/meta.toml";
/// The directory of a scenario that holds the expectation of a registered deviation.
const EXPECTED: &str = "expected";
/// The directories of a scenario whose files the digest covers, in digest order.
const CONTENT_DIRECTORIES: [&str; 2] = ["files", "dirty"];
/// The one record format.
const FORMAT: i64 = 1;

/// A scenario description that breaks the format: which scenario, the key or name at fault, and why.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Malformed {
    /// The scenario id, `<command>/<name>`; for an entry of the scenarios root that is no scenario, its name.
    pub scenario: String,
    /// The key of the description at fault, or the directory or file name at fault.
    pub key: String,
    pub reason: String,
}

impl fmt::Display for Malformed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "scenario {} is malformed at `{}`: {}",
            self.scenario, self.key, self.reason
        )
    }
}

impl std::error::Error for Malformed {}

/// A key of `reference.toml` or `record/meta.toml` that breaks the format.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvalidKey {
    /// The key at fault; the file's name when the file is not TOML.
    pub key: String,
    pub reason: String,
}

impl fmt::Display for InvalidKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "`{}`: {}", self.key, self.reason)
    }
}

impl std::error::Error for InvalidKey {}

/// Something a scenario's behaviour needs beyond a process run to its end in a fixture tree.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Facility {
    Pty,
    Signal,
    Clock,
    Daemon,
    Follow,
    Provider,
    Network,
    Concurrency,
}

impl Facility {
    /// Every facility the format knows.
    pub const ALL: [Self; 8] = [
        Self::Pty,
        Self::Signal,
        Self::Clock,
        Self::Daemon,
        Self::Follow,
        Self::Provider,
        Self::Network,
        Self::Concurrency,
    ];

    /// The name a description declares the facility by.
    pub fn name(self) -> &'static str {
        match self {
            Self::Pty => "pty",
            Self::Signal => "signal",
            Self::Clock => "clock",
            Self::Daemon => "daemon",
            Self::Follow => "follow",
            Self::Provider => "provider",
            Self::Network => "network",
            Self::Concurrency => "concurrency",
        }
    }

    /// The facility of that name, when the format knows one.
    pub fn from_name(name: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|facility| facility.name() == name)
    }
}

/// How a step's stdout is stored and compared.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum StdoutMode {
    #[default]
    Text,
    Json,
    JsonLines,
}

/// How a file of the resulting state is compared.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileMode {
    Bytes,
    Toml,
    Presence,
    Ignore,
}

/// One scenario: the content of its `scenario.toml`, with its id and its directory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Scenario {
    /// `<command>/<name>`, the last two components of the directory.
    pub id: String,
    /// The scenario directory.
    pub dir: PathBuf,
    pub summary: String,
    pub needs: Vec<Facility>,
    pub fixture: Fixture,
    pub steps: Vec<Step>,
    pub compare: Compare,
    pub stubs: Vec<Stub>,
    pub deviation: Option<Deviation>,
}

/// The state a scenario starts from, built in this order: remotes, checkouts, worktrees, raw git calls.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Fixture {
    pub workspace_git: bool,
    pub gitignore: Option<String>,
    pub remotes: Vec<Remote>,
    pub checkouts: Vec<Checkout>,
    pub worktrees: Vec<Worktree>,
    pub git: Vec<GitCall>,
}

/// A bare repository of the fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Remote {
    pub name: String,
    pub default_branch: String,
    pub commits: Vec<Commit>,
}

/// One commit of a remote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Commit {
    pub message: String,
    /// Content by path, relative to the root of the repository.
    pub files: BTreeMap<String, String>,
    pub delete: Vec<String>,
    pub branch: Option<String>,
    pub tag: Option<String>,
}

/// A clone of a remote inside the workspace.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkout {
    pub remote: String,
    pub path: String,
}

/// A worktree of a checkout.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Worktree {
    pub checkout: String,
    pub path: String,
    pub branch: String,
    pub start: String,
}

/// A raw git call of the fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitCall {
    pub cwd: String,
    pub args: Vec<String>,
}

/// One invocation of the executable under test.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step {
    pub args: Vec<String>,
    /// Relative to the workspace directory.
    pub cwd: String,
    /// A file of the scenario directory, relative to it.
    pub stdin: Option<String>,
    pub stdout: StdoutMode,
    pub env: BTreeMap<String, String>,
}

/// The comparison rules of a scenario.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Compare {
    pub message_text: Vec<String>,
    pub git_config_ignore: Vec<String>,
    pub fan_out: Vec<FanOut>,
    pub files: Vec<FileRule>,
}

/// Documents whose order between partitions is not compared.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FanOut {
    pub types: Vec<String>,
    pub key: String,
}

/// How the files a glob matches are compared.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileRule {
    pub path: String,
    pub mode: FileMode,
    pub reason: String,
}

/// A recording program placed on the fixture's `PATH`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stub {
    pub name: String,
    pub exit: u8,
    pub stdout: Option<String>,
    pub env: Vec<String>,
}

/// The registered deviation a scenario's expectation is written under.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Deviation {
    pub decision: String,
}

impl Scenario {
    /// Parses the scenario directory `dir`, whose id is its last two components.
    ///
    /// The description is malformed when it is absent or not TOML, holds a key the format does not define,
    /// lacks a required one, names an unknown facility, has no step, names a path that is not confined, or
    /// has `expected/` without `[deviation]` or the reverse.
    pub fn load(dir: &Path) -> Result<Self, Malformed> {
        let name = dir.file_name().map(|name| name.to_string_lossy());
        let command = dir
            .parent()
            .and_then(Path::file_name)
            .map(|command| command.to_string_lossy());
        let (command, name) = (command.unwrap_or_default(), name.unwrap_or_default());
        let id = format!("{command}/{name}");
        for part in [&command, &name] {
            if !is_directory_name(part) {
                return Err(Malformed {
                    scenario: id.clone(),
                    key: part.to_string(),
                    reason: DIRECTORY_NAME_RULE.to_owned(),
                });
            }
        }
        Self::load_as(id, dir)
    }

    /// Parses the scenario directory `dir` under the id `id`, both names of which are valid.
    fn load_as(id: String, dir: &Path) -> Result<Self, Malformed> {
        let malformed = |invalid: InvalidKey| Malformed {
            scenario: id.clone(),
            key: invalid.key,
            reason: invalid.reason,
        };
        let text = std::fs::read_to_string(dir.join(DESCRIPTION))
            .map_err(|error| invalid(DESCRIPTION, format!("cannot be read: {error}")))
            .map_err(malformed)?;
        let description = parse_description(&text, dir).map_err(malformed)?;
        Ok(Self {
            id,
            dir: dir.to_path_buf(),
            summary: description.summary,
            needs: description.needs,
            fixture: description.fixture,
            steps: description.steps,
            compare: description.compare,
            stubs: description.stubs,
            deviation: description.deviation,
        })
    }

    /// The description digest: the SHA-1, in lowercase hexadecimal, over the bytes of `scenario.toml`, then
    /// every regular file under `files/`, then under `dirty/`, then the stdin files the steps name. Each
    /// group is sorted by scenario-relative path bytes, and each file contributes its path, a NUL byte, its
    /// length in decimal, a NUL byte, and its content.
    pub fn description_digest(&self) -> io::Result<String> {
        let mut hasher = Sha1::new();
        hasher.update(std::fs::read(self.dir.join(DESCRIPTION))?);
        let mut paths = Vec::new();
        for directory in CONTENT_DIRECTORIES {
            let mut group = Vec::new();
            regular_files(&self.dir, Path::new(directory), &mut group)?;
            group.sort_by(|a, b| a.as_os_str().as_bytes().cmp(b.as_os_str().as_bytes()));
            paths.extend(group);
        }
        let stdin: BTreeSet<&[u8]> = self
            .steps
            .iter()
            .filter_map(|step| step.stdin.as_deref())
            .map(str::as_bytes)
            .collect();
        paths.extend(
            stdin
                .into_iter()
                .map(|path| PathBuf::from(std::ffi::OsStr::from_bytes(path))),
        );
        for path in paths {
            let content = std::fs::read(self.dir.join(&path))?;
            hasher.update(path.as_os_str().as_bytes());
            hasher.update(b"\0");
            hasher.update(content.len().to_string());
            hasher.update(b"\0");
            hasher.update(content);
        }
        Ok(hasher
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect())
    }
}

/// Adds every regular file under `relative`, a directory of `dir`, to `found`, as paths relative to `dir`.
/// No link is followed, and a directory that is absent holds nothing.
fn regular_files(dir: &Path, relative: &Path, found: &mut Vec<PathBuf>) -> io::Result<()> {
    let directory = dir.join(relative);
    match directory.symlink_metadata() {
        Ok(metadata) if metadata.is_dir() => {}
        Ok(_) => return Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    }
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        let path = relative.join(entry.file_name());
        let kind = entry.file_type()?;
        if kind.is_dir() {
            regular_files(dir, &path, found)?;
        } else if kind.is_file() {
            found.push(path);
        }
    }
    Ok(())
}

/// What discovery found a scenario directory to be.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum State {
    /// The description parses and the directory holds `record/`.
    Recorded,
    /// The description parses and the directory holds no `record/`.
    Unrecorded,
    /// The description breaks the format, or the entry is not where a scenario may be.
    Malformed(Malformed),
}

/// One entry discovery reports.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Discovered {
    /// `<command>/<name>`; for an entry of the scenarios root that is no scenario, its name.
    pub id: String,
    pub dir: PathBuf,
    pub state: State,
}

/// Every scenario under `root`, sorted by the bytes of its id: each directory exactly two levels under the
/// root, with its state. `reference.toml` is no scenario. A directory one level down that holds no directory,
/// any other file of the root, and a directory whose name is outside the pattern are reported as malformed.
pub fn discover(root: &Path) -> io::Result<Vec<Discovered>> {
    let mut found = Vec::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        let command = entry.file_name().to_string_lossy().into_owned();
        let command_dir = entry.path();
        let not_a_scenario = |reason: &str| Discovered {
            id: command.clone(),
            dir: command_dir.clone(),
            state: State::Malformed(Malformed {
                scenario: command.clone(),
                key: command.clone(),
                reason: reason.to_owned(),
            }),
        };
        if !entry.file_type()?.is_dir() {
            if command != REFERENCE {
                found.push(not_a_scenario(
                    "the scenarios root holds command directories and reference.toml alone",
                ));
            }
            continue;
        }
        if !is_directory_name(&command) {
            found.push(not_a_scenario(DIRECTORY_NAME_RULE));
            continue;
        }
        let mut scenarios = 0;
        for entry in std::fs::read_dir(&command_dir)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            scenarios += 1;
            let name = entry.file_name().to_string_lossy().into_owned();
            let id = format!("{command}/{name}");
            let dir = entry.path();
            let state = if !is_directory_name(&name) {
                State::Malformed(Malformed {
                    scenario: id.clone(),
                    key: name,
                    reason: DIRECTORY_NAME_RULE.to_owned(),
                })
            } else {
                match Scenario::load_as(id.clone(), &dir) {
                    Err(malformed) => State::Malformed(malformed),
                    Ok(_) if is_directory(&dir.join(RECORD)) => State::Recorded,
                    Ok(_) => State::Unrecorded,
                }
            };
            found.push(Discovered { id, dir, state });
        }
        if scenarios == 0 {
            found.push(not_a_scenario(
                "a command directory holds at least one scenario directory",
            ));
        }
    }
    found.sort_by(|a, b| a.id.as_bytes().cmp(b.id.as_bytes()));
    Ok(found)
}

/// The pinned reference, `reference.toml` of the scenarios root.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reference {
    /// The commit of the reference tool's checkout every record is made at: 40 hexadecimal digits.
    pub commit: String,
}

impl Reference {
    /// Parses the text of a `reference.toml`.
    pub fn parse(text: &str) -> Result<Self, InvalidKey> {
        let table = parse_toml(text, REFERENCE)?;
        let top = Section::new(&table, "reference.toml", &["commit"])?;
        Ok(Self {
            commit: top.object_id("commit")?,
        })
    }

    /// Reads `reference.toml` of the scenarios root `root`.
    pub fn load(root: &Path) -> io::Result<Self> {
        let path = root.join(REFERENCE);
        Self::parse(&std::fs::read_to_string(&path)?).map_err(|error| invalid_data(&path, &error))
    }
}

/// What a record was made with, `record/meta.toml` of a scenario directory.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Meta {
    /// The commit of the reference the record was made at.
    pub reference_commit: String,
    /// The version of the git the record ran with.
    pub git_version: String,
    /// The first line the reference prints for its version.
    pub reference_runtime: String,
    /// The digest of the pinned environment the record was made under.
    pub environment: String,
    /// The description digest of the scenario when it was recorded.
    pub description: String,
}

impl Meta {
    /// Parses the text of a `meta.toml`, whose `format` is 1.
    pub fn parse(text: &str) -> Result<Self, InvalidKey> {
        let table = parse_toml(text, "meta.toml")?;
        let top = Section::new(
            &table,
            "meta.toml",
            &[
                "format",
                "reference_commit",
                "git_version",
                "reference_runtime",
                "environment",
                "description",
            ],
        )?;
        let format = top.required_integer("format")?;
        if format != FORMAT {
            return Err(top.invalid("format", format!("{format} is not the format {FORMAT}")));
        }
        Ok(Self {
            reference_commit: top.object_id("reference_commit")?,
            git_version: top.required_str("git_version")?.to_owned(),
            reference_runtime: top.required_str("reference_runtime")?.to_owned(),
            environment: top.object_id("environment")?,
            description: top.object_id("description")?,
        })
    }

    /// Reads `record/meta.toml` of the scenario directory `dir`.
    pub fn load(dir: &Path) -> io::Result<Self> {
        let path = dir.join(META);
        Self::parse(&std::fs::read_to_string(&path)?).map_err(|error| invalid_data(&path, &error))
    }

    /// The text of the `meta.toml` that parses to this value.
    pub fn to_toml(&self) -> String {
        let quoted = |value: &str| toml::Value::String(value.to_owned()).to_string();
        format!(
            "format = {FORMAT}\nreference_commit = {}\ngit_version = {}\nreference_runtime = {}\n\
             environment = {}\ndescription = {}\n",
            quoted(&self.reference_commit),
            quoted(&self.git_version),
            quoted(&self.reference_runtime),
            quoted(&self.environment),
            quoted(&self.description),
        )
    }
}

/// The error of a file at `path` whose key breaks the format.
fn invalid_data(path: &Path, error: &InvalidKey) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!("{}: {error}", path.display()),
    )
}

/// What a scenario or command directory name has to be.
const DIRECTORY_NAME_RULE: &str = "a scenario or command directory name matches [a-z0-9][a-z0-9-]*";

/// Whether `name` matches `[a-z0-9][a-z0-9-]*`.
fn is_directory_name(name: &str) -> bool {
    let plain = |byte: u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
    name.bytes().next().is_some_and(plain) && name.bytes().all(|byte| plain(byte) || byte == b'-')
}

/// Whether `name` is one path segment, `[A-Za-z0-9][A-Za-z0-9._-]*`.
fn is_one_segment(name: &str) -> bool {
    name.bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

/// Whether `name` matches `[a-z][a-z0-9-]*`, the form of a git subcommand.
fn is_subcommand(name: &str) -> bool {
    name.bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_lowercase())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

/// Whether `id` is 40 lowercase hexadecimal digits.
fn is_object_id(id: &str) -> bool {
    id.len() == 40
        && id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Why `path` is not a relative path that stays at or below its base, when it is not: it is empty, holds a
/// NUL, starts with `/`, or, read segment by segment, has a prefix that ends above the base.
pub(super) fn unconfined(path: &str) -> Option<String> {
    if path.is_empty() {
        return Some("the path is empty".to_owned());
    }
    if path.contains('\0') {
        return Some("the path holds a NUL".to_owned());
    }
    if path.starts_with('/') {
        return Some(format!("{path:?} is absolute"));
    }
    let mut depth = 0usize;
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => match depth.checked_sub(1) {
                Some(above) => depth = above,
                None => return Some(format!("{path:?} leads above its base")),
            },
            _ => depth += 1,
        }
    }
    None
}

/// Why `args` may not be the arguments of a raw git call of the fixture, when they may not.
fn refused_git_args(args: &[String]) -> Option<String> {
    match args.first() {
        None => return Some("a git call names a subcommand".to_owned()),
        Some(first) if !is_subcommand(first) => {
            return Some(format!(
                "{first:?} is not a git subcommand, which matches [a-z][a-z0-9-]*"
            ));
        }
        Some(_) => {}
    }
    args.iter().find_map(|arg| {
        let names_a_path_outside = arg.starts_with('/')
            || arg.starts_with('~')
            || arg.contains("=/")
            || arg.contains("=~")
            || arg.split(['/', '=']).any(|segment| segment == "..");
        names_a_path_outside.then(|| {
            format!("{arg:?} names an absolute path, a home directory, or a parent directory")
        })
    })
}

/// Why `stdin`, a confined relative path, is not a regular file under the scenario directory `dir` once
/// every link is resolved, when it is not.
fn refused_stdin(dir: &Path, stdin: &str) -> Option<String> {
    let Ok(base) = dir.canonicalize() else {
        return Some("the scenario directory cannot be resolved".to_owned());
    };
    let Ok(file) = dir.join(stdin).canonicalize() else {
        return Some(format!("{stdin:?} names no file of the scenario directory"));
    };
    if !file.starts_with(&base) {
        return Some(format!("{stdin:?} resolves outside the scenario directory"));
    }
    if !file.is_file() {
        return Some(format!("{stdin:?} is not a regular file"));
    }
    None
}

/// Whether `path` is a directory itself, not a link to one.
fn is_directory(path: &Path) -> bool {
    path.symlink_metadata()
        .is_ok_and(|metadata| metadata.is_dir())
}

/// What `scenario.toml` holds.
struct Description {
    summary: String,
    needs: Vec<Facility>,
    fixture: Fixture,
    steps: Vec<Step>,
    compare: Compare,
    stubs: Vec<Stub>,
    deviation: Option<Deviation>,
}

/// Parses `text`, the `scenario.toml` of the scenario directory `dir`.
fn parse_description(text: &str, dir: &Path) -> Result<Description, InvalidKey> {
    let table = parse_toml(text, DESCRIPTION)?;
    let top = Section::new(
        &table,
        "the top level",
        &[
            "summary",
            "needs",
            "fixture",
            "step",
            "compare",
            "stub",
            "deviation",
        ],
    )?;

    let summary = top.required_str("summary")?.to_owned();
    let needs = top
        .strs("needs")?
        .into_iter()
        .map(|name| {
            Facility::from_name(&name)
                .ok_or_else(|| top.invalid("needs", format!("{name:?} is not a known facility")))
        })
        .collect::<Result<_, _>>()?;

    let fixture = match top.section(
        "fixture",
        &[
            "workspace_git",
            "gitignore",
            "remote",
            "checkout",
            "worktree",
            "git",
        ],
    )? {
        Some(section) => parse_fixture(&section)?,
        None => Fixture::default(),
    };

    let steps = top
        .sections("step", &["args", "cwd", "stdin", "stdout", "env"])?
        .iter()
        .map(|section| parse_step(section, dir))
        .collect::<Result<Vec<_>, _>>()?;
    if steps.is_empty() {
        return Err(top.invalid("step", "a scenario has at least one [[step]]"));
    }

    let compare = match top.section(
        "compare",
        &["message_text", "git_config_ignore", "fan_out", "file"],
    )? {
        Some(section) => parse_compare(&section)?,
        None => Compare::default(),
    };

    let stubs = top
        .sections("stub", &["name", "exit", "stdout", "env"])?
        .iter()
        .map(parse_stub)
        .collect::<Result<_, _>>()?;

    let deviation = top
        .section("deviation", &["decision"])?
        .map(|section| {
            Ok(Deviation {
                decision: section.required_str("decision")?.to_owned(),
            })
        })
        .transpose()?;
    match (&deviation, is_directory(&dir.join(EXPECTED))) {
        (Some(_), false) => {
            return Err(top.invalid("deviation", "[deviation] is present without expected/"));
        }
        (None, true) => {
            return Err(top.invalid("expected", "expected/ is present without [deviation]"));
        }
        _ => {}
    }

    Ok(Description {
        summary,
        needs,
        fixture,
        steps,
        compare,
        stubs,
        deviation,
    })
}

fn parse_fixture(section: &Section<'_>) -> Result<Fixture, InvalidKey> {
    let remotes = section
        .sections("remote", &["name", "default_branch", "commit"])?
        .iter()
        .map(|remote| {
            let commits = remote
                .sections("commit", &["message", "files", "delete", "branch", "tag"])?
                .iter()
                .map(|commit| {
                    let files = commit.str_table("files")?;
                    for path in files.keys() {
                        commit.confined("files", path)?;
                    }
                    let delete = commit.strs("delete")?;
                    for path in &delete {
                        commit.confined("delete", path)?;
                    }
                    Ok(Commit {
                        message: commit.required_str("message")?.to_owned(),
                        files,
                        delete,
                        branch: commit.str("branch")?.map(str::to_owned),
                        tag: commit.str("tag")?.map(str::to_owned),
                    })
                })
                .collect::<Result<_, _>>()?;
            Ok(Remote {
                name: remote.one_segment("name")?,
                default_branch: remote.str("default_branch")?.unwrap_or("main").to_owned(),
                commits,
            })
        })
        .collect::<Result<_, _>>()?;

    let checkouts = section
        .sections("checkout", &["remote", "path"])?
        .iter()
        .map(|checkout| {
            Ok(Checkout {
                remote: checkout.required_str("remote")?.to_owned(),
                path: checkout.confined_path("path")?,
            })
        })
        .collect::<Result<_, _>>()?;

    let worktrees = section
        .sections("worktree", &["checkout", "path", "branch", "start"])?
        .iter()
        .map(|worktree| {
            Ok(Worktree {
                checkout: worktree.confined_path("checkout")?,
                path: worktree.confined_path("path")?,
                branch: worktree.required_str("branch")?.to_owned(),
                start: worktree.required_str("start")?.to_owned(),
            })
        })
        .collect::<Result<_, _>>()?;

    let git = section
        .sections("git", &["cwd", "args"])?
        .iter()
        .map(|call| {
            let args = call.required_strs("args")?;
            if let Some(reason) = refused_git_args(&args) {
                return Err(call.invalid("args", reason));
            }
            Ok(GitCall {
                cwd: call.confined_path("cwd")?,
                args,
            })
        })
        .collect::<Result<_, _>>()?;

    Ok(Fixture {
        workspace_git: section.bool("workspace_git")?.unwrap_or(false),
        gitignore: section.str("gitignore")?.map(str::to_owned),
        remotes,
        checkouts,
        worktrees,
        git,
    })
}

fn parse_step(section: &Section<'_>, dir: &Path) -> Result<Step, InvalidKey> {
    let args = section.required_strs("args")?;
    if let Some(arg) = args.iter().find(|arg| arg.starts_with('/')) {
        return Err(section.invalid("args", format!("{arg:?} starts with \"/\"")));
    }
    let cwd = section.str("cwd")?.unwrap_or(".").to_owned();
    section.confined("cwd", &cwd)?;
    let stdin = section.str("stdin")?.map(str::to_owned);
    if let Some(stdin) = &stdin {
        section.confined("stdin", stdin)?;
        if let Some(reason) = refused_stdin(dir, stdin) {
            return Err(section.invalid("stdin", reason));
        }
    }
    let stdout = match section.str("stdout")? {
        None | Some("text") => StdoutMode::Text,
        Some("json") => StdoutMode::Json,
        Some("json-lines") => StdoutMode::JsonLines,
        Some(other) => {
            return Err(section.invalid(
                "stdout",
                format!("{other:?} is not one of \"text\", \"json\", \"json-lines\""),
            ));
        }
    };
    let env = section.str_table("env")?;
    if let Some((name, value)) = env.iter().find(|(_, value)| value.starts_with('/')) {
        return Err(section.invalid(
            "env",
            format!(
                "the value {value:?} of {name} starts with \"/\"; a path is named through <ROOT>"
            ),
        ));
    }
    Ok(Step {
        args,
        cwd,
        stdin,
        stdout,
        env,
    })
}

fn parse_compare(section: &Section<'_>) -> Result<Compare, InvalidKey> {
    let fan_out = section
        .sections("fan_out", &["types", "key"])?
        .iter()
        .map(|fan_out| {
            Ok(FanOut {
                types: fan_out.required_strs("types")?,
                key: fan_out.required_str("key")?.to_owned(),
            })
        })
        .collect::<Result<_, _>>()?;
    let files = section
        .sections("file", &["path", "mode", "reason"])?
        .iter()
        .map(|file| {
            let mode = match file.required_str("mode")? {
                "bytes" => FileMode::Bytes,
                "toml" => FileMode::Toml,
                "presence" => FileMode::Presence,
                "ignore" => FileMode::Ignore,
                other => {
                    return Err(file.invalid(
                        "mode",
                        format!(
                            "{other:?} is not one of \"bytes\", \"toml\", \"presence\", \"ignore\""
                        ),
                    ));
                }
            };
            Ok(FileRule {
                path: file.required_str("path")?.to_owned(),
                mode,
                reason: file.required_str("reason")?.to_owned(),
            })
        })
        .collect::<Result<_, _>>()?;
    Ok(Compare {
        message_text: section.strs("message_text")?,
        git_config_ignore: section.strs("git_config_ignore")?,
        fan_out,
        files,
    })
}

fn parse_stub(section: &Section<'_>) -> Result<Stub, InvalidKey> {
    let exit = section.required_integer("exit")?;
    let exit = u8::try_from(exit)
        .map_err(|_| section.invalid("exit", format!("{exit} is not an exit code, 0 to 255")))?;
    Ok(Stub {
        name: section.one_segment("name")?,
        exit,
        stdout: section.str("stdout")?.map(str::to_owned),
        env: section.strs("env")?,
    })
}

fn invalid(key: &str, reason: impl Into<String>) -> InvalidKey {
    InvalidKey {
        key: key.to_owned(),
        reason: reason.into(),
    }
}

/// Parses `text` as TOML; an error names `file`.
fn parse_toml(text: &str, file: &str) -> Result<toml::Table, InvalidKey> {
    text.parse::<toml::Table>()
        .map_err(|error| invalid(file, format!("is not TOML: {}", error.message())))
}

/// One table of a file, every key of which the format defines.
struct Section<'a> {
    table: &'a toml::Table,
    /// How an error names the table: `[fixture]`, or `[[step]] 2`.
    place: String,
}

impl<'a> Section<'a> {
    /// The table `table`, when it holds no key outside `keys`.
    fn new(table: &'a toml::Table, place: &str, keys: &[&str]) -> Result<Self, InvalidKey> {
        let section = Self {
            table,
            place: place.to_owned(),
        };
        match table.keys().find(|key| !keys.contains(&key.as_str())) {
            Some(unknown) => Err(section.invalid(unknown, "the format defines no such key")),
            None => Ok(section),
        }
    }

    fn invalid(&self, key: &str, reason: impl Into<String>) -> InvalidKey {
        invalid(key, format!("in {}: {}", self.place, reason.into()))
    }

    fn missing(&self, key: &str) -> InvalidKey {
        self.invalid(key, "the key is required")
    }

    /// How an error names the table at `key` of this one.
    fn place_of(&self, key: &str, array: bool) -> String {
        let path = match self.place.trim_matches(['[', ']']).split(' ').next() {
            Some(parent) if self.place.starts_with('[') => format!("{parent}.{key}"),
            _ => key.to_owned(),
        };
        if array {
            format!("[[{path}]]")
        } else {
            format!("[{path}]")
        }
    }

    fn str(&self, key: &str) -> Result<Option<&'a str>, InvalidKey> {
        match self.table.get(key) {
            None => Ok(None),
            Some(toml::Value::String(value)) => Ok(Some(value)),
            Some(_) => Err(self.invalid(key, "the value is not a string")),
        }
    }

    fn required_str(&self, key: &str) -> Result<&'a str, InvalidKey> {
        self.str(key)?.ok_or_else(|| self.missing(key))
    }

    fn bool(&self, key: &str) -> Result<Option<bool>, InvalidKey> {
        match self.table.get(key) {
            None => Ok(None),
            Some(toml::Value::Boolean(value)) => Ok(Some(*value)),
            Some(_) => Err(self.invalid(key, "the value is not a boolean")),
        }
    }

    fn required_integer(&self, key: &str) -> Result<i64, InvalidKey> {
        match self.table.get(key) {
            None => Err(self.missing(key)),
            Some(toml::Value::Integer(value)) => Ok(*value),
            Some(_) => Err(self.invalid(key, "the value is not an integer")),
        }
    }

    /// The strings of the list at `key`; none when the key is absent.
    fn strs(&self, key: &str) -> Result<Vec<String>, InvalidKey> {
        let not_strings = || self.invalid(key, "the value is not a list of strings");
        match self.table.get(key) {
            None => Ok(Vec::new()),
            Some(toml::Value::Array(values)) => values
                .iter()
                .map(|value| value.as_str().map(str::to_owned).ok_or_else(not_strings))
                .collect(),
            Some(_) => Err(not_strings()),
        }
    }

    fn required_strs(&self, key: &str) -> Result<Vec<String>, InvalidKey> {
        match self.table.contains_key(key) {
            true => self.strs(key),
            false => Err(self.missing(key)),
        }
    }

    /// The table of strings at `key`; empty when the key is absent.
    fn str_table(&self, key: &str) -> Result<BTreeMap<String, String>, InvalidKey> {
        let not_strings = || self.invalid(key, "the value is not a table of strings");
        match self.table.get(key) {
            None => Ok(BTreeMap::new()),
            Some(toml::Value::Table(table)) => table
                .iter()
                .map(|(name, value)| {
                    let value = value.as_str().ok_or_else(not_strings)?;
                    Ok((name.clone(), value.to_owned()))
                })
                .collect(),
            Some(_) => Err(not_strings()),
        }
    }

    /// The table at `key`, which holds no key outside `keys`.
    fn section(&self, key: &str, keys: &[&str]) -> Result<Option<Section<'a>>, InvalidKey> {
        match self.table.get(key) {
            None => Ok(None),
            Some(toml::Value::Table(table)) => {
                Section::new(table, &self.place_of(key, false), keys).map(Some)
            }
            Some(_) => Err(self.invalid(key, "the value is not a table")),
        }
    }

    /// The tables of the list at `key`, none of which holds a key outside `keys`; none when the key is
    /// absent.
    fn sections(&self, key: &str, keys: &[&str]) -> Result<Vec<Section<'a>>, InvalidKey> {
        let not_tables = || self.invalid(key, "the value is not a list of tables");
        match self.table.get(key) {
            None => Ok(Vec::new()),
            Some(toml::Value::Array(values)) => values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    let table = value.as_table().ok_or_else(not_tables)?;
                    let place = format!("{} {}", self.place_of(key, true), index + 1);
                    Section::new(table, &place, keys)
                })
                .collect(),
            Some(_) => Err(not_tables()),
        }
    }

    /// Fails when `path`, a value or a name of the key `key`, is not a confined relative path.
    fn confined(&self, key: &str, path: &str) -> Result<(), InvalidKey> {
        match unconfined(path) {
            Some(reason) => Err(self.invalid(key, reason)),
            None => Ok(()),
        }
    }

    /// The required string at `key`, a confined relative path.
    fn confined_path(&self, key: &str) -> Result<String, InvalidKey> {
        let path = self.required_str(key)?;
        self.confined(key, path)?;
        Ok(path.to_owned())
    }

    /// The required string at `key`, one path segment.
    fn one_segment(&self, key: &str) -> Result<String, InvalidKey> {
        let name = self.required_str(key)?;
        if is_one_segment(name) {
            Ok(name.to_owned())
        } else {
            Err(self.invalid(
                key,
                format!("{name:?} is not one segment, which matches [A-Za-z0-9][A-Za-z0-9._-]*"),
            ))
        }
    }

    /// The required string at `key`, 40 lowercase hexadecimal digits.
    fn object_id(&self, key: &str) -> Result<String, InvalidKey> {
        let id = self.required_str(key)?;
        if is_object_id(id) {
            Ok(id.to_owned())
        } else {
            Err(self.invalid(key, format!("{id:?} is not 40 hexadecimal digits")))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_relative_path_stays_at_or_below_its_base() {
        for path in [".", "feature-one/alpha", "a/../b", "a/b/../..", "./a"] {
            assert_eq!(unconfined(path), None, "{path}");
        }
        for path in ["", "/tmp/x", "..", "../ws", "a/../../b", "a\0b", "./.."] {
            assert!(unconfined(path).is_some(), "{path:?}");
        }
    }

    #[test]
    fn a_directory_name_is_lowercase_digits_and_dashes() {
        for name in ["ws-index", "doctor", "0", "a-"] {
            assert!(is_directory_name(name), "{name}");
        }
        for name in ["", "-a", "Ws", "a_b", "a.b", "a/b"] {
            assert!(!is_directory_name(name), "{name:?}");
        }
    }

    #[test]
    fn one_segment_holds_no_separator_and_starts_plain() {
        for name in ["alpha", "a.b_c-d", "9"] {
            assert!(is_one_segment(name), "{name}");
        }
        for name in ["", "../x", ".x", "a/b", "-a"] {
            assert!(!is_one_segment(name), "{name:?}");
        }
    }

    #[test]
    fn git_arguments_start_with_a_subcommand_and_name_no_outside_path() {
        let args = |args: &[&str]| args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>();
        for accepted in [
            &["commit", "--allow-empty", "-m", "local"][..],
            &["checkout", "-b", "feature/one"],
            &["log", "main..HEAD~1"],
        ] {
            assert_eq!(refused_git_args(&args(accepted)), None, "{accepted:?}");
        }
        for refused in [
            &[][..],
            &["-C", "x", "status"],
            &["-c", "a=b", "status"],
            &["--git-dir=/tmp/x", "status"],
            &["status", "--git-dir=/tmp/x"],
            &["add", "/tmp/x"],
            &["add", "~/x"],
            &["config", "core.hooksPath=~/x"],
            &["add", "a/../../b"],
            &["add", ".."],
            &["status", "--work-tree=../x"],
        ] {
            assert!(refused_git_args(&args(refused)).is_some(), "{refused:?}");
        }
    }

    #[test]
    fn every_facility_is_found_by_its_name() {
        for facility in Facility::ALL {
            assert_eq!(Facility::from_name(facility.name()), Some(facility));
        }
        assert_eq!(Facility::from_name("terminal"), None);
    }
}
