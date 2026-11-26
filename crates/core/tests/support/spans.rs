//! A tracer that records the operation spans a test opens, for the component tests that assert them.

use std::sync::Mutex;

use winter_workspace_core::{CommandTracer, OperationSpan, TraceLog};

/// The name of a span and its attributes.
pub type Span = (String, Vec<(String, String)>);

/// Records every operation span opened through it, in order.
#[derive(Default)]
pub struct Spans(pub Mutex<Vec<Span>>);

struct Open;

impl OperationSpan for Open {
    fn failed(&mut self, _: &'static str) {}
}

impl CommandTracer for Spans {
    fn command_started(&self, _: &[String], _: TraceLog) {}

    fn operation(
        &self,
        name: &'static str,
        attributes: &[(&'static str, &str)],
    ) -> Box<dyn OperationSpan + '_> {
        let attributes = attributes
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect();
        self.0
            .lock()
            .expect("the spans")
            .push((name.to_owned(), attributes));
        Box::new(Open)
    }
}
