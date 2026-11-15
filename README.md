# winter-workspace

This repository is the Rust source of the `winter-workspace` binary, the command-line tool that manages a Winter
workspace: its repositories, its feature environments, and the worktrees they are made of.

## Building

The repository is a Cargo workspace. With a Rust toolchain installed through `rustup`, build the binary from the
repository root:

```bash
cargo build
```

The binary is written to `target/debug/winter-workspace`.

## Contributing

[AGENTS.md](./AGENTS.md) is the entry point for contributors and their agents. [PRINCIPLES.md](./PRINCIPLES.md) states
the tenets behind Winter and why it exists.

## License

Source-available under the Functional Source License 1.1 (FSL-1.1-ALv2). Each release converts to Apache License 2.0 two
years after it ships. See [LICENSE](./LICENSE).
