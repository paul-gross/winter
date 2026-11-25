---
id: repo/list
title: List the repositories of the workspace
---

`winter-workspace repo list` prints one row for each repository: its URL or `-`, its type, its location from the
workspace root, and `[pinned]` on a pinned project. The implicit `product` and `harness` repositories come first when
present, then the standalones, then the projects. Nothing is changed.

When the declared projects and the directories under `projects/` disagree, a notice goes to stderr.

`winter-workspace repo list --json` prints one document with an object for each repository, its location absolute.
