---
id: setup/contributing
title: Write the workspace's contributing guide
---

`context/project/contributing.md` states how finished work is committed and delivered. Make it specific enough that an
agent can carry out the whole delivery without asking. If it exists, show it and ask what to change.

Offer to research the repositories (recent history, `CONTRIBUTING.md` files, formatter, linter, hook, and pipeline
configuration, pull request templates) and confirm the findings with the user, or ask the questions below one at a time.

1. **Commit conventions:** what message format? Cover prefixes, scopes, ticket numbers, when a body is expected, and
   co-author lines.
2. **Pre-commit checks:** what must run before a commit? Get the exact commands, whether they run unasked, and which
   hooks already run them.
3. **Delivery method:** how does work reach the main branch? Cover pull or merge request or direct merge, the tool, the
   target branch, the merge strategy, and required reviews or checks.
4. **Post-delivery:** what happens after the merge? For example a deployment, a release tag, a changelog, a notification.
5. **Multi-repository considerations:** with more than one repository, do they all deliver the same way? Record each
   difference.

Write the answers to the file under these headings: commit message format, with examples; pre-commit checks, as exact
commands; delivery steps, as exact commands; post-delivery steps, if any.

When done, return to `winter-workspace context setup/walkthrough` for any step left.
