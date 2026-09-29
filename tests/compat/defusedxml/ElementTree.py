"""Fail-closed subset used by ingest-file-sanitizer tests."""

from xml.etree import ElementTree as _stdlib_et


class DefusedXmlException(ValueError):
    pass


def fromstring(text, *, forbid_entities=True, forbid_external=True):
    raw = text if isinstance(text, bytes) else str(text).encode("utf-8")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"SYSTEM" in upper or b"PUBLIC" in upper:
        raise DefusedXmlException("unsafe XML declaration")
    return _stdlib_et.fromstring(raw)
