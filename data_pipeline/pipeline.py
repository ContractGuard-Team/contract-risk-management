"""Wiring for the EC2 (systemd, own venv) runtime: environment file and real clients. No Docker.

Configuration is the project's single root .env (section 9 + shared keys); secrets left empty there are read from
AWS Secrets Manager (API_KEYS_SECRET_ID, same secret the Config service uses) with the instance role.
"""
from functools import partial
import os
from pathlib import Path
from types import SimpleNamespace

DEFAULT_ENV_FILE = str(Path(__file__).resolve().parents[1] / ".env")
# What the pipeline uses; INTERNAL_API_TOKEN authenticates its events to the monitoring service.
SECRET_KEYS = ("QDRANT_URL", "QDRANT_API_KEY", "SUPABASE_URL", "SUPABASE_SECRET_KEY", "INTERNAL_API_TOKEN")


def env_file():
    return Path(os.getenv("DATA_PIPELINE_ENV_FILE", DEFAULT_ENV_FILE))


def load_environment():
    """Values already in the process environment win over the file; empty secrets come from Secrets Manager."""
    path = env_file()
    if path.is_file():
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        if name in os.environ and not os.environ[name].strip():
            del os.environ[name]  # empty template keys must not hide the instance role
    missing = [name for name in SECRET_KEYS if not os.getenv(name, "").strip()]
    secret_id = os.getenv("API_KEYS_SECRET_ID", "").strip()
    if missing and secret_id:
        from config.secrets import read_secret_json
        values = read_secret_json(secret_id)
        for name in missing:
            if isinstance(values.get(name), str) and values[name]:
                os.environ[name] = values[name]
    cache = os.getenv("EMBEDDING_CACHE_DIR", "").strip()
    if cache and not Path(cache).is_absolute():  # "models" = the repo's pre-downloaded models folder
        os.environ["EMBEDDING_CACHE_DIR"] = str(Path(DEFAULT_ENV_FILE).parent / cache)
    # Events reach the monitoring container through its loopback port (docker-compose MONITORING_HOST_PORT).
    if os.getenv("DATA_MONITORING_URL", "").strip() and len(os.getenv("INTERNAL_API_TOKEN", "")) >= 32:
        os.environ.setdefault("MONITORING_URL", os.environ["DATA_MONITORING_URL"].strip())


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in {os.getenv('DATA_PIPELINE_ENV_FILE', DEFAULT_ENV_FILE)}")
    return value


def archiver():
    from data_pipeline.ingestion.s3_archiver import S3Archiver
    return S3Archiver(required("DATA_S3_BUCKET"), region=required("AWS_REGION"))


def store():
    from data_pipeline.ingestion.postgres_writer import SupabaseStore
    return SupabaseStore(required("SUPABASE_URL"), required("SUPABASE_SECRET_KEY"))


def source():
    from data_pipeline.sources.legal_source import LegalSource
    urls = [u.strip() for u in (os.getenv("SOURCE_LIST_URLS") or os.getenv("SOURCE_LIST_URL", "")).split(",") if u.strip()]
    if not urls:
        required("SOURCE_LIST_URLS")
    return LegalSource(urls, delay=float(os.getenv("CRAWL_DELAY_SECONDS", "1")),
                       concurrency=int(os.getenv("CRAWL_CONCURRENCY", "3")),
                       timeout=float(os.getenv("CRAWL_TIMEOUT_SECONDS", "60")),
                       user_agent=os.getenv("CRAWL_USER_AGENT", "contract-risk-data-collector/1.0"))


def processing_context():
    from data_pipeline.ingestion.qdrant_writer import QdrantWriter
    from data_pipeline.processing.extractor import extract_pdfs
    from integrations.external.embedding_client import LocalEmbedder  # same model the MCP gateway uses for queries
    required("QDRANT_URL")
    return SimpleNamespace(store=store(), embedder=LocalEmbedder(), qdrant=QdrantWriter(), fetch=fetch_files,
                           extract=partial(extract_pdfs, languages=os.getenv("OCR_LANG", "vie"),
                                           dpi=int(os.getenv("OCR_DPI", "300"))))


def fetch_files(urls):
    """Processor retries download the originals again from the source site (the processor never reads S3)."""
    import asyncio
    from data_pipeline.ingestion.s3_archiver import safe_name

    async def run():
        crawler = source()
        try:
            return [(safe_name(url), await crawler.download(url)) for url in urls]
        finally:
            await crawler.aclose()
    return asyncio.run(run())
