"""Atomic file writes and per-message output directory management."""

import hashlib
import os
import re
import shutil
import tempfile
import time


def _task_key(message_id):
    if re.fullmatch(r'[A-Za-z0-9_-]+', message_id):
        return message_id
    return hashlib.sha256(message_id.encode()).hexdigest()[:24]


def message_cache_path(cfg, message_id, name):
    """Path of an output file scoped to one Gmail source message."""
    message_dir = os.path.join(cfg.app_cache_dir, _task_key(message_id))
    os.makedirs(message_dir, exist_ok=True)
    return os.path.join(message_dir, name)


def resolve_asset(override, bundled):
    """The user's override file if they made one, otherwise the shipped default.

    Prompts, the email template, and the opening jingle ship with the app but are
    the parts a user is most likely to want to change. Layering an override on
    top of the shipped default keeps the app working out of the box, lets the
    defaults keep improving without clobbering anyone's edits, and is the only
    way to customise them at all once the app is a frozen single-file binary,
    whose own files live in a throwaway extraction directory.
    """
    return override if os.path.isfile(override) else bundled


def atomic_write(path, data):
    """Atomically write text or bytes to a file in its target directory."""
    directory = os.path.dirname(path)
    prefix = f'.{os.path.basename(path)}.'
    fd, temp_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        if isinstance(data, str):
            stream = os.fdopen(fd, 'w', encoding='utf-8')
        elif isinstance(data, bytes):
            stream = os.fdopen(fd, 'wb')
        else:
            raise TypeError('atomic_write data must be str or bytes')
        with stream as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def prune_message_cache(cfg, max_age_days=7):
    """Drop message output directories that have not been used recently."""
    if not os.path.isdir(cfg.app_cache_dir):
        return
    cutoff = time.time() - max_age_days * 24 * 3600
    for name in os.listdir(cfg.app_cache_dir):
        path = os.path.join(cfg.app_cache_dir, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
