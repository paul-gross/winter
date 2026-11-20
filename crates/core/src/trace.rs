//! The pure part of tracing: the settings of the command span, read from the environment snapshot, the span's
//! name, the port `run` announces a command to, and the three log records about tracing.
//!
//! Nothing here reads a clock, opens a socket, or names a tracing crate: the instant is an argument, the
//! environment is the snapshot, and the host's tracer is behind the port.

use std::fmt::Display;
use std::io::Write;

use crate::diagnostics::{self, Record};
use crate::logging::{Level, Log};
use crate::process::ProcessEnv;

/// The origin of the records about tracing.
const ORIGIN: &str = "trace";

/// The base address of the collector. Tracing is on only when it is set and not blank.
const ENDPOINT: &str = "WINTER_OTEL_EXPORTER_OTLP_ENDPOINT";
/// The headers of the export request, as `name=value` items separated by commas.
const HEADERS: &str = "WINTER_OTEL_EXPORTER_OTLP_HEADERS";
/// The kill switch: the value `true` turns tracing off.
const KILL_SWITCH: &str = "OTEL_SDK_DISABLED";
const SERVICE_NAME_VARIABLE: &str = "OTEL_SERVICE_NAME";
const RESOURCE_ATTRIBUTES: &str = "OTEL_RESOURCE_ATTRIBUTES";
const TRACEPARENT: &str = "TRACEPARENT";
const LAUNCH_TIME: &str = "WINTER_LAUNCH_TIME";

/// The header the export sets itself: a configured one of this name is not sent.
const CONTENT_TYPE: &str = "content-type";

/// The path every export is sent to, below the configured address.
const EXPORT_PATH: &str = "/v1/traces";
/// The resource attribute that names the service.
const SERVICE_NAME: &str = "service.name";
/// The service name when the environment gives none.
const DEFAULT_SERVICE_NAME: &str = "winter";

const NANOSECONDS_PER_SECOND: u64 = 1_000_000_000;

/// What the environment says about tracing one invocation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TraceSettings {
    /// No endpoint is configured, or the kill switch is set.
    Off,
    /// Tracing is on and cannot be carried out as configured. The text says why.
    Unavailable(String),
    On(TraceConfig),
}

/// What the environment configures of the command span and of its export.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceConfig {
    /// The address the export is sent to; it ends in `/v1/traces`.
    pub endpoint: String,
    /// The configured headers that are sent with the export, each name in lower case and held once. None is
    /// named `content-type`, and no value holds a byte that is not legal in an HTTP header.
    pub headers: Vec<(String, String)>,
    /// The names of the configured headers that are not sent because their value is not legal in an HTTP
    /// header, in the order they were configured.
    pub skipped_headers: Vec<String>,
    /// The resource attributes, `service.name` first.
    pub resource: Vec<(String, String)>,
    /// The value of `TRACEPARENT` as it was given, or `None` when the span starts a trace of its own. Whether
    /// the value is valid is not judged here.
    pub traceparent: Option<String>,
    /// The instant the process was launched, when `WINTER_LAUNCH_TIME` gives a usable one.
    pub launch_time_unix_nano: Option<u64>,
}

/// Reads the tracing settings from `env`. `now_unix_nano` is the instant of the read, which a launch time may
/// not lie after.
///
/// Tracing is off when `WINTER_OTEL_EXPORTER_OTLP_ENDPOINT` is unset or blank, and when `OTEL_SDK_DISABLED`
/// is `true` after trimming, in any letter case. The settings of an invocation that traces come from five more
/// variables: `WINTER_OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`,
/// `TRACEPARENT`, and `WINTER_LAUNCH_TIME`. An endpoint that is not UTF-8 makes tracing unavailable; any
/// other of the seven that is not UTF-8 is read as unset. No variable beside the seven is read, so none
/// switches tracing on or changes the settings.
pub fn settings(env: &ProcessEnv, now_unix_nano: u64) -> TraceSettings {
    let Some(raw) = env.get(ENDPOINT) else {
        return TraceSettings::Off;
    };
    let endpoint = raw.to_str();
    if endpoint.is_some_and(|value| value.trim().is_empty()) {
        return TraceSettings::Off;
    }
    if text(env, KILL_SWITCH).is_some_and(|value| value.trim().eq_ignore_ascii_case("true")) {
        return TraceSettings::Off;
    }
    let Some(endpoint) = endpoint else {
        return TraceSettings::Unavailable(format!("the value of {ENDPOINT} is not UTF-8"));
    };
    let (headers, skipped_headers) = headers(text(env, HEADERS).unwrap_or_default());
    TraceSettings::On(TraceConfig {
        endpoint: format!("{}{EXPORT_PATH}", endpoint.trim().trim_end_matches('/')),
        headers,
        skipped_headers,
        resource: resource(
            text(env, SERVICE_NAME_VARIABLE),
            text(env, RESOURCE_ATTRIBUTES).unwrap_or_default(),
        ),
        traceparent: text(env, TRACEPARENT)
            .filter(|value| !value.is_empty())
            .map(str::to_owned),
        launch_time_unix_nano: text(env, LAUNCH_TIME)
            .and_then(|value| launch_time(value, now_unix_nano)),
    })
}

/// The name of the command span: the literal `winter`, then the command path, joined by single spaces.
pub fn span_name(command_path: &[String]) -> String {
    format!("winter {}", command_path.join(" "))
}

/// The port [`run`](crate::run) announces the command to: the host's tracer opens the command span there.
///
/// It takes `&self` because the host shares its tracer with the process boundary, which ends the span.
pub trait CommandTracer {
    /// Called at most once for an invocation, when a command name was resolved and logging is: before the
    /// command runs, and before a command's help or a usage error of a command alike. An invocation that ends
    /// at the root never calls it.
    fn command_started(&self, command_path: &[String], log: TraceLog);

    /// Opens an operation span, a child of the command span, that ends when the handle drops. With tracing
    /// off, or the command span not recording, it records nothing. The default body returns a span that
    /// records nothing, so a tracer that asserts nothing about operations needs no change.
    ///
    /// The attributes are names and values a command chose; none holds a path, an argument, or output.
    fn operation(
        &self,
        name: &'static str,
        attributes: &[(&'static str, &str)],
    ) -> Box<dyn OperationSpan + '_> {
        let _ = (name, attributes);
        Box::new(Unrecorded)
    }
}

/// An open operation span. Dropping it ends the span.
pub trait OperationSpan {
    /// Marks the span failed: an error status with no description, and the attribute `error.type` set to
    /// `error_type`.
    fn failed(&mut self, error_type: &'static str);
}

/// The operation span of a tracer that records nothing.
struct Unrecorded;

impl OperationSpan for Unrecorded {
    fn failed(&mut self, _: &'static str) {}
}

/// The tracer that does nothing: for a host or a test that asserts nothing about tracing.
pub struct NoTrace;

impl CommandTracer for NoTrace {
    fn command_started(&self, _: &[String], _: TraceLog) {}
}

/// The resolved logging of an invocation, as much of it as the host needs to report about tracing.
#[derive(Clone, Copy)]
pub struct TraceLog(Log);

impl TraceLog {
    pub(crate) fn new(log: Log) -> Self {
        Self(log)
    }

    /// Reports to `err` that the export of the command span failed, with its `cause`: an error record, written
    /// only when `-v` or a valid `WINTER_LOG_LEVEL` switched the record format.
    pub fn export_failed(self, err: &mut dyn Write, cause: &dyn Display) {
        self.report(
            err,
            Level::Error,
            &format!("the command span was not exported: {cause}"),
        );
    }

    /// Reports to `err` that tracing is on and cannot be carried out, with its `cause`: a debug record,
    /// written only when `-v` or a valid `WINTER_LOG_LEVEL` switched the record format.
    pub fn unavailable(self, err: &mut dyn Write, cause: &dyn Display) {
        self.report(
            err,
            Level::Debug,
            &format!("tracing is unavailable: {cause}"),
        );
    }

    /// Reports to `err` that the configured header `name` is not sent with the export, because its value is
    /// not legal in an HTTP header: a debug record, written only when `-v` or a valid `WINTER_LOG_LEVEL`
    /// switched the record format. The record holds the name and nothing of the value.
    pub fn header_skipped(self, err: &mut dyn Write, name: &str) {
        self.report(
            err,
            Level::Debug,
            &format!("the header {name} is not sent: its value is not legal in an HTTP header"),
        );
    }

    fn report(self, err: &mut dyn Write, level: Level, message: &str) {
        diagnostics::record(
            err,
            self.0,
            &Record {
                level,
                origin: ORIGIN,
                message,
                only_when_switched: true,
            },
        );
    }
}

/// The value of `key` as text, or `None` when it is unset or not UTF-8.
fn text<'a>(env: &'a ProcessEnv, key: &str) -> Option<&'a str> {
    env.get(key).and_then(|value| value.to_str())
}

/// The headers `value` lists that are sent, and the names of those that are not sent for their value.
///
/// The items are separated by `,`, each split at its first `=`, the name and the value percent-decoded and
/// trimmed, the name in lower case. An item with no `=`, with an empty name, or with a name that is no HTTP
/// token is skipped, and the others still apply. A name given twice keeps its first place and its last value.
///
/// A `,` always ends an item, one that stands unencoded inside a value included: what follows it is read as an
/// item of its own, so its text before the first `=` is taken for a name, and may be reported as one.
///
/// Two kinds of the pairs read so are left out of the first list, each judged by the value its name ended
/// with. The name `content-type` is left out and named nowhere: the export carries a content type of its own.
/// A pair whose value holds a byte that is not legal in an HTTP header is left out, and its name is in the
/// second list, in the order of the pairs.
fn headers(value: &str) -> (Vec<(String, String)>, Vec<String>) {
    let mut sent = parsed_headers(value);
    let mut skipped = Vec::new();
    sent.retain(|(name, value)| {
        if name == CONTENT_TYPE {
            return false;
        }
        let legal = value.bytes().all(is_header_value_byte);
        if !legal {
            skipped.push(name.clone());
        }
        legal
    });
    (sent, skipped)
}

/// Whether `byte` may stand in the value of an HTTP header, as the HTTP crate of the exporter judges it: the
/// tab and every byte from the space on, except the delete character. A byte above the ASCII range is legal.
fn is_header_value_byte(byte: u8) -> bool {
    byte == b'\t' || (byte >= 0x20 && byte != 0x7f)
}

/// Every header `value` lists, as [`headers`] reads the items, before any is left out.
fn parsed_headers(value: &str) -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    for item in value.split(',') {
        let Some((name, value)) = item.split_once('=') else {
            continue;
        };
        let name = percent_decoded(name).trim().to_ascii_lowercase();
        if name.is_empty() || !name.bytes().all(is_token_byte) {
            continue;
        }
        let value = percent_decoded(value).trim().to_owned();
        set(&mut pairs, name, value);
    }
    pairs
}

/// Whether `byte` may stand in the name of an HTTP header.
fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte)
}

/// The resource attributes: `service.name` first, then every other entry of `attributes`.
///
/// `attributes` holds `key=value` items separated by `,`; the key is trimmed, and the value is trimmed and
/// then percent-decoded. An item with no `=` or with an empty key is skipped, and a key given twice keeps its
/// first place and its last value. The service name is `service_name` when it is not empty, else the
/// `service.name` entry of `attributes` when that is not empty, else `winter`.
fn resource(service_name: Option<&str>, attributes: &str) -> Vec<(String, String)> {
    let mut entries = Vec::new();
    for item in attributes.split(',') {
        let Some((key, value)) = item.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        set(&mut entries, key.to_owned(), percent_decoded(value.trim()));
    }
    let listed = entries
        .iter()
        .position(|(key, _)| key == SERVICE_NAME)
        .map(|index| entries.remove(index).1);
    let name = service_name
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
        .or(listed.filter(|name| !name.is_empty()))
        .unwrap_or_else(|| DEFAULT_SERVICE_NAME.to_owned());
    entries.insert(0, (SERVICE_NAME.to_owned(), name));
    entries
}

/// Gives `key` the value `value` in `pairs`: in place when the key is held, at the end otherwise.
fn set(pairs: &mut Vec<(String, String)>, key: String, value: String) {
    match pairs.iter_mut().find(|(held, _)| *held == key) {
        Some(pair) => pair.1 = value,
        None => pairs.push((key, value)),
    }
}

/// `text` with every `%` that two hexadecimal digits follow replaced by the byte they spell. A `%` that no
/// such digits follow stays, and decoded bytes that are not UTF-8 become the replacement character.
fn percent_decoded(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let escaped = match bytes.get(index..index + 3) {
            Some([b'%', high, low]) => hex_digit(*high).zip(hex_digit(*low)),
            _ => None,
        };
        match escaped {
            Some((high, low)) => {
                decoded.push(high * 16 + low);
                index += 3;
            }
            None => {
                decoded.push(bytes[index]);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

fn hex_digit(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// The launch instant `value` names, in nanoseconds since the epoch.
///
/// The value is trimmed and each `,` is read as `.`; what remains is digits, optionally followed by `.` and
/// digits, and nothing else: seconds since the epoch, with a fraction that is cut or padded to nine digits.
/// Any other text, zero, an instant later than `now_unix_nano`, and one too large to hold give `None`.
fn launch_time(value: &str, now_unix_nano: u64) -> Option<u64> {
    let value = value.trim().replace(',', ".");
    let (seconds, fraction) = match value.split_once('.') {
        Some((seconds, fraction)) => (seconds, Some(fraction)),
        None => (value.as_str(), None),
    };
    if !is_digits(seconds) || fraction.is_some_and(|fraction| !is_digits(fraction)) {
        return None;
    }
    let nanoseconds: u64 = format!("{:0<9.9}", fraction.unwrap_or_default())
        .parse()
        .ok()?;
    let instant = seconds
        .parse::<u64>()
        .ok()?
        .checked_mul(NANOSECONDS_PER_SECOND)?
        .checked_add(nanoseconds)?;
    (instant != 0 && instant <= now_unix_nano).then_some(instant)
}

fn is_digits(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;

    use super::*;

    /// The instant every test reads the settings at: 2023-11-14T22:13:20Z.
    const NOW: u64 = 1_700_000_000_000_000_000;

    const COLLECTOR: (&str, &[u8]) = (ENDPOINT, b"http://c:4318");

    fn read(env: &[(&str, &[u8])]) -> TraceSettings {
        let env = ProcessEnv::from_vars(
            env.iter()
                .map(|(key, value)| (OsString::from(key), OsString::from_vec(value.to_vec()))),
        );
        settings(&env, NOW)
    }

    /// The configuration of an environment that turns tracing on.
    fn config(env: &[(&str, &[u8])]) -> TraceConfig {
        match read(env) {
            TraceSettings::On(config) => config,
            other => panic!("expected tracing on, got {other:?}"),
        }
    }

    /// The configuration of the collector's endpoint and one more variable.
    fn config_with(key: &str, value: &[u8]) -> TraceConfig {
        config(&[COLLECTOR, (key, value)])
    }

    fn pairs(literal: &[(&str, &str)]) -> Vec<(String, String)> {
        literal
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn an_endpoint_alone_turns_tracing_on_with_nothing_else_configured() {
        assert_eq!(
            read(&[COLLECTOR]),
            TraceSettings::On(TraceConfig {
                endpoint: "http://c:4318/v1/traces".to_owned(),
                headers: Vec::new(),
                skipped_headers: Vec::new(),
                resource: pairs(&[("service.name", "winter")]),
                traceparent: None,
                launch_time_unix_nano: None,
            })
        );
    }

    #[test]
    fn a_blank_endpoint_leaves_tracing_off() {
        assert_eq!(read(&[]), TraceSettings::Off);
        for blank in [&b""[..], b" ", b" \t\n "] {
            assert_eq!(read(&[(ENDPOINT, blank)]), TraceSettings::Off, "{blank:?}");
        }
    }

    #[test]
    fn the_kill_switch_turns_tracing_off_in_any_letter_case_and_with_blanks_around_it() {
        for value in [&b"true"[..], b"TRUE", b"True", b"  true\t", b" tRuE "] {
            assert_eq!(
                read(&[COLLECTOR, (KILL_SWITCH, value)]),
                TraceSettings::Off,
                "{value:?}"
            );
        }
    }

    #[test]
    fn any_other_value_of_the_kill_switch_leaves_tracing_on() {
        for value in [
            &b"1"[..],
            b"yes",
            b"false",
            b"",
            b"truely",
            b"tru\xff",
            b"\xff",
        ] {
            assert!(
                matches!(
                    read(&[COLLECTOR, (KILL_SWITCH, value)]),
                    TraceSettings::On(_)
                ),
                "{value:?}"
            );
        }
    }

    #[test]
    fn a_generic_exporter_setting_neither_turns_tracing_on_nor_changes_the_settings() {
        let generic: [(&str, &[u8]); 6] = [
            ("OTEL_EXPORTER_OTLP_ENDPOINT", b"http://generic:4318"),
            (
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                b"http://generic:4318/v1/traces",
            ),
            ("OTEL_EXPORTER_OTLP_HEADERS", b"authorization=generic"),
            ("OTEL_TRACES_SAMPLER", b"always_off"),
            ("OTEL_TRACES_EXPORTER", b"none"),
            ("TRACESTATE", b"vendor=value"),
        ];

        assert_eq!(read(&generic[..1]), TraceSettings::Off);
        assert_eq!(read(&generic), TraceSettings::Off);

        let mut with_endpoint = generic.to_vec();
        with_endpoint.push(COLLECTOR);
        assert_eq!(read(&with_endpoint), read(&[COLLECTOR]));
    }

    #[test]
    fn an_endpoint_that_is_not_utf_8_makes_tracing_unavailable_and_says_why() {
        let TraceSettings::Unavailable(why) = read(&[(ENDPOINT, b"http://c\xff:4318")]) else {
            panic!("expected tracing unavailable");
        };

        assert!(why.contains(ENDPOINT), "{why}");
        assert!(why.contains("UTF-8"), "{why}");
    }

    #[test]
    fn the_kill_switch_wins_over_an_endpoint_that_is_not_utf_8() {
        assert_eq!(
            read(&[(ENDPOINT, b"\xff"), (KILL_SWITCH, b"true")]),
            TraceSettings::Off
        );
    }

    #[test]
    fn trailing_slashes_of_the_endpoint_are_dropped_before_the_export_path() {
        for value in [
            &b"http://c:4318"[..],
            b"http://c:4318/",
            b"http://c:4318///",
        ] {
            assert_eq!(
                config(&[(ENDPOINT, value)]).endpoint,
                "http://c:4318/v1/traces",
                "{value:?}"
            );
        }
    }

    #[test]
    fn a_path_of_the_endpoint_is_kept_and_blanks_around_the_value_are_dropped() {
        assert_eq!(
            config(&[(ENDPOINT, b" http://c:4318/otlp/ ")]).endpoint,
            "http://c:4318/otlp/v1/traces"
        );
    }

    #[test]
    fn an_http_and_an_https_endpoint_both_turn_tracing_on_each_kept_as_given() {
        assert_eq!(
            config(&[(ENDPOINT, b"http://collector.example:4318")]).endpoint,
            "http://collector.example:4318/v1/traces"
        );
        assert_eq!(
            config(&[(ENDPOINT, b"https://collector.example")]).endpoint,
            "https://collector.example/v1/traces"
        );
    }

    #[test]
    fn headers_are_split_decoded_trimmed_and_named_in_lower_case() {
        assert_eq!(
            config_with(HEADERS, b"Authorization=Bearer%20abc%3D, x-tenant = blue ").headers,
            pairs(&[("authorization", "Bearer abc="), ("x-tenant", "blue")])
        );
    }

    #[test]
    fn a_header_value_keeps_every_equals_sign_after_the_first() {
        assert_eq!(
            config_with(HEADERS, b"x-key=a=b==").headers,
            pairs(&[("x-key", "a=b==")])
        );
    }

    #[test]
    fn a_header_item_that_cannot_be_read_is_skipped_and_the_next_applies() {
        assert_eq!(
            config_with(HEADERS, b"no-equals-sign,x-a=1").headers,
            pairs(&[("x-a", "1")])
        );
        assert_eq!(
            config_with(HEADERS, b"=empty,  =blank,x-a=1").headers,
            pairs(&[("x-a", "1")])
        );
        assert_eq!(
            config_with(HEADERS, b"bad name=1,bad%3Aname=2,x-a=1,").headers,
            pairs(&[("x-a", "1")])
        );
    }

    #[test]
    fn a_header_name_given_twice_keeps_its_first_place_and_its_last_value() {
        assert_eq!(
            config_with(HEADERS, b"x-a=1,x-b=2,X-A=3").headers,
            pairs(&[("x-a", "3"), ("x-b", "2")])
        );
    }

    #[test]
    fn a_header_name_and_a_header_value_are_trimmed_after_they_are_decoded() {
        assert_eq!(
            config_with(HEADERS, b"%20x-a%09=%20one%20two%20").headers,
            pairs(&[("x-a", "one two")])
        );
    }

    #[test]
    fn a_header_with_an_empty_value_is_kept() {
        assert_eq!(
            config_with(HEADERS, b"x-a=,x-b= ").headers,
            pairs(&[("x-a", ""), ("x-b", "")])
        );
    }

    #[test]
    fn blank_headers_and_headers_that_are_not_utf_8_give_none() {
        for value in [&b""[..], b"  ", b"x-a=1,x-b=\xff"] {
            assert_eq!(config_with(HEADERS, value).headers, Vec::new(), "{value:?}");
        }
    }

    #[test]
    fn a_configured_content_type_is_left_out_in_any_letter_case_and_named_nowhere() {
        for value in [
            &b"content-type=text/plain,x-a=1"[..],
            b"Content-Type=a,x-a=1",
            b"x-a=1,CONTENT-TYPE=a%0Ab",
        ] {
            let config = config_with(HEADERS, value);

            assert_eq!(config.headers, pairs(&[("x-a", "1")]), "{value:?}");
            assert_eq!(config.skipped_headers, Vec::<String>::new(), "{value:?}");
        }
    }

    #[test]
    fn a_header_whose_value_is_not_legal_is_left_out_and_named_and_the_others_still_apply() {
        for value in [
            &b"x-a=1,x-bad=a%0Ab,x-b=2"[..],
            b"x-a=1,x-bad=a%7Fb,x-b=2",
            b"x-a=1,x-bad=a%00b,x-b=2",
            b"x-a=1,x-bad=a%1Fb,x-b=2",
        ] {
            let config = config_with(HEADERS, value);

            assert_eq!(
                config.headers,
                pairs(&[("x-a", "1"), ("x-b", "2")]),
                "{value:?}"
            );
            assert_eq!(config.skipped_headers, ["x-bad"], "{value:?}");
        }
    }

    #[test]
    fn the_skipped_headers_are_named_in_the_order_they_were_configured() {
        let config = config_with(HEADERS, b"x-c=a%0Ab,x-a=1,x-b=%00%00");

        assert_eq!(config.headers, pairs(&[("x-a", "1")]));
        assert_eq!(config.skipped_headers, ["x-c", "x-b"]);
    }

    #[test]
    fn a_tab_and_bytes_above_the_ascii_range_are_legal_in_a_header_value() {
        let config = config_with(HEADERS, b"x-tab=a%09b,x-high=%C3%A9,x-space=a b~");

        assert_eq!(
            config.headers,
            pairs(&[("x-tab", "a\tb"), ("x-high", "\u{e9}"), ("x-space", "a b~")])
        );
        assert_eq!(config.headers[1].1.as_bytes(), [0xc3, 0xa9]);
        assert_eq!(config.skipped_headers, Vec::<String>::new());
    }

    #[test]
    fn the_last_value_of_a_header_name_given_twice_is_the_one_judged() {
        let legal_last = config_with(HEADERS, b"x-a=a%0Ab,x-a=ok");
        assert_eq!(legal_last.headers, pairs(&[("x-a", "ok")]));
        assert_eq!(legal_last.skipped_headers, Vec::<String>::new());

        let illegal_last = config_with(HEADERS, b"x-a=ok,x-b=1,x-a=a%0Ab");
        assert_eq!(illegal_last.headers, pairs(&[("x-b", "1")]));
        assert_eq!(illegal_last.skipped_headers, ["x-a"]);
    }

    #[test]
    fn a_skipped_header_is_one_debug_record_that_holds_its_name_under_a_switched_log_alone() {
        let written = |log: Log| {
            let mut err = Vec::new();
            TraceLog::new(log).header_skipped(&mut err, "x-bad");
            String::from_utf8(err).expect("UTF-8")
        };
        let switched = |threshold| Log {
            threshold,
            switched: true,
        };

        assert_eq!(
            written(switched(Level::Debug)),
            "DEBUG trace: the header x-bad is not sent: its value is not legal in an HTTP header\n"
        );
        assert_eq!(written(switched(Level::Info)), "");
        assert_eq!(written(switched(Level::Error)), "");
        assert_eq!(written(Log::DEFAULT), "");
        assert_eq!(
            written(Log {
                threshold: Level::Debug,
                switched: false,
            }),
            ""
        );
    }

    #[test]
    fn a_percent_sign_that_spells_no_byte_stays_and_bytes_that_are_not_utf_8_are_replaced() {
        assert_eq!(percent_decoded("100%"), "100%");
        assert_eq!(percent_decoded("%zz%4"), "%zz%4");
        assert_eq!(percent_decoded("%41%62%2c"), "Ab,");
        assert_eq!(percent_decoded("%C3%A9"), "\u{e9}");
        assert_eq!(percent_decoded("%ff"), "\u{fffd}");
        assert_eq!(percent_decoded("a+b"), "a+b");
    }

    #[test]
    fn a_traceparent_is_carried_as_given_sampled_or_not() {
        for value in [
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00",
            " not a traceparent ",
        ] {
            assert_eq!(
                config_with(TRACEPARENT, value.as_bytes()).traceparent,
                Some(value.to_owned())
            );
        }
    }

    #[test]
    fn an_absent_an_empty_and_a_non_utf_8_traceparent_each_give_none() {
        assert_eq!(config(&[COLLECTOR]).traceparent, None);
        assert_eq!(config_with(TRACEPARENT, b"").traceparent, None);
        assert_eq!(config_with(TRACEPARENT, b"00-\xff-01").traceparent, None);
    }

    #[test]
    fn a_launch_time_is_epoch_seconds_with_a_fraction_cut_or_padded_to_nine_digits() {
        for (value, expected) in [
            (&b"1699999999"[..], 1_699_999_999_000_000_000),
            (b"1699999999.5", 1_699_999_999_500_000_000),
            (b"1699999999,25", 1_699_999_999_250_000_000),
            (b"1699999999.123456789", 1_699_999_999_123_456_789),
            (b"1699999999.1234567899999", 1_699_999_999_123_456_789),
            (b"  1699999999.000000001\n", 1_699_999_999_000_000_001),
            (b"0.000000001", 1),
            (b"1700000000", NOW),
            (b"1700000000.0000000009", NOW),
        ] {
            assert_eq!(
                config_with(LAUNCH_TIME, value).launch_time_unix_nano,
                Some(expected),
                "{value:?}"
            );
        }
    }

    #[test]
    fn a_launch_time_that_is_zero_later_than_the_instant_or_no_number_is_ignored() {
        assert_eq!(config(&[COLLECTOR]).launch_time_unix_nano, None);
        for value in [
            &b"0"[..],
            b"0.0",
            b"0,000000000",
            b"1700000000.000000001",
            b"1700000001",
            b"99999999999999999999999999",
            b"soon",
            b"",
            b"1699999999.",
            b".5",
            b"1.2.3",
            b"1,2,3",
            b"-1",
            b"+1",
            b"1e9",
            b"16999 99999",
            b"1699999999\xff",
        ] {
            assert_eq!(
                config_with(LAUNCH_TIME, value).launch_time_unix_nano,
                None,
                "{value:?}"
            );
        }
    }

    #[test]
    fn the_service_name_variable_wins_over_the_resource_attributes_which_win_over_winter() {
        let attributes: (&str, &[u8]) = (
            RESOURCE_ATTRIBUTES,
            b"deployment.environment=dev,service.name=listed,host.name=box",
        );
        let others = [("deployment.environment", "dev"), ("host.name", "box")];

        let named = config(&[COLLECTOR, (SERVICE_NAME_VARIABLE, b"named"), attributes]);
        let listed = config(&[COLLECTOR, attributes]);
        let neither = config(&[
            COLLECTOR,
            (
                RESOURCE_ATTRIBUTES,
                b"deployment.environment=dev,host.name=box",
            ),
        ]);

        for (config, name) in [(named, "named"), (listed, "listed"), (neither, "winter")] {
            let mut expected = pairs(&[("service.name", name)]);
            expected.extend(pairs(&others));
            assert_eq!(config.resource, expected, "{name}");
        }
    }

    #[test]
    fn an_empty_or_non_utf_8_service_name_falls_through_to_the_next_source() {
        assert_eq!(
            config(&[
                COLLECTOR,
                (SERVICE_NAME_VARIABLE, b""),
                (RESOURCE_ATTRIBUTES, b"service.name=listed"),
            ])
            .resource,
            pairs(&[("service.name", "listed")])
        );
        assert_eq!(
            config(&[
                COLLECTOR,
                (SERVICE_NAME_VARIABLE, b"\xff"),
                (RESOURCE_ATTRIBUTES, b"service.name=,k=v"),
            ])
            .resource,
            pairs(&[("service.name", "winter"), ("k", "v")])
        );
        assert_eq!(
            config_with(RESOURCE_ATTRIBUTES, b"k=v,service.name=\xff").resource,
            pairs(&[("service.name", "winter")])
        );
    }

    #[test]
    fn resource_attributes_are_trimmed_their_values_decoded_and_unreadable_items_skipped() {
        assert_eq!(
            config_with(
                RESOURCE_ATTRIBUTES,
                b" team = core%20tools ,no-equals-sign,=nameless,a=1,a=b=2,"
            )
            .resource,
            pairs(&[
                ("service.name", "winter"),
                ("team", "core tools"),
                ("a", "b=2"),
            ])
        );
    }

    #[test]
    fn a_resource_key_is_not_decoded_and_a_resource_value_is_trimmed_before_it_is_decoded() {
        assert_eq!(
            config_with(RESOURCE_ATTRIBUTES, b" a%2Eb = %20v%20 ").resource,
            pairs(&[("service.name", "winter"), ("a%2Eb", " v ")])
        );
    }

    #[test]
    fn a_resource_key_given_twice_keeps_its_first_place_and_its_last_value() {
        assert_eq!(
            config_with(RESOURCE_ATTRIBUTES, b"a=1,b=2,a=3").resource,
            pairs(&[("service.name", "winter"), ("a", "3"), ("b", "2")])
        );
    }

    #[test]
    fn a_service_name_of_blanks_alone_is_not_empty_and_is_the_service_name() {
        assert_eq!(
            config(&[
                COLLECTOR,
                (SERVICE_NAME_VARIABLE, b" "),
                (RESOURCE_ATTRIBUTES, b"service.name=listed"),
            ])
            .resource,
            pairs(&[("service.name", " ")])
        );
        assert_eq!(
            config_with(RESOURCE_ATTRIBUTES, b"service.name=%20").resource,
            pairs(&[("service.name", " ")])
        );
    }

    #[test]
    fn the_span_name_is_winter_and_the_command_path_joined_by_single_spaces() {
        assert_eq!(
            span_name(&["ws".to_owned(), "index".to_owned()]),
            "winter ws index"
        );
        assert_eq!(span_name(&["status".to_owned()]), "winter status");
    }
}
