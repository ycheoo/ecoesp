"""Parse the processed study markdown into per-bullet study items.

Only section 一 (Today's Top Stories) is used for the audio pipeline. The
markdown is code-generated with a fixed shape, so the split is purely
structural — no LLM needed for the English original or the Chinese translation;
only the vocabulary block is later reshaped for speech.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Bullet:
    """One Today's-Top-Stories item, split into its three parts."""
    original: str      # English original, ready to narrate as-is
    translation: str   # Chinese translation, ready to narrate as-is
    vocab_raw: str     # Raw 生词注释 markdown, still needs audio_vocab reshaping


def _clean(text):
    """Trim and drop the leading ▸ bullet glyph so TTS never voices it."""
    return re.sub(r'^[▸►•]\s*', '', text.strip()).strip()


def _strip_trailing_hr(text):
    """Remove a trailing markdown horizontal rule (the --- between bullets)."""
    return re.sub(r'\n?-{3,}\s*$', '', text.strip()).strip()


def parse_top_stories(markdown):
    """Return the section 一 bullets, in document order.

    Returns an empty list when the section is absent. A bullet with a missing
    translation or vocabulary block still parses, with those fields empty.
    """
    section = re.search(
        r'^##\s*一[^\n]*\n(.*?)(?=^##\s|\Z)', markdown, re.M | re.S)
    if not section:
        return []

    bullets = []
    # Each bullet begins at a '#### 原文' header; text before the first is preamble.
    for chunk in re.split(r'^####\s*原文\s*$', section.group(1), flags=re.M)[1:]:
        after_zh = re.split(r'^####\s*中文翻译\s*$', chunk, maxsplit=1, flags=re.M)
        original = after_zh[0]
        remainder = after_zh[1] if len(after_zh) > 1 else ''
        after_vocab = re.split(r'^####\s*生词注释\s*$', remainder, maxsplit=1, flags=re.M)
        translation = after_vocab[0]
        vocab = after_vocab[1] if len(after_vocab) > 1 else ''
        bullets.append(Bullet(
            original=_clean(original),
            translation=_clean(translation),
            vocab_raw=_strip_trailing_hr(vocab),
        ))
    return bullets


def validate_top_stories(bullets):
    """Reject incomplete parsed bullets before vocabulary or TTS API calls."""
    if not bullets:
        raise ValueError("No Today's Top Stories bullets found in processed markdown")

    errors = []
    for index, bullet in enumerate(bullets, start=1):
        missing = [
            name for name in ('original', 'translation', 'vocab_raw')
            if not getattr(bullet, name).strip()
        ]
        if missing:
            errors.append(f'bullet {index} missing {", ".join(missing)}')
    if errors:
        raise ValueError('Incomplete Top Stories: ' + '; '.join(errors))
