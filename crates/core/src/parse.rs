//! The argument parser, built from a slice of registry entries, and what it answers for a command line.
//!
//! The parser is pure: it takes no handle, writes nothing, and calls no command. `process.rs` acts on its
//! answer.

use std::error::Error;
use std::ffi::OsString;

use clap::error::{ContextKind, ContextValue, ErrorKind};
use clap::{Arg, ArgAction, ArgMatches};

use crate::context::{Context, Invoked};
use crate::groups;
use crate::process::Identity;
use crate::registry::{Entry, Registration};
use crate::termination::{InternalError, ParseFault, Termination, Usage, UsageError};

type Run = fn(&ArgMatches, &mut Context<'_>) -> Termination;

/// What the parser answers for one command line.
#[derive(Debug)]
pub(crate) struct Parsed {
    pub stage: Stage,
    pub outcome: Outcome,
}

/// How far the command line got before the token that decided the outcome.
///
/// The deciding token is the first of `--help`, `--version`, or a token at fault. The command path is the
/// command names resolved among the tokens before it, and every name resolved when no token decides before the
/// end of the line: a dispatch, a bare group, a missing required argument.
#[derive(Debug)]
pub(crate) enum Stage {
    /// No command name was resolved.
    Root,
    /// At least one was. `invoked` holds the command path and the usage of its last node.
    Command { invoked: Invoked },
}

/// What the command line asks for.
#[derive(Debug)]
pub(crate) enum Outcome {
    /// The rendered help of a node, asked for with `--help`.
    Help(String),
    /// The version line, without its newline.
    Version(String),
    /// A usage error or a bare group; or, at [`Stage::Root`], an internal failure when the parser cannot be
    /// built from the registry.
    Ended(Termination),
    /// A command was selected and its arguments parsed: the registration's function, and the matches of the
    /// selected node.
    Dispatch { run: Run, matches: ArgMatches },
}

/// An entry the command tree cannot be built with. Each variant names the file that registers the entry.
#[derive(Debug, thiserror::Error)]
pub(crate) enum RegistryError {
    #[error("{file}: alias of '{of}', which is not a command beside it")]
    AliasWithoutCommand {
        file: &'static str,
        of: &'static str,
    },
    #[error("{file}: alias of '{of}', which is itself an alias")]
    AliasOfAlias {
        file: &'static str,
        of: &'static str,
    },
    #[error("{file}: the top-level command '{name}' has the name of a group")]
    GroupName {
        file: &'static str,
        name: &'static str,
    },
    #[error("{file}: neither a top-level command nor a command of a group")]
    Placement { file: &'static str },
}

/// Parses `args` against the tree built from `entries`, in one pass.
pub(crate) fn parse(entries: &[Entry], identity: &Identity, args: &[OsString]) -> Parsed {
    match Parser::build(entries, identity) {
        Ok(parser) => parser.parse(args),
        Err(error) => Parsed {
            stage: Stage::Root,
            outcome: Outcome::Ended(Termination::Internal(Box::new(InternalError::unexpected(
                error,
            )))),
        },
    }
}

struct Parser {
    command: clap::Command,
    routes: Vec<Route>,
    program: &'static str,
    version: &'static str,
}

/// The run function of one leaf, under the path the leaf is typed as.
struct Route {
    path: &'static [&'static str],
    run: Run,
}

impl Parser {
    fn build(entries: &[Entry], identity: &Identity) -> Result<Self, RegistryError> {
        let mut command = root(identity);
        let mut routes = Vec::new();
        for entry in entries {
            let (run, node) = leaf(entries, entry)?;
            command = match entry.path {
                [name] if groups::find(name).is_some() => {
                    return Err(RegistryError::GroupName {
                        file: entry.file,
                        name,
                    });
                }
                [_] => command.subcommand(node),
                [group, _] => {
                    let group =
                        groups::find(group).ok_or(RegistryError::Placement { file: entry.file })?;
                    if command.find_subcommand(group.name).is_none() {
                        command = command.subcommand(with_help(
                            clap::Command::new(group.name).about(group.about),
                        ));
                    }
                    command.mut_subcommand(group.name, |group| group.subcommand(node))
                }
                _ => return Err(RegistryError::Placement { file: entry.file }),
            };
            routes.push(Route {
                path: entry.path,
                run,
            });
        }
        Ok(Self {
            command,
            routes,
            program: identity.program,
            version: identity.version,
        })
    }

    fn parse(mut self, args: &[OsString]) -> Parsed {
        // Gives every node the name its usage line carries, before any of them is asked for it.
        self.command.build();
        match self.command.try_get_matches_from_mut(args) {
            Ok(matches) => self.accepted(matches),
            Err(error) => self.stopped(&error, args),
        }
    }

    /// The answer for a command line the parser accepted: the command it selects, or a bare group.
    fn accepted(mut self, matches: ArgMatches) -> Parsed {
        let mut path = Vec::new();
        let mut selected = matches;
        while let Some((name, below)) = selected.remove_subcommand() {
            path.push(name);
            selected = below;
        }
        let outcome = match self.route(&path) {
            Some(route) => Outcome::Dispatch {
                run: route.run,
                matches: selected,
            },
            None => Outcome::Ended(Termination::BareGroup {
                help: self.help(&path),
            }),
        };
        let usage = self.usage(&path);
        Parsed {
            stage: stage(path, usage),
            outcome,
        }
    }

    /// The answer for a command line the parser stopped at: the help or version a flag asked for, or a usage
    /// error.
    fn stopped(mut self, error: &clap::Error, args: &[OsString]) -> Parsed {
        let Resolved { path, rest } = resolved(&self.command, args);
        let usage = self.usage(&path);
        let outcome = match error.kind() {
            ErrorKind::DisplayHelp => Outcome::Help(error.render().to_string()),
            ErrorKind::DisplayVersion => {
                Outcome::Version(format!("{} {}", self.program, self.version))
            }
            _ => {
                let words = self
                    .route(&path)
                    .map(|_| surplus(node_at(&self.command, &path), rest));
                Outcome::Ended(Termination::Usage(Box::new(UsageError::Parse {
                    command_path: path.clone(),
                    usage: usage.clone(),
                    fault: fault(error, words),
                })))
            }
        };
        Parsed {
            stage: stage(path, usage),
            outcome,
        }
    }

    fn route<S: AsRef<str>>(&self, path: &[S]) -> Option<&Route> {
        self.routes.iter().find(|route| {
            route.path.len() == path.len()
                && route.path.iter().zip(path).all(|(a, b)| *a == b.as_ref())
        })
    }

    /// The text `--help` writes for the node at `path`.
    fn help(&mut self, path: &[String]) -> String {
        let args = path.iter().map(String::as_str).chain(["--help"]);
        self.command
            .try_get_matches_from_mut(args)
            .err()
            .map(|help| help.render().to_string())
            .unwrap_or_default()
    }

    /// The usage of the node at `path`: its usage line, and the command line that prints its help.
    fn usage(&mut self, path: &[String]) -> Usage {
        let words: Vec<&str> = [self.program]
            .into_iter()
            .chain(path.iter().map(String::as_str))
            .chain(["--help"])
            .collect();
        Usage {
            line: usage_line(&mut self.command, path),
            help_command: words.join(" "),
        }
    }
}

/// The stage a command line reached with `path` resolved; `usage` is that of the node at `path`.
fn stage(path: Vec<String>, usage: Usage) -> Stage {
    if path.is_empty() {
        Stage::Root
    } else {
        Stage::Command {
            invoked: Invoked { path, usage },
        }
    }
}

/// The usage line of the node at `path` below `node`, as that node's help prints it.
fn usage_line(node: &mut clap::Command, path: &[String]) -> String {
    if let Some((name, rest)) = path.split_first()
        && let Some(below) = node.find_subcommand_mut(name)
    {
        return usage_line(below, rest);
    }
    node.render_usage().to_string()
}

/// The node at `path` below `node`.
fn node_at<'c>(node: &'c clap::Command, path: &[String]) -> &'c clap::Command {
    path.iter().fold(node, |node, name| {
        node.find_subcommand(name).unwrap_or(node)
    })
}

fn root(identity: &Identity) -> clap::Command {
    with_help(
        clap::Command::new(identity.program)
            .about("Manage a Winter workspace")
            .bin_name(identity.program)
            .version(identity.version)
            .no_binary_name(true)
            .disable_version_flag(true),
    )
    .arg(
        Arg::new("version")
            .long("version")
            .action(ArgAction::Version)
            .help("Print the version"),
    )
}

/// Gives a node its one help flag: the long `--help`, with no short form and no `help` subcommand.
fn with_help(node: clap::Command) -> clap::Command {
    node.disable_help_flag(true)
        .disable_help_subcommand(true)
        .arg(
            Arg::new("help")
                .long("help")
                .action(ArgAction::Help)
                .help("Print help"),
        )
}

/// The run function and the parser node of `entry`; for an alias, those of the sibling it names, under the
/// alias name.
fn leaf(entries: &[Entry], entry: &Entry) -> Result<(Run, clap::Command), RegistryError> {
    let (name, group) = match entry.path.split_last() {
        Some(split) => split,
        None => return Err(RegistryError::Placement { file: entry.file }),
    };
    let (args, run, hidden) = match entry.registration {
        Registration::Command { args, run, hidden } => (args, run, hidden),
        Registration::Alias { of } => {
            let sibling = entries
                .iter()
                .find(|other| other.path.split_last() == Some((of, group)))
                .ok_or(RegistryError::AliasWithoutCommand {
                    file: entry.file,
                    of,
                })?;
            match sibling.registration {
                Registration::Command { args, run, hidden } => (args, run, hidden),
                Registration::Alias { .. } => {
                    return Err(RegistryError::AliasOfAlias {
                        file: entry.file,
                        of,
                    });
                }
            }
        }
    };
    let node = with_help(args(clap::Command::new(*name))).hide(*hidden);
    Ok((*run, node))
}

/// The command names a command line resolved before its deciding token, and the tokens after the last of them.
struct Resolved<'a> {
    path: Vec<String>,
    rest: &'a [OsString],
}

/// Reads the command path of a command line the parser stopped at.
///
/// The walk ends at the first `--help` or `--version`, at the first token that names neither an option nor a
/// command of the node it stands at, and at a node that has no commands. An option's value is stepped over, so
/// that a value spelled like a command does not extend the path.
fn resolved<'a>(root: &clap::Command, args: &'a [OsString]) -> Resolved<'a> {
    let mut path = Vec::new();
    let mut node = root;
    let mut rest = args;
    let mut next = 0;
    while node.has_subcommands()
        && let Some(token) = args.get(next).map(|arg| arg.to_string_lossy())
    {
        next += 1;
        if token == "--" {
            break;
        }
        if token.starts_with('-') {
            let Some(option) = option_named(node, &token) else {
                break;
            };
            if matches!(option.get_action(), ArgAction::Help | ArgAction::Version) {
                break;
            }
            if takes_next_token(option, &token) {
                next += 1;
            }
            continue;
        }
        let Some(below) = node.find_subcommand(token.as_ref()) else {
            break;
        };
        path.push(below.get_name().to_owned());
        node = below;
        rest = args.get(next..).unwrap_or_default();
    }
    Resolved { path, rest }
}

/// The words of `tokens` that `leaf` has no positional argument for, in the order given.
///
/// `tokens` are those that follow the leaf's name. An option and its value are no words; after `--` every
/// token is one, and so is a lone `-`. The answer is empty when an option the leaf does not have stands before
/// the first such word: that option is then the fault of the line.
fn surplus(leaf: &clap::Command, tokens: &[OsString]) -> Vec<String> {
    let positionals = || leaf.get_arguments().filter(|arg| arg.is_positional());
    if positionals().any(|arg| {
        arg.get_num_args()
            .is_some_and(|values| values.max_values() > 1)
    }) {
        return Vec::new();
    }
    let room = positionals().count();

    let mut words = 0;
    let mut surplus = Vec::new();
    let mut options_ended = false;
    let mut tokens = tokens.iter().map(|token| token.to_string_lossy());
    while let Some(token) = tokens.next() {
        if !options_ended && token == "--" {
            options_ended = true;
            continue;
        }
        if !options_ended && token.starts_with('-') && token != "-" {
            match option_named(leaf, &token) {
                Some(option) => {
                    if takes_next_token(option, &token) {
                        tokens.next();
                    }
                }
                None if surplus.is_empty() => return surplus,
                None => {}
            }
            continue;
        }
        words += 1;
        if words > room {
            surplus.push(token.into_owned());
        }
    }
    surplus
}

/// The option of `node` that `token` starts with: a long name up to any `=`, or the first short letter.
fn option_named<'c>(node: &'c clap::Command, token: &str) -> Option<&'c Arg> {
    match token.strip_prefix("--") {
        Some(long) => {
            let name = long.split_once('=').map_or(long, |(name, _)| name);
            node.get_arguments()
                .find(|arg| arg.get_long() == Some(name))
        }
        None => {
            let short = token.chars().nth(1);
            node.get_arguments()
                .find(|arg| short.is_some() && arg.get_short() == short)
        }
    }
}

/// Whether the value of `option`, typed as `token`, is the token after it: the option takes a value and
/// `token` carries none.
fn takes_next_token(option: &Arg, token: &str) -> bool {
    option.get_action().takes_values() && !token.contains('=') && is_whole_name(token)
}

/// Whether `token` is an option name alone, with no value attached to a short letter.
fn is_whole_name(token: &str) -> bool {
    token.starts_with("--") || token.chars().count() == 2
}

/// Classes what the parser refused.
///
/// `surplus` is `Some` when the fault happened in a command's own arguments, where a stray word is an argument
/// too many, and holds every such word of the line; it is `None` at the root or a group, where a stray word
/// names no command. A refused token spelled like an option is an argument too many where it is the first of
/// those words, as after `--`, and an unknown option otherwise.
fn fault(error: &clap::Error, surplus: Option<Vec<String>>) -> ParseFault {
    let text = |kind| match error.get(kind) {
        Some(ContextValue::String(text)) => text.clone(),
        Some(ContextValue::Strings(texts)) => texts.join(" "),
        _ => String::new(),
    };
    match error.kind() {
        ErrorKind::InvalidSubcommand => ParseFault::UnknownCommand {
            name: text(ContextKind::InvalidSubcommand),
        },
        ErrorKind::UnknownArgument => {
            let name = text(ContextKind::InvalidArg);
            let spelled_as_option = name.starts_with('-');
            match surplus {
                Some(values)
                    if values
                        .first()
                        .is_some_and(|first| !spelled_as_option || *first == name) =>
                {
                    ParseFault::UnexpectedArgument { values }
                }
                _ if spelled_as_option => ParseFault::UnknownOption { name },
                Some(_) => ParseFault::UnexpectedArgument { values: vec![name] },
                None => ParseFault::UnknownCommand { name },
            }
        }
        ErrorKind::MissingRequiredArgument => ParseFault::MissingArgument {
            metavar: text(ContextKind::InvalidArg),
        },
        ErrorKind::InvalidValue | ErrorKind::ValueValidation => {
            let param = text(ContextKind::InvalidArg);
            let value = text(ContextKind::InvalidValue);
            // The parser reports an option that was given no value as an invalid, empty one.
            if value.is_empty() && error.kind() == ErrorKind::InvalidValue {
                return ParseFault::MissingOptionValue { option: param };
            }
            let expected = match error.get(ContextKind::ValidValue) {
                Some(ContextValue::Strings(valid)) if !valid.is_empty() => {
                    format!("one of {}", valid.join(", "))
                }
                _ => error
                    .source()
                    .map_or_else(|| "a valid value".to_owned(), ToString::to_string),
            };
            ParseFault::InvalidValue {
                param,
                value,
                expected,
            }
        }
        ErrorKind::TooFewValues | ErrorKind::WrongNumberOfValues | ErrorKind::NoEquals => {
            ParseFault::MissingOptionValue {
                option: text(ContextKind::InvalidArg),
            }
        }
        _ => ParseFault::UnexpectedArgument {
            values: [ContextKind::InvalidArg, ContextKind::InvalidValue]
                .map(text)
                .into_iter()
                .filter(|value| !value.is_empty())
                .collect(),
        },
    }
}

#[cfg(test)]
mod tests {
    use std::panic::{AssertUnwindSafe, catch_unwind};

    use super::*;
    use crate::process::run_in_memory;
    use crate::registry::REGISTRY;

    const IDENTITY: Identity = Identity {
        program: "winter-workspace",
        version: "0.1.0",
    };

    fn up_args(command: clap::Command) -> clap::Command {
        command
            .about("Start the services of the matching environments")
            .arg(
                Arg::new("patterns")
                    .value_name("PATTERNS")
                    .num_args(1..)
                    .required(true),
            )
            .arg(
                Arg::new("tail")
                    .long("tail")
                    .value_name("LINES")
                    .value_parser(clap::value_parser!(u32)),
            )
            .arg(
                Arg::new("mode")
                    .long("mode")
                    .value_parser(["attached", "detached"]),
            )
    }

    fn up_run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        let patterns: Vec<&str> = matches
            .get_many::<String>("patterns")
            .expect("a required argument")
            .map(String::as_str)
            .collect();
        writeln!(cx.out, "up {}", patterns.join(" ")).expect("an in-memory write");
        Termination::Success
    }

    fn catalog_args(command: clap::Command) -> clap::Command {
        command.about("List what the provider offers")
    }

    fn catalog_run(_: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        writeln!(cx.out, "catalog").expect("an in-memory write");
        Termination::Reported
    }

    fn agents_args(command: clap::Command) -> clap::Command {
        command.about("List the agents of the workspace")
    }

    fn agents_run(_: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        writeln!(cx.out, "agents").expect("an in-memory write");
        Termination::Success
    }

    fn index_args(command: clap::Command) -> clap::Command {
        command
            .about("Print the index of a name")
            .arg(Arg::new("name").value_name("NAME").required(true))
            .arg(Arg::new("json").long("json").action(ArgAction::SetTrue))
            .arg(Arg::new("base").long("base").value_name("BASE"))
    }

    fn index_run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        let name = matches
            .get_one::<String>("name")
            .expect("a required argument");
        writeln!(cx.out, "index {name}").expect("an in-memory write");
        Termination::Success
    }

    static UP: Registration = Registration::Command {
        args: up_args,
        run: up_run,
        hidden: false,
    };
    static START: Registration = Registration::Alias { of: "up" };
    static CATALOG: Registration = Registration::Command {
        args: catalog_args,
        run: catalog_run,
        hidden: true,
    };
    static AGENTS: Registration = Registration::Command {
        args: agents_args,
        run: agents_run,
        hidden: false,
    };
    static INDEX: Registration = Registration::Command {
        args: index_args,
        run: index_run,
        hidden: false,
    };

    static ENTRIES: &[Entry] = &[
        Entry {
            path: &["agents"],
            file: "crates/core/src/commands/agents.rs",
            registration: &AGENTS,
        },
        Entry {
            path: &["service", "catalog"],
            file: "crates/core/src/commands/service/catalog.rs",
            registration: &CATALOG,
        },
        Entry {
            path: &["service", "start"],
            file: "crates/core/src/commands/service/start.rs",
            registration: &START,
        },
        Entry {
            path: &["service", "up"],
            file: "crates/core/src/commands/service/up.rs",
            registration: &UP,
        },
    ];

    /// A tree whose one command is `ws index NAME`.
    static INDEX_ENTRIES: &[Entry] = &[Entry {
        path: &["ws", "index"],
        file: "crates/core/src/commands/ws/index.rs",
        registration: &INDEX,
    }];

    fn parsed(entries: &[Entry], args: &[&str]) -> Parsed {
        let args: Vec<OsString> = args.iter().map(OsString::from).collect();
        parse(entries, &IDENTITY, &args)
    }

    /// The command path of the stage: empty at the root.
    fn path_of(stage: &Stage) -> Vec<String> {
        match stage {
            Stage::Root => Vec::new(),
            Stage::Command { invoked } => invoked.path.clone(),
        }
    }

    fn usage_of(parsed: Parsed) -> (Vec<String>, Usage, ParseFault) {
        let staged = path_of(&parsed.stage);
        match parsed.outcome {
            Outcome::Ended(Termination::Usage(error)) => {
                let UsageError::Parse {
                    command_path,
                    usage,
                    fault,
                } = *error;
                assert_eq!(command_path, staged);
                if let Stage::Command { invoked } = &parsed.stage {
                    assert_eq!(invoked.usage, usage);
                }
                (command_path, usage, fault)
            }
            other => panic!("expected a usage error, got {other:?}"),
        }
    }

    fn fault_of(parsed: Parsed) -> (Vec<String>, ParseFault) {
        let (command_path, _, fault) = usage_of(parsed);
        (command_path, fault)
    }

    fn help_of(entries: &[Entry], args: &[&str]) -> String {
        match parsed(entries, args).outcome {
            Outcome::Help(text) => text,
            other => panic!("{args:?}: expected help, got {other:?}"),
        }
    }

    /// The consistency check of a registry: the tree builds, and no node raises one of the parser's own
    /// debug assertions. A failure names the file of the entry at fault.
    fn check(entries: &[Entry]) -> Result<(), String> {
        let parser = Parser::build(entries, &IDENTITY).map_err(|error| error.to_string())?;
        for entry in entries {
            let (_, node) = leaf(entries, entry).map_err(|error| error.to_string())?;
            catch_unwind(AssertUnwindSafe(|| node.debug_assert()))
                .map_err(|_| format!("{}: the parser refuses this node", entry.file))?;
        }
        parser.command.debug_assert();
        Ok(())
    }

    /// What a row of the stage table ends as.
    #[derive(Debug, PartialEq, Eq)]
    enum Kind {
        Dispatch,
        Help,
        Version,
        Ended,
    }

    #[test]
    fn each_command_line_reaches_its_stage_with_its_command_path_and_its_outcome() {
        let rows: [(&[&str], &[&str], Kind); 12] = [
            (&["ws", "index", "NAME"], &["ws", "index"], Kind::Dispatch),
            (&["ws", "index", "--bogus"], &["ws", "index"], Kind::Ended),
            (&["ws", "index"], &["ws", "index"], Kind::Ended),
            (&["ws", "--help"], &["ws"], Kind::Help),
            (&["ws", "nope"], &["ws"], Kind::Ended),
            (&["ws"], &["ws"], Kind::Ended),
            (&["--version"], &[], Kind::Version),
            (&["--help"], &[], Kind::Help),
            (&["--bogus"], &[], Kind::Ended),
            (&["nope"], &[], Kind::Ended),
            (&["--bogus", "ws", "index"], &[], Kind::Ended),
            (&[], &[], Kind::Ended),
        ];

        for (args, path, kind) in rows {
            let parsed = parsed(INDEX_ENTRIES, args);

            match &parsed.stage {
                Stage::Root => assert!(path.is_empty(), "{args:?}: the root stage"),
                Stage::Command { invoked } => {
                    assert_eq!(invoked.path, path, "{args:?}");
                    let help_command = format!("winter-workspace {} --help", path.join(" "));
                    assert_eq!(invoked.usage.help_command, help_command, "{args:?}");
                }
            }
            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            let answered = match &parsed.outcome {
                Outcome::Dispatch { .. } => Kind::Dispatch,
                Outcome::Help(_) => Kind::Help,
                Outcome::Version(_) => Kind::Version,
                Outcome::Ended(_) => Kind::Ended,
            };
            assert_eq!(answered, kind, "{args:?}: {:?}", parsed.outcome);
        }
    }

    #[test]
    fn help_and_version_end_the_command_path_where_they_stand() {
        for (args, path) in [
            (&["--help", "ws", "index"][..], &[][..]),
            (&["--version", "ws", "index"], &[]),
            (&["ws", "--help", "index"], &["ws"]),
            (&["ws", "index", "--help"], &["ws", "index"]),
            (&["ws", "index", "--help", "name"], &["ws", "index"]),
        ] {
            let parsed = parsed(INDEX_ENTRIES, args);

            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            assert!(
                matches!(parsed.outcome, Outcome::Help(_) | Outcome::Version(_)),
                "{args:?}: {:?}",
                parsed.outcome
            );
        }
    }

    #[test]
    fn the_version_line_names_the_program_and_its_version() {
        let parsed = parsed(INDEX_ENTRIES, &["--version"]);

        assert!(
            matches!(&parsed.outcome, Outcome::Version(line) if line == "winter-workspace 0.1.0"),
            "{:?}",
            parsed.outcome
        );
    }

    #[test]
    fn a_dispatch_carries_the_matches_of_the_selected_node() {
        let parsed = parsed(INDEX_ENTRIES, &["ws", "index", "feature-x", "--json"]);

        let Outcome::Dispatch { matches, .. } = parsed.outcome else {
            panic!("expected a dispatch, got {:?}", parsed.outcome);
        };
        assert_eq!(
            matches.get_one::<String>("name").map(String::as_str),
            Some("feature-x")
        );
        assert!(matches.get_flag("json"));
    }

    #[test]
    fn the_usage_of_a_fault_is_that_of_the_node_it_belongs_to() {
        for (args, path) in [
            (&["nope"][..], &[][..]),
            (&["--bogus"], &[]),
            (&["ws", "nope"], &["ws"]),
            (&["ws", "--bogus"], &["ws"]),
            (&["ws", "index", "--bogus"], &["ws", "index"]),
            (&["ws", "index"], &["ws", "index"]),
            (&["ws", "index", "one", "two"], &["ws", "index"]),
        ] {
            let (command_path, usage, _) = usage_of(parsed(INDEX_ENTRIES, args));

            assert_eq!(command_path, path, "{args:?}");
            let words: Vec<&str> = ["winter-workspace"]
                .into_iter()
                .chain(path.iter().copied())
                .collect();
            assert_eq!(
                usage.help_command,
                format!("{} --help", words.join(" ")),
                "{args:?}"
            );
            assert!(
                usage.line.contains(&words.join(" ")),
                "{args:?}: {}",
                usage.line
            );
            let mut help_args = path.to_vec();
            help_args.push("--help");
            let help = help_of(INDEX_ENTRIES, &help_args);
            assert!(
                help.lines().any(|line| line == usage.line),
                "{args:?}: {:?} is no line of\n{help}",
                usage.line
            );
        }
    }

    #[test]
    fn every_word_too_many_is_carried_in_the_order_given() {
        for (args, values) in [
            (&["ws", "index", "one", "two"][..], &["two"][..]),
            (&["ws", "index", "one", "two", "three"], &["two", "three"]),
            (&["ws", "index", "one", "one"], &["one"]),
            (&["ws", "index", "one", "--json", "two", ""], &["two", ""]),
            (&["ws", "index", "--base", "b", "one", "two"], &["two"]),
            (&["ws", "index", "--base=b", "one", "two"], &["two"]),
            (
                &["ws", "index", "one", "two", "--", "--json"],
                &["two", "--json"],
            ),
            (&["ws", "index", "one", "--", "--json"], &["--json"]),
            (&["ws", "index", "one", "--", "--"], &["--"]),
            (&["ws", "index", "--", "one", "--bogus"], &["--bogus"]),
            (&["ws", "index", "one", "-"], &["-"]),
            (&["ws", "index", "one", "-", "-"], &["-", "-"]),
            (&["ws", "index", "one", "--", "-x"], &["-x"]),
            (
                &["ws", "index", "one", "two", "--bogus", "three"],
                &["two", "three"],
            ),
        ] {
            let (command_path, fault) = fault_of(parsed(INDEX_ENTRIES, args));

            assert_eq!(command_path, ["ws", "index"], "{args:?}");
            assert!(
                matches!(&fault, ParseFault::UnexpectedArgument { values: carried } if carried == values),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn an_unknown_option_before_the_first_word_too_many_is_the_fault() {
        for args in [
            &["ws", "index", "one", "--bogus"][..],
            &["ws", "index", "--bogus", "one"],
            &["ws", "index", "--bogus", "one", "two"],
            &["ws", "index", "one", "--bogus", "two"],
            &["ws", "index", "--bogus", "--", "one", "--bogus"],
        ] {
            let (command_path, fault) = fault_of(parsed(INDEX_ENTRIES, args));

            assert_eq!(command_path, ["ws", "index"], "{args:?}");
            assert!(
                matches!(&fault, ParseFault::UnknownOption { name } if name == "--bogus"),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn a_registry_the_tree_cannot_be_built_with_ends_as_an_internal_failure_at_the_root() {
        let entries = [Entry {
            path: &["misc", "index"],
            file: "crates/core/src/commands/misc/index.rs",
            registration: &AGENTS,
        }];

        let parsed = parsed(&entries, &["misc", "index"]);

        assert!(matches!(parsed.stage, Stage::Root), "{:?}", parsed.stage);
        let Outcome::Ended(termination) = parsed.outcome else {
            panic!("expected an ending, got {:?}", parsed.outcome);
        };
        assert!(
            matches!(
                &termination,
                Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
            ),
            "{termination:?}"
        );
        let diagnostic = termination.diagnostic().expect("a diagnostic");
        assert!(
            diagnostic.contains("crates/core/src/commands/misc/index.rs"),
            "{diagnostic}"
        );
    }

    #[test]
    fn a_command_dispatches_to_its_run() {
        let (termination, stdout, stderr) =
            run_in_memory(ENTRIES, &["service", "up", "alpha", "beta"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(stdout, "up alpha beta\n");
        assert_eq!(stderr, "");
    }

    #[test]
    fn a_top_level_command_dispatches_to_its_run() {
        let (termination, stdout, _) = run_in_memory(ENTRIES, &["agents"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(stdout, "agents\n");
    }

    #[test]
    fn an_alias_dispatches_to_the_run_of_the_command_it_names() {
        let (termination, stdout, _) = run_in_memory(ENTRIES, &["service", "start", "alpha"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(stdout, "up alpha\n");
    }

    #[test]
    fn an_alias_takes_the_arguments_of_the_command_it_names() {
        let (command_path, usage, fault) = usage_of(parsed(ENTRIES, &["service", "start"]));

        assert_eq!(command_path, ["service", "start"]);
        assert_eq!(usage.help_command, "winter-workspace service start --help");
        assert!(
            matches!(&fault, ParseFault::MissingArgument { metavar } if metavar.contains("PATTERNS")),
            "{fault:?}"
        );
    }

    #[test]
    fn a_hidden_command_is_absent_from_help_and_dispatches() {
        let help = help_of(ENTRIES, &["service", "--help"]);
        assert!(help.contains("up"), "{help}");
        assert!(!help.contains("catalog"), "{help}");

        let (termination, stdout, _) = run_in_memory(ENTRIES, &["service", "catalog"]);
        assert!(matches!(termination, Termination::Reported));
        assert_eq!(stdout, "catalog\n");
    }

    #[test]
    fn a_group_without_entries_is_an_unknown_command() {
        let (command_path, fault) = fault_of(parsed(ENTRIES, &["ws"]));

        assert!(command_path.is_empty());
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "ws"),
            "{fault:?}"
        );

        let root_help = help_of(ENTRIES, &["--help"]);
        assert!(root_help.contains("service"), "{root_help}");
        assert!(root_help.contains("agents"), "{root_help}");
        for absent in ["ws", "repo", "ext"] {
            assert!(
                !root_help
                    .lines()
                    .any(|line| line.trim_start().starts_with(absent)),
                "{root_help}"
            );
        }
    }

    #[test]
    fn a_bare_group_ends_with_its_help() {
        let parsed = parsed(ENTRIES, &["service"]);

        assert_eq!(path_of(&parsed.stage), ["service"]);
        let Outcome::Ended(Termination::BareGroup { help }) = parsed.outcome else {
            panic!("expected a bare group, got {:?}", parsed.outcome);
        };
        assert_eq!(help, help_of(ENTRIES, &["service", "--help"]));
        let listed: Vec<&str> = help
            .lines()
            .filter_map(|line| line.split_whitespace().next())
            .collect();
        assert!(listed.contains(&"up"), "{help}");
        assert!(listed.contains(&"start"), "{help}");
        assert!(!help.contains("catalog"), "{help}");
    }

    #[test]
    fn a_bare_root_ends_with_the_root_help() {
        let parsed = parsed(ENTRIES, &[]);

        assert!(matches!(parsed.stage, Stage::Root), "{:?}", parsed.stage);
        let Outcome::Ended(Termination::BareGroup { help }) = parsed.outcome else {
            panic!("expected a bare group, got {:?}", parsed.outcome);
        };
        assert_eq!(help, help_of(ENTRIES, &["--help"]));
    }

    #[test]
    fn help_of_a_command_names_its_arguments_and_its_path() {
        let help = help_of(ENTRIES, &["service", "up", "--help"]);

        assert!(help.contains("PATTERNS"), "{help}");
        assert!(help.contains("--tail"), "{help}");
        assert!(help.contains("winter-workspace service up"), "{help}");
    }

    #[test]
    fn an_unknown_command_of_a_group_is_a_usage_error_under_that_group() {
        let (command_path, fault) = fault_of(parsed(ENTRIES, &["service", "nope"]));

        assert_eq!(command_path, ["service"]);
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "nope"),
            "{fault:?}"
        );
    }

    #[test]
    fn commands_and_long_options_are_not_matched_by_prefix() {
        let (_, fault) = fault_of(parsed(ENTRIES, &["serv", "up", "alpha"]));
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "serv"),
            "{fault:?}"
        );

        let (command_path, fault) =
            fault_of(parsed(ENTRIES, &["service", "up", "alpha", "--tai", "3"]));
        assert_eq!(command_path, ["service", "up"]);
        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == "--tai"),
            "{fault:?}"
        );
    }

    #[test]
    fn no_node_has_a_short_help_flag_or_a_help_command() {
        for args in [&["-h"][..], &["service", "-h"], &["service", "up", "-h"]] {
            let (_, fault) = fault_of(parsed(ENTRIES, args));
            assert!(
                matches!(&fault, ParseFault::UnknownOption { name } if name == "-h"),
                "{args:?}: {fault:?}"
            );
        }

        let (_, fault) = fault_of(parsed(ENTRIES, &["help"]));
        assert!(
            matches!(fault, ParseFault::UnknownCommand { .. }),
            "{fault:?}"
        );
        let (_, fault) = fault_of(parsed(ENTRIES, &["service", "help"]));
        assert!(
            matches!(fault, ParseFault::UnknownCommand { .. }),
            "{fault:?}"
        );
    }

    #[test]
    fn the_version_flag_belongs_to_the_root_alone() {
        let (command_path, fault) = fault_of(parsed(ENTRIES, &["service", "--version"]));

        assert_eq!(command_path, ["service"]);
        assert!(
            matches!(fault, ParseFault::UnknownOption { .. }),
            "{fault:?}"
        );
    }

    #[test]
    fn an_option_without_its_value_is_a_missing_option_value() {
        let (_, fault) = fault_of(parsed(ENTRIES, &["service", "up", "alpha", "--tail"]));

        assert!(
            matches!(&fault, ParseFault::MissingOptionValue { option } if option.contains("--tail")),
            "{fault:?}"
        );
    }

    #[test]
    fn a_value_of_the_wrong_type_is_an_invalid_value() {
        let (_, fault) = fault_of(parsed(
            ENTRIES,
            &["service", "up", "alpha", "--tail", "many"],
        ));

        assert!(
            matches!(
                &fault,
                ParseFault::InvalidValue { param, value, .. }
                    if param.contains("--tail") && value == "many"
            ),
            "{fault:?}"
        );
    }

    #[test]
    fn a_value_outside_the_choices_names_the_choices() {
        let (_, fault) = fault_of(parsed(
            ENTRIES,
            &["service", "up", "alpha", "--mode", "loud"],
        ));

        assert!(
            matches!(
                &fault,
                ParseFault::InvalidValue { value, expected, .. }
                    if value == "loud" && expected == "one of attached, detached"
            ),
            "{fault:?}"
        );
    }

    #[test]
    fn a_word_too_many_for_a_command_is_an_unexpected_argument() {
        let (command_path, fault) = fault_of(parsed(ENTRIES, &["agents", "extra"]));

        assert_eq!(command_path, ["agents"]);
        assert!(
            matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &["extra"]),
            "{fault:?}"
        );
    }

    #[test]
    fn the_resolved_path_steps_over_option_values_and_stops_at_the_deciding_token() {
        let tree = with_help(
            clap::Command::new("tool")
                .arg(Arg::new("provider").long("provider").short('p'))
                .arg(
                    Arg::new("verbose")
                        .long("verbose")
                        .action(ArgAction::SetTrue),
                )
                .subcommand(with_help(
                    clap::Command::new("ws").subcommand(clap::Command::new("index")),
                )),
        );
        let path = |args: &[&str]| {
            let args: Vec<OsString> = args.iter().map(OsString::from).collect();
            resolved(&tree, &args).path
        };

        assert_eq!(path(&["ws", "index", "ws"]), ["ws", "index"]);
        assert_eq!(path(&["--provider", "ws", "ws"]), ["ws"]);
        assert_eq!(path(&["-p", "ws", "ws"]), ["ws"]);
        assert_eq!(path(&["--provider=x", "ws"]), ["ws"]);
        assert_eq!(path(&["-px", "ws"]), ["ws"]);
        assert_eq!(path(&["--verbose", "ws", "nope", "index"]), ["ws"]);
        assert_eq!(path(&["--verbose", "ws", "--help", "index"]), ["ws"]);
        assert!(path(&["--help", "ws"]).is_empty());
        assert!(path(&["--nope", "ws"]).is_empty());
        assert!(path(&["--", "ws"]).is_empty());
    }

    #[test]
    fn the_tokens_after_the_last_command_name_are_kept_apart() {
        let tree = clap::Command::new("tool")
            .arg(Arg::new("provider").long("provider"))
            .subcommand(clap::Command::new("ws").subcommand(clap::Command::new("index")));
        let args: Vec<OsString> = ["--provider", "x", "ws", "index", "one", "--json"]
            .iter()
            .map(OsString::from)
            .collect();

        let resolved = resolved(&tree, &args);

        assert_eq!(resolved.path, ["ws", "index"]);
        assert_eq!(resolved.rest, &args[4..]);
    }

    #[test]
    fn an_alias_that_names_no_sibling_fails_the_check_naming_its_file() {
        static STOP: Registration = Registration::Alias { of: "down" };
        let entries = [
            Entry {
                path: &["service", "stop"],
                file: "crates/core/src/commands/service/stop.rs",
                registration: &STOP,
            },
            // A command of that name in another group is not a sibling.
            Entry {
                path: &["down"],
                file: "crates/core/src/commands/down.rs",
                registration: &AGENTS,
            },
        ];

        let error = check(&entries).expect_err("the alias names no sibling");

        assert!(
            error.starts_with("crates/core/src/commands/service/stop.rs"),
            "{error}"
        );
    }

    #[test]
    fn an_alias_of_an_alias_fails_the_check_naming_its_file() {
        static BOOT: Registration = Registration::Alias { of: "start" };
        let entries = [
            Entry {
                path: &["service", "boot"],
                file: "crates/core/src/commands/service/boot.rs",
                registration: &BOOT,
            },
            Entry {
                path: &["service", "start"],
                file: "crates/core/src/commands/service/start.rs",
                registration: &START,
            },
            Entry {
                path: &["service", "up"],
                file: "crates/core/src/commands/service/up.rs",
                registration: &UP,
            },
        ];

        let error = check(&entries).expect_err("the alias names an alias");

        assert!(
            error.starts_with("crates/core/src/commands/service/boot.rs"),
            "{error}"
        );
    }

    #[test]
    fn a_top_level_command_named_as_a_group_fails_the_check_naming_its_file() {
        let entries = [Entry {
            path: &["ws"],
            file: "crates/core/src/commands/ws.rs",
            registration: &AGENTS,
        }];

        let error = check(&entries).expect_err("the name is a group's");

        assert!(
            error.starts_with("crates/core/src/commands/ws.rs"),
            "{error}"
        );
    }

    #[test]
    fn an_entry_outside_the_tree_fails_the_check_naming_its_file() {
        let entries = [Entry {
            path: &["misc", "index"],
            file: "crates/core/src/commands/misc/index.rs",
            registration: &AGENTS,
        }];

        let error = check(&entries).expect_err("the directory is no group");

        assert!(
            error.starts_with("crates/core/src/commands/misc/index.rs"),
            "{error}"
        );
    }

    // The parser's own assertions exist in builds with debug assertions only.
    #[cfg(debug_assertions)]
    #[test]
    fn a_node_the_parser_refuses_fails_the_check_naming_its_file() {
        fn twice(command: clap::Command) -> clap::Command {
            command
                .arg(Arg::new("first").long("same"))
                .arg(Arg::new("second").long("same"))
        }
        static TWICE: Registration = Registration::Command {
            args: twice,
            run: agents_run,
            hidden: false,
        };
        let entries = [Entry {
            path: &["ws", "twice"],
            file: "crates/core/src/commands/ws/twice.rs",
            registration: &TWICE,
        }];

        let error = check(&entries).expect_err("two options share a name");

        assert!(
            error.starts_with("crates/core/src/commands/ws/twice.rs"),
            "{error}"
        );
    }

    #[test]
    fn the_test_entries_and_the_registry_pass_the_check() {
        assert_eq!(check(ENTRIES), Ok(()));
        assert_eq!(check(REGISTRY), Ok(()));
    }
}
