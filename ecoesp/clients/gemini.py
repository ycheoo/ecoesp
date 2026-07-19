"""Gemini client and the retry/fallback generation engine shared by the
text-generation tasks and the audio pipeline.
"""

import logging
import random
import time

import httpx
from google import genai
from google.genai import types
import requests


logger = logging.getLogger(__name__)


class ResponsePayloadError(ValueError):
    """The API succeeded but did not return the requested payload."""


class GeminiClientPool:
    """Gemini SDK clients in configured scheduling order.

    `labels` names each client by API-key position (never the secret itself) so
    generated content can be traced to the key that produced it; they travel
    with the clients through key rotation."""

    def __init__(self, clients, labels=None):
        if not clients:
            raise ValueError('At least one Gemini client is required')
        self.clients = clients
        self.labels = labels if labels is not None else [
            f'key {i + 1}/{len(clients)}' for i in range(len(clients))]
        self.active_index = 0

    def active_key(self):
        """Label of the key most recently used by gemini_generate."""
        return self.labels[self.active_index]


def make_gemini_client(cfg):
    clients = [
        genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=cfg.gemini_timeout_ms),
        )
        for api_key in cfg.gemini_api_keys
    ]
    return GeminiClientPool(clients)


def _is_quota_error(e):
    """True when the request failed because the model is out of quota (429)."""
    return getattr(e, 'code', None) == 429 or 'RESOURCE_EXHAUSTED' in str(e)


def _is_daily_quota_error(e):
    """Only meaningful for a 429: True when it is a per-day quota (RPD), False
    when per-minute (RPM). Reads the structured QuotaFailure detail — its
    `quotaId` contains 'PerDay' for RPD, 'PerMinute' for RPM — and falls back to
    scanning the stringified error. Defaults to False (treat as RPM)."""
    details = getattr(e, 'details', None)
    if isinstance(details, dict):
        for item in details.get('error', {}).get('details', []):
            if str(item.get('@type', '')).endswith('QuotaFailure'):
                for violation in item.get('violations', []):
                    if 'PerDay' in str(violation.get('quotaId', '')):
                        return True
    return 'PerDay' in str(e)


def _is_retryable_error(e):
    if isinstance(e, ResponsePayloadError):
        return True
    if isinstance(e, (httpx.TransportError,
                      requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout)):
        return True
    code = getattr(e, 'code', None)
    return code == 408 or isinstance(code, int) and 500 <= code < 600


def _is_tts_retryable_error(e):
    """True for transient TTS failures worth a segment retry: empty/malformed
    audio, connection/timeout, 500/503, and any 400 INVALID_ARGUMENT. The TTS
    model rejects otherwise-valid content non-deterministically — sometimes
    trying to emit text, sometimes a generic invalid-argument — and a retry
    usually succeeds; the request shape itself is fixed, so a 400 is a content
    hiccup, not a malformed request."""
    if isinstance(e, ResponsePayloadError):
        return True
    if isinstance(e, (httpx.TransportError,
                      requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout)):
        return True
    return getattr(e, 'code', None) in {400, 500, 503}


def _retry_delay(attempt):
    """Exponential backoff capped at 30 seconds, plus a small jitter."""
    return min(2 ** attempt, 30) + random.uniform(0, 1)


def _require_text(response):
    """Extract non-empty text from a response, raising if it is missing."""
    text = response.text
    if not text or not text.strip():
        raise ResponsePayloadError('Model returned an empty text response')
    return text


def _require_audio(response):
    """Extract PCM bytes from a TTS response, raising if they are missing."""
    try:
        data = response.candidates[0].content.parts[0].inline_data.data
    except (AttributeError, IndexError, TypeError) as e:
        raise ResponsePayloadError(f'Malformed TTS response: {e}')
    if not data:
        raise ResponsePayloadError('Model returned an empty audio response')
    return data


def generate_once(sdk_client, model, contents, config=None, extract=None):
    """One model on one API key: a single request with no retry. Any error
    propagates so the caller decides what to do — the TTS path penalizes the key
    and moves on for a 429, and fails the segment for anything else. `extract`
    pulls the payload out of the response (e.g. _require_audio). Text generation
    uses gemini_generate's retry/fallback instead."""
    response = sdk_client.models.generate_content(
        model=model, contents=contents, config=config)
    return extract(response) if extract else response


def gemini_generate(client, models, contents, config=None, start=0, extract=None):
    """Generate content, walking the fallback chain `models` (a list, tried in
    order from index `start`). Transient errors retry the same model with
    backoff. A quota error (429) tries the same model with the next API key,
    then steps to the next model only after all configured keys are exhausted. `extract`
    (e.g. _require_text / _require_audio) pulls the payload out of the response
    inside the retry loop, so empty/malformed responses are retried too.
    Returns (extracted_payload_or_response, index_of_model_used). Raises if the
    chain is exhausted."""
    pool = client if isinstance(client, GeminiClientPool) else GeminiClientPool([client])
    if isinstance(models, str):
        models = [models]
    last_exc = None
    for i in range(start, len(models)):
        model = models[i]
        quota_exc = None
        transient_exhausted = False
        for key_index in range(pool.active_index, len(pool.clients)):
            sdk_client = pool.clients[key_index]
            quota_exc = None
            for attempt in range(5):
                try:
                    response = sdk_client.models.generate_content(
                        model=model, contents=contents, config=config)
                    if extract:
                        response = extract(response)
                    pool.active_index = key_index
                    return response, i
                except Exception as e:
                    last_exc = e
                    if _is_quota_error(e):
                        quota_exc = e
                        break
                    if not _is_retryable_error(e):
                        raise
                    if attempt == 4:
                        logger.warning('%s failed after 5 attempts: %s', model, e)
                        transient_exhausted = True
                        break
                    wait = _retry_delay(attempt)
                    logger.warning(
                        '%s transient error (attempt %s/5): %s. '
                        'Retrying in %.1fs...', model, attempt + 1, e, wait)
                    time.sleep(wait)

            if quota_exc is not None and key_index + 1 < len(pool.clients):
                next_index = key_index + 1
                logger.warning(
                    '%s quota exhausted on API key %s/%s; switching to '
                    'API key %s/%s.', model, key_index + 1, len(pool.clients),
                    next_index + 1, len(pool.clients))
                continue
            break

        if quota_exc is not None and not transient_exhausted:
            nxt = f'; falling back to {models[i + 1]}' if i + 1 < len(models) else ''
            scope = (' across configured API keys'
                     if len(pool.clients) > 1 else '')
            logger.warning('%s out of quota%s%s.', model, scope, nxt)
    raise last_exc
