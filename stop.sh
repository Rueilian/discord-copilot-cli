#!/bin/bash
# stop.sh — stop the bot and copilot tmux session
PID=$(pgrep -f "python.*bot.py" 2>/dev/null | head -1)
if [ -n "$PID" ]; then
  kill -9 "$PID"
  echo "✅ Bot stopped (PID $PID)"
else
  echo "Bot is not running"
fi

tmux kill-session -t copilot-discord 2>/dev/null && echo "✅ tmux session killed" || true
