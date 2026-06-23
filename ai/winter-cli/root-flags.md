# Winter CLI — root flags

`winter --version` prints the installed CLI version (sourced from package metadata, so it tracks the running source) and exits 0. `winter --help` lists every command and root flag.

`winter --verbose` / `winter -v` attaches a stderr `StreamHandler` at DEBUG level so every `logger.debug/info/warning` call inside `winter_cli` becomes visible. Equivalent to `WINTER_LOG_LEVEL=DEBUG`. Diagnostics always go to **stderr**; `--json` stdout stays pure JSON. `WINTER_LOG_LEVEL=<LEVEL>` (e.g. `INFO`, `WARNING`) selects a coarser level without the flag.

`winter --service-orchestrator=<path-or-name> service …` overrides the service orchestrator for a single `winter service` invocation — points dispatch at a local extension directory or a registered name instead of the registry-resolved (bound or sole-provider) extension. See [usage/service.md#local-path-override](./usage/service.md#local-path-override) for the full precedence rule and path-vs-name semantics.
