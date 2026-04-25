---
description: Hand off this Claude Code session to claude-bridge (tmux)
---

Run the handoff script and relay its output to the user:

```
bash __HANDOFF_SCRIPT_ABS__
```

The script:
1. Detects the current session ID from the parent claude `--resume` arg
2. Sends `spawn_request` to the dispatcher via Unix socket (does NOT touch any front-end channel directly)
3. Dispatcher spawns `claude --resume <sessionId>` in a tmux pane
4. Dispatcher's channel adapter surfaces "handoff: spawning ..." to the user

After the script confirms success, remind the user to `/exit` this local session to avoid concurrent writes to the same session JSONL.
