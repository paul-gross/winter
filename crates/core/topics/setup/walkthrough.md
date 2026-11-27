---
id: setup/walkthrough
title: Walk through workspace setup
---

An interactive procedure, safe to run again at any time. Take the steps in order. Before each one, check what the
workspace already has; when the step is done, tell the user what you found and skip it. Ask one question at a time, and
say what you are about to change before you change it.

## 1. Create the workspace

Look for `.winter/config.toml` in the current directory and in each directory above it. The nearest directory that holds
one is the root of the workspace you are already in: say so, and skip this step. Only when none holds one, run
`winter-workspace init` in the current directory, as `winter-workspace context init` describes. Run the later steps from
the workspace root, since their paths start there.

## 2. Declare the project repositories

`winter-workspace repo list` shows what is declared. Declare each missing project repository, one at a time, in the order
the listings should show them. Ask for its clone URL, and find its default branch:

```sh
git ls-remote --symref <url> HEAD
```

When the command fails or prints no `ref:` line, tell the user and ask for the default branch.

Then declare it with `winter-workspace repo add https://example.com/repo.git --main-branch main`, using that URL and
branch. `winter-workspace context repo/add` gives the other options.

## 3. Set the git identity

Ask whether commits in this workspace use a name and email of their own. If so, write both to
`.winter/config.local.toml`, a machine-local file that should stay out of version control:

```toml
[git.user]
name = "Ada Lovelace"
email = "ada@example.com"
```

If not, add nothing: git uses its usual configuration. Do this before the next step, which writes the identity into
each checkout. Removing it later does not remove it from checkouts that already have it.

## 4. Reconcile

Run `winter-workspace ws init` to clone what is missing and apply the identity, excludes, and commands.
`winter-workspace context ws/init` states what it does and what a repeat runs again.

## 5. Write the project documents (optional)

Offer each of these when its file is missing:

- `context/project/project-setup.md`: read `winter-workspace context setup/project-setup`. It may add commands or
  excludes to the configuration; run `winter-workspace ws init` again to apply them.
- `context/project/contributing.md`: read `winter-workspace context setup/contributing`.

End by saying what was done, what was already in place, and what is left to do by hand.
