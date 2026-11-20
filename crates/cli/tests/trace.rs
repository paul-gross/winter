//! The command span, as the built binary exports it: what a trace collector on the loopback address receives,
//! and what a collector that is away, silent, failing, or slow with its answer leaves of the streams and the
//! exit code.
//!
//! Every invocation that traces reads its trust store from a file of its own tree, which holds the one
//! certificate compiled in here, so nothing of the host's store is read.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use winter_workspace_test_support::{
    Answer, Collector, Exit, ReceivedExport, ReceivedRequest, ReceivedSpan, RefusedEndpoint,
    TempTree, WorkspaceBuilder, decode_export, invoke,
};

/// The variable that names the collector, which turns tracing on.
const ENDPOINT: &str = "WINTER_OTEL_EXPORTER_OTLP_ENDPOINT";
const HEADERS: &str = "WINTER_OTEL_EXPORTER_OTLP_HEADERS";
/// The kill switch, which the pinned environment sets to `true`.
const KILL_SWITCH: &str = "OTEL_SDK_DISABLED";
/// The variable the certificate loader of the exporter reads its store from.
const TRUST_STORE: &str = "SSL_CERT_FILE";

/// A self-signed certificate made for the tests, whose key was discarded.
const TRUST_ROOT: &str = include_str!("data/trust-root.pem");

/// A trace id, a parent span id, and the sampled flag, as a caller hands them down.
const SAMPLED: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
/// The same with the sampled flag clear.
const UNSAMPLED: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00";

/// The longest an invocation may take whose collector is away, silent, failing, or slow with its answer:
/// twenty times what the binary waits for an export, so that the start of the process and the reading of the
/// trust store fit in it on a loaded machine. No test asserts a bound nearer to that wait.
const BOUND: Duration = Duration::from_secs(2);

/// A command that succeeds in the fixture workspace and writes one document to stdout.
const INDEX: &[&str] = &["ws", "index", "feature-x", "--json"];
/// The same command without its name: a usage error of the command.
const INDEX_WITHOUT_A_NAME: &[&str] = &["ws", "index"];

/// An isolated tree with a fixture workspace and the two trust stores a test names.
struct Fixture {
    tree: TempTree,
    workspace: PathBuf,
    /// A file that holds the one certificate.
    trust_root: String,
    /// A file that holds nothing: the store of a machine without certificates.
    empty_store: String,
}

/// How one invocation ended.
#[derive(Debug, PartialEq, Eq)]
struct Ended {
    stdout: String,
    stderr: String,
    exit: Exit,
}

/// What a collector saw of the one invocation that was handed its address, and how that invocation ended.
struct Alone {
    ended: Ended,
    elapsed: Duration,
    /// How many connections the collector accepted, the one that counted them left out.
    connections: usize,
    /// The requests the collector read, each sent to the path the invocation was handed.
    requests: Vec<ReceivedRequest>,
}

impl Fixture {
    fn new() -> Self {
        let tree = TempTree::new().expect("an isolated tree");
        let workspace = WorkspaceBuilder::new(&tree)
            .config("# workspace\n")
            .state("[env_index]\nfeature-x = 15\n")
            .build()
            .expect("a workspace");
        let tmp = tree.root().join("tmp");
        let trust_root = tmp.join("trust-root.pem");
        std::fs::write(&trust_root, TRUST_ROOT).expect("the certificate file of the tree");
        let empty_store = tmp.join("empty-store.pem");
        std::fs::write(&empty_store, "").expect("the empty store of the tree");
        let text = |path: PathBuf| {
            path.to_str()
                .expect("a path inside a tree is text")
                .to_owned()
        };
        Self {
            workspace: workspace.root.clone(),
            trust_root: text(trust_root),
            empty_store: text(empty_store),
            tree,
        }
    }

    /// Runs the built binary with `args` in the workspace, under the pinned environment with `variables`
    /// added, and returns how it ended with how long it ran.
    fn run(&self, args: &[&str], variables: &[(&str, &str)]) -> (Ended, Duration) {
        let mut invocation = self
            .tree
            .invocation(std::path::Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
        invocation.args = args.iter().map(Into::into).collect();
        invocation.cwd.clone_from(&self.workspace);
        for (key, value) in variables {
            invocation.env.set(key, value);
        }
        let started = Instant::now();
        let observation = invoke(invocation).expect("the binary runs to its end");
        let elapsed = started.elapsed();
        (
            Ended {
                stdout: String::from_utf8(observation.stdout).expect("UTF-8 on stdout"),
                stderr: String::from_utf8(observation.stderr).expect("UTF-8 on stderr"),
                exit: observation.exit,
            },
            elapsed,
        )
    }

    /// Runs `args` with tracing on towards `endpoint`: the kill switch cleared, the trust store the
    /// certificate file of the tree, and `variables` added after both.
    fn traced(
        &self,
        endpoint: &str,
        args: &[&str],
        variables: &[(&str, &str)],
    ) -> (Ended, Duration) {
        let mut all = vec![
            (ENDPOINT, endpoint),
            (KILL_SWITCH, "false"),
            (TRUST_STORE, self.trust_root.as_str()),
        ];
        all.extend_from_slice(variables);
        self.run(args, &all)
    }

    /// Runs `args` with tracing off, as the pinned environment leaves it.
    fn untraced(&self, args: &[&str]) -> Ended {
        self.run(args, &[]).0
    }

    /// Runs `args` once with tracing on and `variables` added, towards a collector of its own that gives
    /// `answer` and is addressed under `scheme`, and returns what that collector saw.
    ///
    /// The invocation is handed an address with a path no other is handed, the name of its tree, so every
    /// request it sends goes to the traces path under that name: the binary appends the traces path to an
    /// endpoint that has a path. A request the collector read at another target fails the test with the
    /// targets it read.
    ///
    /// A connection that brought no request the collector could read is not told from one of this invocation
    /// and stays counted.
    fn alone(
        &self,
        answer: Answer,
        scheme: &str,
        args: &[&str],
        variables: &[(&str, &str)],
    ) -> Alone {
        let own = self
            .tree
            .root()
            .file_name()
            .and_then(|name| name.to_str())
            .expect("the name of a tree is text");
        let own_target = format!("/{own}/v1/traces");
        let collector = collector(answer);
        let endpoint = collector.endpoint().replacen("http://", scheme, 1);
        assert!(
            endpoint.starts_with(&format!("{scheme}127.0.0.1:")),
            "{endpoint}"
        );

        let (ended, elapsed) = self.traced(&format!("{endpoint}/{own}"), args, variables);

        let connections = connections_before(&collector);
        let requests = collector.requests();
        let targets: Vec<&str> = requests
            .iter()
            .map(|request| request.target.as_str())
            .collect();
        assert!(
            targets.iter().all(|target| *target == own_target),
            "the collector read a request at another target than {own_target}; the targets it read: \
             {targets:?}"
        );
        Alone {
            ended,
            elapsed,
            connections,
            requests,
        }
    }
}

fn collector(answer: Answer) -> Collector {
    Collector::start(answer).expect("a collector on the loopback address")
}

/// The one request `collector` read.
fn one_request(collector: Collector) -> ReceivedRequest {
    let mut requests = collector.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    requests.remove(0)
}

/// The export of the one request `collector` read, which holds one span.
fn one_export(collector: Collector) -> (ReceivedExport, ReceivedSpan) {
    let request = one_request(collector);
    let mut export = decode_export(&request.body).expect("a trace export");
    assert_eq!(export.spans.len(), 1, "{export:?}");
    let span = export.spans.remove(0);
    (export, span)
}

/// How many connections `collector` accepted before this call, the one made here left out.
///
/// The collector takes its connections in the order they were made, and it closes this one, which sends it
/// bytes that are no request, only once it has taken it, so every earlier connection is counted by then, one
/// the child made and the collector had not yet taken included. A collector closes such a connection whatever
/// it answers a request, and what is sent here is not among its requests.
fn connections_before(collector: &Collector) -> usize {
    let address = collector.endpoint();
    let address = address
        .strip_prefix("http://")
        .expect("the endpoint of a collector");
    let mut stream = TcpStream::connect(address).expect("a connection to the collector");
    stream
        .set_read_timeout(Some(BOUND))
        .expect("a bound on the wait for the close");
    stream
        .write_all(b"counted\r\n\r\n")
        .expect("the bytes are written");
    let mut answer = Vec::new();
    stream
        .read_to_end(&mut answer)
        .expect("the collector closes the connection");
    assert_eq!(answer, b"", "no answer to bytes that are no request");
    collector.connections() - 1
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn pairs(literal: &[(&str, &str)]) -> Vec<(String, String)> {
    literal
        .iter()
        .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
        .collect()
}

fn header<'r>(request: &'r ReceivedRequest, name: &str) -> Vec<&'r str> {
    request
        .headers
        .iter()
        .filter(|(held, _)| held == name)
        .map(|(_, value)| value.as_str())
        .collect()
}

/// The records of origin `trace` among the lines of `stderr`.
fn trace_records(stderr: &str) -> Vec<&str> {
    stderr
        .lines()
        .filter(|line| line.contains(" trace: "))
        .collect()
}

#[test]
fn a_command_under_a_sampled_traceparent_exports_one_span_of_that_trace_started_at_the_launch_time()
{
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(
        &collector.endpoint(),
        INDEX,
        &[
            ("TRACEPARENT", SAMPLED),
            ("WINTER_LAUNCH_TIME", "1700000000,5"),
        ],
    );

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert_eq!(ended.stderr, "");
    assert_eq!(ended, fixture.untraced(INDEX));
    let (export, span) = one_export(collector);
    assert_eq!(span.name, "winter ws index");
    assert_eq!(span.kind, 1, "the kind internal");
    assert_eq!(hex(&span.trace_id), "0af7651916cd43dd8448eb211c80319c");
    assert_eq!(hex(&span.parent_span_id), "b7ad6b7169203331");
    assert_eq!(span.start_unix_nano, 1_700_000_000_500_000_000);
    assert!(span.end_unix_nano > span.start_unix_nano, "{span:?}");
    assert_eq!(
        span.attributes,
        pairs(&[("winter.command", "winter ws index")])
    );
    assert_eq!(span.status.map_or(0, |(code, _)| code), 0, "no status set");
    assert_eq!(export.resource, pairs(&[("service.name", "winter")]));
}

#[test]
fn the_export_is_one_post_of_protobuf_to_the_traces_path_of_the_endpoint() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), INDEX, &[]);

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert_eq!(collector.connections(), 1);
    let request = one_request(collector);
    assert_eq!(request.method, "POST");
    assert!(request.target.ends_with("/v1/traces"), "{request:?}");
    assert_eq!(header(&request, "content-type"), ["application/x-protobuf"]);
    let export = decode_export(&request.body).expect("a trace export");
    assert_eq!(export.spans.len(), 1, "{export:?}");
    assert!(export.spans[0].parent_span_id.is_empty(), "a root span");
}

#[test]
fn a_configured_header_is_sent_with_its_value_decoded_once() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    fixture.traced(
        &collector.endpoint(),
        INDEX,
        &[(HEADERS, "x-probe=a%2541,x-plain=b")],
    );

    let request = one_request(collector);
    assert_eq!(header(&request, "x-probe"), ["a%41"]);
    assert_eq!(header(&request, "x-plain"), ["b"]);
}

#[test]
fn a_usage_error_of_a_command_exports_the_span_with_its_error_type_and_an_error_status() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), INDEX_WITHOUT_A_NAME, &[]);

    assert_eq!(ended.exit, Exit::Code(2), "{ended:?}");
    assert_eq!(ended, fixture.untraced(INDEX_WITHOUT_A_NAME));
    let (_, span) = one_export(collector);
    assert_eq!(span.name, "winter ws index");
    assert_eq!(
        span.attributes,
        pairs(&[
            ("winter.command", "winter ws index"),
            ("error.type", "Usage")
        ])
    );
    assert_eq!(span.status, Some((2, String::new())));
}

#[test]
fn a_span_limit_or_a_sampler_of_the_environment_changes_nothing_of_what_is_sent() {
    let fixture = Fixture::new();
    for variable in [
        ("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "0"),
        ("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "1"),
        ("OTEL_TRACES_SAMPLER", "always_off"),
    ] {
        let collector = collector(Answer::Ok);

        let (ended, _) = fixture.traced(&collector.endpoint(), INDEX_WITHOUT_A_NAME, &[variable]);

        assert_eq!(ended.exit, Exit::Code(2), "{variable:?}: {ended:?}");
        let (_, span) = one_export(collector);
        assert_eq!(
            span.attributes,
            pairs(&[
                ("winter.command", "winter ws index"),
                ("error.type", "Usage")
            ]),
            "{variable:?}"
        );
    }
}

#[test]
fn an_invocation_that_ends_at_the_root_makes_no_connection() {
    let fixture = Fixture::new();
    for args in [&["--version"][..], &["nope"], &[]] {
        let collector = collector(Answer::Ok);

        let (ended, _) = fixture.traced(&collector.endpoint(), args, &[]);

        assert_eq!(ended, fixture.untraced(args), "{args:?}");
        assert_eq!(collector.connections(), 0, "{args:?}");
        assert_eq!(collector.requests().len(), 0, "{args:?}");
    }
}

#[test]
fn the_kill_switch_and_a_blank_endpoint_each_leave_tracing_off() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);
    let endpoint = collector.endpoint();
    let trust_store = (TRUST_STORE, fixture.trust_root.as_str());

    let (killed, _) = fixture.run(INDEX, &[(ENDPOINT, &endpoint), trust_store]);
    let (blank, _) = fixture.run(
        INDEX,
        &[(ENDPOINT, "  "), (KILL_SWITCH, "false"), trust_store],
    );

    let untraced = fixture.untraced(INDEX);
    assert_eq!(killed, untraced);
    assert_eq!(blank, untraced);
    assert_eq!(collector.connections(), 0);
    assert_eq!(collector.requests().len(), 0);
}

#[test]
fn an_unsampled_traceparent_sends_no_request() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), INDEX, &[("TRACEPARENT", UNSAMPLED)]);

    assert_eq!(ended, fixture.untraced(INDEX));
    assert_eq!(collector.requests().len(), 0);
}

/// What keeps the address of a failing endpoint as it is for as long as an invocation may use it.
enum Held {
    Collector(Collector),
    Refused(RefusedEndpoint),
}

impl Held {
    fn endpoint(&self) -> String {
        match self {
            Self::Collector(collector) => collector.endpoint(),
            Self::Refused(refused) => refused.endpoint(),
        }
    }
}

/// A collector that is silent, one that fails, and an address at which nothing listens, each with what keeps
/// it so: the caller holds that until the invocation it handed the endpoint to has ended.
fn failing_endpoints() -> Vec<(&'static str, Held)> {
    let refused = RefusedEndpoint::hold().expect("an address at which nothing listens");
    vec![
        (
            "a silent collector",
            Held::Collector(collector(Answer::Hang)),
        ),
        (
            "a failing collector",
            Held::Collector(collector(Answer::Status(500))),
        ),
        ("a refused endpoint", Held::Refused(refused)),
    ]
}

#[test]
fn a_collector_that_is_silent_failing_or_away_changes_nothing_and_does_not_keep_the_process() {
    let fixture = Fixture::new();
    for args in [INDEX, INDEX_WITHOUT_A_NAME] {
        let untraced = fixture.untraced(args);
        for (name, held) in failing_endpoints() {
            let (ended, elapsed) = fixture.traced(&held.endpoint(), args, &[]);

            assert_eq!(ended, untraced, "{name}: {args:?}");
            assert!(elapsed < BOUND, "{name}: {args:?}: {elapsed:?}");
        }
    }
}

#[test]
fn a_delivered_export_writes_no_record_about_tracing_under_the_verbose_flag() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), &[&["-v"], INDEX].concat(), &[]);

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert!(trace_records(&ended.stderr).is_empty(), "{ended:?}");
    assert_eq!(ended.stderr, "DEBUG parse: command path: ws index\n");
    assert_eq!(collector.requests().len(), 1);
}

#[test]
fn an_export_that_fails_is_one_error_record_about_tracing_under_the_verbose_flag() {
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);
    for (name, held) in failing_endpoints() {
        let (ended, elapsed) = fixture.traced(&held.endpoint(), &[&["-v"], INDEX].concat(), &[]);

        assert_eq!(ended.exit, Exit::Code(0), "{name}: {ended:?}");
        assert_eq!(ended.stdout, untraced.stdout, "{name}");
        let records = trace_records(&ended.stderr);
        let [record] = records.as_slice() else {
            panic!("{name}: expected one record about tracing: {ended:?}");
        };
        assert!(
            record.starts_with("ERROR trace: the command span was not exported: "),
            "{name}: {record}"
        );
        assert!(elapsed < BOUND, "{name}: {elapsed:?}");
    }
}

#[test]
fn an_answer_the_exporter_would_retry_and_a_silent_collector_each_see_one_connection() {
    let fixture = Fixture::new();
    for answer in [Answer::Status(503), Answer::Hang] {
        let alone = fixture.alone(answer, "http://", INDEX, &[]);

        assert_eq!(
            alone.ended.exit,
            Exit::Code(0),
            "{answer:?}: {:?}",
            alone.ended
        );
        assert_eq!(alone.connections, 1, "{answer:?}");
        assert_eq!(alone.requests.len(), 1, "{answer:?}: {:?}", alone.requests);
    }
}

/// Runs the command against a collector that gives `answer`, which keeps its client waiting for a body, once
/// without a logging switch and once with `-v`.
///
/// The collector holds its connection until it is stopped, which each round does only after the process has
/// ended: a binary that waits for the collector never ends by itself, and fails at the deadline of the runner
/// instead of passing late. Nothing here waits a fixed time.
fn a_body_that_does_not_arrive_changes_nothing_and_does_not_keep_the_process(answer: Answer) {
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);

    let plain = collector(answer);
    let (ended, elapsed) = fixture.traced(&plain.endpoint(), INDEX, &[]);

    assert_eq!(ended, untraced, "{answer:?}");
    assert!(elapsed < BOUND, "{answer:?}: {elapsed:?}");
    assert_eq!(plain.requests().len(), 1, "{answer:?}");

    let verbose = collector(answer);
    let (ended, elapsed) = fixture.traced(&verbose.endpoint(), &[&["-v"], INDEX].concat(), &[]);

    assert_eq!(ended.exit, untraced.exit, "{answer:?}: {ended:?}");
    assert_eq!(ended.stdout, untraced.stdout, "{answer:?}");
    let records = trace_records(&ended.stderr);
    let [record] = records.as_slice() else {
        panic!("{answer:?}: expected one record about tracing: {ended:?}");
    };
    assert!(
        record.starts_with("ERROR trace: the command span was not exported: "),
        "{answer:?}: {record}"
    );
    assert!(elapsed < BOUND, "{answer:?}: {elapsed:?}");
    assert_eq!(verbose.requests().len(), 1, "{answer:?}");
}

#[test]
fn a_collector_that_answers_a_head_and_no_body_changes_nothing_and_does_not_keep_the_process() {
    a_body_that_does_not_arrive_changes_nothing_and_does_not_keep_the_process(Answer::Stall);
}

/// The case the binary's own wait alone holds: every read of the dripped body succeeds within the timeout of
/// the exporter, so an export bounded by that timeout would last as long as the collector drips.
#[test]
fn a_collector_that_drips_its_body_changes_nothing_and_does_not_keep_the_process() {
    a_body_that_does_not_arrive_changes_nothing_and_does_not_keep_the_process(Answer::Drip);
}

#[test]
fn a_trust_store_that_holds_no_certificate_leaves_tracing_unavailable_and_the_command_as_it_was() {
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);
    let empty_store = [(TRUST_STORE, fixture.empty_store.as_str())];
    let collector = collector(Answer::Ok);
    let endpoint = collector.endpoint();

    let (plain, _) = fixture.traced(&endpoint, INDEX, &empty_store);
    let (verbose, _) = fixture.traced(&endpoint, &[&["-v"], INDEX].concat(), &empty_store);

    assert_eq!(plain, untraced);
    assert_eq!(verbose.exit, Exit::Code(0), "{verbose:?}");
    assert_eq!(verbose.stdout, untraced.stdout);
    let records = trace_records(&verbose.stderr);
    let [record] = records.as_slice() else {
        panic!("expected one record about tracing: {verbose:?}");
    };
    assert!(
        record.starts_with("DEBUG trace: tracing is unavailable: "),
        "{record}"
    );
    assert_eq!(verbose.stderr.lines().count(), 2, "{verbose:?}");
    assert_eq!(collector.connections(), 0);
    assert_eq!(collector.requests().len(), 0);
}

#[test]
fn an_https_endpoint_is_served_and_its_failed_export_is_bounded() {
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);

    let alone = fixture.alone(Answer::Ok, "https://", INDEX, &[]);

    assert_eq!(alone.ended, untraced);
    assert!(alone.elapsed < BOUND, "{:?}", alone.elapsed);
    assert_eq!(alone.connections, 1);
    assert_eq!(alone.requests.len(), 0, "{:?}", alone.requests);
}

#[test]
fn a_generic_exporter_endpoint_neither_redirects_the_export_nor_turns_tracing_on() {
    let fixture = Fixture::new();
    let named = collector(Answer::Ok);
    let generic = collector(Answer::Ok);
    let generic_endpoint = generic.endpoint();
    let generic_variables = [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", generic_endpoint.as_str()),
        (
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            generic_endpoint.as_str(),
        ),
    ];

    let (ended, _) = fixture.traced(&named.endpoint(), INDEX, &generic_variables);

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert_eq!(generic.connections(), 0);
    assert_eq!(named.connections(), 1);
    let without_the_winter_endpoint = [
        generic_variables[0],
        generic_variables[1],
        (KILL_SWITCH, "false"),
        (TRUST_STORE, fixture.trust_root.as_str()),
    ];
    let (ended, _) = fixture.run(INDEX, &without_the_winter_endpoint);

    assert_eq!(ended, fixture.untraced(INDEX));
    assert_eq!(generic.connections(), 0);
    assert_eq!(
        named.connections(),
        1,
        "the one connection of the run before"
    );
    assert_eq!(generic.requests().len(), 0);
    assert_eq!(named.requests().len(), 1);
}

#[test]
fn a_generic_exporter_header_is_not_sent() {
    let fixture = Fixture::new();
    for generic in [
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    ] {
        let collector = collector(Answer::Ok);

        let (ended, _) = fixture.traced(
            &collector.endpoint(),
            INDEX,
            &[(generic, "x-generic=1"), (HEADERS, "x-named=2")],
        );

        assert_eq!(ended.exit, Exit::Code(0), "{generic}: {ended:?}");
        let request = one_request(collector);
        assert!(header(&request, "x-generic").is_empty(), "{generic}");
        assert_eq!(header(&request, "x-named"), ["2"], "{generic}");
    }
}

#[test]
fn a_generic_exporter_variable_the_binary_hid_from_its_exporter_reaches_the_git_it_starts() {
    let fixture = Fixture::new();
    std::fs::write(
        fixture.workspace.join(".winter/config.toml"),
        "[[project_repository]]\nname = \"api\"\n",
    )
    .expect("the configuration of the fixture");
    let worktree = fixture.workspace.join("alpha/api");
    std::fs::create_dir_all(&worktree).expect("a worktree directory");
    std::fs::write(worktree.join(".git"), "gitdir: /nowhere\n").expect("a repository by its file");
    // A stand-in for git, first on the pinned `PATH`: it writes its environment to a file of its own under
    // `calls/` and answers as a git whose working tree is clean and level with its main branch.
    let calls = fixture.tree.root().join("calls");
    let stub = fixture.tree.root().join("bin/git");
    std::fs::write(
        &stub,
        format!(
            "#!/bin/sh\nenv >'{}'/\"$1\"\ncase \"$1\" in rev-list) printf '0\\t0\\n' ;; esac\n",
            calls.display()
        ),
    )
    .expect("the stand-in");
    std::fs::set_permissions(&stub, std::os::unix::fs::PermissionsExt::from_mode(0o755))
        .expect("a mode");
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(
        &collector.endpoint(),
        &["ws", "worktrees", "--status", "--json"],
        &[("OTEL_EXPORTER_OTLP_HEADERS", "x-generic=1")],
    );

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert_eq!(ended.stderr, "", "{ended:?}");
    assert!(ended.stdout.contains("\"dirty\":0"), "{ended:?}");
    let request = one_request(collector);
    assert!(
        header(&request, "x-generic").is_empty(),
        "the exporter of the binary did not read the variable"
    );
    for call in ["status", "rev-list"] {
        let environment = std::fs::read_to_string(calls.join(call)).expect("a recorded call");
        assert!(
            environment
                .lines()
                .any(|line| line == "OTEL_EXPORTER_OTLP_HEADERS=x-generic=1"),
            "{call}: {environment}"
        );
    }
}

#[test]
fn a_generic_exporter_compression_the_binary_cannot_do_does_not_keep_the_export_back() {
    let fixture = Fixture::new();
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(
        &collector.endpoint(),
        &[&["-v"], INDEX].concat(),
        &[("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip")],
    );

    assert_eq!(ended.exit, Exit::Code(0), "{ended:?}");
    assert!(trace_records(&ended.stderr).is_empty(), "{ended:?}");
    let request = one_request(collector);
    assert!(header(&request, "content-encoding").is_empty());
    let export = decode_export(&request.body).expect("a trace export that is not compressed");
    assert_eq!(export.spans.len(), 1, "{export:?}");
}

#[test]
fn a_configured_user_agent_replaces_the_one_of_the_exporter_in_every_invocation() {
    let fixture = Fixture::new();
    // Which of two keys the exporter would send differs from process to process: twenty of them.
    for run in 0..20 {
        let collector = collector(Answer::Ok);

        fixture.traced(
            &collector.endpoint(),
            INDEX,
            &[(HEADERS, "User-Agent=probe/1")],
        );

        let request = one_request(collector);
        assert_eq!(header(&request, "user-agent"), ["probe/1"], "run {run}");
    }
}

#[test]
fn a_configured_content_type_is_not_sent_and_a_header_with_an_illegal_value_is_left_out_and_named()
{
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);
    let headers = [(
        HEADERS,
        "content-type=text/plain,x-bad=s3cr3t%0Aprobe,x-plain=b",
    )];

    let plain = fixture.alone(Answer::Ok, "http://", INDEX, &headers);

    assert_eq!(plain.ended, untraced);
    let [request] = plain.requests.as_slice() else {
        panic!("expected one request: {:?}", plain.requests);
    };
    assert_eq!(header(request, "content-type"), ["application/x-protobuf"]);
    assert_eq!(header(request, "x-plain"), ["b"]);
    assert!(header(request, "x-bad").is_empty(), "{request:?}");
    let export = decode_export(&request.body).expect("a trace export");
    assert_eq!(export.spans.len(), 1, "{export:?}");

    let verbose = fixture.alone(Answer::Ok, "http://", &[&["-v"], INDEX].concat(), &headers);
    let ended = &verbose.ended;

    assert_eq!(ended.exit, untraced.exit, "{ended:?}");
    assert_eq!(ended.stdout, untraced.stdout);
    let records = trace_records(&ended.stderr);
    let [record] = records.as_slice() else {
        panic!("expected one record about tracing: {ended:?}");
    };
    assert!(record.starts_with("DEBUG trace: "), "{record}");
    assert!(record.contains("x-bad"), "{record}");
    for word in ["s3cr3t", "probe", "content-type"] {
        assert!(!ended.stderr.contains(word), "{word}: {ended:?}");
    }
    let [request] = verbose.requests.as_slice() else {
        panic!("expected one request: {:?}", verbose.requests);
    };
    assert_eq!(header(request, "content-type"), ["application/x-protobuf"]);
    assert_eq!(header(request, "x-plain"), ["b"]);
    assert!(header(request, "x-bad").is_empty(), "{request:?}");
}

#[test]
fn a_header_value_with_a_tab_and_a_byte_above_the_ascii_range_arrives_as_it_was_configured() {
    let fixture = Fixture::new();
    let untraced = fixture.untraced(INDEX);
    let headers = [(HEADERS, "x-tab=a%09b,x-high=caf%C3%A9,x-plain=b")];

    let verbose = fixture.alone(Answer::Ok, "http://", &[&["-v"], INDEX].concat(), &headers);

    assert_eq!(verbose.ended.exit, untraced.exit, "{:?}", verbose.ended);
    assert_eq!(verbose.ended.stdout, untraced.stdout);
    assert!(
        trace_records(&verbose.ended.stderr).is_empty(),
        "{:?}",
        verbose.ended
    );
    let [request] = verbose.requests.as_slice() else {
        panic!("expected one request: {:?}", verbose.requests);
    };
    assert_eq!(header(request, "x-tab"), ["a\tb"]);
    assert_eq!(header(request, "x-high"), ["caf\u{e9}"]);
    assert_eq!(header(request, "x-plain"), ["b"]);
}

/// A stand-in `git` that answers the commit count of a clean worktree and nothing else.
const CLEAN_GIT: &str = "case \"$1\" in rev-list) printf '0\\t0\\n' ;; esac";
/// A stand-in `git` that fails as git fails outside a repository.
const FAILING_GIT: &str = "echo 'fatal: the stand-in fails' >&2\nexit 128";

/// An isolated tree with a workspace of two projects, one environment holding a worktree of each, and a
/// stand-in `git` that ends as `git_ending` says.
fn probed_fixture(git_ending: &str) -> Fixture {
    use std::os::unix::fs::PermissionsExt;

    let tree = TempTree::new().expect("an isolated tree");
    let workspace = WorkspaceBuilder::new(&tree)
        .config(
            "[[project_repository]]\nname = \"api\"\n\n[[project_repository]]\nname = \"web\"\n",
        )
        .state("[env_index]\nalpha = 1\n")
        .file("alpha/api/.git", b"gitdir: /nowhere\n")
        .file("alpha/web/.git", b"gitdir: /nowhere\n")
        .build()
        .expect("a workspace");
    let stub = tree.root().join("bin/git");
    std::fs::write(&stub, format!("#!/bin/sh\n{git_ending}\n")).expect("the stand-in");
    std::fs::set_permissions(&stub, std::fs::Permissions::from_mode(0o755)).expect("a mode");
    let trust_root = tree.root().join("tmp/trust-root.pem");
    std::fs::write(&trust_root, TRUST_ROOT).expect("the certificate file of the tree");
    let text = |path: PathBuf| {
        path.to_str()
            .expect("a path of plain characters")
            .to_owned()
    };
    Fixture {
        workspace: workspace.root.clone(),
        trust_root: text(trust_root),
        empty_store: String::new(),
        tree,
    }
}

fn export_of(collector: Collector) -> ReceivedExport {
    let request = one_request(collector);
    decode_export(&request.body).expect("a trace export")
}

fn attribute<'s>(span: &'s ReceivedSpan, key: &str) -> Option<&'s str> {
    span.attributes
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value.as_str())
}

#[test]
fn a_status_listing_exports_one_git_status_span_per_probe_in_the_one_export() {
    let fixture = probed_fixture(CLEAN_GIT);
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), &["ws", "worktrees", "--status"], &[]);
    let export = export_of(collector);

    assert_eq!(ended.exit, Exit::Code(0), "{}", ended.stderr);
    assert_eq!(export.spans.len(), 4, "{export:?}");
    let command = export
        .spans
        .iter()
        .find(|span| span.name == "winter ws worktrees")
        .expect("the command span");
    let children: Vec<&ReceivedSpan> = export
        .spans
        .iter()
        .filter(|span| span.name == "git status")
        .collect();
    assert_eq!(children.len(), 3, "{export:?}");
    let root_name = fixture
        .workspace
        .file_name()
        .and_then(|name| name.to_str())
        .expect("a root name")
        .to_owned();
    let mut seen: Vec<Vec<(String, String)>> = Vec::new();
    for child in &children {
        assert_eq!(child.parent_span_id, command.span_id);
        assert_eq!(child.trace_id, command.trace_id);
        assert_ne!(
            child.status.as_ref().map(|status| status.0),
            Some(2),
            "not failed"
        );
        seen.push(child.attributes.clone());
    }
    seen.sort();
    let mut expected = vec![
        pairs(&[("winter.repo", "api"), ("winter.env", "alpha")]),
        pairs(&[("winter.repo", "web"), ("winter.env", "alpha")]),
        pairs(&[("winter.repo", &root_name)]),
    ];
    expected.sort();
    assert_eq!(seen, expected);
}

#[test]
fn a_failing_worktree_probe_exports_its_span_failed_with_the_repo_error_type() {
    let fixture = probed_fixture(FAILING_GIT);
    let collector = collector(Answer::Ok);

    let (ended, _) = fixture.traced(&collector.endpoint(), &["ws", "worktrees", "--status"], &[]);
    let export = export_of(collector);

    assert_ne!(ended.exit, Exit::Code(0));
    let api = export
        .spans
        .iter()
        .find(|span| attribute(span, "winter.repo") == Some("api"))
        .expect("the span of the failing probe");
    assert_eq!(api.name, "git status");
    assert_eq!(attribute(api, "error.type"), Some("RepoError"));
    let (code, message) = api.status.clone().expect("a status");
    assert_eq!(code, 2, "ERROR");
    assert_eq!(message, "");
}

#[test]
fn without_status_or_without_tracing_no_operation_span_is_exported() {
    let fixture = probed_fixture(CLEAN_GIT);
    let collector = collector(Answer::Ok);
    let (ended, _) = fixture.traced(&collector.endpoint(), &["ws", "worktrees"], &[]);
    assert_eq!(ended.exit, Exit::Code(0), "{}", ended.stderr);
    let export = export_of(collector);
    assert_eq!(export.spans.len(), 1, "{export:?}");

    let collector = collector_for_off();
    let ended = fixture
        .run(
            &["ws", "worktrees", "--status"],
            &[(ENDPOINT, &collector.endpoint())],
        )
        .0;
    assert_eq!(ended.exit, Exit::Code(0), "{}", ended.stderr);
    assert!(collector.requests().is_empty());
}

fn collector_for_off() -> Collector {
    collector(Answer::Ok)
}
