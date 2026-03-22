#!/bin/bash
# start.sh — start the bot in the background
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/discord_bot.log"

# Kill any existing instance
EXISTING=$(pgrep -f "python.*bot.py" 2>/dev/null | head -1)
if [ -n "$EXISTING" ]; then
  echo "Stopping existing bot (PID $EXISTING)..."
  kill -9 "$EXISTING" 2>/dev/null
  sleep 1
fi

# Start bot
nohup python3 -u "$SCRIPT_DIR/bot.py" > "$LOG" 2>&1 &
BOT_PID=$!
disown $BOT_PID

echo "✅ Bot started (PID $BOT_PID)"
echo "📄 Log: $LOG"
echo "   tail -f $LOG"
