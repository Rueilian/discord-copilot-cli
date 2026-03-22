"""
Discord bot bridging GitHub Copilot CLI (copilot --yolo) + shell commands.

  - Any message NOT starting with ! → sent to Copilot session
  - !run <cmd>               → run shell command
  - !sim p0 [p1 ...]         → run ADFP VCS simulation
  - !log p0                  → tail simulation log
  - !ls [path]               → list directory
  - !status                  → host info
  - !restart                 → restart Copilot session
  - !help                    → show help
"""

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import discord
import pexpect
from discord.ext import commands

# ── Credentials ───────────────────────────────────────────────────────────────
def load_credentials():
    creds = Path.home() / ".config" / "copilot-credentials.sh"
    if creds.exists():
        result = subprocess.run(
            f"source {creds} && env",
            shell=True, executable="/bin/bash", capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k, v)

load_credentials()

TOKEN      = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
COPILOT_CMD = "copilot --yolo"

# ── ANSI / output cleanup ─────────────────────────────────────────────────────
_ANSI = re.compile(r'(\x9B|\x1B\[)[0-9:;<=>?]*[ -/]*[@-~]|\x1B[@-_]|\x1b\].*?\x07|\r')
_STATUS_LINE = re.compile(
    r'(Thinking|Loading environment|Environment loaded|shift\+tab|Remaining reqs\.|ctrl\+[sq]|enqueue|switch mode|Type @|claude-sonnet|claude-opus|gpt-|medium|MCP server|Esc to cancel|to cancel)'
)
_DIVIDER = re.compile(r'^[─╭╰│╮╯\s]+$')

def strip_ansi(text: str) -> str:
    return _ANSI.sub('', text)

def extract_response(raw: str) -> str:
    """Extract the actual AI response text from noisy pty output."""
    cleaned = strip_ansi(raw)
    lines = cleaned.split('\n')
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _STATUS_LINE.search(line):
            continue
        if _DIVIDER.match(line):
            continue
        # Remove spinner prefixes (●◉◎○ and similar)
        line = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·\s]+', '', line).strip()
        if not line:
            continue
        result.append(line)
    return '\n'.join(result).strip()

# ── Copilot session via tmux ──────────────────────────────────────────────────
TMUX_SESSION = 'copilot-discord'

class CopilotSession:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ready = False

    def _tmux(self, *args) -> str:
        result = subprocess.run(['tmux'] + list(args), capture_output=True, text=True)
        return result.stdout

    def _capture(self) -> str:
        """Capture current visible tmux pane (strips ANSI)."""
        raw = self._tmux('capture-pane', '-t', TMUX_SESSION, '-p', '-e')
        return strip_ansi(raw)

    def _session_exists(self) -> bool:
        r = subprocess.run(['tmux', 'has-session', '-t', TMUX_SESSION],
                           capture_output=True)
        return r.returncode == 0

    def start(self, resume_id: str = ''):
        if self._session_exists():
            self._tmux('kill-session', '-t', TMUX_SESSION)
            time.sleep(1)
        # Build command — optionally resume a specific session
        cmd = COPILOT_CMD
        if resume_id:
            cmd = f'{COPILOT_CMD} --resume={resume_id}'
        # Create detached tmux session starting from HOME
        self._tmux('new-session', '-d', '-s', TMUX_SESSION, '-x', '220', '-y', '500', '-c', os.path.expanduser('~'))
        self._tmux('send-keys', '-t', TMUX_SESSION, cmd, 'Enter')
        print(f"[COPILOT] tmux session started: {cmd}", flush=True)

        # Wait for environment to load (up to 40s)
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(2)
            out = self._capture()
            print(f"[COPILOT] startup: {out[-200:]!r}", flush=True)
            # Handle multi-line input dialog
            if 'navigate' in out or 'Would you like' in out:
                self._tmux('send-keys', '-t', TMUX_SESSION, '2', 'Enter')
                time.sleep(1)
            if 'Environment loaded' in out or 'Type @' in out:
                self.ready = True
                print("[COPILOT] ready!", flush=True)
                break
        else:
            self.ready = True  # proceed anyway

    def restart(self, resume_id: str = ''):
        self.ready = False
        self.start(resume_id=resume_id)

    async def ask(self, question: str, timeout: int = 60) -> str:
        async with self.lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._ask_sync, question, timeout)

    def _is_idle(self, bottom: str) -> bool:
        """Return True if Copilot is idle (not actively generating a response)."""
        return 'enqueue' not in bottom

    def _wait_for_input_ready(self, timeout: int = 30):
        """Wait until Copilot is at the input prompt (not actively thinking)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])
            if self._is_idle(bottom):
                return True
            time.sleep(0.5)
        return False

    def _ask_sync(self, question: str, timeout: int) -> str:
        t0 = time.time()
        if not self._session_exists():
            self.start()

        # Wait until Copilot is actually at the input prompt
        self._wait_for_input_ready(timeout=20)
        print(f"[ASK] ready_wait={time.time()-t0:.1f}s sending: {question!r}", flush=True)

        # Start capturing ALL new output to pipe file (avoids history contamination)
        self._pipe_start()

        # Send text then Enter as separate commands with small delay
        self._tmux('send-keys', '-t', TMUX_SESSION, question)
        time.sleep(0.3)
        self._tmux('send-keys', '-t', TMUX_SESSION, '', 'Enter')

        # Wait until Copilot starts thinking (enqueue appears) — confirms submission
        think_deadline = time.time() + 15
        got_thinking = False
        while time.time() < think_deadline:
            time.sleep(0.5)
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])
            if 'enqueue' in bottom or 'Thinking' in bottom:
                print(f"[ASK] thinking at {time.time()-t0:.1f}s", flush=True)
                got_thinking = True
                break
            # If message is still in input box (not submitted), send Enter again
            if question[:10] in out and 'ctrl+s' in out and not got_thinking:
                print("[ASK] message still in input, retrying Enter...", flush=True)
                self._tmux('send-keys', '-t', TMUX_SESSION, '', 'Enter')

        if not got_thinking:
            print(f"[ASK] WARNING: never saw thinking at {time.time()-t0:.1f}s", flush=True)

        # Wait until enqueue disappears (= response complete)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])
            if self._is_idle(bottom):
                time.sleep(0.3)  # brief pause to let final render complete
                break

        print(f"[ASK] done at {time.time()-t0:.1f}s", flush=True)

        # Stop pipe (was capturing raw ANSI — not useful) and use capture-pane instead
        self._pipe_stop()

        # Capture visible pane and extract only the latest ● response bubble
        pane = self._capture()
        response = self._extract_from_pane(pane)
        print(f"[ASK] extracted: {response[:80]!r}", flush=True)
        if len(response) > 1800:
            response = response[:1800] + '\n...(truncated)'
        return response or '(no response captured)'

    def _clean_lines(self, lines: list) -> list:
        """Strip status lines, box chars, and spinner prefixes from a list of lines."""
        result = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                result.append('')
                continue
            if _STATUS_LINE.search(stripped):
                continue
            if _DIVIDER.match(stripped):
                break
            stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s│╭╰╮╯─]+', '', stripped).strip()
            if stripped and not re.match(r'^[╭╮╰╯│─\s]+$', stripped):
                result.append(stripped)
        return result

    def _extract_from_pane(self, pane: str) -> str:
        """Extract the LAST AI response bubble from the visible pane."""
        lines = pane.split('\n')

        # Find the first divider from bottom (top edge of input box)
        divider_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if _DIVIDER.match(lines[i].strip()):
                divider_idx = i
                break

        content_lines = lines[:divider_idx] if divider_idx is not None else lines

        # Find the LAST ● bubble in content
        bubble_start = None
        for i, l in enumerate(content_lines):
            if re.match(r'^[●◉◎○◐◑◒◓]', l.strip()):
                bubble_start = i

        # Fallback: no ● found — take last 20 lines
        src = content_lines[bubble_start:] if bubble_start is not None else content_lines[-20:]
        return '\n'.join(self._clean_lines(src)).strip()


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
session = CopilotSession()

# ── Shell helper ──────────────────────────────────────────────────────────────
async def run_shell(cmd: str, timeout: int = 120) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            f"source ~/.config/copilot-credentials.sh 2>/dev/null; {cmd}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable='/bin/bash'
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors='replace').strip()
        if len(output) > 1800:
            output = '...(truncated)\n' + output[-1800:]
        return output or '(no output)'
    except asyncio.TimeoutError:
        return f'❌ Timed out after {timeout}s'
    except Exception as e:
        return f'❌ Error: {e}'

def code_block(text: str) -> str:
    return f'```\n{text}\n```'

HANDOFF_FILE = Path.home() / '.copilot' / 'handoff-request'

async def watch_handoff_file():
    """Watch for handoff requests written by handoff.py in the terminal."""
    while True:
        await asyncio.sleep(2)
        if HANDOFF_FILE.exists():
            try:
                session_id = HANDOFF_FILE.read_text().strip()
                HANDOFF_FILE.unlink()
            except Exception:
                continue
            if not session_id:
                continue
            ch = bot.get_channel(CHANNEL_ID)
            if ch:
                msg = await ch.send(f'📲 Terminal handoff — resuming session `{session_id[:8]}...`')
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, session.restart, session_id)
                await msg.edit(content=f'✅ Resumed session `{session_id[:8]}...` from terminal handoff!')

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    ch = bot.get_channel(CHANNEL_ID)
    print(f'✅ Bot ready as {bot.user}')

    # Kill any leftover tmux session so direct typing starts fresh
    if session._session_exists():
        session._tmux('kill-session', '-t', TMUX_SESSION)
        session.ready = False
        print('[COPILOT] killed leftover tmux session on startup', flush=True)

    # Start watching for terminal handoff requests
    asyncio.ensure_future(watch_handoff_file())

    if ch:
        await ch.send(
            f'🤖 Copilot bot online on `{os.uname().nodename}`\n'
            f'Directly type to start a fresh session, or `!handoff` to resume the most recent one.\n'
            f'Type `!help` for all commands.'
        )

@bot.event
async def on_message(message: discord.Message):
    # Ignore self and other channels
    if message.author == bot.user:
        return
    if message.channel.id != CHANNEL_ID:
        return

    # Let commands through
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return

    # Everything else → Copilot
    question = message.content.strip()
    if not question:
        return

    async with message.channel.typing():
        response = await session.ask(question, timeout=900)  # 15 minutes

    await message.reply(response)

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name='help')
async def help_cmd(ctx):
    await ctx.send(
        '**Copilot Bot**\n'
        'Just type normally to chat with Copilot CLI.\n\n'
        '**Session commands:**\n'
        '`!exit` — close the current Copilot session\n'
        '`!restart` — start a fresh Copilot session (clears history)\n'
        '`!handoff` — resume the most recent session\n\n'
    )

@bot.command(name='restart')
async def restart_cmd(ctx):
    msg = await ctx.send('⏳ Restarting Copilot session...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart)
    await msg.edit(content='✅ Copilot session restarted!')


@bot.command(name='exit')
async def exit_cmd(ctx):
    """Close the current Copilot session (tmux session killed)."""
    if session._session_exists():
        session._tmux('kill-session', '-t', TMUX_SESSION)
        session.ready = False
        await ctx.send('✅ Copilot session closed.')
    else:
        await ctx.send('ℹ️ No active session to close.')

@bot.command(name='handoff')
async def handoff_cmd(ctx, session_id: str = ''):
    """Resume the most recent Copilot session (or a specific one by ID).
    Usage: !handoff              — auto-pick latest session
           !handoff <session-id> — specify session ID explicitly
    """
    if not session_id:
        # Find the most recently modified session directory
        result = await run_shell(
            'ls -t ~/.copilot/session-state/ 2>/dev/null | head -1', timeout=5
        )
        session_id = result.strip()
    if not session_id:
        await ctx.send('❌ No sessions found in `~/.copilot/session-state/`')
        return
    msg = await ctx.send(f'⏳ Resuming session `{session_id[:8]}...` — please wait...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart, session_id)
    await msg.edit(content=f'✅ Resumed session `{session_id[:8]}...` — conversation history carried over!')

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    bot.run(TOKEN)
