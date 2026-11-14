"""Local multilingual embeddings with fastembed (ONNX on CPU): no API key, text never leaves the host.

The data pipeline (documents) and the MCP gateway (agent queries) must use the same model or vectors are not
comparable. e5 models expect "passage: " / "query: " prefixes, which fastembed does not add for them.
The model (~2.2 GB for multilingual-e5-large) downloads once into EMBEDDING_CACHE_DIR.
"""
from collections import Counter
import os
import re
import threading
import unicodedata
import zlib

DEFAULT_MODEL = "intfloat/multilingual-e5-large"
SPARSE_VECTOR = "keywords"  # named sparse vector of the Qdrant collection (IDF applied by Qdrant)
# A law symbol ("61/2020/qh14") stays one token; otherwise Vietnamese syllables.
TOKEN = re.compile(r"\d+(?:/[\w-]+)+|\w+")
_lock, _shared = threading.Lock(), {}


def keyword_tokens(text):
    words = TOKEN.findall(unicodedata.normalize("NFC", text or "").lower())
    # Syllable bigrams carry most Vietnamese words ("hợp đồng", "bồi thường").
    return words + [a + " " + b for a, b in zip(words, words[1:])]


def sparse_vector(text, *, query=False, k1=1.2):
    """BM25-style keyword vector {indices, values}. Documents: saturated term frequency; queries: 1 per term.
    Qdrant multiplies by IDF (collection sparse vector modifier=idf), so no corpus statistics are kept here."""
    counts = Counter(zlib.crc32(t.encode()) & 0x7FFFFFFF for t in keyword_tokens(text))
    indices = sorted(counts)
    values = [1.0 if query else counts[i] * (k1 + 1) / (counts[i] + k1) for i in indices]
    return {"indices": indices, "values": values}


def shared(name, factory):
    """One model instance per process (each is 1-2 GB of RAM), created on first use."""
    with _lock:
        if name not in _shared:
            _shared[name] = factory()
        return _shared[name]


class LocalEmbedder:
    def __init__(self, model=None, *, cache_dir=None, dimensions=None, engine=None):
        self.model_name = model or os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        self.dimensions = int(dimensions or os.getenv("EMBEDDING_DIMENSIONS", "1024"))
        e5 = "e5" in self.model_name.lower()
        self.passage_prefix, self.query_prefix = ("passage: ", "query: ") if e5 else ("", "")
        if engine is None:
            from fastembed import TextEmbedding
            engine = TextEmbedding(self.model_name, cache_dir=cache_dir or os.getenv("EMBEDDING_CACHE_DIR") or None)
        self.engine = engine

    def _vectors(self, texts):
        vectors = [[float(x) for x in v] for v in self.engine.embed(texts, batch_size=16)]
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise ValueError("Embedding count/dimension does not match EMBEDDING_DIMENSIONS")
        return vectors

    def embed(self, texts):
        """Documents (law articles) for indexing."""
        return self._vectors([self.passage_prefix + t for t in texts]) if texts else []

    def embed_query(self, text):
        """An agent's search question."""
        return self._vectors([self.query_prefix + text])[0]
