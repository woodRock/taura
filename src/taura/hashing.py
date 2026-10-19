from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_json(obj) -> str:
    """Hash of canonical JSON, so whitespace and key order don't matter."""
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


# PDF metadata that changes on every save without changing what is drawn. Matplotlib honours
# SOURCE_DATE_EPOCH (taura sets it), but figures made outside taura usually carry a fresh date.
_PDF_VOLATILE = [
    re.compile(rb"/(?:CreationDate|ModDate|Producer|Creator)\s*\((?:\\.|[^\\)])*\)"),
    re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]"),
    re.compile(rb"(?<![A-Za-z])xref\s.*?(?=trailer)", re.S),  # object offsets move with metadata length
    re.compile(rb"startxref\s+\d+"),
]


def content_hash(path: Path) -> str:
    """Hash of what a file shows: for PDFs, the bytes minus volatile metadata; otherwise the bytes."""
    data = Path(path).read_bytes()
    if data.startswith(b"%PDF"):
        for pat in _PDF_VOLATILE:
            data = pat.sub(b"", data)
        return "pdf-" + sha256_bytes(data)
    return sha256_bytes(data)
