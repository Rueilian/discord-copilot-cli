#!/usr/bin/env python3
"""
handoff.py — hand off the current Copilot CLI session to Discord.

Finds the current session ID and writes it to ~/.copilot/handoff-request.
The Discord bot will pick it up and resume the session.
"""
import os
import subprocess
import sys
from pathlib import Path

HANDOFF_FILE = Path.home() / '.copilot' / 'handoff-request'

def find_session_id() -> str:
    """Find the current Copilot CLI session ID from running processes."""
    try:
        result = subprocess.run(
            ['ps', 'aux'], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'copilot' in line and '--resume=' in line:
                for part in line.split():
                    if part.startswith('--resume='):
                        return part.split('=', 1)[1]
    except Exception:
        pass
    return ''

def find_latest_session() -> str:
    """Find the most recently modified session directory."""
    session_dir = Path.home() / '.copilot' / 'session-state'
    if not session_dir.exists():
        return ''
    sessions = sorted(session_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0].name if sessions else ''

def main():
    session_id = find_session_id() or find_latest_session()
    if not session_id:
        print('❌ No active Copilot session found.')
        sys.exit(1)

    HANDOFF_FILE.write_text(session_id)
    print(f'✅ Handoff requested for session {session_id[:8]}...')
    print('   Discord bot will resume this conversation shortly.')
    print('   You can close this terminal.')

if __name__ == '__main__':
    main()
