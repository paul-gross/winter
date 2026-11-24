//! The topic rules and the code generation: which files under the topics directory are topics, what each
//! says, and the topic map generated from them. Pure functions: the build script does the directory walk and
//! the file reads and hands the results in.
//!
//! Names only `std` and `pulldown_cmark`, so that it compiles as a build-script module and, under `cfg(test)`,
//! as a module of the library.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use pulldown_cmark::{Event, Parser};

/// The topics directory, as messages spell it: relative to the repository root.
pub const TOPICS_DIR: &str = "crates/core/topics";

/// The id of the entry point.
const ROOT: &str = "root";
/// The id of the eager safety text.
const SAFETY: &str = "safety";
/// The id of the generated listing; never authored.
const TOPICS: &str = "topics";
/// The title of the generated listing.
const TOPICS_TITLE: &str = "Workspace topics";

/// The most lines the body of `safety` may have.
const SAFETY_LINES: usize = 15;
/// The most lines the served text of `root` may have.
const ROOT_LINES: usize = 80;

/// The code-span text that requests a topic: alone it requests `root`, followed by a space and an id it requests
/// that topic.
const REQUEST: &str = "winter-workspace context";

/// One topic of the compiled map.
#[derive(Debug, PartialEq, Eq)]
pub struct Compiled {
    pub id: String,
    pub title: String,
    /// The served text.
    pub text: String,
    /// The ids the text references, in order of first reference, without repeats or the topic's own id.
    pub next: Vec<String>,
}

/// A rule that the topics directory breaks. Each variant carries the path of the offender relative to the topics
/// directory, except where no single file is at fault.
#[derive(Debug, PartialEq, Eq)]
pub enum TopicError {
    /// A file that is not a `.md` file.
    NotMarkdown { path: String },
    /// The file does not start with exactly the front matter.
    FrontMatter { path: String },
    /// The id is outside the grammar.
    IdGrammar { path: String, id: String },
    /// The id is `topics`, which is generated.
    Generated { path: String },
    /// The id belongs to an earlier file; `path` is the later one in byte order.
    Repeated {
        path: String,
        id: String,
        earlier: String,
    },
    /// No source has the id of a required topic.
    Missing { id: &'static str },
    /// The body of `safety` is longer than the limit.
    SafetyTooLong { path: String, lines: usize },
    /// The served text of `root` is longer than the limit; `path` is the file of `root`.
    RootTooLong { path: String, lines: usize },
    /// A reference names no topic.
    Unresolved { path: String, id: String },
    /// A reference is followed by more text.
    Refused { path: String, span: String },
    /// No chain of references from `root` reaches the topic.
    Unreached { path: String, id: String },
}

impl fmt::Display for TopicError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let at = |path: &str| format!("{TOPICS_DIR}/{path}");
        match self {
            Self::NotMarkdown { path } => write!(
                f,
                "{}: the topics directory holds directories and .md files only",
                at(path)
            ),
            Self::FrontMatter { path } => write!(
                f,
                "{}: a topic starts with exactly `---`, `id: <id>`, `title: <one line>`, `---` on four lines",
                at(path)
            ),
            Self::IdGrammar { path, id } => write!(
                f,
                "{}: the id `{id}` is not one or more segments of [a-z][a-z0-9-]* joined by `/`",
                at(path)
            ),
            Self::Generated { path } => write!(
                f,
                "{}: the id `{TOPICS}` is generated and never authored",
                at(path)
            ),
            Self::Repeated { path, id, earlier } => write!(
                f,
                "{}: the id `{id}` is already the id of {}",
                at(path),
                at(earlier)
            ),
            Self::Missing { id } => write!(f, "{TOPICS_DIR}/: no topic has the required id `{id}`"),
            Self::SafetyTooLong { path, lines } => write!(
                f,
                "{}: the body of `{SAFETY}` is {lines} lines, at most {SAFETY_LINES} are allowed",
                at(path)
            ),
            Self::RootTooLong { path, lines } => write!(
                f,
                "{}: the served text of `{ROOT}` is {lines} lines, at most {ROOT_LINES} are allowed",
                at(path)
            ),
            Self::Unresolved { path, id } => {
                write!(f, "{}: the reference to `{id}` names no topic", at(path))
            }
            Self::Refused { path, span } => write!(
                f,
                "{}: `{span}` is more than a request for one topic; a reference is the bare request",
                at(path)
            ),
            Self::Unreached { path, id } => write!(
                f,
                "{}: the topic `{id}` is reached by no reference from `{ROOT}`",
                at(path)
            ),
        }
    }
}

/// One authored source.
struct Source {
    path: String,
    id: String,
    title: String,
    body: String,
    references: Vec<Reference>,
}

/// An inline code span that starts like a request.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Reference {
    /// The bare request for a topic.
    Link(String),
    /// A request for a topic followed by more text; the whole span.
    Refused(String),
}

/// Whether `id` is one or more segments of `[a-z][a-z0-9-]*` joined by `/`.
fn is_topic_id(id: &str) -> bool {
    id.split('/').all(|segment| {
        let mut chars = segment.chars();
        chars.next().is_some_and(|first| first.is_ascii_lowercase())
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    })
}

/// The references of `text`, in order of appearance: the inline code spans that request a topic. Text inside a
/// code block is never a span.
fn references(text: &str) -> Vec<Reference> {
    let mut found = Vec::new();
    for event in Parser::new(text) {
        let Event::Code(span) = event else {
            continue;
        };
        if &*span == REQUEST {
            found.push(Reference::Link(ROOT.to_owned()));
            continue;
        }
        let Some(rest) = span
            .strip_prefix(REQUEST)
            .and_then(|rest| rest.strip_prefix(' '))
        else {
            continue;
        };
        let (id, more) = match rest.split_once(' ') {
            Some((id, more)) => (id, Some(more)),
            None => (rest, None),
        };
        if !is_topic_id(id) {
            continue;
        }
        found.push(match more {
            None => Reference::Link(id.to_owned()),
            Some(_) => Reference::Refused(span.to_string()),
        });
    }
    found
}

/// The ids `references` link to, in order of first reference, without repeats and without `own`.
fn next_of(references: &[Reference], own: &str) -> Vec<String> {
    let mut next: Vec<String> = Vec::new();
    for reference in references {
        if let Reference::Link(id) = reference
            && id != own
            && !next.contains(id)
        {
            next.push(id.clone());
        }
    }
    next
}

/// Splits a source into its id, title, and body, or `None` when it does not start with exactly the front matter.
fn front_matter(text: &str) -> Option<(&str, &str, &str)> {
    let rest = text.strip_prefix("---\n")?;
    let (id_line, rest) = rest.split_once('\n')?;
    let id = id_line.strip_prefix("id: ")?;
    let (title_line, rest) = rest.split_once('\n')?;
    let title = title_line.strip_prefix("title: ")?;
    let body = rest.strip_prefix("---\n")?;
    (!title.is_empty() && !title.contains('\r')).then_some((id, title, body))
}

/// Applies the rules to every entry found below the topics directory and compiles the map: one topic per
/// source plus the generated `topics`, sorted by id in byte order.
///
/// `relative_paths` are `/`-separated paths relative to the topics directory; a directory is spelled with a
/// trailing `/`. `contents` returns the text of the file at such a path. The first rule that is broken ends the
/// compilation.
pub fn compile(
    relative_paths: &[String],
    contents: &dyn Fn(&str) -> String,
) -> Result<Vec<Compiled>, TopicError> {
    let mut sorted: Vec<&String> = relative_paths.iter().collect();
    sorted.sort();

    let mut sources: Vec<Source> = Vec::new();
    for relative in sorted {
        let path = || relative.clone();
        if relative.ends_with('/') {
            continue;
        }
        if !relative.ends_with(".md") {
            return Err(TopicError::NotMarkdown { path: path() });
        }
        let text = contents(relative);
        let Some((id, title, body)) = front_matter(&text) else {
            return Err(TopicError::FrontMatter { path: path() });
        };
        if !is_topic_id(id) {
            return Err(TopicError::IdGrammar {
                path: path(),
                id: id.to_owned(),
            });
        }
        if id == TOPICS {
            return Err(TopicError::Generated { path: path() });
        }
        if let Some(earlier) = sources.iter().find(|source| source.id == id) {
            return Err(TopicError::Repeated {
                path: path(),
                id: id.to_owned(),
                earlier: earlier.path.clone(),
            });
        }
        sources.push(Source {
            path: path(),
            id: id.to_owned(),
            title: title.to_owned(),
            body: body.to_owned(),
            references: references(body),
        });
    }

    for required in [ROOT, SAFETY] {
        if !sources.iter().any(|source| source.id == required) {
            return Err(TopicError::Missing { id: required });
        }
    }
    let by_id: BTreeMap<&str, &Source> = sources.iter().map(|s| (s.id.as_str(), s)).collect();
    let (root, safety) = (by_id[ROOT], by_id[SAFETY]);

    let lines = safety.body.lines().count();
    if lines > SAFETY_LINES {
        return Err(TopicError::SafetyTooLong {
            path: safety.path.clone(),
            lines,
        });
    }

    for source in &sources {
        for reference in &source.references {
            match reference {
                Reference::Refused(span) => {
                    return Err(TopicError::Refused {
                        path: source.path.clone(),
                        span: span.clone(),
                    });
                }
                Reference::Link(id) if id != TOPICS && !by_id.contains_key(id.as_str()) => {
                    return Err(TopicError::Unresolved {
                        path: source.path.clone(),
                        id: id.clone(),
                    });
                }
                Reference::Link(_) => {}
            }
        }
    }

    let root_text = format!("{}\n{}", safety.body, root.body);
    let lines = root_text.lines().count();
    if lines > ROOT_LINES {
        return Err(TopicError::RootTooLong {
            path: root.path.clone(),
            lines,
        });
    }

    let mut topics: Vec<Compiled> = sources
        .iter()
        .map(|source| {
            let (text, spans) = if source.id == ROOT {
                (root_text.clone(), references(&root_text))
            } else {
                (source.body.clone(), source.references.clone())
            };
            Compiled {
                id: source.id.clone(),
                title: source.title.clone(),
                text,
                next: next_of(&spans, &source.id),
            }
        })
        .collect();

    let mut reached: BTreeSet<&str> = BTreeSet::from([ROOT]);
    let mut pending = vec![ROOT];
    while let Some(id) = pending.pop() {
        let Some(topic) = topics.iter().find(|topic| topic.id == id) else {
            continue;
        };
        for next in &topic.next {
            if by_id.contains_key(next.as_str()) && reached.insert(next.as_str()) {
                pending.push(next.as_str());
            }
        }
    }
    if let Some(source) = sources
        .iter()
        .find(|source| source.id != SAFETY && !reached.contains(source.id.as_str()))
    {
        return Err(TopicError::Unreached {
            path: source.path.clone(),
            id: source.id.clone(),
        });
    }

    topics.push(Compiled {
        id: TOPICS.to_owned(),
        title: TOPICS_TITLE.to_owned(),
        text: String::new(),
        next: Vec::new(),
    });
    topics.sort_by(|a, b| a.id.cmp(&b.id));
    let listing: String = topics
        .iter()
        .map(|topic| format!("- `{REQUEST} {}` — {}\n", topic.id, topic.title))
        .collect();
    let others: Vec<String> = topics
        .iter()
        .filter(|topic| topic.id != TOPICS)
        .map(|topic| topic.id.clone())
        .collect();
    if let Some(generated) = topics.iter_mut().find(|topic| topic.id == TOPICS) {
        generated.text = listing;
        generated.next = others;
    }
    Ok(topics)
}

/// The source of the generated map: `TOPICS`, sorted as `topics` is, and `SAFETY`, the entry of the topic whose
/// id is `safety`. Every text is a string literal that holds exactly the bytes of its source.
pub fn generate(topics: &[Compiled]) -> String {
    let mut source = String::from("pub(crate) static TOPICS: &[Topic] = &[\n");
    let mut safety = None;
    for (index, topic) in topics.iter().enumerate() {
        if topic.id == SAFETY {
            safety = Some(index);
        }
        let next: Vec<String> = topic
            .next
            .iter()
            .map(|id| format!("TopicId({id:?})"))
            .collect();
        source.push_str(&format!(
            "    Topic {{ id: TopicId({:?}), title: {:?}, text: {:?}, next: &[{}] }},\n",
            topic.id,
            topic.title,
            topic.text,
            next.join(", ")
        ));
    }
    source.push_str("];\n");
    if let Some(index) = safety {
        source.push_str(&format!(
            "pub(crate) static SAFETY: &Topic = &TOPICS[{index}];\n"
        ));
    }
    source
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(id: &str, body: &str) -> String {
        format!("---\nid: {id}\ntitle: Title of {id}\n---\n{body}")
    }

    /// The paths and texts of a tree, as `compile` takes them.
    fn tree(files: &[(&str, String)]) -> (Vec<String>, impl Fn(&str) -> String) {
        let paths = files.iter().map(|(path, _)| (*path).to_owned()).collect();
        let owned: Vec<(String, String)> = files
            .iter()
            .map(|(path, text)| ((*path).to_owned(), text.clone()))
            .collect();
        let read = move |path: &str| {
            owned
                .iter()
                .find(|(candidate, _)| candidate == path)
                .map(|(_, text)| text.clone())
                .unwrap_or_default()
        };
        (paths, read)
    }

    fn compiled(files: &[(&str, String)]) -> Result<Vec<Compiled>, TopicError> {
        let (paths, read) = tree(files);
        compile(&paths, &read)
    }

    fn refused(files: &[(&str, String)]) -> TopicError {
        match compiled(files) {
            Ok(_) => panic!("the tree was accepted"),
            Err(error) => error,
        }
    }

    fn minimal() -> Vec<(&'static str, String)> {
        vec![
            ("root.md", source("root", "\nEntry.\n")),
            ("safety.md", source("safety", "\nSafe.\n")),
        ]
    }

    fn with(extra: &[(&'static str, String)]) -> Vec<(&'static str, String)> {
        let mut files = minimal();
        files.extend(extra.iter().cloned());
        files
    }

    fn with_root(body: &str, extra: &[(&'static str, String)]) -> Vec<(&'static str, String)> {
        let mut files = with(extra);
        files[0].1 = source("root", body);
        files
    }

    fn assert_starts_with(error: &TopicError, path: &str) {
        let text = error.to_string();
        assert!(text.starts_with(path), "{text}");
        assert!(text.len() > path.len(), "{text}");
    }

    fn topic<'a>(topics: &'a [Compiled], id: &str) -> &'a Compiled {
        topics
            .iter()
            .find(|topic| topic.id == id)
            .unwrap_or_else(|| panic!("no topic {id}"))
    }

    fn links(text: &str) -> Vec<Reference> {
        references(text)
    }

    fn link(id: &str) -> Reference {
        Reference::Link(id.to_owned())
    }

    #[test]
    fn a_file_that_is_not_markdown_is_refused() {
        let error = refused(&with(&[("group/notes.txt", String::new())]));

        assert_eq!(
            error,
            TopicError::NotMarkdown {
                path: "group/notes.txt".to_owned()
            }
        );
        assert_starts_with(&error, "crates/core/topics/group/notes.txt");
    }

    #[test]
    fn directories_are_allowed_even_when_empty() {
        let mut files = minimal();
        files.push(("empty/", String::new()));

        assert!(compiled(&files).is_ok());
    }

    #[test]
    fn front_matter_that_differs_from_the_exact_form_is_refused() {
        let forms = [
            "id: x\ntitle: T\n---\nbody",
            "---\ntitle: T\nid: x\n---\n",
            "---\nid: x\ntitle: T\n",
            "---\nid: x\ntitle: T\nextra: 1\n---\n",
            "---\nid:x\ntitle: T\n---\n",
            "---\nid: x\ntitle:\n---\n",
            "---\n\nid: x\ntitle: T\n---\n",
            "",
        ];
        for form in forms {
            let error = refused(&with(&[("x.md", form.to_owned())]));

            assert_eq!(
                error,
                TopicError::FrontMatter {
                    path: "x.md".to_owned()
                },
                "{form:?}"
            );
            assert_starts_with(&error, "crates/core/topics/x.md");
        }
    }

    #[test]
    fn an_id_outside_the_grammar_is_refused() {
        for id in [
            "Upper", "1x", "a//b", "a/", "/a", "a_b", "a b", "ws/Push", "",
        ] {
            let error = refused(&with(&[("x.md", source(id, ""))]));

            assert_eq!(
                error,
                TopicError::IdGrammar {
                    path: "x.md".to_owned(),
                    id: id.to_owned()
                },
                "{id:?}"
            );
            assert_starts_with(&error, "crates/core/topics/x.md");
        }
    }

    #[test]
    fn an_id_that_is_a_file_name_is_no_address() {
        assert!(is_topic_id("ws-x"));
        assert!(is_topic_id("ws/push"));
        assert!(is_topic_id("a1/b-2/c"));
    }

    #[test]
    fn a_repeated_id_names_the_later_path_and_then_the_earlier() {
        let error = refused(&with(&[
            ("b/dup.md", source("dup", "")),
            ("a/dup.md", source("dup", "")),
        ]));

        assert_eq!(
            error,
            TopicError::Repeated {
                path: "b/dup.md".to_owned(),
                id: "dup".to_owned(),
                earlier: "a/dup.md".to_owned()
            }
        );
        let text = error.to_string();
        assert!(text.starts_with("crates/core/topics/b/dup.md"), "{text}");
        assert!(text.contains("crates/core/topics/a/dup.md"), "{text}");
    }

    #[test]
    fn an_authored_topics_is_refused() {
        let error = refused(&with(&[("topics.md", source("topics", ""))]));

        assert_eq!(
            error,
            TopicError::Generated {
                path: "topics.md".to_owned()
            }
        );
        assert_starts_with(&error, "crates/core/topics/topics.md");
    }

    #[test]
    fn a_missing_root_or_safety_is_refused_with_the_directory_first() {
        for (kept, missing) in [(1, "root"), (0, "safety")] {
            let files = [minimal().remove(kept)];

            let error = refused(&files);

            assert_eq!(error, TopicError::Missing { id: missing });
            assert_starts_with(&error, "crates/core/topics/");
        }
    }

    #[test]
    fn a_reference_to_an_id_no_topic_has_is_refused() {
        let error = refused(&with_root(
            "\nSee `winter-workspace context ws/push`.\n",
            &[],
        ));

        assert_eq!(
            error,
            TopicError::Unresolved {
                path: "root.md".to_owned(),
                id: "ws/push".to_owned()
            }
        );
        assert_starts_with(&error, "crates/core/topics/root.md");
    }

    #[test]
    fn an_unresolved_reference_in_safety_names_the_safety_file() {
        let mut files = minimal();
        files[1].1 = source("safety", "\n`winter-workspace context nowhere`\n");

        assert_eq!(
            refused(&files),
            TopicError::Unresolved {
                path: "safety.md".to_owned(),
                id: "nowhere".to_owned()
            }
        );
    }

    #[test]
    fn a_topic_other_than_safety_that_root_does_not_reach_is_refused() {
        let error = refused(&with(&[("lonely.md", source("lonely", "\nText.\n"))]));

        assert_eq!(
            error,
            TopicError::Unreached {
                path: "lonely.md".to_owned(),
                id: "lonely".to_owned()
            }
        );
        assert_starts_with(&error, "crates/core/topics/lonely.md");
    }

    #[test]
    fn a_topic_that_only_the_generated_listing_names_is_unreached() {
        let files = with_root(
            "\nSee `winter-workspace context topics`.\n",
            &[("lonely.md", source("lonely", ""))],
        );

        assert!(matches!(refused(&files), TopicError::Unreached { .. }));
    }

    #[test]
    fn a_topic_reached_through_another_topic_is_accepted() {
        let files = with_root(
            "\n`winter-workspace context a`\n",
            &[
                ("a.md", source("a", "\n`winter-workspace context b/c`\n")),
                ("deep/b.md", source("b/c", "\nEnd.\n")),
            ],
        );

        assert!(compiled(&files).is_ok());
    }

    #[test]
    fn a_topic_that_only_safety_references_is_reached_through_root() {
        let mut files = with(&[("a.md", source("a", ""))]);
        files[1].1 = source("safety", "\n`winter-workspace context a`\n");

        assert!(compiled(&files).is_ok());
    }

    #[test]
    fn a_span_with_text_after_the_id_is_refused() {
        let files = with_root(
            "\n`winter-workspace context ws/push --json`\n",
            &[("ws/push.md", source("ws/push", ""))],
        );

        let error = refused(&files);

        assert_eq!(
            error,
            TopicError::Refused {
                path: "root.md".to_owned(),
                span: "winter-workspace context ws/push --json".to_owned()
            }
        );
        assert_starts_with(&error, "crates/core/topics/root.md");
    }

    fn body_of_lines(count: usize) -> String {
        "line\n".repeat(count)
    }

    #[test]
    fn a_safety_body_of_16_lines_is_refused_and_one_of_15_is_not() {
        let mut files = minimal();
        files[1].1 = source("safety", &body_of_lines(15));
        assert!(compiled(&files).is_ok());

        files[1].1 = source("safety", &body_of_lines(16));
        let error = refused(&files);

        assert_eq!(
            error,
            TopicError::SafetyTooLong {
                path: "safety.md".to_owned(),
                lines: 16
            }
        );
        assert_starts_with(&error, "crates/core/topics/safety.md");
    }

    #[test]
    fn a_served_root_of_81_lines_is_refused_and_one_of_80_is_not() {
        // The safety body is one line and the composition's line break adds a blank one.
        let mut files = minimal();
        files[1].1 = source("safety", "safe\n");
        files[0].1 = source("root", &body_of_lines(78));
        assert!(compiled(&files).is_ok());

        files[0].1 = source("root", &body_of_lines(79));
        let error = refused(&files);

        assert_eq!(
            error,
            TopicError::RootTooLong {
                path: "root.md".to_owned(),
                lines: 81
            }
        );
        assert_starts_with(&error, "crates/core/topics/root.md");
    }

    #[test]
    fn the_bare_request_names_root_and_a_request_with_an_id_names_it() {
        assert_eq!(links("`winter-workspace context`"), [link("root")]);
        assert_eq!(
            links("`winter-workspace context ws/push`"),
            [link("ws/push")]
        );
        assert_eq!(links("`winter-workspace context topics`"), [link("topics")]);
    }

    #[test]
    fn a_span_whose_rest_is_no_id_names_nothing() {
        for text in [
            "`winter-workspace context <topic>`",
            "`winter-workspace context <group>/<command>`",
            "`winter-workspace context ws/<cmd>`",
            "`winter-workspace contexts`",
            "`winter-workspace context  x`",
            "`winter-workspace ws push`",
            "winter-workspace context ws/push",
        ] {
            assert_eq!(links(text), [], "{text}");
        }
    }

    #[test]
    fn a_span_inside_a_code_block_names_nothing() {
        let fenced =
            "```\n`winter-workspace context ws/push`\nwinter-workspace context ws/push\n```\n";
        let indented = "text\n\n    `winter-workspace context ws/push`\n";

        assert_eq!(links(fenced), []);
        assert_eq!(links(indented), []);
    }

    #[test]
    fn repeats_and_the_own_id_are_left_out_of_next_in_order_of_first_reference() {
        let spans = links(
            "`winter-workspace context b` `winter-workspace context a` `winter-workspace context b` `winter-workspace context me` `winter-workspace context`",
        );

        assert_eq!(next_of(&spans, "me"), ["b", "a", "root"]);
    }

    #[test]
    fn next_of_a_compiled_topic_keeps_the_order_of_first_reference() {
        let files = with_root(
            "\n`winter-workspace context z` `winter-workspace context a` `winter-workspace context z`\n",
            &[("z.md", source("z", "")), ("a.md", source("a", ""))],
        );

        let topics = compiled(&files).unwrap();

        assert_eq!(topic(&topics, "root").next, ["z", "a"]);
    }

    #[test]
    fn the_served_root_is_the_safety_body_a_line_break_and_the_root_body() {
        let files = with_root("\nEntry.\n", &[]);

        let topics = compiled(&files).unwrap();

        assert_eq!(topic(&topics, "root").text, "\nSafe.\n\n\nEntry.\n");
        assert_eq!(topic(&topics, "safety").text, "\nSafe.\n");
    }

    #[test]
    fn root_next_includes_what_the_composed_safety_part_references() {
        let mut files = minimal();
        files[1].1 = source(
            "safety",
            "\n`winter-workspace context topics` `winter-workspace context`\n",
        );

        let topics = compiled(&files).unwrap();

        assert_eq!(topic(&topics, "root").next, ["topics"]);
        assert_eq!(topic(&topics, "safety").next, ["topics", "root"]);
    }

    #[test]
    fn moving_safety_into_a_subdirectory_changes_neither_text() {
        let flat = compiled(&minimal()).unwrap();
        let mut moved = minimal();
        moved[1].0 = "deep/er/safety.md";

        let moved = compiled(&moved).unwrap();

        assert_eq!(flat, moved);
    }

    #[test]
    fn the_listing_holds_one_line_per_topic_in_byte_order_with_a_dash_before_a_slash() {
        let files = with_root(
            "\n`winter-workspace context ws/push` `winter-workspace context ws-x`\n",
            &[
                ("ws/push.md", source("ws/push", "")),
                ("ws-x.md", source("ws-x", "")),
            ],
        );

        let topics = compiled(&files).unwrap();
        let listing = topic(&topics, "topics");

        assert_eq!(listing.title, "Workspace topics");
        assert_eq!(
            listing.text,
            concat!(
                "- `winter-workspace context root` — Title of root\n",
                "- `winter-workspace context safety` — Title of safety\n",
                "- `winter-workspace context topics` — Workspace topics\n",
                "- `winter-workspace context ws-x` — Title of ws-x\n",
                "- `winter-workspace context ws/push` — Title of ws/push\n",
            )
        );
        assert_eq!(listing.next, ["root", "safety", "ws-x", "ws/push"]);
        let ids: Vec<&str> = topics.iter().map(|topic| topic.id.as_str()).collect();
        assert_eq!(ids, ["root", "safety", "topics", "ws-x", "ws/push"]);
    }

    #[test]
    fn the_input_order_does_not_change_the_map() {
        let files = with(&[("a.md", source("a", "")), ("b.md", source("b", ""))]);
        let mut files = with_root(
            "\n`winter-workspace context a` `winter-workspace context b`\n",
            &files[2..],
        );
        let forward = compiled(&files).unwrap();
        files.reverse();

        assert_eq!(compiled(&files).unwrap(), forward);
    }

    #[test]
    fn the_generated_source_holds_exact_bytes_and_the_safety_entry() {
        let files = with(&[]);
        let mut files = files;
        files[1].1 = source("safety", "\nQuote \" and \\ and \u{2014} and\ttab\n");

        let topics = compiled(&files).unwrap();
        let generated = generate(&topics);

        assert!(
            generated.contains("text: \"\\nQuote \\\" and \\\\ and \u{2014} and\\ttab\\n\""),
            "{generated}"
        );
        assert!(
            generated.contains("Topic { id: TopicId(\"safety\"), title: \"Title of safety\""),
            "{generated}"
        );
        assert!(
            generated.ends_with("pub(crate) static SAFETY: &Topic = &TOPICS[1];\n"),
            "{generated}"
        );
    }

    #[test]
    fn a_title_travels_to_the_generated_source() {
        let mut files = with_root(
            "\n`winter-workspace context probe/x`\n",
            &[("probe/x.md", source("probe/x", ""))],
        );
        let before = generate(&compiled(&files).unwrap());
        files[2].1 = "---\nid: probe/x\ntitle: New title\n---\n".to_owned();

        let after = generate(&compiled(&files).unwrap());

        assert!(before.contains("Title of probe/x"));
        assert!(after.contains("New title") && !after.contains("Title of probe/x"));
    }
}
