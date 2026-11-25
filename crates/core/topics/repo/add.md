---
id: repo/add
title: Declare a repository in the configuration
---

`winter-workspace repo add <url>` appends a project repository to the shared configuration file. It edits that file
only: nothing is cloned. The URL is an `http`, `https`, `ssh`, or `git` URL, or the `user@host:path` form. A repository
of the same type with the same URL or name is refused.

- `winter-workspace repo add <url> --name <name>` sets the name; otherwise it comes from the URL.
- `winter-workspace repo add <url> --main-branch <branch>` sets the main branch.
- `winter-workspace repo add <url> --pinned` pins the project; a standalone cannot take it.
- `winter-workspace repo add <url> --git-exclude <pattern> --cmd <command>` adds exclude patterns and setup commands;
  each option may be repeated.
- `winter-workspace repo add <url> --standalone --ref <ref> --path <path> --prefix <prefix>` declares a standalone;
  `--ref`, `--path`, and `--prefix` are for standalones only. The path is relative to the workspace root and may not
  leave it through `..`.
- `winter-workspace repo add <url> --local` edits the machine-local file, creating it when absent.
- `winter-workspace repo add <url> --json` prints the result as one document.
