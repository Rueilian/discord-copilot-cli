#!/bin/bash
# setup.sh — one-time setup for discord-copilot-cli
set -e

echo "=== discord-copilot-cli setup ==="

# 1. Check dependencies
command -v python3 >/dev/null || { echo "❌ python3 not found"; exit 1; }
command -v tmux    >/dev/null || { echo "❌ tmux not found. Install: sudo apt install tmux"; exit 1; }
command -v copilot >/dev/null || { echo "❌ copilot CLI not found. Install via: npm install -g @githubnext/copilot-cli"; exit 1; }

# 2. Install Python deps
pip install discord.py --quiet
echo "✅ discord.py installed"

# 3. Create credentials file if not exists
CREDS="$HOME/.config/copilot-credentials.sh"
if [ ! -f "$CREDS" ]; then
  mkdir -p "$(dirname "$CREDS")"
  cat > "$CREDS" << 'EOF'
export DISCORD_TOKEN="your-bot-token-here"
export DISCORD_CHANNEL_ID="your-channel-id-here"
EOF
  chmod 600 "$CREDS"
  echo "✅ Created $CREDS — fill in your token and channel ID"
else
  echo "✅ $CREDS already exists"
fi

echo ""
echo "Next steps:"
echo "  1. Edit $CREDS and fill in DISCORD_TOKEN and DISCORD_CHANNEL_ID"
echo "  2. Run: bash start.sh"
