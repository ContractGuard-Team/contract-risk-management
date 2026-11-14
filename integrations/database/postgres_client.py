"""Read-only PostgreSQL connector. Base code: reference-data tables/queries are still TODO."""
import os

from common.exceptions import IntegrationNotConfigured


class PostgresConnector:
    def __init__(self, dsn=None, *, connect=None, statement_timeout_ms=5000, max_rows=100):
        self.dsn = os.getenv("POSTGRES_DSN", "") if dsn is None else dsn
        if not self.dsn:
            raise IntegrationNotConfigured("TODO: set POSTGRES_DSN to connect PostgreSQL")
        if connect is None:
            import psycopg
            connect = psycopg.connect
        self.connect, self.timeout_ms, self.max_rows = connect, statement_timeout_ms, max_rows

    def query(self, sql, params=None):
        statement = (sql or "").strip().rstrip(";").strip()
        # One statement only: "COMMIT; DELETE ..." would otherwise escape the read-only transaction.
        if not statement or ";" in statement:
            raise ValueError("Exactly one SQL statement is allowed; pass values as params")
        from psycopg.rows import dict_row
        with self.connect(self.dsn, connect_timeout=5) as conn:
            conn.read_only = True
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(self.timeout_ms),))
                cur.execute(statement, list(params or []))
                rows = cur.fetchmany(self.max_rows + 1) if cur.description else []
            conn.rollback()
        return {"rows": [{k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                          for k, v in row.items()} for row in rows[:self.max_rows]],
                "truncated": len(rows) > self.max_rows}
