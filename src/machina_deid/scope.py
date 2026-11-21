"""Attempt 2c: production two-pass *shared identity scope* for PDF and OLE (top level and ZIP members).

Pass 1.5 (:func:`build_scope`, after the attempt catalog is frozen) scans every PDF (title-block / role-label
/ contact findings, :mod:`pdf_titleblock`), every CFB file (parsed SummaryInformation saved-account property,
:mod:`ole_transform`), and the PDF/CFB members of ZIP archives, in manifest order, and freezes ONE keyed entity
map (:mod:`pdf_entities`, HMAC value keys, no raw values, never overwritten). Its digest is pinned in the
ledger, so every pass-2 worker resolves against identical state.

Pass 2 (:class:`Scope`) resolves only through that frozen map (plus the file's own catalog-approved strings).
There is no derive mode and no per-file alias: a value absent from the map, or ambiguous in it, is reported
unresolved and left alone. OLE accounts get the entity's pseudonym when it has the same length, otherwise a
deterministic keyed equal-length alias of the original, identical in every file (same identity everywhere).

Fail closed: any validation failure returns the original bytes and a stable value-free reason.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import archive as ar
from . import ole_transform as ot

MAP_NAME = "entity-map.sqlite"
OLE_SOURCE = "structured:ole_lastauthor"
OLE_PIDS = (ot.PID_LASTAUTHOR,)
_ALIAS_LABEL = b"machina-deid/ole-equal-length-alias"


@dataclass(frozen=True)
class Obs:
    """A pass-1 observation (raw value: memory/IPC only, never written)."""

    kind: str
    value: str = field(repr=False)
    source: str


def ole_alias_key(secret: bytes) -> bytes:
    return hmac.new(secret, _ALIAS_LABEL, hashlib.sha256).digest()


def is_cfb_path(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            return ot.is_cfb(f.read(8))
    except OSError:
        return False


# ------------------------------------------------------------------ pass 1.5: scanning


def scan_pdf(source, ocr: bool = False) -> tuple[str, list[Obs]]:
    """``source``: path or bytes. Returns (status, observations). Status: ok | encrypted | failed:<type>."""
    import pymupdf
    from .pdf_titleblock import page_lines, scan_page_lines
    try:
        doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes) else pymupdf.open(source)
    except Exception as exc:  # noqa: BLE001 - type name only
        return f"failed:{type(exc).__name__}", []
    try:
        if doc.needs_pass:
            return "encrypted", []
        out: list[Obs] = []
        for page in doc:
            lines = page_lines(page)
            if not lines and ocr:
                from .pdf_ocr import ocr_page_lines
                lines = ocr_page_lines(page)
            out += [Obs(f.kind, f.value, f.source) for f in scan_page_lines(lines) if f.source != "lookup"]
        return "ok", out
    except Exception as exc:  # noqa: BLE001
        return f"failed:{type(exc).__name__}", []
    finally:
        doc.close()


def scan_cfb(data: bytes) -> tuple[str, list[Obs]]:
    try:
        props = ot.summary_props(ot.parse(data), OLE_PIDS)
    except ot.OleError as exc:
        return f"failed:{exc}"[:60], []
    return "ok", [Obs("account", p.value, OLE_SOURCE) for p in props if p.value.strip()]


def _scan_zip(zf: zipfile.ZipFile, prefix: str, depth: int, budget, out: list, ocr: bool) -> None:
    for info in ar._validate(zf, depth, budget):
        if info.is_dir() or ar._is_symlink(info) or ar.is_metadata_sidecar(info.filename):
            continue
        ext = PurePosixPath(info.filename).suffix.lower()
        name = f"{prefix}!{info.filename}"
        if ext not in ar.NESTED and ext != ".pdf":
            with zf.open(info) as f:  # cheap magic sniff before a bounded full read
                if not ot.is_cfb(f.read(8)):
                    continue
        data = ar._read_bounded(zf, info, budget.limits.max_member_bytes)
        if ext in ar.NESTED:
            try:
                inner = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                continue
            with inner:
                _scan_zip(inner, name, depth + 1, budget, out, ocr)
        elif ot.is_cfb(data):
            out.append((name, *scan_cfb(data)))
        elif data[:5] == b"%PDF-" or ext == ".pdf":
            out.append((name, *scan_pdf(data, ocr)))


def scan_file(path: str | Path, rel: str, ocr: bool = False,
              limits: ar.ArchiveLimits = ar.ArchiveLimits()) -> list[tuple[str, str, list[Obs]]]:
    """``[(document rel, status, observations)]`` for one manifest file (ZIPs yield member documents)."""
    from .twopass import detect_format
    path = Path(path)
    if is_cfb_path(path):
        return [(rel, *scan_cfb(path.read_bytes()))]
    fmt = detect_format(path)
    if fmt == "pdf":
        return [(rel, *scan_pdf(str(path), ocr))]
    if fmt == "zip":
        out: list = []
        try:
            with zipfile.ZipFile(path) as zf:
                _scan_zip(zf, rel, 0, ar._Budget(limits), out, ocr)
        except (ar.ArchiveError, zipfile.BadZipFile, EOFError, OSError) as exc:
            out.append((rel, f"failed:{getattr(exc, 'reason', type(exc).__name__)}", []))
        return out
    return []


def _scan_pair(item, ocr):
    try:
        return scan_file(item[0], item[1], ocr)
    except Exception as exc:  # noqa: BLE001 - counted by type; a scan failure never aborts the build
        return [(item[1], f"failed:{type(exc).__name__}", [])]


def build_scope(map_path: str | Path, secret: bytes, items, *, catalog=None, workers: int = 1,
                ocr: bool = False) -> dict:
    """Scan ``items`` ([(physical path, manifest rel)], manifest order) and freeze the shared map.

    Observations are added in input order regardless of ``workers``, so the frozen digest is reproducible."""
    from .pdf_entities import EntityMapBuilder
    b = EntityMapBuilder(secret, catalog, ole_alias_key=ole_alias_key(secret))
    st: Counter = Counter()

    def consume(results):
        for docs in results:
            for rel, status, obs in docs:
                st[f"documents:{'ole' if any(o.source == OLE_SOURCE for o in obs) else 'pdf_or_other'}"] += 1
                st[f"status:{status.split(':')[0]}"] += 1
                if status != "ok":
                    st[f"failed:{status.split(':', 1)[-1]}"[:60]] += 1
                    continue
                b.add_document(rel, obs)

    items = list(items)
    if workers <= 1 or len(items) <= 1:
        consume(_scan_pair((p, r), ocr) for p, r in items)
    else:
        from concurrent.futures import ProcessPoolExecutor
        from functools import partial
        import multiprocessing as mp
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as pool:
            consume(pool.map(partial(_scan_pair, ocr=ocr), items, chunksize=4))
    stats = b.freeze(map_path)
    return {**stats, **dict(sorted(st.items()))}


# ------------------------------------------------------------------ pass 2


@dataclass
class OleOutcome:
    data: bytes = field(repr=False)
    status: str  # transformed | no_identities | failed
    reason: str = ""
    props: int = 0
    mapped: int = 0
    unresolved: Counter = field(default_factory=Counter)
    property_writes: int = 0
    cross_stream_writes: int = 0
    informative_raw: int = 0  # unverified occurrences left untouched (counted, never written)


class Scope:
    """Read-only pass-2 view of the frozen shared map (one per worker process)."""

    def __init__(self, map_path: str | Path, secret: bytes, expect_digest: str, *, ocr: bool = False):
        from .pdf_entities import Resolver
        if not expect_digest:
            raise ValueError("the frozen entity-map digest is required")
        self.map_path, self._secret, self.digest = Path(map_path), bytes(secret), expect_digest
        self._alias_key = ole_alias_key(self._secret)
        self.ocr = ocr
        Resolver(self._secret, self.map_path, expect_digest=expect_digest).close()  # fail fast

    def resolver(self, catalog_strings: dict[str, str] | None = None):
        from .pdf_entities import Resolver
        return Resolver(self._secret, self.map_path, catalog_strings=catalog_strings, expect_digest=self.digest)

    # -------------------------------------------------------------- PDF
    def pdf(self, src: Path, dst: Path, catalog_strings: dict[str, str] | None = None):
        from .pdf_scoped import transform_pdf_scoped
        r = self.resolver(catalog_strings)
        try:
            return transform_pdf_scoped(src, dst, r, overwrite=True, ocr=self.ocr)
        finally:
            r.close()

    def pdf_bytes(self, data: bytes, work: Path, catalog_strings=None):
        src, dst = work / f"s{os.getpid()}-{id(data)}.pdf", work / f"d{os.getpid()}-{id(data)}.pdf"
        src.write_bytes(data)
        try:
            res = self.pdf(src, dst, catalog_strings)
            return res, (dst.read_bytes() if res.ok and dst.exists() else None)
        finally:
            src.unlink(missing_ok=True)
            dst.unlink(missing_ok=True)

    # -------------------------------------------------------------- OLE
    def ole_alias(self, value: str, pseudonym: str) -> str:
        if pseudonym != value and ot.widths_match(value, pseudonym):
            return pseudonym
        return ot.equal_length_alias(value, self._alias_key)

    def ole(self, data: bytes) -> OleOutcome:
        try:
            props = ot.summary_props(ot.parse(data), OLE_PIDS)
        except ot.OleError as exc:
            return OleOutcome(data, "failed", f"ole_parse:{exc}"[:80])
        out = OleOutcome(data, "no_identities", props=len(props))
        values = sorted({p.value for p in props if p.value.strip()})
        if not values:
            out.reason = "no_saved_account_property"
            return out
        r = self.resolver()
        mapping: dict[str, str] = {}
        try:
            for v in values:
                e = r.entry(v)
                if e is None:
                    out.unresolved["not_in_frozen_map"] += 1
                elif e.status != "ok":
                    out.unresolved["ambiguous_identity"] += 1
                else:
                    mapping[v] = self.ole_alias(v, e.pseudonym)
        finally:
            r.close()
        out.mapped = len(mapping)
        if not mapping:
            out.reason = "unresolved:" + ",".join(sorted(out.unresolved))
            if out.unresolved:
                out.status = "failed"  # a discovered saved account that cannot be rewritten is never "clean"
            return out
        res = ot.transform(data, mapping, OLE_PIDS)
        if res.reason is not None:
            out.status, out.reason = "failed", f"ole:{res.reason}"[:80]
            return out
        out.data, out.status = res.data, "transformed"
        out.property_writes = sum(w.kind == "property" for w in res.writes)
        out.cross_stream_writes = len(res.writes) - out.property_writes
        out.informative_raw = sum(v for k, v in res.counts.items() if k not in ot.VERIFIED_KINDS)
        if out.unresolved:  # partially rewritten: keep output (verified) but never claim fully clean
            out.reason = "partial_unresolved:" + ",".join(sorted(out.unresolved))
        return out


def scope_from_state(private: Path, secret: bytes, digest: str, ocr: bool = False) -> Scope | None:
    p = Path(private) / MAP_NAME
    return Scope(p, secret, digest, ocr=ocr) if digest and p.exists() else None


def temp_workdir(parent: Path | None = None):
    return tempfile.TemporaryDirectory(prefix="deid-scope-", dir=parent)
