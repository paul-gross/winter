---
id: ws/update
title: Re-pin or freeze standalone repositories
---

`winter-workspace ws update` takes each standalone repository that declares a `ref`, fetches it, resolves the `ref`
again, checks out the result, and records it in the lock. It changes checkouts. `winter-workspace ws update <repo>`
limits the run to the standalones named, each a bare name or a glob.

A checkout with uncommitted changes, untracked files included, is reported as an error and left alone. `winter-workspace
ws update --autostash` stashes the changes first and restores them after. A standalone whose checkout is missing is
skipped with a warning. An error on one standalone does not stop the next, and the run then exits non-zero.

`winter-workspace ws update --freeze` fetches nothing and changes no checkout. It selects every standalone, writes the
current commit of each one that has no `ref` as its `ref` into the configuration file that declares it, and records it
in the lock. A standalone that already declares a `ref` is left as it is. A dirty checkout is refused unless
`winter-workspace ws update --freeze --force` is given; `--force` is valid only with `--freeze`, and `--autostash` only
without it.

`winter-workspace ws update --json` prints each event as one line of JSON.
