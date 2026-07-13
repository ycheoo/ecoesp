"""Persistent record of source messages that were successfully delivered."""

from datetime import datetime, timezone
import json
import os

from .files import atomic_write


def _load(path):
    if not os.path.exists(path):
        return {'version': 1, 'messages': {}}
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data.get('messages'), dict):
        raise ValueError(f'Invalid delivery state file: {path}')
    return data


def was_processed(cfg, message_id):
    return message_id in _load(cfg.processed_messages_path)['messages']


def mark_processed(cfg, message_id, subject):
    """Record a successful delivery using an atomic file replacement."""
    data = _load(cfg.processed_messages_path)
    data['messages'][message_id] = {
        'subject': subject,
        'sent_at': datetime.now(timezone.utc).isoformat(),
    }

    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    atomic_write(cfg.processed_messages_path, payload)
