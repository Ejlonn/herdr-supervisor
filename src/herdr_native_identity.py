"""Codex SessionStart identity hook for a verified pane-local (--no-daemon) TUI.

No transcript path required: current Codex's hook contract permits null. Never
uses a PID/thread mapping, newest rollout, or the shared daemon's pane context.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from herdr_core import valid_pane_id
from herdr_session_catalog import canonical_identity


def native_hook_params(event: Any, env: dict[str, str], process: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict) or event.get('hook_event_name') != 'SessionStart' or event.get('source') not in ('startup','resume','clear','compact'):
        return None
    if event.get('agent_id') or event.get('subagent_type') or not canonical_identity(event.get('session_id')):
        return None
    pane = env.get('HERDR_PANE_ID')
    if env.get('HERDR_ENV') != '1' or not valid_pane_id(pane) or not env.get('HERDR_SOCKET_PATH', '').startswith('/'):
        return None
    if not isinstance(process, dict) or process.get('pane_id') != pane:
        return None
    foreground = process.get('foreground_processes')
    if not isinstance(foreground, list) or len(foreground) != 1:
        return None
    p = foreground[0]
    argv = p.get('argv') if isinstance(p, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(a,str) for a in argv):
        return None
    if Path(argv[0]).name != 'codex' or argv.count('--no-daemon') != 1 or any(a in argv for a in ('--remote','--remote-auth-token-env')):
        return None
    if any(a.startswith('--remote=') or a.startswith('--remote-auth-token-env=') for a in argv):
        return None
    return {'pane_id': pane, 'source': 'herdr:codex', 'agent': 'codex',
            'agent_session_id': event['session_id'], 'session_start_source': event['source']}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            return 0
        event = json.loads(raw)
        pane = os.environ.get('HERDR_PANE_ID', '')
        if os.environ.get('HERDR_ENV') != '1' or not valid_pane_id(pane):
            return 0
        result = subprocess.run(['herdr','pane','process-info','--pane',pane],capture_output=True,text=True,check=True,timeout=3)
        process = json.loads(result.stdout).get('result',{}).get('process_info')
        params = native_hook_params(event, dict(os.environ), process)
        if params is None:
            return 0
        params['seq'] = time.time_ns()
        request = {'id': f"supervisor-native-session:{params['seq']}", 'method': 'pane.report_agent_session', 'params': params}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(os.environ['HERDR_SOCKET_PATH'])
            client.sendall((json.dumps(request)+'\n').encode())
            client.recv(4096)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # no hook output, no credential/path/log exposure, no model context added
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
