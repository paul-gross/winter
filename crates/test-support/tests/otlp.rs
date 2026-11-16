//! The loopback collector: what it records of a request, what it answers, and how it stops.
//!
//! Every client here is a plain `TcpStream` of the test, connected to the collector the test started and to
//! nothing else.

use std::io::{ErrorKind, Read, Write};
use std::net::{Ipv4Addr, Shutdown, SocketAddrV4, TcpListener, TcpStream};
use std::time::Duration;

use winter_workspace_test_support::{Answer, Collector, ReceivedRequest};

/// Longer than any answer of a collector takes; a read that waits this long fails its test.
const PATIENCE: Duration = Duration::from_secs(30);
/// How long the client of a collector that hangs waits for an answer before it gives up.
const HANG_DEADLINE: Duration = Duration::from_millis(200);

const BODY: &[u8] = b"\x0a\x00 a body\r\n\r\nwith a blank line in it\x00\xff";

/// The port of `endpoint`, which has to be a loopback address written as a number.
fn port_of(endpoint: &str) -> u16 {
    endpoint
        .strip_prefix("http://127.0.0.1:")
        .unwrap_or_else(|| panic!("no loopback endpoint: {endpoint}"))
        .parse()
        .unwrap_or_else(|error| panic!("no port in {endpoint}: {error}"))
}

fn loopback(port: u16) -> SocketAddrV4 {
    SocketAddrV4::new(Ipv4Addr::LOCALHOST, port)
}

fn connect(endpoint: &str) -> TcpStream {
    let stream = TcpStream::connect(loopback(port_of(endpoint))).expect("a connection");
    stream
        .set_read_timeout(Some(PATIENCE))
        .expect("a read deadline");
    stream
}

/// A `POST` of [`BODY`] to `/v1/traces`, with its length and two more headers.
fn post() -> Vec<u8> {
    let head = format!(
        "POST /v1/traces HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/x-protobuf\r\n\
         X-Tenant:  blue \r\nContent-Length: {}\r\n\r\n",
        BODY.len()
    );
    [head.as_bytes(), BODY].concat()
}

fn posted() -> ReceivedRequest {
    let headers = [
        ("host", "127.0.0.1"),
        ("content-type", "application/x-protobuf"),
        ("x-tenant", "blue"),
        ("content-length", &BODY.len().to_string()),
    ];
    ReceivedRequest {
        method: "POST".to_owned(),
        target: "/v1/traces".to_owned(),
        headers: headers
            .iter()
            .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
            .collect(),
        body: BODY.to_vec(),
    }
}

/// Everything the collector sends on `stream` until it closes the connection.
fn answer_on(mut stream: TcpStream) -> String {
    let mut answer = String::new();
    stream.read_to_string(&mut answer).expect("an answer");
    answer
}

#[test]
fn a_post_is_answered_with_a_success_status_and_recorded_whole() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client.write_all(&post()).expect("a request sent");
    let answer = answer_on(client);

    assert!(answer.starts_with("HTTP/1.1 200 "), "{answer}");
    assert_eq!(collector.connections(), 1);
    assert_eq!(collector.requests(), [posted()]);
}

#[test]
fn a_request_that_arrives_in_pieces_is_recorded_whole() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());
    client.set_nodelay(true).expect("no delay");

    for piece in post().chunks(7) {
        client.write_all(piece).expect("a piece sent");
        client.flush().expect("a piece flushed");
    }
    let answer = answer_on(client);

    assert!(answer.starts_with("HTTP/1.1 200 "), "{answer}");
    assert_eq!(collector.requests(), [posted()]);
}

#[test]
fn a_request_with_no_content_length_has_an_empty_body() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client
        .write_all(b"GET /health HTTP/1.1\r\n\r\n")
        .expect("a request sent");
    let answer = answer_on(client);

    assert!(answer.starts_with("HTTP/1.1 200 "), "{answer}");
    assert_eq!(
        collector.requests(),
        [ReceivedRequest {
            method: "GET".to_owned(),
            target: "/health".to_owned(),
            headers: Vec::new(),
            body: Vec::new(),
        }]
    );
}

#[test]
fn every_connection_is_served_and_its_request_recorded_in_order() {
    let collector = Collector::start(Answer::Ok).expect("a collector");

    for target in ["/first", "/second", "/third"] {
        let mut client = connect(&collector.endpoint());
        client
            .write_all(format!("POST {target} HTTP/1.1\r\nContent-Length: 0\r\n\r\n").as_bytes())
            .expect("a request sent");
        let answer = answer_on(client);
        assert!(answer.starts_with("HTTP/1.1 200 "), "{target}: {answer}");
    }

    assert_eq!(collector.connections(), 3);
    let targets: Vec<String> = collector
        .requests()
        .into_iter()
        .map(|request| request.target)
        .collect();
    assert_eq!(targets, ["/first", "/second", "/third"]);
}

#[test]
fn a_status_answer_is_what_the_client_reads_and_the_request_is_recorded() {
    let collector = Collector::start(Answer::Status(500)).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client.write_all(&post()).expect("a request sent");
    let answer = answer_on(client);

    assert!(answer.starts_with("HTTP/1.1 500 "), "{answer}");
    assert_eq!(collector.requests(), [posted()]);
}

#[test]
fn a_collector_that_hangs_records_the_request_and_answers_nothing() {
    let collector = Collector::start(Answer::Hang).expect("a collector");
    let mut client = connect(&collector.endpoint());
    client
        .set_read_timeout(Some(HANG_DEADLINE))
        .expect("a read deadline");

    client.write_all(&post()).expect("a request sent");
    let mut byte = [0; 1];
    let read = client.read(&mut byte);

    let error = read.expect_err("no answer and an open connection until the deadline");
    assert!(
        matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut),
        "{error:?}"
    );
    assert_eq!(collector.connections(), 1);
    assert_eq!(collector.requests(), [posted()]);
}

#[test]
fn bytes_that_are_no_request_count_as_a_connection_and_add_no_request() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());

    // The opening bytes of a TLS handshake, and then the end of what the client sends.
    client
        .write_all(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03")
        .expect("bytes sent");
    client.shutdown(Shutdown::Write).expect("the sending ended");
    let mut answer = Vec::new();
    client
        .read_to_end(&mut answer)
        .expect("the connection closed");

    assert_eq!(answer, b"");
    assert_eq!(collector.connections(), 1);
    assert_eq!(collector.requests(), []);
}

#[test]
fn a_chunked_body_counts_as_a_connection_and_adds_no_request() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client
        .write_all(b"POST /v1/traces HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
        .expect("a request sent");
    client.shutdown(Shutdown::Write).expect("the sending ended");
    let mut answer = Vec::new();
    client
        .read_to_end(&mut answer)
        .expect("the connection closed");

    assert_eq!(answer, b"");
    assert_eq!(collector.connections(), 1);
    assert_eq!(collector.requests(), []);
}

#[test]
fn a_collector_no_client_reached_has_no_connection_and_no_request() {
    let collector = Collector::start(Answer::Ok).expect("a collector");

    assert_eq!(collector.connections(), 0);
    assert_eq!(collector.requests(), []);
}

#[test]
fn nothing_accepts_at_the_refused_endpoint() {
    let endpoint = Collector::refused_endpoint().expect("an endpoint");

    let connection = TcpStream::connect(loopback(port_of(&endpoint)));

    let error = connection.expect_err("a refused connection");
    assert_eq!(error.kind(), ErrorKind::ConnectionRefused, "{error:?}");
}

#[test]
fn dropping_the_collector_ends_its_thread_and_frees_its_port() {
    let collector = Collector::start(Answer::Hang).expect("a collector");
    let port = port_of(&collector.endpoint());
    let mut client = connect(&collector.endpoint());
    client.write_all(&post()).expect("a request sent");

    drop(collector);

    // The thread held the connection open and silent; its end closes it.
    let mut answer = Vec::new();
    client
        .read_to_end(&mut answer)
        .expect("the connection closed");
    assert_eq!(answer, b"");
    TcpListener::bind(loopback(port)).expect("the port of the collector, free again");
}

/// The most bytes of a body the collector reads: 16 MiB.
const LARGEST_BODY: usize = 16 * 1024 * 1024;

#[test]
fn a_body_of_the_largest_length_the_collector_reads_is_recorded_whole() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());
    let body = vec![0x5a; LARGEST_BODY];

    client
        .write_all(
            format!("POST /v1/traces HTTP/1.1\r\nContent-Length: {LARGEST_BODY}\r\n\r\n")
                .as_bytes(),
        )
        .expect("a head sent");
    client.write_all(&body).expect("a body sent");
    let answer = answer_on(client);

    assert!(answer.starts_with("HTTP/1.1 200 "), "{answer}");
    let requests = collector.requests();
    assert_eq!(requests.len(), 1);
    assert!(
        requests[0].body == body,
        "the body differs from what was sent"
    );
}

#[test]
fn a_body_longer_than_the_collector_reads_adds_no_request() {
    let collector = Collector::start(Answer::Ok).expect("a collector");
    let mut client = connect(&collector.endpoint());
    let body = vec![0x5a; LARGEST_BODY + 1];

    client
        .write_all(
            format!(
                "POST /v1/traces HTTP/1.1\r\nContent-Length: {}\r\n\r\n",
                body.len()
            )
            .as_bytes(),
        )
        .expect("a head sent");
    // The collector closes the connection at the head, so the body may not go through.
    let _ = client.write_all(&body);

    assert_eq!(collector.requests(), []);
}

/// The head a collector that stalls or drips answers a request with.
const ANNOUNCING_HEAD: &[u8] = b"HTTP/1.1 200 OK\r\ncontent-length: 1048576\r\n\r\n";
/// How long the client of a collector that stalls waits for a byte of the body before it gives up.
const STALL_DEADLINE: Duration = Duration::from_millis(300);
/// How long the client of a collector that drips waits for each byte of the body, which is due every 20 ms.
const DRIP_DEADLINE: Duration = Duration::from_secs(2);

/// Reads the head of an answer that announces a body from `client`, and nothing after it.
fn announcing_head_on(client: &mut TcpStream) {
    let mut head = vec![0; ANNOUNCING_HEAD.len()];
    client.read_exact(&mut head).expect("the head of an answer");
    assert_eq!(
        String::from_utf8_lossy(&head),
        String::from_utf8_lossy(ANNOUNCING_HEAD)
    );
}

#[test]
fn a_collector_that_stalls_records_the_request_and_answers_a_head_and_none_of_the_body_it_announces()
 {
    let collector = Collector::start(Answer::Stall).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client.write_all(&post()).expect("a request sent");
    announcing_head_on(&mut client);
    client
        .set_read_timeout(Some(STALL_DEADLINE))
        .expect("a read deadline");
    let mut byte = [0; 1];
    let read = client.read(&mut byte);

    let error = read.expect_err("no byte of the body and an open connection until the deadline");
    assert!(
        matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut),
        "{error:?}"
    );
    assert_eq!(collector.connections(), 1);
    // The held connection adds no wait to the stop: a stop that waited for it would never return.
    assert_eq!(collector.requests(), [posted()]);
}

#[test]
fn a_collector_that_drips_records_the_request_and_sends_the_body_it_announces_a_byte_at_a_time() {
    let collector = Collector::start(Answer::Drip).expect("a collector");
    let mut client = connect(&collector.endpoint());

    client.write_all(&post()).expect("a request sent");
    announcing_head_on(&mut client);
    client
        .set_read_timeout(Some(DRIP_DEADLINE))
        .expect("a read deadline");
    // Each read is given room for the whole body and returns what has arrived: two bytes in one of them would
    // be two bytes the collector wrote less than its pause apart, or a client that did not run for that pause.
    let mut room = [0xff; 64];
    for nth in 1..=3 {
        let count = client.read(&mut room).expect("a byte of the body");
        assert_eq!(room[..count], [0], "byte {nth} of the body");
    }

    drop(collector);

    // Bytes that were under way are read first; then the connection is found closed, not silent.
    let mut rest = Vec::new();
    client
        .read_to_end(&mut rest)
        .expect("the connection closed");
    assert!(rest.iter().all(|byte| *byte == 0), "{rest:?}");
}

#[test]
fn a_collector_that_drips_still_serves_a_client_after_another_left() {
    let collector = Collector::start(Answer::Drip).expect("a collector");
    let mut gone = connect(&collector.endpoint());
    gone.write_all(&post()).expect("a request sent");
    announcing_head_on(&mut gone);
    drop(gone);

    let mut client = connect(&collector.endpoint());
    client.write_all(&post()).expect("a request sent");
    announcing_head_on(&mut client);
    let mut byte = [0xff; 1];
    client.read_exact(&mut byte).expect("a byte of the body");

    assert_eq!(byte, [0]);
    assert_eq!(collector.requests(), [posted(), posted()]);
}
