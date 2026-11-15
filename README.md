# winter-workspace

This repository is the Rust source of the `winter-workspace` binary, a work in progress. The commands that exist today
are `ws index` and `repo remove`; `winter-workspace --help` is the reference for them. The binary is not the installed
`winter` and does not replace it.

## Building

The repository is a Cargo workspace. With a Rust toolchain installed through `rustup`, build the binary from the
repository root:

```bash
cargo build
```

The binary is written to `target/debug/winter-workspace`.

`repo remove` edits the workspace configuration it finds at or above the working directory. [AGENTS.md](./AGENTS.md)
states where the binary may run. It is on no `PATH`: from a directory that rule permits, it is started by its path, as
in `<checkout>/target/debug/winter-workspace --help`, where `<checkout>` is the path of the repository root.

## Contributing

[AGENTS.md](./AGENTS.md) is the entry point for contributors and their agents. [PRINCIPLES.md](./PRINCIPLES.md) states
the tenets behind Winter and why it exists.

## License

Source-available under the Functional Source License 1.1 (FSL-1.1-ALv2). Each release converts to Apache License 2.0 two
years after it ships. See [LICENSE](./LICENSE).
