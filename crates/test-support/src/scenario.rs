//! Recorded scenarios: what a scenario directory holds, read as typed values, the fixture one describes,
//! built in an isolated tree, the state of such a tree, taken as a snapshot and normalized for storing,
//! and the comparison of what was recorded with what was observed.

pub mod compare;
pub mod fixture;
mod model;
pub mod normalize;
pub mod snapshot;

pub use model::{
    Checkout, Commit, Compare, Deviation, Discovered, Facility, FanOut, FileMode, FileRule,
    Fixture, GitCall, InvalidKey, Malformed, Meta, Reference, Remote, Scenario, State, StdoutMode,
    Step, Stub, Worktree, discover,
};
