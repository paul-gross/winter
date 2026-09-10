# Command-entry cookbook

One-line `command` values for common secrets tools, for use in a [command-valued env entry](./command-env-entries.md) in
`.winter/config.toml` — the inline-table band entry that sources its value by running a command instead of a `${...}`
template. Winter runs any of the tools below exactly like it would run `git` or `psql` — it depends on, bundles, and
knows the name of none of them:

| Tool      | `command`                                             | `format` |
| --------- | ----------------------------------------------------- | -------- |
| `vals`    | `vals get ref+vault://secret/data/myapp/db#/password` | `raw`    |
| `chamber` | `chamber export myapp --format json`                  | `json`   |
| `summon`  | `summon -f secrets.yml env`                           | `dotenv` |
| `teller`  | `teller run -- env`                                   | `dotenv` |
| `op`      | `op read op://vault/item/password`                    | `raw`    |
| `sops`    | `sops -d --output-type dotenv secrets.enc.yaml`       | `dotenv` |

The `summon` and `teller` recipes run the wrapped tool's whole environment through `env`, so their output carries every
inherited var alongside the secret — and every line of it must parse as `KEY=VALUE`, so a single multi-line value, or an
exported shell function, anywhere in that environment fails the entry outright, before `exports` narrows anything.
Prefer a recipe that emits the secrets alone where the tool offers one, and use `exports` to keep the merge itself
tight.

Against a locked vault, `op read` may prompt interactively, and which mode it picks — reading stdin, or opening
`/dev/tty` — is not something the operator controls. Neither reliably fails: either can leave the entry resolved to an
empty value, and one of them can tie it up for the entry's whole timeout. See
[Stdin and `/dev/tty` prompts](./command-env-entries.md#stdin-and-devtty-prompts) for what each does. Keep the vault
already unlocked, or supply a service-account token, in the environment winter runs from rather than relying on which
mode `op` picks.
