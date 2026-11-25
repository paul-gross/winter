---
id: ws/fingerprint
title: Print the content digest of the workspace definition
---

`winter-workspace ws fingerprint` prints one digest of the tracked content of the workspace repository and of each
standalone repository, with the standalones' names. Uncommitted changes to tracked files count; untracked files,
history, and where a repository was cloned from do not. Two checkouts of one definition have one digest.

A standalone that is not checked out ends the command with an error. No checkout or index is changed.

`winter-workspace ws fingerprint --json` prints one document with the `digest` and, for the workspace and for each
standalone, its `name`, `commit`, `tree`, and `dirty`.
