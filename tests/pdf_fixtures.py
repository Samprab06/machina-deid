"""Synthetic engineering-drawing PDFs with ground-truth labels (all values invented)."""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf

PEOPLE = {"P1": "Jane Q. Doe", "P2": "Bob Builder", "P3": "Carla Tester"}
ORG = {"O1": "Acme Widgets Ltd"}
EMAIL = {"E1": "jane.doe@acme-widgets.example"}


def _title_block(p, *, drawn: str, checked: str, company: str, labels: list, doc: str, ents: dict,
                 x0: float = 500, y0: float = 480):
    p.draw_rect(pymupdf.Rect(x0, y0, x0 + 330, y0 + 105), color=(0, 0, 0), width=0.8)
    p.draw_line((x0, y0 + 40), (x0 + 330, y0 + 40))
    p.draw_line((x0 + 50, y0), (x0 + 50, y0 + 105))
    for (lx, ly, label, value, kind, ent) in ((x0 + 5, y0 + 20, "DRAWN", drawn, "person", ents.get(drawn)),
                                              (x0 + 5, y0 + 60, "CHECKED", checked, "person", ents.get(checked)),
                                              (x0 + 5, y0 + 95, "COMPANY", company, "org", ents.get(company))):
        p.insert_text((lx, ly), label, fontsize=7)
        p.insert_text((x0 + 60, ly), value, fontsize=8)
        w = pymupdf.get_text_length(value, "helv", 8)
        labels.append({"doc": doc, "page": p.number, "bbox": [x0 + 60, ly - 8 * 1.075 + 0.0, x0 + 60 + w, ly + 8 * 0.3],
                       "text": value, "kind": kind, "entity": ent})
    p.insert_text((x0 + 200, y0 + 20), "SCALE 1:2", fontsize=7)
    labels.append({"doc": doc, "page": p.number, "type": "keep", "text": "SCALE 1:2",
                   "bbox": [x0 + 199, y0 + 12, x0 + 240, y0 + 23]})


def _geometry(p):
    p.draw_circle((200, 200), 60, color=(0, 0, 0))
    p.draw_line((100, 300), (300, 300))
    p.insert_text((180, 320), "Ø120 ±0.05", fontsize=8)


def make_corpus(root: Path) -> list[dict]:
    """Writes 4 PDFs under ``root`` (2 families) and returns the confirmed labels."""
    ents = {v: k for k, v in {**PEOPLE, **ORG, **EMAIL}.items()}
    ents["J. Doe"] = "P1"
    labels: list[dict] = []

    # family A, doc 1: full title block, email, metadata author, rotated inline label, duplicate on page
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    _geometry(p)
    _title_block(p, drawn="Jane Q. Doe", checked="Bob Builder", company="Acme Widgets Ltd", labels=labels,
                 doc="famA/drw-001.pdf", ents=ents)
    p.insert_text((40, 300), "DRAWN BY: Bob Builder", fontsize=9, rotate=90)
    w = pymupdf.get_text_length("Bob Builder", "helv", 9)
    pre = pymupdf.get_text_length("DRAWN BY: ", "helv", 9)
    labels.append({"doc": "famA/drw-001.pdf", "page": 0, "bbox": [40 - 9 * 1.075, 300 - pre - w, 40 + 9 * 0.3, 300 - pre],
                   "text": "Bob Builder", "kind": "person", "entity": "P2"})
    p.insert_text((520, 470), EMAIL["E1"], fontsize=6)
    w = pymupdf.get_text_length(EMAIL["E1"], "helv", 6)
    labels.append({"doc": "famA/drw-001.pdf", "page": 0, "bbox": [520, 470 - 6.45, 520 + w, 470 + 1.8],
                   "text": EMAIL["E1"], "kind": "email", "entity": "E1"})
    d.set_metadata({"author": "Jane Q. Doe", "title": "Bracket"})
    _save(d, root / "famA/drw-001.pdf")

    # family A, doc 2: unlabelled cross-document mention in notes + initial alias in title block
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    _geometry(p)
    _title_block(p, drawn="J. Doe", checked="Carla Tester", company="Acme Widgets Ltd", labels=labels,
                 doc="famA/drw-002.pdf", ents=ents)
    p.insert_text((60, 60), "NOTES: questions to Jane Q. Doe", fontsize=8)
    pre = pymupdf.get_text_length("NOTES: questions to ", "helv", 8)
    w = pymupdf.get_text_length("Jane Q. Doe", "helv", 8)
    labels.append({"doc": "famA/drw-002.pdf", "page": 0, "bbox": [60 + pre, 60 - 8.6, 60 + pre + w, 62.4],
                   "text": "Jane Q. Doe", "kind": "person", "entity": "P1"})
    annot = p.add_text_annot((700, 50), "reviewed by Bob Builder")
    annot.set_info(title="Bob Builder")
    annot.update()
    _save(d, root / "famA/drw-002.pdf")

    # family B, doc 3: rotated page, same people (cross-document), narrow cell -> fitted form
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.set_rotation(90)
    _geometry(p)
    p.insert_text((50, 50), "APPROVED", fontsize=8)
    p.insert_text((100, 50), "Carla Tester", fontsize=5)
    w = pymupdf.get_text_length("Carla Tester", "helv", 5)
    labels.append({"doc": "famB/sheet-3.pdf", "page": 0, "bbox": [100, 50 - 5.4, 100 + w, 51.5],
                   "text": "Carla Tester", "kind": "person", "entity": "P3"})
    _save(d, root / "famB/sheet-3.pdf")

    # family B, doc 4: byte-identical duplicate of doc 1 (holdout leakage check)
    (root / "famB").mkdir(parents=True, exist_ok=True)
    (root / "famB/copy-of-001.pdf").write_bytes((root / "famA/drw-001.pdf").read_bytes())
    for lb in [x for x in labels if x["doc"] == "famA/drw-001.pdf"]:
        labels.append({**lb, "doc": "famB/copy-of-001.pdf"})
    return labels


def _save(d, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    d.close()


def write_labels(labels: list[dict], path: Path) -> None:
    path.write_text("".join(json.dumps(x) + "\n" for x in labels), encoding="utf-8")
