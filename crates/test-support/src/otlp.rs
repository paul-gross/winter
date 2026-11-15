//! What a test sees of a trace export: a decoder of the export request's body, and a collector that listens
//! on the loopback address and records what it is sent.
//!
//! The decoder is written by hand from the field numbers of the OTLP trace messages and stands on no tracing
//! crate, so a test compares what an exporter wrote with what it should have written, not an encoder with
//! its own decoder.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// One export request, read as a single resource: the attributes of its first resource, the scope of its
/// first scope entry, and the spans of every entry.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ReceivedExport {
    /// The string-valued attributes of the resource; an attribute of another kind is skipped.
    pub resource: Vec<(String, String)>,
    /// The name of the instrumentation scope.
    pub scope: String,
    pub spans: Vec<ReceivedSpan>,
}

/// One span of an export.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ReceivedSpan {
    pub trace_id: Vec<u8>,
    pub span_id: Vec<u8>,
    /// Empty for the root of a trace.
    pub parent_span_id: Vec<u8>,
    pub name: String,
    pub kind: u64,
    pub start_unix_nano: u64,
    pub end_unix_nano: u64,
    /// The string-valued attributes; an attribute of another kind is skipped.
    pub attributes: Vec<(String, String)>,
    /// The code and the message of the status, or `None` when the span carries no status.
    pub status: Option<(u64, String)>,
}

/// Decodes the protobuf body of an OTLP trace export request.
///
/// It reads these fields and skips every other by its wire type, whatever it holds:
///
/// ```text
/// ExportTraceServiceRequest   1 resource_spans
/// ResourceSpans               1 resource   2 scope_spans
/// Resource                    1 attributes
/// ScopeSpans                  1 scope   2 spans
/// InstrumentationScope        1 name
/// Span                        1 trace_id   2 span_id   4 parent_span_id   5 name   6 kind
///                             7 start_time_unix_nano   8 end_time_unix_nano   9 attributes   15 status
/// Status                      2 message   3 code
/// KeyValue                    1 key   2 value
/// AnyValue                    1 string_value
/// ```
///
/// The two times are 64-bit fixed-width fields, `kind` and `code` are varints, and every other field read is
/// length-delimited. A field with one of these numbers and another wire type is skipped like an unknown one.
///
/// The error says what could not be read: a message that ends inside a field, a length that reaches past
/// the end of its message, a wire type the format does not have, a field with the number 0, a group that is
/// not closed by its own end, a varint longer than ten bytes, or a string that is not UTF-8.
pub fn decode_export(body: &[u8]) -> Result<ReceivedExport, String> {
    let mut resources = Vec::new();
    each_field(body, |number, value| {
        if let (1, Value::Bytes(message)) = (number, value) {
            resources.push(resource_spans(message)?);
        }
        Ok(())
    })?;

    let mut export = ReceivedExport::default();
    let mut scopes = Vec::new();
    for (index, (resource, scopes_of_resource)) in resources.into_iter().enumerate() {
        if index == 0 {
            export.resource = resource;
        }
        scopes.extend(scopes_of_resource);
    }
    for (index, (scope, spans)) in scopes.into_iter().enumerate() {
        if index == 0 {
            export.scope = scope;
        }
        export.spans.extend(spans);
    }
    Ok(export)
}

/// The value of one field, as its wire type holds it.
enum Value<'a> {
    Varint(u64),
    Fixed64(u64),
    Bytes(&'a [u8]),
    /// A value nothing reads: 32 bits of fixed width, or a group.
    Skipped,
}

/// The fields of one message that are still to be read.
struct Fields<'a> {
    rest: &'a [u8],
}

impl<'a> Fields<'a> {
    /// The number and the wire type of the next field.
    fn tag(&mut self) -> Result<(u64, u8), String> {
        let tag = self.varint()?;
        let number = tag >> 3;
        if number == 0 {
            return Err("a field has the number 0, which the format does not have".to_owned());
        }
        Ok((number, (tag & 7) as u8))
    }

    /// The value of the field `number`, whose tag was just read with `wire_type`.
    fn value(&mut self, number: u64, wire_type: u8) -> Result<Value<'a>, String> {
        match wire_type {
            0 => Ok(Value::Varint(self.varint()?)),
            1 => {
                let mut bytes = [0; 8];
                bytes.copy_from_slice(self.take(8)?);
                Ok(Value::Fixed64(u64::from_le_bytes(bytes)))
            }
            2 => {
                let length = self.varint()?;
                Ok(Value::Bytes(self.take(length)?))
            }
            3 => {
                self.skip_group(number)?;
                Ok(Value::Skipped)
            }
            4 => Err(format!("field {number} ends a group that was not started")),
            5 => {
                self.take(4)?;
                Ok(Value::Skipped)
            }
            _ => Err(format!(
                "field {number} has wire type {wire_type}, which the format does not have"
            )),
        }
    }

    /// Reads to the end of the group that field `number` started.
    ///
    /// The groups inside it are kept in a list of the open ones, not followed by a call each, so no depth of
    /// nesting exhausts the stack.
    fn skip_group(&mut self, number: u64) -> Result<(), String> {
        let mut open = vec![number];
        while let Some(&innermost) = open.last() {
            match self.tag()? {
                (started, 3) => open.push(started),
                (ended, 4) if ended == innermost => {
                    open.pop();
                }
                (ended, 4) => {
                    return Err(format!(
                        "field {ended} ends a group inside the group of field {innermost}"
                    ));
                }
                (inner, wire_type) => {
                    self.value(inner, wire_type)?;
                }
            }
        }
        Ok(())
    }

    fn varint(&mut self) -> Result<u64, String> {
        let mut value = 0;
        for index in 0..10 {
            let byte = self.take(1)?[0];
            value |= u64::from(byte & 0x7f) << (7 * index);
            if byte & 0x80 == 0 {
                return Ok(value);
            }
        }
        Err("a varint is longer than ten bytes".to_owned())
    }

    fn take(&mut self, length: u64) -> Result<&'a [u8], String> {
        let remaining = self.rest.len();
        let taken = usize::try_from(length)
            .ok()
            .and_then(|length| self.rest.split_at_checked(length));
        let Some((taken, rest)) = taken else {
            return Err(format!(
                "the message is truncated: {length} bytes are needed and {remaining} remain"
            ));
        };
        self.rest = rest;
        Ok(taken)
    }
}

/// Calls `read` with the number and the value of every field of `message`, in order.
fn each_field<'a>(
    message: &'a [u8],
    mut read: impl FnMut(u64, Value<'a>) -> Result<(), String>,
) -> Result<(), String> {
    let mut fields = Fields { rest: message };
    while !fields.rest.is_empty() {
        let (number, wire_type) = fields.tag()?;
        let value = fields.value(number, wire_type)?;
        read(number, value)?;
    }
    Ok(())
}

fn string(bytes: &[u8]) -> Result<String, String> {
    match std::str::from_utf8(bytes) {
        Ok(text) => Ok(text.to_owned()),
        Err(_) => Err(format!("a string is not UTF-8: {bytes:?}")),
    }
}

/// The string-valued attributes of a resource or of a span.
type Attributes = Vec<(String, String)>;

/// A scope's name and its spans.
type Scope = (String, Vec<ReceivedSpan>);

/// The attributes of the resource, and the scopes below it.
fn resource_spans(message: &[u8]) -> Result<(Attributes, Vec<Scope>), String> {
    let mut resource = Vec::new();
    let mut scopes = Vec::new();
    each_field(message, |number, value| {
        match (number, value) {
            (1, Value::Bytes(message)) => resource = attributes_of_resource(message)?,
            (2, Value::Bytes(message)) => scopes.push(scope_spans(message)?),
            _ => {}
        }
        Ok(())
    })?;
    Ok((resource, scopes))
}

fn attributes_of_resource(message: &[u8]) -> Result<Attributes, String> {
    let mut attributes = Vec::new();
    each_field(message, |number, value| {
        if let (1, Value::Bytes(message)) = (number, value) {
            attributes.extend(key_value(message)?);
        }
        Ok(())
    })?;
    Ok(attributes)
}

fn scope_spans(message: &[u8]) -> Result<Scope, String> {
    let mut name = String::new();
    let mut spans = Vec::new();
    each_field(message, |number, value| {
        match (number, value) {
            (1, Value::Bytes(message)) => name = scope_name(message)?,
            (2, Value::Bytes(message)) => spans.push(span(message)?),
            _ => {}
        }
        Ok(())
    })?;
    Ok((name, spans))
}

fn scope_name(message: &[u8]) -> Result<String, String> {
    let mut name = String::new();
    each_field(message, |number, value| {
        if let (1, Value::Bytes(bytes)) = (number, value) {
            name = string(bytes)?;
        }
        Ok(())
    })?;
    Ok(name)
}

fn span(message: &[u8]) -> Result<ReceivedSpan, String> {
    let mut span = ReceivedSpan::default();
    each_field(message, |number, value| {
        match (number, value) {
            (1, Value::Bytes(bytes)) => span.trace_id = bytes.to_vec(),
            (2, Value::Bytes(bytes)) => span.span_id = bytes.to_vec(),
            (4, Value::Bytes(bytes)) => span.parent_span_id = bytes.to_vec(),
            (5, Value::Bytes(bytes)) => span.name = string(bytes)?,
            (6, Value::Varint(kind)) => span.kind = kind,
            (7, Value::Fixed64(instant)) => span.start_unix_nano = instant,
            (8, Value::Fixed64(instant)) => span.end_unix_nano = instant,
            (9, Value::Bytes(message)) => span.attributes.extend(key_value(message)?),
            (15, Value::Bytes(message)) => span.status = Some(status(message)?),
            _ => {}
        }
        Ok(())
    })?;
    Ok(span)
}

fn status(message: &[u8]) -> Result<(u64, String), String> {
    let mut status = (0, String::new());
    each_field(message, |number, value| {
        match (number, value) {
            (2, Value::Bytes(bytes)) => status.1 = string(bytes)?,
            (3, Value::Varint(code)) => status.0 = code,
            _ => {}
        }
        Ok(())
    })?;
    Ok(status)
}

/// The key and the string value of one attribute, or `None` when its value is of another kind.
fn key_value(message: &[u8]) -> Result<Option<(String, String)>, String> {
    let mut key = String::new();
    let mut text = None;
    each_field(message, |number, value| {
        match (number, value) {
            (1, Value::Bytes(bytes)) => key = string(bytes)?,
            (2, Value::Bytes(message)) => text = string_value(message)?,
            _ => {}
        }
        Ok(())
    })?;
    Ok(text.map(|text| (key, text)))
}

fn string_value(message: &[u8]) -> Result<Option<String>, String> {
    let mut text = None;
    each_field(message, |number, value| {
        if let (1, Value::Bytes(bytes)) = (number, value) {
            text = Some(string(bytes)?);
        }
        Ok(())
    })?;
    Ok(text)
}

/// One HTTP request a [`Collector`] read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedRequest {
    pub method: String,
    pub target: String,
    /// The headers in the order they were sent, each name in lower case.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// What a [`Collector`] does with a request it has read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Answer {
    /// Status 200 with an empty body.
    Ok,
    /// This status with an empty body.
    Status(u16),
    /// No answer: the connection is kept open and silent until the collector is stopped or dropped.
    Hang,
}

/// How long the listener waits before it asks again for a connection.
const ACCEPT_PAUSE: Duration = Duration::from_millis(1);
/// How long one connection is read for, from its acceptance.
const READ_LIMIT: Duration = Duration::from_secs(5);
/// How long a stopped collector goes on serving the connections that were made before the stop.
const DRAIN_LIMIT: Duration = Duration::from_secs(5);
/// The most bytes a request line and its headers may take.
const HEAD_LIMIT: usize = 64 * 1024;
/// The most bytes a body may take.
const BODY_LIMIT: usize = 16 * 1024 * 1024;
const HEAD_END: &[u8] = b"\r\n\r\n";

/// A stand-in for a trace collector: a listener on a port of 127.0.0.1 the system chooses, served by one
/// thread in the test process.
///
/// It binds the loopback address and nothing else, resolves no name, and holds no file. The thread takes one
/// connection at a time and reads one HTTP/1.1 request from it: the request line, the headers, and a body of
/// `Content-Length` bytes, within five seconds of accepting the connection. A request it cannot read that
/// way, a TLS handshake or a chunked body for one, counts as a connection, adds no request, and is closed
/// unanswered.
///
/// Stopping the collector, through [`Collector::requests`] or a drop, first serves the connections that were
/// made before the stop, for five seconds at most, then joins the thread, which closes the listener and every
/// connection it held. A client that goes on connecting therefore does not keep a stopped collector alive.
/// A panic of the thread is raised again in the thread that stops the collector: always by
/// [`Collector::requests`], and by a drop unless its thread is already unwinding from a panic, in which case
/// the thread's panic is discarded.
pub struct Collector {
    port: u16,
    stop: Arc<AtomicBool>,
    connections: Arc<AtomicUsize>,
    thread: Option<JoinHandle<Vec<ReceivedRequest>>>,
}

impl Collector {
    /// Starts a collector that gives `answer` to every request it reads.
    pub fn start(answer: Answer) -> std::io::Result<Self> {
        Self::start_with(
            answer,
            Limits {
                read: READ_LIMIT,
                drain: DRAIN_LIMIT,
            },
        )
    }

    /// Starts a collector that reads and drains within `limits`.
    fn start_with(answer: Answer, limits: Limits) -> std::io::Result<Self> {
        let listener = loopback_listener()?;
        listener.set_nonblocking(true)?;
        let port = listener.local_addr()?.port();
        let stop = Arc::new(AtomicBool::new(false));
        let connections = Arc::new(AtomicUsize::new(0));
        let thread = std::thread::Builder::new()
            .name("otlp-collector".to_owned())
            .spawn({
                let stop = Arc::clone(&stop);
                let connections = Arc::clone(&connections);
                move || listen(&listener, answer, limits, &stop, &connections)
            })?;
        Ok(Self {
            port,
            stop,
            connections,
            thread: Some(thread),
        })
    }

    /// The base address of the collector: `http://127.0.0.1:<port>`.
    pub fn endpoint(&self) -> String {
        endpoint_of(self.port)
    }

    /// How many connections were accepted so far, whatever was read from them.
    pub fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }

    /// Stops the collector and answers every request it read, in the order their connections were accepted.
    pub fn requests(mut self) -> Vec<ReceivedRequest> {
        match self.stopped() {
            Some(Ok(requests)) => requests,
            Some(Err(panic)) => std::panic::resume_unwind(panic),
            None => Vec::new(),
        }
    }

    /// An address of the same form as [`Collector::endpoint`] at which nothing listens: a loopback port that
    /// was bound and released again, so a connection to it is refused.
    pub fn refused_endpoint() -> std::io::Result<String> {
        let listener = loopback_listener()?;
        Ok(endpoint_of(listener.local_addr()?.port()))
    }

    /// Asks the thread to stop and waits for it; `None` when that was done before.
    fn stopped(&mut self) -> Option<std::thread::Result<Vec<ReceivedRequest>>> {
        self.stop.store(true, Ordering::SeqCst);
        self.thread.take().map(JoinHandle::join)
    }
}

impl Drop for Collector {
    fn drop(&mut self) {
        if let Some(Err(panic)) = self.stopped() {
            // A second panic while one unwinds would abort the process.
            if !std::thread::panicking() {
                std::panic::resume_unwind(panic);
            }
        }
    }
}

/// A listener on a port of 127.0.0.1 that the system chooses.
fn loopback_listener() -> std::io::Result<TcpListener> {
    TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
}

fn endpoint_of(port: u16) -> String {
    format!("http://{}:{port}", Ipv4Addr::LOCALHOST)
}

/// The time bounds of one collector.
#[derive(Clone, Copy)]
struct Limits {
    /// How long one connection is read for, from its acceptance.
    read: Duration,
    /// How long connections are still served for, from the moment the thread sees the stop.
    drain: Duration,
}

/// Serves connections until `stop` is set and none is waiting, or until `limits.drain` after the stop was
/// seen, and answers the requests that were read.
///
/// The connections of [`Answer::Hang`] are held until this returns.
fn listen(
    listener: &TcpListener,
    answer: Answer,
    limits: Limits,
    stop: &AtomicBool,
    connections: &AtomicUsize,
) -> Vec<ReceivedRequest> {
    let mut requests = Vec::new();
    let mut held = Vec::new();
    let mut drained_by = None;
    loop {
        // Read before the listener is asked: a connection made before the stop is then found waiting, and is
        // served unless `limits.drain` has passed since the stop was first seen here.
        let stopping = stop.load(Ordering::SeqCst);
        if stopping {
            let drained_by = *drained_by.get_or_insert_with(|| Instant::now() + limits.drain);
            if Instant::now() >= drained_by {
                break;
            }
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                connections.fetch_add(1, Ordering::SeqCst);
                let deadline = Instant::now() + limits.read;
                let deadline = drained_by.map_or(deadline, |drained_by| deadline.min(drained_by));
                let Some(request) = read_request(&mut stream, deadline) else {
                    continue;
                };
                requests.push(request);
                match answer {
                    Answer::Ok => respond(&mut stream, 200, "OK"),
                    Answer::Status(status) => respond(&mut stream, status, "Status"),
                    Answer::Hang => held.push(stream),
                }
            }
            Err(_) if stopping => break,
            Err(_) => std::thread::sleep(ACCEPT_PAUSE),
        }
    }
    requests
}

/// Writes a response with `status` and no body. A client that is gone is no failure of the collector.
fn respond(stream: &mut TcpStream, status: u16, reason: &str) {
    let response =
        format!("HTTP/1.1 {status} {reason}\r\ncontent-length: 0\r\nconnection: close\r\n\r\n");
    let _ = stream.write_all(response.as_bytes());
}

/// The one request of a connection, or `None` when what arrived before `deadline` is no such request.
fn read_request(stream: &mut TcpStream, deadline: Instant) -> Option<ReceivedRequest> {
    stream.set_nonblocking(false).ok()?;

    let mut received = Vec::new();
    let head_length = loop {
        let end = received
            .windows(HEAD_END.len())
            .position(|window| window == HEAD_END);
        if let Some(end) = end {
            break end;
        }
        if received.len() > HEAD_LIMIT {
            return None;
        }
        read_more(stream, &mut received, deadline)?;
    };
    let mut lines = std::str::from_utf8(&received[..head_length])
        .ok()?
        .split("\r\n");

    let mut request_line = lines.next()?.split(' ');
    let method = request_line.next().filter(|method| !method.is_empty())?;
    let target = request_line.next().filter(|target| !target.is_empty())?;
    let version = request_line.next()?;
    if !version.starts_with("HTTP/1.") || request_line.next().is_some() {
        return None;
    }

    let mut headers = Vec::new();
    for line in lines {
        let (name, value) = line.split_once(':')?;
        if name.is_empty() || name.contains(char::is_whitespace) {
            return None;
        }
        headers.push((name.to_ascii_lowercase(), value.trim().to_owned()));
    }
    let header = |name: &str| {
        headers
            .iter()
            .find(|(held, _)| held == name)
            .map(|(_, value)| value.as_str())
    };
    if header("transfer-encoding").is_some() {
        return None;
    }
    let length = match header("content-length") {
        Some(length) => length.parse().ok().filter(|length| *length <= BODY_LIMIT)?,
        None => 0,
    };

    let mut body = received[head_length + HEAD_END.len()..].to_vec();
    while body.len() < length {
        read_more(stream, &mut body, deadline)?;
    }
    body.truncate(length);

    Some(ReceivedRequest {
        method: method.to_owned(),
        target: target.to_owned(),
        headers,
        body,
    })
}

/// Appends what the connection gives next, or answers `None` at its end, on an error, and past `deadline`.
fn read_more(stream: &mut TcpStream, into: &mut Vec<u8>, deadline: Instant) -> Option<()> {
    let remaining = deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())?;
    stream.set_read_timeout(Some(remaining)).ok()?;
    let mut chunk = [0; 8192];
    match stream.read(&mut chunk) {
        Ok(0) => None,
        Ok(count) => {
            into.extend_from_slice(&chunk[..count]);
            Some(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => Some(()),
        Err(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TRACE_ID: [u8; 16] = [
        0x0a, 0xf7, 0x65, 0x19, 0x16, 0xcd, 0x43, 0xdd, 0x84, 0x48, 0xeb, 0x21, 0x1c, 0x80, 0x31,
        0x9c,
    ];
    const SPAN_ID: [u8; 8] = [0xb7, 0xad, 0x6b, 0x71, 0x69, 0x20, 0x33, 0x31];
    const CHILD_ID: [u8; 8] = [0x00, 0xf0, 0x67, 0xaa, 0x0b, 0xa9, 0x02, 0xb7];

    /// 1 700 000 000 000 000 000 and a quarter of a second later, each as the eight bytes of a 64-bit
    /// fixed-width field, lowest byte first.
    const START: u64 = 1_700_000_000_000_000_000;
    const START_BYTES: [u8; 8] = [0x00, 0x00, 0x2a, 0x36, 0xfe, 0x9c, 0x97, 0x17];
    const END: u64 = 1_700_000_000_250_000_000;
    const END_BYTES: [u8; 8] = [0x80, 0xb2, 0x10, 0x45, 0xfe, 0x9c, 0x97, 0x17];

    fn pairs(literal: &[(&str, &str)]) -> Vec<(String, String)> {
        literal
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    /// An export of one resource named `winter`, one scope named `winter`, and a root span with one
    /// attribute and no status. Each tag is followed by the length its field was counted to by hand.
    fn root_span_export() -> Vec<u8> {
        [
            // ExportTraceServiceRequest: 1 resource_spans, 144 bytes
            &[0x0a, 0x90, 0x01][..],
            // ResourceSpans: 1 resource, 26 bytes
            &[0x0a, 0x1a],
            // Resource: 1 attributes, 24 bytes; KeyValue: 1 key, 12 bytes
            &[0x0a, 0x18, 0x0a, 0x0c],
            b"service.name",
            // KeyValue: 2 value, 8 bytes; AnyValue: 1 string_value, 6 bytes
            &[0x12, 0x08, 0x0a, 0x06],
            b"winter",
            // ResourceSpans: 2 scope_spans, 114 bytes
            &[0x12, 0x72],
            // ScopeSpans: 1 scope, 8 bytes; InstrumentationScope: 1 name, 6 bytes
            &[0x0a, 0x08, 0x0a, 0x06],
            b"winter",
            // ScopeSpans: 2 spans, 102 bytes
            &[0x12, 0x66],
            // Span: 1 trace_id, 16 bytes
            &[0x0a, 0x10],
            &TRACE_ID,
            // Span: 2 span_id, 8 bytes
            &[0x12, 0x08],
            &SPAN_ID,
            // Span: 5 name, 15 bytes
            &[0x2a, 0x0f],
            b"winter ws index",
            // Span: 6 kind, 1
            &[0x30, 0x01],
            // Span: 7 start_time_unix_nano
            &[0x39],
            &START_BYTES,
            // Span: 8 end_time_unix_nano
            &[0x41],
            &END_BYTES,
            // Span: 9 attributes, 35 bytes; KeyValue: 1 key, 14 bytes
            &[0x4a, 0x23, 0x0a, 0x0e],
            b"winter.command",
            // KeyValue: 2 value, 17 bytes; AnyValue: 1 string_value, 15 bytes
            &[0x12, 0x11, 0x0a, 0x0f],
            b"winter ws index",
        ]
        .concat()
    }

    /// An export with no resource and no scope, and a child span with two attributes and the status code 2
    /// with no message.
    fn failed_child_span_export() -> Vec<u8> {
        [
            // ExportTraceServiceRequest: 1 resource_spans, 152 bytes
            &[0x0a, 0x98, 0x01][..],
            // ResourceSpans: 2 scope_spans, 149 bytes
            &[0x12, 0x95, 0x01],
            // ScopeSpans: 2 spans, 146 bytes
            &[0x12, 0x92, 0x01],
            // Span: 1 trace_id, 16 bytes
            &[0x0a, 0x10],
            &TRACE_ID,
            // Span: 2 span_id, 8 bytes
            &[0x12, 0x08],
            &CHILD_ID,
            // Span: 4 parent_span_id, 8 bytes
            &[0x22, 0x08],
            &SPAN_ID,
            // Span: 5 name, 18 bytes
            &[0x2a, 0x12],
            b"winter repo remove",
            // Span: 6 kind, 1
            &[0x30, 0x01],
            // Span: 7 start_time_unix_nano
            &[0x39],
            &START_BYTES,
            // Span: 8 end_time_unix_nano
            &[0x41],
            &END_BYTES,
            // Span: 9 attributes, 38 bytes; KeyValue: 1 key, 14 bytes
            &[0x4a, 0x26, 0x0a, 0x0e],
            b"winter.command",
            // KeyValue: 2 value, 20 bytes; AnyValue: 1 string_value, 18 bytes
            &[0x12, 0x14, 0x0a, 0x12],
            b"winter repo remove",
            // Span: 9 attributes, 22 bytes; KeyValue: 1 key, 10 bytes
            &[0x4a, 0x16, 0x0a, 0x0a],
            b"error.type",
            // KeyValue: 2 value, 8 bytes; AnyValue: 1 string_value, 6 bytes
            &[0x12, 0x08, 0x0a, 0x06],
            b"Config",
            // Span: 15 status, 2 bytes; Status: 3 code, 2
            &[0x7a, 0x02, 0x18, 0x02],
        ]
        .concat()
    }

    /// An export that holds, beside what the decoder reads, fields of every wire type that it does not:
    /// varints, strings, a 32-bit fixed-width field, a group, and attribute values that are no strings.
    fn export_with_fields_the_decoder_does_not_read() -> Vec<u8> {
        [
            // ExportTraceServiceRequest: 1 resource_spans, 209 bytes
            &[0x0a, 0xd1, 0x01][..],
            // ResourceSpans: 1 resource, 47 bytes
            &[0x0a, 0x2f],
            // Resource: 1 attributes, 24 bytes; KeyValue: 1 key, 12 bytes
            &[0x0a, 0x18, 0x0a, 0x0c],
            b"service.name",
            // KeyValue: 2 value, 8 bytes; AnyValue: 1 string_value, 6 bytes
            &[0x12, 0x08, 0x0a, 0x06],
            b"winter",
            // Resource: 1 attributes, 17 bytes; KeyValue: 1 key, 11 bytes
            &[0x0a, 0x11, 0x0a, 0x0b],
            b"process.pid",
            // KeyValue: 2 value, 2 bytes; AnyValue: 3 int_value, 42
            &[0x12, 0x02, 0x18, 0x2a],
            // Resource: 2 dropped_attributes_count, 0
            &[0x10, 0x00],
            // ResourceSpans: 2 scope_spans, 155 bytes
            &[0x12, 0x9b, 0x01],
            // ScopeSpans: 1 scope, 15 bytes; InstrumentationScope: 1 name, 6 bytes
            &[0x0a, 0x0f, 0x0a, 0x06],
            b"winter",
            // InstrumentationScope: 2 version, 5 bytes
            &[0x12, 0x05],
            b"0.1.0",
            // ScopeSpans: 2 spans, 133 bytes
            &[0x12, 0x85, 0x01],
            // Span: 1 trace_id, 16 bytes
            &[0x0a, 0x10],
            &TRACE_ID,
            // Span: 2 span_id, 8 bytes
            &[0x12, 0x08],
            &SPAN_ID,
            // Span: 3 trace_state, 3 bytes
            &[0x1a, 0x03],
            b"a=b",
            // Span: 5 name, 13 bytes
            &[0x2a, 0x0d],
            b"winter status",
            // Span: 6 kind, 1
            &[0x30, 0x01],
            // Span: 7 start_time_unix_nano
            &[0x39],
            &START_BYTES,
            // Span: 8 end_time_unix_nano
            &[0x41],
            &END_BYTES,
            // Span: 9 attributes, 33 bytes; KeyValue: 1 key, 14 bytes
            &[0x4a, 0x21, 0x0a, 0x0e],
            b"winter.command",
            // KeyValue: 2 value, 15 bytes; AnyValue: 1 string_value, 13 bytes
            &[0x12, 0x0f, 0x0a, 0x0d],
            b"winter status",
            // Span: 9 attributes, 14 bytes; KeyValue: 1 key, 8 bytes
            &[0x4a, 0x0e, 0x0a, 0x08],
            b"attempts",
            // KeyValue: 2 value, 2 bytes; AnyValue: 3 int_value, 3
            &[0x12, 0x02, 0x18, 0x03],
            // Span: 10 dropped_attributes_count, 0
            &[0x50, 0x00],
            // Span: 16 flags, 32 bits of fixed width
            &[0x85, 0x01, 0x01, 0x01, 0x00, 0x00],
            // Span: a group as field 20 that holds field 1, a varint, and its end
            &[0xa3, 0x01, 0x08, 0x01, 0xa4, 0x01],
            // ScopeSpans: 3 schema_url, empty
            &[0x1a, 0x00],
            // ResourceSpans: 3 schema_url, empty
            &[0x1a, 0x00],
            // ExportTraceServiceRequest: a field 2 it does not have, a varint
            &[0x10, 0x07],
        ]
        .concat()
    }

    #[test]
    fn a_root_span_with_one_attribute_and_no_status_is_read() {
        assert_eq!(
            decode_export(&root_span_export()),
            Ok(ReceivedExport {
                resource: pairs(&[("service.name", "winter")]),
                scope: "winter".to_owned(),
                spans: vec![ReceivedSpan {
                    trace_id: TRACE_ID.to_vec(),
                    span_id: SPAN_ID.to_vec(),
                    parent_span_id: Vec::new(),
                    name: "winter ws index".to_owned(),
                    kind: 1,
                    start_unix_nano: START,
                    end_unix_nano: END,
                    attributes: pairs(&[("winter.command", "winter ws index")]),
                    status: None,
                }],
            })
        );
    }

    #[test]
    fn a_child_span_with_two_attributes_and_an_error_status_without_a_message_is_read() {
        assert_eq!(
            decode_export(&failed_child_span_export()),
            Ok(ReceivedExport {
                resource: Vec::new(),
                scope: String::new(),
                spans: vec![ReceivedSpan {
                    trace_id: TRACE_ID.to_vec(),
                    span_id: CHILD_ID.to_vec(),
                    parent_span_id: SPAN_ID.to_vec(),
                    name: "winter repo remove".to_owned(),
                    kind: 1,
                    start_unix_nano: START,
                    end_unix_nano: END,
                    attributes: pairs(&[
                        ("winter.command", "winter repo remove"),
                        ("error.type", "Config"),
                    ]),
                    status: Some((2, String::new())),
                }],
            })
        );
    }

    #[test]
    fn a_status_message_is_read() {
        // Span: 15 status, 6 bytes; Status: 2 message, 2 bytes; 3 code, 2
        let span: &[u8] = &[0x7a, 0x06, 0x12, 0x02, b'n', b'o', 0x18, 0x02];
        // ExportTraceServiceRequest: 1 resource_spans; ResourceSpans: 2 scope_spans; ScopeSpans: 2 spans
        let body = [&[0x0a, 0x0c, 0x12, 0x0a, 0x12, 0x08][..], span].concat();

        let export = decode_export(&body).expect("an export");

        assert_eq!(export.spans.len(), 1);
        assert_eq!(export.spans[0].status, Some((2, "no".to_owned())));
    }

    #[test]
    fn fields_the_decoder_does_not_read_are_skipped_whatever_their_wire_type() {
        assert_eq!(
            decode_export(&export_with_fields_the_decoder_does_not_read()),
            Ok(ReceivedExport {
                resource: pairs(&[("service.name", "winter")]),
                scope: "winter".to_owned(),
                spans: vec![ReceivedSpan {
                    trace_id: TRACE_ID.to_vec(),
                    span_id: SPAN_ID.to_vec(),
                    parent_span_id: Vec::new(),
                    name: "winter status".to_owned(),
                    kind: 1,
                    start_unix_nano: START,
                    end_unix_nano: END,
                    attributes: pairs(&[("winter.command", "winter status")]),
                    status: None,
                }],
            })
        );
    }

    #[test]
    fn an_export_of_several_resources_has_the_attributes_of_the_first_and_the_spans_of_all() {
        // Two requests one after the other are one request with the entries of both.
        let body = [root_span_export(), failed_child_span_export()].concat();

        let export = decode_export(&body).expect("an export");

        assert_eq!(export.resource, pairs(&[("service.name", "winter")]));
        assert_eq!(export.scope, "winter");
        let names: Vec<&str> = export.spans.iter().map(|span| span.name.as_str()).collect();
        assert_eq!(names, ["winter ws index", "winter repo remove"]);
    }

    #[test]
    fn an_empty_body_is_an_export_of_nothing() {
        assert_eq!(decode_export(&[]), Ok(ReceivedExport::default()));
    }

    #[test]
    fn a_truncated_message_is_an_error() {
        let whole = root_span_export();

        for length in 1..whole.len() {
            let result = decode_export(&whole[..length]);

            assert!(result.is_err(), "{length} bytes: {result:?}");
        }
    }

    #[test]
    fn a_length_past_the_end_of_its_message_is_an_error() {
        // ExportTraceServiceRequest: 1 resource_spans, 2 bytes; ResourceSpans: 1 resource, 5 bytes of which
        // none is there
        let result = decode_export(&[0x0a, 0x02, 0x0a, 0x05]);

        let error = result.expect_err("an error");
        assert!(error.contains("truncated"), "{error}");
    }

    #[test]
    fn a_wire_type_the_format_does_not_have_is_an_error() {
        for wire_type in [6, 7] {
            // Field 1 with that wire type.
            let result = decode_export(&[0x08 | wire_type, 0x00]);

            let error = result.expect_err("an error");
            assert!(error.contains("wire type"), "{error}");
        }
    }

    #[test]
    fn a_group_that_is_not_closed_by_its_own_end_is_an_error() {
        // Field 2 ends a group that nothing started.
        assert!(decode_export(&[0x14]).is_err());
        // Field 2 starts a group, and field 3 ends one.
        assert!(decode_export(&[0x13, 0x1c]).is_err());
        // Field 2 starts a group, and the message ends.
        assert!(decode_export(&[0x13, 0x08, 0x01]).is_err());
        // Field 1 starts a group, field 2 starts one inside it, and field 1 ends: the outer group, not the
        // innermost.
        let error = decode_export(&[0x0b, 0x13, 0x0c]).expect_err("an error");
        assert!(error.contains("ends a group inside"), "{error}");
    }

    #[test]
    fn a_varint_longer_than_ten_bytes_is_an_error() {
        // Field 2, a varint whose eleventh byte would be the last.
        let mut body = vec![0x10];
        body.extend([0x80; 10]);
        body.push(0x01);

        assert!(decode_export(&body).is_err());
    }

    #[test]
    fn a_string_that_is_not_utf_8_is_an_error() {
        // ExportTraceServiceRequest: 1 resource_spans; ResourceSpans: 2 scope_spans; ScopeSpans: 2 spans;
        // Span: 5 name, the one byte 0xff
        let result = decode_export(&[0x0a, 0x07, 0x12, 0x05, 0x12, 0x03, 0x2a, 0x01, 0xff]);

        let error = result.expect_err("an error");
        assert!(error.contains("UTF-8"), "{error}");
    }

    /// An export that holds, at every level, a field the decoder does not read of every wire type, and whose
    /// span has an attribute value of every kind, a parent, and a status with a message. The comments give
    /// each field's number and, for a nested message, the length its fields add up to.
    fn export_with_values_of_every_kind() -> Vec<u8> {
        [
            // unknown varint, a two-byte tag: field 1000 varint, value 300
            &[0xc0, 0x3e, 0xac, 0x02][..],
            // resource_spans: field 1, length 476
            &[0x0a, 0xdc, 0x03],
            // unknown 32-bit: field 9 32-bit, 7 little-endian
            &[0x4d, 0x07, 0x00, 0x00, 0x00],
            // resource: field 1, length 69
            &[0x0a, 0x45],
            // dropped_attributes_count: field 2 varint, value 1
            &[0x10, 0x01],
            // KeyValue: field 1, length 25
            &[0x0a, 0x19],
            // key: field 1, length 12, "service.name"
            &[
                0x0a, 0x0c, 0x73, 0x65, 0x72, 0x76, 0x69, 0x63, 0x65, 0x2e, 0x6e, 0x61, 0x6d, 0x65,
            ],
            // value AnyValue: field 2, length 9
            &[0x12, 0x09],
            // string_value: field 1, length 7, "svc one"
            &[0x0a, 0x07, 0x73, 0x76, 0x63, 0x20, 0x6f, 0x6e, 0x65],
            // KeyValue: field 1, length 18
            &[0x0a, 0x12],
            // key: field 1, length 11, "process.pid"
            &[
                0x0a, 0x0b, 0x70, 0x72, 0x6f, 0x63, 0x65, 0x73, 0x73, 0x2e, 0x70, 0x69, 0x64,
            ],
            // value AnyValue: field 2, length 3
            &[0x12, 0x03],
            // int_value: field 3 varint, value 4242
            &[0x18, 0x92, 0x21],
            // KeyValue: field 1, length 18
            &[0x0a, 0x12],
            // key: field 1, length 9, "host.name"
            &[
                0x0a, 0x09, 0x68, 0x6f, 0x73, 0x74, 0x2e, 0x6e, 0x61, 0x6d, 0x65,
            ],
            // value AnyValue: field 2, length 5
            &[0x12, 0x05],
            // string_value: field 1, length 3, "box"
            &[0x0a, 0x03, 0x62, 0x6f, 0x78],
            // scope_spans: field 2, length 395
            &[0x12, 0x8b, 0x03],
            // scope: field 1, length 37
            &[0x0a, 0x25],
            // version: field 2, length 1, "9"
            &[0x12, 0x01, 0x39],
            // name: field 1, length 5, "probe"
            &[0x0a, 0x05, 0x70, 0x72, 0x6f, 0x62, 0x65],
            // KeyValue: field 3, length 23
            &[0x1a, 0x17],
            // key: field 1, length 10, "scope.attr"
            &[
                0x0a, 0x0a, 0x73, 0x63, 0x6f, 0x70, 0x65, 0x2e, 0x61, 0x74, 0x74, 0x72,
            ],
            // value AnyValue: field 2, length 9
            &[0x12, 0x09],
            // string_value: field 1, length 7, "ignored"
            &[0x0a, 0x07, 0x69, 0x67, 0x6e, 0x6f, 0x72, 0x65, 0x64],
            // dropped_attributes_count: field 4 varint, value 0
            &[0x20, 0x00],
            // span: field 2, length 350
            &[0x12, 0xde, 0x02],
            // name: field 5, length 16, "winter repo list"
            &[
                0x2a, 0x10, 0x77, 0x69, 0x6e, 0x74, 0x65, 0x72, 0x20, 0x72, 0x65, 0x70, 0x6f, 0x20,
                0x6c, 0x69, 0x73, 0x74,
            ],
            // trace_id: field 1, length 16, 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f 10
            &[
                0x0a, 0x10, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c,
                0x0d, 0x0e, 0x0f, 0x10,
            ],
            // span_id: field 2, length 8, a1 a2 a3 a4 a5 a6 a7 a8
            &[0x12, 0x08, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8],
            // parent_span_id: field 4, length 8, b1 b2 b3 b4 b5 b6 b7 b8
            &[0x22, 0x08, 0xb1, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8],
            // trace_state: field 3, length 3, "k=v"
            &[0x1a, 0x03, 0x6b, 0x3d, 0x76],
            // kind: field 6 varint, value 1
            &[0x30, 0x01],
            // start: field 7 64-bit, 72623859790382856 little-endian
            &[0x39, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01],
            // end: field 8 64-bit, 72623859790382857 little-endian
            &[0x41, 0x09, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01],
            // KeyValue: field 9, length 36
            &[0x4a, 0x24],
            // key: field 1, length 14, "winter.command"
            &[
                0x0a, 0x0e, 0x77, 0x69, 0x6e, 0x74, 0x65, 0x72, 0x2e, 0x63, 0x6f, 0x6d, 0x6d, 0x61,
                0x6e, 0x64,
            ],
            // value AnyValue: field 2, length 18
            &[0x12, 0x12],
            // string_value: field 1, length 16, "winter repo list"
            &[
                0x0a, 0x10, 0x77, 0x69, 0x6e, 0x74, 0x65, 0x72, 0x20, 0x72, 0x65, 0x70, 0x6f, 0x20,
                0x6c, 0x69, 0x73, 0x74,
            ],
            // KeyValue: field 9, length 10
            &[0x4a, 0x0a],
            // key: field 1, length 4, "flag"
            &[0x0a, 0x04, 0x66, 0x6c, 0x61, 0x67],
            // value AnyValue: field 2, length 2
            &[0x12, 0x02],
            // bool_value: field 2 varint, value 1
            &[0x10, 0x01],
            // KeyValue: field 9, length 12
            &[0x4a, 0x0c],
            // key: field 1, length 5, "count"
            &[0x0a, 0x05, 0x63, 0x6f, 0x75, 0x6e, 0x74],
            // value AnyValue: field 2, length 3
            &[0x12, 0x03],
            // int_value: field 3 varint, value 300
            &[0x18, 0xac, 0x02],
            // KeyValue: field 9, length 18
            &[0x4a, 0x12],
            // key: field 1, length 5, "ratio"
            &[0x0a, 0x05, 0x72, 0x61, 0x74, 0x69, 0x6f],
            // value AnyValue: field 2, length 9
            &[0x12, 0x09],
            // double_value: field 4 64-bit, the double 0.5 little-endian
            &[0x21, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xe0, 0x3f],
            // KeyValue: field 9, length 15
            &[0x4a, 0x0f],
            // key: field 1, length 4, "list"
            &[0x0a, 0x04, 0x6c, 0x69, 0x73, 0x74],
            // value AnyValue: field 2, length 7
            &[0x12, 0x07],
            // array_value: field 5, length 5
            &[0x2a, 0x05],
            // values AnyValue: field 1, length 3
            &[0x0a, 0x03],
            // string_value: field 1, length 1, "x"
            &[0x0a, 0x01, 0x78],
            // KeyValue: field 9, length 19
            &[0x4a, 0x13],
            // key: field 1, length 3, "map"
            &[0x0a, 0x03, 0x6d, 0x61, 0x70],
            // value AnyValue: field 2, length 12
            &[0x12, 0x0c],
            // kvlist_value: field 6, length 10
            &[0x32, 0x0a],
            // KeyValue: field 1, length 8
            &[0x0a, 0x08],
            // key: field 1, length 1, "k"
            &[0x0a, 0x01, 0x6b],
            // value AnyValue: field 2, length 3
            &[0x12, 0x03],
            // string_value: field 1, length 1, "v"
            &[0x0a, 0x01, 0x76],
            // KeyValue: field 9, length 11
            &[0x4a, 0x0b],
            // key: field 1, length 3, "raw"
            &[0x0a, 0x03, 0x72, 0x61, 0x77],
            // value AnyValue: field 2, length 4
            &[0x12, 0x04],
            // bytes_value: field 7, length 2, ff 00
            &[0x3a, 0x02, 0xff, 0x00],
            // KeyValue: field 9, length 7
            &[0x4a, 0x07],
            // key: field 1, length 5, "unset"
            &[0x0a, 0x05, 0x75, 0x6e, 0x73, 0x65, 0x74],
            // KeyValue: field 9, length 9
            &[0x4a, 0x09],
            // key: field 1, length 5, "empty"
            &[0x0a, 0x05, 0x65, 0x6d, 0x70, 0x74, 0x79],
            // value AnyValue: field 2, length 0
            &[0x12, 0x00],
            // KeyValue: field 9, length 21
            &[0x4a, 0x15],
            // key: field 1, length 10, "error.type"
            &[
                0x0a, 0x0a, 0x65, 0x72, 0x72, 0x6f, 0x72, 0x2e, 0x74, 0x79, 0x70, 0x65,
            ],
            // value AnyValue: field 2, length 7
            &[0x12, 0x07],
            // string_value: field 1, length 5, "Usage"
            &[0x0a, 0x05, 0x55, 0x73, 0x61, 0x67, 0x65],
            // dropped_attributes_count: field 10 varint, value 0
            &[0x50, 0x00],
            // event: field 11, length 12
            &[0x5a, 0x0c],
            // time: field 1 64-bit, 5 little-endian
            &[0x09, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            // name: field 2, length 1, "e"
            &[0x12, 0x01, 0x65],
            // dropped_events_count: field 12 varint, value 0
            &[0x60, 0x00],
            // dropped_links_count: field 14 varint, value 0
            &[0x70, 0x00],
            // status: field 15, length 8
            &[0x7a, 0x08],
            // code: field 3 varint, value 2
            &[0x18, 0x02],
            // message: field 2, length 4, "boom"
            &[0x12, 0x04, 0x62, 0x6f, 0x6f, 0x6d],
            // flags: field 16 32-bit, 257 little-endian
            &[0x85, 0x01, 0x01, 0x01, 0x00, 0x00],
            // unknown varint: field 100 varint, value 16384
            &[0xa0, 0x06, 0x80, 0x80, 0x01],
            // unknown 64-bit: field 101 64-bit, 18446744073709551615 little-endian
            &[0xa9, 0x06, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff],
            // unknown: field 102 length-delimited, length 3, 0a ff 80
            &[0xb2, 0x06, 0x03, 0x0a, 0xff, 0x80],
            // unknown group: field 103 group start
            &[0xbb, 0x06],
            // a: field 1 varint, value 1
            &[0x08, 0x01],
            // nested group: field 104 group start
            &[0xc3, 0x06],
            // b: field 1 32-bit, 1 little-endian
            &[0x0d, 0x01, 0x00, 0x00, 0x00],
            // nested group: field 104 group end
            &[0xc4, 0x06],
            // c: field 2, length 2, 1c 24
            &[0x12, 0x02, 0x1c, 0x24],
            // d: field 3 64-bit, 2 little-endian
            &[0x19, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            // unknown group: field 103 group end
            &[0xbc, 0x06],
            // unknown 32-bit: field 105 32-bit, 4294967295 little-endian
            &[0xcd, 0x06, 0xff, 0xff, 0xff, 0xff],
            // schema_url: field 3, length 1, "u"
            &[0x1a, 0x01, 0x75],
            // schema_url: field 3, length 0, ""
            &[0x1a, 0x00],
            // unknown 64-bit: field 1001 64-bit, 1 little-endian
            &[0xc9, 0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            // unknown: field 1002 length-delimited, length 2, "zz"
            &[0xd2, 0x3e, 0x02, 0x7a, 0x7a],
            // unknown group: field 1003 group start
            &[0xdb, 0x3e],
            // unknown group: field 1003 group end
            &[0xdc, 0x3e],
            // unknown 32-bit: field 1004 32-bit, 2 little-endian
            &[0xe5, 0x3e, 0x02, 0x00, 0x00, 0x00],
        ]
        .concat()
    }

    /// The lengths at which a cut of that export falls between two fields of the request, and so leaves a
    /// whole message: before each of its six fields, and at its end.
    const CUTS_BETWEEN_FIELDS: [usize; 7] = [0, 4, 483, 493, 498, 502, 508];

    /// An export of two resources and three scopes, the first resource without attributes, whose fields come
    /// in another order than their numbers: a kind that fills a varint of ten bytes, a length written in one
    /// byte more than it needs, numbers of the table under another wire type, and a status with a code alone
    /// and one with nothing.
    fn export_of_two_resources_out_of_order() -> Vec<u8> {
        [
            // resource_spans: field 1, length 80
            &[0x0a, 0x50][..],
            // scope_spans: field 2, length 71
            &[0x12, 0x47],
            // span: field 2, length 64
            &[0x12, 0x40],
            // kind: field 6 varint, value 18446744073709551615
            &[
                0x30, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01,
            ],
            // name: field 5, length 5, "first"
            &[0x2a, 0x05, 0x66, 0x69, 0x72, 0x73, 0x74],
            // span_id: field 2, length 8, a1 a2 a3 a4 a5 a6 a7 a8
            &[0x12, 0x08, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8],
            // trace_id: field 1, length 16, 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f 10
            &[
                0x0a, 0x10, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c,
                0x0d, 0x0e, 0x0f, 0x10,
            ],
            // end: field 8 64-bit, 2 little-endian
            &[0x41, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            // start: field 7 64-bit, 1 little-endian
            &[0x39, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            // scope: field 1, length 3
            &[0x0a, 0x03],
            // name: field 1, length 1, "a"
            &[0x0a, 0x01, 0x61],
            // scope_spans: field 2, length 5
            &[0x12, 0x05],
            // scope: field 1, length 3
            &[0x0a, 0x03],
            // name: field 1, length 1, "b"
            &[0x0a, 0x01, 0x62],
            // resource_spans: field 1, length 76
            &[0x0a, 0x4c],
            // scope_spans: field 2, length 46
            &[0x12, 0x2e],
            // scope: field 1, length 3
            &[0x0a, 0x03],
            // name: field 1, length 1, "c"
            &[0x0a, 0x01, 0x63],
            // span: field 2, length 12
            &[0x12, 0x0c],
            // name: field 5, length 6, "second"
            &[0x2a, 0x06, 0x73, 0x65, 0x63, 0x6f, 0x6e, 0x64],
            // status: field 15, length 2
            &[0x7a, 0x02],
            // code: field 3 varint, value 1
            &[0x18, 0x01],
            // span: field 2, length 25
            &[0x12, 0x19],
            // name: field 5, length 5 written in two bytes 85 00, "third": 2a 85 00 74 68 69 72 64
            &[0x2a, 0x85, 0x00, 0x74, 0x68, 0x69, 0x72, 0x64],
            // status: field 15, length 0
            &[0x7a, 0x00],
            // a varint under the number of name: field 5 varint, value 9
            &[0x28, 0x09],
            // bytes under the number of kind: field 6, length 2, "no"
            &[0x32, 0x02, 0x6e, 0x6f],
            // a varint under the number of start: field 7 varint, value 3
            &[0x38, 0x03],
            // attribute KeyValue with a varint value: field 9, length 5, 0a 01 6b 10 01
            &[0x4a, 0x05, 0x0a, 0x01, 0x6b, 0x10, 0x01],
            // resource: field 1, length 26
            &[0x0a, 0x1a],
            // KeyValue: field 1, length 24
            &[0x0a, 0x18],
            // key: field 1, length 12, "service.name"
            &[
                0x0a, 0x0c, 0x73, 0x65, 0x72, 0x76, 0x69, 0x63, 0x65, 0x2e, 0x6e, 0x61, 0x6d, 0x65,
            ],
            // value AnyValue: field 2, length 8
            &[0x12, 0x08],
            // string_value: field 1, length 6, "second"
            &[0x0a, 0x06, 0x73, 0x65, 0x63, 0x6f, 0x6e, 0x64],
        ]
        .concat()
    }

    #[test]
    fn values_of_every_kind_and_fields_of_every_wire_type_are_read_through() {
        assert_eq!(
            decode_export(&export_with_values_of_every_kind()),
            Ok(ReceivedExport {
                resource: pairs(&[("service.name", "svc one"), ("host.name", "box")]),
                scope: "probe".to_owned(),
                spans: vec![ReceivedSpan {
                    trace_id: (1..=16).collect(),
                    span_id: (0xa1..=0xa8).collect(),
                    parent_span_id: (0xb1..=0xb8).collect(),
                    name: "winter repo list".to_owned(),
                    kind: 1,
                    start_unix_nano: 0x0102_0304_0506_0708,
                    end_unix_nano: 0x0102_0304_0506_0709,
                    attributes: pairs(&[
                        ("winter.command", "winter repo list"),
                        ("error.type", "Usage"),
                    ]),
                    status: Some((2, "boom".to_owned())),
                }],
            })
        );
    }

    #[test]
    fn a_cut_is_an_error_exactly_when_it_falls_inside_a_field() {
        let whole = export_with_values_of_every_kind();
        assert_eq!(whole.len(), CUTS_BETWEEN_FIELDS[6]);

        for length in 0..=whole.len() {
            let result = decode_export(&whole[..length]);

            assert_eq!(
                result.is_ok(),
                CUTS_BETWEEN_FIELDS.contains(&length),
                "{length} bytes: {result:?}"
            );
        }
    }

    #[test]
    fn fields_in_any_order_and_under_another_wire_type_are_read_or_skipped() {
        let root = ReceivedSpan {
            trace_id: (1..=16).collect(),
            span_id: (0xa1..=0xa8).collect(),
            name: "first".to_owned(),
            kind: u64::MAX,
            start_unix_nano: 1,
            end_unix_nano: 2,
            ..ReceivedSpan::default()
        };
        let with_a_code = ReceivedSpan {
            name: "second".to_owned(),
            status: Some((1, String::new())),
            ..ReceivedSpan::default()
        };
        let with_an_empty_status = ReceivedSpan {
            name: "third".to_owned(),
            status: Some((0, String::new())),
            ..ReceivedSpan::default()
        };

        assert_eq!(
            decode_export(&export_of_two_resources_out_of_order()),
            Ok(ReceivedExport {
                resource: Vec::new(),
                scope: "a".to_owned(),
                spans: vec![root, with_a_code, with_an_empty_status],
            })
        );
    }

    #[test]
    fn a_varint_of_eleven_bytes_is_an_error_as_a_tag_as_a_length_and_as_a_value() {
        // Ten bytes that each announce another, and an eleventh that would end the varint.
        let eleven_bytes = [[0x80; 10].as_slice(), &[0x01]].concat();
        let as_a_tag = eleven_bytes.clone();
        // ExportTraceServiceRequest: 1 resource_spans, and its length
        let as_a_length = [&[0x0a][..], &eleven_bytes].concat();
        // ExportTraceServiceRequest: 1 resource_spans, 16 bytes; ResourceSpans: 2 scope_spans, 14 bytes;
        // ScopeSpans: 2 spans, 12 bytes; Span: 6 kind, and its value
        let as_a_value = [
            &[0x0a, 0x10, 0x12, 0x0e, 0x12, 0x0c, 0x30][..],
            &eleven_bytes,
        ]
        .concat();

        for body in [as_a_tag, as_a_length, as_a_value] {
            let error = decode_export(&body).expect_err("an error");

            assert!(error.contains("varint"), "{body:?}: {error}");
        }
    }

    #[test]
    fn a_field_with_the_number_0_is_an_error() {
        // Field 0, a varint.
        let error = decode_export(&[0x00, 0x00]).expect_err("an error");
        assert!(error.contains("number 0"), "{error}");
        // ExportTraceServiceRequest: 1 resource_spans, 5 bytes; ResourceSpans: field 0, 32 bits wide
        let error =
            decode_export(&[0x0a, 0x05, 0x05, 0x00, 0x00, 0x00, 0x00]).expect_err("an error");
        assert!(error.contains("number 0"), "{error}");
        // Field 2 starts a group that holds field 0, a varint, and ends.
        let error = decode_export(&[0x13, 0x00, 0x00, 0x14]).expect_err("an error");
        assert!(error.contains("number 0"), "{error}");
    }

    /// Enough groups, each inside the one before, to exhaust the stack of a decoder that calls itself once
    /// for each.
    const NESTED_GROUPS: usize = 300_000;

    #[test]
    fn groups_nested_far_deeper_than_the_stack_are_skipped() {
        // Field 1 starts a group, that many times over, and ends each.
        let body = [[0x0b].repeat(NESTED_GROUPS), [0x0c].repeat(NESTED_GROUPS)].concat();

        assert_eq!(decode_export(&body), Ok(ReceivedExport::default()));
    }

    #[test]
    fn groups_nested_far_deeper_than_the_stack_and_not_closed_are_an_error() {
        let never_ended = [0x0b].repeat(NESTED_GROUPS);
        // The innermost group is ended as field 2.
        let ended_by_another = [never_ended.as_slice(), &[0x14]].concat();
        let one_end_short = [
            [0x0b].repeat(NESTED_GROUPS),
            [0x0c].repeat(NESTED_GROUPS - 1),
        ]
        .concat();

        for body in [never_ended, ended_by_another, one_end_short] {
            assert!(decode_export(&body).is_err());
        }
    }

    #[test]
    fn the_listener_is_bound_to_the_loopback_address_alone_on_a_port_the_system_chose() {
        let listener = loopback_listener().expect("a listener");

        let address = listener.local_addr().expect("an address");

        assert_eq!(address.ip(), std::net::IpAddr::V4(Ipv4Addr::LOCALHOST));
        assert_ne!(address.port(), 0);
    }

    /// Longer than a collector takes to give a connection up or to stop; a wait this long fails its test.
    const PATIENCE: Duration = Duration::from_secs(30);

    /// A collector that gives a connection up after a twentieth of a second, and that drains for a tenth
    /// of a second once it is stopped.
    fn impatient_collector() -> Collector {
        let limits = Limits {
            read: Duration::from_millis(50),
            drain: Duration::from_millis(100),
        };
        Collector::start_with(Answer::Ok, limits).expect("a collector")
    }

    fn client_of(collector: &Collector) -> TcpStream {
        let stream = TcpStream::connect(SocketAddrV4::new(Ipv4Addr::LOCALHOST, collector.port))
            .expect("a connection");
        stream
            .set_read_timeout(Some(PATIENCE))
            .expect("a read deadline");
        stream
    }

    /// What the collector sends on `client` until it closes the connection.
    fn answer_on(mut client: TcpStream) -> Vec<u8> {
        let mut answer = Vec::new();
        client
            .read_to_end(&mut answer)
            .expect("the connection closed");
        answer
    }

    #[test]
    fn a_client_that_sends_nothing_is_given_up_at_the_read_limit() {
        let collector = impatient_collector();
        let client = client_of(&collector);

        // The client keeps its side open and silent: only the collector's limit ends the read.
        assert_eq!(answer_on(client), b"");
        assert_eq!(collector.connections(), 1);
        assert_eq!(collector.requests(), []);
    }

    #[test]
    fn a_client_that_stalls_inside_a_request_is_given_up_at_the_read_limit() {
        let inside_the_head: &[u8] = b"POST /v1/traces HTTP/1.1\r\nContent-Le";
        let inside_the_body: &[u8] = b"POST /v1/traces HTTP/1.1\r\nContent-Length: 10\r\n\r\nhalf";

        for sent in [inside_the_head, inside_the_body] {
            let collector = impatient_collector();
            let mut client = client_of(&collector);
            client.write_all(sent).expect("bytes sent");

            assert_eq!(answer_on(client), b"");
            assert_eq!(collector.connections(), 1);
            assert_eq!(collector.requests(), []);
        }
    }

    #[test]
    fn a_client_that_keeps_connecting_does_not_outlast_a_stop() {
        // The collector's loop serves a listener of the test's own, which stays bound until the client has
        // ended: every connection the client makes or tries goes to it, whether the loop still serves or not.
        let listener = loopback_listener().expect("a listener");
        listener
            .set_nonblocking(true)
            .expect("a listener that does not block");
        let address = listener.local_addr().expect("an address");
        let limits = Limits {
            read: Duration::from_millis(50),
            drain: Duration::from_millis(100),
        };
        // The stop is there from the start, with one connection waiting, and the client makes more for as
        // long as it may; each stays open and silent, so the loop is never without one to serve.
        let stop = AtomicBool::new(true);
        let connections = AtomicUsize::new(0);
        let done = AtomicBool::new(false);
        let first = TcpStream::connect(address).expect("a connection");
        let (ended, end) = std::sync::mpsc::channel();

        let outcome = std::thread::scope(|scope| {
            scope.spawn(|| {
                let mut open = vec![first];
                while !done.load(Ordering::SeqCst) {
                    open.extend(TcpStream::connect_timeout(
                        &address,
                        Duration::from_millis(10),
                    ));
                }
            });
            scope.spawn(|| {
                let requests = listen(&listener, Answer::Ok, limits, &stop, &connections);
                let _ = ended.send(requests);
            });

            let outcome = end.recv_timeout(PATIENCE);
            done.store(true, Ordering::SeqCst);
            outcome
        });

        let requests = outcome.expect("the loop ended while the client was still connecting");
        assert_eq!(requests, []);
        assert!(connections.load(Ordering::SeqCst) >= 1);
    }
}
