"""Shared fixture helpers: a fake ``requests`` session driven by recorded Metabase JSON.

Lets the *real* :class:`MetabaseClient` run end-to-end (auth, ``legacy-mbql`` pin,
pagination, backoff) with no live Metabase — the whole extract pipeline is exercised
against ``tests/resources/metabase/recorded.json``.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

RECORDED_PATH = Path(__file__).parents[2] / "resources" / "metabase" / "recorded.json"


def load_recorded() -> dict[str, Any]:
    return json.loads(RECORDED_PATH.read_text(encoding="utf-8"))


def build_recorded(
    *,
    cards: list[dict] | None = None,
    dashboards: list[dict] | None = None,
    dashboard_details: dict[str, dict] | None = None,
    database_metadata: dict[str, dict] | None = None,
    snippets: list[dict] | None = None,
    session_properties: dict | None = None,
) -> dict[str, Any]:
    """Assemble a full recorded-payload dict from inline parts, defaulting the rest.

    Every key :class:`FakeSession` may index is filled so a purpose-built corpus (a
    foreign-database card, a bespoke incremental scenario) can be constructed inline
    without mutating the shared ``recorded.json`` (which ``test_client.py`` depends on).
    """
    return {
        "cards": list(cards or []),
        "dashboards": list(dashboards or []),
        "dashboard_details": dict(dashboard_details or {}),
        "database_metadata": dict(database_metadata or {}),
        "snippets": list(snippets or []),
        "session_properties": session_properties or {"version": {"tag": "v0.0.0-test"}},
    }


class FakeResponse:
    def __init__(self, body: Any, status_code: int = 200, headers: dict | None = None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


_DASHBOARD_ID_RE = re.compile(r"/api/dashboard/(\d+)$")
_DB_META_RE = re.compile(r"/api/database/(\d+)/metadata$")


class FakeSession:
    """Routes ``requests``-style calls to recorded JSON.

    ``get_calls`` records ``(url, params)`` so tests can assert the ``legacy-mbql`` pin and
    pagination behaviour. Pass ``fail_first`` to simulate a transient 503 then success
    (exercises the client's retry/backoff).

    Thread-safe: ``MetabaseClient.get_dashboards`` fans ``get`` out across a
    :class:`~concurrent.futures.ThreadPoolExecutor`, so multiple threads may call
    :meth:`get`/:meth:`post` concurrently. ``recorded`` is treated as read-only during a
    run; the only mutable shared state (the call logs and the failure counter) is guarded by
    a lock so the recorded ``(url, params)`` list never races or drops an entry.
    """

    def __init__(self, recorded: dict[str, Any], fail_first: int = 0):
        self.recorded = recorded
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []
        self._remaining_failures = fail_first
        self._lock = threading.Lock()

    def post(self, url: str, json: Any = None, timeout: int = 30, **kwargs: Any) -> FakeResponse:
        with self._lock:
            self.post_calls.append((url, json))
        if url.endswith("/api/session"):
            return FakeResponse({"id": "fake-session-token"})
        return FakeResponse({}, status_code=404)

    def get(
        self,
        url: str,
        headers: Any = None,
        params: Any = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> FakeResponse:
        with self._lock:
            self.get_calls.append((url, dict(params or {})))
            fail = self._remaining_failures > 0
            if fail:
                self._remaining_failures -= 1
        if fail:
            return FakeResponse({}, status_code=503, headers={"Retry-After": "0"})

        offset = int((params or {}).get("offset", 0))

        if url.endswith("/api/card"):
            return FakeResponse([] if offset else list(self.recorded["cards"]))
        if url.endswith("/api/dashboard"):
            return FakeResponse([] if offset else list(self.recorded["dashboards"]))
        if url.endswith("/api/native-query-snippet"):
            return FakeResponse([] if offset else list(self.recorded["snippets"]))
        if url.endswith("/api/session/properties"):
            return FakeResponse(self.recorded["session_properties"])

        dash = _DASHBOARD_ID_RE.search(url)
        if dash:
            return FakeResponse(self.recorded["dashboard_details"][dash.group(1)])
        meta = _DB_META_RE.search(url)
        if meta:
            return FakeResponse(self.recorded["database_metadata"][meta.group(1)])

        return FakeResponse({}, status_code=404)
