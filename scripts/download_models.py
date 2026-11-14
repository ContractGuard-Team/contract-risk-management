"""Download the configured local models into <repo>/models once, so EC2 never downloads them at runtime.

    python scripts/download_models.py          (needs internet; ~3.3 GB)

Models (names from .env / .env.example): EMBEDDING_MODEL (intfloat/multilingual-e5-large, ~2.2 GB) used by the MCP
gateway and the data pipeline, and RAG_RERANK_MODEL (jinaai/jina-reranker-v2-base-multilingual, ~1.1 GB) used by
legal_search. Copy the repo with models/ to EC2: the mcp container mounts it read-only at /models and the pipeline
reads it from the repo, both with HF_HUB_OFFLINE=1. Tesseract (vie) is installed in the input-processing image.
"""
import json
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def setting(name, default):
    for source in (os.environ, dotenv_values(ROOT / ".env"), dotenv_values(ROOT / ".env.example")):
        if name in source and source[name] is not None:
            return source[name].strip()
    return default


def size_gb(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 ** 3


def main():
    target = ROOT / "models"
    target.mkdir(exist_ok=True)
    os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1", HF_HUB_OFFLINE="0")
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    embedding = setting("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    print(f"embedding model {embedding} -> {target}", flush=True)
    vector = next(iter(TextEmbedding(embedding, cache_dir=str(target)).embed(["passage: Hợp đồng mua bán"])))
    print(f"  ok, dimension {len(vector)}", flush=True)
    rerank = setting("RAG_RERANK_MODEL", "jinaai/jina-reranker-v2-base-multilingual")
    if rerank:
        print(f"rerank model {rerank} -> {target}", flush=True)
        scores = list(TextCrossEncoder(rerank, cache_dir=str(target)).rerank("phạt vi phạm", ["Điều 418. Thỏa thuận phạt vi phạm"]))
        print(f"  ok, score {scores[0]:.3f}", flush=True)
    # fastembed records file paths with the OS separator; a cache made on Windows must use "/" to verify on Linux (EC2).
    for meta in target.glob("models--*/files_metadata.json"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        meta.write_text(json.dumps({key.replace("\\", "/"): value for key, value in data.items()}), encoding="utf-8")
    print(f"models/ total {size_gb(target):.2f} GB", flush=True)


if __name__ == "__main__":
    main()
