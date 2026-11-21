"""Durable, bounded, content-addressed processing of a manifest batch.

Per manifest path the pipeline: register in SQLite -> claim -> hash the source ->
(look up / build) one content-addressed (CAS) artifact per ``(source sha256, variant)`` ->
materialize that artifact at the manifest-relative output path (hardlink when safe,
copy otherwise) -> hash the output -> record validation.

Safety properties
- Sources are opened read-only and never modified or deleted; the output root and work
  directory must not overlap the source root (or each other).
- STEP files are rewritten only in explicitly mapped FILE_NAME header strings (see
  ``step_transform``); DATA is byte-identical. No header match -> passed through with
  outcome ``inspected_no_identifiers``; unreplaceable match -> nothing materialized.
- Every record carries an explicit ``outcome``: transformed_validated, inspected_no_identifiers,
  unsupported_passthrough, identifiers_unmodifiable or failed_validation. Missing and mismatched
  sources are terminal failed-validation accounting rows, not silently dropped rows.
  Pass-through is never reported as de-identified.
- PDF/DXF are transformed only when an explicit identity map for that format is given;
  otherwise they are passed through unchanged with a ``no_identity_map`` limitation.
  Nothing is inferred or guessed.
- ZIP archives are always rebuilt and verified via ``archive.transform_zip`` (bounded,
  never extracted under member names). Without ``--archive-map`` supported members are
  copied unchanged and the record outcome is ``inspected_no_map`` (never a
  de-identification claim). With a map the archive outcome is ``transformed_validated`` /
  ``inspected_no_identifiers`` only when the archive result is ``complete``; residual
  identities fail the record (nothing materialized) and unsupported members leave it
  ``unsupported_passthrough``. The note records per-member outcome counts.
- Unsupported-but-valid files (everything else, including native SolidWorks/Inventor
  binaries) are copied byte-for-byte; the record carries ``disposition=passthrough`` and
  a limitation note. No native CAD rewriting is performed.
- Files whose content contradicts their extension (``mismatch``) and missing files are
  *not* copied; they are recorded as skipped with a reason.
- A transform that fails its own validation is recorded as failed and nothing is
  materialized. Validated outputs and CAS artifacts are never deleted or overwritten.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections import Counter
from pathlib import Path

from . import manifest as mf
from .classify import classify_path
from .idmap import IdentityMap, IdentityMapError, load as load_idmap
from .state import StateDB, StateError, output_path_for

HARD_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 5 * 1024**3
DEFAULT_MIN_FREE = 5 * 1024**3
_TRANSFORMERS = {"pdf", "dxf", "step", "zip"}


class ProcessError(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def check_layout(source_root: Path, output_root: Path, work_dir: Path) -> tuple[Path, Path, Path]:
    """Resolve and require source, output and work directories to be pairwise disjoint."""
    s, o, w = (Path(x).resolve() for x in (source_root, output_root, work_dir))
    for (na, a), (nb, b) in (((("source", s), ("output", o))), (("source", s), ("work", w)),
                             (("output", o), ("work", w))):
        if _inside(a, b) or _inside(b, a):
            raise ProcessError(f"{na} and {nb} directories must not overlap")
    if not s.is_dir():
        raise ProcessError("source root is not a directory")
    return s, o, w


def _source_path(root: Path, rel: str) -> Path | None:
    """Physical path for a manifest path, or None when missing/unsafe (symlink escape, non-file)."""
    try:
        cand = root.joinpath(*output_path_for("", rel).parts)
    except StateError:
        return None
    try:
        real = cand.resolve(strict=True)
    except OSError:
        return None
    if not _inside(real, root) or not real.is_file():
        return None
    return real


def ingest(db: StateDB, rows: list[mf.Row], source_root: Path) -> dict[str, int]:
    """Register manifest rows (idempotent); returns counters."""
    out = Counter()
    for r in rows:
        phys = _source_path(source_root, r.path)
        if phys is None:
            try:
                new = db.register(r.path, r.path, source_root / "MISSING", input_size=r.size,
                                  classification="missing")
            except StateError:
                out["rejected_unsafe_path"] += 1
                continue
        else:
            cls = classify_path(phys)
            fmt = cls.format if cls.status in ("verified", "unverified") else None
            new = db.register(r.path, r.path, phys, input_size=r.size,
                              classification=f"{cls.format}/{cls.status}",
                              transformer=fmt if fmt in _TRANSFORMERS else None)
        out["registered" if new else "already_registered"] += 1
    return dict(out)


def _variant(rec_transformer: str | None, maps: dict[str, tuple[IdentityMap, str]]) -> tuple[str, str | None]:
    """Return (variant key, transformer kind or None). Variant ties the artifact to the map bytes."""
    if rec_transformer in maps:
        return f"{rec_transformer}-{maps[rec_transformer][1][:16]}", rec_transformer
    if rec_transformer in _TRANSFORMERS:
        return f"{rec_transformer}-no-map", rec_transformer
    return "passthrough", None


def _archive_note(res, mapped: bool) -> str:
    parts = [f"archive_complete={int(bool(res.complete))}", f"map={int(mapped)}"]
    parts += [f"{k}={v}" for k, v in sorted(res.counts.items())]
    parts += [f"residual_members={len(res.residual_members)}",
              f"unsupported_members={len(res.unsupported_members)}"]
    return "archive:" + ";".join(parts)


def _build_archive(src: Path, sha: str, cas: Path, maps, tmp_dir: Path):
    from .archive import transform_zip
    mapped = "zip" in maps
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex[:12]}.zip"
    try:
        try:
            res = transform_zip(src, tmp, maps["zip"][0] if mapped else None)
        except Exception as exc:
            raise _Fail(f"archive_error_{type(exc).__name__}") from None
        if not res.ok:
            raise _Fail("archive_" + (res.reason or "rebuild_failed"))
        if res.source_sha256 != sha or not res.source_unchanged:
            raise _Fail("source_changed_during_read")
        if not all(res.verification.values()) or sha256_file(tmp) != res.output_sha256:
            raise _Fail("archive_rebuild_verification_failed")
        note = _archive_note(res, mapped)
        if not mapped:
            outcome = "unsupported_passthrough"
        elif res.complete:
            outcome = ("transformed_validated" if res.counts.get("transformed_validated")
                       else "inspected_no_identifiers")
        elif res.residual_members:
            raise _Fail("archive_residual_identifiers", "identifiers_unmodifiable")
        else:
            outcome = "unsupported_passthrough"  # members left unchanged; never complete
        disp = "transformed" if outcome == "transformed_validated" else "archive_rebuilt"
        _atomic_copy(tmp, cas)
        out_sha = sha256_file(cas)
        if out_sha != res.output_sha256:
            cas.unlink()
            raise _Fail("archive_output_hash_mismatch")
        return out_sha, disp, note, "archive_rebuilt_verified", outcome
    finally:
        for leftover in (tmp, *tmp_dir.glob(f".{tmp.name}.*.partial")):
            if leftover.exists():
                leftover.unlink()


def _ext_for(rel: str, kind: str | None) -> str:
    return f".{kind}" if kind else ".bin"


def _cas_path(cas_dir: Path, sha: str, variant: str, ext: str) -> Path:
    return cas_dir / sha[:2] / f"{sha}-{variant}{ext}"


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".deid-{uuid.uuid4().hex[:12]}.tmp")
    try:
        with src.open("rb") as i, tmp.open("wb") as o:
            shutil.copyfileobj(i, o, 1 << 20)
            o.flush()
            os.fsync(o.fileno())
            os.rename(tmp, dst)  # tmp is our own file
        # Make the rename itself durable before the database checkpoint commits.
        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _passthrough(src: Path, sha: str, cas: Path) -> str:
    _atomic_copy(src, cas)
    out_sha = sha256_file(cas)
    if out_sha != sha:
        cas.unlink()  # our own just-created copy
        raise _Fail("source_changed_during_copy")
    return out_sha


def _build_artifact(src: Path, sha: str, variant: str, kind: str | None, cas: Path,
                    maps: dict[str, tuple[IdentityMap, str]], tmp_dir: Path):
    """Create the CAS artifact; return (output_sha, disposition, note, validation, outcome) or raise _Fail."""
    if kind is None:
        return _passthrough(src, sha, cas), "passthrough", None, "bytes_identical", "unsupported_passthrough"
    if kind == "zip":
        return _build_archive(src, sha, cas, maps, tmp_dir)
    if kind not in maps:
        from . import discovery
        try:
            if kind == "pdf":
                candidates = discovery.discover_pdf(src)
            elif kind == "dxf":
                candidates = discovery.discover_dxf(src)
            else:
                candidates = discovery.discover_step(src)
        except Exception as exc:
            raise _Fail(f"inspection_error_{type(exc).__name__}") from None
        if candidates:
            return (_passthrough(src, sha, cas), "passthrough",
                    f"{kind}_identifiers_detected_without_explicit_map", "bytes_identical",
                    "identifiers_unmodifiable")
        return (_passthrough(src, sha, cas), "passthrough",
                f"{kind}_inspected_no_detected_identifiers", "bytes_identical",
                "inspected_no_identifiers")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex[:12]}.{kind}"
    try:
        if kind == "pdf":
            from .pdf_transform import transform_pdf as fn
        elif kind == "dxf":
            from .dxf_transform import transform_dxf as fn
        else:
            from .step_transform import transform_step as fn
        try:
            res = fn(src, tmp, maps[kind][0])
        except Exception as exc:  # corrupt/encrypted input etc.; message may contain content, keep type only
            raise _Fail(f"transform_error_{type(exc).__name__}") from None
        if kind == "step":
            if res.outcome == "inspected_no_identifiers":
                return (_passthrough(src, sha, cas), "passthrough", "step_header_inspected_no_explicit_match",
                        "bytes_identical", "inspected_no_identifiers")
            if res.outcome == "identifiers_unmodifiable":
                raise _Fail("identifiers_unmodifiable", "identifiers_unmodifiable")
            if res.outcome != "transformed_validated" or not res.ok:
                raise _Fail("step_" + (res.reason or "validation_failed"))
            if res.source_sha256 != sha:
                raise _Fail("source_changed_during_read")
        elif not res.ok:
            raise _Fail("validation_failed")
        _atomic_copy(tmp, cas)
        return sha256_file(cas), "transformed", None, "ok", "transformed_validated"
    finally:
        for leftover in (tmp, tmp.with_name(tmp.name + ".partial")):
            if leftover.exists():
                leftover.unlink()


class _Fail(Exception):
    def __init__(self, category: str, outcome: str = "failed_validation"):
        super().__init__(category)
        self.outcome = outcome


def _ensure_readonly(p: Path) -> None:
    """CAS files may be hardlinked into outputs; make them read-only so shared content cannot be mutated."""
    mode = p.stat().st_mode
    if mode & 0o222:
        os.chmod(p, mode & ~0o222)


def _materialize(cas: Path, target: Path, expected_sha: str, mode: str) -> str:
    """Create ``target`` from the CAS file; never overwrite a differing file. Returns link kind."""
    if os.path.lexists(target):
        if target.is_symlink() or not target.is_file() or sha256_file(target) != expected_sha:
            raise _Fail("output_exists_mismatch")
        return "existing"
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "auto":
        _ensure_readonly(cas)
        try:
            os.link(cas, target)  # atomic, refuses to overwrite
            return "hardlink"
        except OSError:
            pass  # cross-device, link-count limit, unsupported FS -> copy
    _atomic_copy(cas, target)
    if sha256_file(target) != expected_sha:
        target.unlink()  # our own file
        raise _Fail("output_hash_mismatch")
    return "copy"


def plan(manifest: str | Path, source_root: str | Path, *, limit: int, pdf_map: str | None = None,
         dxf_map: str | None = None, step_map: str | None = None, archive_map: str | None = None) -> dict:
    """Dry run: classify + hash the first ``limit`` rows and report what ``process`` would do. Writes nothing."""
    _bounds(limit, None)
    root = Path(source_root).resolve()
    rows, bad = mf.parse_manifest(manifest)
    rows = rows[:limit]
    kinds = {k for k, v in (("pdf", pdf_map), ("dxf", dxf_map), ("step", step_map)) if v} | {"zip"}
    for v in (pdf_map, dxf_map, step_map, archive_map):
        if v:
            load_idmap(v)  # validate only
    by_disp, groups, bytes_unique, bytes_total = Counter(), Counter(), 0, 0
    seen: set[tuple[str, str]] = set()
    for r in rows:
        phys = _source_path(root, r.path)
        if phys is None:
            by_disp["skipped:source_missing_or_unsafe"] += 1
            continue
        cls = classify_path(phys)
        if cls.status == "mismatch":
            by_disp["skipped:classification_mismatch"] += 1
            continue
        fmt = cls.format if cls.status in ("verified", "unverified") else None
        kind = fmt if fmt in kinds else None
        sha = sha256_file(phys)
        if kind == "zip":
            label = "archive:" + ("mapped" if archive_map else "inspect_no_map")
        else:
            label = "transform:" + kind if kind else "passthrough"
        by_disp[label] += 1
        key = (sha, label)
        groups[key] += 1
        size = phys.stat().st_size
        bytes_total += size
        if key not in seen:
            seen.add(key)
            bytes_unique += size
    return {"dry_run": True, "rows_considered": len(rows), "malformed_manifest_lines": bad,
            "planned": dict(by_disp), "unique_artifacts": len(seen),
            "alias_paths": sum(n - 1 for n in groups.values()), "bytes_all_paths": bytes_total,
            "bytes_unique": bytes_unique}


def _bounds(limit: int, max_bytes: int | None) -> None:
    if not isinstance(limit, int) or limit <= 0 or limit > HARD_MAX_FILES:
        raise ProcessError(f"limit must be between 1 and {HARD_MAX_FILES}")
    if max_bytes is not None and max_bytes <= 0:
        raise ProcessError("max_bytes must be positive")


def process(manifest: str | Path, source_root: str | Path, output_root: str | Path, work_dir: str | Path, *,
            limit: int, max_bytes: int | None = DEFAULT_MAX_BYTES, pdf_map: str | None = None,
            dxf_map: str | None = None, step_map: str | None = None, archive_map: str | None = None, link_mode: str = "auto", retry_failed: bool = False,
            min_free: int = DEFAULT_MIN_FREE) -> dict:
    """Ingest up to ``limit`` manifest rows and process up to ``limit`` pending records. Resumable."""
    _bounds(limit, max_bytes)
    if link_mode not in ("auto", "copy"):
        raise ProcessError("link_mode must be 'auto' or 'copy'")
    root, out_root, work = check_layout(Path(source_root), Path(output_root), Path(work_dir))
    maps: dict[str, tuple[IdentityMap, str]] = {}
    for kind, path in (("pdf", pdf_map), ("dxf", dxf_map), ("step", step_map), ("zip", archive_map)):
        if path:
            try:
                maps[kind] = (load_idmap(path), sha256_file(Path(path)))
            except (IdentityMapError, OSError) as exc:
                raise ProcessError(f"cannot load {kind} identity map ({type(exc).__name__})") from None
    rows, bad = mf.parse_manifest(manifest)
    out_root.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    cas_dir, tmp_dir = work / "cas", work / "tmp"
    db = StateDB(work / "state.sqlite")
    stats: Counter = Counter()
    try:
        stats["recovered_interrupted"] = db.recover_interrupted()
        stats["recovered_transformed"] = db.recover_checkpoints()
        if retry_failed:
            stats["requeued"] = db.requeue_failed()
        summary_ingest = ingest(db, rows[:limit], root)
        batch = db.select_batch(limit, max_bytes, include_untransformable=True)
        need = 2 * sum(r["input_size"] or 0 for r in batch)
        free = shutil.disk_usage(work).free
        if batch and free - min_free < need:
            db.recover_interrupted()  # release the claim; nothing was written
            raise ProcessError("insufficient free space for this batch plus reserve; lower --max-bytes")
        verified_cas: dict[str, bool] = {}
        for rec in batch:
            _process_one(db, rec, root, out_root, cas_dir, tmp_dir, maps, link_mode, stats, verified_cas)
        counts = db.counts()
    finally:
        db.close()
    return {"dry_run": False, "malformed_manifest_lines": bad, "ingest": summary_ingest,
            "claimed": len(batch), "this_run": dict(stats), "state_counts": counts}


def _process_one(db, rec, root, out_root, cas_dir, tmp_dir, maps, link_mode, stats, verified_cas) -> None:
    rid = rec["id"]
    cls = rec["classification"] or ""
    if cls.startswith("missing") or not Path(rec["source_physical_path"]).is_file():
        db.mark_skipped(rid, "source_missing_or_unsafe")
        stats["skipped"] += 1
        return
    if cls.endswith("/mismatch"):
        db.mark_skipped(rid, "classification_mismatch")
        stats["skipped"] += 1
        return
    src = Path(rec["source_physical_path"])
    try:
        if not _inside(src.resolve(), root):
            raise _Fail("source_outside_root")
        target = output_path_for(out_root, rec["source_rel_path"])
        if not _inside(target.parent.resolve(), out_root) or target.resolve() == src.resolve():
            raise _Fail("unsafe_output_path")
        if rec["input_size"] is not None and src.stat().st_size != rec["input_size"]:
            raise _Fail("size_mismatch_with_manifest")
        sha = sha256_file(src)
        if rec["input_sha256"] and rec["input_sha256"] != sha:
            raise _Fail("source_changed_since_ingest")
        db.set_input_sha256(rid, sha)
        variant, kind = _variant(rec["transformer"], maps)
        cas = _cas_path(cas_dir, sha, variant, _ext_for(rec["source_rel_path"], kind))
        row = db.get_content(sha, variant)
        reuse = False
        if row is not None and Path(row["cas_path"]).is_file():
            key = row["cas_path"]
            if key not in verified_cas:
                verified_cas[key] = sha256_file(Path(key)) == row["output_sha256"]
            reuse = verified_cas[key]
        if reuse:
            stats["cas_reused"] += 1
        else:
            if cas.exists():
                cas.unlink()  # unvalidated/corrupt CAS leftover of ours, never a validated output
            out_sha, disp, note, val, outcome = _build_artifact(src, sha, variant, kind, cas, maps, tmp_dir)
            if rec["transformer"] and kind is None:
                note = "no_identity_map_supplied"
            elif kind is None:
                note = "unsupported_format_unchanged"
            os.chmod(cas, 0o444)
            db.put_content(sha, variant, cas, out_sha, cas.stat().st_size, disp, note, val, outcome)
            row = db.get_content(sha, variant)
            verified_cas[str(cas)] = True
            stats["artifacts_built"] += 1
        link = _materialize(Path(row["cas_path"]), target, row["output_sha256"], link_mode)
        out_sha = row["output_sha256"] if link in ("hardlink", "existing") else sha256_file(target)
        if link == "existing":
            out_sha = sha256_file(target)
        outcome = row["outcome"] or ("transformed_validated" if row["disposition"] == "transformed"
                                     else "unsupported_passthrough")
        db.mark_transformed(rid, target, out_sha, disposition=row["disposition"], note=row["note"],
                            link_kind=link, outcome=outcome)
        db.mark_validated(rid, row["validation_status"])
        stats[f"{row['disposition']}"] += 1
        stats[f"outcome_{outcome}"] += 1
        stats[f"link_{link}"] += 1
    except _Fail as f:
        db.mark_failed(rid, str(f), outcome=f.outcome)
        stats["failed"] += 1
        stats[f"outcome_{f.outcome}"] += 1
    except OSError as exc:
        db.mark_failed(rid, f"os_error_{type(exc).__name__}", outcome="failed_validation")
        stats["failed"] += 1
        stats["outcome_failed_validation"] += 1
