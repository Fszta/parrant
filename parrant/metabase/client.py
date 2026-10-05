"""— the Metabase API client.

**This is the ONLY module in the package that holds credentials or does network I/O.**
The offline gate/artifact path imports :mod:`artifact` (and later the reach index) and
never this module, so credentials are structurally confined to the credentialed
``metabase-extract`` step.

Testability: the client accepts an injected ``session`` (any object with ``requests``-style
``get``/``post`` returning a response with ``status_code`` / ``headers`` / ``json()`` /
``raise_for_status()``). Unit tests inject a fake session backed by recorded JSON, so the
resolvers and extract pipeline run end-to-end with no live Metabase.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any

try:  # ``requests`` is a runtime dependency; import lazily so importing the type/schema
    import requests  # modules never forces it (defensive — mirrors the offline guardrail).
except Exception:  # pragma: no cover - requests is declared in pyproject dependencies
    requests = None  # type: ignore[assignment]

# Metabase serves MBQL 4 (the stable ``["field", <id>, <opts>]`` shape) when this query
# param is set; without it, v0.57+ returns MBQL 5 / pMBQL which the MBQL resolver does not
# read. Pinned in exactly one place.
LEGACY_MBQL_PARAM = {"legacy-mbql": "true"}

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Runaway backstop for the (rarely-used) envelope pagination path: even if a deployment
# ignores ``offset`` and re-serves the same page, iteration is bounded rather than infinite.
_MAX_PAGES = 10_000


class MetabaseAuthError(Exception):
    """No usable credential (neither an API key nor username+password) was supplied,
    or session authentication was rejected by Metabase."""


class MetabaseAPIError(Exception):
    """A Metabase request failed after exhausting retries, or returned a non-2xx status."""


class MetabaseClient:
    """A thin, retrying Metabase REST client.

    Auth precedence: an API key (``x-api-key`` header, Metabase v0.49+) is preferred; else
    username + password session auth (``POST /api/session`` → ``X-Metabase-Session``).
    Credentials are used only to build request headers — they are never returned to callers
    and never persisted into the artifact.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session: Any = None,
        timeout: int = 30,
        max_retries: int = 5,
        page_size: int = 200,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._username = username
        self._password = password
        self._session = session if session is not None else requests
        if self._session is None:  # pragma: no cover - defensive
            raise MetabaseAPIError("No HTTP session available (requests failed to import).")
        self.timeout = timeout
        self.max_retries = max_retries
        self.page_size = page_size
        self._sleep = sleep
        self._session_token: str | None = None
        self._authenticated = False

    # --- auth -------------------------------------------------------------
    def _ensure_auth(self) -> None:
        """Resolve credentials into request headers once, lazily on first use."""
        if self._authenticated:
            return
        if self._api_key:
            self._authenticated = True
            return
        if self._username and self._password:
            resp = self._session.post(
                f"{self.base_url}/api/session",
                json={"username": self._username, "password": self._password},
                timeout=self.timeout,
            )
            if getattr(resp, "status_code", 200) >= 400:
                raise MetabaseAuthError(
                    f"Metabase session auth failed (status {resp.status_code})."
                )
            token = (resp.json() or {}).get("id")
            if not token:
                raise MetabaseAuthError("Metabase session auth returned no session id.")
            self._session_token = token
            self._authenticated = True
            return
        raise MetabaseAuthError(
            "No Metabase credentials: provide --metabase-api-key (preferred) or "
            "--metabase-username/--metabase-password."
        )

    def ensure_auth(self) -> None:
        """Public entry point to resolve credentials into a session token eagerly.

        Callers warm the session token once on the main thread (``ensure_auth()``) before
        fanning out to worker threads, so concurrent workers never race on the lazy
        ``_ensure_auth`` and duplicate the ``POST /api/session``."""
        self._ensure_auth()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        elif self._session_token:
            headers["X-Metabase-Session"] = self._session_token
        return headers

    # --- transport --------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` with retry/backoff on 429/5xx; returns the parsed JSON body."""
        self._ensure_auth()
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url, headers=self._headers(), params=params, timeout=self.timeout
                )
            except Exception as exc:  # network error — retry with backoff
                last_exc = exc
                self._backoff(attempt)
                continue
            status = getattr(resp, "status_code", 200)
            if status in _RETRYABLE_STATUS and attempt < self.max_retries:
                self._backoff(attempt, resp)
                continue
            if status >= 400:
                raise MetabaseAPIError(f"GET {url} failed with status {status}.")
            return resp.json()
        raise MetabaseAPIError(f"GET {url} failed after {self.max_retries} retries: {last_exc}")

    def _backoff(self, attempt: int, resp: Any = None) -> None:
        """Exponential backoff with light jitter; honor ``Retry-After`` when present."""
        delay = min(2.0**attempt, 30.0)
        if resp is not None:
            retry_after = getattr(resp, "headers", {}) or {}
            with contextlib.suppress(TypeError, ValueError):
                delay = max(delay, float(retry_after.get("Retry-After", 0)))
        # Deterministic small jitter (attempt-derived) keeps unit tests reproducible while
        # still de-synchronizing concurrent extractors.
        self._sleep(delay + (attempt % 3) * 0.1)

    def _paginate(self, path: str, extra_params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every item from a Metabase list endpoint.

        Metabase's bulk list endpoints (``/api/card``, ``/api/dashboard``,
        ``/api/native-query-snippet``) return the ENTIRE result set as a **bare JSON array**
        in a single response and IGNORE ``limit``/``offset``. A bare-array response is
        therefore already complete — we must NOT request another "page", or the endpoint
        re-returns the whole list and we loop forever (on a 17k-card / 232MB instance this
        hangs and 503s the gateway). So we probe once *without* limit/offset: a list body is
        the full result. Only an envelope body (``{"data": [...]}``) is genuinely paginated;
        that path advances by ``offset`` and is hard-capped by ``_MAX_PAGES`` so it can never
        loop indefinitely even if a deployment also ignores ``offset``.
        """
        body = self._get(path, params=extra_params)
        if not isinstance(body, dict):
            yield from body or []
            return

        offset = 0
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = dict(extra_params or {})
            params.update({"limit": self.page_size, "offset": offset})
            page = self._get(path, params=params)
            items = page.get("data", []) if isinstance(page, dict) else (page or [])
            if not items:
                return
            yield from items
            if len(items) < self.page_size:
                return
            offset += len(items)

    # --- endpoints --------------------------------------------------------
    def list_cards(self, include_archived: bool = False) -> list[dict]:
        """All cards (``GET /api/card``), MBQL pinned to the legacy (v4) serialization."""
        params = dict(LEGACY_MBQL_PARAM)
        if include_archived:
            params["f"] = "archived"
        return list(self._paginate("/api/card", extra_params=params))

    def list_dashboards(self) -> list[dict]:
        """All dashboards (``GET /api/dashboard``) — summary shells; use
        :meth:`get_dashboard` for each one's ``dashcards``."""
        return list(self._paginate("/api/dashboard"))

    def get_dashboard(self, dashboard_id: int) -> dict:
        """One dashboard with its ``dashcards`` (``GET /api/dashboard/:id``)."""
        return self._get(f"/api/dashboard/{dashboard_id}")

    def get_dashboards(self, dashboard_ids: list[int], max_workers: int = 8) -> dict[int, dict]:
        """Fetch many dashboards concurrently, returning ``{id: detail}``.

        Auth is warmed once up front (:meth:`ensure_auth`) so worker threads never race on
        the lazy ``POST /api/session``. Work fans out over a bounded
        :class:`~concurrent.futures.ThreadPoolExecutor`. A single id, or ``max_workers <= 1``,
        runs sequentially (deterministic, no pool overhead). Per-dashboard failures propagate
        — a partial snapshot must fail loud rather than silently drop a dashboard."""
        if not dashboard_ids:
            return {}
        self.ensure_auth()

        if max_workers <= 1 or len(dashboard_ids) == 1:
            return {did: self.get_dashboard(did) for did in dashboard_ids}

        workers = max(1, min(max_workers, len(dashboard_ids)))
        results: dict[int, dict] = {}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(self.get_dashboard, did): did for did in dashboard_ids}
            for future in concurrent.futures.as_completed(futures):
                # Re-raise the first per-dashboard error (fail loud on a partial snapshot).
                results[futures[future]] = future.result()
        finally:
            # On the error path this cancels queued-but-unstarted fetches so the abort surfaces
            # promptly instead of waiting out the whole batch's retry ladders; on the happy path
            # every future is already done, so it is a no-op.
            executor.shutdown(wait=True, cancel_futures=True)
        return results

    def list_snippets(self) -> list[dict]:
        """All native-query snippets (``GET /api/native-query-snippet``)."""
        return list(self._paginate("/api/native-query-snippet"))

    def server_version(self) -> str | None:
        """Best-effort Metabase version tag (``GET /api/session/properties``) for provenance.

        Returns ``None`` if the endpoint is unavailable — version stamping is nice-to-have,
        never a hard requirement of the extract."""
        try:
            props = self._get("/api/session/properties")
        except Exception:
            return None
        version = (props or {}).get("version") or {}
        tag = version.get("tag") if isinstance(version, dict) else None
        return str(tag) if tag else None

    def database_metadata(self, database_id: int) -> dict:
        """Bulk tables+fields for one database (``GET /api/database/:id/metadata``).

        One call resolves every Table/Field id → ``schema.table.column`` in memory, so the
        MBQL resolver needs no per-field round-trips."""
        return self._get(f"/api/database/{database_id}/metadata")
