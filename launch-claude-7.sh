#!/bin/bash
unset CLAUDE_CODE_SESSION CLAUDE_SESSION_ID CLAUDE_CODE CLAUDE_CODE_RUNNING CLAUDE_PARENT_SESSION ANTHROPIC_CLAUDE_CODE
for var in $(env | grep -i CLAUDE | cut -d= -f1); do unset "$var"; done

SESSION="claude-terminal-7"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach-session -t "$SESSION"
else
  exec tmux new-session -s "$SESSION" -c /home/ubuntu/pr \; \
    set mouse on \; \
    send-keys "/home/ubuntu/.local/bin/claude --dangerously-skip-permissions --effort max" Enter
fi
