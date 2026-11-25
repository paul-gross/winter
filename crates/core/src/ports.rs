//! The ports a domain flow is handed, as one value.

use crate::files::{ReadFiles, WriteFiles};
use crate::programs::RunPrograms;
use crate::trace::CommandTracer;

/// The file, writer, program, and tracer ports, as one value.
pub struct Ports<'p> {
    pub files: &'p dyn ReadFiles,
    pub writer: &'p dyn WriteFiles,
    pub programs: &'p dyn RunPrograms,
    pub tracer: &'p dyn CommandTracer,
}
