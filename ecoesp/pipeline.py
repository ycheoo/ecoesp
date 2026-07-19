"""The end-to-end run: fetch, translate, synthesize, deliver.

This module pulls in the heavy dependencies (google.genai alone costs about a
second to import), so __main__ imports it only after argument parsing —
--version, --help, and argument errors must not pay for the pipeline.
"""

import os
import shutil
import sys

from .config import ConfigError, load_auth_config, load_config
from .storage.delivery_state import mark_processed, was_processed
from .clients.gemini import make_gemini_client
from .clients.gmail_client import (
    GmailAuthenticationError,
    find_espresso_email,
    get_email_content,
    get_gmail_credentials,
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


def run(args):
    try:
        cfg = load_auth_config() if args.command == 'auth' else load_config()
    except ConfigError as e:
        print('Configuration error:', file=sys.stderr)
        for error in e.errors:
            print(f'- {error}', file=sys.stderr)
        print(f'\nConfiguration file: {e.config_path}', file=sys.stderr)
        return 2
    if args.command != 'auth':
        prune_message_cache(cfg)

    if not os.path.exists(cfg.credentials_path):
        print(f'ERROR: credentials.json not found at {cfg.credentials_path}')
        print('Please follow the setup instructions in README to download it from Google Cloud Console.')
        return 1

    if args.command == 'auth':
        print('Authorizing Gmail...', flush=True)
        try:
            get_gmail_credentials(cfg, interactive=True)
        except GmailAuthenticationError as e:
            print(f'Gmail authentication failed: {e}', file=sys.stderr)
            return 1
        print(f'Gmail authorization ready: {cfg.token_path}')
        return 0

    print('Authenticating with Gmail...', flush=True)
    try:
        service = get_gmail_service(cfg)
    except GmailAuthenticationError as e:
        print(f'Gmail authentication failed: {e}', file=sys.stderr)
        return 1

    unit = 'hour' if args.lookback_hours == 1 else 'hours'
    print(f'Searching for an Economist Espresso email within the last '
          f'{args.lookback_hours} {unit}...')
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

    skip_audio = False
    if not args.prepare_only and shutil.which('ffmpeg') is None:
        if args.require_audio:
            print('Audio is required but ffmpeg was not found on PATH. '
                  'Install ffmpeg and try again.', file=sys.stderr)
            return 1
        print('ffmpeg was not found on PATH; skipping audio generation and '
              'sending a text-only email.')
        skip_audio = True

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
    if not skip_audio:
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
