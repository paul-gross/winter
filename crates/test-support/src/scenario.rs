//! Recorded scenarios: what a scenario directory holds, read as typed values, the fixture one describes,
//! built in an isolated tree, and the state of such a tree, taken as a snapshot and normalized for storing.

pub mod fixture;
mod model;
pub mod normalize;
pub mod snapshot;

pub use model::{
    Checkout, Commit, Compare, Deviation, Discovered, Facility, FanOut, FileMode, FileRule,
    Fixture, GitCall, InvalidKey, Malformed, Meta, Reference, Remote, Scenario, State, StdoutMode,
    Step, Stub, Worktree, discover,
};
