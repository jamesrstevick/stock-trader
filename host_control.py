"""
Dell / always-on host control.

The dashboard writes a command file; supervisor.ps1 (on the host) pulls git,
restarts children, and writes a heartbeat status file. This module never
runs git pull itself — that would restart web_app mid-request.
"""

from __future__ import print_function

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
STATUS_PATH = os.path.join(DATA_DIR, 'host_status.json')
COMMAND_PATH = os.path.join(DATA_DIR, 'host_command.json')
STALE_SECONDS = 20
ALLOWED_ACTIONS = ('pull', 'restart')


def _ensure_data_dir() -> None:
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR)


def _run_git(args, timeout=15):
    creationflags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(
            ['git'] + list(args),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            creationflags=creationflags,
        )
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except OSError:
            pass
        return None, None
    except OSError:
        return None, None
    text = (out or '').strip()
    if p.returncode != 0:
        return None, (err or text or 'git failed')
    return text, None


def git_info() -> Dict[str, Any]:
    branch, _ = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
    sha, _ = _run_git(['rev-parse', '--short', 'HEAD'])
    porcelain, _ = _run_git(['status', '--porcelain'])
    return {
        'branch': branch,
        'sha': sha,
        'dirty': bool(porcelain) if porcelain is not None else None,
        'repo': os.path.isdir(os.path.join(ROOT, '.git')),
    }


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def supervisor_alive(status: Optional[Dict[str, Any]] = None) -> bool:
    if status is None:
        status = _read_json(STATUS_PATH)
    if not status or not status.get('supervisor'):
        return False
    updated = status.get('updated_at') or ''
    try:
        # Accept "2026-08-12T13:00:00" (local, no tz)
        ts = datetime.strptime(str(updated)[:19], '%Y-%m-%dT%H:%M:%S')
        age = (datetime.now() - ts).total_seconds()
    except (TypeError, ValueError):
        try:
            age = time.time() - os.path.getmtime(STATUS_PATH)
        except OSError:
            return False
    return age <= STALE_SECONDS


def get_host_status() -> Dict[str, Any]:
    file_status = _read_json(STATUS_PATH) or {}
    alive = supervisor_alive(file_status)
    git = git_info()
    if file_status.get('git'):
        # Prefer live git from this process; keep supervisor copy as fallback.
        for key in ('branch', 'sha', 'dirty', 'repo'):
            if git.get(key) in (None, ''):
                git[key] = file_status['git'].get(key)
    out = {
        'ok': True,
        'supervisor': alive,
        'git': git,
        'children': file_status.get('children') if alive else {},
        'last_command': file_status.get('last_command'),
        'busy': bool(file_status.get('busy')) if alive else False,
        'updated_at': file_status.get('updated_at') if alive else None,
        'supervisor_pid': file_status.get('pid') if alive else None,
    }
    if not alive:
        out['hint'] = (
            'Supervisor is not running on this machine. '
            'On the Dell, run supervisor.ps1 (or install_host_startup.ps1).'
        )
    pending = _read_json(COMMAND_PATH)
    if pending and pending.get('action'):
        out['pending_command'] = pending.get('action')
        out['busy'] = True
    return out


def queue_host_command(action: str) -> Dict[str, Any]:
    action = (action or '').strip().lower()
    if action not in ALLOWED_ACTIONS:
        return {'ok': False, 'error': 'action must be pull or restart'}
    if not supervisor_alive():
        return {
            'ok': False,
            'error': (
                'Supervisor is not running. On the Dell, start supervisor.ps1 '
                'before pulling or restarting.'
            ),
        }
    pending = _read_json(COMMAND_PATH)
    if pending and pending.get('action'):
        return {
            'ok': False,
            'error': 'A host command is already queued (%s)' % pending.get('action'),
        }
    status = _read_json(STATUS_PATH) or {}
    if status.get('busy'):
        return {'ok': False, 'error': 'Supervisor is busy with another command'}
    _ensure_data_dir()
    payload = {
        'id': str(uuid.uuid4()),
        'action': action,
        'requested_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
    }
    tmp = COMMAND_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    os.replace(tmp, COMMAND_PATH)
    return {
        'ok': True,
        'queued': True,
        'action': action,
        'id': payload['id'],
        'message': (
            'Pull requested — supervisor will fetch GitHub and restart processes'
            if action == 'pull'
            else 'Restart requested — supervisor will bounce loop, dashboard, and tunnel'
        ),
    }
