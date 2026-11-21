#!/usr/bin/env python3
"""Independent, conservative PDF occurrence audit (private local use; aggregate-only export).

The rubric is built from the *raw* source PDFs by an independent extractor (Poppler ``pdftotext -layout`` by
default; ``--extractor mupdf`` exists only for synthetic tests on hosts without Poppler). It never imports the
de-identification pipeline. Rubric (strict, proxy for ground truth):
  * literal e-mail addresses;
  * title-block role fields: ``<ROLE>[:] <value>`` where ROLE is a strict label (DRAWN, CHECKED, APPROVED, ...)
    and value looks like a person (2-3 capitalised alphabetic tokens / initial + surname) or a company
    (alphabetic tokens ending in a legal suffix). Values containing digits, drawing/boilerplate words are dropped.
For each package (``--package NAME=DIR``; output path = DIR/<relative path>) every rubric occurrence is scored:
  removed            original value absent (boundary-aware) from that output file's text; a missing/unreadable
                     output counts as NOT removed (stays in the denominator)
  visible_replaced   removed AND the same role label is still followed by a non-empty, different value
  consistent groups  exact value repeated in >=2 files: all visible replacements identical
  collisions         two distinct values whose visible replacements are identical
Only counts/rates and an opaque checkpoint are written to ``--out`` (safe to send back). ``--private-report``
(optional, keep on E:) lists per-occurrence rows with values for local triage.

Modes: ``inventory`` (read-only counts of manifest rows/splits/existing files), ``sample`` (first --limit
selected rows), ``full``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROLES = ("DRAWN BY", "DRAWN", "DRN", "CHECKED BY", "CHECKED", "CHK", "APPROVED BY", "APPROVED", "APPD",
         "DESIGNED BY", "DESIGNED", "DESIGNER", "ENGINEER", "PREPARED BY", "AUTHOR", "COMPANY", "CUSTOMER")
_ROLE_RX = re.compile(r"(?<![A-Za-z])(" + "|".join(r"\s+".join(map(re.escape, r.split())) for r in ROLES)
                      + r")\s*[:.]?\s+(.+)$")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
LEGAL = r"(?:Inc|Ltd|LLC|GmbH|Corp|Corporation|Company|Co|AG|SA|BV|PLC|Limited|Fabrication|Engineering)\.?"
_TOK = r"[A-Z][a-z]+(?:-[A-Z][a-z]+)?"
PERSON = re.compile(rf"^(?:[A-Z]\.\s?)?(?:{_TOK}|[A-Z]\.)(?:\s+(?:[A-Z]\.|{_TOK})){{1,2}}$")
COMPANY = re.compile(rf"^(?:[A-Z][A-Za-z&]+\s+){{1,4}}{LEGAL}$")
STOP = {"Scale", "Sheet", "Date", "Rev", "Revision", "Title", "Drawing", "Size", "Weight", "Material", "Finish",
        "Assembly", "Valve", "Load", "Check", "Part", "Number", "Tolerance", "Unless", "Otherwise", "Specified",
        "None", "See", "Note", "Notes", "By", "Name", "Signature", "Approved", "Drawn", "Checked", "Third",
        "Angle", "Projection", "Do", "Not", "Copy", "Proprietary", "Confidential"}


def extract(path: Path, extractor: str) -> list[str] | None:
    try:
        if extractor == "pdftotext":
            r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True,
                               timeout=120)
            if r.returncode:
                return None
            txt = r.stdout.decode("utf-8", "replace")
        else:
            import pymupdf
            with pymupdf.open(path) as d:
                txt = "\n".join(p.get_text(sort=True) for p in d)
    except Exception:
        return None
    return [l.rstrip() for l in txt.splitlines() if l.strip()]  # keep column gaps


def _value(raw: str) -> str | None:
    # split columns: layout text separates fields by runs of spaces (already collapsed) -> take up to 4 tokens
    v = re.split(r"\s{2,}|\s(?=[A-Z]{2,}\b)|\s(?=\d)", raw.strip())[0].strip(" ,;")
    if not v or re.search(r"\d", v) or any(t.strip(".") in STOP for t in v.split()):
        return None
    if PERSON.match(v):
        return v
    if COMPANY.match(v):
        return v
    return None


def rubric(lines: list[str]) -> list[tuple[str, str, str]]:
    """(kind, role, value) occurrences."""
    out = []
    for ln in lines:
        for m in EMAIL.finditer(ln):
            out.append(("email", "", m.group(0)))
        m = _ROLE_RX.search(ln)
        if m:
            v = _value(m.group(2))
            if v:
                out.append(("role", " ".join(m.group(1).split()), v))
    return out


def _bound(v: str) -> re.Pattern:
    return re.compile(r"(?<![^\W_])(?<![^\W_]-)" + r"\s+".join(map(re.escape, v.split())) + r"(?![^\W_]|-[^\W_])")


def replacement(lines: list[str], role: str) -> list[str]:
    rx = re.compile(r"(?<![A-Za-z])" + r"\s+".join(map(re.escape, role.split())) + r"\s*[:.]?\s+(\S+(?: \S+)*)")
    return [re.split(r"\s{2,}", m.group(1))[0] for ln in lines for m in [rx.search(ln)] if m]


def load_rows(manifest: Path, split: str | None) -> list[dict]:
    data = json.loads(manifest.read_text())
    rows = data["rows"] if isinstance(data, dict) and "rows" in data else data
    return [r for r in rows if (split is None or r.get("split") == split) and str(r["path"]).lower().endswith(".pdf")]


def audit(source: Path, rows: list[dict], packages: dict[str, Path], extractor: str, private=None) -> dict:
    occ = []  # (rel, kind, role, value, family)
    unreadable_src = 0
    for r in rows:
        lines = extract(source / r["path"], extractor)
        if lines is None:
            unreadable_src += 1
            continue
        for kind, role, v in rubric(lines):
            occ.append((r["path"], kind, role, v, r.get("family", "")))
    files_by_value = defaultdict(set)
    for rel, _, _, v, _ in occ:
        files_by_value[v].add(rel)
    repeated = {v for v, fs in files_by_value.items() if len(fs) >= 2}
    report = {"rows": len(rows), "source_unreadable": unreadable_src, "occurrences": len(occ),
              "by_kind": dict(Counter(k for _, k, _, _, _ in occ)), "distinct_values": len(files_by_value),
              "repeated_groups": len(repeated), "proxy_note": "heuristic rubric; counts are proxies",
              "checkpoint": hashlib.sha256("\n".join(sorted(
                  hashlib.sha256(f"{a}|{b}|{c}|{d}".encode()).hexdigest() for a, b, c, d, _ in occ)).encode()
              ).hexdigest()[:16], "packages": {}}
    for name, pdir in packages.items():
        cache: dict[str, list[str] | None] = {}
        c = Counter({k: 0 for k in ("occurrences", "removed", "original_present", "visible_replaced",
                                    "output_missing_or_unreadable")})
        rep_by_value = defaultdict(set)
        for rel, kind, role, v, fam in occ:
            if rel not in cache:
                p = pdir / rel
                cache[rel] = extract(p, extractor) if p.is_file() else None
            lines = cache[rel]
            c["occurrences"] += 1
            if lines is None:
                c["output_missing_or_unreadable"] += 1
                row = "missing"
            else:
                txt = "\n".join(lines)
                if _bound(v).search(txt):
                    c["original_present"] += 1
                    row = "present"
                else:
                    c["removed"] += 1
                    row = "removed"
                    if kind == "role":
                        reps = [x for x in replacement(lines, role) if x and not _bound(v).search(x)]
                        if reps:
                            c["visible_replaced"] += 1
                            rep_by_value[v].add(reps[0])
            if private is not None:
                private.write(json.dumps({"package": name, "path": rel, "kind": kind, "role": role,
                                          "value": v, "result": row}) + "\n")
        groups = [v for v in repeated if v in rep_by_value]
        c["repeated_groups_scored"] = len(groups)
        c["repeated_groups_consistent"] = sum(len(rep_by_value[v]) == 1 for v in groups)
        owner = defaultdict(set)
        for v, reps in rep_by_value.items():
            for x in reps:
                owner[x].add(v)
        c["replacement_collisions"] = sum(len(vs) > 1 for vs in owner.values())
        n = c["occurrences"] or 1
        report["packages"][name] = dict(c, removed_rate=round(c["removed"] / n, 4),
                                        present_rate=round(c["original_present"] / n, 4))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("inventory", "sample", "full"))
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True, help="JSON rows with path/split[/family/sha256]")
    ap.add_argument("--split", default="development")
    ap.add_argument("--package", action="append", default=[], help="NAME=DIR (repeatable)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--extractor", choices=("pdftotext", "mupdf"), default="pdftotext")
    ap.add_argument("--out", type=Path, help="aggregate-only JSON (safe to export)")
    ap.add_argument("--private-report", type=Path, help="per-occurrence JSONL with values (keep local)")
    a = ap.parse_args(argv)
    if a.extractor == "pdftotext" and not shutil.which("pdftotext"):
        ap.error("pdftotext (Poppler) not on PATH")
    rows = load_rows(a.manifest, a.split or None)
    packages = dict(p.split("=", 1) for p in a.package)
    packages = {k: Path(v) for k, v in packages.items()}
    if a.mode == "inventory":
        rep = {"rows": len(rows), "source_present": sum((a.source / r["path"]).is_file() for r in rows),
               "families": len({r.get("family") for r in rows}),
               "packages": {k: sum((v / r["path"]).is_file() for r in rows) for k, v in packages.items()}}
    else:
        if a.mode == "sample":
            rows = rows[:a.limit]
        priv = a.private_report.open("w") if a.private_report else None
        try:
            rep = audit(a.source, rows, packages, a.extractor, priv)
        finally:
            if priv:
                priv.close()
    s = json.dumps(rep, indent=1, sort_keys=True)
    if a.out:
        a.out.write_text(s + "\n")
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
