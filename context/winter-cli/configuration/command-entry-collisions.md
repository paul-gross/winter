# Command-entry output collisions

A [command-valued env entry](./command-env-entries.md) whose output is parsed under `format = "dotenv"`/`"json"` can
emit a key that some other entry in the same scope also declares directly. This page covers how that collision resolves,
and a mid-resolution visibility gap the resolution order leaves open. It assumes the field reference, `exports`, and the
gate documented on [Command-valued env entries](./command-env-entries.md).

For example:

```toml
[env.feature.vars]
_aws        = { command = "chamber export myapp", format = "json" }  # imports DB_PASSWORD, among other keys
DB_PASSWORD = "literal"
```

**Collisions with a command's output key.** A `dotenv`/`json` entry's output keys aren't known until the command has
run, so a key some entry declares directly and a key a *different* entry's output also emits resolve by provenance, not
by fixpoint round: band precedence wins first (workspace < feature < named), then, within one band, whichever entry is
declared further down the TOML table. In the example above, a literal declared after `_aws` overrides one of the keys
`_aws` imports; one declared before it loses to the import.

That rule settles a key's *final* value, but a reader that resolves *before* the winning write lands can still observe
the value about to be replaced. A literal outranking a colliding command is fully pre-empted — known up front, so the
command's losing write is dropped before any third entry sees it. A command's output outranking an *earlier-resolving*
literal has no such pre-emption: the literal is already visible by the time the command runs, so a third entry that
reads it in that window renders against the value about to be replaced. The final value in the injected scope is
unaffected — only a mid-resolution read is. This is a known, accepted limitation, not a bug: closing it would mean
withholding every entry until every higher-provenance candidate for every key it might read has also resolved,
restructuring the fixpoint rather than fixing this one interaction. Declaring the literal *after* the colliding command
slot avoids it.
