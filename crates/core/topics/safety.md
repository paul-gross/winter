---
id: safety
title: Workspace safety rules
---

## Workspace safety

- Bring a feature environment up in order: `winter-workspace ws init <env>`, then `winter-workspace provision <env>`,
  then `winter-workspace service up <env>`. Never run or exercise an environment that has not been provisioned.
- Never run a destructive command against a live environment you do not intend to change:
  `winter-workspace ws checkout`, `winter-workspace ws reset --hard`, `winter-workspace ws clean`,
  `winter-workspace ws destroy`, `winter-workspace ws restack`, `winter-workspace clean`, and
  `winter-workspace provision --destroy`. Each reaches every worktree or environment its arguments match. Try one in a
  scratch environment first.
- `winter-workspace context` prints this Workspace version's guidance, `winter-workspace context topics` lists every
  topic, and `winter-workspace context <group>/<command>`, as in `ws/init`, gives a command's details. Other documents
  cite a topic as `winter:/workspace/<topic>`.
