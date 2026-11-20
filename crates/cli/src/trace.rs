//! The binary's tracer: the command span, its end, and its one export to a trace collector.
//!
//! Nothing of the tracing stack exists before the library announces a command, and nothing ever does when the
//! settings do not turn tracing on. Beside the process boundary this is the one file of the binary that touches
//! the process environment: [`hide_generic_exporter_variables`] removes variables from it.
//!
//! It is also the one file of the binary that starts a thread: [`Tracer::finish`] ends and exports an open
//! span on the thread `trace-export`, and waits for that thread no longer than [`EXPORT_CAP`]. A thread that
//! has not answered by then is left to itself and ends with the process, so nothing here or in `main` may wait
//! for it afterwards, by a join or by dropping something that thread owns.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::panic::{self, AssertUnwindSafe};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;
use std::time::{Duration, SystemTime};

use opentelemetry::propagation::TextMapPropagator;
use opentelemetry::trace::{
    Span as _, SpanId, SpanKind, Status, TraceContextExt as _, Tracer as _, TracerProvider as _,
};
use opentelemetry::{Context, KeyValue};
use opentelemetry_otlp::{Protocol, RetryPolicy, WithExportConfig, WithHttpConfig};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::{
    Sampler, SdkTracerProvider, Span, SpanData, SpanExporter, SpanLimits,
};
use winter_workspace_core::{
    CommandTracer, OperationSpan, ProcessEnv, TraceConfig, TraceLog, TraceSettings, span_name,
};

/// The longest [`Tracer::finish`] waits for the export thread, from the start of that wait: the bound on what
/// an export costs an invocation beside the start of that thread, whatever the thread is doing when it runs
/// out.
const EXPORT_CAP: Duration = Duration::from_millis(100);

/// The timeout the exporter is given. It ends an ordinary slow export with the exporter's own cause, and it is
/// no bound on the export: the exporter's HTTP client starts it afresh with every read of a response's body.
const EXPORT_TIMEOUT: Duration = EXPORT_CAP;

/// The name of the thread that ends and exports the span.
const EXPORT_THREAD: &str = "trace-export";

/// The cause of the record about an export thread that had not answered when [`EXPORT_CAP`] ran out.
const CAP_EXPIRED: &str = "the export exceeded its time of 100 ms";

/// The start of the name of every variable the exporter crate reads for itself.
const GENERIC_EXPORTER_PREFIX: &[u8] = b"OTEL_EXPORTER_OTLP_";

/// The key the exporter crate keeps its default `User-Agent` header under, in the map of the headers it sends.
const EXPORTER_USER_AGENT: &str = "User-Agent";

/// The name of the instrumentation scope the span is recorded under.
const SCOPE: &str = "winter-workspace";

/// The attribute that holds the name of the span.
const COMMAND_ATTRIBUTE: &str = "winter.command";
/// The attribute that names how an invocation that failed ended.
const ERROR_TYPE_ATTRIBUTE: &str = "error.type";

/// The most attributes, events, and links the provider keeps for a span: the SDK's defaults, written out so
/// that no variable sets them.
const SPAN_LIMIT: u32 = 128;

/// What stands in for the construction of the exporter.
type Build<E> = Box<dyn FnOnce(&TraceConfig) -> Result<E, String>>;

/// The result of the one export, as the exporter answered it: `None` until it was called.
type Kept = Arc<Mutex<Option<Result<(), String>>>>;

/// The tracer of one invocation. `E` is the exporter the span is handed to: the OTLP exporter in `main`.
pub(crate) struct Tracer<E> {
    state: RefCell<State<E>>,
}

/// Where the tracer of an invocation stands.
enum State<E> {
    /// Tracing is off, or the invocation is over: nothing is built and nothing will be.
    Idle,
    /// Tracing is on and no command was announced yet.
    Waiting {
        config: Box<TraceConfig>,
        build: Build<E>,
    },
    /// Tracing is on and cannot be carried out; `log` is `None` until a command is announced.
    Unavailable {
        cause: String,
        log: Option<TraceLog>,
    },
    /// The command span is open.
    Open {
        provider: SdkTracerProvider,
        span: Box<Span>,
        log: TraceLog,
        kept: Kept,
        /// The names of the configured headers the export goes out without, for their records.
        skipped_headers: Vec<String>,
    },
}

impl Tracer<opentelemetry_otlp::SpanExporter> {
    /// The tracer of `settings` that exports over OTLP. It constructs nothing of the tracing stack.
    pub(crate) fn new(settings: TraceSettings) -> Self {
        Self::with_exporter(settings, Box::new(otlp_exporter))
    }
}

impl<E: SpanExporter + 'static> Tracer<E> {
    /// The tracer of `settings` whose exporter `build` makes, when a command is announced with tracing on and
    /// never otherwise.
    pub(crate) fn with_exporter(settings: TraceSettings, build: Build<E>) -> Self {
        let state = match settings {
            TraceSettings::Off => State::Idle,
            TraceSettings::Unavailable(cause) => State::Unavailable { cause, log: None },
            TraceSettings::On(config) => State::Waiting {
                config: Box::new(config),
                build,
            },
        };
        Self {
            state: RefCell::new(state),
        }
    }

    /// Ends the invocation's tracing, and writes its records to `stderr`: with a span open, one for each
    /// configured header that is not sent, in their order, and then at most one about the export.
    ///
    /// An open span gains the attribute `error.type` and an error status with an empty message when
    /// `error_type` names one, and the instant of its end is read, both on this thread. The span and the
    /// provider then go to the export thread, which [`export_within_the_cap`] starts and waits for no longer
    /// than [`EXPORT_CAP`]; an export that failed, or that had not ended by then, is reported through the log of
    /// the invocation. A tracer that is unavailable reports that, when a command was announced, and names no
    /// header: nothing was to be sent. Anything else does nothing, and starts no thread.
    pub(crate) fn finish(&self, error_type: Option<&'static str>, stderr: &mut dyn Write) {
        match self.state.replace(State::Idle) {
            State::Idle | State::Waiting { .. } | State::Unavailable { log: None, .. } => {}
            State::Unavailable {
                cause,
                log: Some(log),
            } => log.unavailable(stderr, &cause),
            State::Open {
                provider,
                mut span,
                log,
                kept,
                skipped_headers,
            } => {
                if let Some(error_type) = error_type {
                    span.set_attribute(KeyValue::new(ERROR_TYPE_ATTRIBUTE, error_type));
                    span.set_status(Status::error(""));
                }
                let ended = SystemTime::now();
                for name in &skipped_headers {
                    log.header_skipped(stderr, name);
                }
                if let Err(cause) = export_within_the_cap(Exported { span, provider }, ended, kept)
                {
                    log.export_failed(stderr, &cause);
                }
            }
        }
    }
}

/// What the export thread owns outright: the open span, and the provider with its exporter and the HTTP client
/// of that exporter. Dropping the span would end and export it, and dropping the provider waits for the helper
/// thread of the client, so neither is ever dropped on the thread that ends the invocation.
struct Exported {
    span: Box<Span>,
    provider: SdkTracerProvider,
}

/// Has `exported` ended at `ended`, exported, shut down, and dropped on a thread of its own, and waits for that
/// thread for [`EXPORT_CAP`] at most, from the moment the thread is started. The error is the cause of the
/// record about an export that failed.
///
/// - An answer in time: the thread is joined, which has nothing left to do, and its answer is returned.
/// - No answer in time: the handle of the thread is dropped and the thread is not joined. It runs on with
///   everything it owns and ends with the process; what it answers later is read by no one.
/// - A thread that cannot be started: what it was to own is forgotten, not dropped, because a drop here would
///   export on this thread with no bound from outside. The error is the one of the system.
///
/// The thread takes what it owns from a slot it shares with this function, so that a start that failed leaves
/// it here to forget; once the thread has taken it, this function holds nothing of the tracing stack.
fn export_within_the_cap(exported: Exported, ended: SystemTime, kept: Kept) -> Result<(), String> {
    let slot = Arc::new(Mutex::new(Some(exported)));
    let (answer, answered) = mpsc::channel();
    let started = thread::Builder::new()
        .name(EXPORT_THREAD.to_owned())
        .spawn({
            let slot = Arc::clone(&slot);
            move || {
                let result = end_and_export(&slot, ended, &kept);
                // The answer is the last thing this thread does: the thread that reads it joins this one.
                drop((slot, kept));
                let _ = answer.send(result);
            }
        });
    let thread = match started {
        Ok(thread) => thread,
        Err(error) => {
            std::mem::forget(take(&slot));
            return Err(error.to_string());
        }
    };
    match answered.recv_timeout(EXPORT_CAP) {
        Ok(result) => {
            let _ = thread.join();
            result
        }
        Err(RecvTimeoutError::Timeout) => {
            drop(thread);
            Err(CAP_EXPIRED.to_owned())
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = thread.join();
            Err("the export thread ended without an answer".to_owned())
        }
    }
}

/// The body of the export thread, up to its answer: takes what `slot` holds, ends the span at `ended`, which
/// has the processor hand a recorded span to the exporter on this thread, reads the result of that export from
/// `kept`, shuts the provider down, and drops the span and the provider, the exporter and its HTTP client with
/// them. A span the sampler did not record is handed to no exporter, and the answer is `Ok` as for an export
/// that was delivered.
///
/// A panic of any of that is the answer of a failed export. The thread is armed for the whole of it, so the
/// panic hook writes nothing for the panic and stores its message, which is the cause; a thread that is still
/// at work when the wait for it has run out is still armed.
fn end_and_export(
    slot: &Mutex<Option<Exported>>,
    ended: SystemTime,
    kept: &Mutex<Option<Result<(), String>>>,
) -> Result<(), String> {
    let thread = thread::current().id();
    crate::arm(thread);
    let ran = panic::catch_unwind(AssertUnwindSafe(|| {
        let Exported { mut span, provider } = take(slot)?;
        span.end_with_timestamp(ended);
        let result = kept.lock().unwrap_or_else(PoisonError::into_inner).take();
        // The span is out or lost by now: what a shutdown answers changes nothing.
        let _ = provider.shutdown();
        drop(span);
        drop(provider);
        result
    }));
    let stored = crate::disarm(thread);
    match ran {
        Ok(None | Some(Ok(()))) => Ok(()),
        Ok(Some(Err(cause))) => Err(cause),
        Err(payload) => Err(match stored {
            Some(panicked) => panicked.message,
            None => crate::payload_text(payload.as_ref()).to_owned(),
        }),
    }
}

/// Takes what `slot` holds, whatever a holder of its lock did before.
fn take<T>(slot: &Mutex<Option<T>>) -> Option<T> {
    slot.lock().unwrap_or_else(PoisonError::into_inner).take()
}

impl<E: SpanExporter + 'static> CommandTracer for Tracer<E> {
    /// Opens a child of the command span, recorded only when the command span is. The span is ended when the
    /// handle drops, and is sent in the command span's one export.
    fn operation(
        &self,
        name: &'static str,
        attributes: &[(&'static str, &str)],
    ) -> Box<dyn OperationSpan + '_> {
        let Some((tracer, parent)) = self.operation_parent() else {
            return Box::new(NotRecorded);
        };
        let span = tracer
            .span_builder(name)
            .with_kind(SpanKind::Internal)
            .with_attributes(
                attributes
                    .iter()
                    .map(|(key, value)| KeyValue::new(*key, (*value).to_owned())),
            )
            .start_with_context(&tracer, &parent);
        Box::new(Operation { span })
    }

    /// Builds the exporter and the provider and opens the command span, when tracing is on. A tracer that is
    /// unavailable keeps `log` for its record, and so does one whose exporter cannot be built, or whose
    /// building panics: the panic is caught here and leaves tracing unavailable, with its message as the cause.
    fn command_started(&self, command_path: &[String], log: TraceLog) {
        let next = match self.state.replace(State::Idle) {
            State::Waiting { config, build } => {
                // A panic of the tracing stack while it is built is caught here, so that it ends neither the
                // command nor the invocation: tracing is unavailable instead. The panic hook has kept it from
                // stderr, because the boundary armed this thread for the run.
                let built = panic::catch_unwind(AssertUnwindSafe(|| {
                    build(&config).map(|exporter| open(exporter, &config, command_path, log))
                }));
                match built {
                    Ok(Ok(opened)) => opened,
                    Ok(Err(cause)) => State::Unavailable {
                        cause,
                        log: Some(log),
                    },
                    Err(payload) => State::Unavailable {
                        cause: format!(
                            "the tracing stack panicked: {}",
                            crate::payload_text(payload.as_ref())
                        ),
                        log: Some(log),
                    },
                }
            }
            State::Unavailable { cause, .. } => State::Unavailable {
                cause,
                log: Some(log),
            },
            other => other,
        };
        self.state.replace(next);
    }
}

impl<E: SpanExporter + 'static> Tracer<E> {
    /// The tracer and the context an operation span starts under, when the command span is open and
    /// recording.
    fn operation_parent(&self) -> Option<(opentelemetry_sdk::trace::SdkTracer, Context)> {
        let state = self.state.borrow();
        let State::Open { provider, span, .. } = &*state else {
            return None;
        };
        if !span.is_recording() {
            return None;
        }
        let context = Context::new().with_remote_span_context(span.span_context().clone());
        Some((provider.tracer(SCOPE), context))
    }
}

/// The operation span of a tracer whose command span is not open or not recording.
struct NotRecorded;

impl OperationSpan for NotRecorded {
    fn failed(&mut self, _: &'static str) {}
}

/// An operation span of the binary's tracer: a child of the command span, ended when it drops. The span
/// reaches the exporter, which holds it until the command span's export.
struct Operation {
    span: Span,
}

impl OperationSpan for Operation {
    fn failed(&mut self, error_type: &'static str) {
        self.span
            .set_attribute(KeyValue::new(ERROR_TYPE_ATTRIBUTE, error_type));
        self.span.set_status(Status::error(""));
    }
}

/// Builds the provider over `exporter` and opens the command span of `command_path`.
///
/// The sampler, the span limits, and the resource are each given, so the SDK reads no variable for any of them:
/// the resource starts from the empty one and holds exactly what `config` lists.
fn open<E: SpanExporter + 'static>(
    exporter: E,
    config: &TraceConfig,
    command_path: &[String],
    log: TraceLog,
) -> State<E> {
    let kept = Kept::default();
    let command = Arc::new(Mutex::new(None));
    let resource = Resource::builder_empty()
        .with_attributes(
            config
                .resource
                .iter()
                .map(|(key, value)| KeyValue::new(key.clone(), value.clone())),
        )
        .build();
    let provider = SdkTracerProvider::builder()
        .with_simple_exporter(Keeping {
            exporter,
            kept: Arc::clone(&kept),
            command: Arc::clone(&command),
            held: Mutex::default(),
        })
        .with_sampler(Sampler::ParentBased(Box::new(Sampler::AlwaysOn)))
        .with_span_limits(SpanLimits {
            max_attributes_per_span: SPAN_LIMIT,
            max_events_per_span: SPAN_LIMIT,
            max_links_per_span: SPAN_LIMIT,
            ..SpanLimits::default()
        })
        .with_resource(resource)
        .build();

    let name = span_name(command_path);
    let tracer = provider.tracer(SCOPE);
    let mut builder = tracer
        .span_builder(name.clone())
        .with_kind(SpanKind::Internal)
        .with_attributes([KeyValue::new(COMMAND_ATTRIBUTE, name)]);
    if let Some(launched) = config.launch_time_unix_nano {
        builder = builder.with_start_time(SystemTime::UNIX_EPOCH + Duration::from_nanos(launched));
    }
    let span = builder.start_with_context(&tracer, &parent(config.traceparent.as_deref()));
    *command.lock().unwrap_or_else(PoisonError::into_inner) = Some(span.span_context().span_id());
    State::Open {
        provider,
        span: Box::new(span),
        log,
        kept,
        skipped_headers: config.skipped_headers.clone(),
    }
}

/// The context the span starts under: what the W3C trace-context propagator reads from `traceparent`, given as
/// the one entry of its carrier. No value, and a value the propagator does not accept, give a context without a
/// parent.
fn parent(traceparent: Option<&str>) -> Context {
    let Some(traceparent) = traceparent else {
        return Context::new();
    };
    let carrier = HashMap::from([("traceparent".to_owned(), traceparent.to_owned())]);
    TraceContextPropagator::new().extract_with_context(&Context::new(), &carrier)
}

/// Builds the OTLP exporter of `config`: HTTP with protobuf, the configured headers, the export timeout, and
/// no retry. The error is the text of what the exporter crate refused.
///
/// The exporter percent-decodes each header value it is handed, and `config` holds the values decoded already,
/// so each `%` is handed as `%25` and the exporter holds the value of `config`. The headers of `config` hold no
/// `content-type` and no value that is not legal in an HTTP header, so the exporter is handed nothing that
/// takes the place of its protobuf content type and nothing it leaves out for its value.
///
/// The exporter keeps its own `User-Agent` under that spelling in a map keyed by text, beside the headers it is
/// handed, and sends one of two keys that differ in case alone, whichever its map yields last. A configured
/// `user-agent` is therefore handed under the exporter's spelling, so that it always replaces the default.
fn otlp_exporter(config: &TraceConfig) -> Result<opentelemetry_otlp::SpanExporter, String> {
    let headers: HashMap<String, String> = config
        .headers
        .iter()
        .map(|(name, value)| (exporter_key(name), value.replace('%', "%25")))
        .collect();
    opentelemetry_otlp::SpanExporter::builder()
        .with_http()
        .with_endpoint(config.endpoint.clone())
        .with_protocol(Protocol::HttpBinary)
        .with_timeout(EXPORT_TIMEOUT)
        .with_headers(headers)
        .with_retry_policy(RetryPolicy::disabled())
        .build()
        .map_err(|error| error.to_string())
}

/// The key the exporter is handed the header `name` under: its own spelling of `User-Agent` for that header,
/// whatever the case of `name`, and `name` itself for any other.
fn exporter_key(name: &str) -> String {
    if name.eq_ignore_ascii_case(EXPORTER_USER_AGENT) {
        EXPORTER_USER_AGENT.to_owned()
    } else {
        name.to_owned()
    }
}

/// An exporter that keeps what `exporter` answers to an export, for the export thread to read once the span has
/// ended: the processor that calls it reports that answer to no one.
#[derive(Debug)]
///
/// It also holds the operation spans, which end before the command span, until the command span's export, and
/// sends them in it: the invocation makes one export.
struct Keeping<E> {
    exporter: E,
    kept: Kept,
    /// The id of the command span, once it is started.
    command: Arc<Mutex<Option<SpanId>>>,
    /// The ended spans of the operations, not yet exported.
    held: Mutex<Vec<SpanData>>,
}

impl<E: SpanExporter> SpanExporter for Keeping<E> {
    fn export(&self, batch: Vec<SpanData>) -> impl Future<Output = OTelSdkResult> + Send {
        let command = *self.command.lock().unwrap_or_else(PoisonError::into_inner);
        let ends_command = batch
            .iter()
            .any(|span| Some(span.span_context.span_id()) == command);
        let mut held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
        let batch = if ends_command {
            let mut all = std::mem::take(&mut *held);
            all.extend(batch);
            all
        } else {
            held.extend(batch);
            Vec::new()
        };
        drop(held);
        let keeps = ends_command;
        let export = keeps.then(|| self.exporter.export(batch));
        let kept = Arc::clone(&self.kept);
        async move {
            let Some(export) = export else {
                return Ok(());
            };
            let result = export.await;
            let answer = match &result {
                Ok(()) => Ok(()),
                Err(error) => Err(error.to_string()),
            };
            *kept.lock().unwrap_or_else(PoisonError::into_inner) = Some(answer);
            result
        }
    }

    fn shutdown_with_timeout(&self, timeout: Duration) -> OTelSdkResult {
        self.exporter.shutdown_with_timeout(timeout)
    }

    fn force_flush(&self) -> OTelSdkResult {
        self.exporter.force_flush()
    }

    fn set_resource(&mut self, resource: &Resource) {
        self.exporter.set_resource(resource);
    }
}

/// Removes from the process environment every variable of `env` whose name starts with `OTEL_EXPORTER_OTLP_`.
///
/// The exporter crate reads those variables from the process environment when it builds an exporter: it lets
/// some stand in where the program set nothing and adds the headers of others to the programmed ones. `env`
/// itself is not changed.
///
/// Two conditions bind every caller, and each call site says how it meets the first:
///
/// - The process has one thread during the call. The function does not check it. `main` calls this once,
///   directly after it took the snapshot `env`, before it starts a thread and before anything it calls has
///   started one, and only when tracing is on.
/// - After the call the process environment is no longer what the process was started with. The environment of
///   a child process is therefore built from the snapshot `env`, which was taken before the removal, and never
///   inherited from the process: an inherited one would lack these variables in every traced invocation.
pub(crate) fn hide_generic_exporter_variables(env: &ProcessEnv) {
    for name in env.names().filter(|name| is_generic_exporter(name)) {
        // SAFETY: changing the process environment is sound while no other thread reads or writes it, which
        // is the first condition this function puts on its caller. `main` meets it: it calls this before it
        // starts a thread and before anything it calls has started one, so the process has one thread here.
        unsafe { std::env::remove_var(name) };
    }
}

/// Whether `name` is a variable the exporter crate reads for itself.
fn is_generic_exporter(name: &OsStr) -> bool {
    name.as_bytes().starts_with(GENERIC_EXPORTER_PREFIX)
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::ffi::OsString;
    use std::path::PathBuf;
    use std::rc::Rc;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::mpsc::{Receiver, Sender};
    use std::thread::ThreadId;

    use opentelemetry::trace::{SpanId, TraceId};
    use opentelemetry_sdk::error::OTelSdkError;
    use winter_workspace_core::{Identity, Interrupts, Invocation, run, settings};
    use winter_workspace_test_support::{Answer, Collector, Exit, Observation, TempTree, invoke};

    use super::*;
    use crate::interrupt;

    /// What the exporter of a test was handed, shared with the test that built it.
    #[derive(Debug, Clone, Default)]
    struct Handed {
        spans: Arc<Mutex<Vec<SpanData>>>,
        resource: Arc<Mutex<Option<Resource>>>,
        /// The thread of each call of the export.
        exported_on: Arc<Mutex<Vec<ThreadId>>>,
        /// The thread the exporter was dropped on, once it was.
        dropped_on: Arc<Mutex<Option<ThreadId>>>,
    }

    impl Handed {
        fn spans(&self) -> Vec<SpanData> {
            self.spans.lock().expect("the spans").clone()
        }

        /// The one span the exporter was handed.
        fn span(&self) -> SpanData {
            let spans = self.spans();
            assert_eq!(spans.len(), 1, "{spans:?}");
            spans.into_iter().next().expect("one span")
        }

        /// The resource the exporter was given, as pairs sorted by key.
        fn resource(&self) -> Vec<(String, String)> {
            let resource = self.resource.lock().expect("the resource");
            let mut pairs: Vec<_> = resource
                .as_ref()
                .expect("a resource")
                .iter()
                .map(|(key, value)| (key.as_str().to_owned(), value.as_str().into_owned()))
                .collect();
            pairs.sort();
            pairs
        }

        /// The one thread the export was called on.
        fn exported_on(&self) -> ThreadId {
            let threads = self.exported_on.lock().expect("the threads");
            assert_eq!(threads.len(), 1, "{threads:?}");
            threads[0]
        }

        fn dropped_on(&self) -> Option<ThreadId> {
            *self.dropped_on.lock().expect("the thread of the drop")
        }
    }

    /// An exporter that opens no socket: it records what it is handed and on which thread, and then does what
    /// `then` says.
    #[derive(Debug)]
    struct Recorder {
        handed: Handed,
        then: Then,
    }

    /// What an export of a [`Recorder`] does once it has recorded what it was handed.
    #[derive(Debug)]
    enum Then {
        Succeeds,
        Fails,
        Panics,
        Blocks(Blocked),
    }

    /// How long an export that blocks stays inside its export when the test does not release it. A `finish`
    /// that waits for the exporter returns only after it, and its test then fails.
    const UNRELEASED: Duration = Duration::from_secs(30);

    /// The exporter's side of an export that does not return before the test releases it.
    #[derive(Debug)]
    struct Blocked {
        /// Told once the export is entered.
        entered: Mutex<Sender<()>>,
        /// Answers once the test releases the export.
        released: Mutex<Receiver<()>>,
        /// Set when the export is left.
        left: Arc<AtomicBool>,
    }

    /// The test's side of the same export.
    struct Blocking {
        entered: Receiver<()>,
        release: Sender<()>,
        left: Arc<AtomicBool>,
    }

    impl SpanExporter for Recorder {
        fn export(&self, batch: Vec<SpanData>) -> impl Future<Output = OTelSdkResult> + Send {
            self.handed.spans.lock().expect("the spans").extend(batch);
            self.handed
                .exported_on
                .lock()
                .expect("the threads")
                .push(thread::current().id());
            let answer = match &self.then {
                Then::Succeeds => Ok(()),
                Then::Fails => Err(OTelSdkError::InternalFailure(
                    "the collector is away".to_owned(),
                )),
                Then::Panics => panic!("the exporter gave up"),
                Then::Blocks(blocked) => {
                    let _ = blocked.entered.lock().expect("the sender").send(());
                    let _ = blocked
                        .released
                        .lock()
                        .expect("the receiver")
                        .recv_timeout(UNRELEASED);
                    blocked.left.store(true, Ordering::SeqCst);
                    Ok(())
                }
            };
            std::future::ready(answer)
        }

        fn set_resource(&mut self, resource: &Resource) {
            *self.handed.resource.lock().expect("the resource") = Some(resource.clone());
        }
    }

    impl Drop for Recorder {
        fn drop(&mut self) {
            *self
                .handed
                .dropped_on
                .lock()
                .expect("the thread of the drop") = Some(thread::current().id());
        }
    }

    /// A tracer over an exporter of the test's own, with what that exporter is handed and how often its
    /// builder was called.
    struct Probe {
        tracer: Tracer<Recorder>,
        handed: Handed,
        built: Rc<Cell<usize>>,
    }

    enum Exporter {
        Records,
        Fails,
        Panics,
        Blocks(Blocked),
        CannotBeBuilt,
        BuildPanics,
    }

    fn probe(settings: TraceSettings, exporter: Exporter) -> Probe {
        let handed = Handed::default();
        let built = Rc::new(Cell::new(0));
        let build: Build<Recorder> = {
            let handed = handed.clone();
            let built = Rc::clone(&built);
            Box::new(move |_| {
                built.set(built.get() + 1);
                let then = match exporter {
                    Exporter::Records => Then::Succeeds,
                    Exporter::Fails => Then::Fails,
                    Exporter::Panics => Then::Panics,
                    Exporter::Blocks(blocked) => Then::Blocks(blocked),
                    Exporter::CannotBeBuilt => {
                        return Err("the endpoint is no address".to_owned());
                    }
                    Exporter::BuildPanics => panic!("the client could not start"),
                };
                Ok(Recorder { handed, then })
            })
        };
        Probe {
            tracer: Tracer::with_exporter(settings, build),
            handed,
            built,
        }
    }

    /// The two sides of an export that does not return before the test releases it.
    fn blocking() -> (Blocked, Blocking) {
        let (entered, was_entered) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let left = Arc::new(AtomicBool::new(false));
        (
            Blocked {
                entered: Mutex::new(entered),
                released: Mutex::new(released),
                left: Arc::clone(&left),
            },
            Blocking {
                entered: was_entered,
                release,
                left,
            },
        )
    }

    fn on() -> TraceConfig {
        TraceConfig {
            endpoint: "http://collector.invalid:4318/v1/traces".to_owned(),
            headers: Vec::new(),
            skipped_headers: Vec::new(),
            resource: vec![
                ("service.name".to_owned(), "winter".to_owned()),
                ("deployment".to_owned(), "test".to_owned()),
            ],
            traceparent: None,
            launch_time_unix_nano: None,
        }
    }

    /// How an invocation asks for its log records.
    #[derive(Clone, Copy, Debug)]
    enum Logging {
        /// No switch: warnings and above, unswitched.
        Unswitched,
        /// `-v`: every record, switched.
        Debug,
        /// `WINTER_LOG_LEVEL=ERROR`: errors and above, switched.
        Error,
    }

    /// Runs the library over `args` with in-memory handles and `tracer` as its tracer, which is how a tracer is
    /// told a command with the log of an invocation.
    fn run_library(tracer: &dyn CommandTracer, logging: Logging, args: &[&str]) {
        let mut stdin: &[u8] = &[];
        let interrupts = Interrupts::new();
        let verbose: &[&str] = match logging {
            Logging::Debug => &["-v"],
            Logging::Unswitched | Logging::Error => &[],
        };
        let env = match logging {
            Logging::Error => vec![(OsString::from("WINTER_LOG_LEVEL"), OsString::from("ERROR"))],
            Logging::Unswitched | Logging::Debug => Vec::new(),
        };
        run(Invocation {
            args: verbose.iter().chain(args).map(OsString::from).collect(),
            cwd: Ok(PathBuf::from("/")),
            env: ProcessEnv::from_vars(env),
            identity: Identity {
                program: "winter-workspace",
                version: "0.1.0",
            },
            stdin: &mut stdin,
            stdout: &mut Vec::new(),
            stderr: &mut Vec::new(),
            stdout_is_terminal: false,
            stderr_is_terminal: false,
            interrupts: &interrupts,
            tracer,
        });
    }

    /// Announces the command `ws index` to `tracer`: a usage error of that command, which reads no file.
    fn announce(tracer: &dyn CommandTracer, logging: Logging) {
        run_library(tracer, logging, &["ws", "index", "--bogus"]);
    }

    /// What `finish` writes to a stderr of the test's own.
    fn finished<E: SpanExporter + 'static>(
        tracer: &Tracer<E>,
        error_type: Option<&'static str>,
    ) -> String {
        let mut stderr = Vec::new();
        tracer.finish(error_type, &mut stderr);
        String::from_utf8(stderr).expect("UTF-8 on stderr")
    }

    fn attributes(span: &SpanData) -> Vec<(String, String)> {
        span.attributes
            .iter()
            .map(|pair| {
                (
                    pair.key.as_str().to_owned(),
                    pair.value.as_str().into_owned(),
                )
            })
            .collect()
    }

    fn pairs(literal: &[(&str, &str)]) -> Vec<(String, String)> {
        literal
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn with_tracing_off_the_builder_is_never_called_and_finish_writes_nothing() {
        for announced in [false, true] {
            let probe = probe(TraceSettings::Off, Exporter::Records);
            if announced {
                announce(&probe.tracer, Logging::Debug);
            }

            assert_eq!(finished(&probe.tracer, Some("Usage")), "", "{announced}");
            assert_eq!(probe.built.get(), 0, "{announced}");
            assert_eq!(probe.handed.spans(), [], "{announced}");
        }
    }

    #[test]
    fn with_tracing_on_and_no_command_announced_the_builder_is_never_called() {
        let probe = probe(TraceSettings::On(on()), Exporter::Records);

        run_library(&probe.tracer, Logging::Debug, &["--version"]);

        assert_eq!(finished(&probe.tracer, None), "");
        assert_eq!(probe.built.get(), 0);
        assert_eq!(probe.handed.spans(), []);
    }

    #[test]
    fn an_announced_command_is_exported_as_one_internal_span_named_for_it_under_the_resource_of_the_settings()
     {
        let began = SystemTime::now();
        let probe = probe(TraceSettings::On(on()), Exporter::Records);

        announce(&probe.tracer, Logging::Debug);
        assert_eq!(
            probe.handed.spans(),
            [],
            "nothing is exported before the end"
        );
        let written = finished(&probe.tracer, None);

        assert_eq!(written, "");
        assert_eq!(probe.built.get(), 1);
        let span = probe.handed.span();
        assert_eq!(span.name, "winter ws index");
        assert_eq!(span.span_kind, SpanKind::Internal);
        assert_eq!(
            attributes(&span),
            pairs(&[("winter.command", "winter ws index")])
        );
        assert_eq!(span.status, Status::Unset);
        assert_eq!(span.parent_span_id, SpanId::INVALID);
        assert!(span.start_time >= began, "{:?}", span.start_time);
        assert!(span.end_time >= span.start_time, "{:?}", span.end_time);
        assert_eq!(span.events.len(), 0);
        assert_eq!(span.links.len(), 0);
        assert_eq!(
            probe.handed.resource(),
            pairs(&[("deployment", "test"), ("service.name", "winter")])
        );
    }

    #[test]
    fn the_span_is_named_for_the_command_path_that_was_announced() {
        let probe = probe(TraceSettings::On(on()), Exporter::Records);

        run_library(&probe.tracer, Logging::Unswitched, &["ws", "--help"]);
        finished(&probe.tracer, None);

        let span = probe.handed.span();
        assert_eq!(span.name, "winter ws");
        assert_eq!(attributes(&span), pairs(&[("winter.command", "winter ws")]));
    }

    #[test]
    fn a_launch_time_is_the_start_of_the_span() {
        let launched = 1_700_000_000_123_456_789;
        let probe = probe(
            TraceSettings::On(TraceConfig {
                launch_time_unix_nano: Some(launched),
                ..on()
            }),
            Exporter::Records,
        );

        announce(&probe.tracer, Logging::Unswitched);
        finished(&probe.tracer, None);

        assert_eq!(
            probe.handed.span().start_time,
            SystemTime::UNIX_EPOCH + Duration::from_nanos(launched)
        );
    }

    #[test]
    fn an_error_type_adds_its_attribute_and_an_error_status_with_an_empty_message() {
        let probe = probe(TraceSettings::On(on()), Exporter::Records);

        announce(&probe.tracer, Logging::Debug);
        let written = finished(&probe.tracer, Some("Usage"));

        assert_eq!(written, "");
        let span = probe.handed.span();
        assert_eq!(
            attributes(&span),
            pairs(&[
                ("winter.command", "winter ws index"),
                ("error.type", "Usage")
            ])
        );
        assert_eq!(span.status, Status::error(""));
    }

    #[test]
    fn a_sampled_traceparent_gives_the_span_its_trace_and_its_parent() {
        let probe = probe(
            TraceSettings::On(TraceConfig {
                traceparent: Some(
                    "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01".to_owned(),
                ),
                ..on()
            }),
            Exporter::Records,
        );

        announce(&probe.tracer, Logging::Unswitched);
        finished(&probe.tracer, None);

        let span = probe.handed.span();
        assert_eq!(
            span.span_context.trace_id().to_string(),
            "0af7651916cd43dd8448eb211c80319c"
        );
        assert_eq!(span.parent_span_id.to_string(), "b7ad6b7169203331");
        assert_ne!(span.span_context.span_id(), span.parent_span_id);
    }

    #[test]
    fn an_invalid_traceparent_gives_the_root_span_of_a_new_trace() {
        for invalid in [
            "not a traceparent",
            "00-00000000000000000000000000000000-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",
        ] {
            let probe = probe(
                TraceSettings::On(TraceConfig {
                    traceparent: Some(invalid.to_owned()),
                    ..on()
                }),
                Exporter::Records,
            );

            announce(&probe.tracer, Logging::Unswitched);
            finished(&probe.tracer, None);

            let span = probe.handed.span();
            assert_eq!(span.parent_span_id, SpanId::INVALID, "{invalid}");
            assert_ne!(span.span_context.trace_id(), TraceId::INVALID, "{invalid}");
            assert_ne!(
                span.span_context.trace_id().to_string(),
                "0af7651916cd43dd8448eb211c80319c",
                "{invalid}"
            );
        }
    }

    #[test]
    fn an_unsampled_traceparent_exports_nothing_and_finish_writes_nothing() {
        let probe = probe(
            TraceSettings::On(TraceConfig {
                traceparent: Some(
                    "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00".to_owned(),
                ),
                ..on()
            }),
            Exporter::Fails,
        );

        announce(&probe.tracer, Logging::Debug);
        let written = finished(&probe.tracer, Some("Usage"));

        assert_eq!(written, "");
        assert_eq!(probe.built.get(), 1);
        assert_eq!(probe.handed.spans(), []);
    }

    #[test]
    fn an_export_that_fails_is_one_record_under_a_switched_log_and_nothing_otherwise() {
        for (logging, record) in [
            (Logging::Unswitched, ""),
            (
                Logging::Debug,
                "ERROR trace: the command span was not exported: \
                 Operation failed: the collector is away\n",
            ),
            (
                Logging::Error,
                "ERROR trace: the command span was not exported: \
                 Operation failed: the collector is away\n",
            ),
        ] {
            let probe = probe(TraceSettings::On(on()), Exporter::Fails);

            announce(&probe.tracer, logging);
            let written = finished(&probe.tracer, None);

            assert_eq!(written, record, "{logging:?}");
            assert_eq!(probe.handed.spans().len(), 1, "{logging:?}");
        }
    }

    /// Settings that turn tracing on and name two configured headers the export goes out without.
    fn with_skipped_headers() -> TraceConfig {
        TraceConfig {
            skipped_headers: vec!["x-bad".to_owned(), "x-c".to_owned()],
            ..on()
        }
    }

    const SKIPPED_RECORDS: &str = "\
        DEBUG trace: the header x-bad is not sent: its value is not legal in an HTTP header\n\
        DEBUG trace: the header x-c is not sent: its value is not legal in an HTTP header\n";

    #[test]
    fn each_skipped_header_is_one_debug_record_in_order_before_the_record_of_the_export() {
        let failed = "ERROR trace: the command span was not exported: \
                      Operation failed: the collector is away\n";
        for (exporter, logging, records) in [
            (
                Exporter::Records,
                Logging::Debug,
                SKIPPED_RECORDS.to_owned(),
            ),
            (
                Exporter::Fails,
                Logging::Debug,
                format!("{SKIPPED_RECORDS}{failed}"),
            ),
            (Exporter::Fails, Logging::Unswitched, String::new()),
            (Exporter::Fails, Logging::Error, failed.to_owned()),
        ] {
            let probe = probe(TraceSettings::On(with_skipped_headers()), exporter);

            announce(&probe.tracer, logging);
            let written = finished(&probe.tracer, None);

            assert_eq!(written, records, "{logging:?}");
            assert_eq!(
                probe.handed.spans().len(),
                1,
                "{logging:?}: the span is handed to the exporter all the same"
            );
        }
    }

    #[test]
    fn a_span_the_sampler_did_not_record_still_has_its_skipped_headers_named() {
        let probe = probe(
            TraceSettings::On(TraceConfig {
                traceparent: Some(
                    "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00".to_owned(),
                ),
                ..with_skipped_headers()
            }),
            Exporter::Fails,
        );

        announce(&probe.tracer, Logging::Debug);

        assert_eq!(finished(&probe.tracer, None), SKIPPED_RECORDS);
        assert_eq!(probe.handed.spans(), []);
    }

    #[test]
    fn no_skipped_header_is_named_when_no_span_was_opened() {
        // No command is announced.
        let unannounced = probe(TraceSettings::On(with_skipped_headers()), Exporter::Records);
        run_library(&unannounced.tracer, Logging::Debug, &["--version"]);
        assert_eq!(finished(&unannounced.tracer, None), "");

        // The settings are unavailable.
        let unavailable = probe(
            TraceSettings::Unavailable("the value is not UTF-8".to_owned()),
            Exporter::Records,
        );
        announce(&unavailable.tracer, Logging::Debug);
        assert_eq!(
            finished(&unavailable.tracer, None),
            "DEBUG trace: tracing is unavailable: the value is not UTF-8\n"
        );

        // The exporter cannot be built.
        let unbuilt = probe(
            TraceSettings::On(with_skipped_headers()),
            Exporter::CannotBeBuilt,
        );
        announce(&unbuilt.tracer, Logging::Debug);
        assert_eq!(
            finished(&unbuilt.tracer, None),
            "DEBUG trace: tracing is unavailable: the endpoint is no address\n"
        );
    }

    #[test]
    fn the_span_is_exported_and_the_exporter_dropped_on_another_thread_before_finish_returns() {
        let probe = probe(TraceSettings::On(on()), Exporter::Records);
        announce(&probe.tracer, Logging::Debug);

        let written = finished(&probe.tracer, None);
        // Read at once: `finish` is back, and nothing here waits for the other thread.
        let spans = probe.handed.spans();
        let dropped_on = probe.handed.dropped_on();

        assert_eq!(written, "");
        assert_eq!(spans.len(), 1, "{spans:?}");
        let exported_on = probe.handed.exported_on();
        assert_ne!(exported_on, thread::current().id());
        assert_eq!(
            dropped_on,
            Some(exported_on),
            "the exporter ends on the thread that exported"
        );
    }

    #[test]
    fn an_export_that_does_not_return_is_left_behind_and_is_one_record_under_a_switched_log() {
        for (logging, record) in [
            (Logging::Unswitched, ""),
            (
                Logging::Debug,
                "ERROR trace: the command span was not exported: \
                 the export exceeded its time of 100 ms\n",
            ),
        ] {
            let (blocked, blocking) = blocking();
            let probe = probe(TraceSettings::On(on()), Exporter::Blocks(blocked));
            announce(&probe.tracer, logging);

            let written = finished(&probe.tracer, None);
            // `finish` is back. The export was entered, before or after that, and is left only once it is
            // released below or after `UNRELEASED`, which a `finish` that waited for it would have sat out.
            blocking
                .entered
                .recv()
                .expect("the export is entered on a thread that `finish` started");
            let left = blocking.left.load(Ordering::SeqCst);

            assert!(
                !left,
                "{logging:?}: `finish` returned only after the export"
            );
            assert_eq!(written, record, "{logging:?}");
            assert_ne!(probe.handed.exported_on(), thread::current().id());
            assert_eq!(
                probe.handed.dropped_on(),
                None,
                "{logging:?}: the exporter is still owned by the thread inside it"
            );
            // The thread is let go, and nothing waits for it.
            let _ = blocking.release.send(());
        }
    }

    #[test]
    fn an_export_that_panics_is_one_record_under_a_switched_log_and_reaches_no_other_hook() {
        crate::tests::install();
        for logging in [Logging::Unswitched, Logging::Debug] {
            let probe = probe(TraceSettings::On(on()), Exporter::Panics);
            announce(&probe.tracer, logging);

            let written = finished(&probe.tracer, None);

            match logging {
                Logging::Debug => {
                    assert!(
                        written.starts_with(
                            "ERROR trace: the command span was not exported: panicked at "
                        ),
                        "{written}"
                    );
                    assert!(written.contains(file!()), "{written}");
                    assert!(written.ends_with(": the exporter gave up\n"), "{written}");
                    assert_eq!(written.lines().count(), 1, "{written}");
                }
                Logging::Unswitched | Logging::Error => assert_eq!(written, ""),
            }
            let exported_on = probe.handed.exported_on();
            assert_ne!(exported_on, thread::current().id());
            assert!(!crate::tests::was_forwarded(exported_on), "{logging:?}");
            assert!(
                crate::slots()
                    .iter()
                    .all(|(armed, _)| *armed != exported_on),
                "{logging:?}: the slot of the export thread is emptied"
            );
            assert_eq!(finished(&probe.tracer, None), "", "{logging:?}");
        }
    }

    #[test]
    fn an_exporter_that_cannot_be_built_exports_nothing_and_is_one_debug_record_under_a_switched_log()
     {
        for (logging, record) in [
            (Logging::Unswitched, ""),
            (
                Logging::Debug,
                "DEBUG trace: tracing is unavailable: the endpoint is no address\n",
            ),
            (Logging::Error, ""),
        ] {
            let probe = probe(TraceSettings::On(on()), Exporter::CannotBeBuilt);

            announce(&probe.tracer, logging);
            let written = finished(&probe.tracer, Some("Usage"));

            assert_eq!(written, record, "{logging:?}");
            assert_eq!(probe.built.get(), 1, "{logging:?}");
            assert_eq!(probe.handed.spans(), [], "{logging:?}");
        }
    }

    #[test]
    fn a_builder_that_panics_leaves_tracing_unavailable_and_the_command_running() {
        for (logging, record) in [
            (Logging::Unswitched, ""),
            (
                Logging::Debug,
                "DEBUG trace: tracing is unavailable: the tracing stack panicked: \
                 the client could not start\n",
            ),
            (Logging::Error, ""),
        ] {
            let probe = probe(TraceSettings::On(on()), Exporter::BuildPanics);

            // The announcement returns: the panic of the builder does not reach the library.
            announce(&probe.tracer, logging);
            let written = finished(&probe.tracer, Some("Usage"));

            assert_eq!(written, record, "{logging:?}");
            assert_eq!(probe.built.get(), 1, "{logging:?}");
            assert_eq!(probe.handed.spans(), [], "{logging:?}");
            assert_eq!(finished(&probe.tracer, None), "", "{logging:?}");
        }
    }

    #[test]
    fn settings_that_are_unavailable_build_nothing_and_are_one_debug_record_under_a_switched_log() {
        for (logging, record) in [
            (Logging::Unswitched, ""),
            (
                Logging::Debug,
                "DEBUG trace: tracing is unavailable: the value is not UTF-8\n",
            ),
            (Logging::Error, ""),
        ] {
            let probe = probe(
                TraceSettings::Unavailable("the value is not UTF-8".to_owned()),
                Exporter::Records,
            );

            announce(&probe.tracer, logging);
            let written = finished(&probe.tracer, None);

            assert_eq!(written, record, "{logging:?}");
            assert_eq!(probe.built.get(), 0, "{logging:?}");
            assert_eq!(probe.handed.spans(), [], "{logging:?}");
        }
    }

    #[test]
    fn a_tracer_that_is_unavailable_writes_nothing_when_no_command_was_announced() {
        for settings in [
            TraceSettings::Unavailable("the value is not UTF-8".to_owned()),
            TraceSettings::On(on()),
        ] {
            let probe = probe(settings, Exporter::CannotBeBuilt);

            run_library(&probe.tracer, Logging::Debug, &["nope"]);

            assert_eq!(finished(&probe.tracer, Some("Usage")), "");
            assert_eq!(probe.built.get(), 0);
        }
    }

    #[test]
    fn a_second_finish_does_nothing() {
        let probe = probe(TraceSettings::On(on()), Exporter::Fails);
        announce(&probe.tracer, Logging::Debug);
        assert_ne!(finished(&probe.tracer, None), "");

        assert_eq!(finished(&probe.tracer, None), "");
        assert_eq!(probe.handed.spans().len(), 1);
    }

    #[test]
    fn a_user_agent_header_is_handed_to_the_exporter_under_the_exporters_own_key() {
        for name in ["user-agent", "User-Agent", "USER-AGENT"] {
            assert_eq!(exporter_key(name), "User-Agent", "{name}");
        }
        for name in [
            "x-user-agent",
            "user-agent2",
            "authorization",
            "content-type",
        ] {
            assert_eq!(exporter_key(name), name);
        }
    }

    #[test]
    fn only_a_name_that_starts_with_the_generic_exporter_prefix_is_a_generic_exporter_variable() {
        for name in [
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
            "OTEL_EXPORTER_OTLP_",
        ] {
            assert!(is_generic_exporter(OsStr::new(name)), "{name}");
        }
        for name in [
            "WINTER_OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP",
            "OTEL_SERVICE_NAME",
            "otel_exporter_otlp_endpoint",
        ] {
            assert!(!is_generic_exporter(OsStr::new(name)), "{name}");
        }
    }

    /// Names the fixture a child test process was started to run. A fixture does nothing without its own name
    /// here.
    const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

    /// A test of this module that changes the environment of its own process, so it runs only in a child
    /// process, which runs nothing else: beside the thread of the fixture that process has the thread of the
    /// test harness alone, which waits for the fixture.
    const HIDE_TEST: &str = "trace::tests::fixture_the_generic_exporter_variables_are_hidden";

    /// A test of this module that raises SIGINT on its own process with a span open, which ends the process,
    /// so it runs only in a child process.
    const INTERRUPT_TEST: &str = "trace::tests::fixture_an_interrupt_with_the_command_span_open";

    /// What the hiding fixture writes to stdout once every assertion held.
    const HIDDEN_LINE: &str = "the variables are hidden\n";

    /// What the interrupt fixture writes to stdout once the span is open.
    const READY_LINE: &str = "the command span is open\n";

    /// The variable that names the collector, which turns tracing on.
    const ENDPOINT_VARIABLE: &str = "WINTER_OTEL_EXPORTER_OTLP_ENDPOINT";

    /// The certificate a child that builds the real exporter reads as its whole trust store.
    const TRUST_ROOT: &str = include_str!("../tests/data/trust-root.pem");

    /// Whether this process is the child test process a test started to run the fixture `name`.
    fn started_as_fixture(name: &str) -> bool {
        std::env::var_os(FIXTURE_VARIABLE).is_some_and(|fixture| fixture == name)
    }

    /// Runs `fixture` alone in a child process of this test binary, under the pinned environment of `tree`
    /// with `variables` added, and returns what the child wrote and how it ended.
    fn fixture(tree: &TempTree, fixture: &str, variables: &[(&str, &str)]) -> Observation {
        let this_test_binary = std::env::current_exe().expect("the path of the test executable");
        let mut invocation = tree.invocation(&this_test_binary);
        invocation.args = [fixture, "--exact", "--ignored", "--nocapture"]
            .iter()
            .map(Into::into)
            .collect();
        let parent = tree.root().join("tmp");
        invocation.env.set(
            "WINTER_TEST_TMPDIR",
            parent.to_str().expect("a path inside a tree is text"),
        );
        invocation.env.set(FIXTURE_VARIABLE, fixture);
        for (key, value) in variables {
            invocation.env.set(key, value);
        }
        invoke(invocation).expect("the child test process runs to its end")
    }

    fn text(bytes: &[u8]) -> String {
        String::from_utf8_lossy(bytes).into_owned()
    }

    /// Writes `line` to the stdout of the process itself, past the capture of the test harness, and flushes.
    fn say(line: &str) {
        let mut stdout = std::io::stdout().lock();
        stdout.write_all(line.as_bytes()).expect("a stdout");
        stdout.flush().expect("a stdout");
    }

    #[test]
    #[ignore = "a fixture: it changes its own environment, so it runs only as the child of the test that asserts on it"]
    fn fixture_the_generic_exporter_variables_are_hidden() {
        if !started_as_fixture(HIDE_TEST) {
            return;
        }
        let hidden = ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"];
        for name in hidden {
            assert!(std::env::var_os(name).is_some(), "{name} before");
        }
        let snapshot = ProcessEnv::from_vars(std::env::vars_os());

        // This process has two threads, not the one the removal requires: this one, and the thread of the test
        // harness, which started this fixture alone and does nothing but wait for it, so it neither reads nor
        // writes the environment during the call.
        hide_generic_exporter_variables(&snapshot);

        for name in hidden {
            assert_eq!(std::env::var_os(name), None, "{name}");
            assert!(snapshot.get(name).is_some(), "{name} in the snapshot");
        }
        assert!(
            std::env::var_os("OTEL_SERVICE_NAME").is_some(),
            "another variable stays"
        );
        say(HIDDEN_LINE);
    }

    #[test]
    fn hiding_removes_the_generic_exporter_variables_from_the_process_and_leaves_the_snapshot() {
        let tree = TempTree::new().expect("an isolated tree");

        let observation = fixture(
            &tree,
            HIDE_TEST,
            &[
                ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1"),
                ("OTEL_EXPORTER_OTLP_HEADERS", "x-generic=1"),
                ("OTEL_SERVICE_NAME", "kept"),
            ],
        );

        assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
        let stdout = text(&observation.stdout);
        assert!(stdout.contains(HIDDEN_LINE), "{observation:?}");
        assert!(
            stdout.contains("test result: ok. 1 passed"),
            "{observation:?}"
        );
    }

    #[test]
    #[ignore = "a fixture: it ends its own process, so it runs only as the child of the test that asserts on it"]
    fn fixture_an_interrupt_with_the_command_span_open() {
        if !started_as_fixture(INTERRUPT_TEST) {
            return;
        }
        // SAFETY: `signal` takes a signal number and a disposition, and the disposition is the constant for
        // the default action, which is no function the kernel would call; it touches no memory of the caller.
        // It gives this process the disposition a launcher starts one with, whatever the test run was started
        // with.
        let before = unsafe { libc::signal(libc::SIGINT, libc::SIG_DFL) };
        assert_ne!(before, libc::SIG_ERR, "the disposition of SIGINT is set");
        interrupt::install(&interrupt::INTERRUPTS);
        let env = ProcessEnv::from_vars(std::env::vars_os());
        let TraceSettings::On(config) = settings(&env, u64::MAX) else {
            panic!("the environment of the fixture turns tracing on");
        };
        let tracer = Tracer::new(TraceSettings::On(config));

        announce(&tracer, Logging::Unswitched);

        assert!(
            matches!(*tracer.state.borrow(), State::Open { .. }),
            "the command span is open"
        );
        say(READY_LINE);
        // SAFETY: `raise` takes a signal number and touches no memory of the caller. What it runs is the
        // handler installed above, which is async-signal-safe and ends the process.
        let raised = unsafe { libc::raise(libc::SIGINT) };
        assert_eq!(raised, 0, "SIGINT is raised");

        say("the handler returned\n");
        tracer.finish(None, &mut Vec::new());
    }

    #[test]
    fn an_interrupt_with_the_command_span_open_exits_1_and_exports_nothing() {
        let tree = TempTree::new().expect("an isolated tree");
        let trust_root = tree.root().join("tmp").join("trust-root.pem");
        std::fs::write(&trust_root, TRUST_ROOT).expect("the certificate file of the tree");
        let collector = Collector::start(Answer::Ok).expect("a collector");

        let observation = fixture(
            &tree,
            INTERRUPT_TEST,
            &[
                (ENDPOINT_VARIABLE, &collector.endpoint()),
                ("OTEL_SDK_DISABLED", "false"),
                (
                    "SSL_CERT_FILE",
                    trust_root.to_str().expect("a path inside a tree is text"),
                ),
            ],
        );

        assert_eq!(observation.exit, Exit::Code(1), "{observation:?}");
        assert_eq!(text(&observation.stderr), "aborted\n", "{observation:?}");
        let stdout = text(&observation.stdout);
        assert!(stdout.ends_with(READY_LINE), "{observation:?}");
        assert_eq!(collector.requests().len(), 0);
    }
}
