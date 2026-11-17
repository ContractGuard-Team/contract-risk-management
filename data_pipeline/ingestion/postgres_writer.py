"""Structured legal store on Supabase (PostgREST via supabase-py). Schema: data_pipeline/migrations/001_legal_schema.sql

Store holds the domain operations; a backend only implements four primitives (select/insert/update/upsert), so
the same logic runs against Supabase in production and an in-memory backend in tests.
"""
from datetime import datetime, timezone
import os
from data_pipeline.ingestion.chunker import ascii_fold


def now():
    return datetime.now(timezone.utc).isoformat()


def norm_title(text):
    # Accent-free: target law names come from OCR text ("Luật Đầu tư" may read "Luật Đẩu tư").
    return ascii_fold(text or "")


class Store:
    # --- primitives (backend) ---
    def select(self, table, filters=None, *, order=None, limit=None):
        raise NotImplementedError

    def insert(self, table, rows):
        raise NotImplementedError

    def update(self, table, filters, fields):
        raise NotImplementedError

    def upsert(self, table, row, on_conflict):
        raise NotImplementedError

    def _one(self, table, **filters):
        rows = self.select(table, filters, limit=1)
        return rows[0] if rows else None

    # --- documents ---
    def document_by_docid(self, docid):
        return self._one("legal_documents", docid=docid)

    def document_by_symbol(self, symbol):
        return self._one("legal_documents", symbol=symbol)

    def documents_titled(self, title):
        wanted = norm_title(title)
        return [d for d in self.select("legal_documents", {}) if norm_title(d.get("title")) == wanted]

    def save_document(self, row):
        return self.upsert("legal_documents", {**row, "updated_at": now()}, "docid")

    def update_document(self, document_id, **fields):
        self.update("legal_documents", {"id": document_id}, {**fields, "updated_at": now()})

    # --- versions ---
    def version_by_sha(self, sha):
        return self._one("legal_document_versions", sha256=sha)

    def latest_version(self, document_id):
        rows = self.select("legal_document_versions", {"document_id": document_id}, order=("version", True), limit=1)
        return rows[0] if rows else None

    def add_version(self, row):
        return self.insert("legal_document_versions", [{**row, "collected_at": now()}])[0]

    def update_version(self, version_id, **fields):
        self.update("legal_document_versions", {"id": version_id}, fields)

    # --- articles (consolidated text, versioned in time) ---
    def current_articles(self, document_id):
        return self.select("legal_articles", {"document_id": document_id, "is_current": True}, order=("position", False))

    def add_articles(self, rows):
        return self.insert("legal_articles", rows) if rows else []

    def close_article(self, article_id, valid_to):
        self.update("legal_articles", {"id": article_id}, {"is_current": False, "valid_to": valid_to})

    def reopen_article(self, article_id):
        self.update("legal_articles", {"id": article_id}, {"is_current": True, "valid_to": None})

    def flag_article(self, article_id, **fields):
        self.update("legal_articles", {"id": article_id}, fields)

    # --- relations (amends / replaces) ---
    def add_relation(self, row):
        return self.insert("legal_relations", [{**row, "created_at": now()}])[0]

    def relations(self, status):
        return self.select("legal_relations", {"status": status}, order=("effective_on", False))

    def update_relation(self, relation_id, **fields):
        self.update("legal_relations", {"id": relation_id}, fields)

    # --- collector -> processor hand-off ---
    def add_job(self, row, state="pending"):
        # Same docid+sha256 already queued or done: keep its state (a re-crawl must not reprocess it).
        existing = self._one("ingest_jobs", docid=row["docid"], sha256=row["sha256"])
        return existing or self.insert("ingest_jobs", [{**row, "state": state, "updated_at": now()}])[0]

    def pending_jobs(self, limit=20):
        return self.select("ingest_jobs", {"state": "pending"}, order=("id", False), limit=limit)

    def update_job(self, job_id, **fields):
        self.update("ingest_jobs", {"id": job_id}, {**fields, "updated_at": now()})

    def save_run(self, row):
        return self.upsert("crawl_runs", row, "run_id")


class SupabaseStore(Store):
    def __init__(self, url=None, key=None, client=None):
        if client is None:
            from supabase import create_client
            client = create_client(url or os.environ["SUPABASE_URL"], key or os.environ["SUPABASE_SECRET_KEY"])
        self.client = client

    def select(self, table, filters=None, *, order=None, limit=None):
        query = self.client.table(table).select("*")
        for column, value in (filters or {}).items():
            query = query.eq(column, value)
        if order:
            query = query.order(order[0], desc=order[1])
        if limit:
            query = query.limit(limit)
        return query.execute().data

    def insert(self, table, rows):
        return self.client.table(table).insert(rows).execute().data

    def update(self, table, filters, fields):
        query = self.client.table(table).update(fields)
        for column, value in filters.items():
            query = query.eq(column, value)
        query.execute()

    def upsert(self, table, row, on_conflict):
        return self.client.table(table).upsert(row, on_conflict=on_conflict).execute().data[0]
