"""Versioned product-safety lexicon for deterministic downgrading."""

from __future__ import annotations

import unicodedata


LEXICON_VERSION = "2026-08-20.1"
ASSERTIVE_PHRASES = (
    "安全です", "安全である", "被害はありません", "被害なし", "避難してください", "避難せよ",
    "点検は不要", "点検不要", "立入可能", "使用可能",
)


def normalize_assertive_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    chars: list[str] = []
    for char in normalized:
        code = ord(char)
        chars.append(chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else char)
    return "".join(chars)


def contains_assertive_phrase(value: str) -> bool:
    normalized = normalize_assertive_text(value)
    return any(normalize_assertive_text(phrase) in normalized for phrase in ASSERTIVE_PHRASES)


def downgrade_assertive_sentences(value: str) -> tuple[str, bool]:
    """Append the fixed marker once to every matching sentence."""

    if not value:
        return value, False
    output: list[str] = []
    buffer = ""
    changed = False
    for char in value:
        buffer += char
        if char in "。！？!?\n":
            downgraded, matched = _downgrade_one(buffer)
            output.append(downgraded)
            changed = changed or matched
            buffer = ""
    if buffer:
        downgraded, matched = _downgrade_one(buffer)
        output.append(downgraded)
        changed = changed or matched
    return "".join(output), changed


def _downgrade_one(sentence: str) -> tuple[str, bool]:
    if not contains_assertive_phrase(sentence):
        return sentence, False
    marker = "（要確認）"
    if marker in sentence:
        return sentence, True
    terminator = sentence[-1] if sentence and sentence[-1] in "。！？!?\n" else ""
    body = sentence[:-1] if terminator else sentence
    return body + marker + terminator, True
