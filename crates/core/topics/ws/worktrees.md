---
id: ws/worktrees
title: List the checkouts that exist
---

`winter-workspace ws worktrees` lists the checkout directories that exist: the project worktrees of each environment
present, then the standalone repositories, then the workspace itself, which is always listed. Each row has a label, a
kind, and an absolute path.

`winter-workspace ws worktrees --status` adds the commits ahead and behind and the number of dirty paths. A worktree is
counted against `origin/<main-branch>` of its project, and the workspace against the upstream of its branch; a
standalone is not counted. A git call that fails on a worktree ends the command with an error.

`winter-workspace ws worktrees --json` prints one document with an object for each location. With `--status` every
location also has `ahead`, `behind`, and `dirty`, each a number, or null where it was not counted.
