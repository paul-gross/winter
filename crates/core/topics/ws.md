---
id: ws
title: Feature environment and workspace commands
---

The `ws` commands act on feature environments and on the workspace that holds them.

- Read `winter-workspace context ws/init` to reconcile source checkouts, standalones, and managed workspace content.
- Read `winter-workspace context ws/index` to look up the port-offset index of a feature environment name.
- Read `winter-workspace context ws/worktrees` to list the checkouts that exist and their git counts.
- Read `winter-workspace context ws/update` to re-pin standalone repositories, or to freeze them where they are.
- Read `winter-workspace context ws/fingerprint` to print the content digest of the workspace definition.
