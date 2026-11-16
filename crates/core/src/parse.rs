//! The argument parser, built from a slice of registry entries, and what it answers for a command line.
//!
//! The parser is pure: it takes no handle, writes nothing, and calls no command. `process.rs` acts on its
//! answer.

use std::error::Error;
use std::ffi::{OsStr, OsString};

use clap::error::{ContextKind, ContextValue, ErrorKind};
use clap::{Arg, ArgAction, ArgMatches};

use crate::context::{Context, Invoked, RootOptions};
use crate::groups;
use crate::logging::Log;
use crate::process::{Identity, ProcessEnv};
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
/// The deciding token is the first argument that is not UTF-8, when the line holds one. Otherwise it is the
/// first of `--help`, `--version`, or a token at fault. The command path is the command names resolved among
/// the tokens before it, and every name resolved when no token decides before the end of the line: a dispatch,
/// a bare group, a missing required argument.
#[derive(Debug)]
pub(crate) enum Stage {
    /// No command name was resolved.
    Root,
    /// At least one was. `invoked` holds the command path and the usage of its last node, and `root` the root
    /// options given before the first command name, whether or not the tokens after it parsed.
    Command { invoked: Invoked, root: RootArgs },
}

/// The root's own options as typed, before the environment is consulted.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct RootArgs {
    pub verbose: bool,
    pub service_orchestrator: Option<String>,
    pub source_override: Option<String>,
}

/// The one place the root's arguments and the environment become values.
///
/// The provider override is the root option when its value is not empty, else the variable when it is set and
/// not empty, with its bytes as given. The source override is the option's value as given.
pub(crate) fn root_values(args: &RootArgs, env: &ProcessEnv) -> (RootOptions, Log) {
    let typed = args
        .service_orchestrator
        .as_deref()
        .filter(|value| !value.is_empty())
        .map(OsString::from);
    let service_orchestrator = typed.or_else(|| {
        env.get(SERVICE_ORCHESTRATOR_VARIABLE)
            .filter(|value| !value.is_empty())
            .map(OsStr::to_os_string)
    });
    let options = RootOptions {
        service_orchestrator,
        source_override: args.source_override.clone(),
    };
    (
        options,
        Log::resolve(args.verbose, env.get(LOG_LEVEL_VARIABLE)),
    )
}

const SERVICE_ORCHESTRATOR_VARIABLE: &str = "WINTER_SERVICE_ORCHESTRATOR";
const LOG_LEVEL_VARIABLE: &str = "WINTER_LOG_LEVEL";

const VERBOSE: &str = "verbose";
const SERVICE_ORCHESTRATOR: &str = "service-orchestrator";
const SOURCE_OVERRIDE: &str = "source-override";

/// What the command line asks for.
#[derive(Debug)]
pub(crate) enum Outcome {
    /// The rendered help of a node, asked for with `--help`.
    Help(String),
    /// The version line, without its newline.
    Version(String),
    /// A usage error or a bare group; or, at [`Stage::Root`], an internal failure when the parser cannot be
    /// built from the registry, or when root options it accepted do not read on their own.
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

/// The root options before a command name, which the parser accepted, do not parse on their own.
#[derive(Debug, thiserror::Error)]
#[error("the root options before the command '{command}' do not parse alone: {source}")]
struct RootOptionsUnread {
    command: String,
    source: clap::Error,
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
        // An argument that is not UTF-8 is refused before any argument is interpreted.
        if let Some(at) = args.iter().position(|arg| arg.to_str().is_none()) {
            return self.not_utf8(args, at);
        }
        // A token the walk itself refuses decides the line where it stands. The parser is not asked: it holds
        // back a repeat whose value is a token of its own until the command after it has parsed, and drops it
        // when a help or a fault ends that parse; and it reports the word after a `--` as what it refused.
        let resolved = resolved(&self.command, args);
        match resolved.refused.clone() {
            Some(Refused::Repeated { option }) => {
                return self.ended_at(resolved, |_, _| ParseFault::RepeatedOption { option });
            }
            Some(Refused::DoubleDash) => {
                return self.ended_at(resolved, |_, _| ParseFault::UnexpectedArgument {
                    values: vec!["--".to_owned()],
                });
            }
            None => {}
        }
        match self.command.try_get_matches_from_mut(args) {
            Ok(matches) => self.accepted(matches),
            Err(error) => self.stopped(&error, resolved),
        }
    }

    /// The answer for a command line whose argument at `at` is the first that is not UTF-8: a usage error of
    /// the deepest node the tokens before it resolved.
    fn not_utf8(self, args: &[OsString], at: usize) -> Parsed {
        let (before, refused) = args.split_at(at);
        let argument = refused
            .first()
            .map(|arg| arg.to_string_lossy().into_owned())
            .unwrap_or_default();
        let resolved = resolved(&self.command, before);
        self.ended_at(resolved, |_, _| ParseFault::NotUtf8 { argument })
    }

    /// The answer for a command line the parser accepted: the command it selects, or a bare group.
    fn accepted(mut self, matches: ArgMatches) -> Parsed {
        let root = root_args(&matches);
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
            stage: stage(path, usage, root),
            outcome,
        }
    }

    /// The answer for a command line the parser stopped at: the help or version a flag asked for, or a usage
    /// error.
    fn stopped(self, error: &clap::Error, resolved: Resolved<'_>) -> Parsed {
        match error.kind() {
            ErrorKind::DisplayHelp => {
                self.answered_at(resolved, |_| Outcome::Help(error.render().to_string()))
            }
            ErrorKind::DisplayVersion => {
                let line = format!("{} {}", self.program, self.version);
                self.answered_at(resolved, |_| Outcome::Version(line))
            }
            _ => self.ended_at(resolved, |node, words| fault(error, node, words)),
        }
    }

    /// A usage error of the node `resolved` ends at. `fault` is handed that node and, when it is a command,
    /// the words of the line the command has no positional argument for.
    fn ended_at(
        self,
        resolved: Resolved<'_>,
        fault: impl FnOnce(&clap::Command, Option<Vec<String>>) -> ParseFault,
    ) -> Parsed {
        let words = self
            .route(&resolved.path)
            .map(|_| surplus(node_at(&self.command, &resolved.path), resolved.rest));
        let fault = fault(node_at(&self.command, &resolved.path), words);
        let command_path = resolved.path.clone();
        self.answered_at(resolved, |usage| {
            Outcome::Ended(Termination::Usage(Box::new(UsageError::Parse {
                command_path,
                usage: usage.clone(),
                fault,
            })))
        })
    }

    /// The answer `outcome` gives, at the stage `resolved` reached. `outcome` is handed the usage of the node
    /// the path ends at.
    ///
    /// The parser left no matches, so the root options before the first command name are read again, against
    /// the root's option definitions alone. The parser accepted them before it reached that name; a second
    /// reading that refuses them is an internal failure.
    fn answered_at(
        mut self,
        resolved: Resolved<'_>,
        outcome: impl FnOnce(&Usage) -> Outcome,
    ) -> Parsed {
        let Resolved { path, before, .. } = resolved;
        let root = match path.first() {
            None => RootArgs::default(),
            Some(command) => {
                let options = root_options(
                    clap::Command::new(self.program)
                        .no_binary_name(true)
                        .disable_help_flag(true),
                );
                match options.try_get_matches_from(before) {
                    Ok(matches) => root_args(&matches),
                    Err(source) => {
                        let error = RootOptionsUnread {
                            command: command.clone(),
                            source,
                        };
                        return Parsed {
                            stage: Stage::Root,
                            outcome: Outcome::Ended(Termination::Internal(Box::new(
                                InternalError::unexpected(error),
                            ))),
                        };
                    }
                }
            }
        };
        let usage = self.usage(&path);
        let outcome = outcome(&usage);
        Parsed {
            stage: stage(path, usage, root),
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

/// The stage a command line reached with `path` resolved; `usage` is that of the node at `path`, and `root`
/// the root options given before the first name of `path`.
fn stage(path: Vec<String>, usage: Usage, root: RootArgs) -> Stage {
    if path.is_empty() {
        Stage::Root
    } else {
        Stage::Command {
            invoked: Invoked { path, usage },
            root,
        }
    }
}

/// The root's options as the matches of the root hold them.
fn root_args(matches: &ArgMatches) -> RootArgs {
    RootArgs {
        verbose: matches.get_flag(VERBOSE),
        service_orchestrator: matches.get_one::<String>(SERVICE_ORCHESTRATOR).cloned(),
        source_override: matches.get_one::<String>(SOURCE_OVERRIDE).cloned(),
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

/// The line the root help ends with, after the options. The help of a group and of a command has no such
/// line.
const TRACING_NOTICE: &str = "WINTER_OTEL_EXPORTER_OTLP_ENDPOINT makes a command send a trace span; README.md says what is sent.";

fn root(identity: &Identity) -> clap::Command {
    root_options(
        with_help(
            clap::Command::new(identity.program)
                .about("Manage a Winter workspace")
                .bin_name(identity.program)
                .after_help(TRACING_NOTICE)
                .version(identity.version)
                .no_binary_name(true)
                .disable_version_flag(true),
        )
        .arg(
            Arg::new("version")
                .long("version")
                .action(ArgAction::Version)
                .help("Print the version"),
        ),
    )
}

/// Gives a node the root's own options. They belong to the root alone, so they are read only before the first
/// command name; after it they are options of the node they follow.
fn root_options(node: clap::Command) -> clap::Command {
    node.arg(
        Arg::new(VERBOSE)
            .short('v')
            .long("verbose")
            .action(ArgAction::SetTrue)
            .help("Log at debug level on stderr; wins over WINTER_LOG_LEVEL"),
    )
    .arg(
        Arg::new(SERVICE_ORCHESTRATOR)
            .long("service-orchestrator")
            .value_name("PATH_OR_NAME")
            .allow_hyphen_values(true)
            .help("Use this service provider for this invocation"),
    )
    .arg(
        Arg::new(SOURCE_OVERRIDE)
            .long("source-override")
            .value_name("TEXT")
            .allow_hyphen_values(true)
            .hide(true),
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

/// The command names a command line resolved before its deciding token, with the tokens before the first of
/// them and the tokens after the last, and what the walk itself refused the deciding token as.
struct Resolved<'a> {
    path: Vec<String>,
    before: &'a [OsString],
    rest: &'a [OsString],
    refused: Option<Refused>,
}

/// A deciding token the walk refuses on its own account, at a node that takes commands.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Refused {
    /// An option that may not repeat, given a second time at one node; `option` is the name it is typed as.
    Repeated { option: String },
    /// A `--` with a token after it: a node that takes commands has no positional argument, so nothing may
    /// follow the `--` there.
    DoubleDash,
}

/// Reads the command path of a command line, up to the token that decides it.
///
/// The walk ends at the first `--help` or `--version`, at the first token that names neither an option nor a
/// command of the node it stands at, at a flag given a value, at an option given a second time that may not
/// repeat, at an option whose value is missing, at a `--`, and at a node that has no commands. An option's
/// value is stepped over, so that a value spelled like a command does not extend the path.
///
/// Two of these endings are answered as `refused`, and decide the line without the parser: the repeat, which
/// the parser reports only on some lines, and the `--` that a token follows. Every other ending is where the
/// parser decides too, and its answer is taken. The tokens of a node that has no commands are not walked: a
/// repeat among a command's own options is the parser's to report.
///
/// An option that takes more than one value is read as taking one.
fn resolved<'a>(root: &clap::Command, args: &'a [OsString]) -> Resolved<'a> {
    let mut path = Vec::new();
    let mut node = root;
    let mut before = args;
    let mut rest = args;
    let mut refused = None;
    let mut given: Vec<&clap::Id> = Vec::new();
    let mut next = 0;
    'walk: while node.has_subcommands()
        && let Some(token) = args.get(next).map(|arg| arg.to_string_lossy())
    {
        next += 1;
        if token == "--" {
            if next < args.len() {
                refused = Some(Refused::DoubleDash);
            }
            break;
        }
        if token.starts_with('-') {
            let OptionToken::Options { named, value_from } = option_token(node, &token) else {
                break;
            };
            for option in named {
                if given.contains(&option.get_id()) && !may_repeat(option) {
                    refused = Some(Refused::Repeated {
                        option: option_name(option).unwrap_or_else(|| token.to_string()),
                    });
                    break 'walk;
                }
                given.push(option.get_id());
            }
            if let Some(option) = value_from {
                match args.get(next) {
                    Some(value) if is_value_of(option, &value.to_string_lossy()) => next += 1,
                    _ => break,
                }
            }
            continue;
        }
        let Some(below) = node.find_subcommand(token.as_ref()) else {
            break;
        };
        if path.is_empty() {
            before = args.get(..next - 1).unwrap_or_default();
        }
        path.push(below.get_name().to_owned());
        node = below;
        given.clear();
        rest = args.get(next..).unwrap_or_default();
    }
    Resolved {
        path,
        before,
        rest,
        refused,
    }
}

/// The words of `tokens` that `leaf` has no positional argument for, in the order given.
///
/// `tokens` are those that follow the leaf's name. An option and its value are no words; after `--` every
/// token is one, and so is a lone `-`. The answer is empty when a token the leaf has no option for, or a flag
/// given a value, stands before the first such word: that token is then the fault of the line.
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
    let mut tokens = tokens
        .iter()
        .map(|token| token.to_string_lossy())
        .peekable();
    while let Some(token) = tokens.next() {
        if !options_ended && token == "--" {
            options_ended = true;
            continue;
        }
        if !options_ended && token.starts_with('-') && token != "-" {
            match option_token(leaf, &token) {
                OptionToken::Options {
                    value_from: Some(option),
                    ..
                } => {
                    tokens.next_if(|value| is_value_of(option, value));
                }
                OptionToken::Options { .. } | OptionToken::Decides => {}
                OptionToken::Fault if surplus.is_empty() => return surplus,
                OptionToken::Fault => {}
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

/// What a token spelled like an option is to the node it stands at.
enum OptionToken<'c> {
    /// It holds a name the node has no option for, or gives a value to an option that takes none.
    Fault,
    /// It holds `--help` or `--version`.
    Decides,
    /// The options it names, in the order given: one long option, or the letters of a short cluster.
    /// `value_from` is the last of them when it takes a value and the token carries none, so that the value
    /// is the token after it.
    Options {
        named: Vec<&'c Arg>,
        value_from: Option<&'c Arg>,
    },
}

/// Reads `token`, which starts with `-`, against the options of `node`.
///
/// A long name ends at the first `=`, and what follows is its value. In a short cluster every letter is a
/// flag but the last option named: when that one takes a value, the rest of the token is the value.
fn option_token<'c>(node: &'c clap::Command, token: &str) -> OptionToken<'c> {
    let decides = |arg: &Arg| matches!(arg.get_action(), ArgAction::Help | ArgAction::Version);
    if let Some(long) = token.strip_prefix("--") {
        let (name, attached) = match long.split_once('=') {
            Some((name, _)) => (name, true),
            None => (long, false),
        };
        let Some(option) = node
            .get_arguments()
            .find(|arg| arg.get_long() == Some(name))
        else {
            return OptionToken::Fault;
        };
        let takes_value = option.get_action().takes_values();
        return match (attached, takes_value) {
            (true, false) => OptionToken::Fault,
            _ if decides(option) => OptionToken::Decides,
            _ => OptionToken::Options {
                named: vec![option],
                value_from: (takes_value && !attached).then_some(option),
            },
        };
    }
    let mut named = Vec::new();
    let mut letters = token.chars().skip(1);
    while let Some(letter) = letters.next() {
        let Some(option) = node
            .get_arguments()
            .find(|arg| arg.get_short() == Some(letter))
        else {
            return OptionToken::Fault;
        };
        if decides(option) {
            return OptionToken::Decides;
        }
        named.push(option);
        if option.get_action().takes_values() {
            let value_from = letters.next().is_none().then_some(option);
            return OptionToken::Options { named, value_from };
        }
    }
    if named.is_empty() {
        return OptionToken::Fault;
    }
    OptionToken::Options {
        named,
        value_from: None,
    }
}

/// Whether `option` may be given more than once.
fn may_repeat(option: &Arg) -> bool {
    matches!(option.get_action(), ArgAction::Append | ArgAction::Count)
}

/// Whether `token`, which follows `option` on the line, is its value. An option declared to accept a value
/// that starts with `-` takes the token whatever it looks like; any other takes a lone `-` and a token that
/// does not start with one.
fn is_value_of(option: &Arg, token: &str) -> bool {
    option.is_allow_hyphen_values_set() || !token.starts_with('-') || token == "-"
}

/// Classes what the parser refused.
///
/// `surplus` is `Some` when the fault happened in a command's own arguments, where a stray word is an argument
/// too many, and holds every such word of the line; it is `None` at the root or a group, where a stray word
/// names no command. A refused token spelled like an option is an argument too many where it is the first of
/// those words, as after `--`, and an unknown option otherwise. `node` is the node at fault: an option of it
/// that was given twice is named as the node declares it.
fn fault(error: &clap::Error, node: &clap::Command, surplus: Option<Vec<String>>) -> ParseFault {
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
        // The parser reports an option given twice as one in conflict with itself.
        ErrorKind::ArgumentConflict
            if text(ContextKind::PriorArg) == text(ContextKind::InvalidArg) =>
        {
            let shown = text(ContextKind::InvalidArg);
            let option = node
                .get_arguments()
                .find(|arg| arg.to_string() == shown)
                .and_then(option_name)
                .unwrap_or(shown);
            ParseFault::RepeatedOption { option }
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

/// The name `option` is typed as: its long name, or its short letter when it has no long name.
fn option_name(option: &Arg) -> Option<String> {
    match (option.get_long(), option.get_short()) {
        (Some(long), _) => Some(format!("--{long}")),
        (None, Some(short)) => Some(format!("-{short}")),
        (None, None) => None,
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::ffi::OsStringExt;
    use std::panic::{AssertUnwindSafe, catch_unwind};

    use super::*;
    use crate::logging::Level;
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
            Stage::Command { invoked, .. } => invoked.path.clone(),
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
                if let Stage::Command { invoked, .. } = &parsed.stage {
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

    fn kind_of(outcome: &Outcome) -> Kind {
        match outcome {
            Outcome::Dispatch { .. } => Kind::Dispatch,
            Outcome::Help(_) => Kind::Help,
            Outcome::Version(_) => Kind::Version,
            Outcome::Ended(_) => Kind::Ended,
        }
    }

    #[test]
    fn each_command_line_reaches_its_stage_with_its_command_path_and_its_outcome() {
        let rows: [(&[&str], &[&str], Kind); 13] = [
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
            (&["-v", "-v", "ws", "index", "NAME"], &[], Kind::Ended),
            (&[], &[], Kind::Ended),
        ];

        for (args, path, kind) in rows {
            let parsed = parsed(INDEX_ENTRIES, args);

            match &parsed.stage {
                Stage::Root => assert!(path.is_empty(), "{args:?}: the root stage"),
                Stage::Command { invoked, .. } => {
                    assert_eq!(invoked.path, path, "{args:?}");
                    let help_command = format!("winter-workspace {} --help", path.join(" "));
                    assert_eq!(invoked.usage.help_command, help_command, "{args:?}");
                }
            }
            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            assert_eq!(
                kind_of(&parsed.outcome),
                kind,
                "{args:?}: {:?}",
                parsed.outcome
            );
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
    fn the_root_help_lists_exactly_the_groups_and_top_level_commands_of_the_registry() {
        let help = help_of(REGISTRY, &["--help"]);
        let listed: Vec<&str> = help
            .lines()
            .skip_while(|line| *line != "Commands:")
            .skip(1)
            .take_while(|line| !line.is_empty())
            .filter_map(|line| line.split_whitespace().next())
            .collect();

        let mut held: Vec<&str> = REGISTRY
            .iter()
            .filter(|entry| {
                !matches!(
                    entry.registration,
                    Registration::Command { hidden: true, .. }
                ) || entry.path.len() > 1
            })
            .filter_map(|entry| entry.path.first().copied())
            .collect();
        held.sort_unstable();
        held.dedup();
        assert_eq!(listed, held, "{help}");
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

    fn parsed_os(entries: &[Entry], args: &[&[u8]]) -> Parsed {
        let args: Vec<OsString> = args
            .iter()
            .map(|arg| OsString::from_vec(arg.to_vec()))
            .collect();
        parse(entries, &IDENTITY, &args)
    }

    /// The root options of a command line that reached a command.
    fn root_of(parsed: &Parsed) -> &RootArgs {
        match &parsed.stage {
            Stage::Command { root, .. } => root,
            Stage::Root => panic!("expected the command stage, got {:?}", parsed.outcome),
        }
    }

    /// What `root_values` answers for `args`, parsed with the parser the registry builds, under `env`.
    fn values(args: &[&str], env: &[(&str, &[u8])]) -> (RootOptions, Log) {
        let parsed = parsed(REGISTRY, args);
        let env = ProcessEnv::from_vars(
            env.iter()
                .map(|(key, value)| (OsString::from(key), OsString::from_vec(value.to_vec()))),
        );
        root_values(root_of(&parsed), &env)
    }

    /// Every option of the tree that `is` holds for, each as the command and the option; the root is named by
    /// the program.
    fn options_where(entries: &[Entry], is: fn(&Arg) -> bool) -> Vec<String> {
        fn collect(node: &clap::Command, name: &str, is: fn(&Arg) -> bool, held: &mut Vec<String>) {
            for option in node.get_arguments() {
                if !option.is_positional() && is(option) {
                    let option = option_name(option).unwrap_or_default();
                    held.push(format!("{name}: {option}"));
                }
            }
            for below in node.get_subcommands() {
                let name = if name == IDENTITY.program {
                    below.get_name().to_owned()
                } else {
                    format!("{name} {}", below.get_name())
                };
                collect(below, &name, is, held);
            }
        }
        let mut parser = Parser::build(entries, &IDENTITY).expect("a tree");
        // Building the tree fills in what the parser derives, the number of values among it.
        parser.command.build();
        let mut held = Vec::new();
        collect(&parser.command, IDENTITY.program, is, &mut held);
        held
    }

    /// Every option that takes a value and refuses one that starts with `-`.
    fn options_refusing_a_hyphen_value(entries: &[Entry]) -> Vec<String> {
        options_where(entries, |option| {
            option.get_action().takes_values() && !option.is_allow_hyphen_values_set()
        })
    }

    /// Every option declared to take more than one value at one giving.
    fn options_taking_several_values(entries: &[Entry]) -> Vec<String> {
        options_where(entries, |option| {
            option
                .get_num_args()
                .is_some_and(|values| values.max_values() > 1)
        })
    }

    /// Every positional of the tree declared to accept a value that starts with `-`, named as above.
    fn positionals_accepting_a_hyphen_value(entries: &[Entry]) -> Vec<String> {
        fn collect(node: &clap::Command, name: &str, accepting: &mut Vec<String>) {
            for positional in node.get_arguments().filter(|arg| arg.is_positional()) {
                if positional.is_allow_hyphen_values_set() {
                    accepting.push(format!("{name}: {}", positional.get_id()));
                }
            }
            for below in node.get_subcommands() {
                collect(below, &format!("{name} {}", below.get_name()), accepting);
            }
        }
        let parser = Parser::build(entries, &IDENTITY).expect("a tree");
        let mut accepting = Vec::new();
        collect(&parser.command, IDENTITY.program, &mut accepting);
        accepting
    }

    #[test]
    fn every_value_taking_option_accepts_a_value_that_starts_with_a_hyphen() {
        assert_eq!(
            options_refusing_a_hyphen_value(REGISTRY),
            Vec::<String>::new(),
            "declare each with allow_hyphen_values(true)"
        );
    }

    #[test]
    fn an_option_that_refuses_a_hyphen_value_is_named_with_its_command() {
        assert_eq!(
            options_refusing_a_hyphen_value(ENTRIES),
            [
                "service start: --tail",
                "service start: --mode",
                "service up: --tail",
                "service up: --mode"
            ]
        );
        assert_eq!(
            options_refusing_a_hyphen_value(INDEX_ENTRIES),
            ["ws index: --base"]
        );
    }

    #[test]
    fn no_option_takes_more_than_one_value_at_one_giving() {
        assert_eq!(
            options_taking_several_values(REGISTRY),
            Vec::<String>::new(),
            "the walk of the command path reads an option as taking one value"
        );

        fn span_args(command: clap::Command) -> clap::Command {
            command
                .arg(Arg::new("between").long("between").num_args(2))
                .arg(Arg::new("range").long("range").value_names(["FROM", "TO"]))
        }
        static SPAN: Registration = Registration::Command {
            args: span_args,
            run: agents_run,
            hidden: false,
        };
        static SPAN_ENTRIES: &[Entry] = &[Entry {
            path: &["ws", "span"],
            file: "crates/core/src/commands/ws/span.rs",
            registration: &SPAN,
        }];
        assert_eq!(
            options_taking_several_values(SPAN_ENTRIES),
            ["ws span: --between", "ws span: --range"]
        );
        assert_eq!(options_taking_several_values(ENTRIES), Vec::<String>::new());
    }

    #[test]
    fn no_positional_accepts_a_value_that_starts_with_a_hyphen() {
        assert_eq!(
            positionals_accepting_a_hyphen_value(REGISTRY),
            Vec::<String>::new()
        );

        let (command_path, fault) = fault_of(parsed(REGISTRY, &["ws", "index", "--bogus"]));
        assert_eq!(command_path, ["ws", "index"]);
        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == "--bogus"),
            "{fault:?}"
        );
    }

    #[test]
    fn the_verbose_flag_gives_debug_logging_and_wins_over_the_variable() {
        let verbose = Log {
            threshold: Level::Debug,
            switched: true,
        };
        for flag in ["-v", "--verbose"] {
            let args = [flag, "ws", "index", "NAME"];
            assert_eq!(values(&args, &[]).1, verbose, "{flag}");
            assert_eq!(
                values(&args, &[("WINTER_LOG_LEVEL", b"ERROR")]).1,
                verbose,
                "{flag}"
            );
        }
    }

    #[test]
    fn without_the_verbose_flag_the_variable_decides_the_logging() {
        let args = ["ws", "index", "NAME"];

        assert_eq!(values(&args, &[]).1, Log::DEFAULT);
        assert_eq!(
            values(&args, &[("WINTER_LOG_LEVEL", b"info")]).1,
            Log {
                threshold: Level::Info,
                switched: true
            }
        );
    }

    #[test]
    fn the_orchestrator_option_gives_the_override() {
        let (options, _) = values(&["--service-orchestrator", "X", "ws", "index", "NAME"], &[]);
        assert_eq!(options.service_orchestrator, Some(OsString::from("X")));

        let (options, _) = values(
            &["--service-orchestrator=X", "ws", "index", "NAME"],
            &[("WINTER_SERVICE_ORCHESTRATOR", b"Y")],
        );
        assert_eq!(options.service_orchestrator, Some(OsString::from("X")));
    }

    #[test]
    fn an_empty_orchestrator_option_falls_back_to_the_variable() {
        let (options, _) = values(
            &["--service-orchestrator", "", "ws", "index", "NAME"],
            &[("WINTER_SERVICE_ORCHESTRATOR", b"Y")],
        );

        assert_eq!(options.service_orchestrator, Some(OsString::from("Y")));
    }

    #[test]
    fn no_orchestrator_option_and_an_empty_or_absent_variable_give_no_override() {
        let args = ["ws", "index", "NAME"];

        assert_eq!(
            values(&args, &[("WINTER_SERVICE_ORCHESTRATOR", b"")])
                .0
                .service_orchestrator,
            None
        );
        assert_eq!(values(&args, &[]).0.service_orchestrator, None);
        let empty = ["--service-orchestrator", "", "ws", "index", "NAME"];
        assert_eq!(
            values(&empty, &[("WINTER_SERVICE_ORCHESTRATOR", b"")])
                .0
                .service_orchestrator,
            None
        );
    }

    #[test]
    fn an_orchestrator_variable_that_is_not_utf_8_is_kept_as_its_bytes() {
        let (options, _) = values(
            &["ws", "index", "NAME"],
            &[("WINTER_SERVICE_ORCHESTRATOR", b"pro\xffvider")],
        );

        assert_eq!(
            options.service_orchestrator,
            Some(OsString::from_vec(b"pro\xffvider".to_vec()))
        );
    }

    #[test]
    fn an_orchestrator_value_may_start_with_a_hyphen() {
        for (args, value) in [
            (
                &["--service-orchestrator", "-x", "ws", "index", "NAME"][..],
                "-x",
            ),
            (
                &["--service-orchestrator", "--help", "ws", "index", "NAME"],
                "--help",
            ),
            (
                &["--service-orchestrator", "--version", "ws", "index", "NAME"],
                "--version",
            ),
            (
                &["--service-orchestrator", "--", "ws", "index", "NAME"],
                "--",
            ),
            (
                &["--service-orchestrator", "-v", "ws", "index", "NAME"],
                "-v",
            ),
            (&["--service-orchestrator=-x", "ws", "index", "NAME"], "-x"),
        ] {
            let parsed = parsed(REGISTRY, args);
            assert!(
                matches!(parsed.outcome, Outcome::Dispatch { .. }),
                "{args:?}: {:?}",
                parsed.outcome
            );
            assert_eq!(path_of(&parsed.stage), ["ws", "index"], "{args:?}");
            assert_eq!(
                root_of(&parsed).service_orchestrator.as_deref(),
                Some(value),
                "{args:?}"
            );
            assert!(!root_of(&parsed).verbose, "{args:?}");
        }
    }

    #[test]
    fn the_source_override_option_gives_its_value_as_given() {
        let (options, _) = values(&["--source-override", "T", "ws", "index", "NAME"], &[]);
        assert_eq!(options.source_override.as_deref(), Some("T"));

        let (options, _) = values(&["--source-override", "", "ws", "index", "NAME"], &[]);
        assert_eq!(options.source_override.as_deref(), Some(""));

        let (options, _) = values(&["--source-override", "-t", "ws", "index", "NAME"], &[]);
        assert_eq!(options.source_override.as_deref(), Some("-t"));

        let (options, _) = values(&["ws", "index", "NAME"], &[]);
        assert_eq!(options.source_override, None);
    }

    #[test]
    fn the_root_options_are_kept_when_the_tokens_after_the_command_name_do_not_parse() {
        let parsed = parsed(REGISTRY, &["-v", "ws", "index", "--bogus"]);

        assert_eq!(path_of(&parsed.stage), ["ws", "index"]);
        assert_eq!(
            root_of(&parsed),
            &RootArgs {
                verbose: true,
                service_orchestrator: None,
                source_override: None
            }
        );
        assert!(
            matches!(parsed.outcome, Outcome::Ended(Termination::Usage(_))),
            "{:?}",
            parsed.outcome
        );
    }

    #[test]
    fn the_root_options_are_kept_whatever_ends_the_line_after_a_command_name() {
        let all = RootArgs {
            verbose: true,
            service_orchestrator: Some("--help".to_owned()),
            source_override: Some("ws".to_owned()),
        };
        let given = [
            "--service-orchestrator",
            "--help",
            "-v",
            "--source-override=ws",
        ];
        for after in [
            &["ws", "index", "NAME"][..],
            &["ws", "index", "--bogus"],
            &["ws", "index"],
            &["ws", "index", "--help"],
            &["ws", "--help"],
            &["ws", "nope"],
            &["ws"],
            &["ws", "index", "-v", "NAME"],
        ] {
            let args: Vec<&str> = given.iter().chain(after).copied().collect();

            let parsed = parsed(REGISTRY, &args);

            assert_eq!(root_of(&parsed), &all, "{args:?}");
        }
    }

    #[test]
    fn a_repeated_root_option_ends_at_the_root_as_a_repeated_option() {
        for (args, option) in [
            (&["-v", "-v", "ws", "index", "NAME"][..], "--verbose"),
            (&["-vv", "ws", "index", "NAME"], "--verbose"),
            (&["-v", "--verbose", "ws", "index", "NAME"], "--verbose"),
            (&["-v", "-v", "ws", "index", "--bogus"], "--verbose"),
            (&["-v", "-v"], "--verbose"),
            (
                &[
                    "--service-orchestrator",
                    "a",
                    "--service-orchestrator",
                    "b",
                    "ws",
                    "index",
                    "NAME",
                ],
                "--service-orchestrator",
            ),
            (
                &[
                    "--service-orchestrator=a",
                    "--service-orchestrator=b",
                    "ws",
                    "index",
                ],
                "--service-orchestrator",
            ),
            (
                &[
                    "--source-override",
                    "a",
                    "--source-override",
                    "b",
                    "ws",
                    "index",
                    "NAME",
                ],
                "--source-override",
            ),
        ] {
            let parsed = parsed(REGISTRY, args);

            assert!(
                matches!(parsed.stage, Stage::Root),
                "{args:?}: {:?}",
                parsed.stage
            );
            let (command_path, usage, fault) = usage_of(parsed);
            assert!(command_path.is_empty(), "{args:?}");
            assert_eq!(usage.help_command, "winter-workspace --help", "{args:?}");
            assert!(
                matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn a_repeated_root_option_decides_before_a_help_or_a_fault_that_follows_it() {
        for option in ["--service-orchestrator", "--source-override"] {
            for tail in [
                &["ws", "index", "--help"][..],
                &["ws", "--help"],
                &["ws", "index", "--bogus"],
                &["ws", "nope"],
                &["--bogus"],
                &["nope"],
                &["--help"],
                &["--version"],
                &[],
            ] {
                let args: Vec<&str> = [option, "a", option, "b"]
                    .into_iter()
                    .chain(tail.iter().copied())
                    .collect();

                let parsed = parsed(REGISTRY, &args);

                assert!(
                    matches!(parsed.stage, Stage::Root),
                    "{args:?}: {:?}",
                    parsed.stage
                );
                let (command_path, usage, fault) = usage_of(parsed);
                assert!(command_path.is_empty(), "{args:?}");
                assert_eq!(usage.help_command, "winter-workspace --help", "{args:?}");
                assert!(
                    matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
                    "{args:?}: {fault:?}"
                );
            }

            // Given a second time without a value, it is the repeat that is refused, not the missing value.
            let (command_path, fault) = fault_of(parsed(REGISTRY, &[option, "a", option]));
            assert!(command_path.is_empty(), "{option}");
            assert!(
                matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
                "{option}: {fault:?}"
            );
        }
    }

    #[test]
    fn a_repeated_option_of_a_command_is_a_repeated_option_of_that_command() {
        for args in [
            &["ws", "index", "NAME", "--json", "--json"][..],
            &["ws", "index", "--json", "NAME", "--json"],
            &["ws", "index", "--json", "--json"],
            &["-v", "ws", "index", "--json", "--json", "one", "two"],
        ] {
            let (command_path, fault) = fault_of(parsed(REGISTRY, args));

            assert_eq!(command_path, ["ws", "index"], "{args:?}");
            assert!(
                matches!(&fault, ParseFault::RepeatedOption { option } if option == "--json"),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn a_root_option_after_a_command_name_is_an_unknown_option_of_that_node() {
        for (args, path, name) in [
            (
                &["ws", "index", "-v", "NAME"][..],
                &["ws", "index"][..],
                "-v",
            ),
            (
                &["ws", "index", "NAME", "--verbose"],
                &["ws", "index"],
                "--verbose",
            ),
            (
                &["ws", "index", "NAME", "--service-orchestrator", "X"],
                &["ws", "index"],
                "--service-orchestrator",
            ),
            (&["ws", "-v", "index", "NAME"], &["ws"], "-v"),
            (
                &["ws", "--source-override", "T", "index", "NAME"],
                &["ws"],
                "--source-override",
            ),
        ] {
            let (command_path, fault) = fault_of(parsed(REGISTRY, args));

            assert_eq!(command_path, path, "{args:?}");
            assert!(
                matches!(&fault, ParseFault::UnknownOption { name: named } if named == name),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn root_options_and_no_command_end_as_the_bare_root_does() {
        let help = help_of(REGISTRY, &["--help"]);
        for args in [
            &["-v"][..],
            &["--service-orchestrator", "X"],
            &["-v", "--service-orchestrator", "X"],
            &["--service-orchestrator", "ws"],
            &["--service-orchestrator", "--help"],
            &["--source-override", "T"],
        ] {
            let parsed = parsed(REGISTRY, args);

            assert!(
                matches!(parsed.stage, Stage::Root),
                "{args:?}: {:?}",
                parsed.stage
            );
            assert!(
                matches!(&parsed.outcome, Outcome::Ended(Termination::BareGroup { help: carried }) if *carried == help),
                "{args:?}: {:?}",
                parsed.outcome
            );
        }
    }

    #[test]
    fn a_fault_among_the_root_options_ends_at_the_root_whatever_follows() {
        for args in [
            &["--verbose=x", "ws", "index", "NAME"][..],
            &["-vx", "ws", "index", "NAME"],
            &["-xv", "ws", "index", "NAME"],
            &["-v=x", "ws", "index", "NAME"],
            &["--service-orchestrator"],
            &["-v", "--service-orchestrator"],
            &["--service-orchestrator", "ws", "index", "NAME"],
            &["--help=x", "ws", "index", "NAME"],
            &["--version=x", "ws", "index", "NAME"],
            &["-", "ws", "index", "NAME"],
        ] {
            let parsed = parsed(REGISTRY, args);

            assert!(
                matches!(parsed.stage, Stage::Root),
                "{args:?}: {:?}",
                parsed.stage
            );
            let (command_path, usage, _) = usage_of(parsed);
            assert!(command_path.is_empty(), "{args:?}");
            assert_eq!(usage.help_command, "winter-workspace --help", "{args:?}");
        }
    }

    #[test]
    fn a_double_dash_before_a_command_name_is_refused_at_the_node_it_stands_at() {
        for (args, path) in [
            (&["--", "ws", "index", "NAME"][..], &[][..]),
            (&["-v", "--", "ws", "index", "NAME"], &[]),
            (&["--", "--help"], &[]),
            (&["--", "nope"], &[]),
            (&["ws", "--", "index", "NAME"], &["ws"]),
            (&["ws", "--", "--help"], &["ws"]),
        ] {
            let parsed = parsed(REGISTRY, args);

            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            let (command_path, fault) = fault_of(parsed);
            assert_eq!(command_path, path, "{args:?}");
            assert!(
                matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &["--"]),
                "{args:?}: {fault:?}"
            );
        }

        // With nothing after it, the line ends as the bare node does.
        for (args, path) in [(&["--"][..], &[][..]), (&["ws", "--"], &["ws"])] {
            let parsed = parsed(REGISTRY, args);

            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            assert!(
                matches!(
                    parsed.outcome,
                    Outcome::Ended(Termination::BareGroup { .. })
                ),
                "{args:?}: {:?}",
                parsed.outcome
            );
        }

        // At a command it ends the options, and the word after it is the command's argument.
        let parsed = parsed(REGISTRY, &["ws", "index", "--", "NAME"]);
        assert!(
            matches!(parsed.outcome, Outcome::Dispatch { .. }),
            "{:?}",
            parsed.outcome
        );
    }

    #[test]
    fn the_faults_of_the_root_options_are_classed() {
        let (_, fault) = fault_of(parsed(REGISTRY, &["--service-orchestrator"]));
        assert!(
            matches!(&fault, ParseFault::MissingOptionValue { option } if option.contains("--service-orchestrator")),
            "{fault:?}"
        );

        let (_, fault) = fault_of(parsed(
            REGISTRY,
            &["--service-orchestrator", "ws", "index", "NAME"],
        ));
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "index"),
            "{fault:?}"
        );

        let (_, fault) = fault_of(parsed(REGISTRY, &["-vx", "ws", "index", "NAME"]));
        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == "-x"),
            "{fault:?}"
        );
    }

    #[test]
    fn a_value_given_to_a_flag_is_refused_at_the_node_of_the_flag() {
        for (args, path, flag) in [
            (
                &["--verbose=x", "ws", "index", "NAME"][..],
                &[][..],
                "--verbose",
            ),
            (
                &["ws", "index", "--json=x", "one"],
                &["ws", "index"],
                "--json",
            ),
            (
                &["repo", "remove", "project/a", "--local=x"],
                &["repo", "remove"],
                "--local",
            ),
        ] {
            let (command_path, fault) = fault_of(parsed(REGISTRY, args));

            assert_eq!(command_path, path, "{args:?}");
            assert!(
                matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &[flag, "x"]),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn help_or_version_and_a_fault_at_one_node_end_as_the_one_that_stands_first() {
        let rows: [(&[&str], &[&str], Kind); 9] = [
            (&["--version", "--bogus"], &[], Kind::Version),
            (&["--bogus", "--version"], &[], Kind::Ended),
            (&["--help", "--bogus"], &[], Kind::Help),
            (&["--bogus", "--help"], &[], Kind::Ended),
            (
                &["ws", "index", "--help", "--bogus"],
                &["ws", "index"],
                Kind::Help,
            ),
            (
                &["ws", "index", "--bogus", "--help"],
                &["ws", "index"],
                Kind::Ended,
            ),
            (&["--version", "ws", "bogus"], &[], Kind::Version),
            (&["ws", "index", "--help"], &["ws", "index"], Kind::Help),
            (&["-v", "-v", "--help"], &[], Kind::Ended),
        ];

        for (args, path, kind) in rows {
            let parsed = parsed(REGISTRY, args);

            assert_eq!(path_of(&parsed.stage), path, "{args:?}");
            assert_eq!(
                kind_of(&parsed.outcome),
                kind,
                "{args:?}: {:?}",
                parsed.outcome
            );
        }
    }

    #[test]
    fn an_argument_that_is_not_utf_8_is_refused_at_the_node_the_tokens_before_it_reached() {
        let bad: &[u8] = b"na\xffme";
        let rows: [(&[&[u8]], &[&str]); 14] = [
            (&[b"ws", b"index", bad], &["ws", "index"]),
            (&[b"ws", b"index", b"--", bad], &["ws", "index"]),
            (&[b"ws", b"index", b"--help", bad], &["ws", "index"]),
            (
                &[b"ws", b"index", b"NAME", b"--json", bad],
                &["ws", "index"],
            ),
            (&[b"ws", bad, b"index"], &["ws"]),
            (&[b"ws", b"--help", bad], &["ws"]),
            (&[bad], &[]),
            (&[bad, b"ws", b"index", b"NAME"], &[]),
            (
                &[b"--service-orchestrator", bad, b"ws", b"index", b"NAME"],
                &[],
            ),
            (&[b"--help", bad], &[]),
            (&[b"--version", bad], &[]),
            (&[b"--bogus", b"ws", b"index", bad], &[]),
            (&[b"-v", b"-v", b"ws", b"index", bad], &[]),
            (&[b"--", bad], &[]),
        ];

        for (args, path) in rows {
            let parsed = parsed_os(REGISTRY, args);

            let (command_path, usage, fault) = usage_of(parsed);
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
                matches!(&fault, ParseFault::NotUtf8 { argument } if argument == "na\u{fffd}me"),
                "{args:?}: {fault:?}"
            );
        }
    }

    #[test]
    fn the_first_argument_that_is_not_utf_8_is_the_one_reported_and_the_root_options_before_it_are_kept()
     {
        let parsed = parsed_os(
            REGISTRY,
            &[
                b"-v",
                b"--service-orchestrator",
                b"X",
                b"ws",
                b"index",
                b"one\xff",
                b"two\xfe",
                b"--bogus",
            ],
        );

        assert_eq!(
            root_of(&parsed),
            &RootArgs {
                verbose: true,
                service_orchestrator: Some("X".to_owned()),
                source_override: None
            }
        );
        let (command_path, fault) = fault_of(parsed);
        assert_eq!(command_path, ["ws", "index"]);
        assert!(
            matches!(&fault, ParseFault::NotUtf8 { argument } if argument == "one\u{fffd}"),
            "{fault:?}"
        );
    }

    /// The command path of an accepted line is the parser's own; the walk that reads the path of a refused
    /// line has to answer the same for it.
    #[test]
    fn the_walk_reads_the_command_path_the_parser_selects() {
        let mut parser = Parser::build(REGISTRY, &IDENTITY).expect("a tree");
        parser.command.build();
        for args in [
            &["ws", "index", "NAME"][..],
            &["-v", "ws", "index", "NAME"],
            &["--verbose", "ws", "index", "NAME"],
            &["--service-orchestrator", "ws", "ws", "index", "NAME"],
            &["--service-orchestrator", "--help", "ws", "index", "NAME"],
            &["--service-orchestrator", "--bogus", "ws", "index", "NAME"],
            &["--service-orchestrator", "-v", "-v", "ws", "index", "NAME"],
            &["--service-orchestrator=", "ws", "index", "NAME"],
            &["--service-orchestrator", "--", "ws", "index", "NAME"],
            &[
                "--source-override",
                "--service-orchestrator",
                "--service-orchestrator",
                "-v",
                "ws",
                "index",
                "x",
            ],
            &[
                "-v",
                "--service-orchestrator",
                "X",
                "--source-override",
                "T",
                "repo",
                "remove",
                "project/a",
            ],
            &["-v"],
            &["--service-orchestrator", "ws"],
            &["ws"],
        ] {
            let os_args: Vec<OsString> = args.iter().map(OsString::from).collect();
            let mut selected = parser
                .command
                .try_get_matches_from_mut(&os_args)
                .unwrap_or_else(|error| panic!("{args:?}: {error}"));
            let mut path = Vec::new();
            while let Some((name, below)) = selected.remove_subcommand() {
                path.push(name);
                selected = below;
            }

            assert_eq!(resolved(&parser.command, &os_args).path, path, "{args:?}");
        }
    }

    #[test]
    fn the_resolved_path_follows_what_each_option_takes_as_its_value() {
        let tree = with_help(
            clap::Command::new("tool")
                .arg(Arg::new("provider").long("provider").short('p'))
                .arg(
                    Arg::new("source")
                        .long("source")
                        .short('s')
                        .allow_hyphen_values(true),
                )
                .arg(
                    Arg::new("verbose")
                        .long("verbose")
                        .short('v')
                        .action(ArgAction::SetTrue),
                )
                .arg(Arg::new("quiet").short('q').action(ArgAction::SetTrue))
                .arg(Arg::new("level").short('l').action(ArgAction::Count))
                .subcommand(with_help(
                    clap::Command::new("ws").subcommand(clap::Command::new("index")),
                )),
        );
        let path = |args: &[&str]| {
            let args: Vec<OsString> = args.iter().map(OsString::from).collect();
            resolved(&tree, &args).path
        };

        // A value slot that holds an option: taken by an option that accepts a hyphen-leading value, and
        // the fault or the help of the line for one that does not.
        assert_eq!(path(&["--source", "--help", "ws"]), ["ws"]);
        assert_eq!(path(&["--source", "--verbose", "--verbose", "ws"]), ["ws"]);
        assert_eq!(path(&["--source", "--", "ws"]), ["ws"]);
        assert_eq!(path(&["-s", "-v", "ws"]), ["ws"]);
        assert!(path(&["--provider", "--help", "ws"]).is_empty());
        assert!(path(&["--provider", "--verbose", "ws"]).is_empty());
        assert!(path(&["--provider", "-v", "ws"]).is_empty());
        assert!(path(&["-p", "--", "ws"]).is_empty());
        assert_eq!(path(&["--provider", "-", "ws"]), ["ws"]);
        assert!(path(&["--provider"]).is_empty());
        assert!(path(&["--source"]).is_empty());

        // A short cluster: every letter is read, and the letters after one that takes a value are its
        // value.
        assert_eq!(path(&["-vq", "ws"]), ["ws"]);
        assert_eq!(path(&["-qv", "ws", "index"]), ["ws", "index"]);
        assert_eq!(path(&["-vp", "ws", "ws"]), ["ws"]);
        assert_eq!(path(&["-vpws", "ws"]), ["ws"]);
        assert_eq!(path(&["-vsq", "ws"]), ["ws"]);
        assert_eq!(path(&["-vs", "--help", "ws"]), ["ws"]);
        assert!(path(&["-vx", "ws"]).is_empty());
        assert!(path(&["-xv", "ws"]).is_empty());
        assert!(path(&["-vp", "--help", "ws"]).is_empty());

        // An option given twice ends the path unless it may repeat.
        assert!(path(&["-vv", "ws"]).is_empty());
        assert!(path(&["-v", "--verbose", "ws"]).is_empty());
        assert!(path(&["-vqv", "ws"]).is_empty());
        assert!(path(&["--provider", "a", "-pb", "ws"]).is_empty());
        assert!(path(&["--source=a", "--source", "b", "ws"]).is_empty());
        assert_eq!(path(&["-ll", "-l", "ws"]), ["ws"]);

        // A flag takes no value.
        assert!(path(&["--verbose=x", "ws"]).is_empty());
        assert!(path(&["--help=x", "ws"]).is_empty());
        assert_eq!(path(&["--provider=--help", "ws"]), ["ws"]);
    }

    #[test]
    fn the_walk_and_the_parser_agree_on_where_a_line_of_that_tree_is_decided() {
        let mut tree = with_help(
            clap::Command::new("tool")
                .no_binary_name(true)
                .arg(Arg::new("provider").long("provider").short('p'))
                .arg(
                    Arg::new("source")
                        .long("source")
                        .short('s')
                        .allow_hyphen_values(true),
                )
                .arg(
                    Arg::new("verbose")
                        .long("verbose")
                        .short('v')
                        .action(ArgAction::SetTrue),
                )
                .arg(Arg::new("quiet").short('q').action(ArgAction::SetTrue))
                .arg(Arg::new("level").short('l').action(ArgAction::Count))
                .subcommand(with_help(clap::Command::new("ws")).subcommand_required(false)),
        );
        tree.build();
        // Each line ends in the one command of the tree, so the parser accepts it exactly when nothing
        // before that name decides, which is when the walk reads the name into the path.
        for options in [
            &["--source", "--help"][..],
            &["--source", "--verbose", "--verbose"],
            &["--source", "--"],
            &["-s", "-v"],
            &["--provider", "--help"],
            &["--provider", "--verbose"],
            &["--provider", "-v"],
            &["-p", "--"],
            &["--provider", "-"],
            &["-vq"],
            &["-vp", "ws"],
            &["-vpws"],
            &["-vsq"],
            &["-vs", "--help"],
            &["-vx"],
            &["-xv"],
            &["-vp", "--help"],
            &["-vv"],
            &["-v", "--verbose"],
            &["-vqv"],
            &["--provider", "a", "-pb"],
            &["--source=a", "--source", "b"],
            &["-ll", "-l"],
            &["--verbose=x"],
            &["--help=x"],
            &["--provider=--help"],
            &["-v=x"],
            &["-p=x"],
            &["-"],
            &["--source", "a", "--source", "b"],
            &["--provider", "a", "--provider", "b"],
            &["-p", "a", "-s", "b", "-p", "c"],
        ] {
            // The same holds when the command is followed by its help flag or by a fault of its own: the
            // parser answers with that token exactly when the walk read the name.
            for tail in [&["ws"][..], &["ws", "--help"], &["ws", "--bogus"]] {
                let args: Vec<OsString> = options.iter().chain(tail).map(OsString::from).collect();

                let walked = resolved(&tree, &args);
                let at_the_tail = match (tail, tree.try_get_matches_from_mut(&args)) {
                    ([_], answer) => answer.is_ok(),
                    ([_, "--help"], Err(help)) => {
                        help.kind() == ErrorKind::DisplayHelp
                            && help
                                .render()
                                .to_string()
                                .lines()
                                .any(|line| line.starts_with("Usage:") && line.ends_with("ws"))
                    }
                    (_, Err(fault)) => {
                        fault.kind() == ErrorKind::UnknownArgument
                            && matches!(
                                fault.get(ContextKind::InvalidArg),
                                Some(ContextValue::String(name)) if name == "--bogus"
                            )
                    }
                    (_, Ok(_)) => false,
                };

                // A repeat the walk refuses is decided by the walk, and the parser is not asked: before a
                // help or a fault of the command it answers some such lines with that later token.
                if tail.len() > 1 && matches!(walked.refused, Some(Refused::Repeated { .. })) {
                    assert!(walked.path.is_empty(), "{options:?} {tail:?}");
                    continue;
                }
                assert_eq!(!walked.path.is_empty(), at_the_tail, "{options:?} {tail:?}");
            }
        }
    }

    #[test]
    fn the_walk_refuses_a_repeat_and_a_double_dash_that_a_token_follows() {
        let tree = with_help(
            clap::Command::new("tool")
                .no_binary_name(true)
                .arg(Arg::new("provider").long("provider").short('p'))
                .arg(Arg::new("quiet").short('q').action(ArgAction::SetTrue))
                .arg(Arg::new("level").short('l').action(ArgAction::Count))
                .subcommand(with_help(
                    clap::Command::new("ws").subcommand(clap::Command::new("index")),
                )),
        );
        let refused = |args: &[&str]| {
            let args: Vec<OsString> = args.iter().map(OsString::from).collect();
            let walked = resolved(&tree, &args);
            (walked.path, walked.refused)
        };
        let repeated = |option: &str| {
            Some(Refused::Repeated {
                option: option.to_owned(),
            })
        };

        for tail in [&[][..], &["ws"], &["ws", "--help"], &["--bogus"], &["nope"]] {
            let line: Vec<&str> = ["--provider", "a", "--provider", "b"]
                .into_iter()
                .chain(tail.iter().copied())
                .collect();
            assert_eq!(refused(&line), (vec![], repeated("--provider")), "{tail:?}");
        }
        assert_eq!(
            refused(&["-p", "a", "-pb", "ws"]),
            (vec![], repeated("--provider"))
        );
        assert_eq!(refused(&["-qq", "ws"]), (vec![], repeated("-q")));
        assert_eq!(
            refused(&["--provider", "a", "--provider"]),
            (vec![], repeated("--provider"))
        );
        // An option that may repeat is not refused, and an option's value is no second giving of it.
        assert_eq!(refused(&["-l", "-ll", "ws"]), (vec!["ws".to_owned()], None));
        assert_eq!(refused(&["--provider", "--provider", "ws"]), (vec![], None));

        assert_eq!(refused(&["--", "ws"]), (vec![], Some(Refused::DoubleDash)));
        assert_eq!(
            refused(&["ws", "--", "index"]),
            (vec!["ws".to_owned()], Some(Refused::DoubleDash))
        );
        assert_eq!(
            refused(&["-q", "--", "--help"]),
            (vec![], Some(Refused::DoubleDash))
        );
        // Nothing follows it, so nothing is refused; and at a node that has no commands it is not read.
        assert_eq!(refused(&["--"]), (vec![], None));
        assert_eq!(refused(&["ws", "--"]), (vec!["ws".to_owned()], None));
        assert_eq!(
            refused(&["ws", "index", "--", "x"]),
            (vec!["ws".to_owned(), "index".to_owned()], None)
        );
        // A help, a version, or a fault that stands first decides, and nothing after it is refused.
        assert_eq!(refused(&["--help", "-q", "-q"]), (vec![], None));
        assert_eq!(refused(&["--bogus", "--", "ws"]), (vec![], None));
    }

    #[test]
    fn no_line_of_root_tokens_before_a_command_ends_as_an_internal_failure() {
        const TOKENS: [&[u8]; 16] = [
            b"-v",
            b"--verbose",
            b"-vv",
            b"--verbose=x",
            b"--service-orchestrator",
            b"--service-orchestrator=a",
            b"--source-override",
            b"--source-override=",
            b"--help",
            b"--version",
            b"--",
            b"-",
            b"",
            b"--bogus",
            b"ws",
            b"x",
        ];
        const ENDS: [&[&[u8]]; 6] = [
            &[b"ws", b"index", b"NAME"],
            &[b"ws", b"index", b"--bogus"],
            &[b"ws", b"nope"],
            &[b"ws", b"index", b"--help"],
            &[b"ws", b"index", b"\xff"],
            &[b"ws", b"--", b"index"],
        ];
        // Every line of up to three of the tokens, each followed by each ending.
        let mut lines: Vec<Vec<&[u8]>> = vec![Vec::new()];
        let mut longest = lines.clone();
        for _ in 0..3 {
            longest = longest
                .iter()
                .flat_map(|line| {
                    TOKENS.iter().map(move |token| {
                        let mut line = line.clone();
                        line.push(*token);
                        line
                    })
                })
                .collect();
            lines.extend(longest.iter().cloned());
        }

        for line in &lines {
            for end in ENDS {
                let args: Vec<&[u8]> = line.iter().chain(end).copied().collect();

                let parsed = parsed_os(REGISTRY, &args);

                assert!(
                    !matches!(parsed.outcome, Outcome::Ended(Termination::Internal(_))),
                    "{:?}",
                    args.iter()
                        .map(|arg| String::from_utf8_lossy(arg))
                        .collect::<Vec<_>>()
                );
            }
        }
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
