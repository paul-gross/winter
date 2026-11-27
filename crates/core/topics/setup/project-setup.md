---
id: setup/project-setup
title: Write the workspace's project setup guide
---

`context/project/project-setup.md` is the recipe for the setup that brings a fresh checkout to a ready state and that
the configuration does not already do. It covers setup only, not how to run the project. If it exists, show it and ask
what to change.

Offer to research the repositories (package manifests, environment templates, setup sections of README files) and
confirm the findings with the user, or ask the questions below one at a time.

## Where each need goes

1. **Dependencies:** how are they installed in each repository? A single command that is safe to repeat goes in that
   repository's `cmd` list in `.winter/config.toml`. `winter-workspace ws init` runs it every time. A step that branches
   or depends on the machine goes in the document. Do not invent a script to force a step into `cmd`; say what the user
   must do by hand.
2. **Environment files:** which files and variables does the project need, and which values are secrets or differ
   between checkouts? The steps that generate them go in the document. Name each generated file in `git_excludes`, so
   that it is never committed.
3. **Verification:** which command shows that setup worked? It is the last step of the document.

The configuration keys look like this. A top-level `git_excludes`, above every table, applies to all repositories; one
in a repository's block applies to that repository. Add the keys to the block that is already there:

```toml
[[project_repository]]
name = "app"
url = "https://example.com/app.git"
cmd = ["npm ci"]
git_excludes = [".env.local"]
```

## Write the document

Number the steps and give exact commands. Do not repeat what `cmd` or `git_excludes` already does, and do not write
secrets into it. Write each sentence or paragraph on one line, so a later edit changes only that line.

Then apply configuration changes with `winter-workspace ws init`, and return to
`winter-workspace context setup/walkthrough` for any step left.
