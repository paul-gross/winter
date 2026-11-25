---
id: context
title: The context command
---

`winter-workspace context` prints the root of this version's guidance, and `winter-workspace context <TOPIC>` prints one
topic; `winter-workspace context topics` lists every topic with its title. A TOPIC is an id matched exactly, as in
`ws/index`; an id that no topic holds ends the command with an error and prints nothing on stdout. The command reads no
workspace and no file, so it runs the same in any directory.

With `--json` the topic is one document: its id, title, text, and the ids it references in order.

Other documents cite a topic as `winter:/workspace/<id>`; the `workspace` segment names this product and is never part
of the id, so read it with `winter-workspace context <id>`.
