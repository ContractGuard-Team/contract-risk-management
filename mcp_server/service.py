"""MCP tool gateway for Agent 2/3: streamable HTTP, bearer token, connectors opened on first use."""
from contextlib import asynccontextmanager
import hmac
import importlib
import json
import os
import time

import anyio
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP

from common.exceptions import IntegrationNotConfigured
from common.service_http import base_app, serve
from monitoring.client import emit

def lazy(module, cls):
    return lambda: getattr(importlib.import_module(module), cls)()


# Tool name -> connector factory. A connector is created only when an agent first asks for that tool.
FACTORIES = {
    "legal_search": lazy("rag.retriever", "LegalRetriever"),
    "web_search": lazy("mcp_server.tavily_search", "TavilySearch"),
    "qdrant_search": lazy("mcp_server.qdrant_reader", "QdrantReader"),
    "qdrant_collection_info": lazy("mcp_server.qdrant_reader", "QdrantReader"),
    "postgres_query": lazy("integrations.database.postgres_client", "PostgresConnector"),
    "supabase_select": lazy("mcp_server.supabase_reader", "SupabaseReader"),
    "supabase_list_tables": lazy("mcp_server.supabase_reader", "SupabaseReader"),
    "s3_get_object": lazy("mcp_server.s3_reader", "S3ReadOnly"),
    "s3_list_objects": lazy("mcp_server.s3_reader", "S3ReadOnly"),
    "partner_document": lazy("mcp_server.partner_reader", "PartnerDocuments"),
}
CONFIGURED_BY = {"legal_search": "QDRANT_URL", "web_search": "TAVILY_API_KEY", "qdrant_search": "QDRANT_URL", "qdrant_collection_info": "QDRANT_URL", "postgres_query": "POSTGRES_DSN", "supabase_select": "SUPABASE_URL",
                 "supabase_list_tables": "SUPABASE_URL",
                 "s3_get_object": "MCP_S3_BUCKETS", "s3_list_objects": "MCP_S3_BUCKETS",
                 "partner_document": "PARTNER_S3_BUCKET"}


IDENTITY = ("job_id", "runtime_id", "contract_id", "trace_id", "request_id")


def job_context(ctx) -> dict:
    """The x-job-context header the calling agent sent for its current job ({} when absent or unreadable)."""
    try:
        raw = ctx.request_context.request.headers.get("x-job-context")
        value = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class ToolGateway:
    def __init__(self, factories=FACTORIES):
        self.factories, self.open = factories, {}

    def connector(self, name):
        if name not in self.open:
            self.open[name] = self.factories[name]()
        return self.open[name]

    async def call(self, ctx, name, action):
        start = time.monotonic()
        job = job_context(ctx)
        identity = {key: job[key] for key in IDENTITY if isinstance(job.get(key), str) and job[key]}
        try:
            # Connectors are blocking (tavily/psycopg/boto3/qdrant); keep the event loop free.
            result = await anyio.to_thread.run_sync(lambda: action(self.connector(name)))
        except Exception as exc:
            emit("mcp", "tool." + name, "failed", error_type=type(exc).__name__,
                 duration_ms=(time.monotonic() - start) * 1000, **identity)
            if isinstance(exc, (IntegrationNotConfigured, ValueError, PermissionError)):
                raise  # safe, operator-written messages: tell the agent what is missing/invalid
            raise RuntimeError(f"{name} failed: {type(exc).__name__}") from None
        emit("mcp", "tool." + name, "completed", duration_ms=(time.monotonic() - start) * 1000, **identity)
        return result


def build_mcp(gateway):
    mcp = FastMCP("contract-risk-tools", host="0.0.0.0", stateless_http=True, json_response=True)

    @mcp.tool()
    async def legal_search(query: str = "", limit: int = 5, symbol: str = "", article_no: str = "", doc_class: str = "",
                           as_of: str = "", include_inactive: bool = False, ctx: Context = None) -> dict:
        """Search Vietnamese law (Hiến pháp, Bộ luật, Luật, luật sửa đổi) for evidence. Use this BEFORE web_search.
        Hybrid keyword + semantic search, reranked; each evidence item is one whole Điều with citation, evidence_id,
        validity dates, source and warnings (read them). Default: current text of laws in force.
        query: the legal question (≤1000 chars, no contract text needed); limit ≤20.
        symbol + article_no without query: fetch that Điều (e.g. symbol="91/2015/QH13", article_no="418").
        doc_class: constitution|code|law|amendment (comma-separated). as_of=YYYY-MM-DD: text in force on that date
        (e.g. the contract's signing date). include_inactive: also replaced/not-yet-effective laws.
        found=0 means the store has no matching law text."""
        return await gateway.call(ctx, "legal_search", lambda c: c.search(
            query, limit=limit, symbol=symbol, article_no=article_no, doc_class=doc_class, as_of=as_of,
            include_inactive=include_inactive))

    @mcp.tool()
    async def web_search(query: str, max_results: int = 5, ctx: Context = None) -> dict:
        """Search the public web (Tavily). Returns title, url, snippet, score per result; verify sources before citing.
        Query ≤400 characters: send a short question, never contract text."""
        return {"results": await gateway.call(ctx, "web_search", lambda c: c.search(query, max_results))}

    @mcp.tool()
    async def qdrant_search(query: str = "", vector: list[float] | None = None, collection: str = "", limit: int = 5,
                            query_filter: dict | None = None, ctx: Context = None) -> dict:
        """Semantic search over law articles (collection QDRANT_COLLECTION). Pass `query` as text; it is embedded
        with the same local model as the documents. Default filter: current articles of laws in force; pass
        query_filter={} for everything, or Qdrant filter JSON such as
        {"must": [{"key": "symbol", "match": {"value": "91/2015/QH13"}}]}."""
        return {"points": await gateway.call(ctx, "qdrant_search",
                lambda c: c.search(vector, query=query or None, collection=collection, limit=limit,
                                   query_filter=query_filter))}

    @mcp.tool()
    async def qdrant_collection_info(collection: str = "", ctx: Context = None) -> dict:
        """Status, point count, vector size/distance and payload indexes of a Qdrant collection."""
        return await gateway.call(ctx, "qdrant_collection_info", lambda c: c.info(collection))

    @mcp.tool()
    async def postgres_query(sql: str, params: list | None = None, ctx: Context = None) -> dict:
        """Run one read-only SQL statement against the reference database; pass values via params."""
        return await gateway.call(ctx, "postgres_query", lambda c: c.query(sql, params))

    @mcp.tool()
    async def supabase_select(table: str, columns: str = "*", filters: dict | None = None,
                              order: str | None = None, limit: int = 50, ctx: Context = None) -> dict:
        """Read rows (read-only, RLS applies) from a Supabase table. filters: {"column": "eq.value"}
        with eq/neq/gt/gte/lt/lte/like/ilike/is/in; order: "column.asc|desc"; limit <= 100."""
        return await gateway.call(ctx, "supabase_select",
                                  lambda c: c.select(table, columns, filters, order, limit))

    @mcp.tool()
    async def supabase_list_tables(ctx: Context = None) -> dict:
        """Tables agents may read (SUPABASE_TABLES); the publishable key cannot list the schema itself."""
        return await gateway.call(ctx, "supabase_list_tables", lambda c: c.list_tables())

    @mcp.tool()
    async def s3_get_object(key: str, bucket: str = "", ctx: Context = None) -> dict:
        """Read an object (read-only) from the configured S3 bucket; bucket may be omitted.
        Legal originals (PDF/DOC) come back binary=True, text=None: read their text with qdrant_search/supabase_select."""
        return await gateway.call(ctx, "s3_get_object", lambda c: c.get_text(bucket, key))

    @mcp.tool()
    async def s3_list_objects(prefix: str = "", bucket: str = "", max_keys: int = 100,
                              continuation_token: str | None = None, ctx: Context = None) -> dict:
        """List objects (read-only) in the configured S3 bucket under a prefix; page with next_token.
        Legal originals: prefix = qdrant_search payload s3_prefix + "/source/" (e.g. legal/QH15/2025/144-2025-QH15/source/)."""
        return await gateway.call(ctx, "s3_list_objects",
                                  lambda c: c.list_objects(bucket, prefix, max_keys, continuation_token))

    @mcp.tool()
    async def partner_document(key: str, offset: int = 0, max_chars: int = 15000, ctx: Context = None) -> dict:
        """Read the ORIGINAL contract of the current job from ContractGuard's S3 bucket (read-only).
        key: context.request.source_file_key of the job (quarantine/...); clean_file_key also accepted.
        Returns extracted text (PDF/DOCX/scan go through the contract extraction/OCR pipeline) from `offset`,
        at most max_chars (<=15000) characters; call again with next_offset until it is null.
        Quote contract wording from this text exactly; do not paraphrase it as a quote.
        Only this job's own source_file_key / clean_file_key (from its request.json) are readable."""
        job_id = job_context(ctx).get("job_id")
        return await gateway.call(ctx, "partner_document",
                                  lambda c: c.read(key, offset, max_chars, allowed=c.job_keys(job_id)))

    return mcp


def create_app(gateway=None):
    mcp = build_mcp(gateway or ToolGateway())
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_):
        async with mcp.session_manager.run():
            yield

    tools = lambda: {"tools": {n: (env is None or bool(os.getenv(env))) for n, env in CONFIGURED_BY.items()}}
    app = base_app("mcp", lifespan=lifespan, ready=tools, authenticate=False)

    @app.middleware("http")
    async def mcp_token(request, call_next):
        # Only Agent 2/3 receive MCP_API_TOKEN; INTERNAL_API_TOKEN holders cannot reach external tools.
        expected = os.getenv("MCP_API_TOKEN", "")
        if request.url.path not in ("/healthz", "/readyz") and (
                len(expected) < 32 or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected)):
            return JSONResponse({"error_type": "Unauthorized"}, status_code=401)
        return await call_next(request)

    app.mount("/", mcp_app)
    return app


if __name__ == "__main__":
    serve(create_app, "mcp")
