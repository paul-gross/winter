---
id: repo
title: Repository commands
---

The `repo` commands read and edit the repositories a workspace declares: projects, which each environment has a worktree
of, and standalones, which the workspace checks out once. They clone and delete nothing.

- Read `winter-workspace context repo/list` to list the repositories of the workspace.
- Read `winter-workspace context repo/add` to declare a repository.
- Read `winter-workspace context repo/remove` to remove a declaration.
