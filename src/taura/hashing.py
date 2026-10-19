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


def _npz_hash(data: bytes) -> str | None:
    """Numeric arrays of an .npz (name, dtype, shape, values), ignoring text arrays: provenance
    strings, revisions and paths that change on every run without changing the numbers."""
    import ast
    import io
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    h = hashlib.sha256()
    for name in sorted(zf.namelist()):
        raw = zf.read(name)
        if not (name.endswith(".npy") and raw.startswith(b"\x93NUMPY")):
            return None
        major = raw[6]
        hlen_size = 2 if major == 1 else 4
        hlen = int.from_bytes(raw[8:8 + hlen_size], "little")
        start = 8 + hlen_size
        try:
            header = ast.literal_eval(raw[start:start + hlen].decode("latin1"))
        except (ValueError, SyntaxError):
            return None
        descr = header.get("descr")
        if isinstance(descr, str) and descr.lstrip("<>|=")[:1] in ("U", "S", "O"):
            continue  # text or objects: metadata
        h.update(f"{name}\0{descr}\0{header.get('shape')}\0{header.get('fortran_order')}\0".encode())
        h.update(raw[start + hlen:])
    return "npz-sha256:" + h.hexdigest()


def content_hash(path: Path) -> str:
    """Hash of what a file shows: for PDFs, the bytes minus volatile metadata; for .npz files, the
    numeric arrays (text arrays such as provenance strings are metadata); otherwise the bytes."""
    data = Path(path).read_bytes()
    if Path(path).suffix == ".npz" and data.startswith(b"PK"):
        h = _npz_hash(data)
        if h:
            return h
    if data.startswith(b"%PDF"):
        for pat in _PDF_VOLATILE:
            data = pat.sub(b"", data)
        return "pdf-" + sha256_bytes(data)
    return sha256_bytes(data)
