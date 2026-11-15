//! The argument parser: built from a slice of registry entries, and the dispatch to the command it selects.

use std::backtrace::Backtrace;
use std::error::Error;
use std::ffi::OsString;
use std::io;

use clap::error::{ContextKind, ContextValue, ErrorKind};
use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::groups;
use crate::process::Identity;
use crate::registry::{Entry, Registration};
use crate::termination::{InternalError, ParseFault, Termination, UsageError};

type Run = fn(&ArgMatches, &mut Context<'_>) -> Termination;

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

/// Parses `args` against the tree built from `entries` and runs the command they select.
///
/// Writes the help or the version line a flag asks for to `cx.out`; writes nothing for any other ending that
/// is not a command's own.
pub(crate) fn run(entries: &[Entry], args: &[OsString], cx: &mut Context<'_>) -> Termination {
    match Parser::build(entries, cx.identity) {
        Ok(parser) => parser.dispatch(args, cx),
        Err(error) => internal(error),
    }
}

struct Parser {
    command: clap::Command,
    routes: Vec<Route>,
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
        Ok(Self { command, routes })
    }

    fn dispatch(mut self, args: &[OsString], cx: &mut Context<'_>) -> Termination {
        let matches = match self.command.try_get_matches_from_mut(args) {
            Ok(matches) => matches,
            Err(error) => return self.answer(&error, args, cx),
        };

        let mut path = Vec::new();
        let mut selected = &matches;
        while let Some((name, below)) = selected.subcommand() {
            path.push(name);
            selected = below;
        }
        match self.route(&path) {
            Some(route) => (route.run)(selected, cx),
            None => Termination::BareGroup {
                help: self.help(&path),
            },
        }
    }

    /// Ends an invocation the parser stopped: the help or version a flag asked for, or a usage error.
    fn answer(&self, error: &clap::Error, args: &[OsString], cx: &mut Context<'_>) -> Termination {
        match error.kind() {
            ErrorKind::DisplayHelp => written(write!(cx.out, "{}", error.render())),
            ErrorKind::DisplayVersion => written(writeln!(
                cx.out,
                "{} {}",
                cx.identity.program, cx.identity.version
            )),
            _ => {
                let command_path = parsed_path(&self.command, args);
                let at_leaf = self.route(&command_path).is_some();
                Termination::Usage(UsageError::Parse {
                    command_path,
                    fault: fault(error, at_leaf),
                })
            }
        }
    }

    fn route<S: AsRef<str>>(&self, path: &[S]) -> Option<&Route> {
        self.routes.iter().find(|route| {
            route.path.len() == path.len()
                && route.path.iter().zip(path).all(|(a, b)| *a == b.as_ref())
        })
    }

    /// The text `--help` writes for the node at `path`.
    fn help(&mut self, path: &[&str]) -> String {
        let args = path.iter().copied().chain(["--help"]);
        self.command
            .try_get_matches_from_mut(args)
            .err()
            .map(|help| help.render().to_string())
            .unwrap_or_default()
    }
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

/// The command path `args` selects before the first token that names no command: the path a parse fault
/// happened under.
///
/// An option's value is stepped over, so that a value spelled like a command does not extend the path.
fn parsed_path(root: &clap::Command, args: &[OsString]) -> Vec<String> {
    let mut path = Vec::new();
    let mut node = root;
    let mut tokens = args.iter().map(|arg| arg.to_string_lossy());
    while node.has_subcommands()
        && let Some(token) = tokens.next()
    {
        if token == "--" {
            break;
        }
        if token.starts_with('-') {
            let Some(option) = option_named(node, &token) else {
                break;
            };
            if option.get_action().takes_values() && !token.contains('=') && is_whole_name(&token) {
                tokens.next();
            }
            continue;
        }
        let Some(below) = node.find_subcommand(token.as_ref()) else {
            break;
        };
        path.push(below.get_name().to_owned());
        node = below;
    }
    path
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

/// Whether `token` is an option name alone, with no value attached to a short letter.
fn is_whole_name(token: &str) -> bool {
    token.starts_with("--") || token.chars().count() == 2
}

/// Classes what the parser refused. `at_leaf` tells whether the fault happened in a command's own arguments,
/// where a stray word is an argument too many, or at the root or a group, where it names no command.
fn fault(error: &clap::Error, at_leaf: bool) -> ParseFault {
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
            if name.starts_with('-') {
                ParseFault::UnknownOption { name }
            } else if at_leaf {
                ParseFault::UnexpectedArgument { values: vec![name] }
            } else {
                ParseFault::UnknownCommand { name }
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

fn written(result: io::Result<()>) -> Termination {
    match result {
        Ok(()) => Termination::Success,
        Err(error) => internal(error),
    }
}

fn internal(error: impl Error + Send + Sync + 'static) -> Termination {
    Termination::Internal(InternalError::Unexpected {
        source: Box::new(error),
        backtrace: Backtrace::capture(),
    })
}

#[cfg(test)]
mod tests {
    use std::panic::{AssertUnwindSafe, catch_unwind};
    use std::path::Path;

    use super::*;
    use crate::files::OsFiles;
    use crate::process::ProcessEnv;
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

    struct Ended {
        termination: Termination,
        stdout: String,
        stderr: String,
    }

    fn run_with(entries: &[Entry], args: &[&str]) -> Ended {
        let args: Vec<OsString> = args.iter().map(OsString::from).collect();
        let env = ProcessEnv::from_vars([]);
        let mut stdin: &[u8] = &[];
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let termination = run(
            entries,
            &args,
            &mut Context {
                cwd: Path::new("/"),
                env: &env,
                identity: &IDENTITY,
                stdin: &mut stdin,
                out: &mut stdout,
                err: &mut stderr,
                out_is_terminal: false,
                err_is_terminal: false,
                files: &OsFiles,
            },
        );
        Ended {
            termination,
            stdout: String::from_utf8(stdout).expect("UTF-8 on stdout"),
            stderr: String::from_utf8(stderr).expect("UTF-8 on stderr"),
        }
    }

    fn usage_of(ended: Ended) -> (Vec<String>, ParseFault) {
        assert_eq!(ended.stdout, "");
        assert_eq!(ended.stderr, "");
        match ended.termination {
            Termination::Usage(UsageError::Parse {
                command_path,
                fault,
            }) => (command_path, fault),
            other => panic!("expected a usage termination, got {other:?}"),
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

    #[test]
    fn a_command_dispatches_to_its_run() {
        let ended = run_with(ENTRIES, &["service", "up", "alpha", "beta"]);

        assert!(matches!(ended.termination, Termination::Success));
        assert_eq!(ended.stdout, "up alpha beta\n");
        assert_eq!(ended.stderr, "");
    }

    #[test]
    fn a_top_level_command_dispatches_to_its_run() {
        let ended = run_with(ENTRIES, &["agents"]);

        assert!(matches!(ended.termination, Termination::Success));
        assert_eq!(ended.stdout, "agents\n");
    }

    #[test]
    fn an_alias_dispatches_to_the_run_of_the_command_it_names() {
        let ended = run_with(ENTRIES, &["service", "start", "alpha"]);

        assert!(matches!(ended.termination, Termination::Success));
        assert_eq!(ended.stdout, "up alpha\n");
    }

    #[test]
    fn an_alias_takes_the_arguments_of_the_command_it_names() {
        let (command_path, fault) = usage_of(run_with(ENTRIES, &["service", "start"]));

        assert_eq!(command_path, ["service", "start"]);
        assert!(
            matches!(&fault, ParseFault::MissingArgument { metavar } if metavar.contains("PATTERNS")),
            "{fault:?}"
        );
    }

    #[test]
    fn a_hidden_command_is_absent_from_help_and_dispatches() {
        let help = run_with(ENTRIES, &["service", "--help"]);
        assert!(matches!(help.termination, Termination::Success));
        assert!(help.stdout.contains("up"), "{}", help.stdout);
        assert!(!help.stdout.contains("catalog"), "{}", help.stdout);

        let ended = run_with(ENTRIES, &["service", "catalog"]);
        assert!(matches!(ended.termination, Termination::Reported));
        assert_eq!(ended.stdout, "catalog\n");
    }

    #[test]
    fn a_group_without_entries_is_an_unknown_command() {
        let (command_path, fault) = usage_of(run_with(ENTRIES, &["ws"]));

        assert!(command_path.is_empty());
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "ws"),
            "{fault:?}"
        );

        let root_help = run_with(ENTRIES, &["--help"]).stdout;
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
    fn a_bare_group_returns_its_help_and_writes_nothing() {
        let ended = run_with(ENTRIES, &["service"]);

        let Termination::BareGroup { help } = ended.termination else {
            panic!("expected a bare group, got {:?}", ended.termination);
        };
        assert_eq!(ended.stdout, "");
        assert_eq!(ended.stderr, "");
        assert_eq!(help, run_with(ENTRIES, &["service", "--help"]).stdout);
        let listed: Vec<&str> = help
            .lines()
            .filter_map(|line| line.split_whitespace().next())
            .collect();
        assert!(listed.contains(&"up"), "{help}");
        assert!(listed.contains(&"start"), "{help}");
        assert!(!help.contains("catalog"), "{help}");
    }

    #[test]
    fn a_bare_root_returns_the_root_help() {
        let ended = run_with(ENTRIES, &[]);

        let Termination::BareGroup { help } = ended.termination else {
            panic!("expected a bare group, got {:?}", ended.termination);
        };
        assert_eq!(ended.stdout, "");
        assert_eq!(help, run_with(ENTRIES, &["--help"]).stdout);
    }

    #[test]
    fn help_of_a_command_is_written_to_stdout() {
        let ended = run_with(ENTRIES, &["service", "up", "--help"]);

        assert!(matches!(ended.termination, Termination::Success));
        assert!(ended.stdout.contains("PATTERNS"), "{}", ended.stdout);
        assert!(ended.stdout.contains("--tail"), "{}", ended.stdout);
        assert!(
            ended.stdout.contains("winter-workspace service up"),
            "{}",
            ended.stdout
        );
        assert_eq!(ended.stderr, "");
    }

    #[test]
    fn an_unknown_command_of_a_group_is_a_usage_error_under_that_group() {
        let (command_path, fault) = usage_of(run_with(ENTRIES, &["service", "nope"]));

        assert_eq!(command_path, ["service"]);
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "nope"),
            "{fault:?}"
        );
    }

    #[test]
    fn commands_and_long_options_are_not_matched_by_prefix() {
        let (_, fault) = usage_of(run_with(ENTRIES, &["serv", "up", "alpha"]));
        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == "serv"),
            "{fault:?}"
        );

        let (command_path, fault) =
            usage_of(run_with(ENTRIES, &["service", "up", "alpha", "--tai", "3"]));
        assert_eq!(command_path, ["service", "up"]);
        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == "--tai"),
            "{fault:?}"
        );
    }

    #[test]
    fn no_node_has_a_short_help_flag_or_a_help_command() {
        for args in [&["-h"][..], &["service", "-h"], &["service", "up", "-h"]] {
            let (_, fault) = usage_of(run_with(ENTRIES, args));
            assert!(
                matches!(&fault, ParseFault::UnknownOption { name } if name == "-h"),
                "{args:?}: {fault:?}"
            );
        }

        let (_, fault) = usage_of(run_with(ENTRIES, &["help"]));
        assert!(
            matches!(fault, ParseFault::UnknownCommand { .. }),
            "{fault:?}"
        );
        let (_, fault) = usage_of(run_with(ENTRIES, &["service", "help"]));
        assert!(
            matches!(fault, ParseFault::UnknownCommand { .. }),
            "{fault:?}"
        );
    }

    #[test]
    fn the_version_flag_belongs_to_the_root_alone() {
        let (command_path, fault) = usage_of(run_with(ENTRIES, &["service", "--version"]));

        assert_eq!(command_path, ["service"]);
        assert!(
            matches!(fault, ParseFault::UnknownOption { .. }),
            "{fault:?}"
        );
    }

    #[test]
    fn an_option_without_its_value_is_a_missing_option_value() {
        let (_, fault) = usage_of(run_with(ENTRIES, &["service", "up", "alpha", "--tail"]));

        assert!(
            matches!(&fault, ParseFault::MissingOptionValue { option } if option.contains("--tail")),
            "{fault:?}"
        );
    }

    #[test]
    fn a_value_of_the_wrong_type_is_an_invalid_value() {
        let (_, fault) = usage_of(run_with(
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
        let (_, fault) = usage_of(run_with(
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
        let (command_path, fault) = usage_of(run_with(ENTRIES, &["agents", "extra"]));

        assert_eq!(command_path, ["agents"]);
        assert!(
            matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &["extra"]),
            "{fault:?}"
        );
    }

    #[test]
    fn the_parsed_path_steps_over_option_values_and_stops_at_the_first_stranger() {
        let tree = clap::Command::new("tool")
            .arg(Arg::new("provider").long("provider").short('p'))
            .arg(
                Arg::new("verbose")
                    .long("verbose")
                    .action(ArgAction::SetTrue),
            )
            .subcommand(clap::Command::new("ws").subcommand(clap::Command::new("index")));
        let path = |args: &[&str]| {
            let args: Vec<OsString> = args.iter().map(OsString::from).collect();
            parsed_path(&tree, &args)
        };

        assert_eq!(path(&["ws", "index", "ws"]), ["ws", "index"]);
        assert_eq!(path(&["--provider", "ws", "ws"]), ["ws"]);
        assert_eq!(path(&["-p", "ws", "ws"]), ["ws"]);
        assert_eq!(path(&["--provider=x", "ws"]), ["ws"]);
        assert_eq!(path(&["-px", "ws"]), ["ws"]);
        assert_eq!(path(&["--verbose", "ws", "nope", "index"]), ["ws"]);
        assert!(path(&["--nope", "ws"]).is_empty());
        assert!(path(&["--", "ws"]).is_empty());
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
