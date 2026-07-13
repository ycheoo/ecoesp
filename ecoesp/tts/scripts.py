"""Build the per-bullet study scripts for the segmented audio pipeline.

Each Today's-Top-Stories bullet becomes three short scripts — English original,
Chinese translation, and a spoken vocabulary explanation — written one file per
bullet into three folders under the message cache:

    <cache>/<msgid>/text/original/bullet_00.txt
                         translation/bullet_00.txt
                         vocab/bullet_00.txt

The original and translation are used verbatim from the parsed markdown, so they
are just written out. Only the vocabulary is reshaped for speech by the
audio_vocab prompt. All three files are atomically overwritten on each build.
"""

from dataclasses import dataclass
import os
import re

from ..storage.files import atomic_write, message_cache_path
from ..clients.gemini import ResponsePayloadError, _require_text, gemini_generate


@dataclass(frozen=True)
class BulletScripts:
    """The three final, TTS-ready scripts for one bullet."""
    original: str
    translation: str
    vocab: str


def _script_path(cfg, message_id, folder, index):
    """Path of one per-bullet script file, creating its folder."""
    path = message_cache_path(
        cfg, message_id,
        os.path.join('text', folder, f'bullet_{index:02d}.txt'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _parse_vocab_batch(text, expected_count):
    """Split and validate a model response containing numbered vocab scripts."""
    marker = re.compile(r'^<<<(END_)?BULLET_(\d{2})>>>\s*$', re.M)
    markers = list(marker.finditer(text))
    actual = [
        ('end' if match.group(1) else 'start', int(match.group(2)))
        for match in markers
    ]
    expected = [
        marker_info
        for index in range(expected_count)
        for marker_info in (('start', index), ('end', index))
    ]
    if actual != expected:
        raise ResponsePayloadError(
            f'Vocab batch markers must be {expected}, got {actual}')

    outside = [text[:markers[0].start()]] if markers else [text]
    outside.extend(
        text[markers[index].end():markers[index + 1].start()]
        for index in range(1, len(markers) - 1, 2)
    )
    if markers:
        outside.append(text[markers[-1].end():])
    if any(part.strip() for part in outside):
        raise ResponsePayloadError('Vocab batch contains text outside a marked section')

    scripts = []
    for index in range(expected_count):
        start = markers[index * 2]
        end = markers[index * 2 + 1]
        script = text[start.end():end.start()].strip()
        if not script:
            raise ResponsePayloadError(f'Vocab batch BULLET_{index:02d} is empty')
        scripts.append(script)
    return scripts


def build_scripts(cfg, client, message_id, bullets, vocab_prompt, manifest=None):
    """Write the original/translation/vocab script files for every bullet and
    return the matching BulletScripts list in document order."""
    for i, bullet in enumerate(bullets):
        atomic_write(_script_path(cfg, message_id, 'original', i), bullet.original)
        atomic_write(_script_path(cfg, message_id, 'translation', i), bullet.translation)

    items = '\n\n'.join(
        f'<<<INPUT_BULLET_{i:02d}>>>\n{bullet.vocab_raw}\n'
        f'<<<END_INPUT_BULLET_{i:02d}>>>'
        for i, bullet in enumerate(bullets)
    )
    print(f'Generating vocab scripts for {len(bullets)} bullets in one request...')
    prompt = vocab_prompt.format(items=items)

    # Parse inside extract so a malformed batch is retried, while capturing the
    # complete raw response of the successful attempt to persist it.
    raw_response = None

    def extract(response):
        nonlocal raw_response
        text = _require_text(response)
        parsed = _parse_vocab_batch(text, len(bullets))
        raw_response = text
        return parsed

    vocabs, model_index = gemini_generate(
        client, cfg.text_models, prompt, extract=extract)
    if raw_response is not None:
        atomic_write(
            message_cache_path(cfg, message_id, 'vocab_response.txt'),
            raw_response)
    if manifest:
        # This one batched request produces the complete vocab response, which is
        # then split into the text/vocab/bullet_NN.txt files.
        manifest.record('vocab', cfg.text_models[model_index],
                        client.active_key(), 'vocab_response.txt')

    scripts = []
    for i, (bullet, vocab) in enumerate(zip(bullets, vocabs)):
        vocab_path = _script_path(cfg, message_id, 'vocab', i)
        atomic_write(vocab_path, vocab)

        scripts.append(BulletScripts(
            original=bullet.original, translation=bullet.translation, vocab=vocab))
    return scripts
