"""The end-to-end run: fetch, translate, synthesize, deliver.

This module pulls in the heavy dependencies (google.genai alone costs about a
second to import), so __main__ imports it only after argument parsing —
--version, --help, and argument errors must not pay for the pipeline.
"""

import logging
import os
import shutil

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


logger = logging.getLogger(__name__)


def run(args):
    try:
        cfg = load_auth_config() if args.command == 'auth' else load_config()
    except ConfigError as e:
        logger.error('Configuration error:')
        for error in e.errors:
            logger.error('- %s', error)
        logger.error('\nConfiguration file: %s', e.config_path)
        return 2
    if args.command != 'auth':
        prune_message_cache(cfg)

    if not os.path.exists(cfg.credentials_path):
        logger.error('credentials.json not found at %s', cfg.credentials_path)
        logger.error('Please follow the setup instructions in README to '
                     'download it from Google Cloud Console.')
        return 1

    if args.command == 'auth':
        logger.info('Authorizing Gmail...')
        try:
            get_gmail_credentials(cfg, interactive=True)
        except GmailAuthenticationError as e:
            logger.error('Gmail authentication failed: %s', e)
            return 1
        logger.info('Gmail authorization ready: %s', cfg.token_path)
        return 0

    logger.info('Authenticating with Gmail...')
    try:
        service = get_gmail_service(cfg)
    except GmailAuthenticationError as e:
        logger.error('Gmail authentication failed: %s', e)
        return 1

    unit = 'hour' if args.lookback_hours == 1 else 'hours'
    logger.info(
        'Searching for an Economist Espresso email within the last %s %s...',
        args.lookback_hours, unit)
    message = find_espresso_email(
        cfg, service, lookback_hours=args.lookback_hours)
    if not message:
        logger.info('No Economist Espresso email found. Exiting.')
        return 0

    message_id = message['id']
    if was_processed(cfg, message_id) and not args.force:
        logger.info(
            'This source email was already processed. Exiting. '
            '(Use --force to resend.)')
        return 0

    subject, sender, body = get_email_content(service, message['id'])
    logger.info('Found: "%s" from %s', subject, sender)

    if not body.strip():
        logger.error('Email body is empty. Exiting.')
        return 1

    skip_audio = False
    if not args.prepare_only and shutil.which('ffmpeg') is None:
        if args.require_audio:
            logger.error('Audio is required but ffmpeg was not found on PATH. '
                         'Install ffmpeg and try again.')
            return 1
        logger.warning(
            'ffmpeg was not found on PATH; skipping audio generation and '
            'sending a text-only email.')
        skip_audio = True

    client = make_gemini_client(cfg)
    manifest = GenerationManifest(cfg, message_id)

    logger.info('Processing with Gemini (translating + annotating vocabulary)...')
    translation_prompt = load_prompt(cfg, 'text_translation.md')
    processed = process_with_gemini(
        cfg, client, subject, body, translation_prompt, manifest=manifest)
    atomic_write(
        message_cache_path(cfg, message_id, 'processed.md'), processed)

    logger.info('Building HTML email...')
    html_email = build_html_email(cfg, processed)

    audio_path = None
    if not skip_audio:
        try:
            logger.info('Building per-bullet study scripts...')
            bullets = parse_top_stories(processed)
            validate_top_stories(bullets)
            vocab_prompt = load_prompt(cfg, 'text_vocab.md')
            scripts = build_scripts(
                cfg, client, message_id, bullets, vocab_prompt, manifest=manifest)

            if args.prepare_only:
                logger.info(
                    'Preparation complete: %s bullet script(s); '
                    'TTS and email delivery skipped.', len(scripts))
                return 0

            bullet_unit = 'bullet' if len(scripts) == 1 else 'bullets'
            logger.info(
                'Synthesizing audio for %s %s...', len(scripts), bullet_unit)
            opening_pcm = load_opening_pcm(cfg)
            instructions = {part: load_prompt(cfg, f'tts_{part}.md')
                            for part in ('original', 'vocab', 'translation')}
            audio_path = synthesize_study_audio(
                cfg, client, message_id, scripts, opening_pcm, instructions,
                manifest=manifest)
            size_mb = os.path.getsize(audio_path) / 1024 / 1024
            logger.info('Audio ready: %s (%.1f MB)', audio_path, size_mb)
        except Exception as e:
            if args.prepare_only:
                logger.error('Audio preparation failed (%s); email not sent.', e)
                return 1
            if args.require_audio:
                logger.error('Audio generation failed (%s); email not sent.', e)
                return 1
            logger.warning(
                'Audio generation failed (%s); sending text-only email.', e)

    logger.info('Sending processed email...')
    send_email(cfg, service, subject, html_email, processed, audio_path)
    mark_processed(cfg, message_id, subject)
    logger.info('Done!')
    return 0
