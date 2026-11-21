#!/usr/bin/env python3
"""Phase 5E Milestone A: balanced, manifest-driven benchmark (discovery baseline; optional --transform).

Steps (each reusable; ``--all`` runs select -> fetch -> run):

  --select  Read the official ``manifest.tsv`` and deterministically pick a balanced sample (default
            ~20 STEP/STP, 20 PDF, 10 DXF, 5 ZIP, 10 neutral/mesh) under a hard byte cap (default 8 GiB) and a
            per-file cap. Within each format the choice round-robins over (size tier, source) strata, ordered by
            a fixed-seed digest of the manifest path, preferring not-yet-used repositories. Rows already present
            locally (matching manifest size, and MD5 when the manifest has one) are preferred inside each stratum,
            so existing downloads are reused. The selection is written once (frozen) and never overwritten.
  --fetch   For selected rows not reusable in place, download from the official mirror with bounded,
            resumable ``curl`` into ``<bench>/raw/`` (opaque sample-id names), verify manifest size + MD5, then
            record sha256 checksums. Never downloads anything that was not selected.
  --run     Verify every frozen checksum, then run the production discovery path (twopass.discover_file +
            IdentityCatalog.record_candidates + twopass.apply_auto, i.e. twopass.discover_batch unrolled) and
            write a redacted public report with metrics by format. ``--transform`` additionally calls
            twopass.transform_file for already-approved candidates only, reported in a separate section.

All private state (frozen selection, checksums, catalog secret, raw downloads, catalog, outputs) lives under
``/srv/new2/machina-deid/work/phase5e-balanced`` (0700/0600), never in Git. The public report holds counts,
opaque ids and location categories only: no manifest paths, file names, URLs or candidate values.
Approval rules are the unchanged twopass policy; no new transformers.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import IdentityCatalog  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("phase5d_benchmark", REPO / "scripts" / "phase5d_benchmark.py")
_p5d = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_p5d)
sha256_file, location_category, peak_rss_mib = _p5d.sha256_file, _p5d.location_category, _p5d.peak_rss_mib
_private_dir, _write_private, _is_within = _p5d._private_dir, _p5d._write_private, _p5d._is_within

WORK_ROOT = Path("/srv/new2/machina-deid/work")
DEFAULT_BENCH = "phase5e-balanced"
PROTECTED_BENCHES = {"phase5d-frozen"}
DEFAULT_MANIFEST = REPO / "manifest.tsv"
DEFAULT_BASE = os.environ.get("MACHINA_INPUT_BASE", "https://machina-input.joinbuilders.org")
DEFAULT_REUSE_ROOTS = (REPO / "input", Path("/srv/new2/machina-deid/input"),
                       Path("/srv/new2/machina-deid/representative-pdf"),
                       Path("/srv/new2/machina-deid/representative-dxf"))
GIB, MIB = 1024**3, 1024**2
DEFAULT_BYTE_CAP = 8 * GIB
HARD_BYTE_CAP = 8 * GIB  # the configurable cap may be lowered, never raised above this
DEFAULT_MAX_FILE = 128 * MIB
SELECTION_VERSION = 1
SEED = "phase5e-balanced-v1"
SIZE_TIERS = (("tiny", 64 * 1024), ("small", MIB), ("medium", 16 * MIB), ("large", None))
GROUP_EXTS = {
    "step": (".step", ".stp", ".p21"),
    "pdf": (".pdf",),
    "dxf": (".dxf",),
    "zip": (".zip",),
    "neutral_mesh": (".stl", ".obj", ".3mf", ".igs", ".iges", ".wrl", ".ply", ".x_t", ".x_b", ".off", ".amf"),
}
DEFAULT_TARGETS = {"step": 20, "pdf": 20, "dxf": 10, "zip": 5, "neutral_mesh": 10}
_EXT_GROUP = {e: g for g, exts in GROUP_EXTS.items() for e in exts}
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
_SAFE_URL = re.compile(r"[A-Za-z0-9._~%!$&'()*+,;=:@/-]+")


# ------------------------------------------------------------------ helpers


def size_tier(n: int) -> str:
    return next(t for t, b in SIZE_TIERS if b is None or n < b)


def group_of(path: str) -> str | None:
    return _EXT_GROUP.get(Path(path).suffix.lower())


def source_bucket(path: str) -> tuple[str, str]:
    """(source kind, repository key) from a manifest path such as ``sources/github/owner/repo/...``."""
    parts = path.split("/")
    if parts[0] == "sources" and len(parts) >= 3:
        return parts[1], "/".join(parts[1:4])
    return parts[0], "/".join(parts[:2])


def order_key(path: str) -> str:
    return hashlib.sha256(f"{SEED}\0{path}".encode()).hexdigest()


def md5_file(p: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - integrity check against the official manifest only
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def safe_rel(path: str, *, url: bool = False) -> bool:
    """Relative, no traversal/empty segments, no control chars or backslashes; URL paths must be encoded."""
    if path.startswith("/") or _UNSAFE_CHARS.search(path) or (url and not _SAFE_URL.fullmatch(path)):
        return False
    return all(p not in ("", ".", "..") for p in path.split("/"))


def read_manifest(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4 or not cols[1].isdigit():
                continue
            rel, n, md5, url = cols[0], int(cols[1]), cols[2].strip().lower(), cols[3]
            if n <= 0 or not safe_rel(rel) or not safe_rel(url, url=True):
                continue
            rows.append({"manifest_path": rel, "bytes": n, "md5": md5 if re.fullmatch(r"[0-9a-f]{32}", md5) else "",
                         "url_path": url})
    return rows


def find_local(row: dict, roots: list[Path], *, check_md5: bool = True) -> Path | None:
    """An existing complete copy of ``row`` under a reuse root (same manifest-relative path, size, MD5)."""
    for r in roots:
        p = r / row["manifest_path"]
        try:
            if p.is_symlink() or not p.is_file() or p.stat().st_size != row["bytes"]:
                continue
        except OSError:
            continue
        if check_md5 and row["md5"] and md5_file(p) != row["md5"]:
            continue
        return p.resolve()
    return None


def check_no_overlap(bench: Path, sources: list[Path] = (), outputs: list[Path] = ()) -> None:
    """Bench must be outside Git; no output may overlap a source or a protected bench."""
    b = bench.resolve()
    if _is_within(b, REPO) or _is_within(REPO, b):
        raise SystemExit(f"refusing: bench dir must be outside the Git repository ({REPO})")
    if b.name in PROTECTED_BENCHES:
        raise SystemExit("refusing: protected frozen benchmark")
    for o in outputs:
        o = Path(o).resolve()
        for s in sources:
            s = Path(s).resolve()
            if _is_within(s, o) or _is_within(o, s):
                raise SystemExit("refusing: source/output overlap")


# ------------------------------------------------------------------ select


def select(rows: list[dict], targets: dict[str, int], *, byte_cap: int, max_file: int,
           local: set[str] = frozenset()) -> list[dict]:
    """Deterministic balanced selection (independent of manifest row order).

    Per group, strata = (size tier, source kind); round-robin over strata; inside a stratum prefer
    locally available rows, then rows from not-yet-used repositories, then the seeded path digest.
    A row is taken only if it keeps the running total within ``byte_cap``.
    """
    if byte_cap > HARD_BYTE_CAP:
        raise SystemExit(f"refusing: byte cap above hard limit {HARD_BYTE_CAP}")
    by_group: dict[str, dict[tuple[str, str], list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        g = group_of(r["manifest_path"])
        if g in targets and r["bytes"] <= max_file:
            by_group[g][(size_tier(r["bytes"]), source_bucket(r["manifest_path"])[0])].append(r)
    total = 0
    chosen: list[dict] = []
    # Smallest-target groups first is not needed; interleave groups so the cap is shared fairly.
    queues = {g: [sorted(v, key=lambda r: (r["manifest_path"] not in local, order_key(r["manifest_path"])))
                  for _, v in sorted(by_group[g].items())] for g in sorted(targets)}
    count = Counter()
    used_repos: dict[str, set[str]] = defaultdict(set)
    rr = Counter()
    progress = True
    while progress:
        progress = False
        for g in sorted(targets):
            qs = [q for q in queues.get(g, []) if q]
            queues[g] = qs
            while count[g] < targets[g] and qs:
                q = qs[rr[g] % len(qs)]
                rr[g] += 1
                pick = next((r for r in q if source_bucket(r["manifest_path"])[1] not in used_repos[g]
                             and total + r["bytes"] <= byte_cap), None)
                if pick is None:
                    pick = next((r for r in q if total + r["bytes"] <= byte_cap), None)
                if pick is None:  # nothing in this stratum fits under the cap any more
                    qs.remove(q)
                    continue
                q.remove(pick)
                chosen.append({**pick, "group": g})
                total += pick["bytes"]
                count[g] += 1
                used_repos[g].add(source_bucket(pick["manifest_path"])[1])
                progress = True
                break
    tiers = [t for t, _ in SIZE_TIERS]
    chosen.sort(key=lambda r: (r["group"], tiers.index(size_tier(r["bytes"])), order_key(r["manifest_path"])))
    n = Counter()
    for r in chosen:
        n[r["group"]] += 1
        r["tier"] = size_tier(r["bytes"])
        r["source_kind"] = source_bucket(r["manifest_path"])[0]
        ext = Path(r["manifest_path"]).suffix.lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9_]{1,8}", ext) else ""
        r["id"] = f"{r['group']}-{r['tier']}-{n[r['group']]:03d}{ext}"
    return chosen


def do_select(bench: Path, manifest: Path, targets: dict[str, int], *, byte_cap: int, max_file: int,
              reuse_roots: list[Path]) -> dict:
    check_no_overlap(bench)
    sel_path = bench / "selection.json"
    if sel_path.exists():
        raise SystemExit(f"refusing: frozen selection already exists at {sel_path} (use a new --bench)")
    rows = read_manifest(manifest)
    if not rows:
        raise SystemExit("manifest has no usable rows")
    avail = Counter(group_of(r["manifest_path"]) for r in rows)
    # Size/MD5-checked local presence for rows in the target groups only (cheap stat first, MD5 lazily).
    roots = [r.resolve() for r in reuse_roots if r.is_dir()]
    local = {r["manifest_path"] for r in rows if group_of(r["manifest_path"]) in targets and r["bytes"] <= max_file
             and find_local(r, roots)}
    chosen = select(rows, targets, byte_cap=byte_cap, max_file=max_file, local=local)
    _private_dir(bench)
    secret = bench / "catalog.secret"
    if not secret.exists():
        _write_private(secret, secrets.token_hex(32) + "\n")
    sel = {"version": SELECTION_VERSION, "seed": SEED, "created_unix": int(time.time()),
           "manifest_sha256": sha256_file(manifest), "manifest_rows": len(rows),
           "targets": targets, "byte_cap": byte_cap, "max_file_bytes": max_file,
           "reuse_roots": [str(r) for r in roots],
           "available_by_group": {g: avail.get(g, 0) for g in sorted(targets)}, "files": chosen}
    _write_private(sel_path, json.dumps(sel, indent=2, sort_keys=True))
    summary = {"selected": len(chosen), "selected_bytes": sum(r["bytes"] for r in chosen), "byte_cap": byte_cap,
               "selected_by_group": dict(sorted(Counter(r["group"] for r in chosen).items())),
               "available_by_group": sel["available_by_group"],
               "reusable_locally": sum(1 for r in chosen if r["manifest_path"] in local),
               "strata": dict(sorted(Counter(f"{r['group']}/{r['tier']}/{r['source_kind']}" for r in chosen).items()))}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


# ------------------------------------------------------------------ fetch


def load_selection(bench: Path) -> dict:
    s = json.loads((bench / "selection.json").read_text(encoding="utf-8"))
    if s.get("version") != SELECTION_VERSION:
        raise SystemExit("unsupported selection version")
    return s


def curl_fetch(url: str, dst: Path, expected_bytes: int, *, timeout_s: int) -> None:
    """Bounded, resumable download into ``dst`` (``.part`` sibling until verified by the caller)."""
    cmd = ["curl", "-fsS", "--proto", "=https", "--retry", "5", "--retry-connrefused", "--retry-delay", "2",
           "--connect-timeout", "30", "--max-time", str(timeout_s), "--max-filesize", str(expected_bytes),
           "-C", "-", "-o", str(dst), "--", url]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s + 60)
    if r.returncode == 33 or (r.returncode == 22 and dst.exists() and dst.stat().st_size >= expected_bytes):
        pass  # range not satisfiable / already complete; size verified below
    elif r.returncode != 0:
        raise RuntimeError(f"curl_exit_{r.returncode}")


def verify_row(p: Path, row: dict) -> str:
    """Return the sha256 after checking manifest size and MD5 (when present); raise on mismatch."""
    if p.stat().st_size != row["bytes"]:
        raise RuntimeError("size_mismatch")
    if row["md5"] and md5_file(p) != row["md5"]:
        raise RuntimeError("md5_mismatch")
    return sha256_file(p)


def do_fetch(bench: Path, *, base: str, timeout_s: int = 1800, fetcher=curl_fetch) -> dict:
    sel = load_selection(bench)
    if not base.startswith("https://"):
        raise SystemExit("refusing: mirror base must be https")
    raw = _private_dir(bench / "raw")
    roots = [Path(r) for r in sel["reuse_roots"]]
    check_no_overlap(bench, roots, [raw])
    ck_path = bench / "checksums.json"
    checks = json.loads(ck_path.read_text()) if ck_path.exists() else {}
    stats = Counter()
    budget = sel["byte_cap"]
    for row in sel["files"]:
        if row["id"] in checks and Path(checks[row["id"]]["path"]).is_file():
            stats["already_frozen"] += 1
            continue
        src = find_local(row, roots)
        origin = "reused"
        if src is None:
            dst = raw / row["id"]
            part = raw / (row["id"] + ".part")
            if sum(c["bytes"] for c in checks.values()) + row["bytes"] > budget:
                stats["skipped_byte_cap"] += 1
                continue
            try:
                if not (dst.is_file() and dst.stat().st_size == row["bytes"]):
                    fetcher(f"{base.rstrip('/')}/{row['url_path']}", part, row["bytes"], timeout_s=timeout_s)
                    verify_row(part, row)
                    os.chmod(part, 0o400)
                    part.replace(dst)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                stats[f"failed:{exc if isinstance(exc, RuntimeError) else type(exc).__name__}"] += 1
                if isinstance(exc, RuntimeError) and "mismatch" in str(exc):
                    part.unlink(missing_ok=True)
                continue
            src, origin = dst, "downloaded"
        try:
            sha = verify_row(src, row)
        except RuntimeError as exc:
            stats[f"failed:{exc}"] += 1
            continue
        checks[row["id"]] = {"path": str(src), "bytes": row["bytes"], "sha256": sha, "md5": row["md5"],
                             "origin": origin}
        stats[origin] += 1
        _write_private(ck_path, json.dumps(checks, indent=2, sort_keys=True))
    _write_private(bench / "checksums.sha256",
                   "".join(f"{c['sha256']}  {i}\n" for i, c in sorted(checks.items())))
    summary = {"selected": len(sel["files"]), "frozen": len(checks), **dict(sorted(stats.items())),
               "frozen_bytes": sum(c["bytes"] for c in checks.values())}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


# ------------------------------------------------------------------ run


def verify_frozen(files: list[dict], checks: dict) -> list[str]:
    bad = []
    for e in files:
        c = checks.get(e["id"])
        if c is None:
            continue
        p = Path(c["path"])
        if not p.is_file() or p.stat().st_size != c["bytes"] or sha256_file(p) != c["sha256"]:
            bad.append(e["id"])
    return bad


_COUNTERS = ("selected", "available_in_manifest", "frozen", "reused", "downloaded", "readable", "entries",
             "unsupported", "failures", "no_identifiers", "candidates", "confident_candidates",
             "confident_identities", "auto_eligible", "review_required", "input_bytes")


def run_discovery(cat: IdentityCatalog, files: list[dict], checks: dict, secret: bytes, avail: dict) -> dict:
    t0 = time.monotonic()
    fm: dict[str, Counter] = defaultdict(Counter)
    confident: dict[str, set[str]] = defaultdict(set)
    sha_seen: Counter = Counter()
    member_sha: Counter = Counter()
    extra: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    elapsed: Counter = Counter()
    per_file = []
    for g, n in avail.items():
        fm[g]["available_in_manifest"] = n
    for e in files:
        g = e["group"]
        m = fm[g]
        m["selected"] += 1
        c = checks.get(e["id"])
        if c is None:
            continue
        m["frozen"] += 1
        m[c["origin"]] += 1
        m["input_bytes"] += c["bytes"]
        sha_seen[c["sha256"]] += 1
        tf = time.monotonic()
        try:
            found = tp.discover_file(Path(c["path"]), e["id"])
        except Exception as exc:  # noqa: BLE001
            found = [tp.DiscoveredFile(e["id"], None, c["sha256"], "failed", f"discovery_error:{type(exc).__name__}")]
        row = Counter()
        for d in found:
            row["entries"] += 1
            if d.sha256:
                member_sha[d.sha256] += 1
            if d.status == "unsupported":
                row["unsupported"] += 1
                extra[g]["unsupported_by_reason"][d.reason] += 1
                continue
            if d.status == "failed":
                row["failures"] += 1
                extra[g]["failures_by_reason"][d.reason] += 1
                continue
            row["readable"] += 1
            if not d.candidates:
                row["no_identifiers"] += 1
            for cand in d.candidates:
                row["candidates"] += 1
                extra[g]["candidates_by_kind"][cand.kind] += 1
                extra[g]["candidates_by_location_category"][location_category(cand.location)] += 1
                if cand.confidence == "high":
                    row["confident_candidates"] += 1
                    confident[g].add(hmac.new(secret, f"{cand.kind}\0{cand.value}".encode(), "sha256").hexdigest())
                status, reason = tp.auto_decision(cand)
                if status == "approved":
                    row["auto_eligible"] += 1
                else:
                    row["review_required"] += 1
                    extra[g]["review_required_by_reason"][reason] += 1
            try:
                cat.record_candidates(d.candidates, source_sha256=d.sha256, path=d.path)
                tp.apply_auto(cat, d.candidates)
            except Exception as exc:  # noqa: BLE001
                extra[g]["failures_by_reason"][f"catalog_error:{type(exc).__name__}"] += 1
        m.update(row)
        dt = time.monotonic() - tf
        elapsed[g] += dt
        per_file.append({"id": e["id"], "group": g, "tier": e["tier"], "source_kind": e["source_kind"],
                         "bytes": c["bytes"], **{k: row[k] for k in ("entries", "readable", "unsupported",
                                                                     "failures", "candidates", "auto_eligible",
                                                                     "review_required")},
                         "elapsed_s": round(dt, 3)})
    by_format = {}
    for g in sorted(fm):
        m = fm[g]
        m["confident_identities"] = len(confident[g])
        by_format[g] = {**{k: m.get(k, 0) for k in _COUNTERS},
                        "runtime_s": round(elapsed[g], 3),
                        **{k: dict(sorted(v.items())) for k, v in sorted(extra[g].items())}}
    appr = cat.approvals_report()
    return {"by_format": by_format,
            "totals": {k: sum(by_format[g][k] for g in by_format) for k in _COUNTERS if k != "confident_identities"},
            "duplicate_source_sha_groups": sum(1 for n in sha_seen.values() if n > 1),
            "duplicate_source_sha_files": sum(n for n in sha_seen.values() if n > 1),
            "duplicate_entry_sha_groups": sum(1 for n in member_sha.values() if n > 1),
            "catalog_approvals": appr["counts"],
            "per_file": per_file,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "peak_rss_mib_after_discovery": peak_rss_mib()}


def run_transform(cat: IdentityCatalog, files: list[dict], checks: dict, out_dir: Path) -> dict:
    """Existing approvals only (twopass.transform_file); no new approvals or transformers."""
    t0 = time.monotonic()
    by: dict[str, Counter] = defaultdict(Counter)
    for e in files:
        c = checks.get(e["id"])
        if c is None:
            continue
        try:
            o = tp.transform_file(cat, Path(c["path"]), out_dir / e["id"], e["id"])
        except Exception as exc:  # noqa: BLE001
            o = tp.TransformOutcome(e["id"], None, "failed", f"transform_error:{type(exc).__name__}")
        b = by[e["group"]]
        b[f"status:{o.status}"] += 1
        b.update(o.replacement_counts())
        b["fields_replaced"] += o.fields_replaced
        b["residual_approved"] += o.residual_approved
        b["source_unchanged"] += bool(o.source_unchanged)
    return {"scope": "already-approved candidates only (unchanged twopass policy)",
            "by_format": {g: dict(sorted(v.items())) for g, v in sorted(by.items())},
            "total_verified_identity_fields_changed": sum(
                v["total_verified_identity_fields_changed"] for v in by.values()),
            "elapsed_s": round(time.monotonic() - t0, 3), "peak_rss_mib_cumulative": peak_rss_mib()}


def do_run(bench: Path, *, transform: bool, run_name: str | None = None) -> dict:
    t0 = time.monotonic()
    sel = load_selection(bench)
    ck_path = bench / "checksums.json"
    checks = json.loads(ck_path.read_text()) if ck_path.exists() else {}
    if not checks:
        raise SystemExit("refusing: nothing frozen yet (run --fetch)")
    files = sel["files"]
    run_dir = bench / "runs" / (run_name or time.strftime("%Y%m%dT%H%M%S"))
    srcs = [Path(c["path"]) for c in checks.values()]
    check_no_overlap(bench, srcs, [run_dir])
    bad = verify_frozen(files, checks)
    if bad:
        raise SystemExit(f"refusing: {len(bad)} frozen source(s) missing or changed: {', '.join(bad[:10])}")
    _private_dir(_private_dir(bench / "runs") / run_dir.name)
    if any(run_dir.iterdir()):
        raise SystemExit(f"refusing: run dir {run_dir} is not empty")
    secret = (bench / "catalog.secret").read_bytes().strip()
    cat = IdentityCatalog(_private_dir(run_dir / "catalog") / "catalog.sqlite", secret)
    try:
        disc = run_discovery(cat, files, checks, secret, sel["available_by_group"])
        xf = run_transform(cat, files, checks, _private_dir(run_dir / "output")) if transform else None
    finally:
        cat.close()
    changed = verify_frozen(files, checks)
    report = {"phase": "5E-A", "selection_sha256": sha256_file(bench / "selection.json")[:16],
              "byte_cap": sel["byte_cap"], "targets": sel["targets"],
              "baseline_discovery": disc,
              "transformation": xf if xf is not None else "not run (pass --transform)",
              "source_preserved": not changed, "source_changed_ids": changed,
              "approval_policy": "unchanged twopass.auto_decision",
              "raw_values_in_report": False,
              "elapsed_s_total": round(time.monotonic() - t0, 3), "peak_rss_mib": peak_rss_mib()}
    assert_redacted(report, sel, checks)
    _write_private(run_dir / "public-report.json", json.dumps(report, indent=2, sort_keys=True))
    return report


def assert_redacted(report: dict, sel: dict, checks: dict) -> None:
    """Fail closed if any private path / manifest path / file name leaks into the public report."""
    text = json.dumps(report)
    needles = {str(REPO), "/srv/", *sel.get("reuse_roots", ())}
    for r in sel["files"]:
        needles.add(r["manifest_path"])
        needles.add(r["url_path"])
    for c in checks.values():
        needles.add(c["path"])
    leaked = [n for n in needles if n and n in text]
    if leaked:
        raise SystemExit(f"refusing: public report would leak {len(leaked)} private string(s)")


# ------------------------------------------------------------------ CLI


def _targets(spec: str | None) -> dict[str, int]:
    t = dict(DEFAULT_TARGETS)
    for part in (spec or "").split(","):
        if part.strip():
            k, _, v = part.partition("=")
            if k not in GROUP_EXTS or not v.isdigit() or not 0 <= int(v) <= 200:
                raise SystemExit(f"invalid --targets entry: {part}")
            t[k] = int(v)
    return {k: v for k, v in t.items() if v}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    for s in ("select", "fetch", "run", "all"):
        g.add_argument(f"--{s}", action="store_true")
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--work-root", default=str(WORK_ROOT), type=Path)
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), type=Path)
    ap.add_argument("--reuse-root", action="append", type=Path, help="existing download root (repeatable)")
    ap.add_argument("--targets", help="e.g. step=20,pdf=20,dxf=10,zip=5,neutral_mesh=10")
    ap.add_argument("--byte-cap-gib", type=float, default=DEFAULT_BYTE_CAP / GIB)
    ap.add_argument("--max-file-mib", type=float, default=DEFAULT_MAX_FILE / MIB)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--timeout", type=int, default=1800, help="per-file curl --max-time seconds")
    ap.add_argument("--transform", action="store_true", help="also transform using existing approvals only")
    ap.add_argument("--run-name")
    ap.add_argument("--summary", action="store_true", help="omit per-file rows from stdout")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.bench) or a.bench.startswith(".") or a.bench in PROTECTED_BENCHES:
        ap.error("invalid or protected --bench name")
    cap = int(a.byte_cap_gib * GIB)
    if not 0 < cap <= HARD_BYTE_CAP:
        ap.error("--byte-cap-gib must be in (0, 8]")
    bench = a.work_root.resolve() / a.bench
    if a.select or a.all:
        do_select(bench, a.manifest, _targets(a.targets), byte_cap=cap, max_file=int(a.max_file_mib * MIB),
                  reuse_roots=a.reuse_root or list(DEFAULT_REUSE_ROOTS))
    if a.fetch or a.all:
        do_fetch(bench, base=a.base, timeout_s=a.timeout)
    if a.run or a.all:
        rep = do_run(bench, transform=a.transform, run_name=a.run_name)
        if a.summary:
            rep = json.loads(json.dumps(rep))
            rep["baseline_discovery"].pop("per_file")
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0 if rep["source_preserved"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
