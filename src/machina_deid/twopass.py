"""Phase 5C: two-pass workflow connecting discovery to the persistent identity catalog.

Pass 1 (:func:`discover_batch`): format-dispatch discovery (PDF/DXF/STEP and PDF/DXF/STEP members of
bounded ZIP archives) returns :class:`discovery.Candidate` objects *in memory*; they are recorded in the
catalog with source sha256/path context (raw values are reduced to keyed digests by the catalog), and a
conservative auto-approval policy (:func:`auto_decision`) records approve/review decisions with reasons.

Pass 2 (:func:`transform_file`): candidates are re-discovered from the *current* file and
:func:`build_identity_map` asks the catalog which exact values are approved, returning an in-memory
:class:`IdentityMap` (strings only). No raw map file is ever written. The existing transformers are used
unchanged. For STEP, FILE_NAME header strings use the untyped map, and the only DATA rewrite is
``ORGANIZATION.name`` (:mod:`step_data_transform`), only for values approved under the typed ``org`` kind.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import archive as ar
from .catalog import IdentityCatalog
from .classify import classify_path
from .discovery import Candidate, discover_dxf, discover_path, discover_pdf, discover_step
from .idmap import IdentityMap
from .transform_counts import TransformCounts

SUPPORTED = ("pdf", "dxf", "step", "zip")
_AUTO_LOCATIONS = re.compile(r"^(?:pdf:metadata\.author|step:FILE_NAME\.(?:author|organization)\[\d+\])$")
_NEVER_AUTO = {"file_name", "project", "part", "description", "text_label_value", "path_component",
               "software_or_authorization", "timestamp", "org_name"}
DATA_ORG_KIND = "org_name"  # STEP DATA ORGANIZATION.name candidates; replaced only via typed ``org`` approval


@dataclass
class DiscoveredFile:
    """Discovery result for one file or archive member. ``candidates`` hold raw values: memory only."""

    path: str  # manifest-relative path; members as "archive.zip!member/path"
    format: str | None
    sha256: str
    status: str  # ok | unsupported | failed
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list, repr=False)


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def detect_format(path: str | Path) -> str | None:
    c = classify_path(path)
    if c.status in ("verified", "unverified") and c.format in SUPPORTED:
        return c.format
    return None


def _discover_one(path: Path, fmt: str) -> list[Candidate]:
    if fmt == "pdf":
        return discover_pdf(path)
    if fmt == "dxf":
        return discover_dxf(path)
    if fmt == "step":
        return discover_step(path)
    raise ValueError("unsupported format")


def _member_fmt(name: str) -> str | None:
    ext = PurePosixPath(name).suffix.lower()
    return "zip" if ext in ar.NESTED else ar.SUPPORTED.get(ext)


def _discover_zip(zf: zipfile.ZipFile, prefix: str, depth: int, budget, work: Path, out: list[DiscoveredFile]):
    for info in ar._validate(zf, depth, budget):
        if info.is_dir():
            continue
        name = f"{prefix}!{info.filename}"
        if ar.is_metadata_sidecar(info.filename):
            out.append(DiscoveredFile(name, None, "", "unsupported", "metadata_sidecar"))
            continue
        fmt = _member_fmt(info.filename)
        if fmt is None or ar._is_symlink(info):
            out.append(DiscoveredFile(name, None, "", "unsupported", "unsupported_member"))
            continue
        data = ar._read_bounded(zf, info, budget.limits.max_member_bytes)
        sha = _sha_bytes(data)
        if fmt == "zip":
            try:
                inner = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                out.append(DiscoveredFile(name, "zip", sha, "failed", "bad_zip"))
                continue
            with inner:
                _discover_zip(inner, name, depth + 1, budget, work, out)
            continue
        tmp = work / f"m{len(out)}{PurePosixPath(info.filename).suffix.lower()}"
        tmp.write_bytes(data)
        try:
            out.append(DiscoveredFile(name, fmt, sha, "ok", candidates=_discover_one(tmp, fmt)))
        except Exception as exc:  # noqa: BLE001 - parser errors are reported by type only
            out.append(DiscoveredFile(name, fmt, sha, "failed", f"discovery_error:{type(exc).__name__}"))
        finally:
            tmp.unlink(missing_ok=True)


def discover_file(path: str | Path, rel_path: str | None = None, *, include_paths: bool = False,
                  limits: ar.ArchiveLimits = ar.ArchiveLimits()) -> list[DiscoveredFile]:
    """Discover candidates in one file. ZIPs yield one entry per member (bounded by ``limits``)."""
    path = Path(path)
    rel = rel_path or path.name
    fmt = detect_format(path)
    if fmt is None:
        return [DiscoveredFile(rel, None, _sha_file(path), "unsupported", "unsupported_format")]
    sha = _sha_file(path)
    if fmt == "zip":
        out: list[DiscoveredFile] = []
        try:
            with tempfile.TemporaryDirectory(prefix="deid-disc-") as work, zipfile.ZipFile(path) as zf:
                _discover_zip(zf, rel, 0, ar._Budget(limits), Path(work), out)
        except ar.ArchiveError as exc:
            return [DiscoveredFile(rel, "zip", sha, "failed", exc.reason)]
        except (zipfile.BadZipFile, EOFError, OSError):
            return [DiscoveredFile(rel, "zip", sha, "failed", "bad_zip")]
        return out
    try:
        cands = _discover_one(path, fmt)
    except Exception as exc:  # noqa: BLE001
        return [DiscoveredFile(rel, fmt, sha, "failed", f"discovery_error:{type(exc).__name__}")]
    if include_paths:
        cands += discover_path(rel)
    return [DiscoveredFile(rel, fmt, sha, "ok", candidates=cands)]


# ---------------------------------------------------------------- auto approval


def auto_decision(c: Candidate) -> tuple[str, str]:
    """Conservative policy -> ``(status, reason)``; status is ``approved`` or ``review``.

    Only high-confidence ``person_or_org`` candidates from structured PDF Info ``/Author`` or STEP
    ``FILE_NAME`` author/organization tuples are approved. Everything else requires review.
    """
    if c.kind in _NEVER_AUTO or c.kind != "person_or_org":
        return "review", f"kind {c.kind if re.fullmatch(r'[a-z_]{1,40}', c.kind) else 'unknown'} requires review"
    if c.confidence != "high":
        return "review", "person_or_org below high confidence"
    if not _AUTO_LOCATIONS.match(c.location):
        return "review", "person_or_org outside structured PDF/STEP author fields"
    return "approved", "auto: high-confidence structured " + ("PDF metadata author" if c.location.startswith("pdf:")
                                                              else "STEP FILE_NAME " + c.location.split(".")[1].split("[")[0])


def auto_kind(c: Candidate) -> str | None:
    """Return a typed kind only where the source field establishes its semantics."""
    if c.location.startswith("step:FILE_NAME.organization["):
        return "org"
    if c.location.startswith("step:FILE_NAME.author["):
        return "person"
    # PDF /Author is commonly either a person or an organization; do not guess.
    return None


def apply_auto(cat: IdentityCatalog, cands: list[Candidate]) -> dict[str, int]:
    """Record auto decisions (never overriding existing decisions). Returns resulting status counts
    per candidate value (deduplicated by exact normalized value)."""
    out = {"approved": 0, "review": 0, "rejected": 0, "conflict": 0}
    seen: set[str] = set()
    for c in cands:
        key = cat.value_key(c.value)
        if key in seen:
            continue
        seen.add(key)
        status, reason = auto_decision(c)
        decided = cat.value_decided(c.value)
        if decided:  # an existing decision under any kind wins; auto never overrides or conflicts
            res = next(s for s in ("approved", "conflict", "rejected", "review") if s in decided)
        else:
            kind = auto_kind(c) if status == "approved" else None
            if kind is None:
                # The candidate is untrusted text and can literally equal a word in a
                # human-readable reason.  Use an opaque keyed reference so the catalog's
                # value-leak guard remains meaningful without rejecting valid candidates.
                status, reason = "review", "ambiguous_source_field:" + key[:16]
                res = cat.decide("text", c.value, status, reason=reason, source="auto")
            else:
                res = cat.decide(kind, c.value, status, reason=reason, source="auto")
        out[res] += 1
    return out


@dataclass
class BatchResult:
    files: int = 0
    members: int = 0
    ok: int = 0
    unsupported: int = 0
    failed: int = 0
    candidates: int = 0
    new_observations: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    skipped_over_budget: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def discover_batch(cat: IdentityCatalog, items: list[tuple[str | Path, str]], *, limit: int,
                   max_bytes: int | None = None, auto: bool = True, include_paths: bool = False) -> BatchResult:
    """Bounded discovery pass over ``(physical_path, rel_path)`` items.

    Each file is recorded in its own transaction, so an interrupted batch keeps completed files and a
    rerun is idempotent (duplicate observations are ignored; existing decisions are never overridden).
    A failing file is counted and the batch continues.
    """
    if limit <= 0 or limit > 5000:
        raise ValueError("limit must be in 1..5000")
    res, used = BatchResult(), 0
    for phys, rel in items[:limit]:
        phys = Path(phys)
        size = phys.stat().st_size
        if max_bytes is not None and used + size > max_bytes:
            res.skipped_over_budget += 1
            continue
        used += size
        res.files += 1
        for d in discover_file(phys, rel, include_paths=include_paths):
            res.members += 1
            if d.status != "ok":
                setattr(res, d.status, getattr(res, d.status) + 1)
                if d.status == "failed":
                    res.failures[d.reason] = res.failures.get(d.reason, 0) + 1
                continue
            res.ok += 1
            res.candidates += len(d.candidates)
            res.new_observations += cat.record_candidates(d.candidates, source_sha256=d.sha256, path=d.path)
            if auto:
                for k, v in apply_auto(cat, d.candidates).items():
                    res.decisions[k] = res.decisions.get(k, 0) + v
    return res


# ---------------------------------------------------------------- transformation


@dataclass
class MapStats:
    candidates: int = 0
    approved_values: int = 0
    unapproved_values: int = 0


def build_identity_map(cat: IdentityCatalog, candidates: list[Candidate]) -> tuple[IdentityMap, MapStats]:
    """In-memory map of only approved exact candidate strings -> deterministic pseudonyms (strings only:
    layers/blocks are never renamed by this path)."""
    strings: dict[str, str] = {}
    st = MapStats(candidates=len(candidates))
    seen: set[str] = set()
    for c in candidates:
        if c.kind == DATA_ORG_KIND:  # handled only by build_org_map (typed)
            continue
        if c.value in seen:
            continue
        seen.add(c.value)
        hit = cat.approved_pseudonym(c.value)
        if hit is None or hit[1] == c.value:
            st.unapproved_values += 1
            continue
        strings[c.value] = hit[1]
        st.approved_values += 1
    return IdentityMap(strings=strings), st


def build_org_map(cat: IdentityCatalog, candidates: list[Candidate]) -> tuple[dict[str, str], int]:
    """Exact decoded DATA ORGANIZATION names -> pseudonym, only where approved under kind ``org``.
    Returns ``(map, approved_under_other_kind)``; the latter are left unchanged and reported as residual."""
    out: dict[str, str] = {}
    other = 0
    for c in candidates:
        if c.kind != DATA_ORG_KIND or c.value in out:
            continue
        hit = cat.approved_pseudonym(c.value, kind="org")
        if hit is not None and hit[1] != c.value:
            out[c.value] = hit[1]
        elif cat.approved_pseudonym(c.value) is not None:
            other += 1
    return out, other


def _transform_step(src: Path, dst: Path, idmap: IdentityMap, org_map: dict[str, str], out, overwrite: bool):
    """Header stage (FILE_NAME, untyped map) then DATA stage (ORGANIZATION.name, typed org map)."""
    from .step_data_transform import transform_organizations
    from .step_transform import transform_step
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    stage = src
    with tempfile.TemporaryDirectory(prefix="deid-step-", dir=dst.parent) as work:
        if idmap.strings:
            mid = Path(work) / "header.stp"
            r = transform_step(src, mid, idmap)
            out.residual_approved += _sum(r.unmodifiable) + _sum(r.mapped_present_in_unrequested_fields)
            if r.outcome == "transformed_validated":
                out.changes.step_header_replacements = _sum(r.replacements)
                stage = mid
            elif not r.ok:
                out.reason = r.reason or r.outcome
                return False
        if org_map:
            d = transform_organizations(stage, dst, org_map, overwrite=overwrite)
            out.residual_approved += d.unmodifiable
            if d.outcome == "transformed_validated":
                out.changes.step_data_replacements = _sum(d.replacements)
                return True
            if not d.ok:
                out.reason = "data:" + (d.reason or d.outcome)
                return False
        if stage is src:
            out.reason = "no_exact_match_in_transformable_fields"
            return False
        os.replace(stage, dst)
        return True


def _independent_pdf_check(src: Path, dst: Path, values: list[str], out) -> bool:
    """Rescan the written PDF with the independent inspector; on any residual approved value, missing
    coverage, or page geometry change, remove the output and fail. Records redacted counts only."""
    from .pdf_residual import inspect_pdf
    rep = inspect_pdf(dst, values)
    before = inspect_pdf(src, [])
    out.independent_residual = {k: v for k, v in rep.residual.items() if v}
    out.residual_approved += rep.total
    reason = ("independent_inspection_incomplete" if not rep.ok or not before.ok
              else "independent_geometry_mismatch" if (rep.pages, rep.dimensions) != (before.pages, before.dimensions)
              else "independent_residual" if rep.total else "")
    if reason:
        out.reason = reason
        dst.unlink(missing_ok=True)  # our own output only; never claimed clean
        return False
    return True


@dataclass
class TransformOutcome:
    path: str
    format: str | None
    status: str  # transformed | no_approved_identities | unsupported | failed
    reason: str = ""
    fields_replaced: int = 0
    residual_approved: int = 0
    approved_values: int = 0
    data_organizations_replaced: int = 0
    source_unchanged: bool = True
    output_written: bool = False
    independent_residual: dict[str, int] = field(default_factory=dict)  # category -> count (PDF rescan)
    fully_clean: bool = False  # transformed AND no residual approved value detected anywhere

    changes: TransformCounts = field(default_factory=TransformCounts)
    archive_replacements: int = 0
    nested_archive_replacements: int = 0

    @property
    def total_verified_identity_fields_changed(self) -> int:
        return self.changes.total

    def replacement_counts(self) -> dict[str, int]:
        return {**asdict(self.changes), "archive_replacements": self.archive_replacements,
                "nested_archive_replacements": self.nested_archive_replacements,
                "total_verified_identity_fields_changed": self.total_verified_identity_fields_changed}

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        del data["changes"]
        return {**data, **self.replacement_counts()}


def _sum(d: dict) -> int:
    return sum(d.values()) if d else 0


def transform_file(cat: IdentityCatalog, src: str | Path, dst: str | Path, rel_path: str | None = None, *,
                   overwrite: bool = False) -> TransformOutcome:
    """Pass 2 for one file: rediscover current candidates, build the approved in-memory map, transform."""
    src, dst = Path(src), Path(dst)
    rel = rel_path or src.name
    before = _sha_file(src)
    found = discover_file(src, rel)
    fmt = detect_format(src)
    out = TransformOutcome(rel, fmt, "failed")
    if fmt is None:
        out.status, out.reason = "unsupported", "unsupported_format"
        return out
    if any(d.status == "failed" for d in found) and fmt != "zip":
        out.reason = found[0].reason
        return out
    cands = [c for d in found for c in d.candidates]
    idmap, st = build_identity_map(cat, cands)
    org_map, org_other = build_org_map(cat, cands) if fmt == "step" else ({}, 0)
    out.approved_values = st.approved_values + len(org_map)
    out.residual_approved = org_other
    if not idmap.strings and not org_map:
        out.status, out.reason = "no_approved_identities", "nothing approved for current candidates"
        out.source_unchanged = _sha_file(src) == before
        return out
    try:
        if fmt == "pdf":
            from .pdf_transform import transform_pdf
            r = transform_pdf(src, dst, idmap, overwrite=overwrite)
            ok, out.residual_approved = r.ok, _sum(r.residual)
            out.changes = r.changes
            if ok:
                ok = _independent_pdf_check(src, dst, list(idmap.strings), out)
        elif fmt == "dxf":
            from .dxf_transform import transform_dxf
            r = transform_dxf(src, dst, idmap, overwrite=overwrite)
            ok = r.ok
            out.changes.dxf_replacements = r.verified_identity_fields_changed
            out.residual_approved = _sum(r.residual_in_entities) + _sum(r.residual_raw)
        elif fmt == "step":
            dst.parent.mkdir(parents=True, exist_ok=True)
            ok = _transform_step(src, dst, idmap, org_map, out, overwrite)
        else:
            r = ar.transform_zip(src, dst, idmap, overwrite=overwrite)
            ok = r.ok
            flat = list(ar._walk(r.members))
            if ok:
                for member in flat:
                    if member.outcome == "transformed_validated":
                        out.changes.add(member.changes)
                        if member.depth:
                            out.nested_archive_replacements += member.changes.total
                out.archive_replacements = out.changes.total
            out.residual_approved = sum(_sum(m.residual) for m in flat)
            out.reason = r.reason
    except (OSError, ValueError) as exc:
        ok, out.reason = False, f"transform_error:{type(exc).__name__}"
    out.output_written = dst.exists() and ok
    if not out.output_written:
        out.changes = TransformCounts()
    out.fields_replaced = out.changes.total
    out.data_organizations_replaced = out.changes.step_data_replacements
    out.status = "transformed" if ok else "failed"
    if not ok and not out.reason:
        out.reason = "transformer_validation_failed"
    out.source_unchanged = _sha_file(src) == before
    out.fully_clean = (ok and out.output_written and out.residual_approved == 0
                       and not any(out.independent_residual.values()))
    return out
