#!/usr/bin/env python3
"""Phase 5D frozen benchmark: deterministic stratified selection + discovery baseline (+ optional transform).

Two steps, reusable across code changes so results stay comparable:

  --freeze  Select ~50-100 *already-downloaded* challenge files (repo ``input/`` sample, representative
            PDF/DXF directories, official ZIP/7z samples), stratified by format and size tier, ordered by
            content digest so the choice is deterministic. Writes a private frozen manifest (absolute source
            paths + sha256) and a persistent catalog secret into a 0700 bench directory under
            ``/srv/new2/machina-deid/work`` -- never inside the Git repository.
  --run     Verify every manifest checksum, then run the existing two-pass *discovery-only* pipeline
            (twopass.discover_file + catalog.record_candidates + twopass.apply_auto, i.e. the same steps as
            twopass.discover_batch, unrolled for per-kind/location metrics). With ``--transform`` it then
            runs twopass.transform_file for already-approved candidates only; those metrics are reported in a
            separate section and never mixed into the discovery baseline.

Privacy: raw candidate values live only in memory and in the private catalog (keyed digests + protected
pseudonym store) inside the run directory. The public report holds counts, opaque sample ids, truncated
digests and location *categories* (indices stripped) only: no source paths, file names or values.
Approval rules are the unchanged twopass policy. The only STEP DATA rewrite is ISO 10303-41
ORGANIZATION.name, and only for values approved under the typed ``org`` kind (never auto-approved).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import secrets
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import IdentityCatalog  # noqa: E402

WORK_ROOT = Path("/srv/new2/machina-deid/work")
DEFAULT_ROOTS = (REPO / "input",
                 Path("/srv/new2/machina-deid/representative-pdf/sources"),
                 Path("/srv/new2/machina-deid/representative-dxf/sources"))
MANIFEST_VERSION = 1
# (tier name, exclusive upper bound in bytes)
SIZE_TIERS = (("tiny", 64 * 1024), ("small", 1024**2), ("medium", 16 * 1024**2), ("large", None))
_EXT_FORMAT = {".stp": "step", ".step": "step", ".p21": "step", ".pdf": "pdf", ".dxf": "dxf", ".zip": "zip",
               ".7z": "7z", ".sldprt": "sldprt", ".sldasm": "sldasm", ".slddrw": "slddrw"}
_SKIP_SUFFIXES = (".resume-partial", ".part", ".partial", ".tmp", ".crdownload")


# ------------------------------------------------------------------ helpers


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def size_tier(n: int) -> str:
    for name, bound in SIZE_TIERS:
        if bound is None or n < bound:
            return name
    raise AssertionError


def fmt_label(p: Path) -> str:
    ext = p.suffix.lower()
    return _EXT_FORMAT.get(ext, re.sub(r"[^a-z0-9]", "", ext)[:8] or "noext")


def location_category(loc: str) -> str:
    """Strip per-instance indices so categories aggregate (``step:FILE_NAME.author[0]`` -> ``...author[]``)."""
    cat = re.sub(r"\[\d+\]", "[]", loc)
    return cat if re.fullmatch(r"[A-Za-z0-9_:.\[\]$!-]{1,80}", cat) else "other"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_no_overlap(work_dir: Path, sources: list[Path], roots: list[Path] = ()) -> None:
    """Refuse any overlap between the private work/output tree, the sources and the Git repo."""
    w = work_dir.resolve()
    if _is_within(w, REPO) or _is_within(REPO, w):
        raise SystemExit(f"refusing: work dir must be outside the Git repository ({REPO})")
    for s in [*roots, *sources]:
        r = Path(s).resolve()
        if _is_within(r, w) or _is_within(w, r if r.is_dir() else r.parent):
            raise SystemExit("refusing: source/output overlap between work dir and a source path")


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def _write_private(p: Path, text: str) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0o600)


def peak_rss_mib() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


# ------------------------------------------------------------------ freeze


def scan_pool(roots: list[Path]) -> list[dict]:
    """All regular, complete files under ``roots`` (dot-directories such as ``.get-input`` skipped)."""
    seen: set[Path] = set()
    pool = []
    for root in roots:
        root = root.resolve()
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath, name)
                if p.is_symlink() or not p.is_file() or name.lower().endswith(_SKIP_SUFFIXES):
                    continue
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                size = rp.stat().st_size
                if size == 0:
                    continue
                pool.append({"path": str(rp), "bytes": size, "format": fmt_label(rp), "tier": size_tier(size),
                             "sha256": sha256_file(rp)})
    return pool


def select(pool: list[dict], target: int) -> list[dict]:
    """Deterministic stratified selection: round-robin over (format, tier) strata, each ordered by
    (sha256, path). Rare strata are therefore always represented; large strata fill the remainder."""
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in pool:
        strata[(e["format"], e["tier"])].append(e)
    queues = [sorted(v, key=lambda e: (e["sha256"], e["path"])) for _, v in sorted(strata.items())]
    out: list[dict] = []
    i = 0
    while len(out) < target and any(queues):
        q = queues[i % len(queues)]
        if q:
            out.append(q.pop(0))
        i += 1
    out.sort(key=lambda e: (e["format"], [t for t, _ in SIZE_TIERS].index(e["tier"]), e["sha256"], e["path"]))
    counters: Counter = Counter()
    for e in out:
        counters[e["format"]] += 1
        ext = Path(e["path"]).suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", Path(e["path"]).suffix) else ""
        e["id"] = f"{e['format']}-{e['tier']}-{counters[e['format']]:03d}{ext}"
    return out


def freeze(bench: Path, roots: list[Path], target: int, force: bool = False) -> dict:
    manifest_path = bench / "manifest.json"
    check_no_overlap(bench, [], roots)
    if manifest_path.exists() and not force:
        raise SystemExit(f"refusing: frozen manifest already exists at {manifest_path} (use a new --bench name)")
    pool = scan_pool(roots)
    if not pool:
        raise SystemExit("no source files found under the configured roots")
    chosen = select(pool, target)
    _private_dir(bench)
    secret_path = bench / "catalog.secret"
    if not secret_path.exists():
        _write_private(secret_path, secrets.token_hex(32) + "\n")
    manifest = {"version": MANIFEST_VERSION, "created_unix": int(time.time()), "target": target,
                "roots": [str(r.resolve()) for r in roots], "pool_files": len(pool),
                "size_tiers": {n: b for n, b in SIZE_TIERS}, "files": chosen}
    _write_private(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    summary = {"bench_dir": str(bench), "pool_files": len(pool), "selected": len(chosen),
               "selected_bytes": sum(e["bytes"] for e in chosen),
               "strata": dict(sorted(Counter(f"{e['format']}/{e['tier']}" for e in chosen).items())),
               "pool_strata": dict(sorted(Counter(f"{e['format']}/{e['tier']}" for e in pool).items()))}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


# ------------------------------------------------------------------ run


def load_manifest(bench: Path) -> dict:
    m = json.loads((bench / "manifest.json").read_text(encoding="utf-8"))
    if m.get("version") != MANIFEST_VERSION:
        raise SystemExit("unsupported manifest version")
    return m


def verify_sources(files: list[dict]) -> None:
    bad = [e["id"] for e in files
           if not Path(e["path"]).is_file() or Path(e["path"]).stat().st_size != e["bytes"]
           or sha256_file(Path(e["path"])) != e["sha256"]]
    if bad:
        raise SystemExit(f"refusing: {len(bad)} frozen source(s) missing or changed: {', '.join(bad[:10])}")


def _member_format(d: tp.DiscoveredFile) -> str:
    return d.format or "unsupported"


def run_discovery(cat: IdentityCatalog, files: list[dict]) -> dict:
    """Discovery-only baseline. Mirrors twopass.discover_batch per file (record + conservative auto policy)."""
    t0 = time.monotonic()
    by_kind: Counter = Counter()
    by_loc: Counter = Counter()
    by_kind_conf: Counter = Counter()
    eligible_kind: Counter = Counter()
    review_reasons: Counter = Counter()
    decisions: Counter = Counter()
    member_formats: Counter = Counter()
    unsupported_reasons: Counter = Counter()
    failures: Counter = Counter()
    member_shas: dict[str, int] = Counter()
    per_file = []
    totals = Counter()
    for e in files:
        tf = time.monotonic()
        row = {"id": e["id"], "format": e["format"], "tier": e["tier"], "bytes": e["bytes"],
               "members": 0, "ok": 0, "unsupported": 0, "failed": 0, "candidates": 0,
               "auto_eligible": 0, "review_required": 0}
        try:
            found = tp.discover_file(Path(e["path"]), e["id"])
        except Exception as exc:  # noqa: BLE001 - report type only; continue the batch
            found = [tp.DiscoveredFile(e["id"], None, e["sha256"], "failed", f"discovery_error:{type(exc).__name__}")]
        for d in found:
            row["members"] += 1
            member_formats[_member_format(d)] += 1
            if d.sha256:
                member_shas[d.sha256] += 1
            if d.status == "unsupported":
                row["unsupported"] += 1
                unsupported_reasons[f"{d.reason}:{fmt_label(Path(d.path.rsplit('!', 1)[-1]))}"] += 1
                continue
            if d.status == "failed":
                row["failed"] += 1
                failures[d.reason] += 1
                continue
            row["ok"] += 1
            row["candidates"] += len(d.candidates)
            for c in d.candidates:
                by_kind[c.kind] += 1
                by_loc[location_category(c.location)] += 1
                by_kind_conf[f"{c.kind}/{c.confidence}"] += 1
                status, reason = tp.auto_decision(c)
                if status == "approved":
                    row["auto_eligible"] += 1
                    eligible_kind[c.kind] += 1
                else:
                    row["review_required"] += 1
                    review_reasons[reason] += 1
            try:
                totals["new_observations"] += cat.record_candidates(d.candidates, source_sha256=d.sha256, path=d.path)
                for k, v in tp.apply_auto(cat, d.candidates).items():
                    decisions[k] += v
            except Exception as exc:  # noqa: BLE001
                failures[f"catalog_error:{type(exc).__name__}"] += 1
        row["elapsed_s"] = round(time.monotonic() - tf, 3)
        per_file.append(row)
    for k in ("members", "ok", "unsupported", "failed", "candidates", "auto_eligible", "review_required"):
        totals[k] = sum(r[k] for r in per_file)
    files_by_format = Counter(e["format"] for e in files)
    bytes_by_format = Counter()
    for e in files:
        bytes_by_format[e["format"]] += e["bytes"]
    appr = cat.approvals_report()
    return {
        "files": len(files),
        "files_by_format": dict(sorted(files_by_format.items())),
        "files_by_tier": dict(sorted(Counter(e["tier"] for e in files).items())),
        "input_bytes": sum(e["bytes"] for e in files),
        "input_bytes_by_format": dict(sorted(bytes_by_format.items())),
        "entries_discovered": totals["members"],
        "entries_by_format": dict(sorted(member_formats.items())),
        "entries_ok": totals["ok"],
        "unsupported_entries": totals["unsupported"],
        "unsupported_by_reason_format": dict(sorted(unsupported_reasons.items())),
        "unsupported_top_level_files": sum(1 for r in per_file if r["members"] == r["unsupported"] and r["members"]),
        "discovery_failures": totals["failed"],
        "discovery_failures_by_reason": dict(sorted(failures.items())),
        "files_with_failures": sum(1 for r in per_file if r["failed"]),
        "candidates": totals["candidates"],
        "candidates_by_kind": dict(sorted(by_kind.items())),
        "candidates_by_kind_confidence": dict(sorted(by_kind_conf.items())),
        "candidates_by_location_category": dict(sorted(by_loc.items())),
        "auto_approval_eligible_candidates": totals["auto_eligible"],
        "auto_approval_eligible_by_kind": dict(sorted(eligible_kind.items())),
        "review_required_candidates": totals["review_required"],
        "review_required_by_reason": dict(sorted(review_reasons.items())),
        "catalog_new_observations": totals["new_observations"],
        "catalog_decision_results_per_unique_value": dict(sorted(decisions.items())),
        "catalog_approvals": appr["counts"],
        "catalog_approved_by_kind": dict(sorted(Counter(i["kind"] for i in appr["items"]
                                                        if i["status"] == "approved").items())),
        "duplicate_member_sha_groups": sum(1 for n in member_shas.values() if n > 1),
        "per_file": per_file,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_rss_mib_after_discovery": peak_rss_mib(),
    }


def run_transform(cat: IdentityCatalog, files: list[dict], out_dir: Path) -> dict:
    """Transformation of already-approved candidates only (no new approvals are made here)."""
    t0 = time.monotonic()
    by_status: Counter = Counter()
    reasons: Counter = Counter()
    per_file = []
    replaced = residual_t = residual_rescan = data_orgs = 0
    for e in files:
        dst = out_dir / e["id"]
        try:
            o = tp.transform_file(cat, Path(e["path"]), dst, e["id"])
        except Exception as exc:  # noqa: BLE001
            o = tp.TransformOutcome(e["id"], None, "failed", f"transform_error:{type(exc).__name__}")
        rescan = 0
        if o.output_written:
            for d in tp.discover_file(dst, e["id"]):
                rescan += sum(1 for c in d.candidates if cat.approved_pseudonym(c.value))
        by_status[o.status] += 1
        if o.status in ("failed", "unsupported"):
            reasons[o.reason] += 1
        replaced += o.fields_replaced
        data_orgs += o.data_organizations_replaced
        residual_t += o.residual_approved
        residual_rescan += rescan
        per_file.append({"id": e["id"], "format": e["format"], "status": o.status, "reason": o.reason,
                         "approved_values": o.approved_values, "fields_replaced": o.fields_replaced,
                         "residual_approved_transformer": o.residual_approved, "residual_approved_rescan": rescan,
                         "source_unchanged": o.source_unchanged})
    return {
        "scope": "already-approved candidates only; STEP DATA: ORGANIZATION.name with typed org approval only",
        "by_status": dict(sorted(by_status.items())),
        "files_transformed": by_status.get("transformed", 0),
        "fields_replaced": replaced,
        "step_data_organization_names_replaced": data_orgs,
        "step_geometry_validation": "not performed (no trusted geometry kernel); structural/numeric invariants only",
        "residual_approved_transformer": residual_t,
        "residual_approved_rescan": residual_rescan,
        "failed_or_unsupported_by_reason": dict(sorted(reasons.items())),
        "per_file": per_file,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_rss_mib_cumulative": peak_rss_mib(),
    }


def run(bench: Path, *, transform: bool, run_name: str | None = None) -> dict:
    t0 = time.monotonic()
    m = load_manifest(bench)
    files = m["files"]
    srcs = [Path(e["path"]) for e in files]
    check_no_overlap(bench, srcs, [Path(r) for r in m["roots"]])
    verify_sources(files)
    secret = (bench / "catalog.secret").read_bytes().strip()
    run_dir = _private_dir(_private_dir(bench / "runs") / (run_name or time.strftime("%Y%m%dT%H%M%S")))
    if any(run_dir.iterdir()):
        raise SystemExit(f"refusing: run dir {run_dir} is not empty")
    dup = Counter(e["sha256"] for e in files)
    cat = IdentityCatalog(_private_dir(run_dir / "catalog") / "catalog.sqlite", secret)
    try:
        disc = run_discovery(cat, files)
        xf = run_transform(cat, files, _private_dir(run_dir / "output")) if transform else None
    finally:
        cat.close()
    verify_after = [e["id"] for e in files if sha256_file(Path(e["path"])) != e["sha256"]]
    report = {
        "phase": "5D",
        "manifest_sha256": sha256_file(bench / "manifest.json")[:16],
        "selected_files": len(files),
        "duplicate_source_sha_groups": sum(1 for n in dup.values() if n > 1),
        "duplicate_source_sha_files": sum(n for n in dup.values() if n > 1),
        "baseline_discovery": disc,
        "transformation": xf if xf is not None else "not run (pass --transform)",
        "source_preserved": not verify_after,
        "source_changed_ids": verify_after,
        "raw_values_in_report": False,
        "step_data_section": "ORGANIZATION.name only (typed org approval); all other DATA unchanged",
        "approval_policy": "unchanged twopass.auto_decision",
        "elapsed_s_total": round(time.monotonic() - t0, 3),
        "peak_rss_mib": peak_rss_mib(),
        "run_dir": str(run_dir),
    }
    _write_private(run_dir / "public-report.json", json.dumps(report, indent=2, sort_keys=True))
    return report


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze", action="store_true", help="select files and write the frozen private manifest")
    g.add_argument("--run", action="store_true", help="run discovery baseline against the frozen manifest")
    ap.add_argument("--bench", default="phase5d-frozen", help="bench name (directory under --work-root)")
    ap.add_argument("--work-root", default=str(WORK_ROOT), type=Path)
    ap.add_argument("--root", action="append", type=Path, help="source root (repeatable; default: challenge samples)")
    ap.add_argument("--target", type=int, default=72, help="files to select (50-100 recommended)")
    ap.add_argument("--transform", action="store_true", help="also transform already-approved candidates")
    ap.add_argument("--run-name", help="run directory name (default: timestamp)")
    ap.add_argument("--summary", action="store_true", help="print report without per-file rows")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.bench) or a.bench.startswith("."):
        ap.error("invalid --bench name")
    if not 1 <= a.target <= 1000:
        ap.error("--target must be in 1..1000")
    bench = a.work_root.resolve() / a.bench
    if a.freeze:
        freeze(bench, a.root or list(DEFAULT_ROOTS), a.target)
        return 0
    rep = run(bench, transform=a.transform, run_name=a.run_name)
    if a.summary:
        rep = json.loads(json.dumps(rep))
        rep["baseline_discovery"].pop("per_file")
        if isinstance(rep["transformation"], dict):
            rep["transformation"].pop("per_file")
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0 if rep["source_preserved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
