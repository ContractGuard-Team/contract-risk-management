"""Keep only Hiến pháp, Luật, Bộ luật and their amendments; everything else is skipped with a reason."""
import re
import unicodedata

KEPT_TYPES = {"hiến pháp", "luật", "bộ luật"}
QH_SYMBOL = re.compile(r"^\d+/\d{4}/QH\d+$")
AMENDMENT = re.compile(r"^((luật|bộ luật)\s+)?sửa đổi,?\s+bổ sung|^luật sửa đổi|^bộ luật sửa đổi")
# Cheap pre-filter on the list row title: never fetch details for Pháp lệnh/Nghị quyết/Lệnh/...
TITLE_PREFIX = re.compile(r"^(hiến pháp|luật|bộ luật|sửa đổi)")


def _norm(text):
    return " ".join(unicodedata.normalize("NFC", text or "").lower().split())


def maybe_kept(title):
    return bool(TITLE_PREFIX.match(_norm(title)))


def classify(doc_type, title, symbol):
    """Return (doc_class, None) for kept documents or (None, reason)."""
    kind, name = _norm(doc_type), _norm(title)
    if kind not in KEPT_TYPES:
        return None, f"type:{doc_type or 'unknown'}"
    if kind == "hiến pháp" or name.startswith("hiến pháp"):
        return "constitution", None
    if not QH_SYMBOL.match((symbol or "").strip()):
        return None, f"symbol:{symbol or 'missing'}"
    if AMENDMENT.match(name):
        return "amendment", None
    if kind == "bộ luật" or name.startswith("bộ luật"):
        return "code", None
    return "law", None


def document_symbol(symbol, doc_class, title, issued_on):
    """Hiến pháp entries carry symbols like "." or "68/LCT/HĐNN8": key them as HP-<year> so they stay unique."""
    symbol = (symbol or "").strip()
    if doc_class != "constitution" or QH_SYMBOL.match(symbol):
        return symbol
    year = re.search(r"(1[89]\d\d|20\d\d)", title or "") or re.match(r"(\d{4})", issued_on or "")
    return f"HP-{year[1]}" if year else symbol
