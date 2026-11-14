"""Read-only Supabase (PostgREST) access for Agent 2/3 through the official `supabase` Python client.

Key: SUPABASE_SECRET_KEY when set (backend use, can list the schema, bypasses RLS), else SUPABASE_PUBLISHABLE_KEY
(RLS applies). Read-only either way: the HTTP client refuses every non-GET request before it leaves the container,
and PostgREST runs GET requests in a READ ONLY transaction.
"""
import os
import re

import httpx

from common.exceptions import IntegrationNotConfigured

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "is", "in"}


def _refuse_writes(request):
    if request.method not in ("GET", "HEAD"):
        raise PermissionError(f"Supabase {request.method} refused: MCP Supabase access is read-only")


class SupabaseReader:
    def __init__(self, url=None, key=None, *, http_client=None, tables=None, max_rows=100):
        url = os.getenv("SUPABASE_URL", "") if url is None else url
        if key is None:
            key = os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
        if not url or not key:
            raise IntegrationNotConfigured("TODO: set SUPABASE_URL and SUPABASE_SECRET_KEY or SUPABASE_PUBLISHABLE_KEY")
        self.url, self.secret = url.rstrip("/"), key.startswith("sb_secret_")
        self.headers = {"apikey": key, "Authorization": "Bearer " + key}
        raw = os.getenv("SUPABASE_TABLES", "") if tables is None else ",".join(tables)
        self.tables = [t.strip() for t in raw.split(",") if t.strip()]
        http = http_client if http_client is not None else httpx.Client(timeout=15, follow_redirects=False)
        http.event_hooks = {**http.event_hooks, "request": [*http.event_hooks["request"], _refuse_writes]}
        from supabase import create_client
        from supabase.lib.client_options import SyncClientOptions
        self.http = http
        self.client = create_client(url, key, options=SyncClientOptions(
            httpx_client=http, auto_refresh_token=False, persist_session=False))
        self.max_rows = max_rows

    def list_tables(self):
        if not self.secret:  # Supabase: "Secret API key required" for the schema endpoint
            return {"tables": self.tables, "restricted": bool(self.tables), "source": "SUPABASE_TABLES"}
        spec = self.http.get(self.url + "/rest/v1/", headers=self.headers).raise_for_status().json()
        found = sorted(p.strip("/") for p in spec.get("paths", {}) if p != "/" and not p.startswith("/rpc/"))
        return {"tables": [t for t in found if not self.tables or t in self.tables],
                "restricted": bool(self.tables), "source": "schema"}

    def select(self, table, columns="*", filters=None, order=None, limit=50):
        if not NAME.fullmatch(table or ""):
            raise ValueError("Invalid table name")
        if self.tables and table not in self.tables:
            raise PermissionError("Table is not in SUPABASE_TABLES")
        if not re.fullmatch(r"[A-Za-z0-9_,*() :.>-]{1,500}", columns or ""):
            raise ValueError("Invalid columns selector")
        query = self.client.table(table).select(columns)
        for column, expression in (filters or {}).items():
            operator, _, value = str(expression).partition(".")
            if not NAME.fullmatch(column) or operator not in OPERATORS or not value:
                raise ValueError("Filters look like {\"column\": \"eq.value\"} with eq/neq/gt/gte/lt/lte/like/ilike/is/in")
            query = query.filter(column, operator, value)
        if order:
            column, _, direction = order.partition(".")
            if not NAME.fullmatch(column) or direction not in ("", "asc", "desc"):
                raise ValueError("order looks like \"column.asc\" or \"column.desc\"")
            query = query.order(column, desc=direction == "desc")
        rows = query.limit(max(1, min(int(limit), self.max_rows))).execute().data
        return {"table": table, "rows": rows, "count": len(rows)}
