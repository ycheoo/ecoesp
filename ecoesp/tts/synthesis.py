"""Text-to-speech: turn the spoken study script into an MP3.

This module is the seam for swapping the TTS backend (currently Gemini TTS).
"""

from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import subprocess
import time

from google.genai import types

from ..storage.files import atomic_write, message_cache_path
from ..clients.gemini import (
    GeminiClientPool, _is_daily_quota_error, _is_quota_error,
    _is_tts_retryable_error, _require_audio, generate_once)
from ..clients.scheduler import KeyScheduler


logger = logging.getLogger(__name__)


# Requests per minute allowed for one API key on one model. Worker concurrency
# is derived from this and the number of keys (keys x TTS_RPM): each key can
# have that many requests in flight, and the KeyScheduler holds the rate to
# TTS_RPM per key per window.
TTS_RPM = 3
# Rate-limit window. Slightly above 60s as a safety margin against clock skew
# and fixed-vs-sliding window boundary effects, so we stay under the API's
# per-minute quota rather than riding exactly on it.
TTS_WINDOW_SECONDS = 61
TTS_TRANSIENT_RETRIES = 3

# Gemini TTS returns raw 16-bit 24kHz mono PCM, i.e. 24000 * 2 bytes per second.
PCM_BYTES_PER_SECOND = 48000
PROGRESS_WIDTH = 20


def _silence(seconds):
    """Silent PCM of the given duration, to keep concatenated segments from
    running into each other. The durations come from the config, since how much
    of a pause reads as natural is a matter of taste."""
    return b'\x00' * int(PCM_BYTES_PER_SECOND * seconds)


def _bullet_progress(completed, total):
    """A journal-friendly progress bar: one complete line per update."""
    filled = completed * PROGRESS_WIDTH // total
    bar = '#' * filled + '-' * (PROGRESS_WIDTH - filled)
    return f'Bullet progress: [{bar}] {completed}/{total}'


# Samples whose absolute value stays below this count as silence; measured
# well above Gemini TTS's noise floor and well below any speech.
SILENCE_AMPLITUDE = 300
# The longest pause allowed to survive inside one synthesized clip. The
# longest legitimate pause observed is ~2s, so 3s only catches pathologies.
MAX_SILENCE_SECONDS = 3.0
COMPRESSED_SILENCE_SECONDS = 1.0


def _compress_long_silences(pcm):
    """Cap pathological pauses inside one synthesized clip.

    The TTS models render style instructions like "pause noticeably between
    entries" unpredictably; one observed clip held 9-10 seconds of dead air
    between vocabulary entries. Any run of near-silence longer than
    MAX_SILENCE_SECONDS is cut down to COMPRESSED_SILENCE_SECONDS, while
    legitimate pauses pass through untouched. Scans in 10ms windows; input
    shorter than one window (or not sample-aligned at the tail) is passed
    through as-is."""
    hop = PCM_BYTES_PER_SECOND // 2 // 100  # samples per 10ms window
    samples = array('h', pcm[:len(pcm) - (len(pcm) % 2)])
    windows = len(samples) // hop
    max_run = int(MAX_SILENCE_SECONDS * 100)
    quiet = [
        max(abs(s) for s in samples[i * hop:(i + 1) * hop]) < SILENCE_AMPLITUDE
        for i in range(windows)
    ]
    pieces, kept_to, i = [], 0, 0
    while i < windows:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < windows and quiet[j]:
            j += 1
        if j - i >= max_run:
            pieces.append(pcm[kept_to:i * hop * 2])
            pieces.append(_silence(COMPRESSED_SILENCE_SECONDS))
            kept_to = j * hop * 2
        i = j
    if not pieces:
        return pcm
    pieces.append(pcm[kept_to:])
    return b''.join(pieces)


def _tts_config(cfg):
    """Gemini TTS request config: audio output in the configured voice."""
    return types.GenerateContentConfig(
        response_modalities=['AUDIO'],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.tts_voice)
            )
        ),
    )


def _encode_mp3(cfg, message_id, pcm):
    """Encode the assembled PCM stream to the message's espresso.mp3."""
    mp3_path = message_cache_path(cfg, message_id, 'espresso.mp3')
    # Gemini TTS returns raw 16-bit 24kHz mono PCM.
    subprocess.run(
        ['ffmpeg', '-y', '-f', 's16le', '-ar', '24000', '-ac', '1', '-i', '-',
         '-b:a', '64k', mp3_path],
        input=pcm, check=True, capture_output=True,
    )
    return mp3_path


def load_opening_pcm(cfg):
    """Read the optional preconverted 24kHz 16-bit mono opening PCM.

    Unlike the prompts and the email template there is no shipped default to fall
    back to — the jingle is a personal file, so a user's own copy in the data
    directory is the only source. With no file there the audio simply starts at
    the first bullet, so returning empty bytes (rather than failing) lets the
    pipeline run out of the box without one.
    """
    path = os.path.join(cfg.app_data_dir, 'opening.pcm')
    if not os.path.isfile(path):
        logger.debug('No opening asset; starting at the first bullet.')
        return b''
    logger.debug('Using opening asset: %s', path)
    with open(path, 'rb') as f:
        return f.read()


def _synthesize_segment(scheduler, label, content, config):
    """Synthesize one segment (original, vocabulary, or translation) as its own
    TTS request. The scheduler picks the (key, model): it prefers the primary
    model and only downgrades once every key's primary is exhausted for the day.
    A per-day (RPD) 429 marks that (key, model) exhausted; a per-minute (RPM) 429
    penalizes its window; either way the segment retries on whatever the
    scheduler hands out next. A 400 (the TTS model non-deterministically
    rejecting content), 500, 503, network failure, or missing/malformed audio
    payload retries up to three times with 2/4/8-second backoff, acquiring
    scheduler budget again before every request; anything else fails the segment.
    Returns the PCM plus the model and API-key label that produced it."""
    logger.debug('Synthesizing %s...', label)
    transient_retries = 0
    while True:
        index, sdk_client, key_label, model = scheduler.acquire()
        try:
            data = generate_once(
                sdk_client, model, content, config, extract=_require_audio)
            logger.debug('%s ready with %s on %s.', label, model, key_label)
            return data, model, key_label
        except Exception as e:
            if _is_quota_error(e):
                if _is_daily_quota_error(e):
                    first_report = scheduler.mark_exhausted(index, model)
                    if first_report:
                        logger.warning(
                            '%s exhausted %s for the day.', key_label, model)
                    else:
                        logger.debug(
                            '%s: %s also returned daily quota exhaustion for '
                            '%s; already marked exhausted.',
                            label, key_label, model)
                else:
                    logger.warning(
                        '%s: %s hit %s rate limit; retrying elsewhere.',
                        label, key_label, model)
                    scheduler.penalize(index, model)
                continue
            if (_is_tts_retryable_error(e)
                    and transient_retries < TTS_TRANSIENT_RETRIES):
                delay = 2 ** (transient_retries + 1)
                transient_retries += 1
                logger.warning(
                    '%s: transient error from %s on %s; retry %s/%s in %ss: %s',
                    label, model, key_label, transient_retries,
                    TTS_TRANSIENT_RETRIES, delay, e)
                time.sleep(delay)
                continue
            raise


def synthesize_study_audio(cfg, client, message_id, scripts, opening_pcm=b'',
                           instructions=None, manifest=None):
    """Render the per-bullet study scripts into one MP3 and return its path.

    Each bullet's original, vocabulary, and translation are synthesized as three
    separate TTS clips; the English original is synthesized once and its clip is
    reused for all three readings, so they are byte-identical. `instructions`
    maps each part ('original', 'vocab', 'translation') to the TTS instruction
    prepended to that segment's text, so each part can be narrated differently.
    Keys and models are handed out by a shared KeyScheduler: it holds each
    (key, model) to TTS_RPM requests per minute, prefers the first `TTS_MODELS`
    entry, and downgrades to the next model only once every key's earlier model
    is exhausted for the day. Concurrency is keys x TTS_RPM. Clips are assembled
    in document order: the opening, then per bullet original / vocabulary /
    original / translation / original."""
    if not scripts:
        raise ValueError('At least one bullet script is required for TTS')

    instructions = instructions or {}
    config = _tts_config(cfg)
    pool = client if isinstance(client, GeminiClientPool) else GeminiClientPool([client])
    scheduler = KeyScheduler(
        pool.clients, pool.labels, cfg.tts_models,
        rpm=TTS_RPM, window=TTS_WINDOW_SECONDS)
    workers = len(pool.clients) * TTS_RPM
    # One synthesis task per unique segment; the original is rendered just once.
    tasks = [
        (index, part, instructions.get(part, '') + text)
        for index, script in enumerate(scripts)
        for part, text in (('original', script.original),
                           ('vocab', script.vocab),
                           ('translation', script.translation))
    ]

    clips = {}
    completed_parts = [0] * len(scripts)
    completed_bullets = 0
    logger.info(_bullet_progress(completed_bullets, len(scripts)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for index, part, content in tasks:
            future = executor.submit(
                _synthesize_segment, scheduler,
                f'bullet {index + 1} {part}', content, config)
            futures[future] = (index, part)
        # Persist each segment's PCM and record its source in the main thread as
        # results arrive; each part has its own tts/ subdirectory, and files are
        # named by bullet and overwritten each run (no reuse).
        for future in as_completed(futures):
            index, part = futures[future]
            data, model, key = future.result()
            clips[(index, part)] = data
            name = os.path.join(
                'tts', part, f'bullet_{index:02d}.pcm')
            path = message_cache_path(cfg, message_id, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            atomic_write(path, data)
            if manifest:
                manifest.record(f'bullet {index + 1} {part}', model, key, name)
            completed_parts[index] += 1
            if completed_parts[index] == 3:
                completed_bullets += 1
                logger.info(
                    _bullet_progress(completed_bullets, len(scripts)))

    segment_silence = _silence(cfg.segment_gap_seconds)
    bullet_silence = _silence(cfg.bullet_gap_seconds)
    bullets = []
    for index in range(len(scripts)):
        # The cached per-segment PCMs stay as the model produced them; the
        # pathological-pause compression applies only to what gets assembled.
        original, vocab, translation = (
            _compress_long_silences(clips[(index, part)])
            for part in ('original', 'vocab', 'translation'))
        segments = [original, vocab, original, translation, original]
        # Segments within one bullet get the shorter gap.
        bullets.append(segment_silence.join(segments))
    # The opening already ends with its own pause, so it leads straight in;
    # consecutive bullets are separated by the longer gap.
    pcm = bytes(opening_pcm) + bullet_silence.join(bullets)
    return _encode_mp3(cfg, message_id, pcm)
