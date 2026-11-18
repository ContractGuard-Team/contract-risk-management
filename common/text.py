"""Text comparison shared by agents (output checks) and orchestration (quotes, scoring)."""
import unicodedata

QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'", "\u2019": "'", "\u201a": "'"})


def normalize(text) -> str:
    """risk_policy `matching`: NFKC, straight quotes, whitespace/newlines collapsed (case handled by the caller)."""
    return " ".join(unicodedata.normalize("NFKC", text or "").translate(QUOTES).split())


def contains(source, part) -> bool:
    """`part` is a contiguous piece of `source` after normalize() (never a paraphrase or two joined pieces)."""
    return bool(normalize(part)) and normalize(part) in normalize(source)
