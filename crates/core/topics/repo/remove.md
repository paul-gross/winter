---
id: repo/remove
title: Remove a repository from the configuration
---

`winter-workspace repo remove <target>` removes one repository block from the shared configuration file. The target is
`project/<name>` or `standalone/<name>`. It edits that file only: no checkout or worktree is deleted.

Only the one file is searched, so use `winter-workspace repo remove <target> --local` for a repository declared in the
machine-local file. A target the file does not declare is an error.

`winter-workspace repo remove <target> --json` prints the result as one document.
