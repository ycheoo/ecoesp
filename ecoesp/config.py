"""Configuration loading for the Economist Espresso translator."""

from dataclasses import dataclass
from email.utils import parseaddr
import json
import math
import os
import re

from dotenv import load_dotenv

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
]

# The shipped prompts and email template are package data: the app cannot run
# without them, so they travel inside the package and resolve from its own
# location — which holds for a source checkout, a pip install, and a frozen
# binary alike. Users override them from their config dir rather than editing
# these, so nothing here needs to be writable.
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'template')
DEFAULT_TEXT_MODELS = [
    'gemini-3.8-flash',
    'gemini-3.7-flash',
    'gemini-3.6-flash',
]
DEFAULT_TTS_MODELS = ['gemini-3.1-flash-tts-preview', 'gemini-2.5-flash-preview-tts']

# The app's runtime identity (XDG directory leaf and env-var prefix) follows the
# top-level package name, so ~/.config/ecoesp, ECOESP_CONFIG_DIR, and their
# state/cache/data siblings all come from here. The fallback matters only if
# __package__ is somehow unavailable.
APP_NAME = (__package__ or 'ecoesp').split('.')[0]

# The spoken parts a track may arrange. 'original' and 'translation' come free
# from the main translation; 'vocab' costs a separate Gemini call, generated
# only when some track uses it.
AUDIO_PARTS = ('original', 'vocab', 'translation')
DEFAULT_SEGMENT_GAP_SECONDS = 0.8
DEFAULT_BULLET_GAP_SECONDS = 1.2
MAX_AUDIO_GAP_SECONDS = 60.0

# Written to <config dir>/audio.json on first run. Each track becomes one MP3:
# 'segments' is the per-bullet playback order, and the optional 'opening',
# 'segment_gap_seconds', and 'bullet_gap_seconds' fall back to the defaults
# above when omitted.
DEFAULT_AUDIO_CONFIG = {
    'tracks': [
        {
            'name': 'study',
            'segments': ['original', 'vocab', 'original', 'translation', 'original'],
        }
    ]
}


class ConfigError(ValueError):
    def __init__(self, errors, config_path):
        self.errors = errors
        self.config_path = config_path
        super().__init__('; '.join(errors))


@dataclass(frozen=True)
class AuthConfig:
    app_config_dir: str
    app_state_dir: str
    token_path: str
    credentials_path: str


@dataclass(frozen=True)
class AudioTrack:
    """One rendered MP3: a per-bullet segment order with its own pacing."""
    name: str
    segments: tuple[str, ...]
    opening: bool
    segment_gap_seconds: float
    bullet_gap_seconds: float


@dataclass(frozen=True)
class Config:
    template_dir: str
    app_config_dir: str
    app_state_dir: str
    app_cache_dir: str
    app_data_dir: str
    gemini_api_keys: tuple[str, ...]
    reader_email: str
    dest_email: str
    gmail_query: str
    token_path: str
    credentials_path: str
    processed_messages_path: str
    text_models: list[str]
    tts_models: list[str]
    tts_voice: str
    gemini_timeout_ms: int
    audio_tracks: tuple[AudioTrack, ...]
    subject_prefix: str


def _xdg_dir(env_name, default):
    return os.environ.get(env_name, os.path.join(os.path.expanduser('~'), default))


def _models(name, default, errors):
    value = os.environ.get(name)
    if value is None:
        return list(default)
    models = [item.strip() for item in value.split(',') if item.strip()]
    if not models:
        errors.append(f'{name} must contain at least one model name')
    return models


def _gemini_api_keys(errors):
    """One or more comma-separated API keys in scheduling order."""
    raw = _required('GEMINI_API_KEY', errors)
    keys = [key.strip() for key in raw.split(',') if key.strip()]
    if raw and not keys:
        errors.append('GEMINI_API_KEY must contain at least one API key')
    if len(set(keys)) != len(keys):
        errors.append('GEMINI_API_KEY must not contain duplicate keys')
    return tuple(dict.fromkeys(keys))


def _required(name, errors):
    value = os.environ.get(name, '').strip()
    if not value:
        errors.append(f'{name} is required')
    return value


def _email(name, errors):
    value = _required(name, errors)
    if value:
        _, address = parseaddr(value)
        if address != value or '@' not in address or address.startswith('@') or address.endswith('@'):
            errors.append(f'{name} must be a plain email address, got {value!r}')
    return value


def _positive_int(name, default, errors):
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        errors.append(f'{name} must be a positive integer, got {raw!r}')
        return default
    if value <= 0:
        errors.append(f'{name} must be a positive integer, got {raw!r}')
    return value


def _track_seconds(raw, key, label, errors):
    """A per-track duration in seconds, defaulting when the field is absent.
    Zero is allowed: it turns the gap off. Values are capped to prevent a typo
    from allocating an unbounded silence buffer during assembly."""
    default = (DEFAULT_SEGMENT_GAP_SECONDS if key == 'segment_gap_seconds'
               else DEFAULT_BULLET_GAP_SECONDS)
    if key not in raw:
        return default
    value = raw[key]
    if (isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= MAX_AUDIO_GAP_SECONDS
            or not math.isfinite(value)):
        errors.append(
            f'{label} "{key}" must be a finite number between 0 and '
            f'{MAX_AUDIO_GAP_SECONDS:g} seconds')
        return default
    return float(value)


def _parse_audio_tracks(data, config_path, errors):
    """Validate the parsed audio.json into a tuple of AudioTrack, collecting
    every problem into errors rather than stopping at the first."""
    if not isinstance(data, dict) or not isinstance(data.get('tracks'), list):
        errors.append('audio.json must be an object with a "tracks" array')
        return ()
    if not data['tracks']:
        errors.append('audio.json must define at least one track')
        return ()

    tracks = []
    seen = set()
    for i, raw in enumerate(data['tracks']):
        label = f'audio.json tracks[{i}]'
        if not isinstance(raw, dict):
            errors.append(f'{label} must be an object')
            continue

        name = raw.get('name')
        if not isinstance(name, str) or not name.strip():
            errors.append(f'{label} needs a non-empty "name"')
            name = None
        else:
            name = name.strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                errors.append(
                    f'{label} "name" may contain only ASCII letters, digits, '
                    'hyphens, and underscores')
                name = None
            elif name in seen:
                errors.append(f'audio.json has a duplicate track name {name!r}')
            if name is not None:
                seen.add(name)

        segments = raw.get('segments')
        if not isinstance(segments, list) or not segments:
            errors.append(f'{label} needs a non-empty "segments" list')
            segments = None
        else:
            invalid = [s for s in segments if s not in AUDIO_PARTS]
            if invalid:
                errors.append(
                    f'{label} has invalid segment(s) {invalid}; valid parts '
                    f'are {list(AUDIO_PARTS)}')
                segments = None

        opening = raw.get('opening', True)
        if not isinstance(opening, bool):
            errors.append(f'{label} "opening" must be true or false')
            opening = True

        segment_gap = _track_seconds(raw, 'segment_gap_seconds', label, errors)
        bullet_gap = _track_seconds(raw, 'bullet_gap_seconds', label, errors)

        if name is not None and segments is not None:
            tracks.append(AudioTrack(
                name=name, segments=tuple(segments), opening=opening,
                segment_gap_seconds=segment_gap, bullet_gap_seconds=bullet_gap))
    return tuple(tracks)


def _load_audio_tracks(app_config_dir, errors):
    """Read <config dir>/audio.json, materializing the default on first run.

    The file is a settings file the user is meant to edit, so unlike the
    prompt/email overrides it is written out once (never overwritten) rather
    than shadowed from a package default. A present-but-broken file is reported,
    not clobbered."""
    path = os.path.join(app_config_dir, 'audio.json')
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_AUDIO_CONFIG, f, indent=2, ensure_ascii=False)
            f.write('\n')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f'audio.json could not be read ({e})')
        return ()
    return _parse_audio_tracks(data, path, errors)


def _load_base_paths():
    app_config_dir = os.environ.get(
        f'{APP_NAME.upper()}_CONFIG_DIR',
        os.path.join(_xdg_dir('XDG_CONFIG_HOME', '.config'), APP_NAME),
    )
    # Create the config dir up front — before validation can fail — so a first
    # run has somewhere to drop the .env the error below tells the user to
    # create; otherwise that path never comes into existence.
    os.makedirs(app_config_dir, exist_ok=True)
    config_path = os.path.join(app_config_dir, '.env')
    load_dotenv(config_path)

    app_state_dir = os.environ.get(
        f'{APP_NAME.upper()}_STATE_DIR',
        os.path.join(_xdg_dir('XDG_STATE_HOME', '.local/state'), APP_NAME),
    )
    return app_config_dir, app_state_dir, config_path


def _credentials_path(app_config_dir, errors):
    path = os.environ.get(
        'GOOGLE_CREDENTIALS_PATH',
        os.path.join(app_config_dir, 'credentials.json'),
    ).strip()
    if not path:
        errors.append('GOOGLE_CREDENTIALS_PATH must not be empty')
    return path


def load_auth_config():
    """Load only the paths needed to authorize Gmail.

    The dedicated `auth` command must work before Gemini and delivery settings
    exist, so it deliberately does not validate the main pipeline's environment.
    """
    app_config_dir, app_state_dir, config_path = _load_base_paths()
    errors = []
    credentials_path = _credentials_path(app_config_dir, errors)
    if errors:
        raise ConfigError(errors, config_path)

    os.makedirs(app_state_dir, exist_ok=True)
    return AuthConfig(
        app_config_dir=app_config_dir,
        app_state_dir=app_state_dir,
        token_path=os.path.join(app_state_dir, 'token.pickle'),
        credentials_path=credentials_path,
    )


def load_config():
    app_config_dir, app_state_dir, config_path = _load_base_paths()
    app_cache_dir = os.environ.get(
        f'{APP_NAME.upper()}_CACHE_DIR',
        os.path.join(_xdg_dir('XDG_CACHE_HOME', '.cache'), APP_NAME),
    )
    # User-supplied assets the app plays back rather than settings it reads —
    # currently the optional opening jingle — so they belong in the XDG data
    # directory, not alongside the .env in the config directory.
    app_data_dir = os.environ.get(
        f'{APP_NAME.upper()}_DATA_DIR',
        os.path.join(_xdg_dir('XDG_DATA_HOME', '.local/share'), APP_NAME),
    )

    errors = []
    gemini_api_keys = _gemini_api_keys(errors)
    reader_email = _email('READER_EMAIL', errors)
    dest_email = _email('DEST_EMAIL', errors)
    gmail_query = _required('GMAIL_QUERY', errors)
    text_models = _models('TEXT_MODELS', DEFAULT_TEXT_MODELS, errors)
    tts_models = _models('TTS_MODELS', DEFAULT_TTS_MODELS, errors)
    tts_voice = os.environ.get('TTS_VOICE', 'Kore').strip()
    if not tts_voice:
        errors.append('TTS_VOICE must not be empty')
    gemini_timeout_ms = _positive_int('GEMINI_TIMEOUT_MS', 180000, errors)
    # The audio tracks — segment order, pacing, and opening per rendered MP3 —
    # live in audio.json, generated on first run and edited by the user.
    audio_tracks = _load_audio_tracks(app_config_dir, errors)
    # Prepended to the source subject on the email we send back. Empty sends the
    # subject through unchanged.
    subject_prefix = os.environ.get('SUBJECT_PREFIX', '[译]').strip()

    credentials_path = _credentials_path(app_config_dir, errors)

    if errors:
        raise ConfigError(errors, config_path)

    # The config dir was already created above; the rest are only needed once
    # the config is valid and the run actually proceeds.
    for directory in (app_state_dir, app_cache_dir, app_data_dir):
        os.makedirs(directory, exist_ok=True)

    return Config(
        template_dir=TEMPLATE_DIR,
        app_config_dir=app_config_dir,
        app_state_dir=app_state_dir,
        app_cache_dir=app_cache_dir,
        app_data_dir=app_data_dir,
        gemini_api_keys=gemini_api_keys,
        reader_email=reader_email,
        dest_email=dest_email,
        gmail_query=gmail_query,
        token_path=os.path.join(app_state_dir, 'token.pickle'),
        credentials_path=credentials_path,
        processed_messages_path=os.path.join(app_state_dir, 'processed_messages.json'),
        text_models=text_models,
        tts_models=tts_models,
        tts_voice=tts_voice,
        gemini_timeout_ms=gemini_timeout_ms,
        audio_tracks=audio_tracks,
        subject_prefix=subject_prefix,
    )
