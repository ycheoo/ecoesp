#!/usr/bin/env python3
"""
Economist Espresso Translator
Reads a matching Economist Espresso email, translates it with vocabulary annotations,
synthesizes a bilingual audio version, and sends both back to the inbox.
"""

import argparse
import logging
import sys

from . import __version__
from .config import APP_NAME


class _CurrentStreamHandler(logging.StreamHandler):
    """Write each record to the current sys stream, including test redirects."""

    def __init__(self, stream_name):
        super().__init__()
        self._stream_name = stream_name

    def emit(self, record):
        self.stream = getattr(sys, self._stream_name)
        super().emit(record)


def _configure_logging(verbose=False):
    """Configure application logs without enabling noisy dependency logs.

    Progress (INFO, and DEBUG under --verbose) goes to stdout; problems
    (WARNING and up) go to stderr — one rule for every message, so shell
    redirection separates them the conventional way."""
    logger = logging.getLogger(APP_NAME)
    logger.handlers.clear()
    stdout_handler = _CurrentStreamHandler('stdout')
    stdout_handler.addFilter(lambda record: record.levelno < logging.WARNING)
    stderr_handler = _CurrentStreamHandler('stderr')
    stderr_handler.setLevel(logging.WARNING)
    for handler in (stdout_handler, stderr_handler):
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False


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
        prog=APP_NAME,
        description='Process an Economist Espresso email.')
    # Subcommands are what a reader scans the help for first. Declaring them
    # without a title keeps them in the parser's positional group — the one
    # section argparse prints before the options — retitled, since argparse
    # would otherwise head it "positional arguments".
    parser._positionals.title = 'commands'
    commands = parser.add_subparsers(dest='command')
    commands.add_parser(
        'auth',
        help='Authorize Gmail and write token.pickle, then exit.',
        description='Authorize Gmail and write token.pickle without running '
                    'the email pipeline.',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Show per-segment generation details.')
    parser.add_argument(
        '-V', '--version', action='version', version=f'{APP_NAME} {__version__}')
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
        '--lookback-hours', type=_positive_int, default=24, metavar='N',
        help='Search for source emails received within this many hours '
             '(default: 24).')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    _configure_logging(args.verbose)
    # Deferred until after parsing: the pipeline's imports cost about a
    # second, which --version, --help, and argument errors must not pay.
    from .pipeline import run
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
