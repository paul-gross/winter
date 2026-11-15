//! The pinned environment: the whole environment of every child the harness starts.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use sha1::{Digest, Sha1};

/// Stands for the root of the isolated tree in a value's digest form.
const ROOT: &str = "<ROOT>";
/// Stands for the directory of the host's git in a value's digest form.
const DIRECTORY_OF_GIT: &str = "<directory of git>";

/// `PATH` on a host that has git, and on one that has none.
const PATH_WITH_GIT: &str = "<ROOT>/bin:<directory of git>:/usr/bin:/bin";
const PATH_WITHOUT_GIT: &str = "<ROOT>/bin:/usr/bin:/bin";

/// Every pinned variable but `PATH`, with the placeholders where the root of the tree goes.
const TABLE: [(&str, &str); 23] = [
    ("HOME", "<ROOT>/home"),
    ("XDG_CONFIG_HOME", "<ROOT>/home/.config"),
    ("XDG_CACHE_HOME", "<ROOT>/cache"),
    ("TMPDIR", "<ROOT>/tmp"),
    ("GIT_CONFIG_GLOBAL", "<ROOT>/gitconfig"),
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_TEMPLATE_DIR", "<ROOT>/git-template"),
    // The parent of the root, which git resolves to its real path before comparing. Git examines neither a
    // ceiling directory nor one above it, and a ceiling binds only a working directory strictly below it: with
    // the parent as the ceiling the search for a repository is bounded from the root downward, and a
    // repository made inside the tree is still found from inside it.
    ("GIT_CEILING_DIRECTORIES", "<ROOT>/.."),
    ("GIT_TERMINAL_PROMPT", "0"),
    ("GIT_AUTHOR_NAME", "Fixture Author"),
    ("GIT_AUTHOR_EMAIL", "author@fixture.invalid"),
    ("GIT_COMMITTER_NAME", "Fixture Committer"),
    ("GIT_COMMITTER_EMAIL", "committer@fixture.invalid"),
    ("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+0000"),
    ("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+0000"),
    ("TZ", "UTC"),
    ("LC_ALL", "C.UTF-8"),
    ("LANG", "C.UTF-8"),
    ("NO_COLOR", "1"),
    ("TERM", "dumb"),
    ("COLUMNS", "80"),
    ("LINES", "24"),
    ("OTEL_SDK_DISABLED", "true"),
];

/// The global git configuration of a tree: fixture remote URLs resolve to the tree's bare repositories.
const GITCONFIG: &str = "[url \"<ROOT>/remotes/\"]\n\tinsteadOf = https://fixture.invalid/\n";

/// One variable: what the child sees, and the same text with the placeholders left in.
#[derive(Clone, Debug)]
struct Pinned {
    value: OsString,
    digest_form: String,
}

/// The environment of a child process: exactly these variables, nothing inherited.
#[derive(Clone, Debug)]
pub struct PinnedEnv {
    root: String,
    variables: BTreeMap<OsString, Pinned>,
}

impl PinnedEnv {
    /// The pinned table for the tree at `root`, a path that is valid UTF-8.
    pub(crate) fn for_root(root: &str) -> Self {
        let git = directory_of_git().and_then(Path::to_str);
        let path = match git {
            Some(_) => PATH_WITH_GIT,
            None => PATH_WITHOUT_GIT,
        };
        let variables = std::iter::once(("PATH", path))
            .chain(TABLE)
            .map(|(name, template)| {
                let value = template
                    .replace(ROOT, root)
                    .replace(DIRECTORY_OF_GIT, git.unwrap_or_default());
                let pinned = Pinned {
                    value: value.into(),
                    digest_form: template.to_owned(),
                };
                (OsString::from(name), pinned)
            })
            .collect();
        Self {
            root: root.to_owned(),
            variables,
        }
    }

    /// Adds the variable `key`, or replaces its value.
    pub fn set(&mut self, key: &str, value: &str) {
        let pinned = Pinned {
            value: value.into(),
            digest_form: value.replace(&self.root, ROOT),
        };
        self.variables.insert(key.into(), pinned);
    }

    /// The SHA-1, in lowercase hexadecimal, of the sorted `NAME=VALUE` lines followed by the text of the tree's
    /// global git configuration, with the root of the tree and the directory of git left as placeholders: equal
    /// for the same variables in any tree on any host.
    pub fn digest(&self) -> String {
        let mut hasher = Sha1::new();
        for (name, pinned) in &self.variables {
            hasher.update(name.as_encoded_bytes());
            hasher.update(b"=");
            hasher.update(pinned.digest_form.as_bytes());
            hasher.update(b"\n");
        }
        hasher.update(GITCONFIG.as_bytes());
        hasher
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    }

    /// Every variable with its value, sorted by name.
    pub(crate) fn variables(&self) -> impl Iterator<Item = (&OsStr, &OsStr)> {
        self.variables
            .iter()
            .map(|(name, pinned)| (name.as_os_str(), pinned.value.as_os_str()))
    }
}

/// The text of the global git configuration of the tree at `root`.
pub(crate) fn gitconfig(root: &str) -> String {
    GITCONFIG.replace(ROOT, root)
}

/// The directory of the first `git` on the test process's own `PATH`, resolved once; `None` when there is none.
fn directory_of_git() -> Option<&'static Path> {
    static DIRECTORY: OnceLock<Option<PathBuf>> = OnceLock::new();
    DIRECTORY
        .get_or_init(|| {
            let path = std::env::var_os("PATH")?;
            std::env::split_paths(&path).find(|directory| {
                directory.is_absolute()
                    && std::fs::metadata(directory.join("git")).is_ok_and(|metadata| {
                        metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
                    })
            })
        })
        .as_deref()
}
