"""Shared, rate- and quota-aware scheduler that hands out (API key, model) pairs.

The TTS workers run concurrently and must not exceed each key's per-model
requests-per-minute quota, and must react to per-day (RPD) exhaustion by falling
back through the model chain. Rather than let every worker guess, they all draw
from one KeyScheduler:

- Per (key, model) it enforces a sliding window of at most `rpm` requests per
  `window` seconds, blocking a worker until some slot has budget.
- It always prefers the earliest model in the chain: it only hands out a later
  (fallback) model once *every* key's earlier model has been marked exhausted
  for the day. While an earlier model still has a live key, a worker waits for
  that key's per-minute budget instead of downgrading.
- `mark_exhausted(key, model)` records an RPD hit so that (key, model) is never
  handed out again this run; `penalize(key, model)` fills its minute window as
  the rare RPM-429 safety net.

Key/model selection lives here and only here, so usage is coordinated instead of
cascading.
"""

import threading
import time


class KeyScheduler:
    def __init__(self, clients, labels, models, rpm=3, window=60.0,
                 now=time.monotonic):
        if not clients:
            raise ValueError('KeyScheduler needs at least one client')
        if not models:
            raise ValueError('KeyScheduler needs at least one model')
        self._clients = list(clients)
        self._labels = list(labels)
        self._models = list(models)
        self._rpm = rpm
        self._window = window
        self._now = now
        self._hits = {(m, k): [] for m in range(len(self._models))
                      for k in range(len(self._clients))}
        self._exhausted = set()  # (model_index, key_index) that hit RPD this run
        self._cond = threading.Condition()

    def acquire(self):
        """Block until some (key, model) has budget, record a request against it,
        and return (key_index, client, label, model). Raises when every model is
        exhausted on every key."""
        with self._cond:
            while True:
                now = self._now()
                self._prune(now)
                choice = self._pick()
                if choice is not None:
                    m, k = choice
                    self._hits[(m, k)].append(now)
                    return k, self._clients[k], self._labels[k], self._models[m]
                if self._all_exhausted():
                    raise RuntimeError(
                        'Every TTS model is exhausted on every API key')
                self._cond.wait(timeout=self._time_until_free(now))

    def mark_exhausted(self, key_index, model):
        """Record a per-day (RPD) exhaustion: never hand out this (key, model)
        again for the rest of the run. Return True only for the first report so
        concurrent workers can suppress duplicate warnings atomically."""
        with self._cond:
            pair = (self._models.index(model), key_index)
            first_report = pair not in self._exhausted
            self._exhausted.add(pair)
            self._cond.notify_all()
            return first_report

    def penalize(self, key_index, model):
        """Fill (key, model)'s minute window — the RPM-429 safety net."""
        with self._cond:
            self._hits[(self._models.index(model), key_index)] = \
                [self._now()] * self._rpm
            self._cond.notify_all()

    def _prune(self, now):
        cutoff = now - self._window
        for hits in self._hits.values():
            hits[:] = [t for t in hits if t > cutoff]

    def _live_keys(self, m):
        return [k for k in range(len(self._clients))
                if (m, k) not in self._exhausted]

    def _pick(self):
        """(model_index, key_index) to use, or None. Walks models in preference
        order; the first model that still has any live key "owns" this request —
        return its key with the most budget, or None (wait for its RPM) if all
        its live keys are saturated. Only fully-exhausted models are skipped."""
        for m in range(len(self._models)):
            live = self._live_keys(m)
            if not live:
                continue
            free = [k for k in live if len(self._hits[(m, k)]) < self._rpm]
            if not free:
                return None  # stay on this model, wait for its minute budget
            return m, min(free, key=lambda k: (
                len(self._hits[(m, k)]),
                self._hits[(m, k)][-1] if self._hits[(m, k)] else 0.0))
        return None

    def _all_exhausted(self):
        return len(self._exhausted) == len(self._models) * len(self._clients)

    def _time_until_free(self, now):
        for m in range(len(self._models)):
            live = self._live_keys(m)
            if live:
                return max(0.0, min(self._hits[(m, k)][0] + self._window - now
                                    for k in live))
        return 0.0
