"""Conservative, bounded ZIP archive transformation (stdlib ``zipfile`` only).

Safety model
------------
* The whole member list (recursively, for nested ``.zip`` members) is validated
  *before* any member is decompressed: unsafe names (absolute, drive-letter, ``..``,
  NUL, backslash), duplicate / case-folded / file-vs-directory collisions, encrypted
  members, and configurable limits (depth, member count, per-member and total
  declared uncompressed size) all reject the archive with :class:`ArchiveError`.
* Decompression is bounded again at read time (declared sizes are not trusted).
* Nothing is ever extracted under a member's own name; supported members are written
  to private temp files with generated names and handed to the existing PDF / DXF /
  STEP transformers.
* Supported members are only transformed when an explicit global :class:`IdentityMap`
  is supplied. Without one they are reported ``inspected_no_map`` and copied unchanged;
  this is *not* a de-identification claim.
* Unsupported members (and directory entries) are copied byte-identical and reported.
* The rebuilt archive goes to a separate path via a temp file + ``os.replace`` and is
  reopened and verified (names, order, count, CRC/readability, transformed member
  hashes) before being committed. The source archive is never written.

Reports never contain raw identity values or member names (which may themselves contain
identities): members are referred to by index, a SHA-256 of the name, and extension.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .idmap import IdentityMap
from .transform_counts import TransformCounts


def is_metadata_sidecar(name: str) -> bool:
    """AppleDouble/resource-fork sidecars (``__MACOSX/...`` or ``._*``): metadata, never the named format."""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    return bool(parts) and ("__MACOSX" in parts[:-1] or parts[0] == "__MACOSX" or parts[-1].startswith("._"))


SUPPORTED = {".pdf": "pdf", ".dxf": "dxf", ".step": "step", ".stp": "step"}
NESTED = {".zip"}

MEMBER_OUTCOMES = ("directory_preserved", "unsupported_passthrough", "inspected_no_map",
                   "inspected_no_identifiers", "transformed_validated", "identifiers_unmodifiable",
                   "nested_archive")


class ArchiveError(Exception):
    """Archive rejected; ``reason`` is a stable machine-readable code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ArchiveLimits:
    max_depth: int = 2  # 0 = top-level archive only; nested zips deeper than this reject
    max_members: int = 10_000  # across all nesting levels
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024  # across all nesting levels


@dataclass
class MemberReport:
    index: int
    depth: int
    name_sha256: str
    ext: str
    is_dir: bool
    outcome: str
    bytes_in: int
    bytes_out: int = 0
    reason: str = ""
    replacements: dict[int, int] = field(default_factory=dict)
    residual: dict[int, int] = field(default_factory=dict)  # identity index -> occurrences left
    name_contains_identity: bool = False
    children: list["MemberReport"] = field(default_factory=list)
    changes: TransformCounts = field(default_factory=TransformCounts)


@dataclass
class ArchiveResult:
    ok: bool = False
    reason: str = ""
    output_written: bool = False
    complete: bool = False  # True only if a map was given and no residual/unmodifiable/unsupported remains
    source_sha256: str = ""
    source_unchanged: bool = False
    output_sha256: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    members: list[MemberReport] = field(default_factory=list)
    residual_members: list[int] = field(default_factory=list)  # flat indices (see MemberReport.index)
    unsupported_members: list[int] = field(default_factory=list)
    verification: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ validation

_DRIVE = re.compile(r"^[A-Za-z]:")


def _check_name(name: str) -> str:
    """Return the normalized POSIX path (without trailing '/') or raise."""
    if not name or "\x00" in name:
        raise ArchiveError("unsafe_member_name")
    if "\\" in name:
        raise ArchiveError("backslash_in_member_name")
    if name.startswith("/") or _DRIVE.match(name):
        raise ArchiveError("absolute_member_path")
    parts = [p for p in name.rstrip("/").split("/")]
    if any(p in ("", ".", "..") for p in parts):
        if ".." in parts:
            raise ArchiveError("path_traversal")
        raise ArchiveError("unsafe_member_name")  # empty or '.' components ('a//b', './a')
    return "/".join(parts)


def _ext(name: str) -> str:
    return PurePosixPath(name.rstrip("/")).suffix.lower()


class _Budget:
    def __init__(self, limits: ArchiveLimits):
        self.limits, self.members, self.total = limits, 0, 0

    def take(self, n_members: int, n_bytes: int) -> None:
        self.members += n_members
        self.total += n_bytes
        if self.members > self.limits.max_members:
            raise ArchiveError("member_count_limit")
        if self.total > self.limits.max_total_bytes:
            raise ArchiveError("total_size_limit")


def _validate(zf: zipfile.ZipFile, depth: int, budget: _Budget) -> list[zipfile.ZipInfo]:
    lim = budget.limits
    if depth > lim.max_depth:
        raise ArchiveError("depth_limit")
    infos = zf.infolist()
    seen: set[str] = set()
    files: set[str] = set()
    dirs: set[str] = set()
    for info in infos:
        norm = _check_name(info.filename)
        key = norm.casefold()
        if key in seen:
            raise ArchiveError("duplicate_member_path")
        seen.add(key)
        if info.flag_bits & 0x1:
            raise ArchiveError("encrypted_member")
        if info.file_size > lim.max_member_bytes:
            raise ArchiveError("member_size_limit")
        (dirs if info.is_dir() else files).add(key)
        parents = key.split("/")[:-1]
        for i in range(1, len(parents) + 1):
            dirs.add("/".join(parents[:i]))
    if files & dirs:
        raise ArchiveError("file_directory_collision")
    budget.take(len(infos), sum(i.file_size for i in infos))
    return infos


def _read_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with zf.open(info) as f:  # zipfile checks CRC at EOF
        data = f.read(limit + 1)
    if len(data) > limit or len(data) != info.file_size:
        raise ArchiveError("member_size_mismatch")
    return data


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


# ------------------------------------------------------------------ member transforms

SCAN_ENCODINGS = ("utf-8", "cp1252", "utf-16-le")


def scan_encodings(value: str) -> dict[str, bytes]:
    """Distinct, *strict*, non-empty byte encodings of ``value`` (an encoding that cannot represent
    the whole value is skipped: searching a degraded/empty byte string is not a match for the value)."""
    out: dict[str, bytes] = {}
    for enc in SCAN_ENCODINGS:
        try:
            b = value.encode(enc)
        except UnicodeEncodeError:
            continue
        if b and b not in out.values():
            out[enc] = b
    return out


def _scan(data: bytes, idmap: IdentityMap) -> dict[int, int]:
    """Exact raw occurrences per identity index: max over the value's strict encodings (no double
    counting across encodings of the same value). Overlap between *different* values is not removed
    here (conservative); ``zip_diagnostic`` reports it separately."""
    out: dict[int, int] = {}
    for i, old in enumerate(idmap.strings):
        n = max((data.count(b) for b in scan_encodings(old).values()), default=0)
        if n:
            out[i] = n
    return out


def _scan_legacy(data: bytes, idmap: IdentityMap) -> dict[int, int]:
    """Attempt-1 / pre-fix semantics (``errors='ignore'``), kept only so diagnostics can reproduce the
    old exact metric. An unencodable value degraded to a shorter or empty needle (b"" matches
    ``len(data)+1`` times)."""
    out: dict[int, int] = {}
    for i, old in enumerate(idmap.strings):
        n = max(data.count(old.encode(enc, "ignore")) for enc in SCAN_ENCODINGS)
        if n:
            out[i] = n
    return out


def _transform_member(kind: str, data: bytes, idmap: IdentityMap, work: Path, rep: MemberReport) -> bytes:
    """Return bytes to store for this member and fill ``rep``. Never raises for content errors."""
    src = work / f"{uuid.uuid4().hex}.in"
    dst = work / f"{uuid.uuid4().hex}.out"
    src.write_bytes(data)
    try:
        if kind == "step":
            from .step_transform import PASS2_FIELDS, transform_step
            from .step_data_transform import transform_descriptive, transform_identities
            r = transform_step(src, dst, idmap, fields=PASS2_FIELDS)
            rep.replacements = dict(r.replacements)
            if r.outcome in ("transformed_validated", "inspected_no_identifiers"):
                stage = dst if r.outcome == "transformed_validated" else src
                if r.outcome == "transformed_validated":
                    rep.changes.step_header_replacements = sum(r.replacements.values())
                # Attempt 2: DATA person/organization positions (exact approved values only)
                dd = work / f"{uuid.uuid4().hex}.data"
                try:
                    d = transform_identities(stage, dd, dict(idmap.strings))
                    if d.outcome == "transformed_validated":
                        rep.changes.step_data_replacements = sum(d.replacements.values())
                        os.replace(dd, dst)
                        stage = dst
                finally:
                    dd.unlink(missing_ok=True)
                # Attempt 2b: scoped descriptive product-structure literals (boundary-aware, verified)
                dd = work / f"{uuid.uuid4().hex}.descr"
                try:
                    g = transform_descriptive(stage, dd, dict(idmap.strings))
                    if g.outcome == "transformed_validated":
                        rep.changes.step_descriptive_replacements = sum(g.replacements.values())
                        os.replace(dd, dst)
                        stage = dst
                finally:
                    dd.unlink(missing_ok=True)
                if stage is dst:
                    rep.outcome = "transformed_validated"
                    out = dst.read_bytes()
                    rep.residual = _scan(out, idmap)  # independent raw rescan; genuine leftovers stay visible
                    return out
                rep.outcome = "inspected_no_identifiers"
                rep.residual = _scan(data, idmap)
            else:
                rep.outcome, rep.reason = "identifiers_unmodifiable", r.reason or r.outcome
                rep.residual = dict(r.unmodifiable) or _scan(data, idmap)
            return data
        if kind == "pdf":
            from .pdf_transform import transform_pdf
            r = transform_pdf(src, dst, idmap)
            rep.replacements = dict(r.matches)
            if r.ok and (r.matches or r.changes.total):
                from .pdf_residual import inspect_pdf
                ind, base = inspect_pdf(dst, list(idmap.strings)), inspect_pdf(src, [])
                if (ind.ok and base.ok and not ind.total
                        and (ind.pages, ind.dimensions) == (base.pages, base.dimensions)):
                    out = dst.read_bytes()
                    rep.outcome = "transformed_validated"
                    rep.changes = r.changes
                    return out
                rep.outcome, rep.reason = "identifiers_unmodifiable", "pdf_independent_residual"
                rep.residual = dict(ind.residual_by_index) or _scan(data, idmap)
                return data
            if r.ok:
                rep.outcome = "inspected_no_identifiers"
                rep.residual = _scan(data, idmap)
                return data
            rep.outcome, rep.reason = "identifiers_unmodifiable", "pdf_validation_failed"
            rep.residual = dict(r.residual) or _scan(data, idmap)
            return data
        if kind == "dxf":
            from .dxf_transform import transform_dxf
            r = transform_dxf(src, dst, idmap)
            rep.replacements = dict(r.text_replacements)
            changed = bool(r.text_replacements or r.header_replacements or r.layers_renamed or r.blocks_renamed)
            if r.ok and changed:
                rep.outcome = "transformed_validated"
                out = dst.read_bytes()
                rep.residual = dict(r.residual_raw)
                rep.changes.dxf_replacements = r.verified_identity_fields_changed
                return out
            if r.ok:
                rep.outcome = "inspected_no_identifiers"
                rep.residual = dict(r.residual_raw)
                return data
            rep.outcome, rep.reason = "identifiers_unmodifiable", "dxf_validation_failed"
            rep.residual = dict(r.residual_in_entities) or _scan(data, idmap)
            return data
    except Exception as exc:  # malformed member: keep original bytes, report, never claim success
        rep.outcome, rep.reason = "identifiers_unmodifiable", f"parse_error:{type(exc).__name__}"
        rep.residual = _scan(data, idmap)
        return data
    finally:
        for p in (src, dst):
            p.unlink(missing_ok=True)
    raise AssertionError(kind)


# ------------------------------------------------------------------ rebuild

def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    n = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    n.compress_type = info.compress_type if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) \
        else zipfile.ZIP_DEFLATED
    n.external_attr, n.create_system, n.comment = info.external_attr, info.create_system, info.comment
    return n


def _scope_member(scope, kind: str | None, data: bytes, idmap: IdentityMap | None, work: Path,
                  rep: MemberReport) -> bytes | None:
    """Attempt 2c: PDF scoped rewrite / CFB saved account through the frozen shared map.

    Returns the bytes to store, or ``None`` to continue with the legacy member path (nothing resolved)."""
    from .ole_transform import is_cfb
    if is_cfb(data[:8]):
        r = scope.ole(data)
        if r.status == "transformed":
            rep.outcome = "transformed_validated"
            rep.changes.ole_property_replacements = r.property_writes
            rep.changes.ole_cross_stream_replacements = r.cross_stream_writes
            if r.unresolved:
                rep.residual = {-1: sum(r.unresolved.values())}
            return r.data
        if r.status == "failed":
            rep.outcome, rep.reason = "identifiers_unmodifiable", r.reason[:80]
            rep.residual = {-1: max(1, sum(r.unresolved.values()))}
        else:
            rep.outcome = "inspected_no_identifiers"
        return data
    if kind != "pdf":
        return None
    try:
        res, new = scope.pdf_bytes(data, work, dict(idmap.strings) if idmap is not None else None)
    except Exception as exc:  # noqa: BLE001
        rep.outcome, rep.reason = "identifiers_unmodifiable", f"pdf_scoped_exception:{type(exc).__name__}"
        rep.residual = {-1: 1}
        return data
    if res.ok and new is not None:
        from .pdf_residual import inspect_pdf
        tmp = work / f"{uuid.uuid4().hex}.chk.pdf"
        base = work / f"{uuid.uuid4().hex}.base.pdf"
        tmp.write_bytes(new)
        base.write_bytes(data)
        try:
            vals = sorted(set(res.resolved_values) | set(idmap.strings if idmap is not None else {}))
            ind, b0 = inspect_pdf(tmp, vals), inspect_pdf(base, [])
        finally:
            tmp.unlink(missing_ok=True)
            base.unlink(missing_ok=True)
        if ind.ok and b0.ok and not ind.total and (ind.pages, ind.dimensions) == (b0.pages, b0.dimensions):
            rep.outcome = "transformed_validated"
            from .twopass import _pdf_counts
            rep.changes = _pdf_counts(res)
            left = sum(res.unresolved.values()) + sum(res.residual.values())
            if left:
                rep.residual = {-1: left}
            return new
        rep.outcome, rep.reason = "identifiers_unmodifiable", "pdf_scoped:independent_check_failed"
        rep.residual = {-1: max(1, ind.total)}
        return data
    if res.reason == "no_resolved_identities":
        return None
    rep.outcome, rep.reason = "identifiers_unmodifiable", f"pdf_scoped:{res.reason}"[:80]
    rep.residual = {-1: max(1, sum(res.unresolved.values()))}
    return data


class _Ctx:
    def __init__(self, idmap: IdentityMap | None, limits: ArchiveLimits, work: Path, scope=None):
        self.idmap, self.limits, self.work, self.scope = idmap, limits, work, scope
        self.budget = _Budget(limits)
        self.next_index = 0
        self.flat: list[MemberReport] = []


def _rebuild(zin: zipfile.ZipFile, zout: zipfile.ZipFile, depth: int, ctx: _Ctx,
             expected: dict[str, tuple[int, str | None]]) -> list[MemberReport]:
    """Copy/transform every member of ``zin`` into ``zout``.

    ``expected`` collects name -> (crc, sha256 of new bytes or None) for verification.
    """
    infos = _validate(zin, depth, ctx.budget)
    reports = []
    for info in infos:
        rep = MemberReport(index=ctx.next_index, depth=depth,
                           name_sha256=hashlib.sha256(info.filename.encode("utf-8")).hexdigest(),
                           ext=_ext(info.filename), is_dir=info.is_dir(), outcome="", bytes_in=info.file_size)
        ctx.next_index += 1
        ctx.flat.append(rep)
        reports.append(rep)
        if ctx.idmap is not None:
            rep.name_contains_identity = bool(_scan(info.filename.encode("utf-8"), ctx.idmap))
        data = b"" if info.is_dir() else _read_bounded(zin, info, ctx.limits.max_member_bytes)
        new = data
        kind = SUPPORTED.get(rep.ext)
        if info.is_dir():
            rep.outcome = "directory_preserved"
        elif _is_symlink(info):
            rep.outcome, rep.reason = "unsupported_passthrough", "symlink"
        elif is_metadata_sidecar(info.filename):
            rep.outcome, rep.reason = "unsupported_passthrough", "metadata_sidecar"
            if ctx.idmap is not None:
                rep.residual = _scan(data, ctx.idmap)
        elif rep.ext in NESTED:
            new = _nested(data, depth + 1, ctx, rep)
        elif ctx.scope is not None and (got := _scope_member(ctx.scope, kind, data, ctx.idmap, ctx.work,
                                                            rep)) is not None:
            new = got
        elif kind is None:
            rep.outcome = "unsupported_passthrough"
            if ctx.idmap is not None:
                rep.residual = _scan(data, ctx.idmap)
        elif ctx.idmap is None:
            rep.outcome = "inspected_no_map"
        else:
            new = _transform_member(kind, data, ctx.idmap, ctx.work, rep)
        rep.bytes_out = len(new)
        zout.writestr(_copy_info(info), new)
        expected[info.filename] = (zlib.crc32(new) & 0xFFFFFFFF,
                                   hashlib.sha256(new).hexdigest() if new is not data else None)
    return reports


def _nested(data: bytes, depth: int, ctx: _Ctx, rep: MemberReport) -> bytes:
    import io
    rep.outcome = "nested_archive"
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        rep.outcome, rep.reason = "unsupported_passthrough", "bad_nested_zip"
        if ctx.idmap is not None:
            rep.residual = _scan(data, ctx.idmap)
        return data
    buf = io.BytesIO()
    expected: dict[str, tuple[int, str | None]] = {}
    with zin, zipfile.ZipFile(buf, "w") as zout:
        rep.children = _rebuild(zin, zout, depth, ctx, expected)
    out = buf.getvalue()
    _verify_zip(io.BytesIO(out), [i.filename for i in zin.infolist()], expected)
    if not any(c.outcome == "transformed_validated" for c in _walk(rep.children)):
        return data  # nothing changed inside: keep original nested bytes exactly
    return out


def _walk(reps):
    for r in reps:
        yield r
        yield from _walk(r.children)


def _verify_zip(f, names: list[str], expected: dict[str, tuple[int, str | None]]) -> dict[str, bool]:
    v = {}
    with zipfile.ZipFile(f) as z:
        infos = z.infolist()
        v["names_equal"] = [i.filename for i in infos] == names
        v["count_equal"] = len(infos) == len(names)
        v["crc_readable"] = z.testzip() is None
        ok_crc = ok_sha = True
        for i in infos:
            crc, sha = expected.get(i.filename, (None, None))
            ok_crc &= i.CRC == crc
            if sha is not None:
                ok_sha &= hashlib.sha256(z.read(i)).hexdigest() == sha
        v["crc_matches_expected"] = ok_crc
        v["transformed_members_match"] = ok_sha
    if not all(v.values()):
        raise ArchiveError("rebuild_verification_failed")
    return v


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def transform_zip(src: str | Path, dst: str | Path, idmap: IdentityMap | None = None, *,
                  limits: ArchiveLimits = ArchiveLimits(), overwrite: bool = False, scope=None) -> ArchiveResult:
    """Rebuild ``src`` into ``dst``; see module docstring. ``dst`` exists only if ``ok``."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = ArchiveResult()
    res.source_sha256 = _sha_file(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:10]}.partial")
    try:
        with tempfile.TemporaryDirectory(prefix="deid-zip-") as work:
            ctx = _Ctx(idmap, limits, Path(work), scope)
            try:
                with zipfile.ZipFile(src) as zin:
                    # validate the whole tree up-front (nested names/limits) before decompressing
                    _prevalidate(zin, 0, _Budget(limits))
                    expected: dict[str, tuple[int, str | None]] = {}
                    with tmp.open("wb") as fo:
                        with zipfile.ZipFile(fo, "w") as zout:
                            zout.comment = zin.comment
                            res.members = _rebuild(zin, zout, 0, ctx, expected)
                        fo.flush()
                        os.fsync(fo.fileno())
                    names = [i.filename for i in zin.infolist()]
                res.verification = _verify_zip(tmp, names, expected)
            except ArchiveError as exc:
                res.reason = exc.reason
                return res
            except (zipfile.BadZipFile, zlib.error, EOFError):
                res.reason = "bad_zip"
                return res
        res.source_unchanged = _sha_file(src) == res.source_sha256
        if not res.source_unchanged:
            res.reason = "source_changed_during_read"
            return res
        if dst.exists() and not overwrite:
            res.reason = "output_exists"
            return res
        os.replace(tmp, dst)
        res.ok, res.output_written = True, True
        res.output_sha256 = _sha_file(dst)
        for r in ctx.flat:
            res.counts[r.outcome] = res.counts.get(r.outcome, 0) + 1
            if r.residual or r.name_contains_identity or r.outcome == "identifiers_unmodifiable":
                res.residual_members.append(r.index)
            if r.outcome == "unsupported_passthrough":
                res.unsupported_members.append(r.index)
        res.complete = (idmap is not None and not res.residual_members and not res.unsupported_members
                        and not res.counts.get("inspected_no_map"))
        return res
    finally:
        tmp.unlink(missing_ok=True)
        if not res.source_unchanged and not res.reason:
            res.source_unchanged = _sha_file(src) == res.source_sha256


def _prevalidate(zf: zipfile.ZipFile, depth: int, budget: _Budget) -> None:
    """Validate names/limits for this archive and any nested ``.zip`` members.

    Nested archives must be decompressed to read their directories; each is bounded by
    the per-member limit and already counted against the total budget.
    """
    import io
    for info in _validate(zf, depth, budget):
        if not info.is_dir() and _ext(info.filename) in NESTED:
            data = _read_bounded(zf, info, budget.limits.max_member_bytes)
            try:
                inner = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                continue
            with inner:
                _prevalidate(inner, depth + 1, budget)
