"""NFKC numeric extraction without Unicode-word-boundary bypasses."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


UNIT_TERMS = ("cm", "mm", "棟", "件", "名", "人", "時", "分", "秒", "円", "m", "%", "％", "階")
_UNIT_PATTERN = "|".join(re.escape(unit) for unit in UNIT_TERMS)
_ASCII = re.compile(rf"[0-9]+(?:\.[0-9]+)?(?P<unit>{_UNIT_PATTERN})?")
_KANJI_RUN = re.compile(r"[〇一二三四五六七八九十百千万億]+")
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}


@dataclass(frozen=True)
class NumericValue:
    value: str
    unit: str | None


def extract_numeric_values(text: str) -> tuple[list[NumericValue], list[str]]:
    """Return parsed values and unsupported numeric expressions."""

    normalized = unicodedata.normalize("NFKC", text)
    values = [
        NumericValue(_canonical_decimal(match.group(0)[: -len(match.group("unit"))] if match.group("unit") else match.group(0)), _unit(match.group("unit")))
        for match in _ASCII.finditer(normalized)
    ]
    unsupported: list[str] = []
    for match in _KANJI_RUN.finditer(normalized):
        expression = match.group(0)
        unit_match = re.match(_UNIT_PATTERN, normalized[match.end() :])
        unit = _unit(unit_match.group(0)) if unit_match else None
        try:
            parsed = parse_kanji_number(expression)
        except ValueError:
            unsupported.append(expression)
        else:
            values.append(NumericValue(str(parsed), unit))
    return values, unsupported


def parse_kanji_number(text: str) -> int:
    if not text or "億" in text or any(char not in _KANJI_DIGITS and char not in _SMALL_UNITS and char != "万" for char in text):
        raise ValueError("unsupported kanji number")
    if text == "〇":
        return 0
    if "〇" in text or re.search(r"[〇一二三四五六七八九]{2,}", text):
        raise ValueError("positional kanji digits are not supported")
    if text.count("万") > 1:
        raise ValueError("unsupported kanji number")
    if "万" in text:
        high, low = text.split("万", 1)
        high_value = _parse_under_10000(high or "一")
        low_value = _parse_under_10000(low) if low else 0
        value = high_value * 10_000 + low_value
    else:
        value = _parse_under_10000(text)
    if not 0 <= value < 100_000_000:
        raise ValueError("kanji number outside supported range")
    return value


def _parse_under_10000(text: str) -> int:
    if not text:
        return 0
    total = 0
    pending: int | None = None
    last_unit = 10_000
    for char in text:
        if char in _KANJI_DIGITS:
            if pending is not None:
                raise ValueError("positional kanji digits are not supported")
            pending = _KANJI_DIGITS[char]
            continue
        unit = _SMALL_UNITS.get(char)
        if unit is None or unit >= last_unit:
            raise ValueError("invalid kanji unit order")
        total += (1 if pending is None else pending) * unit
        pending = None
        last_unit = unit
    return total + (pending or 0)


def _canonical_decimal(value: str) -> str:
    if "." in value:
        return format(float(value), "g")
    return str(int(value))


def _unit(value: str | None) -> str | None:
    return "%" if value == "％" else value
