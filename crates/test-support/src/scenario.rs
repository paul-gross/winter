//! Recorded scenarios: what a scenario directory holds, read as typed values, and the fixture one describes,
//! built in an isolated tree.

pub mod fixture;
mod model;

pub use model::{
    Checkout, Commit, Compare, Deviation, Discovered, Facility, FanOut, FileMode, FileRule,
    Fixture, GitCall, InvalidKey, Malformed, Meta, Reference, Remote, Scenario, State, StdoutMode,
    Step, Stub, Worktree, discover,
};
