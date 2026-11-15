//! Normalization of what a scenario stores: the root of the tree replaced by its token, and a JSON stream
//! parsed and encoded again in one canonical form.
//!
//! The root is replaced in bytes, before anything is parsed: a JSON stream is read from bytes that were
//! normalized already, which [`stdout_json`] and [`json_values`] leave to their caller. Nothing else is
//! rewritten.
//!
//! The canonical form of a JSON stream is not the stream: two streams that differ can have one canonical
//! form, so equal canonical forms do not show that the outputs are equal, and the raw output has to be
//! compared as well. Beyond the white space and the order of keys it is meant to merge, the form merges:
//!
//! - integers beyond 64 bits: a number that fits neither a signed nor an unsigned 64-bit integer is read
//!   as a floating-point number, so `18446744073709551616` and `18446744073709551617` are encoded alike;
//! - a repeated key: the last member of an object with a given key is the one kept, so `{"a":1,"a":2}` is
//!   encoded as `{"a":2}` is;
//! - under `json-lines`, the line `{"$text":"x"}` and the line `x` that is not JSON, which are both the
//!   object `{"$text":"x"}`;
//! - under `json-lines`, lines that are not UTF-8: each invalid sequence becomes the replacement
//!   character, so two such lines that differ only there are encoded alike.

use std::fmt;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use serde_json::Value;

use super::fixture::ROOT_TOKEN;
use super::model::StdoutMode;

/// The key of the one-member object a line that is not JSON is stored as.
const TEXT_KEY: &str = "$text";

/// `content` with every occurrence of `root`, the canonical root of a tree, as bytes, replaced by the six
/// bytes `<ROOT>`. An occurrence is replaced wherever it stands, inside a longer name included.
pub fn root(content: &[u8], root: &Path) -> Vec<u8> {
    replace(content, root.as_os_str().as_bytes(), ROOT_TOKEN)
}

/// `content` with every occurrence of the bytes `path` replaced by `token`, read from the start and never
/// inside a replacement. An empty `path` occurs nowhere.
pub fn replace(content: &[u8], path: &[u8], token: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return content.to_vec();
    }
    let mut replaced = Vec::with_capacity(content.len());
    let mut rest = content;
    while let Some(at) = rest.windows(path.len()).position(|window| window == path) {
        replaced.extend_from_slice(&rest[..at]);
        replaced.extend_from_slice(token);
        rest = &rest[at + path.len()..];
    }
    replaced.extend_from_slice(rest);
    replaced
}

/// The content of a file as a snapshot stores and hashes it: with the root replaced when `content` is valid
/// UTF-8, and the bytes as they are otherwise.
pub fn stored(content: &[u8], root: &Path) -> Vec<u8> {
    if std::str::from_utf8(content).is_ok() {
        self::root(content, root)
    } else {
        content.to_vec()
    }
}

/// A stream of mode `json` that is not a sequence of JSON values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvalidStream {
    /// Which value of the stream did not parse, counted from 1.
    pub value: usize,
    /// What the parser reported, with the line and the column it stopped at.
    pub reason: String,
}

impl fmt::Display for InvalidStream {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "value {} of the stream is not JSON: {}",
            self.value, self.reason
        )
    }
}

impl std::error::Error for InvalidStream {}

/// The values of `stdout`, bytes with the root replaced already, read as `mode` says; `None` for a mode
/// that stores no JSON.
///
/// Under [`StdoutMode::Json`] the stream is JSON values one after another, with or without white space
/// between them, and anything else is an [`InvalidStream`]. Under [`StdoutMode::JsonLines`] every line is one
/// value, and a line that is not, an empty one and one that is not UTF-8 included, is the object
/// `{"$text": "<line>"}`; bytes that are not UTF-8 are replaced in that text.
pub fn json_values(stdout: &[u8], mode: StdoutMode) -> Result<Option<Vec<Value>>, InvalidStream> {
    match mode {
        StdoutMode::Text => Ok(None),
        StdoutMode::Json => {
            let mut values = Vec::new();
            for value in serde_json::Deserializer::from_slice(stdout).into_iter::<Value>() {
                values.push(value.map_err(|error| InvalidStream {
                    value: values.len() + 1,
                    reason: error.to_string(),
                })?);
            }
            Ok(Some(values))
        }
        StdoutMode::JsonLines => Ok(Some(
            lines(stdout)
                .map(|line| {
                    serde_json::from_slice(line).unwrap_or_else(|_| {
                        let text = String::from_utf8_lossy(line).into_owned();
                        let member = (TEXT_KEY.to_owned(), Value::String(text));
                        Value::Object(std::iter::once(member).collect())
                    })
                })
                .collect(),
        )),
    }
}

/// `values` in the canonical form: one value per line, the keys of every object sorted by their bytes, and
/// no white space that is not part of a string.
pub fn encode(values: &[Value]) -> String {
    let mut encoded = String::new();
    for value in values {
        encode_value(value, &mut encoded);
        encoded.push('\n');
    }
    encoded
}

/// What `stdout.json` holds for `stdout`, bytes with the root replaced already: its values, read as
/// [`json_values`] reads them and encoded as [`encode`] does; `None` for a mode that stores no JSON.
pub fn stdout_json(stdout: &[u8], mode: StdoutMode) -> Result<Option<String>, InvalidStream> {
    Ok(json_values(stdout, mode)?.map(|values| encode(&values)))
}

/// The lines of `content`, each without its line feed; what follows the last line feed is a line when it
/// is not empty.
pub(super) fn lines(content: &[u8]) -> impl Iterator<Item = &[u8]> {
    let ended = content.strip_suffix(b"\n").unwrap_or(content);
    let none = content.is_empty();
    ended.split(|byte| *byte == b'\n').filter(move |_| !none)
}

/// Appends `value` to `encoded`. The keys are sorted here, whatever order the map of the parser keeps.
fn encode_value(value: &Value, encoded: &mut String) {
    match value {
        Value::Array(items) => {
            encoded.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    encoded.push(',');
                }
                encode_value(item, encoded);
            }
            encoded.push(']');
        }
        Value::Object(members) => {
            let mut members: Vec<(&String, &Value)> = members.iter().collect();
            members.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
            encoded.push('{');
            for (index, (key, member)) in members.into_iter().enumerate() {
                if index > 0 {
                    encoded.push(',');
                }
                encoded.push_str(&Value::String(key.clone()).to_string());
                encoded.push(':');
                encode_value(member, encoded);
            }
            encoded.push('}');
        }
        scalar => encoded.push_str(&scalar.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_occurrence_is_replaced_and_nothing_else() {
        assert_eq!(replace(b"a/r/b /r", b"/r", b"<T>"), b"a<T>/b <T>");
        assert_eq!(replace(b"aaa", b"aa", b"<T>"), b"<T>a");
        assert_eq!(replace(b"abc", b"", b"<T>"), b"abc");
        assert_eq!(replace(b"", b"/r", b"<T>"), b"");
        assert_eq!(replace(b"/r", b"/r/longer", b"<T>"), b"/r");
    }

    #[test]
    fn the_lines_of_a_stream_end_at_line_feeds() {
        let read = |content: &'static [u8]| lines(content).collect::<Vec<_>>();
        assert_eq!(read(b""), Vec::<&[u8]>::new());
        assert_eq!(read(b"a\n"), [b"a"]);
        assert_eq!(read(b"a\nb"), [b"a", b"b"]);
        assert_eq!(read(b"a\n\nb\n"), [&b"a"[..], b"", b"b"]);
        assert_eq!(read(b"\n"), [b""]);
    }

    #[test]
    fn the_canonical_form_sorts_keys_at_every_depth_and_keeps_the_order_of_arrays() {
        let value: Value =
            serde_json::from_str(r#"{ "b": [ {"z": 1, "a": null}, 2.50, "x y" ], "a": true }"#)
                .expect("a JSON value");
        assert_eq!(
            encode(&[value]),
            "{\"a\":true,\"b\":[{\"a\":null,\"z\":1},2.5,\"x y\"]}\n"
        );
        assert_eq!(encode(&[]), "");
    }
}
