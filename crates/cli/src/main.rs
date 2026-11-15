use std::io::{IsTerminal, Write};
use std::process::ExitCode;

use winter_workspace_core::{Identity, Invocation, ProcessEnv, run};

fn main() -> ExitCode {
    let args = std::env::args_os().skip(1).collect();
    let cwd = std::env::current_dir().unwrap_or_default();
    let env = ProcessEnv::from_vars(std::env::vars_os());

    let mut stdin = std::io::stdin().lock();
    let mut stdout = std::io::stdout().lock();
    let mut stderr = std::io::stderr().lock();
    let stdout_is_terminal = stdout.is_terminal();
    let stderr_is_terminal = stderr.is_terminal();

    let termination = run(Invocation {
        args,
        cwd,
        env,
        identity: Identity {
            program: "winter-workspace",
            version: env!("CARGO_PKG_VERSION"),
        },
        stdin: &mut stdin,
        stdout: &mut stdout,
        stderr: &mut stderr,
        stdout_is_terminal,
        stderr_is_terminal,
    });

    if stdout.flush().is_err() {
        return ExitCode::from(1);
    }
    if let Some(diagnostic) = termination.diagnostic() {
        let newline = if diagnostic.ends_with('\n') { "" } else { "\n" };
        if write!(stderr, "{diagnostic}{newline}").is_err() {
            return ExitCode::from(1);
        }
    }

    ExitCode::from(termination.exit_code())
}
