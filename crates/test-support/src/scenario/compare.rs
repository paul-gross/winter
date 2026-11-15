//! The comparison of what a scenario recorded with what was observed: two runs of the same steps, each in
//! the form `record/steps/**` has in memory, read under the rules of the scenario's `[compare]`, and
//! answered as the list of their differences. No difference means the observation agrees with the record.
//!
//! Nothing here reads a file, starts a process, or looks at a tree: [`differences`], [`fixture`],
//! [`stale_record`], and [`overlay`] take values and answer values.
//!
//! [`differences`] compares, step by step and in this order: the exit; the JSON output of a step whose
//! stdout mode stores JSON; and of the state after the step its listing, the content of its files, and the
//! sections of its repositories. A rule of the scenario is the only thing that leaves a part of these out,
//! and a rule is never dropped in silence:
//!
//! - a rule that does not read, a pointer that is neither empty nor starts with `/`, an escape `~` that
//!   is neither `~0` nor `~1`, a path that is no confined relative path or no glob, a fan-out without a
//!   type or with a type another fan-out lists, a configuration key git could not have written, is one
//!   difference of [`Dimension::Malformed`] whose subject is the key, and nothing else is compared;
//! - a rule that relaxes nothing, a message-text pointer no string of the recorded output is at, a fan-out
//!   no recorded value has a type of, a file rule that decides the mode of no path either side lists, is
//!   one difference in the dimension the rule would have relaxed, without a step.
//!
//! A number of recorded or observed steps other than the number the scenario describes is one difference
//! of [`Dimension::Malformed`] with the subject `step`, and nothing else is compared.
//!
//! # What is never compared
//!
//! The bytes of stderr, and the bytes of stdout as text: white space and the order of keys in a JSON
//! stream, the text of a line that is not JSON under `json-lines`, and all of stdout under the mode
//! `text`. The snapshot before a step is not compared here; [`fixture`] compares the first one. The keys
//! of [`Compare::git_config_ignore`] were left out when a snapshot was taken, so whether one of them
//! names a key any repository has cannot be told from two snapshots, and is not reported.
//!
//! # JSON
//!
//! `stdout.json` is the canonical form of the stream, which merges streams that differ. A side whose
//! `stdout.json` is the canonical form of its own stdout is therefore read again from that stdout, with
//! every number kept as it is written and every member of an object kept: integers beyond 64 bits, a
//! repeated key, and a line `{"$text": …}` against a line that is not JSON all differ. A side whose
//! `stdout.json` is not that form, as a record whose `stdout.json` an expectation replaced, is read from
//! its `stdout.json` as that is written, with its numbers and members kept likewise. Each side is read on
//! its own, so what the canonical form merges in an observation still differs from an expectation, and
//! an expectation states a number as the observation prints it, not as the canonical form rounds it.
//!
//! Numbers compare by their value as decimals of any length, so `1`, `1.0`, and `1e0` are equal, and
//! `0.1` and `0.10000000000000000001`, which are one floating-point number, are not. An object compares
//! member by member, a key that is repeated in the order it is written.
//!
//! Under `json-lines` a line that is not JSON equals any other such line. Where a side is read from
//! its `stdout.json`, an object whose one member is a string at `$text` stands for such a line, as the
//! form stores it. Under `json` no value stands for a line.
//!
//! A pointer of `message_text` is read against each value of the stream, and `*` in it stands for any one
//! key or index, so a key that is `*` cannot be named alone. The `key` of a fan-out is a plain pointer.
//! The values of a fan-out run that lack the key, or hold it twice, form one partition.
//!
//! The subject of a difference in a value is its pointer, the empty pointer for the value as a whole; in
//! a stream that holds more or less than one value on either side, `value <n>`, counted from 1 in the
//! record, stands before it, with the place in the observation where that is another, and
//! `observed value <n>` where the record has no such value. Two values that are written without white
//! space between them are read as two only where one is a string, an array, or an object.
//!
//! # State
//!
//! A path under an `ignore` rule is left out of the listing and of the files altogether, whatever kind of
//! entry it is. Under `toml` and `presence` the entry is compared without its hash. The glob of a rule is
//! matched against the path of the tree, the root-relative one a line of `tree.txt` holds: `*` stands for
//! any run of bytes inside one segment, a leading dot included, a segment `**` for any number of
//! segments, none included, and every other byte for itself.
//!
//! The content of a file is stored only by the step that changed it, so the files are compared where a
//! side stored content for the step, and for a `toml` path wherever the two hashes differ. A `toml` path
//! whose content on one side was stored by no step, because the file is as the fixture made it, cannot be
//! parsed and is reported as a difference. A `bytes` path whose two hashes are equal is equal; its
//! content is compared as well where both sides stored it. A file whose two hashes differ and whose
//! content neither side stored for the step is a difference of the listing alone.
//!
//! Equal SHA-1 hashes stand for equal content wherever content is not at hand. The sections of the
//! repositories are compared as the lines a snapshot holds, with what the snapshot leaves out of them.
//! A file an `ignore` rule leaves out still shows in the `status` block of its repository.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;

use super::model::{self, Compare, FileMode, Meta, Reference, Scenario, StdoutMode};
use super::normalize;
use super::snapshot::{self, Entry, EntryKind, Repository, Snapshot, SnapshotError};
use crate::process::Exit;

/// The file of a step directory that holds how the step ended.
pub const EXIT_CODE_FILE: &str = "exit-code";

/// The file of a step directory that holds what the step wrote to its output.
pub const STDOUT_FILE: &str = "stdout";

/// The file of a step directory that holds what the step wrote to its error output.
pub const STDERR_FILE: &str = "stderr";

/// The file of a step directory that holds the canonical form of a JSON output.
pub const STDOUT_JSON_FILE: &str = "stdout.json";

/// What an exit that is a signal starts with in the exit file.
const SIGNAL_PREFIX: &str = "signal:";

/// What a difference shows for a side that lacks the subject.
const ABSENT: &str = "absent";

/// The key of the one-member object a line that is not JSON is stored as.
const TEXT_KEY: &str = "$text";

/// The member of a value that names its type, for a fan-out.
const TYPE_KEY: &str = "type";

/// How deep the arrays and objects of a value may nest, the limit the canonical form is parsed under.
const DEPTH: usize = 128;

/// What two runs can differ in.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Dimension {
    /// How a step ended.
    Exit,
    /// The JSON output of a step.
    Json,
    /// The entries of the tree after a step.
    Tree,
    /// The content of the files after a step.
    Files,
    /// The repositories after a step.
    Git,
    /// The calls of the programs a step started.
    Calls,
    /// The fixture, before the first step.
    Fixture,
    /// What the record was made with.
    StaleRecord,
    /// The scenario itself.
    Malformed,
}

/// One thing in which the observation is not the record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Difference {
    pub dimension: Dimension,
    /// The step, counted from 1; `None` for what belongs to no step.
    pub step: Option<u32>,
    /// What differs: a JSON pointer, a root-relative path, a repository and its block, or a key.
    pub subject: String,
    pub recorded: String,
    pub observed: String,
}

/// What one step did, as `record/steps/<nn>/` stores it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StepRecord {
    pub exit: Exit,
    /// The bytes of stdout, with the root replaced.
    pub stdout: Vec<u8>,
    /// The bytes of stderr, with the root replaced.
    pub stderr: Vec<u8>,
    /// The canonical form of stdout, for a step whose stdout mode stores JSON.
    pub stdout_json: Option<String>,
    /// The state before the step.
    pub before: Snapshot,
    /// The state after the step, its stored content that of the files the step changed.
    pub after: Snapshot,
}

/// How `exit` is written in the exit file, without the line end: the code in decimal, or `signal:<n>`.
pub fn exit_text(exit: Exit) -> String {
    match exit {
        Exit::Code(code) => code.to_string(),
        Exit::Signal(signal) => format!("{SIGNAL_PREFIX}{signal}"),
    }
}

/// The exit `text`, the content of an exit file, stands for: one line, with or without its line end, that
/// is a code or a signal in decimal digits and nothing else.
pub fn parse_exit(text: &str) -> Option<Exit> {
    let line = text.strip_suffix('\n').unwrap_or(text);
    let number = |digits: &str| -> Option<i32> {
        let plain = !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit());
        plain.then(|| digits.parse().ok()).flatten()
    };
    match line.strip_prefix(SIGNAL_PREFIX) {
        Some(signal) => number(signal).map(Exit::Signal),
        None => number(line).map(Exit::Code),
    }
}

/// Every difference between `recorded` and `observed`, the steps of `scenario` run twice, under the
/// scenario's rules; empty when the observation agrees with the record. The module says what is compared
/// and how a rule that does not read, or relaxes nothing, is answered.
///
/// The differences are in the order of the steps, and within a step exit, JSON, listing, files,
/// repositories; those that belong to no step come last.
pub fn differences(
    scenario: &Scenario,
    recorded: &[StepRecord],
    observed: &[StepRecord],
) -> Vec<Difference> {
    let rules = match Rules::new(&scenario.compare) {
        Ok(rules) => rules,
        Err(malformed) => return malformed,
    };
    let described = scenario.steps.len();
    if recorded.len() != described || observed.len() != described {
        let steps = |count: usize| format!("{count} of {described} described steps");
        return vec![Difference {
            dimension: Dimension::Malformed,
            step: None,
            subject: "step".to_owned(),
            recorded: steps(recorded.len()),
            observed: steps(observed.len()),
        }];
    }

    let mut contents = BTreeMap::new();
    for step in recorded.iter().chain(observed) {
        for snapshot in [&step.before, &step.after] {
            let listed: BTreeMap<&str, &str> = snapshot.tree.iter().filter_map(file_of).collect();
            for (path, content) in &snapshot.files {
                let hash = snapshot::hash_of(content);
                if listed.get(path.as_str()) == Some(&hash.as_str()) {
                    contents.insert(hash, content.as_slice());
                }
            }
        }
    }

    let mut comparison = Comparison::new(rules);
    for (index, step) in scenario.steps.iter().enumerate() {
        let number = u32::try_from(index + 1).unwrap_or(u32::MAX);
        let (recorded, observed) = (&recorded[index], &observed[index]);
        if recorded.exit != observed.exit {
            comparison.report(
                Dimension::Exit,
                Some(number),
                EXIT_CODE_FILE,
                exit_text(recorded.exit),
                exit_text(observed.exit),
            );
        }
        if step.stdout != StdoutMode::Text {
            comparison.json(number, step.stdout, recorded, observed);
        }
        comparison.tree(number, &recorded.after, &observed.after);
        comparison.files(number, &recorded.after, &observed.after, &contents);
        comparison.git(number, &recorded.after, &observed.after);
    }
    comparison.unused();
    comparison.found
}

/// The first difference between `recorded`, the snapshot a record holds from before its first step, and
/// `built`, the snapshot of the fixture as it was just built; `None` when the two are equal. No rule of a
/// scenario applies: every entry, every hash, every block, and the stored content are compared.
pub fn fixture(recorded: &Snapshot, built: &Snapshot) -> Option<Difference> {
    let mut comparison = Comparison::new(Rules::default());
    comparison.tree(1, recorded, built);
    comparison.git(1, recorded, built);
    if recorded.files != built.files {
        let paths = |snapshot: &Snapshot| {
            let paths: Vec<&str> = snapshot.files.keys().map(String::as_str).collect();
            format!("content stored for: {}", paths.join(", "))
        };
        comparison.report(
            Dimension::Files,
            Some(1),
            snapshot::FILES_DIRECTORY,
            paths(recorded),
            paths(built),
        );
    }
    let first = comparison.found.into_iter().next()?;
    let subject = match first.dimension {
        Dimension::Tree => format!("{} {}", snapshot::TREE_FILE, first.subject),
        Dimension::Git => format!("{} {}", snapshot::GIT_FILE, first.subject),
        _ => first.subject.clone(),
    };
    Some(Difference {
        dimension: Dimension::Fixture,
        subject,
        ..first
    })
}

/// One difference for each of the three things `meta`, the `meta.toml` of a record, says the record was
/// made with and that is no longer so: the commit of `reference`, the digest `environment` of the pinned
/// environment, and the digest `description` of the scenario's description. Empty when the record is
/// current.
pub fn stale_record(
    meta: &Meta,
    reference: &Reference,
    environment: &str,
    description: &str,
) -> Vec<Difference> {
    [
        (
            "reference_commit",
            &meta.reference_commit,
            &*reference.commit,
        ),
        ("environment", &meta.environment, environment),
        ("description", &meta.description, description),
    ]
    .into_iter()
    .filter(|(_, recorded, current)| recorded.as_str() != *current)
    .map(|(key, recorded, current)| Difference {
        dimension: Dimension::StaleRecord,
        step: None,
        subject: key.to_owned(),
        recorded: recorded.clone(),
        observed: current.to_owned(),
    })
    .collect()
}

/// Why an expectation could not be laid over a record.
#[derive(Debug)]
pub enum ExpectationError {
    /// The expectation names a step the record does not hold.
    NoStep { step: u32 },
    /// The expectation holds a file that is none an expectation replaces.
    NotReplaced { step: u32, file: String },
    /// A file of the expectation does not read as what it replaces.
    Unreadable {
        step: u32,
        file: String,
        reason: &'static str,
    },
    /// The state after a step, with the expectation laid over it, does not read as a stored snapshot.
    Snapshot { step: u32, error: SnapshotError },
}

impl fmt::Display for ExpectationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoStep { step } => {
                write!(
                    f,
                    "the expectation names step {step}, which the record lacks"
                )
            }
            Self::NotReplaced { step, file } => write!(
                f,
                "the expectation of step {step} holds {file}, which is no file an expectation replaces"
            ),
            Self::Unreadable { step, file, reason } => {
                write!(f, "{file} of the expectation of step {step}: {reason}")
            }
            Self::Snapshot { step, error } => {
                write!(f, "the expected state after step {step}: {error}")
            }
        }
    }
}

impl std::error::Error for ExpectationError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Snapshot { error, .. } => Some(error),
            Self::NoStep { .. } | Self::NotReplaced { .. } | Self::Unreadable { .. } => None,
        }
    }
}

/// `recorded` with `expected` laid over it: the files of `expected/steps/<nn>/`, by the number of the
/// step and by their path under its directory, as the bytes they hold, each replacing the file of the
/// record at the same path.
///
/// An expectation replaces what a step produced: the exit file, `stdout`, `stderr`, `stdout.json`, and
/// under `after/` the listing, the sections of the repositories, and stored content, whose paths are in
/// the stored form. Any other file, one under `before/` included, is refused.
///
/// The state after a step whose listing or stored content the expectation replaces is read as a stored
/// snapshot is, against the state before the step: the content of a file the laid-over listing implies
/// is taken from the expectation, else from the record, and has to hash to the value on its line; content
/// of the expectation that the listing does not imply fails; and content of the record that it no longer
/// implies is left out.
pub fn overlay(
    recorded: &[StepRecord],
    expected: &BTreeMap<u32, BTreeMap<String, Vec<u8>>>,
) -> Result<Vec<StepRecord>, ExpectationError> {
    let mut steps = recorded.to_vec();
    for (&number, files) in expected {
        let index = usize::try_from(number).ok().and_then(|n| n.checked_sub(1));
        let step = index
            .and_then(|index| steps.get_mut(index))
            .ok_or(ExpectationError::NoStep { step: number })?;
        overlay_step(number, step, files)?;
    }
    Ok(steps)
}

/// Lays `files`, the expectation of the step `number`, over `step`.
fn overlay_step(
    number: u32,
    step: &mut StepRecord,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<(), ExpectationError> {
    let after = |name: &str| format!("{}/{name}", snapshot::AFTER);
    let (tree_file, git_file) = (after(snapshot::TREE_FILE), after(snapshot::GIT_FILE));
    let content_prefix = after(&format!("{}/", snapshot::FILES_DIRECTORY));
    let place =
        |source: &str, file: &str| PathBuf::from(format!("{source}/steps/{number:02}/{file}"));
    let unreadable = |file: &str, reason| ExpectationError::Unreadable {
        step: number,
        file: file.to_owned(),
        reason,
    };
    let stored = |error| ExpectationError::Snapshot {
        step: number,
        error,
    };
    let text = |file: &str, content: &[u8]| {
        String::from_utf8(content.to_vec()).map_err(|_| unreadable(file, "the file is not UTF-8"))
    };

    let mut tree = None;
    let mut contents = BTreeMap::new();
    for (file, content) in files {
        if file == EXIT_CODE_FILE {
            step.exit = parse_exit(&text(file, content)?)
                .ok_or_else(|| unreadable(file, "not a code or a signal on one line"))?;
        } else if file == STDOUT_FILE {
            step.stdout = content.clone();
        } else if file == STDERR_FILE {
            step.stderr = content.clone();
        } else if file == STDOUT_JSON_FILE {
            step.stdout_json = Some(text(file, content)?);
        } else if *file == tree_file {
            let listing = snapshot::parse_tree(&place("expected", file), &text(file, content)?);
            tree = Some(listing.map_err(stored)?);
        } else if *file == git_file {
            let sections = snapshot::parse_git(&place("expected", file), &text(file, content)?);
            step.after.git = sections.map_err(stored)?;
        } else if let Some(path) = file.strip_prefix(&content_prefix) {
            let path = snapshot::tree_path(path)
                .filter(|path| !path.is_empty())
                .ok_or_else(|| unreadable(file, "the path is not in the stored form"))?;
            contents.insert(path, content.clone());
        } else {
            return Err(ExpectationError::NotReplaced {
                step: number,
                file: file.clone(),
            });
        }
    }

    if tree.is_none() && contents.is_empty() {
        return Ok(());
    }
    let tree = tree.unwrap_or_else(|| step.after.tree.clone());
    let expected: BTreeSet<String> = contents.keys().cloned().collect();
    for path in snapshot::implied(&tree, Some(&step.before)) {
        if let Some(content) = step.after.files.get(path) {
            contents
                .entry(path.to_owned())
                .or_insert_with(|| content.clone());
        }
    }
    let location = |path: &str| {
        let source = match expected.contains(path) {
            true => "expected",
            false => "record",
        };
        place(source, &content_prefix).join(snapshot::stored_path(path))
    };
    snapshot::held(&tree, &contents, Some(&step.before), &location).map_err(stored)?;
    step.after.tree = tree;
    step.after.files = contents;
    Ok(())
}

/// One token of a message-text pointer.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Token {
    /// `*`: any one key or index.
    Any,
    Key(String),
}

/// One fan-out of a scenario, read.
#[derive(Clone, Debug)]
struct FanOut {
    types: Vec<String>,
    /// The tokens of the pointer to the value the run is partitioned by.
    key: Vec<String>,
}

/// One segment of a glob.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Segment {
    /// `**`: any number of segments.
    Across,
    /// One segment, `*` in it standing for any run of bytes.
    Within(String),
}

/// The comparison rules of a scenario, read.
#[derive(Clone, Debug, Default)]
struct Rules {
    /// Each pointer as it is written, and its tokens.
    message_text: Vec<(String, Vec<Token>)>,
    fan_out: Vec<FanOut>,
    /// Each glob as it is written, its segments, and the mode of the paths it matches.
    files: Vec<(String, Vec<Segment>, FileMode)>,
}

impl Rules {
    /// The rules of `compare`, or one difference for every rule that does not read.
    fn new(compare: &Compare) -> Result<Self, Vec<Difference>> {
        let mut malformed = Vec::new();
        let mut refuse = |key: &str, written: &str, place: String, reason: String| {
            malformed.push(Difference {
                dimension: Dimension::Malformed,
                step: None,
                subject: key.to_owned(),
                recorded: written.to_owned(),
                observed: format!("in {place}: {reason}"),
            });
        };
        let mut rules = Self::default();

        for pointer in &compare.message_text {
            match pointer_tokens(pointer) {
                Ok(tokens) => {
                    let tokens = tokens.into_iter().map(|token| match token.as_str() {
                        "*" => Token::Any,
                        _ => Token::Key(token),
                    });
                    rules.message_text.push((pointer.clone(), tokens.collect()));
                }
                Err(reason) => refuse(
                    "message_text",
                    pointer,
                    "[compare]".to_owned(),
                    reason.to_owned(),
                ),
            }
        }

        for key in &compare.git_config_ignore {
            let written = key.contains('.') && !key.contains(['=', '\n', '\r']);
            if !written {
                refuse(
                    "git_config_ignore",
                    key,
                    "[compare]".to_owned(),
                    "no key of a git configuration, which holds a `.` and no `=` or line break"
                        .to_owned(),
                );
            }
        }

        let mut listed = BTreeSet::new();
        for (index, fan_out) in compare.fan_out.iter().enumerate() {
            let place = format!("[[compare.fan_out]] {}", index + 1);
            if fan_out.types.is_empty() {
                let reason = "a fan-out lists at least one type";
                refuse("types", "[]", place.clone(), reason.to_owned());
            }
            for name in &fan_out.types {
                if !listed.insert(name.as_str()) {
                    let reason = "the type is listed twice, and a value belongs to one fan-out";
                    refuse("types", name, place.clone(), reason.to_owned());
                }
            }
            match pointer_tokens(&fan_out.key) {
                Ok(key) => rules.fan_out.push(FanOut {
                    types: fan_out.types.clone(),
                    key,
                }),
                Err(reason) => refuse("key", &fan_out.key, place, reason.to_owned()),
            }
        }

        for (index, rule) in compare.files.iter().enumerate() {
            let place = format!("[[compare.file]] {}", index + 1);
            let segments = match model::unconfined(&rule.path) {
                Some(reason) => Err(reason),
                None => glob(&rule.path).map_err(str::to_owned),
            };
            match segments {
                Ok(segments) => rules.files.push((rule.path.clone(), segments, rule.mode)),
                Err(reason) => refuse("path", &rule.path, place, reason),
            }
        }

        match malformed.is_empty() {
            true => Ok(rules),
            false => Err(malformed),
        }
    }

    /// Whether a message-text pointer matches the place `path` of a value.
    fn is_message_text(&self, path: &[String]) -> bool {
        self.message_text
            .iter()
            .any(|(_, tokens)| matches_pointer(tokens, path))
    }

    /// The fan-out that lists `name`, the type of a value.
    fn fan_out_of(&self, name: &str) -> Option<usize> {
        self.fan_out
            .iter()
            .position(|fan_out| fan_out.types.iter().any(|listed| listed == name))
    }
}

/// The tokens of the JSON pointer `pointer`, each with `~1` read as `/` and `~0` as `~`, or why it is no
/// pointer.
fn pointer_tokens(pointer: &str) -> Result<Vec<String>, &'static str> {
    if pointer.is_empty() {
        return Ok(Vec::new());
    }
    let rest = pointer
        .strip_prefix('/')
        .ok_or("a JSON pointer is empty or starts with `/`")?;
    rest.split('/')
        .map(|token| {
            let mut read = String::new();
            let mut characters = token.chars();
            while let Some(character) = characters.next() {
                read.push(match character {
                    '~' => match characters.next() {
                        Some('0') => '~',
                        Some('1') => '/',
                        _ => return Err("in a JSON pointer `~` is followed by `0` or `1`"),
                    },
                    other => other,
                });
            }
            Ok(read)
        })
        .collect()
}

/// The pointer that names the place `path` of a value.
fn pointer(path: &[String]) -> String {
    path.iter()
        .map(|token| format!("/{}", token.replace('~', "~0").replace('/', "~1")))
        .collect()
}

/// Whether `tokens`, a message-text pointer, names the place `path`.
fn matches_pointer(tokens: &[Token], path: &[String]) -> bool {
    tokens.len() == path.len()
        && tokens.iter().zip(path).all(|(token, at)| match token {
            Token::Any => true,
            Token::Key(key) => key == at,
        })
}

/// The segments of the glob `path`, or why it is none: a segment is empty, `.`, or `..`, none of which a
/// path of a listing holds, or holds `**` beside something else.
fn glob(path: &str) -> Result<Vec<Segment>, &'static str> {
    path.split('/')
        .map(|segment| match segment {
            "" => Err("a glob holds no empty segment"),
            "." | ".." => Err("a glob holds no segment `.` or `..`"),
            "**" => Ok(Segment::Across),
            _ if segment.contains("**") => Err("`**` stands as a whole segment"),
            _ => Ok(Segment::Within(segment.to_owned())),
        })
        .collect()
}

/// Whether `segments`, a glob, matches `path`, the segments of a path.
fn matches_glob(segments: &[Segment], path: &[&str]) -> bool {
    match segments.split_first() {
        None => path.is_empty(),
        Some((Segment::Across, rest)) => {
            (0..=path.len()).any(|skipped| matches_glob(rest, &path[skipped..]))
        }
        Some((Segment::Within(pattern), rest)) => {
            path.split_first().is_some_and(|(first, path)| {
                matches_segment(pattern.as_bytes(), first.as_bytes()) && matches_glob(rest, path)
            })
        }
    }
}

/// Whether `pattern`, in which `*` stands for any run of bytes, matches all of `text`.
fn matches_segment(pattern: &[u8], text: &[u8]) -> bool {
    match pattern.split_first() {
        None => text.is_empty(),
        Some((b'*', rest)) => (0..=text.len()).any(|taken| matches_segment(rest, &text[taken..])),
        Some((byte, rest)) => text
            .split_first()
            .is_some_and(|(first, text)| first == byte && matches_segment(rest, text)),
    }
}

/// The path and the hash of `entry`, when it is a file.
fn file_of(entry: &Entry) -> Option<(&str, &str)> {
    match &entry.kind {
        EntryKind::File { hash, .. } => Some((entry.path.as_str(), hash.as_str())),
        _ => None,
    }
}

/// `content` as a difference shows it: the text when it is UTF-8, and every byte in hexadecimal
/// otherwise, so that no two contents that differ are shown alike.
fn shown(content: &[u8]) -> String {
    match std::str::from_utf8(content) {
        Ok(text) => text.to_owned(),
        Err(_) => {
            let bytes: String = content.iter().map(|byte| format!("{byte:02x}")).collect();
            format!("{} bytes that are not UTF-8: {bytes}", content.len())
        }
    }
}

/// A JSON value, with every number as it is written and every member of an object in the order written.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Json {
    Null,
    Bool(bool),
    Number(String),
    Text(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

/// One value of a stream.
#[derive(Clone, Debug)]
enum Item {
    /// A line that is not JSON, with the text of it.
    Line(String),
    Document(Json),
}

/// A part of a stream that is compared as one.
#[derive(Clone, Debug)]
enum Part {
    /// The value at this place of the stream.
    Single(usize),
    /// A maximal run of values whose types one fan-out lists: the fan-out, and the places of the values.
    Run(usize, Vec<usize>),
}

/// A reader of JSON text.
struct Reader<'t> {
    bytes: &'t [u8],
    at: usize,
}

impl Reader<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    /// Reads `word` when it stands here.
    fn word(&mut self, word: &str) -> Option<()> {
        let end = self.at.checked_add(word.len())?;
        (self.bytes.get(self.at..end)? == word.as_bytes()).then(|| self.at = end)
    }

    /// Reads one value, `depth` arrays and objects deep.
    fn value(&mut self, depth: usize) -> Option<Json> {
        match self.peek()? {
            b'n' => self.word("null").map(|()| Json::Null),
            b't' => self.word("true").map(|()| Json::Bool(true)),
            b'f' => self.word("false").map(|()| Json::Bool(false)),
            b'"' => self.string().map(Json::Text),
            b'[' if depth < DEPTH => {
                self.at += 1;
                let mut items = Vec::new();
                self.skip_space();
                if self.peek()? == b']' {
                    self.at += 1;
                    return Some(Json::Array(items));
                }
                loop {
                    self.skip_space();
                    items.push(self.value(depth + 1)?);
                    self.skip_space();
                    match self.peek()? {
                        b',' => self.at += 1,
                        b']' => {
                            self.at += 1;
                            return Some(Json::Array(items));
                        }
                        _ => return None,
                    }
                }
            }
            b'{' if depth < DEPTH => {
                self.at += 1;
                let mut members = Vec::new();
                self.skip_space();
                if self.peek()? == b'}' {
                    self.at += 1;
                    return Some(Json::Object(members));
                }
                loop {
                    self.skip_space();
                    let key = self.string()?;
                    self.skip_space();
                    self.word(":")?;
                    self.skip_space();
                    members.push((key, self.value(depth + 1)?));
                    self.skip_space();
                    match self.peek()? {
                        b',' => self.at += 1,
                        b'}' => {
                            self.at += 1;
                            return Some(Json::Object(members));
                        }
                        _ => return None,
                    }
                }
            }
            b'-' | b'0'..=b'9' => self.number().map(Json::Number),
            _ => None,
        }
    }

    /// Reads a string, which starts here with its quote.
    fn string(&mut self) -> Option<String> {
        self.word("\"")?;
        let mut read = Vec::new();
        loop {
            let byte = self.peek()?;
            self.at += 1;
            match byte {
                b'"' => return String::from_utf8(read).ok(),
                b'\\' => {
                    let escape = self.peek()?;
                    self.at += 1;
                    let character = match escape {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => self.escaped()?,
                        _ => return None,
                    };
                    read.extend_from_slice(character.encode_utf8(&mut [0; 4]).as_bytes());
                }
                0..=0x1f => return None,
                other => read.push(other),
            }
        }
    }

    /// Reads four hexadecimal digits.
    fn hex(&mut self) -> Option<u32> {
        let end = self.at.checked_add(4)?;
        let digits = self.bytes.get(self.at..end)?;
        let digits = std::str::from_utf8(digits).ok()?;
        if !digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return None;
        }
        self.at = end;
        u32::from_str_radix(digits, 16).ok()
    }

    /// Reads the character of a `\u` escape, whose digits start here: one unit, or the two of a pair.
    fn escaped(&mut self) -> Option<char> {
        let first = self.hex()?;
        let code = match first {
            0xd800..=0xdbff => {
                self.word("\\u")?;
                let second = self.hex()?;
                if !(0xdc00..=0xdfff).contains(&second) {
                    return None;
                }
                0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00)
            }
            other => other,
        };
        char::from_u32(code)
    }

    /// Reads a number, as it is written.
    fn number(&mut self) -> Option<String> {
        let start = self.at;
        let digits = |reader: &mut Self| {
            let from = reader.at;
            while matches!(reader.peek(), Some(b'0'..=b'9')) {
                reader.at += 1;
            }
            reader.at - from
        };
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        let leading_zero = self.peek() == Some(b'0');
        let whole = digits(self);
        if whole == 0 || (leading_zero && whole > 1) {
            return None;
        }
        if self.peek() == Some(b'.') {
            self.at += 1;
            if digits(self) == 0 {
                return None;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if digits(self) == 0 {
                return None;
            }
        }
        let token = self.bytes.get(start..self.at)?;
        std::str::from_utf8(token).ok().map(str::to_owned)
    }
}

/// The values `text` holds one after another; `None` when it is not such a stream. White space stands
/// between two values, and may be left out only where one of the two is a string, an array, or an object,
/// which ends or starts by itself.
fn stream(text: &[u8]) -> Option<Vec<Json>> {
    let mut reader = Reader { bytes: text, at: 0 };
    let mut values = Vec::new();
    let mut ended = 0;
    loop {
        reader.skip_space();
        let Some(next) = reader.peek() else {
            return Some(values);
        };
        let apart = reader.at > ended
            || values.is_empty()
            || matches!(next, b'"' | b'[' | b'{')
            || matches!(text.get(ended.wrapping_sub(1)), Some(b'"' | b']' | b'}'));
        if !apart {
            return None;
        }
        values.push(reader.value(0)?);
        ended = reader.at;
    }
}

/// The one value `text` is, white space around it aside.
fn document(text: &[u8]) -> Option<Json> {
    let mut values = stream(text)?.into_iter();
    values.next().filter(|_| values.next().is_none())
}

/// The value of the number `token` as a decimal: whether it is below zero, its digits without a zero at
/// either end, and the power of ten they are multiplied by. Zero has no digit and no sign. `None` when
/// the exponent is beyond what is counted.
fn decimal(token: &str) -> Option<(bool, String, i64)> {
    let (negative, rest) = match token.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, token),
    };
    let (mantissa, exponent) = match rest.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, exponent.parse::<i64>().ok()?),
        None => (rest, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let exponent = exponent.checked_sub(i64::try_from(fraction.len()).ok()?)?;
    let digits = format!("{whole}{fraction}");
    let digits = digits.trim_start_matches('0');
    if digits.is_empty() {
        return Some((false, String::new(), 0));
    }
    let significant = digits.trim_end_matches('0');
    let dropped = i64::try_from(digits.len() - significant.len()).ok()?;
    Some((
        negative,
        significant.to_owned(),
        exponent.checked_add(dropped)?,
    ))
}

/// Whether the numbers `a` and `b` have one value; two whose exponents are beyond what is counted are
/// equal only when they are written alike.
fn same_number(a: &str, b: &str) -> bool {
    match (decimal(a), decimal(b)) {
        (Some(a), Some(b)) => a == b,
        _ => a == b,
    }
}

/// The members of an object in the order of the bytes of their keys, those of one key in the order
/// written.
fn sorted(members: &[(String, Json)]) -> Vec<(&str, &Json)> {
    let mut members: Vec<(&str, &Json)> = members
        .iter()
        .map(|(key, value)| (key.as_str(), value))
        .collect();
    members.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    members
}

/// Whether `a` and `b` are one value, under no rule of a scenario.
fn same(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Number(a), Json::Number(b)) => same_number(a, b),
        (Json::Array(a), Json::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| same(a, b))
        }
        (Json::Object(a), Json::Object(b)) => {
            let (a, b) = (sorted(a), sorted(b));
            a.len() == b.len() && a.iter().zip(&b).all(|(a, b)| a.0 == b.0 && same(a.1, b.1))
        }
        (Json::Number(_) | Json::Array(_) | Json::Object(_), _) => false,
        _ => a == b,
    }
}

/// `value` as a difference shows it: on one line, the keys of every object sorted, every number as it is
/// written.
fn encoded(value: &Json) -> String {
    let quoted = |text: &str| serde_json::Value::String(text.to_owned()).to_string();
    match value {
        Json::Null => "null".to_owned(),
        Json::Bool(value) => value.to_string(),
        Json::Number(token) => token.clone(),
        Json::Text(text) => quoted(text),
        Json::Array(items) => {
            let items: Vec<String> = items.iter().map(encoded).collect();
            format!("[{}]", items.join(","))
        }
        Json::Object(members) => {
            let members: Vec<String> = sorted(members)
                .into_iter()
                .map(|(key, value)| format!("{}:{}", quoted(key), encoded(value)))
                .collect();
            format!("{{{}}}", members.join(","))
        }
    }
}

/// `item` as a difference shows it.
fn shown_item(item: &Item) -> String {
    match item {
        Item::Line(text) => format!("a line that is not JSON: {text}"),
        Item::Document(value) => encoded(value),
    }
}

/// The one member `key` of `value`, an object; `None` when it has none or more than one.
fn member<'v>(value: &'v Json, key: &str) -> Option<&'v Json> {
    let Json::Object(members) = value else {
        return None;
    };
    let mut found = members.iter().filter(|(name, _)| name == key);
    found
        .next()
        .filter(|_| found.next().is_none())
        .map(|(_, value)| value)
}

/// The value at the pointer `tokens` of `value`, when there is exactly one.
fn at<'v>(value: &'v Json, tokens: &[String]) -> Option<&'v Json> {
    tokens.iter().try_fold(value, |value, token| match value {
        Json::Array(items) => {
            let plain = token == "0" || !token.starts_with('0');
            let index = plain.then(|| token.parse::<usize>().ok()).flatten()?;
            items.get(index)
        }
        other => member(other, token),
    })
}

/// The text of the line `value` stands for in a stored stream: that of an object whose one member is a
/// string at `$text`.
fn stored_line(value: &Json) -> Option<&str> {
    match value {
        Json::Object(members) => match &members[..] {
            [(key, Json::Text(text))] if key == TEXT_KEY => Some(text),
            _ => None,
        },
        _ => None,
    }
}

/// The values of the stdout of `step`, read again as they are written, when its `stdout.json` is the
/// canonical form of that stdout under `mode`.
fn written_items(step: &StepRecord, mode: StdoutMode) -> Option<Vec<Item>> {
    let values = normalize::json_values(&step.stdout, mode).ok()??;
    if step.stdout_json.as_deref() != Some(normalize::encode(&values).as_str()) {
        return None;
    }
    let items: Vec<Item> = match mode {
        StdoutMode::Text => return None,
        StdoutMode::Json => stream(&step.stdout)?
            .into_iter()
            .map(Item::Document)
            .collect(),
        StdoutMode::JsonLines => normalize::lines(&step.stdout)
            .map(
                |line| match serde_json::from_slice::<serde_json::Value>(line) {
                    Ok(_) => document(line).map(Item::Document),
                    Err(_) => Some(Item::Line(String::from_utf8_lossy(line).into_owned())),
                },
            )
            .collect::<Option<_>>()?,
    };
    (items.len() == values.len()).then_some(items)
}

/// The values `text`, the content of a `stdout.json`, holds; under `json-lines` an object that stands for
/// a line that is not JSON is that line.
fn stored_items(text: &str, mode: StdoutMode) -> Option<Vec<Item>> {
    let values = stream(text.as_bytes())?.into_iter();
    Some(
        values
            .map(|value| match stored_line(&value) {
                Some(line) if mode == StdoutMode::JsonLines => Item::Line(line.to_owned()),
                _ => Item::Document(value),
            })
            .collect(),
    )
}

/// How a difference names the value at the place `recorded` of the record's stream and the place
/// `observed` of the observation's, with `pointer` into it. In a stream of one value on both sides,
/// `single`, the pointer alone.
fn subject(
    single: bool,
    recorded: Option<usize>,
    observed: Option<usize>,
    pointer: &str,
) -> String {
    let value = match (recorded, observed) {
        _ if single => String::new(),
        (Some(recorded), Some(observed)) if recorded != observed => {
            format!("value {} (observed value {})", recorded + 1, observed + 1)
        }
        (Some(recorded), _) => format!("value {}", recorded + 1),
        (None, Some(observed)) => format!("observed value {}", observed + 1),
        (None, None) => String::new(),
    };
    match (value.is_empty(), pointer.is_empty()) {
        (true, _) => pointer.to_owned(),
        (false, true) => value,
        (false, false) => format!("{value}: {pointer}"),
    }
}

/// One comparison under the rules of a scenario: what it found, and which rules it used.
struct Comparison {
    rules: Rules,
    message_text_used: Vec<bool>,
    fan_out_used: Vec<bool>,
    files_used: Vec<bool>,
    found: Vec<Difference>,
}

impl Comparison {
    fn new(rules: Rules) -> Self {
        Self {
            message_text_used: vec![false; rules.message_text.len()],
            fan_out_used: vec![false; rules.fan_out.len()],
            files_used: vec![false; rules.files.len()],
            found: Vec::new(),
            rules,
        }
    }

    fn report(
        &mut self,
        dimension: Dimension,
        step: Option<u32>,
        subject: impl Into<String>,
        recorded: impl Into<String>,
        observed: impl Into<String>,
    ) {
        self.found.push(Difference {
            dimension,
            step,
            subject: subject.into(),
            recorded: recorded.into(),
            observed: observed.into(),
        });
    }

    /// One difference for every rule that decided nothing.
    fn unused(&mut self) {
        let unused = |used: &[bool]| -> Vec<usize> {
            let unused = used.iter().enumerate().filter(|(_, used)| !**used);
            unused.map(|(index, _)| index).collect()
        };
        for index in unused(&self.message_text_used) {
            let pointer = self.rules.message_text[index].0.clone();
            self.report(
                Dimension::Json,
                None,
                format!("message_text {pointer}"),
                "no string of the recorded output is at the pointer",
                "the rule relaxes nothing",
            );
        }
        for index in unused(&self.fan_out_used) {
            let types = self.rules.fan_out[index].types.join(", ");
            self.report(
                Dimension::Json,
                None,
                format!("fan_out {types}"),
                "no value of the recorded output has one of the types",
                "the rule relaxes nothing",
            );
        }
        for index in unused(&self.files_used) {
            let path = self.rules.files[index].0.clone();
            self.report(
                Dimension::Files,
                None,
                format!("file {path}"),
                "the rule decides the mode of no path of the state after a step",
                "the rule relaxes nothing",
            );
        }
    }

    /// How the file at `path` is compared: by the first rule whose glob matches, by its bytes without one.
    fn mode(&mut self, path: &str) -> FileMode {
        let segments: Vec<&str> = path.split('/').collect();
        let rules = self.rules.files.iter();
        match rules
            .enumerate()
            .find(|(_, (_, glob, _))| matches_glob(glob, &segments))
        {
            Some((index, (_, _, mode))) => {
                self.files_used[index] = true;
                *mode
            }
            None => FileMode::Bytes,
        }
    }

    /// The JSON output of the step `step`, whose stdout mode is `mode`.
    fn json(&mut self, step: u32, mode: StdoutMode, recorded: &StepRecord, observed: &StepRecord) {
        /// The values of one side: those of its stdout where its `stdout.json` is the canonical form of
        /// it, and those of its `stdout.json` otherwise.
        fn read(side: &StepRecord, mode: StdoutMode) -> Option<Result<Vec<Item>, &str>> {
            if let Some(written) = written_items(side, mode) {
                return Some(Ok(written));
            }
            let text = side.stdout_json.as_deref()?;
            Some(stored_items(text, mode).ok_or(text))
        }
        let (recorded, observed) = match (read(recorded, mode), read(observed, mode)) {
            (Some(Ok(recorded)), Some(Ok(observed))) => (recorded, observed),
            (recorded, observed) => {
                let shown = |side: Option<Result<Vec<Item>, &str>>| match side {
                    None => ABSENT.to_owned(),
                    Some(Ok(_)) => "a stream of JSON values".to_owned(),
                    Some(Err(text)) => format!("not a stream of JSON values: {text}"),
                };
                let (recorded, observed) = (shown(recorded), shown(observed));
                self.report(
                    Dimension::Json,
                    Some(step),
                    STDOUT_JSON_FILE,
                    recorded,
                    observed,
                );
                return;
            }
        };

        for item in &recorded {
            if let Item::Document(value) = item {
                self.mark_message_text(&mut Vec::new(), value);
            }
        }
        let single = recorded.len() == 1 && observed.len() == 1;
        let (recorded_parts, observed_parts) = (self.parts(&recorded), self.parts(&observed));
        for part in &recorded_parts {
            if let Part::Run(rule, _) = part {
                self.fan_out_used[*rule] = true;
            }
        }
        let first = |part: &Part| match part {
            Part::Single(place) => *place,
            Part::Run(_, places) => places[0],
        };
        let shown = |part: Option<&Part>, items: &[Item]| match part {
            None => ABSENT.to_owned(),
            Some(Part::Single(place)) => shown_item(&items[*place]),
            Some(Part::Run(_, places)) => format!(
                "a fan-out run of {} values, the first: {}",
                places.len(),
                shown_item(&items[places[0]])
            ),
        };
        for index in 0..recorded_parts.len().max(observed_parts.len()) {
            match (recorded_parts.get(index), observed_parts.get(index)) {
                (Some(Part::Single(r)), Some(Part::Single(o))) => {
                    self.item(step, single, (*r, &recorded[*r]), (*o, &observed[*o]));
                }
                (Some(Part::Run(rule, r)), Some(Part::Run(other, o))) if rule == other => {
                    self.run(step, single, *rule, (r, &recorded), (o, &observed));
                }
                (r, o) => {
                    let subject = subject(single, r.map(first), o.map(first), "");
                    let (r, o) = (shown(r, &recorded), shown(o, &observed));
                    self.report(Dimension::Json, Some(step), subject, r, o);
                }
            }
        }
    }

    /// Marks every message-text pointer that a string of `value`, a recorded value, is at.
    fn mark_message_text(&mut self, path: &mut Vec<String>, value: &Json) {
        match value {
            Json::Text(_) => {
                for (index, (_, tokens)) in self.rules.message_text.iter().enumerate() {
                    if matches_pointer(tokens, path) {
                        self.message_text_used[index] = true;
                    }
                }
            }
            Json::Array(items) => {
                for (index, item) in items.iter().enumerate() {
                    path.push(index.to_string());
                    self.mark_message_text(path, item);
                    path.pop();
                }
            }
            Json::Object(members) => {
                for (key, member) in members {
                    path.push(key.clone());
                    self.mark_message_text(path, member);
                    path.pop();
                }
            }
            Json::Null | Json::Bool(_) | Json::Number(_) => {}
        }
    }

    /// The parts `items`, a stream, is compared in: each maximal run of values whose types one fan-out
    /// lists, and every other value alone.
    fn parts(&self, items: &[Item]) -> Vec<Part> {
        let mut parts: Vec<Part> = Vec::new();
        for (place, item) in items.iter().enumerate() {
            let rule = match item {
                Item::Document(value) => match member(value, TYPE_KEY) {
                    Some(Json::Text(name)) => self.rules.fan_out_of(name),
                    _ => None,
                },
                Item::Line(_) => None,
            };
            match (rule, parts.last_mut()) {
                (Some(rule), Some(Part::Run(last, places))) if *last == rule => places.push(place),
                (Some(rule), _) => parts.push(Part::Run(rule, vec![place])),
                (None, _) => parts.push(Part::Single(place)),
            }
        }
        parts
    }

    /// Two values of a stream, each with its place.
    fn item(
        &mut self,
        step: u32,
        single: bool,
        recorded: (usize, &Item),
        observed: (usize, &Item),
    ) {
        let subject = |pointer: &str| subject(single, Some(recorded.0), Some(observed.0), pointer);
        match (recorded.1, observed.1) {
            (Item::Line(_), Item::Line(_)) => {}
            (Item::Document(r), Item::Document(o)) => {
                self.values(step, &subject, &mut Vec::new(), r, o);
            }
            (r, o) => {
                let (r, o) = (shown_item(r), shown_item(o));
                self.report(Dimension::Json, Some(step), subject(""), r, o);
            }
        }
    }

    /// Two fan-out runs of the fan-out `rule`, each as the places of its values and the stream: the
    /// partitions as a set, and the values of one partition in order.
    fn run(
        &mut self,
        step: u32,
        single: bool,
        rule: usize,
        recorded: (&[usize], &[Item]),
        observed: (&[usize], &[Item]),
    ) {
        let key = self.rules.fan_out[rule].key.clone();
        let partitions = |(places, items): (&[usize], &[Item])| {
            let mut partitions: Vec<(Option<Json>, Vec<usize>)> = Vec::new();
            for &place in places {
                let of = match &items[place] {
                    Item::Document(value) => at(value, &key).cloned(),
                    Item::Line(_) => None,
                };
                let same_key = |known: &Option<Json>| match (known, &of) {
                    (Some(known), Some(of)) => same(known, of),
                    (None, None) => true,
                    _ => false,
                };
                match partitions.iter_mut().find(|(known, _)| same_key(known)) {
                    Some((_, places)) => places.push(place),
                    None => partitions.push((of, vec![place])),
                }
            }
            partitions
        };
        let (mut left, right) = (partitions(recorded), partitions(observed));
        let named = |of: &Option<Json>| match of {
            Some(of) => format!("fan-out partition {} = {}", pointer(&key), encoded(of)),
            None => format!("fan-out partition without one {}", pointer(&key)),
        };
        let held = |places: &[usize]| match places.len() {
            1 => "1 value".to_owned(),
            count => format!("{count} values"),
        };
        for (of, theirs) in &right {
            let same_key = |known: &Option<Json>| match (known, of) {
                (Some(known), Some(of)) => same(known, of),
                (None, None) => true,
                _ => false,
            };
            let Some(index) = left.iter().position(|(known, _)| same_key(known)) else {
                let subject = subject(false, None, Some(theirs[0]), "");
                let subject = format!("{subject}: {}", named(of));
                self.report(Dimension::Json, Some(step), subject, ABSENT, held(theirs));
                continue;
            };
            let (_, ours) = left.remove(index);
            for index in 0..ours.len().max(theirs.len()) {
                match (ours.get(index), theirs.get(index)) {
                    (Some(r), Some(o)) => {
                        self.item(step, single, (*r, &recorded.1[*r]), (*o, &observed.1[*o]));
                    }
                    (r, o) => {
                        let subject = subject(false, r.copied(), o.copied(), "");
                        let subject = format!("{subject}: {}", named(of));
                        let shown = |place: Option<&usize>, items: &[Item]| {
                            place.map_or(ABSENT.to_owned(), |place| shown_item(&items[*place]))
                        };
                        let (r, o) = (shown(r, recorded.1), shown(o, observed.1));
                        self.report(Dimension::Json, Some(step), subject, r, o);
                    }
                }
            }
        }
        for (of, ours) in &left {
            let subject = subject(false, Some(ours[0]), None, "");
            let subject = format!("{subject}: {}", named(of));
            self.report(Dimension::Json, Some(step), subject, held(ours), ABSENT);
        }
    }

    /// Two values at the place `path` of a value of a stream; `subject` names a pointer into that value.
    fn values(
        &mut self,
        step: u32,
        subject: &dyn Fn(&str) -> String,
        path: &mut Vec<String>,
        recorded: &Json,
        observed: &Json,
    ) {
        match (recorded, observed) {
            (Json::Null, Json::Null) => {}
            (Json::Bool(a), Json::Bool(b)) if a == b => {}
            (Json::Number(a), Json::Number(b)) if same_number(a, b) => {}
            (Json::Text(a), Json::Text(b)) if a == b || self.rules.is_message_text(path) => {}
            (Json::Array(a), Json::Array(b)) => {
                for index in 0..a.len().max(b.len()) {
                    path.push(index.to_string());
                    self.places(step, subject, path, a.get(index), b.get(index));
                    path.pop();
                }
            }
            (Json::Object(a), Json::Object(b)) => {
                let (a, b) = (sorted(a), sorted(b));
                let keys: BTreeSet<&[u8]> =
                    a.iter().chain(&b).map(|(key, _)| key.as_bytes()).collect();
                for key in keys {
                    fn of<'v>(members: &[(&str, &'v Json)], key: &[u8]) -> Vec<&'v Json> {
                        let of = members.iter().filter(|(name, _)| name.as_bytes() == key);
                        of.map(|(_, value)| *value).collect()
                    }
                    let (a, b) = (of(&a, key), of(&b, key));
                    path.push(String::from_utf8_lossy(key).into_owned());
                    for index in 0..a.len().max(b.len()) {
                        let (a, b) = (a.get(index).copied(), b.get(index).copied());
                        self.places(step, subject, path, a, b);
                    }
                    path.pop();
                }
            }
            _ => {
                let subject = subject(&pointer(path));
                let (recorded, observed) = (encoded(recorded), encoded(observed));
                self.report(Dimension::Json, Some(step), subject, recorded, observed);
            }
        }
    }

    /// What two values hold at the place `path`, where either may hold nothing.
    fn places(
        &mut self,
        step: u32,
        subject: &dyn Fn(&str) -> String,
        path: &mut Vec<String>,
        recorded: Option<&Json>,
        observed: Option<&Json>,
    ) {
        match (recorded, observed) {
            (Some(recorded), Some(observed)) => {
                self.values(step, subject, path, recorded, observed)
            }
            (recorded, observed) => {
                let subject = subject(&pointer(path));
                let shown = |value: Option<&Json>| value.map_or(ABSENT.to_owned(), encoded);
                let (recorded, observed) = (shown(recorded), shown(observed));
                self.report(Dimension::Json, Some(step), subject, recorded, observed);
            }
        }
    }

    /// The listings of two snapshots: the same entries, the hash of a file compared where its mode is
    /// bytes, and a path under an `ignore` rule not at all.
    fn tree(&mut self, step: u32, recorded: &Snapshot, observed: &Snapshot) {
        let entries = |snapshot: &Snapshot| -> BTreeMap<String, Vec<Entry>> {
            let mut entries: BTreeMap<String, Vec<Entry>> = BTreeMap::new();
            for entry in &snapshot.tree {
                let at = entries.entry(entry.path.clone()).or_default();
                at.push(entry.clone());
            }
            entries
        };
        let (recorded, observed) = (entries(recorded), entries(observed));
        let paths: BTreeSet<&String> = recorded.keys().chain(observed.keys()).collect();
        for path in paths {
            let mode = self.mode(path);
            if mode == FileMode::Ignore {
                continue;
            }
            let none = Vec::new();
            let (r, o) = (
                recorded.get(path).unwrap_or(&none),
                observed.get(path).unwrap_or(&none),
            );
            let equal = match (&r[..], &o[..]) {
                ([r], [o]) => match (&r.kind, &o.kind) {
                    (
                        EntryKind::File {
                            executable: a,
                            hash: left,
                        },
                        EntryKind::File {
                            executable: b,
                            hash: right,
                        },
                    ) => a == b && (mode != FileMode::Bytes || left == right),
                    (a, b) => a == b,
                },
                _ => false,
            };
            if !equal {
                let shown = |entries: &[Entry]| match entries {
                    [] => ABSENT.to_owned(),
                    entries => {
                        let lines: Vec<String> = entries.iter().map(snapshot::line_of).collect();
                        lines.join("\n")
                    }
                };
                self.report(Dimension::Tree, Some(step), path, shown(r), shown(o));
            }
        }
    }

    /// The content of the files of two snapshots, each path by its mode. `contents` holds, by its hash,
    /// every content either run stored at any step.
    fn files(
        &mut self,
        step: u32,
        recorded: &Snapshot,
        observed: &Snapshot,
        contents: &BTreeMap<String, &[u8]>,
    ) {
        let listed = |snapshot: &Snapshot| -> BTreeMap<String, String> {
            let files = snapshot.tree.iter().filter_map(file_of);
            files
                .map(|(path, hash)| (path.to_owned(), hash.to_owned()))
                .collect()
        };
        let (left, right) = (listed(recorded), listed(observed));
        let paths: BTreeSet<&String> = left
            .keys()
            .chain(right.keys())
            .chain(recorded.files.keys())
            .chain(observed.files.keys())
            .collect();
        for path in paths {
            let mode = self.mode(path);
            if mode == FileMode::Ignore {
                continue;
            }
            let stored = (recorded.files.get(path), observed.files.get(path));
            let content = |stored: Option<&Vec<u8>>| stored.map_or(ABSENT.to_owned(), |c| shown(c));
            let (r, o) = match (left.get(path), right.get(path)) {
                (Some(r), Some(o)) => (r, o),
                (r, o) => {
                    // The listing answers for a file one side lacks; the files answer where content was
                    // stored for the step.
                    if stored.0.is_some() || stored.1.is_some() {
                        let shown = |hash: Option<&String>, stored: Option<&Vec<u8>>| match (
                            hash, stored,
                        ) {
                            (None, Some(_)) => {
                                "content is stored, and the listing has no file at the path"
                                    .to_owned()
                            }
                            (None, None) => ABSENT.to_owned(),
                            (Some(_), stored) => content(stored),
                        };
                        let (r, o) = (shown(r, stored.0), shown(o, stored.1));
                        self.report(Dimension::Files, Some(step), path, r, o);
                    }
                    continue;
                }
            };
            let known = |hash: &String, stored: Option<&Vec<u8>>| -> Option<Vec<u8>> {
                stored
                    .cloned()
                    .or_else(|| contents.get(hash).map(|content| content.to_vec()))
            };
            let unstored = |hash: &String, content: Option<Vec<u8>>| match content {
                Some(content) => shown(&content),
                None => format!("content that no step stored, with the hash {hash}"),
            };
            match mode {
                FileMode::Ignore | FileMode::Presence => {}
                FileMode::Bytes => match stored {
                    (Some(a), Some(b)) => {
                        if a != b {
                            self.report(Dimension::Files, Some(step), path, shown(a), shown(b));
                        }
                    }
                    (None, None) => {}
                    (a, b) => {
                        if r != o {
                            let (a, b) = (unstored(r, known(r, a)), unstored(o, known(o, b)));
                            self.report(Dimension::Files, Some(step), path, a, b);
                        }
                    }
                },
                FileMode::Toml => {
                    let (a, b) = (known(r, stored.0), known(o, stored.1));
                    let equal = match (&a, &b) {
                        (Some(a), Some(b)) => a == b || same_toml(a, b),
                        _ => stored.0.is_none() && stored.1.is_none() && r == o,
                    };
                    if !equal && (r != o || stored.0.is_some() || stored.1.is_some()) {
                        let (a, b) = (unstored(r, a), unstored(o, b));
                        self.report(Dimension::Files, Some(step), path, a, b);
                    }
                }
            }
        }
    }

    /// The sections of the repositories of two snapshots: the same repositories, of the same kinds, block
    /// for block the same lines.
    fn git(&mut self, step: u32, recorded: &Snapshot, observed: &Snapshot) {
        let sections = |snapshot: &Snapshot| -> BTreeMap<String, Vec<Repository>> {
            let mut sections: BTreeMap<String, Vec<Repository>> = BTreeMap::new();
            for repository in &snapshot.git {
                let at = sections.entry(repository.path.clone()).or_default();
                at.push(repository.clone());
            }
            sections
        };
        let (recorded, observed) = (sections(recorded), sections(observed));
        let paths: BTreeSet<&String> = recorded.keys().chain(observed.keys()).collect();
        for path in paths {
            let none = Vec::new();
            let (r, o) = (
                recorded.get(path).unwrap_or(&none),
                observed.get(path).unwrap_or(&none),
            );
            let ([r], [o]) = (&r[..], &o[..]) else {
                let shown = |sections: &[Repository]| match sections.len() {
                    0 => ABSENT.to_owned(),
                    1 => "one section".to_owned(),
                    count => format!("{count} sections"),
                };
                self.report(Dimension::Git, Some(step), path, shown(r), shown(o));
                continue;
            };
            if r.kind != o.kind {
                self.report(
                    Dimension::Git,
                    Some(step),
                    format!("{path} repository"),
                    format!("kind: {}", r.kind.name()),
                    format!("kind: {}", o.kind.name()),
                );
            }
            let labels = |repository: &Repository| -> Vec<String> {
                let blocks = repository.blocks.iter();
                blocks.map(|block| block.label.clone()).collect()
            };
            let (left, right) = (labels(r), labels(o));
            let aligned = left == right;
            if !aligned {
                let shown = |labels: &[String]| format!("blocks: {}", labels.join(", "));
                self.report(
                    Dimension::Git,
                    Some(step),
                    path,
                    shown(&left),
                    shown(&right),
                );
            }
            let once = |labels: &[String], label: &String| {
                labels.iter().filter(|other| *other == label).count() == 1
            };
            for (index, block) in r.blocks.iter().enumerate() {
                let other = match aligned {
                    true => o.blocks.get(index),
                    false if once(&left, &block.label) && once(&right, &block.label) => {
                        o.blocks.iter().find(|other| other.label == block.label)
                    }
                    false => None,
                };
                if let Some(other) = other.filter(|other| other.lines != block.lines) {
                    self.report(
                        Dimension::Git,
                        Some(step),
                        format!("{path} {}", block.label),
                        block.lines.join("\n"),
                        other.lines.join("\n"),
                    );
                }
            }
        }
    }
}

/// Whether `a` and `b`, the contents of two files, are TOML documents of one value. Content that is not
/// TOML equals nothing.
fn same_toml(a: &[u8], b: &[u8]) -> bool {
    let parsed = |content: &[u8]| {
        let text = std::str::from_utf8(content).ok()?;
        text.parse::<toml::Table>().ok()
    };
    match (parsed(a), parsed(b)) {
        (Some(a), Some(b)) => a == b,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_pointer_is_empty_or_starts_with_a_slash_and_reads_its_escapes() {
        let tokens = |pointer: &str| pointer_tokens(pointer).expect("a pointer");
        assert_eq!(tokens(""), Vec::<String>::new());
        assert_eq!(tokens("/"), [""]);
        assert_eq!(tokens("/a/0/*"), ["a", "0", "*"]);
        assert_eq!(tokens("/a~1b/~0~01"), ["a/b", "~~1"]);
        for refused in ["a", "a/b", "*", "/a~", "/~2", "/a~x"] {
            assert!(pointer_tokens(refused).is_err(), "{refused}");
        }
        let path = ["a/b".to_owned(), "~".to_owned(), "0".to_owned()];
        assert_eq!(pointer(&path), "/a~1b/~0/0");
        assert_eq!(tokens(&pointer(&path)), path);
    }

    #[test]
    fn a_glob_matches_within_a_segment_and_across_segments() {
        let matches = |glob_text: &str, path: &str| {
            let segments = glob(glob_text).expect("a glob");
            matches_glob(&segments, &path.split('/').collect::<Vec<_>>())
        };
        for (pattern, path) in [
            ("ws/a.toml", "ws/a.toml"),
            ("ws/*.toml", "ws/a.toml"),
            ("ws/*", "ws/.hidden"),
            ("ws/a*c*e", "ws/abcde"),
            ("ws/*a*", "ws/a"),
            ("ws/**", "ws/a/b/c"),
            ("ws/**", "ws"),
            ("**/c", "c"),
            ("**/c", "a/b/c"),
            ("ws/**/c", "ws/c"),
            ("ws/**/b/**/d", "ws/a/b/c/d"),
            ("**", "anything/at/all"),
        ] {
            assert!(matches(pattern, path), "{pattern} against {path}");
        }
        for (pattern, path) in [
            ("ws/*.toml", "ws/a/b.toml"),
            ("ws/*", "ws"),
            ("ws/*", "ws/a/b"),
            ("ws/a", "ws/ab"),
            ("ws/a*c", "ws/abd"),
            ("ws/**/c", "ws/a/b"),
            ("a.toml", "ws/a.toml"),
            ("ws/?.toml", "ws/a.toml"),
        ] {
            assert!(!matches(pattern, path), "{pattern} against {path}");
        }
        for refused in [
            "", "/ws", "ws/", "ws//a", "ws/./a", "ws/../a", "ws/a**", "***",
        ] {
            assert!(glob(refused).is_err(), "{refused:?}");
        }
    }

    #[test]
    fn a_number_is_compared_by_its_value_at_any_length() {
        for (a, b) in [
            ("1", "1.0"),
            ("1", "1e0"),
            ("100", "1e2"),
            ("100", "1.00E+2"),
            ("0.5", "5e-1"),
            ("0", "-0.0"),
            ("0", "0e99"),
            ("-12.50", "-125e-1"),
            ("18446744073709551616", "18446744073709551616.0"),
        ] {
            assert!(same_number(a, b), "{a} and {b}");
        }
        for (a, b) in [
            ("1", "2"),
            ("1", "-1"),
            ("1", "10"),
            ("0.1", "0.10000000000000000001"),
            ("18446744073709551616", "18446744073709551617"),
            ("18446744073709551616", "1.8446744073709552e19"),
            ("1e99999999999999999999", "1e99999999999999999998"),
        ] {
            assert!(!same_number(a, b), "{a} and {b}");
        }
        assert!(same_number(
            "1e99999999999999999999",
            "1e99999999999999999999"
        ));
    }

    #[test]
    fn a_value_is_read_with_its_numbers_and_members_as_written() {
        let value =
            document(br#" {"a": 1.50, "a": [true, null, "x\n\u00e9\ud83d\ude00"], "b": {}} "#)
                .expect("a value");
        assert_eq!(
            value,
            Json::Object(vec![
                ("a".to_owned(), Json::Number("1.50".to_owned())),
                (
                    "a".to_owned(),
                    Json::Array(vec![
                        Json::Bool(true),
                        Json::Null,
                        Json::Text("x\n\u{e9}\u{1f600}".to_owned())
                    ])
                ),
                ("b".to_owned(), Json::Object(Vec::new())),
            ])
        );
        assert_eq!(
            encoded(&value),
            r#"{"a":1.50,"a":[true,null,"x\né😀"],"b":{}}"#
        );
        assert_eq!(stream(b"1 2\n[3]{}").map(|values| values.len()), Some(4));
        assert_eq!(stream(b"1\"a\"2[]3").map(|values| values.len()), Some(5));
        assert_eq!(stream(b" \n").map(|values| values.len()), Some(0));
    }

    #[test]
    fn what_is_not_json_is_not_read() {
        for text in [
            "{",
            "[1,]",
            "{\"a\"}",
            "{\"a\":}",
            "{a:1}",
            "01",
            "1.",
            ".5",
            "1e",
            "-",
            "+1",
            "tru",
            "\"a",
            "\"\\x\"",
            "\"\\ud800\"",
            "\"\\udc00\"",
            "\"a\tb\"",
            "nul",
            "[1 2]",
            "x",
            "1-2",
            "truefalse",
            "1null",
        ] {
            assert_eq!(stream(text.as_bytes()), None, "{text}");
        }
        assert_eq!(document(b"1 2"), None);
        assert_eq!(document(b""), None);
        let deep = format!("{}{}", "[".repeat(DEPTH + 1), "]".repeat(DEPTH + 1));
        assert_eq!(stream(deep.as_bytes()), None);
        let deep = format!("{}{}", "[".repeat(DEPTH), "]".repeat(DEPTH));
        assert!(stream(deep.as_bytes()).is_some());
    }

    #[test]
    fn an_exit_is_written_and_read_as_one_line() {
        for (exit, text) in [
            (Exit::Code(0), "0"),
            (Exit::Code(101), "101"),
            (Exit::Signal(9), "signal:9"),
        ] {
            assert_eq!(exit_text(exit), text);
            assert_eq!(parse_exit(text), Some(exit));
            assert_eq!(parse_exit(&format!("{text}\n")), Some(exit));
        }
        for refused in [
            "",
            "\n",
            "1\n\n",
            " 1",
            "1 ",
            "+1",
            "-1",
            "0x1",
            "signal:",
            "signal:-9",
            "signal: 9",
            "1\n2",
            "99999999999",
        ] {
            assert_eq!(parse_exit(refused), None, "{refused:?}");
        }
    }
}
