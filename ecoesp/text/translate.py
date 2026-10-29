"""Gemini text-generation task for translating and annotating the newsletter."""

import os

from ..clients.gemini import _require_text, gemini_generate
from ..storage.files import resolve_asset


def load_prompt(cfg, name):
    """A prompt, preferring the user's <config dir>/prompts/<name> override."""
    path = resolve_asset(
        os.path.join(cfg.app_config_dir, 'prompts', name),
        os.path.join(cfg.template_dir, 'prompts', name))
    with open(path, encoding='utf-8') as f:
        return f.read()


def process_with_gemini(cfg, client, subject, body, prompt_template, manifest=None):
    prompt = prompt_template.format(subject=subject, body=body)
    text, model_index = gemini_generate(
        client, cfg.text_models, prompt, extract=_require_text)
    if manifest:
        # main writes this translation to processed.md in the message cache dir.
        manifest.record('translation', cfg.text_models[model_index],
                        client.active_key(), 'processed.md')
    return text
