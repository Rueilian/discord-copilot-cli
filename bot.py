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

    PIPE_FILE = Path('/tmp/copilot-discord-pipe.txt')

    def _pipe_start(self):
        """Start piping ALL new terminal output to PIPE_FILE."""
        self.PIPE_FILE.unlink(missing_ok=True)
        self._tmux('pipe-pane', '-t', TMUX_SESSION, '-o', f'cat >> {self.PIPE_FILE}')

    def _pipe_stop(self) -> str:
        """Stop piping and return accumulated output (stripped of ANSI)."""
        self._tmux('pipe-pane', '-t', TMUX_SESSION)  # stop pipe
        try:
            raw = self.PIPE_FILE.read_text(errors='replace')
        except FileNotFoundError:
            raw = ''
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
        # Create detached tmux session — tall pane (500 lines) so responses don't scroll off
        self._tmux('new-session', '-d', '-s', TMUX_SESSION, '-x', '220', '-y', '500')
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

    def _wait_for_input_ready(self, timeout: int = 30):
        """Wait until Copilot is at the input prompt (not actively thinking)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Only check the bottom portion of the pane (status area)
            # to avoid false positives from conversation history
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])  # last 10 lines = status bar area
            # Ready = input box visible AND no active Thinking spinner in status area
            has_prompt = 'Type @' in bottom or 'shift+tab' in bottom
            is_thinking = 'Thinking' in bottom
            if has_prompt and not is_thinking:
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

        # Wait until Copilot starts thinking (confirms submission) — check bottom only
        think_deadline = time.time() + 15
        got_thinking = False
        while time.time() < think_deadline:
            time.sleep(0.5)
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])
            if 'Thinking' in bottom:
                print(f"[ASK] thinking at {time.time()-t0:.1f}s", flush=True)
                got_thinking = True
                break
            # If message is still in input box (not submitted), send Enter again
            if question[:10] in out and 'ctrl+s' in out and not got_thinking:
                print("[ASK] message still in input, retrying Enter...", flush=True)
                self._tmux('send-keys', '-t', TMUX_SESSION, '', 'Enter')

        if not got_thinking:
            print(f"[ASK] WARNING: never saw Thinking at {time.time()-t0:.1f}s", flush=True)

        # Wait until Thinking disappears from the status area
        deadline = time.time() + timeout

        while time.time() < deadline:
            time.sleep(0.5)
            out = self._capture()
            lines = out.split('\n')
            bottom = '\n'.join(lines[-10:])
            is_thinking = 'Thinking' in bottom
            is_idle = ('Type @' in bottom or 'shift+tab' in bottom) and not is_thinking
            if is_idle:
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

    def _extract_response(self, after: str, question: str) -> str:
        """Extract AI response from visible pane content.
        Structure: [conversation] [divider] [input box] [divider] [status bar]
        Response is ABOVE the first divider from the bottom.
        """
        all_lines = after.split('\n')
        # Find the first divider from the bottom (= top of input box)
        first_divider_from_bottom = None
        for i in range(len(all_lines) - 1, -1, -1):
            if _DIVIDER.match(all_lines[i].strip()):
                first_divider_from_bottom = i
                break

        if first_divider_from_bottom is not None:
            content_lines = all_lines[:first_divider_from_bottom]
        else:
            content_lines = all_lines

        # Find where the user's question is — search BACKWARDS to get the LAST occurrence
        # (resume sessions show full history; we want the freshly-sent question near the bottom)
        q_short = question[:20]
        q_idx = None
        for i in range(len(content_lines) - 1, -1, -1):
            l = content_lines[i]
            if q_short in l and ('❯' in l or '>' in l):
                q_idx = i
                break
        if q_idx is not None:
            content_lines = content_lines[q_idx + 1:]
        else:
            # Fallback: take only the last 30 lines (fresh response area)
            content_lines = content_lines[-30:]

        # Find the LAST ● response bubble after the question
        # Copilot responses start with ● on their own line
        # Anything before the first ● is "thinking" text — skip it
        bubble_start = None
        for i, l in enumerate(content_lines):
            if re.match(r'^[●◉◎○◐◑◒◓]\s', l.strip()) or l.strip().startswith('●'):
                bubble_start = i
        if bubble_start is not None:
            content_lines = content_lines[bubble_start:]

        result = []
        for line in content_lines:
            stripped = line.strip()
            if not stripped:
                result.append('')  # preserve blank lines in response
                continue
            if _STATUS_LINE.search(stripped):
                continue
            if _DIVIDER.match(stripped):
                continue
            # Strip box-drawing and spinner prefix chars
            stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s│╭╰╮╯─]+', '', stripped).strip()
            if not stripped:
                continue
            # Skip lines that are just box borders
            if re.match(r'^[╭╮╰╯│─\s]+$', stripped):
                continue
            result.append(stripped)

        return '\n'.join(result).strip()

    def _extract_from_pipe(self, raw: str) -> str:
        """Extract AI response from pipe-pane output (new output only, no history).
        Looks for the last ● response bubble in the piped content.
        """
        lines = raw.split('\n')
        # Find the LAST ● bubble start
        bubble_start = None
        for i, l in enumerate(lines):
            s = l.strip()
            if re.match(r'^[●◉◎○◐◑◒◓]\s', s) or s.startswith('●'):
                bubble_start = i
        if bubble_start is not None:
            lines = lines[bubble_start:]

        result = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                result.append('')
                continue
            if _STATUS_LINE.search(stripped):
                continue
            if _DIVIDER.match(stripped):
                break  # stop at input box divider
            # Strip spinner/box prefix chars
            stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s│╭╰╮╯─]+', '', stripped).strip()
            if not stripped:
                continue
            if re.match(r'^[╭╮╰╯│─\s]+$', stripped):
                continue
            result.append(stripped)

        return '\n'.join(result).strip()

    def _extract_from_pane(self, pane: str) -> str:
        """Extract the LAST AI response bubble from the visible pane.
        Finds the last ● line above the input box divider.
        """
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
            s = l.strip()
            if re.match(r'^[●◉◎○◐◑◒◓]', s):
                bubble_start = i

        if bubble_start is None:
            return ''

        result = []
        for line in content_lines[bubble_start:]:
            stripped = line.strip()
            if not stripped:
                result.append('')
                continue
            if _STATUS_LINE.search(stripped):
                continue
            if _DIVIDER.match(stripped):
                break
            stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s│╭╰╮╯─]+', '', stripped).strip()
            if not stripped:
                continue
            if re.match(r'^[╭╮╰╯│─\s]+$', stripped):
                continue
            result.append(stripped)

        return '\n'.join(result).strip()


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

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    ch = bot.get_channel(CHANNEL_ID)
    print(f'✅ Bot ready as {bot.user}')

    if ch:
        await ch.send(
            f'🤖 Copilot bot online on `{os.uname().nodename}`\n'
            f'Type `!start` to start a new session, or `!handoff` to resume the most recent one.\n'
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
        '`!start` — start a new Copilot session\n'
        '`!exit` — close the current Copilot session\n'
        '`!restart` — restart Copilot session (fresh)\n'
        '`!handoff` — resume the most recent session\n\n'
    )

@bot.command(name='restart')
async def restart_cmd(ctx):
    msg = await ctx.send('⏳ Restarting Copilot session...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart)
    await msg.edit(content='✅ Copilot session restarted!')

@bot.command(name='start')
async def start_cmd(ctx):
    """Start a new fresh Copilot session."""
    msg = await ctx.send('⏳ Starting new Copilot session...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart)
    await msg.edit(content='✅ New Copilot session started — you can chat now!')

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
