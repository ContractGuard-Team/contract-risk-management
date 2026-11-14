"""Content hashes and change detection: the site sends no ETag/Last-Modified, so bytes are the only truth."""
import hashlib
import json


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def version_hash(file_hashes):
    """One hash for a document version made of one or more attachments (order-independent)."""
    return sha256("\n".join(sorted(file_hashes)).encode()) if len(file_hashes) > 1 else file_hashes[0]


def row_fingerprint(row):
    """Cheap change signal from the list page: an unchanged fingerprint lets incremental runs skip downloads."""
    keys = ("symbol", "issued_on", "title")
    return sha256(json.dumps([*(row.get(k) for k in keys), sorted(row.get("files", []))], ensure_ascii=False).encode())


def decide(known, row, *, full):
    """new | unchanged | recheck. 'recheck' downloads and compares the content hash."""
    if known is None:
        return "new"
    if known.get("list_fingerprint") == row_fingerprint(row) and not full:
        return "unchanged"
    return "recheck"
