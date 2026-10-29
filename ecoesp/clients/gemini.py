"""Gemini client and the retry/fallback generation engine shared by the
text-generation tasks and the audio pipeline.
"""

import io
import logging
import random
import time
import wave

import httpx
from google import genai
from google.genai import types
import requests


logger = logging.getLogger(__name__)


class ResponsePayloadError(ValueError):
    """The API succeeded but did not return the requested payload."""


class PathologicalPauseError(ResponsePayloadError):
    """A TTS clip arrived intact but holds an unacceptably long silent run.

    Subclasses ResponsePayloadError so the segment retry path treats it like
    any other bad payload. It carries the offending PCM so a caller whose
    retry budget runs out can still repair this clip rather than lose the
    segment, which would cost the whole run its audio."""

    def __init__(self, message, pcm):
        super().__init__(message)
        self.pcm = pcm


class GeminiLocationError(RuntimeError):
    """The Gemini API rejected the request's network location."""


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
        # Models gemini_generate gave up on during this run, so later calls
        # on the same pool skip them (see gemini_generate).
        self.given_up = set()
        # (model, key index) pairs that answered 429 this run. Quota is
        # counted per key and per model, so a key out of quota on one
        # model is skipped on that model only: until 2026-10-28 the pool
        # remembered just the key that last worked, and a model the chain
        # stepped to never saw the keys before it.
        self.out_of_quota = set()

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


def _is_model_unavailable_error(e):
    """True when the chain's current model is not there to call: retired by
    Google, misspelled, or not enabled for this API key's project. The API
    answers a generate request for such a model with 404 NOT_FOUND."""
    return (getattr(e, 'code', None) == 404
            and getattr(e, 'status', None) == 'NOT_FOUND')


def _raise_for_unsupported_location(error):
    message = str(getattr(error, 'message', error)).lower()
    if (getattr(error, 'code', None) == 400
            and getattr(error, 'status', None) == 'FAILED_PRECONDITION'
            and 'location' in message
            and 'not supported' in message):
        raise GeminiLocationError(
            'the current network location is not supported by the Gemini API; '
            'use a proxy or outbound route in a supported region and try again'
        ) from error


def _is_tts_retryable_error(e):
    """True for transient TTS failures worth a segment retry: empty/malformed
    audio, a clip holding pathological dead air, connection/timeout, 500/503,
    and any 400 INVALID_ARGUMENT. The TTS model rejects otherwise-valid
    content non-deterministically — sometimes trying to emit text, sometimes a
    generic invalid-argument — and a retry usually succeeds; the request shape
    itself is fixed, so a 400 is a content hiccup, not a malformed request."""
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


def _error_summary(error):
    """Concise retry reason for normal logs; verbose logs retain the detail."""
    code = getattr(error, 'code', None)
    status = getattr(error, 'status', None)
    if code is not None:
        return f'{code} {status}' if status else str(code)
    if isinstance(error, ResponsePayloadError):
        return str(error)
    return type(error).__name__


def _require_text(response):
    """Extract non-empty text from a response, raising if it is missing."""
    text = response.text
    if not text or not text.strip():
        raise ResponsePayloadError('Model returned an empty text response')
    return text


# What everything downstream of _require_audio takes a clip to be: the
# gaps, the dead-air check and ffmpeg's `-f s16le -ar 24000 -ac 1`.
PCM_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_BYTES = 2


def _pcm_from_blob(data, mime_type):
    """The 16-bit 24 kHz mono samples in a TTS response's audio blob.

    The TTS models answer in one of two containers (measured
    2026-10-24). gemini-3.1-flash-tts-preview sends
    'audio/l16; rate=24000; channels=1': the bare samples, little-endian
    whatever RFC 2586 says of L16, as every MP3 it has made shows. The
    3.8 TTS models send 'audio/wav', a RIFF file whose data chunk is
    followed by a C2PA provenance chunk; taken as samples, the header
    and that chunk would sound at each clip's ends, so only the data
    chunk is kept. Any other format is a payload error: the segment
    retries, then fails, rather than play at the wrong pitch or
    speed."""
    kind, _, params = (mime_type or '').partition(';')
    kind = kind.strip().lower()
    if kind in ('audio/wav', 'audio/x-wav', 'audio/wave') or data[:4] == b'RIFF':
        try:
            with wave.open(io.BytesIO(data)) as w:
                shape = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames = w.getnframes()
                pcm = w.readframes(frames)
        except (wave.Error, EOFError) as e:
            raise ResponsePayloadError(f'Malformed WAV in TTS response: {e}')
        # readframes returns what is there without complaint when the
        # data chunk ends before its declared size, so a reply cut off
        # mid-clip would otherwise pass as a shorter clip and the
        # recording would lose the rest of the sentence.
        expected = frames * shape[0] * shape[1]
        if len(pcm) < expected:
            raise ResponsePayloadError(
                f'Truncated WAV in TTS response: {len(pcm)} of {expected} '
                f'sample bytes')
    elif kind in ('', 'audio/l16', 'audio/pcm'):
        fields = dict(
            (k.strip().lower(), v.strip())
            for k, _, v in (p.partition('=') for p in params.split(';'))
            if v)
        try:
            shape = (int(fields.get('channels', PCM_CHANNELS)),
                     PCM_SAMPLE_BYTES, int(fields.get('rate', PCM_RATE)))
        except ValueError:
            raise ResponsePayloadError(
                f'Unreadable TTS audio type {mime_type!r}')
        pcm = data
    else:
        raise ResponsePayloadError(
            f'TTS response is {mime_type!r}, neither L16 nor WAV')
    if shape != (PCM_CHANNELS, PCM_SAMPLE_BYTES, PCM_RATE):
        channels, width, rate = shape
        raise ResponsePayloadError(
            f'TTS audio is {channels}-channel {width * 8}-bit {rate} Hz, '
            f'not mono 16-bit {PCM_RATE} Hz')
    return pcm


def _require_audio(response):
    """Extract 16-bit 24 kHz mono PCM from a TTS response, raising if it
    is missing or in any other format."""
    try:
        blob = response.candidates[0].content.parts[0].inline_data
        data = blob.data
    except (AttributeError, IndexError, TypeError) as e:
        raise ResponsePayloadError(f'Malformed TTS response: {e}')
    if not data:
        raise ResponsePayloadError('Model returned an empty audio response')
    pcm = _pcm_from_blob(data, getattr(blob, 'mime_type', None))
    if not pcm:
        raise ResponsePayloadError('Model returned an empty audio response')
    return pcm


def generate_once(sdk_client, model, contents, config=None, extract=None):
    """One model on one API key: a single request with no retry. Any error
    propagates so the caller decides what to do — the TTS path penalizes the key
    and moves on for a 429, and fails the segment for anything else. `extract`
    pulls the payload out of the response (e.g. _require_audio). Text generation
    uses gemini_generate's retry/fallback instead."""
    try:
        response = sdk_client.models.generate_content(
            model=model, contents=contents, config=config)
    except Exception as e:
        _raise_for_unsupported_location(e)
        raise
    return extract(response) if extract else response


# Attempts per model for a text request before the chain steps to the next
# model. Three, down from the five of June 2026, which were set
# when there was no fallback model: on 2026-10-27 five attempts on each of
# two unavailable models kept the run translating for 19 minutes.
TEXT_ATTEMPTS = 3


def _is_deadline_error(e):
    """True when the request ran out its whole deadline: the API's own
    504 DEADLINE_EXCEEDED or the SDK's client-side timeout. Either way the
    call has already cost the full `GEMINI_TIMEOUT_MS`, which is what sets
    such a failure apart from a 503 that comes back in seconds."""
    if isinstance(e, (httpx.TimeoutException, requests.exceptions.Timeout)):
        return True
    return getattr(e, 'code', None) == 504


def gemini_generate(client, models, contents, config=None, start=0, extract=None):
    """Generate content, walking the fallback chain `models` (a list, tried in
    order from index `start`). Transient errors retry the same model with
    backoff, TEXT_ATTEMPTS times in all. A quota error (429) tries the same
    model with the next API key, then steps to the next model only after all
    configured keys are exhausted; the (model, key) pairs that answered 429
    are remembered on the pool for the run, per model, so the next model in
    the chain starts from the first key again. A model the API does not have
    (404) steps to the next model at once, without retrying it or trying
    other keys.
    `extract` (e.g. _require_text / _require_audio) pulls the payload out of
    the response inside the retry loop, so empty/malformed responses are
    retried too.

    Two rules keep a chain of unavailable models from eating the run, which
    on 2026-10-27 cost the newsletter (the production unit timed out at
    30 min with the text work still running): a deadline failure (see
    _is_deadline_error)
    steps to the next model at once instead of retrying, since it has
    already cost a full request deadline and on that day came in runs of
    four; and a model given up on for any reason is remembered on the pool
    and skipped by later calls in the same run, so the vocab request does
    not re-walk the models the translation already found down. The last
    model of a call's chain is exempt from both, and from the quota memory
    above: with nothing to fall back to, retrying is better than failing
    the run.

    Returns (extracted_payload_or_response, index_of_model_used). Raises if the
    chain is exhausted."""
    pool = client if isinstance(client, GeminiClientPool) else GeminiClientPool([client])
    if isinstance(models, str):
        models = [models]
    candidates = [i for i in range(start, len(models))
                  if models[i] not in pool.given_up]
    if not candidates:
        candidates = [len(models) - 1]
    skipped = [models[i] for i in range(start, len(models))
               if i not in candidates]
    if skipped:
        logger.info('Skipping %s, given up earlier in this run.',
                    ', '.join(skipped))
    last_exc = None
    for position, i in enumerate(candidates):
        model = models[i]
        fallback = (models[candidates[position + 1]]
                    if position + 1 < len(candidates) else None)
        quota_exc = None
        missing_exc = None
        deadline_exc = None
        transient_exhausted = False
        live_keys = [k for k in range(len(pool.clients))
                     if (model, k) not in pool.out_of_quota]
        if not live_keys:
            if fallback is not None:
                logger.info('%s is out of quota on every API key; '
                            'skipping it.', model)
                continue
            # The last model is exempt, as from given_up: with nothing
            # to fall back to, a 429 that was per minute may have lifted.
            live_keys = list(range(len(pool.clients)))
        for position_in_keys, key_index in enumerate(live_keys):
            sdk_client = pool.clients[key_index]
            quota_exc = None
            for attempt in range(TEXT_ATTEMPTS):
                try:
                    response = sdk_client.models.generate_content(
                        model=model, contents=contents, config=config)
                    if extract:
                        response = extract(response)
                    pool.active_index = key_index
                    return response, i
                except Exception as e:
                    last_exc = e
                    _raise_for_unsupported_location(e)
                    if _is_model_unavailable_error(e):
                        missing_exc = e
                        break
                    if _is_quota_error(e):
                        quota_exc = e
                        break
                    if not _is_retryable_error(e):
                        raise
                    if fallback is not None and _is_deadline_error(e):
                        deadline_exc = e
                        break
                    if attempt == TEXT_ATTEMPTS - 1:
                        logger.debug(
                            '%s final transient error detail: %s', model, e)
                        transient_exhausted = True
                        break
                    wait = _retry_delay(attempt)
                    logger.warning(
                        '%s transient failure (%s); retry %s/%s in %.1fs.',
                        model, _error_summary(e), attempt + 1,
                        TEXT_ATTEMPTS - 1, wait)
                    logger.debug(
                        '%s transient error detail: %s', model, e)
                    time.sleep(wait)

            if missing_exc is not None or deadline_exc is not None:
                break
            if quota_exc is not None:
                pool.out_of_quota.add((model, key_index))
                if position_in_keys + 1 < len(live_keys):
                    next_index = live_keys[position_in_keys + 1]
                    logger.warning(
                        '%s quota exhausted on API key %s/%s; switching to '
                        'API key %s/%s.', model, key_index + 1,
                        len(pool.clients), next_index + 1, len(pool.clients))
                    continue
            break

        pool.given_up.add(model)

        if missing_exc is not None:
            if fallback is not None:
                logger.warning(
                    '%s is not available (%s); falling back to %s.',
                    model, _error_summary(missing_exc), fallback)
            else:
                logger.warning(
                    '%s is not available (%s); no fallback model remains.',
                    model, _error_summary(missing_exc))
            continue

        if deadline_exc is not None:
            logger.warning(
                '%s ran out its deadline (%s); falling back to %s without '
                'retrying.', model, _error_summary(deadline_exc), fallback)
            logger.debug('%s deadline error detail: %s', model, deadline_exc)
            continue

        if transient_exhausted:
            if fallback is not None:
                logger.warning(
                    '%s unavailable after %s attempts; falling back to %s.',
                    model, TEXT_ATTEMPTS, fallback)
            else:
                logger.warning(
                    '%s unavailable after %s attempts; no fallback model '
                    'remains.', model, TEXT_ATTEMPTS)
            continue

        if quota_exc is not None:
            nxt = f'; falling back to {fallback}' if fallback is not None else ''
            scope = (' across configured API keys'
                     if len(pool.clients) > 1 else '')
            logger.warning('%s out of quota%s%s.', model, scope, nxt)
    raise last_exc
