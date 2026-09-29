"""Server-issued envelope indirection over the vendored scoped payload store."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from src.payload_store.payload_store import PayloadStore, PayloadStoreError, is_reference


class ScopedPayloadBroker:
    """Keep the verified scope beside server-minted envelope references.

    Ordinary report/note resolution still requires an explicit scope.  The
    envelope-only method exists because scope itself is inside the sealed
    internal envelope and therefore cannot be supplied before it is opened.
    The session binding and the underlying store's scope comparison remain in
    force.
    """

    def __init__(self, store: PayloadStore) -> None:
        self._store = store
        self._envelope_scopes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def put(
        self,
        value: Any,
        *,
        scope: Mapping[str, str],
        session_id: str,
        envelope: bool = False,
    ) -> str:
        ref = self._store.put(value, scope=scope, session_id=session_id)
        if envelope:
            with self._lock:
                self._envelope_scopes[ref] = dict(scope)
        return ref

    def resolve(
        self,
        ref: str | None,
        *,
        scope: Mapping[str, str],
        session_id: str,
        consume: bool = False,
    ) -> Any:
        return self._store.resolve(ref, scope=scope, session_id=session_id, consume=consume)

    def resolve_envelope(self, ref: str | None, *, session_id: str, consume: bool = True) -> Any:
        if not is_reference(ref):
            raise PayloadStoreError("payload_ref_unknown")
        with self._lock:
            scope = self._envelope_scopes.get(ref)
        if scope is None:
            raise PayloadStoreError("payload_ref_unknown")
        value = self._store.resolve(ref, scope=scope, session_id=session_id, consume=consume)
        if consume:
            with self._lock:
                self._envelope_scopes.pop(ref, None)
        return value

    def delete(self, ref: str | None, *, scope: Mapping[str, str], session_id: str) -> None:
        self._store.delete(ref, scope=scope, session_id=session_id)
        if isinstance(ref, str):
            with self._lock:
                self._envelope_scopes.pop(ref, None)

    def envelope_count(self) -> int:
        """Testing/operations metric; never exposes stored values."""

        with self._lock:
            return len(self._envelope_scopes)
