"""Records which API key and model produced each generated artifact.

Each message's cache directory gets a generation.json manifest so any produced
content (the translation, the per-bullet vocabulary scripts, each audio segment)
can be traced back to the exact API key and model that generated it, which also
gives visibility into per-key usage. The manifest is a sidecar file rather than
an in-content annotation, because the vocabulary scripts are fed verbatim to TTS
and any inline note would be read aloud. Keys are named by position (e.g.
"key 2/3"), never by the secret value.
"""

import json
import threading

from .files import atomic_write, message_cache_path

MANIFEST_NAME = 'generation.json'


class GenerationManifest:
    """Thread-safe recorder of (api, task, model) generation entries.

    Audio segments are synthesized concurrently, so record() is guarded by a
    lock and rewrites the whole manifest atomically on each entry — the file
    stays valid and complete even if a run stops partway."""

    def __init__(self, cfg, message_id):
        self._cfg = cfg
        self._message_id = message_id
        self._records = []
        self._lock = threading.Lock()

    def record(self, task, model, key, file):
        """Record that `task` was produced by `model` using API `key`, writing
        output `file` (relative to the message cache dir), and persist the
        manifest so far."""
        with self._lock:
            self._records.append(
                {'key': key, 'task': task, 'model': model, 'file': file})
            payload = json.dumps(
                sorted(self._records, key=lambda r: r['task']),
                ensure_ascii=False, indent=2) + '\n'
            path = message_cache_path(self._cfg, self._message_id, MANIFEST_NAME)
            atomic_write(path, payload)
