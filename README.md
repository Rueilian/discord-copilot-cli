# discord-copilot-cli

Bridge **GitHub Copilot CLI** (`copilot --yolo`) to Discord — chat with Copilot from your phone.

## How it works

```
Discord message → bot.py → tmux session running copilot --yolo → response → Discord
```

The bot uses `tmux` to run a persistent Copilot CLI session. Any message you send in the Discord channel (not starting with `!`) is forwarded to Copilot and the response is sent back.

## Requirements

- Linux / WSL2
- `python3`
- `tmux` (`sudo apt install tmux`)
- `copilot` CLI (`npm install -g @githubnext/copilot-cli` — requires GitHub Copilot subscription)
- Discord bot token + channel ID

## Setup

### 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. **New Application** → give it a name
3. Go to **Bot** tab → **Add Bot**
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. Copy the **Bot Token**
6. Go to **OAuth2 → URL Generator** → scopes: `bot` → permissions: `Send Messages`, `Read Message History`
7. Open the generated URL to invite the bot to your server
8. Get your **Channel ID**: enable Developer Mode (Settings → Advanced), right-click your channel → Copy ID

### 2. Install and configure

```bash
git clone https://github.com/Rueilian/discord-copilot-cli
cd discord-copilot-cli
bash setup.sh
```

Then edit `~/.config/copilot-credentials.sh`:

```bash
export DISCORD_TOKEN="your-bot-token-here"
export DISCORD_CHANNEL_ID="your-channel-id-here"
```

### 3. Start the bot

```bash
bash start.sh
```

The bot starts a background tmux session (`copilot-discord`) running `copilot --yolo`, then listens on Discord.

### 4. Stop the bot

```bash
bash stop.sh
```

## Usage

In your Discord channel:

| Input | Action |
|-------|--------|
| Any text | Chat with Copilot CLI |
| `!restart` | Restart the Copilot session (clears memory) |
| `!run <cmd>` | Run a shell command on the host machine |
| `!status` | Show hostname, user, date |
| `!ls [path]` | List a directory |
| `!help` | Show command list |

## Security

- Only messages from the configured `DISCORD_CHANNEL_ID` are processed
- Credentials are stored in `~/.config/copilot-credentials.sh` (chmod 600), never committed
- Consider making your Discord channel private (only you can access it)

## Troubleshooting

**Bot starts typing but never replies:**
```bash
tail -f ~/discord-copilot-cli.log   # check for errors
bash stop.sh && bash start.sh       # restart everything
```

**Copilot session dies:**
Send `!restart` in Discord to respawn the tmux session.

**Check tmux session manually:**
```bash
tmux attach -t copilot-discord
# Ctrl+B, D to detach
```
