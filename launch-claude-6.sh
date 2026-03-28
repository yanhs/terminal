#!/bin/bash
# Clear Claude session detection env vars
unset CLAUDE_CODE_SESSION
unset CLAUDE_SESSION_ID
unset CLAUDE_CODE
unset CLAUDE_CODE_RUNNING
unset CLAUDE_PARENT_SESSION
unset ANTHROPIC_CLAUDE_CODE

# Clear any env var containing CLAUDE
for var in $(env | grep -i CLAUDE | cut -d= -f1); do
  unset "$var"
done

SESSION="claude-terminal-6"

# If tmux session exists, attach to it; otherwise create it
if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach-session -t "$SESSION"
else
  exec tmux new-session -s "$SESSION" -c /home/ubuntu/pr \
    /home/ubuntu/.local/bin/claude --dangerously-skip-permissions --effort max
fi
