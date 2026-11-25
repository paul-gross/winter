---
id: ws/update
title: Re-pin or freeze standalone repositories
---

`winter-workspace ws update` takes each standalone repository that declares a `ref`, fetches it, resolves the `ref`
again, checks out the result, and records it in the lock. It changes checkouts. `winter-workspace ws update <repo>`
limits the run to the standalones named, each a bare name or a glob.

A checkout with uncommitted changes, untracked files included, is reported as an error and left alone. `winter-workspace
ws update --autostash` stashes the changes to tracked files and restores them after; untracked files are not stashed. Do
not use it on a checkout whose only changes are untracked files: nothing is stashed, yet the run still pops the newest
stash the checkout already holds. A standalone whose checkout is missing is skipped with a warning.

A pin error on one standalone does not stop the next, and the run then exits non-zero. A `HEAD` that cannot be read, a
lock that cannot be read or written, and a git that cannot be started end the whole run.

`winter-workspace ws update --freeze` fetches nothing and changes no checkout. It selects every standalone, writes the
current commit of each one that has no `ref` as its `ref` into the configuration file that declares it, and records it
in the lock. A standalone that already declares a `ref` is left as it is. A dirty checkout is refused unless
`winter-workspace ws update --freeze --force` is given; `--force` is valid only with `--freeze`, and `--autostash` only
without it. A refused standalone does not stop the next, and the run then exits non-zero; a lock or configuration file
that cannot be written, and a git that cannot be started, end the whole run.

`winter-workspace ws update --json` prints each event as one line of JSON.
