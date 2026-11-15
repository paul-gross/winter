//! Recorded scenarios: what a scenario directory holds, read as typed values, the fixture one describes,
//! built in an isolated tree, the state of such a tree, taken as a snapshot and normalized for storing,
//! the comparison of what was recorded with what was observed, the steps of a scenario run with a given
//! executable, and the record of one written when two runs agree.

pub mod compare;
pub mod fixture;
mod model;
pub mod normalize;
pub mod record;
pub mod runner;
pub mod snapshot;

pub use model::{
    Checkout, Commit, Compare, Deviation, Discovered, Facility, FanOut, FileMode, FileRule,
    Fixture, GitCall, InvalidKey, Malformed, Meta, Reference, Remote, Scenario, State, StdoutMode,
    Step, Stub, Worktree, discover,
};
