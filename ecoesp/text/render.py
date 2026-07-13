"""Render the processed markdown into the HTML email body."""

import os

import markdown


def build_html_email(cfg, processed_markdown):
    html_body = markdown.markdown(
        processed_markdown,
        extensions=['extra', 'nl2br'],
    )
    path = os.path.join(cfg.script_dir, 'template', 'email', 'email.html')
    with open(path, encoding='utf-8') as f:
        template = f.read()
    return template.replace('{body}', html_body)
