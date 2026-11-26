//! The lock file of a workspace: its reader, its upsert, and the errors of reading it.
//!
//! The file keeps the reference tool's format, and the bytes written are the reference's for the same entries.

use std::collections::BTreeMap;
use std::io::{Error, ErrorKind};
use std::path::{Path, PathBuf};

use crate::config_model;
use crate::files::{ReadFiles, WriteFiles};
use crate::git::Sha;
use crate::termination::{ConfigError, ConfigValue, FileCause, TomlParseError};
use crate::workspace_files::LOCK_FILE;

/// The first line of every written lock file.
const HEADER: &str = "# .winter/config.lock — managed by winter; commit this file.";

/// What a ref of a lock entry names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefKind {
    Branch,
    Tag,
    Commit,
}

impl RefKind {
    /// The text of the kind in the lock file, which is also how a kind is named to the user.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Branch => "branch",
            Self::Tag => "tag",
            Self::Commit => "commit",
        }
    }

    fn parse(text: &str) -> Option<Self> {
        match text {
            "branch" => Some(Self::Branch),
            "tag" => Some(Self::Tag),
            "commit" => Some(Self::Commit),
            _ => None,
        }
    }
}

/// One pinned standalone of the lock: the commit its `ref` resolved to. The commit is kept as read, unvalidated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockEntry {
    pub name: String,
    pub r#ref: String,
    pub kind: RefKind,
    pub commit: Sha,
}

/// The entries of the lock file of the workspace at `root`, by name.
///
/// A missing file gives an empty lock. Every other fault is [`ConfigError::Lock`]: a file that cannot be read
/// or is not valid TOML is [`LockError::Load`], a `version` that is not `1`, `1.0`, or `true` (or is absent) is
/// [`LockError::UnsupportedVersion`], and a `[[standalone]]` entry without a field, with an unknown `kind`, or
/// with a field that is no scalar is [`LockError::MalformedEntry`]. A field that is an integer, a float, or a
/// boolean reads as Python's `str()` renders it. Of two entries of one name the later is kept.
pub fn read_lock(
    files: &dyn ReadFiles,
    root: &Path,
) -> Result<BTreeMap<String, LockEntry>, ConfigError> {
    let path = root.join(LOCK_FILE);
    let load = |cause: FileCause| {
        ConfigError::Lock(LockError::Load {
            path: path.clone(),
            cause,
        })
    };
    let bytes = files
        .read(&path)
        .map_err(|error| load(FileCause::Io(error)))?;
    parse(&path, bytes.as_deref())
}

/// Replaces the entry of `entry.name` in the lock file of the workspace at `root`, or adds it, and keeps every
/// other entry.
///
/// One read-modify-write: the file is read, merged, sorted by name, and written whole, all under the lock of
/// [`WriteFiles::update`]. A lock that cannot be read leaves the file unwritten and is [`ConfigError::Lock`].
pub fn upsert_lock(
    writer: &dyn WriteFiles,
    root: &Path,
    entry: LockEntry,
) -> Result<(), ConfigError> {
    let path = root.join(LOCK_FILE);
    let mut refused = None;
    let updated = writer.update(&path, &mut |current| {
        let mut entries = match parse(&path, current) {
            Ok(entries) => entries,
            Err(error) => {
                refused = Some(error);
                return None;
            }
        };
        entries.insert(entry.name.clone(), entry.clone());
        Some(serialise(entries.values()).into_bytes())
    });
    if let Some(error) = refused {
        return Err(error);
    }
    updated
        .map(drop)
        .map_err(|cause| ConfigError::FileWrite { path, cause })
}

/// The entries of the lock text `bytes`, `None` for a missing file.
fn parse(path: &Path, bytes: Option<&[u8]>) -> Result<BTreeMap<String, LockEntry>, ConfigError> {
    let load = |cause: FileCause| {
        ConfigError::Lock(LockError::Load {
            path: path.to_path_buf(),
            cause,
        })
    };
    let Some(bytes) = bytes else {
        return Ok(BTreeMap::new());
    };
    let text = std::str::from_utf8(bytes)
        .map_err(|error| load(FileCause::Io(Error::new(ErrorKind::InvalidData, error))))?;
    let document = toml::from_str::<toml::Table>(text)
        .map_err(|error| load(FileCause::Toml(TomlParseError::from(error))))?;
    let version = document.get("version");
    let supported = match version {
        Some(toml::Value::Integer(1)) | Some(toml::Value::Boolean(true)) => true,
        Some(toml::Value::Float(number)) => *number == 1.0,
        _ => false,
    };
    if !supported {
        return Err(ConfigError::Lock(LockError::UnsupportedVersion {
            path: path.to_path_buf(),
            found: version.map(config_model::config_value),
        }));
    }
    let malformed = |fault| {
        ConfigError::Lock(LockError::MalformedEntry {
            path: path.to_path_buf(),
            fault,
        })
    };
    let blocks = match document.get("standalone") {
        None => &[][..],
        Some(toml::Value::Array(blocks)) => blocks.as_slice(),
        Some(_) => return Err(malformed(LockEntryFault::MissingField { field: "name" })),
    };
    let mut entries = BTreeMap::new();
    for block in blocks {
        let field = |field: &'static str| {
            block
                .as_table()
                .and_then(|table| table.get(field))
                .and_then(python_str)
                .ok_or_else(|| malformed(LockEntryFault::MissingField { field }))
        };
        let name = field("name")?;
        let r#ref = field("ref")?;
        let kind_text = field("kind")?;
        let commit = field("commit")?;
        let kind = RefKind::parse(&kind_text)
            .ok_or_else(|| malformed(LockEntryFault::InvalidKind { value: kind_text }))?;
        entries.insert(
            name.clone(),
            LockEntry {
                name,
                r#ref,
                kind,
                commit,
            },
        );
    }
    Ok(entries)
}

/// The text Python's `str()` gives a scalar TOML value; `None` for an array or a table.
fn python_str(value: &toml::Value) -> Option<String> {
    match value {
        toml::Value::String(text) => Some(text.clone()),
        toml::Value::Integer(number) => Some(number.to_string()),
        toml::Value::Float(number) => Some(python_float(*number)),
        toml::Value::Boolean(true) => Some("True".to_owned()),
        toml::Value::Boolean(false) => Some("False".to_owned()),
        toml::Value::Datetime(moment) => Some(moment.to_string()),
        toml::Value::Array(_) | toml::Value::Table(_) => None,
    }
}

/// A float as Python's `repr` renders it: the shortest round-trip digits, an exponent written `e+20` or `e-05`.
fn python_float(number: f64) -> String {
    if number.is_nan() {
        return "nan".to_owned();
    }
    if number.is_infinite() {
        return if number < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    let rendered = format!("{number:?}");
    let Some((mantissa, exponent)) = rendered.split_once('e') else {
        return rendered;
    };
    let (sign, digits) = match exponent.strip_prefix('-') {
        Some(digits) => ('-', digits),
        None => ('+', exponent),
    };
    format!("{mantissa}e{sign}{digits:0>2}")
}

/// The lock file text of `entries`, which are given in order of name.
fn serialise<'e>(entries: impl Iterator<Item = &'e LockEntry>) -> String {
    let mut text = format!("{HEADER}\nversion = 1\n");
    for entry in entries {
        text.push_str("\n[[standalone]]\n");
        for (key, value) in [
            ("name", entry.name.as_str()),
            ("ref", entry.r#ref.as_str()),
            ("kind", entry.kind.as_str()),
            ("commit", entry.commit.as_str()),
        ] {
            text.push_str(&format!("{key} = {}\n", basic_string(value)));
        }
    }
    text
}

/// `value` as a TOML basic string, escaped as the reference's writer escapes it.
fn basic_string(value: &str) -> String {
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('"');
    for character in value.chars() {
        match character {
            '"' => quoted.push_str("\\\""),
            '\\' => quoted.push_str("\\\\"),
            '\u{8}' => quoted.push_str("\\b"),
            '\t' => quoted.push_str("\\t"),
            '\n' => quoted.push_str("\\n"),
            '\u{c}' => quoted.push_str("\\f"),
            '\r' => quoted.push_str("\\r"),
            control if control < ' ' || control == '\u{7f}' => {
                quoted.push_str(&format!("\\u{:04x}", u32::from(control)));
            }
            other => quoted.push(other),
        }
    }
    quoted.push('"');
    quoted
}

/// A failure to read the lock file, carried as `ConfigError::Lock`.
#[derive(Debug, thiserror::Error)]
pub enum LockError {
    /// The file is unreadable, or not valid TOML.
    #[error("cannot load {}: {cause}", .path.display())]
    Load {
        path: PathBuf,
        #[source]
        cause: FileCause,
    },
    #[error("{}: unsupported lock file version", .path.display())]
    UnsupportedVersion {
        path: PathBuf,
        found: Option<ConfigValue>,
    },
    #[error("{}: malformed [[standalone]] entry", .path.display())]
    MalformedEntry {
        path: PathBuf,
        fault: LockEntryFault,
    },
}

/// What is wrong with a `[[standalone]]` entry of the lock.
#[derive(Debug)]
pub enum LockEntryFault {
    /// A field the entry lacks: `name`, `ref`, `kind`, or `commit`.
    MissingField {
        field: &'static str,
    },
    InvalidKind {
        value: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    const LOCK: &str = "/ws/.winter/config.lock";

    fn entry(name: &str, r#ref: &str, kind: RefKind, commit: &str) -> LockEntry {
        LockEntry {
            name: name.to_owned(),
            r#ref: r#ref.to_owned(),
            kind,
            commit: commit.to_owned(),
        }
    }

    fn read_text(text: &str) -> Result<BTreeMap<String, LockEntry>, ConfigError> {
        parse(Path::new(LOCK), Some(text.as_bytes()))
    }

    fn written(entries: &[LockEntry]) -> String {
        let sorted: BTreeMap<_, _> = entries
            .iter()
            .map(|e| (e.name.clone(), e.clone()))
            .collect();
        serialise(sorted.values())
    }

    const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

    #[test]
    fn one_entry_is_written_as_the_lock_example() {
        let text = written(&[entry("winter-context", "main", RefKind::Branch, COMMIT)]);

        assert_eq!(
            text,
            "# .winter/config.lock — managed by winter; commit this file.\nversion = 1\n\n\
             [[standalone]]\nname = \"winter-context\"\nref = \"main\"\nkind = \"branch\"\n\
             commit = \"0123456789abcdef0123456789abcdef01234567\"\n"
        );
    }

    #[test]
    fn entries_are_written_sorted_by_name() {
        let text = written(&[
            entry("zeta", "v1", RefKind::Tag, COMMIT),
            entry("alpha", "main", RefKind::Branch, COMMIT),
        ]);

        assert!(text.find("\"alpha\"").unwrap() < text.find("\"zeta\"").unwrap());
        assert_eq!(text.matches("\n\n[[standalone]]\n").count(), 2);
    }

    #[test]
    fn an_empty_lock_is_the_comment_and_the_version_alone() {
        assert_eq!(
            written(&[]),
            "# .winter/config.lock — managed by winter; commit this file.\nversion = 1\n"
        );
    }

    #[test]
    fn a_written_lock_reads_back_and_escapes_what_toml_requires() {
        let tricky = entry("a\"b\\c", "x\ty", RefKind::Commit, "\u{1}é");
        let read = read_text(&written(std::slice::from_ref(&tricky))).expect("a lock");

        assert_eq!(read.get("a\"b\\c"), Some(&tricky));
    }

    #[test]
    fn versions_one_one_point_zero_and_true_are_read() {
        for version in ["1", "1.0", "true"] {
            assert!(
                read_text(&format!("version = {version}\n")).is_ok(),
                "{version}"
            );
        }
    }

    #[test]
    fn other_versions_and_an_absent_version_are_unsupported() {
        for text in ["version = 2\n", "version = \"1\"\n", "[[standalone]]\n"] {
            assert!(
                matches!(
                    read_text(text),
                    Err(ConfigError::Lock(LockError::UnsupportedVersion { .. }))
                ),
                "{text}"
            );
        }
    }

    fn lock_with(field_lines: &str) -> String {
        format!("version = 1\n[[standalone]]\nname = \"n\"\n{field_lines}")
    }

    #[test]
    fn a_scalar_field_reads_as_pythons_str_gives_it() {
        let read =
            read_text(&lock_with("ref = 1.5\nkind = \"branch\"\ncommit = 123\n")).expect("a lock");
        assert_eq!(read["n"].commit, "123");
        assert_eq!(read["n"].r#ref, "1.5");

        let read =
            read_text(&lock_with("ref = true\nkind = \"tag\"\ncommit = false\n")).expect("a lock");
        assert_eq!(read["n"].r#ref, "True");
        assert_eq!(read["n"].commit, "False");
    }

    #[test]
    fn floats_render_as_python_renders_them() {
        for (number, text) in [
            (1.0, "1.0"),
            (1e20, "1e+20"),
            (1e-5, "1e-05"),
            (-2.5e-7, "-2.5e-07"),
            (f64::INFINITY, "inf"),
        ] {
            assert_eq!(python_float(number), text);
        }
    }

    #[test]
    fn an_unsupported_version_names_the_file() {
        let error = LockError::UnsupportedVersion {
            path: PathBuf::from("/ws/.winter/config.lock"),
            found: None,
        };
        assert_eq!(
            error.to_string(),
            "/ws/.winter/config.lock: unsupported lock file version"
        );
    }

    #[test]
    fn a_malformed_entry_names_the_file() {
        let error = LockError::MalformedEntry {
            path: PathBuf::from("/ws/.winter/config.lock"),
            fault: LockEntryFault::MissingField { field: "commit" },
        };
        assert_eq!(
            error.to_string(),
            "/ws/.winter/config.lock: malformed [[standalone]] entry"
        );
    }
}
