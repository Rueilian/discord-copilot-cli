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
    r'(Thinking|Loading environment|Environment loaded|shift\+tab|Remaining reqs\.|ctrl\+[sq]|enqueue|switch mode|Type @|claude-sonnet|claude-opus|gpt-|medium|MCP server)'
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
        raw = self._tmux('capture-pane', '-t', TMUX_SESSION, '-p', '-e')
        return strip_ansi(raw)

    def _session_exists(self) -> bool:
        r = subprocess.run(['tmux', 'has-session', '-t', TMUX_SESSION],
                           capture_output=True)
        return r.returncode == 0

    def _get_terminal_session_id(self) -> str:
        """Find session ID used by the non-bot, non-tmux copilot process in the terminal."""
        try:
            result = subprocess.run(
                ['pgrep', '-a', '-f', 'copilot'],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                # Skip bot's own tmux session and unrelated processes
                if TMUX_SESSION in subprocess.run(
                    ['tmux', 'list-panes', '-t', TMUX_SESSION, '-F', '#{pane_pid}'],
                    capture_output=True, text=True
                ).stdout:
                    pass
                # Look for --resume=<uuid> pattern in non-tmux copilot process
                import re as _re
                m = _re.search(r'--resume[= ]([0-9a-f-]{36})', line)
                if m:
                    sid = m.group(1)
                    # Skip the bot's own session (started by tmux)
                    tmux_pids = subprocess.run(
                        ['tmux', 'list-panes', '-t', TMUX_SESSION, '-F', '#{pane_pid}'],
                        capture_output=True, text=True
                    ).stdout.split()
                    pid = line.split()[0]
                    if pid not in tmux_pids:
                        return sid
        except Exception as e:
            print(f"[HANDOFF] error finding session: {e}", flush=True)
        return ''

    def start(self, resume_id: str = ''):
        if self._session_exists():
            self._tmux('kill-session', '-t', TMUX_SESSION)
            time.sleep(1)
        # Build command — optionally resume a specific session
        cmd = COPILOT_CMD
        if resume_id:
            cmd = f'{COPILOT_CMD} --resume={resume_id}'
        # Create detached tmux session and start copilot
        self._tmux('new-session', '-d', '-s', TMUX_SESSION, '-x', '220', '-y', '50')
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

    def _ask_sync(self, question: str, timeout: int) -> str:
        if not self._session_exists():
            self.start()

        # Snapshot pane before sending to detect new content
        before = self._capture()
        print(f"[ASK] sending: {question!r}", flush=True)

        # Send message (escape special chars for tmux)
        self._tmux('send-keys', '-t', TMUX_SESSION, question, 'Enter')

        # Poll until response appears and copilot goes idle
        deadline = time.time() + timeout
        last_out = before
        stable_since = None

        while time.time() < deadline:
            time.sleep(2)
            out = self._capture()

            is_thinking = 'Thinking' in out
            is_idle = ('Type @' in out or 'shift+tab' in out) and not is_thinking

            if out != last_out:
                last_out = out
                stable_since = None
            elif is_idle:
                # Output stable + copilot idle → done
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 2:
                    break

        print(f"[ASK] pane (last 800): {last_out[-800:]!r}", flush=True)
        response = self._extract_response(before, last_out, question)
        print(f"[ASK] extracted: {response!r}", flush=True)
        if len(response) > 1800:
            response = response[:1800] + '\n...(truncated)'
        return response or '(no response captured)'

    def _extract_response(self, before: str, after: str, question: str) -> str:
        """Extract only the new AI response lines added after the question."""
        lines = after.split('\n')
        result = []
        # Find where the user's question appears, take content after it
        found_q = False
        for line in lines:
            stripped = line.strip()
            if not found_q:
                if question[:20] in line:
                    found_q = True
                continue
            if not stripped:
                continue
            if _STATUS_LINE.search(stripped):
                continue
            if _DIVIDER.match(stripped):
                continue
            stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s]+', '', stripped).strip()
            if not stripped:
                continue
            result.append(stripped)

        if not result:
            # Fallback: just return non-UI lines from after
            for line in lines:
                stripped = re.sub(r'^[●◉◎○◐◑◒◓▸▹►▻•·❯~\s]+', '', line.strip()).strip()
                if not stripped:
                    continue
                if _STATUS_LINE.search(stripped):
                    continue
                if _DIVIDER.match(stripped):
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

HANDOFF_FILE = Path.home() / '.copilot' / 'handoff-request'

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    ch = bot.get_channel(CHANNEL_ID)
    print(f'✅ Bot ready as {bot.user}')

    # Start Copilot session in background thread
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.start)

    if ch:
        await ch.send(f'🤖 Copilot bot online on `{os.uname().nodename}` — session ready, just type to chat!')

    # Start handoff file watcher
    bot.loop.create_task(watch_handoff(ch))

async def watch_handoff(ch):
    """Watch for handoff-request file written by the terminal skill."""
    print("[HANDOFF] watcher started", flush=True)
    while True:
        await asyncio.sleep(2)
        if HANDOFF_FILE.exists():
            session_id = HANDOFF_FILE.read_text().strip()
            HANDOFF_FILE.unlink()
            if session_id:
                print(f"[HANDOFF] picking up session {session_id}", flush=True)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, session.restart, session_id)
                if ch:
                    await ch.send(f'📲 Handoff received! Resumed session `{session_id[:8]}...` — continuing your conversation here.')

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
        response = await session.ask(question, timeout=60)

    await message.reply(response)

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name='help')
async def help_cmd(ctx):
    await ctx.send(
        '**Copilot Bot**\n'
        'Just type normally to chat with Copilot.\n\n'
        '**Shell commands:**\n'
        '`!run <cmd>` — run shell command\n'
        '`!sim p0 [p1 ...]` — run ADFP VCS simulation\n'
        '`!log p0` — tail simulation log\n'
        '`!ls [path]` — list directory\n'
        '`!status` — host info\n'
        '`!restart` — restart Copilot session (fresh)\n'
        '`!handoff` — resume current terminal session here\n'
    )

@bot.command(name='restart')
async def restart_cmd(ctx):
    msg = await ctx.send('⏳ Restarting Copilot session...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart)
    await msg.edit(content='✅ Copilot session restarted!')

@bot.command(name='handoff')
async def handoff_cmd(ctx, session_id: str = ''):
    """Resume the terminal Copilot session in Discord.
    Usage: !handoff           (auto-detect terminal session)
           !handoff <id>      (specify session ID explicitly)
    """
    if not session_id:
        session_id = session._get_terminal_session_id()
    if not session_id:
        await ctx.send(
            '❌ Could not find terminal Copilot session.\n'
            'Use `!handoff <session-id>` with the ID shown in your terminal.\n'
            'Find it with: `pgrep -a -f copilot | grep resume`'
        )
        return
    msg = await ctx.send(f'⏳ Handing off session `{session_id[:8]}...` to Discord...')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, session.restart, session_id)
    await msg.edit(content=f'✅ Resumed session `{session_id[:8]}...` — conversation history carried over!')

@bot.command(name='run')
async def run_cmd(ctx, *, cmd: str):
    msg = await ctx.send(f'⏳ Running: `{cmd}`')
    output = await run_shell(cmd, timeout=120)
    await msg.edit(content=f'✅ `{cmd}`\n{code_block(output)}')

@bot.command(name='sim')
async def sim_cmd(ctx, *patterns: str):
    if not patterns:
        await ctx.send('Usage: `!sim p0 [p1 p2 p3 p4]`')
        return
    rtl_dir = '~/1142_hw1/01_RTL'
    await ctx.send(f'⏳ Running: `{" ".join(patterns)}`')
    await run_shell(f'cd {rtl_dir} && cb', timeout=15)
    for pat in patterns:
        msg = await ctx.send(f'⏳ Simulating `{pat}`...')
        await run_shell(f'tcsh {rtl_dir}/01_run {pat}', timeout=90)
        log = f'{rtl_dir}/rtl_{pat}.log'
        result = await run_shell(f'grep -E "ALL PASS|FAIL|Total Errors" {log} | tail -3', timeout=5)
        icon = '✅' if 'ALL PASS' in result else '❌'
        await msg.edit(content=f'{icon} `{pat}`: {result}')

@bot.command(name='log')
async def log_cmd(ctx, pattern: str = 'p0'):
    log = f'~/1142_hw1/01_RTL/rtl_{pattern}.log'
    output = await run_shell(f'tail -30 {log}', timeout=5)
    await ctx.send(f'📄 `{log}` (tail)\n{code_block(output)}')

@bot.command(name='ls')
async def ls_cmd(ctx, path: str = '~/1142_hw1'):
    output = await run_shell(f'ls -la {path}', timeout=5)
    await ctx.send(code_block(output))

@bot.command(name='status')
async def status_cmd(ctx):
    output = await run_shell('uname -n && whoami && date', timeout=5)
    await ctx.send(f'🖥️ Host info:\n{code_block(output)}')

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    bot.run(TOKEN)
