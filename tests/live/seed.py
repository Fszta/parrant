"""Metabase REST-API seeding helpers for the live end-to-end test.

Everything here talks to a *real*, already-running Metabase over HTTP with ``requests``.
It handles both auth paths (a pre-minted API key, or session auth — including running the
first-boot ``/api/setup`` when the container is fresh), then seeds a small but
representative corpus against the built-in Sample Database:

- one NATIVE card whose SQL references a real Sample-DB table (``orders``) — the native
  resolver parses the SQL with sqlglot and maps table names via the connection's warehouse
  metadata, so the table must exist in the ``--database-id`` connection to resolve;
- one MBQL card on a real Sample-DB table (ORDERS) using real Table/Field ids;
- one dashboard showing both cards.

No warehouse / DuckDB driver is required. Metabase stores whatever legacy-shaped query we
POST but *serves it back* in the running version's serialization (legacy MBQL on old
versions, pMBQL on >=0.57) — which is exactly the drift the live test exercises.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

# The native card references a REAL table in the connected Sample DB. This mirrors production,
# where native cards query warehouse tables Metabase has synced: the extract's native resolver
# maps table names via the warehouse metadata (``WarehouseMeta.resolve_name``), so the referenced
# table must exist in the ``--database-id`` connection to resolve column-precise. (A dbt-style
# ``db.schema.table`` not present in the connection would honestly resolve to precision=none.)
NATIVE_TABLE = "orders"
NATIVE_COLUMNS = ("id", "total")
NATIVE_SQL = "SELECT id, total FROM orders WHERE total > 0"

# The real Sample-DB table the MBQL card aggregates over.
SAMPLE_TABLE = "ORDERS"


class SeedError(RuntimeError):
    """A seeding step against the live Metabase failed."""


@dataclass
class Auth:
    """Resolved auth for the live Metabase.

    Exactly one of ``api_key`` / ``session_id`` is populated. ``headers()`` yields the
    header dict to attach to every request; ``cli_args()`` yields the matching
    ``parrant metabase-extract`` credential flags so the test drives the CLI the same way a
    user would.
    """

    base_url: str
    api_key: str | None = None
    session_id: str | None = None
    username: str | None = None
    password: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        elif self.session_id:
            headers["X-Metabase-Session"] = self.session_id
        return headers

    def cli_args(self) -> list[str]:
        """Credential flags for ``parrant metabase-extract``.

        Prefers the API key; else username+password. A bare session id is not a CLI-facing
        credential, so when only a session was minted we fall back to username/password
        (which the client re-authenticates with).
        """
        if self.api_key:
            return ["--metabase-api-key", self.api_key]
        if self.username and self.password:
            return [
                "--metabase-username",
                self.username,
                "--metabase-password",
                self.password,
            ]
        raise SeedError("No CLI-usable credential (need an API key or username+password).")


@dataclass
class SeededContent:
    """Ids of everything the seeder created, for the test's assertions."""

    database_id: int
    native_card_id: int
    mbql_card_id: int
    dashboard_id: int
    card_ids: list[int] = field(default_factory=list)


def wait_for_health(base_url: str, timeout: float = 180.0, interval: float = 3.0) -> None:
    """Poll ``GET /api/health`` until ``{"status": "ok"}`` or ``timeout`` seconds elapse.

    Metabase takes 30-90s to boot, so the default timeout is generous.
    """
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/health", timeout=10)
            if resp.status_code == 200 and (resp.json() or {}).get("status") == "ok":
                return
            last_error = f"status={resp.status_code} body={resp.text[:200]}"
        except requests.RequestException as exc:  # not up yet
            last_error = str(exc)
        time.sleep(interval)
    raise SeedError(f"Metabase at {base_url} not healthy within {timeout}s (last: {last_error})")


def authenticate(
    base_url: str,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    site_name: str = "parrant-live",
) -> Auth:
    """Resolve credentials into an :class:`Auth`.

    - If ``api_key`` is given, use it directly (no network round-trip).
    - Else, if the instance is fresh (``GET /api/session/properties`` exposes a
      ``setup-token``), run ``POST /api/setup`` to create the admin ``username``/``password``
      and capture the returned session id.
    - Else (already set up), ``POST /api/session`` to log in with ``username``/``password``.
    """
    base_url = base_url.rstrip("/")
    if api_key:
        return Auth(base_url=base_url, api_key=api_key)
    if not (username and password):
        raise SeedError("Session auth needs both username and password (or pass an api_key).")

    setup_token = _setup_token(base_url)
    if setup_token:
        session_id = _run_setup(base_url, setup_token, username, password, site_name)
    else:
        session_id = _login(base_url, username, password)
    return Auth(base_url=base_url, session_id=session_id, username=username, password=password)


def _setup_token(base_url: str) -> str | None:
    resp = requests.get(f"{base_url}/api/session/properties", timeout=15)
    if resp.status_code != 200:
        return None
    return (resp.json() or {}).get("setup-token")


def _run_setup(base_url: str, token: str, username: str, password: str, site_name: str) -> str:
    payload = {
        "token": token,
        "user": {
            "first_name": "Parrant",
            "last_name": "Live",
            "email": username,
            "password": password,
            "site_name": site_name,
        },
        "prefs": {"site_name": site_name, "allow_tracking": False},
    }
    resp = requests.post(f"{base_url}/api/setup", json=payload, timeout=60)
    if resp.status_code >= 400:
        raise SeedError(f"POST /api/setup failed: {resp.status_code} {resp.text[:300]}")
    session_id = (resp.json() or {}).get("id")
    if not session_id:
        raise SeedError("POST /api/setup returned no session id.")
    return str(session_id)


def _login(base_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{base_url}/api/session",
        json={"username": username, "password": password},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SeedError(f"POST /api/session failed: {resp.status_code} {resp.text[:300]}")
    session_id = (resp.json() or {}).get("id")
    if not session_id:
        raise SeedError("POST /api/session returned no session id.")
    return str(session_id)


def _request(auth: Auth, method: str, path: str, **kwargs: Any) -> Any:
    resp = requests.request(
        method, f"{auth.base_url}{path}", headers=auth.headers(), timeout=60, **kwargs
    )
    if resp.status_code >= 400:
        raise SeedError(f"{method} {path} failed: {resp.status_code} {resp.text[:300]}")
    if resp.content:
        return resp.json()
    return None


def find_sample_database(auth: Auth) -> int:
    """Return the id of the built-in Sample Database (``GET /api/database``).

    Matches on a known embedded engine (h2 / sqlite) or a name containing "sample".
    """
    body = _request(auth, "GET", "/api/database")
    databases = body.get("data") if isinstance(body, dict) else body
    for db in databases or []:
        engine = (db.get("engine") or "").lower()
        name = (db.get("name") or "").lower()
        if engine in {"h2", "sqlite"} or "sample" in name:
            return int(db["id"])
    raise SeedError("No Sample Database found among /api/database results.")


def table_and_field_ids(
    auth: Auth, database_id: int, table_name: str = SAMPLE_TABLE
) -> dict[str, Any]:
    """Return ``{"table_id": int, "field_ids": {NAME: id}}`` for one Sample-DB table.

    Uses the bulk ``GET /api/database/:id/metadata`` so the MBQL card can be built from real
    Table/Field ids (which is what makes MBQL resolution meaningful).
    """
    body = _request(auth, "GET", f"/api/database/{database_id}/metadata")
    for table in body.get("tables", []):
        if (table.get("name") or "").upper() == table_name.upper():
            fields = {(f.get("name") or "").upper(): int(f["id"]) for f in table.get("fields", [])}
            return {"table_id": int(table["id"]), "field_ids": fields}
    raise SeedError(f"Table {table_name!r} not found in database {database_id} metadata.")


def create_native_card(
    auth: Auth, database_id: int, name: str = "parrant-live native orders"
) -> int:
    """Create a NATIVE card whose SQL references a real Sample-DB table (``orders``)."""
    dataset_query = {
        "type": "native",
        "database": database_id,
        "native": {"query": NATIVE_SQL, "template-tags": {}},
    }
    return _create_card(auth, name, dataset_query)


def create_mbql_card(
    auth: Auth,
    database_id: int,
    table_id: int,
    breakout_field_id: int,
    name: str = "parrant-live mbql orders count",
) -> int:
    """Create an MBQL card on a real Sample-DB table (count aggregated by one field).

    Posted in legacy MBQL shape, which every Metabase version accepts on write; the server
    then serves it back in *its* serialization (legacy or pMBQL) on read — the drift the
    live test exercises.
    """
    dataset_query = {
        "type": "query",
        "database": database_id,
        "query": {
            "source-table": table_id,
            "aggregation": [["count"]],
            "breakout": [["field", breakout_field_id, None]],
        },
    }
    return _create_card(auth, name, dataset_query)


def _create_card(auth: Auth, name: str, dataset_query: dict[str, Any]) -> int:
    body = _request(
        auth,
        "POST",
        "/api/card",
        json={
            "name": name,
            "dataset_query": dataset_query,
            "display": "table",
            "visualization_settings": {},
        },
    )
    card_id = (body or {}).get("id")
    if not isinstance(card_id, int):
        raise SeedError(f"POST /api/card returned no id for {name!r}.")
    return card_id


def create_dashboard(auth: Auth, card_ids: list[int], name: str = "parrant-live dashboard") -> int:
    """Create a dashboard (``POST /api/dashboard``) and place each card on it (``PUT``)."""
    body = _request(auth, "POST", "/api/dashboard", json={"name": name})
    dashboard_id = (body or {}).get("id")
    if not isinstance(dashboard_id, int):
        raise SeedError("POST /api/dashboard returned no id.")
    dashcards = [
        {
            "id": -(index + 1),
            "card_id": card_id,
            "row": 0,
            "col": index * 6,
            "size_x": 6,
            "size_y": 4,
        }
        for index, card_id in enumerate(card_ids)
    ]
    _request(auth, "PUT", f"/api/dashboard/{dashboard_id}", json={"dashcards": dashcards})
    return dashboard_id


def seed_all(auth: Auth) -> SeededContent:
    """Seed the full representative corpus and return the ids created."""
    database_id = find_sample_database(auth)
    table = table_and_field_ids(auth, database_id, SAMPLE_TABLE)
    # ``ORDERS`` always has an ID field; fall back to the first available field.
    field_ids = table["field_ids"]
    breakout_field_id = field_ids.get("ID") or next(iter(field_ids.values()))

    native_card_id = create_native_card(auth, database_id)
    mbql_card_id = create_mbql_card(auth, database_id, table["table_id"], breakout_field_id)
    card_ids = [native_card_id, mbql_card_id]
    dashboard_id = create_dashboard(auth, card_ids)
    return SeededContent(
        database_id=database_id,
        native_card_id=native_card_id,
        mbql_card_id=mbql_card_id,
        dashboard_id=dashboard_id,
        card_ids=card_ids,
    )
