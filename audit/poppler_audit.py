"""RCAC independent Poppler occurrence audit (standalone, stdlib only).

Designed to be copied out of the repo and run by the desktop orchestrator on the
local machine with Poppler (pdftotext) available. It builds a conservative,
independent rubric over selected raw PDFs and reports ONLY anonymized machine-
readable aggregates: counts, rates, opaque sha checkpoints. No source text, no
paths, no labels are ever emitted.

Extraction is independent of the production stack: words + bboxes come from
``pdftotext -bbox`` (Poppler). For parser validation on machines without Poppler,
pass --bbox-dir DIR and the same XML files are read from DIR/<relpath>.xml.

Usage (see --help):
  python poppler_audit.py audit --source RAW_DIR --manifest split.json \
      --labels approved.json --out-a SCORED_PKG_A [--out-b SCORED_PKG_B] \
      [--poppler pdftotext | --bbox-dir DIR]

Modes:
  inventory  read-only: manifest/directory agreement, sha check, per-family
             and per-split file counts. No PDF parsing at all.
  sample     audit rubric restricted to the first N qualified rows.
  audit      audit rubric over all qualified rows.

Manifest: JSON list (or {"rows": [...]}) of {family, path, sha256, split}.
Labels:   JSON {"<entity>": ["literal value", ...]} — approved literal values
          for exact boundary-aware occurrence checks (heuristic role-field
          counts are additionally reported and annotated as proxy).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

TOOL = "rcac-poppler-audit"
VERSION = 1

# strict title-block role keywords (case-insensitive, whole word, line start)
ROLE_KEYWORDS = ("drawn", "checked", "prepared", "approved", "issued", "company", "for")
# conservative exclusions for heuristic role-field values: drawing numbers,
# pure numerics, scales, dates, product-like all-uppercase tokens are NOT identities
_NUMERICISH = re.compile(r"^[\d\s.\-/+:%Ø°$€£]+$")


def _oid(*parts: str) -> str:
    """Opaque id: short sha of parts; no input text is emitted."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _sha_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: str):
    data = json.loads(open(path, "r", encoding="utf-8").read())
    rows = data["rows"] if isinstance(data, dict) and "rows" in data else data
    out = []
    for r in rows:
        out.append({"family": r["family"], "path": r["path"],
                    "sha256": r["sha256"], "split": r.get("split", "all")})
    return out


def load_labels(path: str | None):
    if not path:
        return []
    data = json.loads(open(path, "r", encoding="utf-8").read())
    vals: list[str] = []
    for v in data.values():
        vals.extend(v)
    return [v for v in dict.fromkeys(vals)]


# ---------- Poppler -bbox XML parsing ----------

_PAGE_RE = re.compile(r'<div class="page"[^>]*id="(\w+)"\s+bbox="([^"]+)"', re.S)
_WORD_RE = re.compile(r'<span class="word" bbox="([^"]+)">(.*?)</span>', re.S)
_NUM = r"-?\d+(?:\.\d+)?"


def parse_bbox_xml(xml: str) -> list[list[tuple[str, float, float, float, float]]]:
    """Pages of words (text, x0, y0, x1, y1) in Poppler -bbox convention."""
    pages = []
    for pm in _PAGE_RE.finditer(xml):
        start = pm.end()
        nxt = xml.find('<div class="page"', start)
        seg = xml[start:nxt if nxt != -1 else len(xml)]
        words = []
        for wm in _WORD_RE.finditer(seg):
            x0, y0, x1, y2 = (float(t) for t in wm.group(1).split())
            words.append((wm.group(2), x0, y0, x1, y2))
        pages.append(words)
    return pages


def extract_words(bin: str, pdf_path) -> list[list[tuple[str, float, float, float, float]]]:
    proc = subprocess.run([bin, "-bbox", str(pdf_path), "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("pdftotext failed")
    return parse_bbox_xml(proc.stdout)


# ---------- rubric ----------

def find_occurrences(words, value: str):
    """Boundary-aware exact occurrences of ``value`` in a page's words.
    Returns list of (idx_start, idx_end, union_rect) with rect padded.
    Words are line-grouped tokens, so a match aligns on word boundaries by
    construction (no substring hits inside longer tokens)."""
    pad = 1.5
    target = value.split()
    out = []
    i = 0
    while i <= len(words) - len(target):
        if words[i][0] == target[0]:
            j = i
            while j - i < len(target) and j < len(words) and words[j][0] == target[j - i]:
                j += 1
            if j - i == len(target):
                xs0 = min(w[1] for w in words[i:j]); ys0 = min(w[2] for w in words[i:j])
                xs1 = max(w[3] for w in words[i:j]); ys1 = max(w[4] for w in words[i:j])
                out.append((i, j, (xs0 - pad, ys0 - pad, xs1 + pad, ys1 + pad)))
                i = j
                continue
        i += 1
    return out


def _intersects(rect, word) -> bool:
    return not (word[1] >= rect[2] or word[3] <= rect[0] or word[2] >= rect[3] or word[4] <= rect[1])


def region_output(words, rect, original):
    """Classify the output page region under the source occurrence rect.
    Returns (residual, visible_replacement, replacement_token)."""
    inside = [w for w in words if _intersects(rect, w)]
    text = [w[0] for w in inside]
    residual = text == original
    visible = bool(text) and not residual
    repl = None
    for t in text:
        if t not in original:
            repl = t
            break
    return residual, visible, repl


def role_field_values(words, keys=ROLE_KEYWORDS, y_tol=2.0):
    """Heuristic proxy: values following a strict title-block role keyword on
    the same visual line (grouped by y-center). Numeric-ish tokens (drawing
    numbers, scales, dates) end the value; they are never identities."""
    lines: list[tuple[float, list[tuple[str, float]]]] = []
    for t, x0, y0, x1, y2 in words:
        yc = (y0 + y2) / 2
        for ln in lines:
            if abs(ln[0] - yc) <= y_tol:
                ln[1].append((t, x0))
                break
        else:
            lines.append((yc, [(t, x0)]))
    vals = []
    for _, ln in lines:
        toks = [t for t, _ in sorted(ln, key=lambda tt: tt[1])]
        for i, t in enumerate(toks):
            if t.rstrip(":").lower() in keys:
                tail = []
                for x in toks[i + 1:i + 4]:
                    if _NUMERICISH.match(x):
                        break  # drawing number / scale / date
                    if x.isalpha() and x.isupper():
                        break  # acronym / drawing-number prefix, not an identity
                    tail.append(x)
                if tail:
                    vals.append(" ".join(tail))
    return vals


def audit_file(src_words, out_words, labels, values_for_groups):
    m = {"occ_total": 0, "occ_residual": 0, "region_checked": 0, "region_visible": 0,
         "alias_map": {}, "role_proxy": 0}
    for value in labels:
        for pi in range(len(src_words)):
            occ = find_occurrences(src_words[pi], value)
            m["occ_total"] += len(occ)
            owords = out_words[pi] if pi < len(out_words) else []
            for i, j, rect in occ:
                original = [w[0] for w in src_words[pi][i:j]]
                o = find_occurrences(owords, value)
                m["occ_residual"] += len(o)
                residual, visible, repl = region_output(owords, rect, original)
                m["region_checked"] += 1
                m["region_visible"] += int(visible)
                if visible and repl:
                    m["alias_map"].setdefault(value, {}).setdefault(repl, 0)
                    m["alias_map"][value][repl] += 1
                values_for_groups[value] = values_for_groups.get(value, 0) + 1
    m["role_proxy"] = sum(len(role_field_values(p)) for p in src_words)
    return m


def new_pkg() -> dict:
    return {"files": 0, "fallback": 0, "missing": 0, "occ_total": 0, "occ_residual": 0,
            "region_checked": 0, "region_visible": 0, "alias_map": {}, "role_proxy": 0,
            "values_seen": {}}


def finalize_pkg(m: dict) -> dict:
    total = m["occ_total"]
    ver = col = 0
    by_alias: dict[str, list[str]] = {}
    for value, aliases in m["alias_map"].items():
        top = max(aliases.values())
        if len(aliases) == 1:
            ver += 1
        for a in aliases:
            by_alias.setdefault(a, []).append(value)
    for a, vals in by_alias.items():
        col += len(vals) * (len(vals) - 1) // 2
    rep_groups = len({v for v, c in m["values_seen"].items() if c > 1})
    out = {
        "files": m["files"], "fallback": m["fallback"], "missing": m["missing"],
        "occ_total": total, "occ_residual": m["occ_residual"],
        "removal_rate": round(1 - m["occ_residual"] / total, 4) if total else None,
        "region_checked": m["region_checked"],
        "region_visible_rate": round(m["region_visible"] / m["region_checked"], 4)
        if m["region_checked"] else None,
        "verified_alias_values": ver,
        "collision_triplets": col,
        "repeated_value_groups": rep_groups,
        "role_field_proxy": m["role_proxy"],
        "role_field_proxy_is_heuristic": True,
    }
    return out


def run_audit(args) -> dict:
    rows = load_manifest(args.manifest)
    if args.limit:
        rows = rows[:args.limit]
    labels = load_labels(args.labels)
    if args.poppler:
        if shutil.which(args.poppler) is None:
            print(f"error: poppler binary {args.poppler!r} not found on this machine", file=sys.stderr)
            raise SystemExit(2)
        kind = "pdftotext-bbox"

        def words_of(key: str, root: str, rel: str):  # noqa: ARG001 - key unused
            return extract_words(args.poppler, os.path.join(root, rel))
    else:
        # bbox-dir layout: <bbox-dir>/<key>/<rel>.xml with key in {src, a, b}
        kind = "bbox-dir"

        def words_of(key: str, root: str, rel: str):  # noqa: ARG001 - root unused
            xml = open(os.path.join(args.bbox_dir, key, rel + ".xml"), "r",
                       encoding="utf-8").read()
            return parse_bbox_xml(xml)

    per_split: dict[str, dict] = {}
    sha_mismatch = 0
    family_counts: dict[str, int] = {}
    for r in rows:
        sp = r["split"]
        fam = per_split.setdefault(sp, {"source": new_pkg(), "out-a": new_pkg(),
                                        "out-b": new_pkg()})
        family_counts[r["family"]] = family_counts.get(r["family"], 0) + 1
        src = os.path.join(args.source, r["path"])
        if not os.path.exists(src):
            continue
        if _sha_file(src) != r["sha256"]:
            sha_mismatch += 1
        try:
            swords = words_of("src", args.source, r["path"])
        except Exception:
            continue
        pkg = fam["source"]
        pkg["files"] += 1
        m = audit_file(swords, swords, labels, pkg["values_seen"])
        for k in ("occ_total", "occ_residual", "region_checked", "region_visible", "role_proxy"):
            pkg[k] += m[k]
        for v, a in m["alias_map"].items():
            for x, c in a.items():
                pkg["alias_map"].setdefault(v, {}).setdefault(x, 0)
                pkg["alias_map"][v][x] += c
        for name in ("a", "b"):
            root = getattr(args, f"out_{name}")
            if not root:
                continue
            pkg2 = fam[f"out-{name}"]
            opath = os.path.join(root, r["path"])
            if not os.path.exists(opath):
                pkg2["missing"] += 1
                continue
            pkg2["files"] += 1
            if _sha_file(opath) == _sha_file(src):
                pkg2["fallback"] += 1
                continue  # rejected/fallback output: original bytes, nothing to audit
            try:
                owords = words_of(name, root, r["path"])
            except Exception:
                continue
            m2 = audit_file(swords, owords, labels, pkg2["values_seen"])
            for k in ("occ_total", "occ_residual", "region_checked", "region_visible", "role_proxy"):
                pkg2[k] += m2[k]
            for v, a in m2["alias_map"].items():
                for x, c in a.items():
                    pkg2["alias_map"].setdefault(v, {}).setdefault(x, 0)
                    pkg2["alias_map"][v][x] += c
    report = {
        "tool": TOOL, "version": VERSION, "mode": args.mode,
        "manifest_sha256": _sha_file(args.manifest), "manifest_rows": len(rows),
        "family_counts": family_counts, "sha_mismatch": sha_mismatch,
        "extractor": kind,
        "splits": {sp: {p: finalize_pkg(m) for p, m in
                        (("source", f["source"]), ("out-a", f["out-a"]), ("out-b", f["out-b"]))}
                   for sp, f in per_split.items()},
    }
    return report


def run_inventory(args) -> dict:
    rows = load_manifest(args.manifest)
    import os
    fam: dict[str, int] = {}
    sp: dict[str, int] = {}
    missing = mismatch = 0
    for r in rows:
        fam[r["family"]] = fam.get(r["family"], 0) + 1
        sp[r["split"]] = sp.get(r["split"], 0) + 1
        p = f"{args.source}/{r['path']}"
        if not os.path.exists(p):
            missing += 1
            continue
        if _sha_file(p) != r["sha256"]:
            mismatch += 1
    return {"tool": TOOL, "version": VERSION, "mode": "inventory",
            "manifest_sha256": _sha_file(args.manifest), "manifest_rows": len(rows),
            "family_counts": fam, "split_counts": sp, "missing_files": missing,
            "sha_mismatch": mismatch}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="Independent Poppler-based occurrence/control audit. "
                    "Emits only machine-readable anonymized aggregates (counts/rates/opaque "
                    "sha checkpoints); never emits source text, paths, or labels.")
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("inventory", "sample", "audit"):
        s = sub.add_parser(name, help=f"{name} mode")
        s.add_argument("--source", required=True, help="raw corpus directory")
        s.add_argument("--manifest", required=True, help="split manifest JSON (family/path/sha256/split)")
        s.add_argument("--labels", help="approved labels JSON {entity: [literal values]}")
        s.add_argument("--out-a", help="scored package A directory")
        s.add_argument("--out-b", help="scored package B directory")
        s.add_argument("--poppler", help="pdftotext binary (default when set)")
        s.add_argument("--bbox-dir", help="directory of pre-extracted XML: <bbox-dir>/{src,a,b}/<relpath>.xml")
        s.add_argument("--limit", type=int, help="sample: first N qualified rows")
    args = ap.parse_args(argv)
    if args.mode == "inventory":
        report = run_inventory(args)
    else:
        if not args.poppler and not args.bbox_dir:
            ap.error("audit requires --poppler or --bbox-dir")
        args.limit = args.limit if args.mode == "sample" else 0
        report = run_audit(args)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
