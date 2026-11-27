---
id: init
title: Create a workspace here
---

Run `winter-workspace init` in the directory to make a workspace there. It writes `.winter/config.toml` with this
binary's workspace version, places the safety rules in `AGENTS.md`, and creates a `CLAUDE.md` import when absent.
Existing user content is preserved; conflicting agent files are reported as warnings. An existing configuration is
refused without changes. The command clones nothing and does not run git.

Declare repositories with `winter-workspace repo add https://example.com/repo.git`, then reconcile them with
`winter-workspace ws init`.
