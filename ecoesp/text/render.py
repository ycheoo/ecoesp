"""Render the processed markdown into the HTML email body."""

import os

import markdown

from ..storage.files import resolve_asset


def build_html_email(cfg, processed_markdown):
    html_body = markdown.markdown(
        processed_markdown,
        extensions=['extra', 'nl2br'],
    )
    # The user's <config dir>/email.html wins over the shipped template.
    path = resolve_asset(
        os.path.join(cfg.app_config_dir, 'email.html'),
        os.path.join(cfg.template_dir, 'email', 'email.html'))
    with open(path, encoding='utf-8') as f:
        template = f.read()
    return template.replace('{body}', html_body)
