#!/usr/bin/env python3
"""Real-file integration run (NOT part of the unit suite; outputs are never committed).

Runs the PDF and DXF transformers on the first representative file found in each
directory, writing only under --out-dir. Sources are opened read-only and their
SHA-256 is checked before/after. No identities are guessed: with no
--pdf-map/--dxf-map, a *probe* string (a single alphabetic word taken from the
file's own visible text) is mapped purely to exercise redaction; this is stated
in the report. If a file has no extractable text, only metadata/page invariants
are validated and no visible redaction is claimed.

    uv run --extra pdf --extra cad python scripts/integration_real.py \\
        --pdf-dir /srv/new2/machina-deid/representative-pdf \\
        --dxf-dir /srv/new2/machina-deid/representative-dxf \\
        --out-dir /srv/new2/machina-deid/work/phase2-integration
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ezdxf  # noqa: E402
import pymupdf  # noqa: E402

from machina_deid.dxf_transform import _entity_texts, transform_dxf  # noqa: E402
from machina_deid.idmap import IdentityMap, load  # noqa: E402
from machina_deid.pdf_transform import transform_pdf  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def first(d: Path, suffix: str) -> Path | None:
    files = sorted(p for p in d.rglob("*") if p.is_file() and p.suffix.lower() == suffix)
    return files[0] if files else None


def probe_word(text: str) -> str | None:
    words = re.findall(r"[A-Za-z]{5,}", text)
    return max(words, key=len) if words else None


def run_pdf(src: Path, out: Path, map_path: str | None) -> dict:
    info = {"source_bytes": src.stat().st_size, "source_sha256": sha(src)}
    doc = pymupdf.open(src)
    text = "\n".join(p.get_text() for p in doc)
    info.update(pages=doc.page_count, extractable_text_chars=len(text.strip()),
                source_metadata_nonempty=sorted(k for k, v in doc.metadata.items() if v and k != "format"),
                source_attachments=len(doc.embfile_names()))
    doc.close()
    if map_path:
        idmap, probe = load(map_path), False
    else:
        w = probe_word(text)
        idmap, probe = (IdentityMap(strings={w: "Probe-PDF"}) if w else IdentityMap()), bool(w)
    info["identity_source"] = "explicit map" if map_path else ("probe word from document text" if probe else "none (no text)")
    res = transform_pdf(src, out, idmap, overwrite=True)
    info["result"] = res.to_dict()
    info["visible_redaction_exercised"] = bool(sum(res.matches.values()))
    info["source_sha256_after"] = sha(src)
    info["source_unchanged"] = info["source_sha256"] == info["source_sha256_after"]
    return info


def run_dxf(src: Path, out: Path, map_path: str | None) -> dict:
    info = {"source_bytes": src.stat().st_size, "source_sha256": sha(src)}
    doc = ezdxf.readfile(src)
    texts = [t for _, t in _entity_texts(doc) if t]
    types: dict[str, int] = {}
    for lay in list(doc.layouts) + list(doc.blocks):
        for e in lay:
            types[e.dxftype()] = types.get(e.dxftype(), 0) + 1
    info.update(dxf_version=doc.dxfversion, entity_types=dict(sorted(types.items())), text_entities=len(texts))
    if map_path:
        idmap, probe = load(map_path), False
    else:
        w = probe_word("\n".join(texts))
        idmap, probe = (IdentityMap(strings={w: "Probe-DXF"}) if w else IdentityMap()), bool(w)
    info["identity_source"] = "explicit map" if map_path else ("probe word from TEXT/MTEXT/ATTRIB" if probe else "none (no text)")
    res = transform_dxf(src, out, idmap, overwrite=True)
    info["result"] = res.to_dict()
    info["text_replaced"] = bool(sum(res.text_replacements.values()))
    info["source_sha256_after"] = sha(src)
    info["source_unchanged"] = info["source_sha256"] == info["source_sha256_after"]
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", type=Path, required=True)
    ap.add_argument("--dxf-dir", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--pdf-map")
    ap.add_argument("--dxf-map")
    a = ap.parse_args()
    out_dir = a.out_dir.resolve()
    for d in (a.pdf_dir, a.dxf_dir):
        d = d.resolve()
        if out_dir == d or d in out_dir.parents or out_dir in d.parents:
            ap.error("--out-dir must not overlap a source directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"policy": "no upload / submission performed"}
    ok = True
    for kind, d, fn, mp in (("pdf", a.pdf_dir, run_pdf, a.pdf_map), ("dxf", a.dxf_dir, run_dxf, a.dxf_map)):
        src = first(d, "." + kind)
        if src is None:
            report[kind] = {"error": "no file found"}
            ok = False
            continue
        try:
            report[kind] = {"file": src.name, **fn(src, out_dir / f"{kind}-out{src.suffix}", mp)}
            ok &= report[kind]["result"]["ok"] and report[kind]["source_unchanged"]
        except Exception as exc:  # report category only
            report[kind] = {"file": src.name, "error": type(exc).__name__}
            ok = False
    (out_dir / "integration-report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
