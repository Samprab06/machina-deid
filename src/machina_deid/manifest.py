"""Manifest parsing and storage/prioritization reporting.

The manifest is streamed once. Raw paths are held only in memory during the
analysis; reports identify files by a short SHA-256 path id plus extension,
top-level directory and size, so a committed report never contains raw names
unless ``include_paths=True`` is passed explicitly.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ARCHIVE_EXTS = {".zip", ".7z", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".z", ".rar"}
GIB = 1024**3

# extension -> (tier, handler status). Anything else: P4.
_PRIORITY = {
    ".pdf": ("P1", "implemented (PyMuPDF)"),
    ".dxf": ("P1", "implemented (ezdxf)"),
    ".step": ("P1", "implemented (header FILE_NAME rewrite; DATA untouched)"),
    ".stp": ("P1", "implemented (header FILE_NAME rewrite; DATA untouched)"),
    ".zip": ("P2", "planned (container; recurse)"),
    ".7z": ("P2", "planned (container; recurse)"),
    ".png": ("P3", "planned (metadata strip)"),
    ".jpg": ("P3", "planned (metadata strip)"),
    ".jpeg": ("P3", "planned (metadata strip)"),
    ".sldprt": ("P3", "none (binary; exception record)"),
    ".sldasm": ("P3", "none (binary; exception record)"),
    ".slddrw": ("P3", "none (binary; exception record)"),
    ".ipt": ("P3", "none (binary; exception record)"),
    ".iam": ("P3", "none (binary; exception record)"),
    ".idw": ("P3", "none (binary; exception record)"),
    ".dwg": ("P3", "none (binary; exception record)"),
}


@dataclass(frozen=True)
class Row:
    path: str
    size: int
    md5: str

    @property
    def ext(self) -> str:
        return PurePosixPath(self.path).suffix.lower() or "<none>"

    @property
    def top_dir(self) -> str:
        parts = PurePosixPath(self.path).parts
        return parts[0] if len(parts) > 1 else ""

    @property
    def path_id(self) -> str:
        return hashlib.sha256(self.path.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*", text, re.I)
    if not m:
        raise ValueError(f"bad size: {text!r}")
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": GIB, "t": 1024**4}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def parse_manifest(path: str | Path) -> tuple[list[Row], int]:
    """Return (rows, malformed_line_count). Header/comment lines start with '#'."""
    rows: list[Row] = []
    bad = 0
    with Path(path).open("r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            try:
                rows.append(Row(cols[0], int(cols[1]), cols[2].strip().lower() if len(cols) > 2 else ""))
            except (IndexError, ValueError):
                bad += 1
    return rows, bad


def _fmt(row: Row, include_paths: bool) -> dict:
    top_dir = row.top_dir if include_paths else (
        hashlib.sha256(row.top_dir.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        if row.top_dir else "")
    d = {"path_id": row.path_id, "ext": row.ext, "top_dir": top_dir, "bytes": row.size}
    if include_paths:
        d["path"] = row.path
    return d


def measure_mounts(reserves: dict[str, int]) -> list[dict]:
    """Measure free space for each configured path; reserve is bytes kept free."""
    out = []
    for p, reserve in reserves.items():
        probe = Path(p)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        du = shutil.disk_usage(probe)
        out.append(
            {
                "path": p,
                "device": probe.stat().st_dev,
                "total": du.total,
                "free": du.free,
                "reserve": reserve,
                "usable": max(0, du.free - reserve),
            }
        )
    return out


def project_usage(rows: list[Row], mounts: list[dict], batch_bytes: int, output_ratio: float = 1.0,
                  archive_expansion: float = 3.0, staged_input: bool = True) -> dict:
    """Project space for one batch and for the whole corpus.

    One batch needs: staged input (if copied locally) + output (input*ratio) +
    scratch (archive bytes expanded by ``archive_expansion``). The per-byte
    multiplier uses the corpus-wide archive share.
    """
    total = sum(r.size for r in rows)
    arch = sum(r.size for r in rows if r.ext in ARCHIVE_EXTS)
    arch_share = arch / total if total else 0.0
    mult = (1.0 if staged_input else 0.0) + output_ratio + arch_share * archive_expansion
    batch_need = batch_bytes * mult
    per_mount = []
    for m in mounts:
        per_mount.append(
            {
                "path": m["path"],
                "usable": m["usable"],
                "batch_fits": batch_need <= m["usable"],
                "max_safe_batch_bytes": int(m["usable"] / mult) if mult else 0,
                "output_only_corpus_bytes": int(total * output_ratio),
                "output_fits_corpus": total * output_ratio <= m["usable"],
            }
        )
    return {
        "assumptions": {"output_ratio": output_ratio, "archive_expansion": archive_expansion,
                        "staged_input": staged_input, "archive_share": round(arch_share, 6),
                        "per_byte_multiplier": round(mult, 4)},
        "batch_bytes": batch_bytes,
        "batch_projected_usage": int(batch_need),
        "corpus_bytes": total,
        "batches_for_corpus": -(-total // batch_bytes) if batch_bytes else None,
        "files_larger_than_batch": sum(1 for r in rows if r.size > batch_bytes),
        "mounts": per_mount,
    }


def analyze(rows: list[Row], malformed: int = 0, *, top_n: int = 20, long_path: int = 200,
            long_component: int = 100, reserves: dict[str, int] | None = None,
            batch_bytes: int = 5 * GIB, output_ratio: float = 1.0, archive_expansion: float = 3.0,
            include_paths: bool = False) -> dict:
    total = sum(r.size for r in rows)
    by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        e = by_ext[r.ext]
        e[0] += 1
        e[1] += r.size
    ext_table = [{"ext": k, "files": v[0], "bytes": v[1]} for k, v in sorted(by_ext.items(), key=lambda kv: -kv[1][1])]

    largest = [_fmt(r, include_paths) for r in sorted(rows, key=lambda r: -r.size)[:top_n]]

    arch_rows = [r for r in rows if r.ext in ARCHIVE_EXTS]
    arch_by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in arch_rows:
        arch_by_ext[r.ext][0] += 1
        arch_by_ext[r.ext][1] += r.size
    archives = {
        "files": len(arch_rows),
        "bytes": sum(r.size for r in arch_rows),
        "by_ext": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(arch_by_ext.items())},
        "largest": [_fmt(r, include_paths) for r in sorted(arch_rows, key=lambda r: -r.size)[:top_n]],
    }

    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.md5:
            groups[r.md5].append(r)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    dup_sorted = sorted(dups.items(), key=lambda kv: -(len(kv[1]) - 1) * kv[1][0].size)
    duplicates = {
        "groups": len(dups),
        "redundant_files": sum(len(v) - 1 for v in dups.values()),
        "redundant_bytes": sum((len(v) - 1) * v[0].size for v in dups.values()),
        "largest_groups": [
            {"md5": k, "copies": len(v), "bytes_each": v[0].size, "redundant_bytes": (len(v) - 1) * v[0].size,
             "members": [_fmt(r, include_paths) for r in v[:5]]}
            for k, v in dup_sorted[:top_n]
        ],
        "blank_md5_files": sum(1 for r in rows if not r.md5),
    }

    longp = [r for r in rows
             if len(r.path) > long_path or any(len(c) > long_component for c in PurePosixPath(r.path).parts)]
    long_paths = {
        "threshold_path": long_path, "threshold_component": long_component, "count": len(longp),
        "max_path_length": max((len(r.path) for r in rows), default=0),
        "longest": [dict(_fmt(r, include_paths), length=len(r.path))
                    for r in sorted(longp, key=lambda r: -len(r.path))[:top_n]],
    }

    prio = []
    for e in ext_table:
        tier, handler = _PRIORITY.get(e["ext"], ("P4", "none (record + pass-through)"))
        prio.append({**e, "tier": tier, "handler": handler,
                     "pct_files": round(100 * e["files"] / len(rows), 3) if rows else 0,
                     "pct_bytes": round(100 * e["bytes"] / total, 3) if total else 0})
    prio.sort(key=lambda p: (p["tier"], -p["bytes"]))

    mounts = measure_mounts(reserves or {})
    projection = project_usage(rows, mounts, batch_bytes, output_ratio, archive_expansion)
    if not include_paths:
        mounts = [
            {**{k: v for k, v in mount.items() if k != "path"},
             "mount_id": hashlib.sha256(mount["path"].encode("utf-8", "surrogateescape")).hexdigest()[:12]}
            for mount in mounts
        ]
        projection = {**projection, "mounts": [
            {**{k: v for k, v in mount.items() if k != "path"},
             "mount_id": hashlib.sha256(mount["path"].encode("utf-8", "surrogateescape")).hexdigest()[:12]}
            for mount in projection["mounts"]
        ]}
    return {
        "summary": {"files": len(rows), "bytes": total, "malformed_lines": malformed,
                    "empty_files": sum(1 for r in rows if r.size == 0)},
        "by_extension": ext_table,
        "largest_files": largest,
        "archives": archives,
        "duplicates": duplicates,
        "long_paths": long_paths,
        "prioritization": prio,
        "storage": {"mounts": mounts, "projection": projection},
        "paths_included": include_paths,
    }


def _gib(n: int) -> str:
    return f"{n / GIB:.2f} GiB"


def to_markdown(rep: dict) -> str:
    s = rep["summary"]
    L = ["# Manifest storage report", "",
         f"- Files: {s['files']:,}; bytes: {s['bytes']:,} ({_gib(s['bytes'])}); empty: {s['empty_files']};"
         f" malformed lines: {s['malformed_lines']}",
         f"- Raw paths included: {rep['paths_included']}", "", "## Counts/bytes by extension", "",
         "| ext | files | bytes |", "|---|---:|---:|"]
    L += [f"| {e['ext']} | {e['files']:,} | {e['bytes']:,} |" for e in rep["by_extension"][:40]]
    L += ["", "## Largest files", "", "| path id | ext | dir | bytes |", "|---|---|---|---:|"]
    L += [f"| {f['path_id']} | {f['ext']} | {f['top_dir']} | {f['bytes']:,} |" for f in rep["largest_files"]]
    a = rep["archives"]
    L += ["", "## Archives", "", f"- {a['files']:,} archive files, {a['bytes']:,} bytes ({_gib(a['bytes'])})"]
    L += [f"- {k}: {v['files']:,} files, {v['bytes']:,} bytes" for k, v in a["by_ext"].items()]
    d = rep["duplicates"]
    L += ["", "## Duplicate hash groups (md5)", "",
          f"- {d['groups']:,} groups; {d['redundant_files']:,} redundant files; {d['redundant_bytes']:,} redundant"
          f" bytes; blank md5: {d['blank_md5_files']:,}", "",
          "| md5 | copies | bytes each | redundant bytes |", "|---|---:|---:|---:|"]
    L += [f"| {g['md5']} | {g['copies']} | {g['bytes_each']:,} | {g['redundant_bytes']:,} |"
          for g in d["largest_groups"][:10]]
    lp = rep["long_paths"]
    L += ["", "## Long paths", "",
          f"- {lp['count']:,} paths exceed {lp['threshold_path']} chars or a {lp['threshold_component']}-char"
          f" component; max length {lp['max_path_length']}", ""]
    L += ["## Prioritization", "", "| tier | ext | files | bytes | % files | % bytes | handler |",
          "|---|---|---:|---:|---:|---:|---|"]
    L += [f"| {p['tier']} | {p['ext']} | {p['files']:,} | {p['bytes']:,} | {p['pct_files']} | {p['pct_bytes']} |"
          f" {p['handler']} |" for p in rep["prioritization"][:40]]
    st = rep["storage"]
    pr = st["projection"]
    L += ["", "## Storage (measured free space minus reserve)", "",
          "| mount | free | reserve | usable |", "|---|---:|---:|---:|"]
    L += [f"| {m.get('path', m.get('mount_id'))} | {_gib(m['free'])} | {_gib(m['reserve'])} | {_gib(m['usable'])} |"
          for m in st["mounts"]]
    A = pr["assumptions"]
    L += ["", f"Projection: batch {_gib(pr['batch_bytes'])}, multiplier {A['per_byte_multiplier']} "
          f"(output ratio {A['output_ratio']}, archive share {A['archive_share']}, archive expansion"
          f" {A['archive_expansion']}) => {_gib(pr['batch_projected_usage'])} per batch; corpus"
          f" {_gib(pr['corpus_bytes'])} in {pr['batches_for_corpus']} batches; files larger than batch:"
          f" {pr['files_larger_than_batch']}", "",
          "| mount | batch fits | max safe batch | corpus output fits |", "|---|---|---:|---|"]
    L += [f"| {m.get('path', m.get('mount_id'))} | {m['batch_fits']} | {_gib(m['max_safe_batch_bytes'])} | {m['output_fits_corpus']} |"
          for m in pr["mounts"]]
    return "\n".join(L) + "\n"


def write_reports(rep: dict, out_dir: str | Path, stem: str = "manifest-report") -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    j, m = out / f"{stem}.json", out / f"{stem}.md"
    j.write_text(json.dumps(rep, indent=2, sort_keys=True), encoding="utf-8")
    m.write_text(to_markdown(rep), encoding="utf-8")
    return [j, m]
