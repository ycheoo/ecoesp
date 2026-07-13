#!/usr/bin/env python3
"""
Economist Espresso Translator
Reads today's Economist Espresso email, translates it with vocabulary annotations,
synthesizes a bilingual audio version, and sends both back to the inbox.
"""

import argparse
import os
import sys

from .config import ConfigError, load_config
from .storage.delivery_state import mark_processed, was_processed
from .clients.gemini import make_gemini_client
from .clients.gmail_client import (
    find_espresso_email,
    get_email_content,
    get_gmail_service,
    send_email,
)
from .storage.files import atomic_write, message_cache_path, prune_message_cache
from .storage.manifest import GenerationManifest
from .text.bullets import parse_top_stories, validate_top_stories
from .tts.scripts import build_scripts
from .text.translate import load_prompt, process_with_gemini
from .text.render import build_html_email
from .tts.synthesis import load_opening_pcm, synthesize_study_audio


def _positive_int(value):
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError('must be a positive integer') from e
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Process an Economist Espresso email.')
    parser.add_argument(
        '--force', action='store_true',
        help='Process the selected email even if it was already delivered.')
    parser.add_argument(
        '--require-audio', action='store_true',
        help='Do not send the email if audio generation fails.')
    parser.add_argument(
        '--prepare-only', action='store_true',
        help='Generate text scripts without TTS or email delivery.')
    parser.add_argument(
        '--lookback-hours', type=_positive_int, default=24,
        help='Search for source emails received within this many hours '
             '(default: 24).')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print('Configuration error:', file=sys.stderr)
        for error in e.errors:
            print(f'- {error}', file=sys.stderr)
        print(f'\nConfiguration file: {e.config_path}', file=sys.stderr)
        return 2
    prune_message_cache(cfg)

    if not os.path.exists(cfg.credentials_path):
        print(f'ERROR: credentials.json not found at {cfg.credentials_path}')
        print('Please follow the setup instructions in README to download it from Google Cloud Console.')
        return 1

    print('Authenticating with Gmail...')
    service = get_gmail_service(cfg)

    print('Searching for today\'s Economist Espresso email...')
    message = find_espresso_email(
        cfg, service, lookback_hours=args.lookback_hours)
    if not message:
        print('No Economist Espresso email found. Exiting.')
        return 0

    message_id = message['id']
    if was_processed(cfg, message_id) and not args.force:
        print('This source email was already processed. Exiting. (Use --force to resend.)')
        return 0

    subject, sender, body = get_email_content(service, message['id'])
    print(f'Found: "{subject}" from {sender}')

    if not body.strip():
        print('Email body is empty. Exiting.')
        return 1

    client = make_gemini_client(cfg)
    manifest = GenerationManifest(cfg, message_id)

    print('Processing with Gemini (translating + annotating vocabulary)...')
    translation_prompt = load_prompt(cfg, 'text_translation.md')
    processed = process_with_gemini(
        cfg, client, subject, body, translation_prompt, manifest=manifest)
    atomic_write(
        message_cache_path(cfg, message_id, 'processed.md'), processed)

    print('Building HTML email...')
    html_email = build_html_email(cfg, processed)

    audio_path = None
    try:
        print('Building per-bullet study scripts...')
        bullets = parse_top_stories(processed)
        validate_top_stories(bullets)
        vocab_prompt = load_prompt(cfg, 'text_vocab.md')
        scripts = build_scripts(
            cfg, client, message_id, bullets, vocab_prompt, manifest=manifest)

        if args.prepare_only:
            print(f'Preparation complete: {len(scripts)} bullet script(s); '
                  'TTS and email delivery skipped.')
            return 0

        print('Synthesizing speech...')
        opening_pcm = load_opening_pcm(cfg)
        instructions = {part: load_prompt(cfg, f'tts_{part}.md')
                        for part in ('original', 'vocab', 'translation')}
        audio_path = synthesize_study_audio(
            cfg, client, message_id, scripts, opening_pcm, instructions,
            manifest=manifest)
        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        print(f'Audio ready: {audio_path} ({size_mb:.1f} MB)')
    except Exception as e:
        if args.prepare_only:
            print(f'Audio preparation failed ({e}); email not sent.')
            return 1
        if args.require_audio:
            print(f'Audio generation failed ({e}); email not sent.')
            return 1
        print(f'Audio generation failed ({e}); sending text-only email.')

    print('Sending processed email...')
    send_email(cfg, service, subject, html_email, processed, audio_path)
    mark_processed(cfg, message_id, subject)
    print('Done!')
    return 0


if __name__ == '__main__':
    sys.exit(main())
