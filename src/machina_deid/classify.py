"""Conservative file-format classifier.

Classification uses the extension plus a small magic-byte check. When the two
disagree, or the content is not positively recognised, the result is
``unknown`` / ``mismatch`` rather than a guess. Only the first few bytes of a
file are read; file content is never executed or parsed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HEADER_BYTES = 16

# extension -> (format, family)
_EXT = {
    ".pdf": ("pdf", "document"),
    ".png": ("png", "image"),
    ".jpg": ("jpeg", "image"),
    ".jpeg": ("jpeg", "image"),
    ".gif": ("gif", "image"),
    ".bmp": ("bmp", "image"),
    ".tif": ("tiff", "image"),
    ".tiff": ("tiff", "image"),
    ".zip": ("zip", "archive"),
    ".7z": ("7z", "archive"),
    ".step": ("step", "neutral_cad"),
    ".stp": ("step", "neutral_cad"),
    ".dxf": ("dxf", "drawing"),
    ".dwg": ("dwg", "drawing"),
    ".sldprt": ("sldprt", "native_cad"),
    ".sldasm": ("sldasm", "native_cad"),
    ".slddrw": ("slddrw", "native_cad"),
    ".ipt": ("ipt", "native_cad"),
    ".iam": ("iam", "native_cad"),
    ".idw": ("idw", "native_cad"),
    ".fcstd": ("fcstd", "native_cad"),
    ".stl": ("stl", "mesh"),
    ".svg": ("svg", "vector"),  # attempt 2b: no short magic (XML); svg_meta validates the root element
}

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# format -> predicate over header bytes returning True when content is consistent.
# None means no reliable magic; extension alone is trusted only as "unverified".
def _magic_ok(fmt: str, h: bytes) -> bool | None:
    if fmt == "pdf":
        return h.startswith(b"%PDF-")
    if fmt == "png":
        return h.startswith(b"\x89PNG\r\n\x1a\n")
    if fmt == "jpeg":
        return h.startswith(b"\xff\xd8\xff")
    if fmt == "gif":
        return h.startswith((b"GIF87a", b"GIF89a"))
    if fmt == "bmp":
        return h.startswith(b"BM")
    if fmt == "tiff":
        return h.startswith((b"II*\x00", b"MM\x00*"))
    if fmt in ("zip", "fcstd"):
        return h.startswith(_ZIP)
    if fmt == "7z":
        return h.startswith(b"7z\xbc\xaf\x27\x1c")
    if fmt == "dwg":
        return h.startswith(b"AC10") or h.startswith(b"AC1")
    if fmt == "step":
        return h.lstrip().upper().startswith(b"ISO-10303-21")
    if fmt in ("sldprt", "sldasm", "slddrw", "ipt", "iam", "idw"):
        # SolidWorks/Inventor files are commonly OLE2 containers, but newer
        # versions differ; so OLE is a positive signal, anything else unverified.
        return True if h.startswith(_OLE) else None
    return None  # dxf, stl: ASCII/binary variants without a reliable short magic


# Content signatures that identify a file regardless of its extension.
_SNIFF = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"PK\x03\x04", "zip"),
)


@dataclass(frozen=True)
class Classification:
    format: str  # e.g. "pdf", "step", "unknown"
    family: str  # document|image|archive|neutral_cad|drawing|native_cad|mesh|unknown
    status: str  # "verified" | "unverified" | "mismatch" | "unknown" | "empty"
    extension: str

    @property
    def supported_for_rewrite(self) -> bool:
        """Always False for now: classification does not imply a safe transform."""
        return False


def classify_bytes(name: str, header: bytes, size: int | None = None) -> Classification:
    ext = os.path.splitext(name)[1].lower()
    if size == 0:
        return Classification(_EXT.get(ext, ("unknown", "unknown"))[0], _EXT.get(ext, ("unknown", "unknown"))[1], "empty", ext)
    header = header[:HEADER_BYTES]
    if ext in _EXT:
        fmt, family = _EXT[ext]
        ok = _magic_ok(fmt, header)
        if ok is True:
            return Classification(fmt, family, "verified", ext)
        if ok is None:
            # No positive magic; refuse if content positively looks like something else.
            for sig, other in _SNIFF:
                if header.startswith(sig) and other != fmt:
                    return Classification("unknown", "unknown", "mismatch", ext)
            return Classification(fmt, family, "unverified", ext)
        return Classification("unknown", "unknown", "mismatch", ext)
    return Classification("unknown", "unknown", "unknown", ext)


def classify_path(path: str | os.PathLike) -> Classification:
    """Classify a file by reading at most ``HEADER_BYTES`` bytes."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            header = fh.read(HEADER_BYTES)
    except OSError:
        return Classification("unknown", "unknown", "unknown", p.suffix.lower())
    return classify_bytes(p.name, header, size)
