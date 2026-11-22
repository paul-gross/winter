//! Repository selection patterns: the shape a command accepts, and the match of a name against a glob.
//!
//! Pure: it reads nothing and writes nothing. The match is the one Python 3.14's `fnmatch.fnmatchcase`
//! makes, so a pattern selects here what it selects in the reference tool.

use crate::termination::SelectionError;

/// Refuses a pattern that is no bare name: an empty one, and one that holds a `/`.
pub(crate) fn bare_name(pattern: &str) -> Result<(), SelectionError> {
    if pattern.is_empty() {
        return Err(SelectionError::EmptyPattern);
    }
    if pattern.contains('/') {
        return Err(SelectionError::NotBareName {
            pattern: pattern.to_owned(),
        });
    }
    Ok(())
}

/// Whether `pattern` is a glob, one that holds `*`, `?`, or `[`, and not a literal name.
pub(crate) fn is_glob(pattern: &str) -> bool {
    pattern.contains(['*', '?', '['])
}

/// Whether the whole of `name` matches `pattern`, as `fnmatch.fnmatchcase(name, pattern)` answers it.
///
/// The match is case-sensitive and by character. `*` matches any run of characters, an empty one included,
/// `?` any one character, `[seq]` one character of the set, and `[!seq]` one character outside it. A `]`
/// that comes first in a set, after the `!` if there is one, belongs to the set. A `[` that no `]` closes
/// is the character itself. Every other character, a backslash included, matches itself.
pub(crate) fn fnmatchcase(name: &str, pattern: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let name: Vec<char> = name.chars().collect();
    matches(&name, &parts(&pattern))
}

/// One piece of a pattern: a run of any length, or what one character has to be.
#[derive(Debug, PartialEq, Eq)]
enum Part {
    /// `*`, and any run of `*`.
    Run,
    One(One),
}

/// What one character of a name is held against.
#[derive(Debug, PartialEq, Eq)]
enum One {
    /// `?`, and a negated set that holds nothing.
    Any,
    /// A set that holds nothing.
    Nothing,
    Literal(char),
    Set(Set),
}

/// The characters of a bracket expression: single characters and inclusive ranges.
#[derive(Debug, PartialEq, Eq)]
struct Set {
    negated: bool,
    singles: Vec<char>,
    ranges: Vec<(char, char)>,
}

impl One {
    fn admits(&self, c: char) -> bool {
        match self {
            Self::Any => true,
            Self::Nothing => false,
            Self::Literal(literal) => *literal == c,
            Self::Set(set) => {
                let held = set.singles.contains(&c)
                    || set
                        .ranges
                        .iter()
                        .any(|(low, high)| (*low..=*high).contains(&c));
                held != set.negated
            }
        }
    }
}

/// The parts of `pattern`, in order.
fn parts(pattern: &[char]) -> Vec<Part> {
    let mut parts = Vec::new();
    let mut at = 0;
    while let Some(&c) = pattern.get(at) {
        at += 1;
        match c {
            '*' => {
                if parts.last() != Some(&Part::Run) {
                    parts.push(Part::Run);
                }
            }
            '?' => parts.push(Part::One(One::Any)),
            '[' => match closing(pattern, at) {
                Some(close) => {
                    parts.push(Part::One(set(&pattern[at..close])));
                    at = close + 1;
                }
                None => parts.push(Part::One(One::Literal('['))),
            },
            other => parts.push(Part::One(One::Literal(other))),
        }
    }
    parts
}

/// The index of the `]` that closes the bracket expression whose content starts at `start`, if one does.
/// A `!` that comes first, and a `]` that comes first or right after that `!`, are content.
fn closing(pattern: &[char], start: usize) -> Option<usize> {
    let mut at = start;
    if pattern.get(at) == Some(&'!') {
        at += 1;
    }
    if pattern.get(at) == Some(&']') {
        at += 1;
    }
    (at..pattern.len()).find(|&index| pattern[index] == ']')
}

/// What the content of a bracket expression admits.
///
/// The content is cut into chunks at each `-` that makes a range: a `-` that comes first, or right after a
/// leading `!`, makes none, and neither does one within two characters of the `-` before it. A range runs
/// from the last character of one chunk to the first of the next. A range whose start is above its end is
/// dropped with both of its ends. A `-` that comes last is a character of the set. What is left decides the
/// rest: nothing at all admits no character, a lone `!` admits every character, and a leading `!` negates.
fn set(content: &[char]) -> One {
    let mut chunks: Vec<Vec<char>> = Vec::new();
    let mut from = 0;
    let mut search = if content.first() == Some(&'!') { 2 } else { 1 };
    while let Some(hyphen) = (search..content.len()).find(|&index| content[index] == '-') {
        chunks.push(content[from..hyphen].to_vec());
        from = hyphen + 1;
        search = hyphen + 3;
    }
    let rest = &content[from..];
    match chunks.last_mut() {
        Some(last) if rest.is_empty() => last.push('-'),
        _ => chunks.push(rest.to_vec()),
    }
    for index in (1..chunks.len()).rev() {
        let (Some(&start), Some(&end)) = (chunks[index - 1].last(), chunks[index].first()) else {
            continue;
        };
        if start > end {
            let follower = chunks.remove(index);
            let leader = &mut chunks[index - 1];
            leader.pop();
            leader.extend_from_slice(&follower[1..]);
        }
    }

    // The chunks joined again, with `None` for the `-` of a range between two of them.
    let mut joined: Vec<Option<char>> = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        if index > 0 {
            joined.push(None);
        }
        joined.extend(chunk.iter().copied().map(Some));
    }
    let negated = joined.first() == Some(&Some('!'));
    match (joined.len(), negated) {
        (0, _) => return One::Nothing,
        (1, true) => return One::Any,
        _ => {}
    }
    let mut singles = Vec::new();
    let mut ranges = Vec::new();
    let mut rest = &joined[usize::from(negated)..];
    while let Some((first, after)) = rest.split_first() {
        let low = first.unwrap_or('-');
        match after {
            [None, Some(high), after @ ..] => {
                ranges.push((low, *high));
                rest = after;
            }
            _ => {
                singles.push(low);
                rest = after;
            }
        }
    }
    One::Set(Set {
        negated,
        singles,
        ranges,
    })
}

/// Whether the whole of `name` matches `parts`. Every part but a run takes exactly one character, so the
/// match walks both once and, on a mismatch, lets the last run take one character more.
fn matches(name: &[char], parts: &[Part]) -> bool {
    let (mut n, mut p) = (0, 0);
    let mut resume: Option<(usize, usize)> = None;
    while n < name.len() {
        match parts.get(p) {
            Some(Part::Run) => {
                resume = Some((p, n));
                p += 1;
            }
            Some(Part::One(one)) if one.admits(name[n]) => {
                n += 1;
                p += 1;
            }
            _ => match resume {
                Some((run, taken)) => {
                    resume = Some((run, taken + 1));
                    n = taken + 1;
                    p = run + 1;
                }
                None => return false,
            },
        }
    }
    parts[p..].iter().all(|part| *part == Part::Run)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Asserts each `(pattern, name, expected)` case, as Python 3.14 answers `fnmatchcase(name, pattern)`.
    fn check(cases: &[(&str, &str, bool)]) {
        for (pattern, name, expected) in cases {
            assert_eq!(
                fnmatchcase(name, pattern),
                *expected,
                "fnmatchcase({name:?}, {pattern:?})"
            );
        }
    }

    #[test]
    fn an_empty_pattern_is_refused() {
        let refusal = bare_name("").expect_err("a refusal");

        assert!(matches!(refusal, SelectionError::EmptyPattern));
        assert_eq!(refusal.to_string(), "empty pattern is not allowed");
    }

    #[test]
    fn a_pattern_with_a_slash_is_refused_by_name() {
        for pattern in ["a/b", "/", "a/", "/b", "*/lib", "a/b/c"] {
            let refusal = bare_name(pattern).expect_err("a refusal");

            assert!(
                matches!(&refusal, SelectionError::NotBareName { pattern: named } if named == pattern),
                "{pattern:?}: {refusal:?}"
            );
        }
        assert_eq!(
            bare_name("a/b").expect_err("a refusal").to_string(),
            "invalid pattern 'a/b': this command takes bare names, not '<segment>/<segment>' (no '/')"
        );
    }

    #[test]
    fn a_bare_name_passes_glob_or_not() {
        for pattern in ["lib", "lib-*", "?", "[ab]", " ", "a\\b", "."] {
            assert!(bare_name(pattern).is_ok(), "{pattern:?}");
        }
    }

    #[test]
    fn a_pattern_is_a_glob_when_it_holds_a_wildcard_character() {
        for pattern in ["*", "lib-*", "li?", "[ab]", "a[", "["] {
            assert!(is_glob(pattern), "{pattern:?}");
        }
        for pattern in ["", "lib", "a]", "a!b", "a-b", "a\\b", "{a,b}"] {
            assert!(!is_glob(pattern), "{pattern:?}");
        }
    }

    #[test]
    fn a_literal_matches_itself_alone() {
        check(&[
            ("lib", "lib", true),
            ("lib", "libs", false),
            ("lib", "li", false),
            ("lib", "", false),
            ("", "", true),
            ("", "a", false),
            ("a.b", "a.b", true),
            ("a.b", "axb", false),
            ("a\\b", "a\\b", true),
            ("a\\*", "a\\", true),
            ("a\\*", "a*", false),
            ("a+(b)|^$", "a+(b)|^$", true),
        ]);
    }

    #[test]
    fn a_star_matches_any_run() {
        check(&[
            ("*", "", true),
            ("*", "anything", true),
            ("*", "a/b", true),
            ("*", "a\nb", true),
            ("lib-*", "lib-", true),
            ("lib-*", "lib-core", true),
            ("lib-*", "lib", false),
            ("*-core", "lib-core", true),
            ("*-core", "lib-core-x", false),
            ("a*b*c", "abc", true),
            ("a*b*c", "aXbYbZc", true),
            ("a*b*c", "aXbY", false),
            ("a**b", "ab", true),
            ("a**b", "aXYb", true),
            ("*a", "ba", true),
            ("*a", "aab", false),
            ("*ab", "aab", true),
            ("a*", "b", false),
        ]);
    }

    #[test]
    fn a_question_mark_matches_exactly_one_character() {
        check(&[
            ("?", "a", true),
            ("?", "", false),
            ("?", "ab", false),
            ("?", "\n", true),
            ("?", "é", true),
            ("li?", "lib", true),
            ("li?", "li", false),
            ("??", "ab", true),
            ("a?c*", "abc", true),
            ("a?c*", "ac", false),
        ]);
    }

    #[test]
    fn a_set_matches_one_of_its_characters() {
        check(&[
            ("[abc]", "a", true),
            ("[abc]", "c", true),
            ("[abc]", "d", false),
            ("[abc]", "", false),
            ("[abc]", "ab", false),
            ("lib[12]", "lib1", true),
            ("lib[12]", "lib3", false),
            ("[*]", "*", true),
            ("[*]", "a", false),
            ("[?]", "?", true),
            ("[?]", "a", false),
            ("[[]", "[", true),
            ("[[a]", "a", true),
            ("[^a]", "^", true),
            ("[^a]", "a", true),
            ("[^a]", "b", false),
            ("[a!]", "!", true),
            ("[a\\]", "\\", true),
            ("[a\\]", "a", true),
            ("[a&&b]", "&", true),
            ("[a&&b]", "b", true),
            ("[a||b~~c]", "|", true),
            ("[a||b~~c]", "~", true),
            ("[a||b~~c]", "c", true),
        ]);
    }

    #[test]
    fn a_negated_set_matches_one_character_outside_it() {
        check(&[
            ("[!abc]", "d", true),
            ("[!abc]", "a", false),
            ("[!abc]", "", false),
            ("[!abc]", "de", false),
            ("[!a]*", "bcd", true),
            ("[!a]*", "abc", false),
            ("[!!]", "a", true),
            ("[!!]", "!", false),
            ("[!^]", "^", false),
            ("[!^]", "a", true),
            ("[!a-c]", "b", false),
            ("[!a-c]", "d", true),
            ("[!-a]", "-", false),
            ("[!-a]", "b", true),
        ]);
    }

    #[test]
    fn a_leading_bracket_close_belongs_to_the_set() {
        check(&[
            ("[]]", "]", true),
            ("[]]", "a", false),
            ("[]a]", "]", true),
            ("[]a]", "a", true),
            ("[]a]", "b", false),
            ("[!]]", "]", false),
            ("[!]]", "a", true),
            ("[!]a]", "a", false),
            ("[!]a]", "b", true),
            ("[]-a]", "]", true),
            ("[]-a]", "_", true),
            ("[]-a]", "b", false),
            ("[a]]", "a]", true),
            ("[a]]", "]", false),
        ]);
    }

    #[test]
    fn a_range_matches_the_characters_between_its_ends() {
        check(&[
            ("[a-c]", "a", true),
            ("[a-c]", "b", true),
            ("[a-c]", "c", true),
            ("[a-c]", "d", false),
            ("[a-c]", "-", false),
            ("[a-a]", "a", true),
            ("[0-9a-f]", "7", true),
            ("[0-9a-f]", "e", true),
            ("[0-9a-f]", "g", false),
            ("[a-cx]", "x", true),
            ("[xa-c]", "b", true),
            ("[xa-c]", "x", true),
            ("v[0-9].[0-9]", "v1.2", true),
            ("v[0-9].[0-9]", "v1x2", false),
        ]);
    }

    #[test]
    fn a_hyphen_at_an_edge_or_right_after_a_range_is_a_character() {
        check(&[
            ("[-a]", "-", true),
            ("[-a]", "a", true),
            ("[-a]", "b", false),
            ("[a-]", "-", true),
            ("[a-]", "a", true),
            ("[a-]", "b", false),
            ("[-]", "-", true),
            ("[--]", "-", true),
            ("[a-c-e]", "-", true),
            ("[a-c-e]", "e", true),
            ("[a-c-e]", "d", false),
            ("[a-c-e]", "b", true),
            ("[---]", "-", true),
            ("[+--]", ",", true),
            ("[+--]", ".", false),
        ]);
    }

    #[test]
    fn a_range_that_runs_backwards_is_dropped_with_its_ends() {
        check(&[
            ("[c-a]", "b", false),
            ("[c-a]", "a", false),
            ("[c-a]", "c", false),
            ("[c-a]", "-", false),
            ("x[c-a]", "x", false),
            ("[!c-a]", "b", true),
            ("[!c-a]", "\n", true),
            ("[!c-a]", "", false),
            ("[xc-a]", "x", true),
            ("[xc-a]", "c", false),
            ("[c-ax]", "x", true),
            ("[c-ax]", "a", false),
            ("[c-a!]", "z", true),
            ("[c-a!x]", "x", false),
            ("[c-a!x]", "y", true),
            ("[c-a^]", "^", true),
            ("[c-a^]", "a", false),
            ("[a--]", "a", false),
            ("[a--]", "-", false),
            ("[b-ac-d]", "c", true),
            ("[b-ac-d]", "d", true),
            ("[b-ac-d]", "a", false),
            ("[a-cb-a]", "a", true),
            ("[a-cb-a]", "c", true),
            ("[a-cb-a]", "d", false),
        ]);
    }

    #[test]
    fn an_unclosed_bracket_is_the_character_itself() {
        check(&[
            ("[", "[", true),
            ("[", "", false),
            ("[abc", "[abc", true),
            ("[abc", "a", false),
            ("a[", "a[", true),
            ("[!", "[!", true),
            ("[]", "[]", true),
            ("[!]", "[!]", true),
            ("[a*", "[a", true),
            ("[a*", "[abc", true),
            ("[a*", "a", false),
            ("[?", "[x", true),
            ("[[a]", "[", true),
            ("a[b[c]", "ab", true),
            ("a[b[c]", "a[", true),
        ]);
    }

    #[test]
    fn the_match_is_case_sensitive() {
        check(&[
            ("lib", "LIB", false),
            ("LIB", "lib", false),
            ("Lib*", "Library", true),
            ("Lib*", "library", false),
            ("[a-c]", "B", false),
            ("[A-C]", "b", false),
            ("[!a]", "A", true),
        ]);
    }
}
