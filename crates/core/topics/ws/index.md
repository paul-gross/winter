---
id: ws/index
title: Look up the index of a feature environment name
---

`winter-workspace ws index <name>` prints the port-offset index of a feature environment name. The name need not belong
to an existing environment, and nothing is recorded or written.

The answer is the index the workspace's registry records for the name; otherwise the position of the name among the
configured environment aliases; otherwise a slot hashed from the name. A hash slot is marked `(suggested)`, and the
lookup does not check whether another name already records it.

`winter-workspace ws index <name> --json` prints one document with `name`, `index`, and `source`, which is `registry` or
`suggested`; an alias position is also `suggested`.
