"""Configuration loading for the Economist Espresso translator."""

from dataclasses import dataclass
from email.utils import parseaddr
import os

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
DEFAULT_TEXT_MODELS = ['gemini-3.5-flash', 'gemini-3-flash-preview', 'gemini-2.5-flash']
DEFAULT_TTS_MODELS = ['gemini-3.1-flash-tts-preview', 'gemini-2.5-flash-preview-tts']

# The app's runtime identity (XDG directory leaf and env-var prefix) follows the
# top-level package name. Renaming the package — as publish.sh does to produce
# the public `ecoesp` fork — therefore gives it its own config/state/cache
# namespace automatically, with no source edits.
APP_NAME = (__package__ or 'ecoesp').split('.')[0]


class ConfigError(ValueError):
    def __init__(self, errors, config_path):
        self.errors = errors
        self.config_path = config_path
        super().__init__('; '.join(errors))


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
    segment_gap_seconds: float
    bullet_gap_seconds: float
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


def _seconds(name, default, errors):
    """A duration in seconds. Zero is allowed: it turns the gap off."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        errors.append(f'{name} must be a non-negative number, got {raw!r}')
        return default
    if value < 0:
        errors.append(f'{name} must be a non-negative number, got {raw!r}')
    return value


def load_config():
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
    # Silence between spoken segments inside one bullet, and the longer silence
    # between one bullet and the next. Tune after listening.
    segment_gap_seconds = _seconds('SEGMENT_GAP_SECONDS', 0.8, errors)
    bullet_gap_seconds = _seconds('BULLET_GAP_SECONDS', 1.2, errors)
    # Prepended to the source subject on the email we send back. Empty sends the
    # subject through unchanged.
    subject_prefix = os.environ.get('SUBJECT_PREFIX', '[译]').strip()

    credentials_path = os.environ.get(
        'GOOGLE_CREDENTIALS_PATH',
        os.path.join(app_config_dir, 'credentials.json'),
    ).strip()
    if not credentials_path:
        errors.append('GOOGLE_CREDENTIALS_PATH must not be empty')

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
        segment_gap_seconds=segment_gap_seconds,
        bullet_gap_seconds=bullet_gap_seconds,
        subject_prefix=subject_prefix,
    )
