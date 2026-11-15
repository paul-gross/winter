use std::ffi::OsString;
use std::path::Path;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let root = Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."));
    ExitCode::from(winter_workspace_dev::main_with(
        &args,
        &|key| std::env::var_os(key),
        root,
    ))
}
