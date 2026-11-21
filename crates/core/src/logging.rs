//! The levels of a log record, and the resolution of `-v` and `WINTER_LOG_LEVEL` into what is logged.

use std::ffi::OsStr;

/// The level of a log record, lowest first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Debug,
    Info,
    Warning,
    Error,
    Critical,
}

/// What one invocation logs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Log {
    /// Records below it are dropped.
    pub threshold: Level,
    /// Whether `-v` or a valid `WINTER_LOG_LEVEL` switched the record format.
    pub switched: bool,
}

impl Log {
    /// What is logged when nothing asks for more: warnings and above, in the format that is not switched.
    pub const DEFAULT: Log = Log {
        threshold: Level::Warning,
        switched: false,
    };

    /// Resolves the root's verbose flag and the value of `WINTER_LOG_LEVEL`.
    ///
    /// The flag wins over the variable. The variable names a level in any letter case: `DEBUG`, `INFO`,
    /// `WARNING` or `WARN`, `ERROR`, `CRITICAL` or `FATAL`. `NOTSET` switches the format and leaves the
    /// threshold at warning. Any other value, an empty one and one that is not UTF-8 included, is ignored.
    pub(crate) fn resolve(verbose: bool, variable: Option<&OsStr>) -> Log {
        if verbose {
            return Log {
                threshold: Level::Debug,
                switched: true,
            };
        }
        let Some(name) = variable.and_then(OsStr::to_str) else {
            return Log::DEFAULT;
        };
        let threshold = match name.to_ascii_uppercase().as_str() {
            "DEBUG" => Level::Debug,
            "INFO" => Level::Info,
            "WARNING" | "WARN" | "NOTSET" => Level::Warning,
            "ERROR" => Level::Error,
            "CRITICAL" | "FATAL" => Level::Critical,
            _ => return Log::DEFAULT,
        };
        Log {
            threshold,
            switched: true,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;

    use super::*;

    const VERBOSE: Log = Log {
        threshold: Level::Debug,
        switched: true,
    };

    fn switched(threshold: Level) -> Log {
        Log {
            threshold,
            switched: true,
        }
    }

    fn not_utf8() -> OsString {
        OsString::from_vec(vec![b'D', b'E', 0xff])
    }

    /// Every name the variable may hold, with the threshold it gives.
    const NAMES: [(&str, Level); 8] = [
        ("DEBUG", Level::Debug),
        ("INFO", Level::Info),
        ("WARNING", Level::Warning),
        ("WARN", Level::Warning),
        ("ERROR", Level::Error),
        ("CRITICAL", Level::Critical),
        ("FATAL", Level::Critical),
        ("NOTSET", Level::Warning),
    ];

    #[test]
    fn the_levels_are_ordered_from_debug_to_critical() {
        assert!(Level::Debug < Level::Info);
        assert!(Level::Info < Level::Warning);
        assert!(Level::Warning < Level::Error);
        assert!(Level::Error < Level::Critical);
    }

    #[test]
    fn neither_the_flag_nor_the_variable_gives_the_default() {
        assert_eq!(Log::resolve(false, None), Log::DEFAULT);
        assert_eq!(
            Log::DEFAULT,
            Log {
                threshold: Level::Warning,
                switched: false
            }
        );
    }

    #[test]
    fn the_flag_alone_gives_debug_and_switches() {
        assert_eq!(Log::resolve(true, None), VERBOSE);
    }

    #[test]
    fn each_level_name_gives_its_level_and_switches_in_upper_and_lower_case() {
        for (name, level) in NAMES {
            for spelled in [name.to_owned(), name.to_lowercase()] {
                assert_eq!(
                    Log::resolve(false, Some(OsStr::new(&spelled))),
                    switched(level),
                    "{spelled}"
                );
            }
        }
    }

    #[test]
    fn a_level_name_in_mixed_case_is_read() {
        assert_eq!(
            Log::resolve(false, Some(OsStr::new("Info"))),
            switched(Level::Info)
        );
    }

    #[test]
    fn notset_switches_and_leaves_the_threshold_at_warning() {
        for spelled in ["NOTSET", "notset"] {
            assert_eq!(
                Log::resolve(false, Some(OsStr::new(spelled))),
                Log {
                    threshold: Level::Warning,
                    switched: true
                },
                "{spelled}"
            );
        }
    }

    #[test]
    fn an_unknown_name_gives_the_default() {
        for unknown in ["LOUD", "TRACE", " DEBUG", "DEBUG ", "10"] {
            assert_eq!(
                Log::resolve(false, Some(OsStr::new(unknown))),
                Log::DEFAULT,
                "{unknown:?}"
            );
        }
    }

    #[test]
    fn an_empty_value_gives_the_default() {
        assert_eq!(Log::resolve(false, Some(OsStr::new(""))), Log::DEFAULT);
    }

    #[test]
    fn a_value_that_is_not_utf_8_gives_the_default() {
        assert_eq!(Log::resolve(false, Some(&not_utf8())), Log::DEFAULT);
    }

    #[test]
    fn the_flag_wins_over_every_value_of_the_variable() {
        for (name, _) in NAMES {
            for spelled in [name.to_owned(), name.to_lowercase()] {
                assert_eq!(
                    Log::resolve(true, Some(OsStr::new(&spelled))),
                    VERBOSE,
                    "{spelled}"
                );
            }
        }
        for other in ["LOUD", ""] {
            assert_eq!(
                Log::resolve(true, Some(OsStr::new(other))),
                VERBOSE,
                "{other:?}"
            );
        }
        assert_eq!(Log::resolve(true, Some(&not_utf8())), VERBOSE);
    }
}
