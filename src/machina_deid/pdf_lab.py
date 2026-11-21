"""Private local PDF annotation / run / evaluation CLI (attempt 2c).

    python -m machina_deid.pdf_lab sample    --input IN --n 120 --seed-file S --out LIST
    python -m machina_deid.pdf_lab survey    --input IN --list LIST                     # redacted aggregates
    python -m machina_deid.pdf_lab propose   --input IN --list LIST --out DRAFT.jsonl [--crops DIR]
    python -m machina_deid.pdf_lab build-map --input IN --list LIST --secret-file S --map MAP [--catalog DB]
    python -m machina_deid.pdf_lab run       --input IN --list LIST --output OUT --secret-file S (--map MAP | --derive)
    python -m machina_deid.pdf_lab evaluate  --input IN --output OUT --labels LABELS [--trace OUT/trace.jsonl]
                                             [--holdout-frac 0.3 --seed-file S]

Files that contain raw values (lists, draft labels, crops, outputs, traces are value-free but path-bearing) are
private: they are written 0600 and refused inside the git checkout. stdout carries only redacted aggregates
(counts, rates, failure classes), safe to relay.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _private_path(p: str | Path) -> Path:
    p = Path(p).resolve()
    if p == REPO or REPO in p.parents:
        raise SystemExit("refusing to write private data inside the repository checkout")
    return p


def _write_private(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _list_docs(input_root: Path, list_file: str | None) -> list[str]:
    if list_file:
        return [ln.strip() for ln in Path(list_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    return sorted(str(p.relative_to(input_root)) for p in input_root.rglob("*") if p.is_file()
                  and p.suffix.lower() == ".pdf")


def _secret(path: str) -> bytes:
    data = Path(path).read_bytes().strip()
    if len(data) < 16:
        raise SystemExit("secret file must hold >= 16 bytes")
    return data


def _emit(d: dict) -> None:
    print(json.dumps(d, indent=1, sort_keys=True, default=str))


# ------------------------------------------------------------------ commands


def cmd_sample(a) -> None:
    root = Path(a.input)
    docs = _list_docs(root, a.list)
    seed = Path(a.seed_file).read_bytes()
    rng = random.Random(seed + b"/pdf-sample")
    pick = sorted(rng.sample(docs, min(a.n, len(docs))))
    _write_private(_private_path(a.out), "\n".join(pick) + "\n")
    _emit({"population": len(docs), "sampled": len(pick)})


def cmd_survey(a) -> None:
    import pymupdf
    from .pdf_titleblock import (EMAIL_RE, match_label, page_is_textless, page_lines, scan_page_lines,
                                 _split_lines)
    root = Path(a.input)
    c: Counter = Counter()
    for rel in _list_docs(root, a.list):
        c["docs"] += 1
        try:
            doc = pymupdf.open(root / rel)
        except Exception:  # noqa: BLE001
            c["open_error"] += 1
            continue
        try:
            if doc.needs_pass:
                c["encrypted"] += 1
                continue
            if not doc.page_count:
                c["no_pages"] += 1
                continue
            p = doc[0]
            cls = page_is_textless(p)
            c[f"first_page:{cls['class']}"] += 1
            lines = page_lines(p)
            roles = Counter(match_label(ln.text) for ln in _split_lines(lines))
            roles.pop(None, None)
            if roles:
                c["docs_with_role_labels"] += 1
            for r, n in roles.items():
                c[f"role_label:{r}"] += n
            txt = "\n".join(ln.text for ln in lines)
            if EMAIL_RE.search(txt):
                c["docs_with_email_shape"] += 1
            f = scan_page_lines(lines)
            if f:
                c["docs_with_findings"] += 1
            for x in f:
                c[f"finding:{x.source}:{x.kind}"] += 1
            if doc.metadata and doc.metadata.get("author"):
                c["docs_with_info_author"] += 1
            if doc.embfile_count():
                c["docs_with_attachments"] += 1
            if any(True for pg in doc for _ in (pg.widgets() or [])):
                c["docs_with_form_fields"] += 1
        finally:
            doc.close()
    _emit({"survey": dict(sorted(c.items())), "ocr": _ocr_status()})


def _ocr_status() -> dict:
    import shutil
    return {"tesseract": bool(shutil.which("tesseract")), "tessdata_env": bool(os.environ.get("TESSDATA_PREFIX"))}


def cmd_propose(a) -> None:
    """Draft labels for a human to confirm (``status: proposed`` -> edit to ``confirmed`` / ``rejected``,
    add missed boxes, assign ``entity`` ids). Raw text: private output only."""
    import pymupdf
    from .pdf_titleblock import match_label, page_lines, scan_page_lines, _split_lines
    root = Path(a.input)
    out = _private_path(a.out)
    crops = _private_path(a.crops) if a.crops else None
    rows = []
    for rel in _list_docs(root, a.list):
        try:
            doc = pymupdf.open(root / rel)
        except Exception:  # noqa: BLE001
            continue
        try:
            if doc.needs_pass:
                continue
            for p in doc.pages(0, min(doc.page_count, a.max_pages)):
                lines = page_lines(p)
                found = scan_page_lines(lines)
                for f in found:
                    rows.append({"doc": rel, "page": p.number, "bbox": [round(x, 2) for x in f.rect],
                                 "text": f.value, "kind": f.kind, "entity": "", "status": "proposed",
                                 "hint": f"{f.source}:{f.role}"})
                for ln in _split_lines(lines):  # unmatched role labels: the human looks next to these
                    if match_label(ln.text):
                        rows.append({"doc": rel, "page": p.number, "bbox": [round(x, 2) for x in ln.bbox],
                                     "text": ln.text, "type": "role_label_hint", "status": "rejected"})
                if crops is not None:
                    crops.mkdir(parents=True, exist_ok=True)
                    for f in found:
                        p.add_rect_annot(f.rect).set_colors(stroke=(1, 0, 0))
                    pix = p.get_pixmap(dpi=a.dpi)
                    name = rel.replace("/", "__") + f".p{p.number}.png"
                    pix.save(crops / name)
                    os.chmod(crops / name, 0o600)
        finally:
            doc.close()
    _write_private(out, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    _emit({"proposed": sum(r["status"] == "proposed" for r in rows),
           "role_label_hints": sum(r.get("type") == "role_label_hint" for r in rows)})


def _open_catalog(a):
    if not getattr(a, "catalog", None):
        return None
    from .catalog import IdentityCatalog
    return IdentityCatalog(a.catalog, _secret(a.secret_file), read_only=True)


def cmd_build_map(a) -> None:
    import pymupdf
    from .pdf_entities import EntityMapBuilder
    from .pdf_titleblock import page_lines, scan_page_lines
    root = Path(a.input)
    cat = _open_catalog(a)
    b = EntityMapBuilder(_secret(a.secret_file), cat)
    errors = Counter()
    for rel in _list_docs(root, a.list):
        try:
            doc = pymupdf.open(root / rel)
        except Exception as exc:  # noqa: BLE001
            errors[type(exc).__name__] += 1
            continue
        try:
            if doc.needs_pass:
                errors["encrypted"] += 1
                continue
            found = []
            for p in doc:
                found += scan_page_lines(page_lines(p))
            b.add_document(rel, found)
        finally:
            doc.close()
    stats = b.freeze(_private_path(a.map))
    _emit({"build_map": stats, "errors": dict(errors)})


def cmd_run(a) -> None:
    from .pdf_entities import Resolver
    from .pdf_scoped import transform_pdf_scoped
    root, out_root = Path(a.input), _private_path(a.output)
    if not a.map and not a.derive:
        raise SystemExit("--map (frozen entity map) or --derive is required")
    res_ = Resolver(_secret(a.secret_file), _private_path(a.map) if a.map else None, derive=a.derive)
    c: Counter = Counter()
    trace_lines = []
    try:
        for rel in _list_docs(root, a.list):
            dst = out_root / rel
            c["docs"] += 1
            try:
                r = transform_pdf_scoped(root / rel, dst, res_, overwrite=True)
            except Exception as exc:  # noqa: BLE001
                c[f"exception:{type(exc).__name__}"] += 1
                r = None
            if r is None or not r.ok:
                c[f"not_transformed:{r.reason if r else 'exception'}"] += 1
                if not a.no_copy:  # evaluator scores copies as failures (fallback_copy)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes((root / rel).read_bytes())
            else:
                c["transformed"] += 1
            if r is not None:
                for k, v in r.findings.items():
                    c[f"finding:{k}"] += v
                for k, v in r.unresolved.items():
                    c[f"unresolved:{k}"] += v
                for k, v in r.residual.items():
                    c[f"residual:{k}"] += v
                c["edits_planned"] += r.edits_planned
                c["edits_verified"] += r.edits_verified
                c["fitted_used"] += r.fitted_used
                c["metadata_replacements"] += r.metadata_replacements
                c["annotation_or_form_replacements"] += r.annotation_replacements + r.widget_replacements
                c["textless_pages"] += r.textless_pages
                for t in r.trace:
                    trace_lines.append(json.dumps({"doc": rel, **t}))
    finally:
        res_.close()
    _write_private(out_root / "trace.jsonl", "\n".join(trace_lines) + "\n")
    _emit({"run": dict(sorted(c.items())), "entity_map_digest": res_.digest})


def cmd_evaluate(a) -> None:
    from .pdf_eval import evaluate, holdout_split, load_labels
    labels = load_labels(a.labels)
    split = None
    if a.holdout_frac:
        if not a.seed_file:
            raise SystemExit("--holdout-frac needs --seed-file")
        split = holdout_split(labels, Path(a.input), Path(a.seed_file).read_bytes(), a.holdout_frac)
    rep = evaluate(labels, a.input, a.output, trace_path=a.trace, split=split)
    _emit({"evaluate": rep})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pdf_lab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--input", required=True); s.add_argument("--list"); s.add_argument("--n", type=int, default=120)
    s.add_argument("--seed-file", required=True); s.add_argument("--out", required=True)
    s = sub.add_parser("survey")
    s.add_argument("--input", required=True); s.add_argument("--list")
    s = sub.add_parser("propose")
    s.add_argument("--input", required=True); s.add_argument("--list"); s.add_argument("--out", required=True)
    s.add_argument("--crops"); s.add_argument("--dpi", type=int, default=110); s.add_argument("--max-pages", type=int, default=3)
    s = sub.add_parser("build-map")
    s.add_argument("--input", required=True); s.add_argument("--list"); s.add_argument("--secret-file", required=True)
    s.add_argument("--map", required=True); s.add_argument("--catalog")
    s = sub.add_parser("run")
    s.add_argument("--input", required=True); s.add_argument("--list"); s.add_argument("--output", required=True)
    s.add_argument("--secret-file", required=True); s.add_argument("--map"); s.add_argument("--derive", action="store_true")
    s.add_argument("--no-copy", action="store_true", help="do not copy untransformed PDFs to the output")
    s = sub.add_parser("evaluate")
    s.add_argument("--input", required=True); s.add_argument("--output", required=True)
    s.add_argument("--labels", required=True); s.add_argument("--trace")
    s.add_argument("--holdout-frac", type=float, default=0.0); s.add_argument("--seed-file")
    a = ap.parse_args(argv)
    {"sample": cmd_sample, "survey": cmd_survey, "propose": cmd_propose, "build-map": cmd_build_map,
     "run": cmd_run, "evaluate": cmd_evaluate}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
