from pathlib import Path
from types import SimpleNamespace

import pymupdf

from machina_deid.path_audit import audit, candidate_path


def entry(value):
    return SimpleNamespace(status="ok", kind="person", pseudonym="Person 123") if value == "Jane Doe" else None


def test_exact_paths_preserve_extensions_and_leave_lookalikes():
    assert candidate_path("Jane Doe/Jane Doe.pdf", entry) == "Person 123/Person 123.pdf"
    assert candidate_path("Jane Doe Shaft.pdf", entry) == "Jane Doe Shaft.pdf"


def test_audit_reports_pdf_backreference_collision_and_opaque_cad(tmp_path: Path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_link({"kind": pymupdf.LINK_GOTOR, "file": "Jane Doe.pdf",
                          "page": 0, "to": pymupdf.Point(0, 0), "from": pymupdf.Rect(10, 10, 50, 30)})
        doc.save(tmp_path / "source.pdf")
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(tmp_path / "Jane Doe.pdf")
        doc.save(tmp_path / "Person 123.pdf")
    report = audit(tmp_path, ["source.pdf", "Jane Doe.pdf", "Person 123.pdf", "assembly.sldasm"], entry)
    assert report["exact_identity_path_candidates"] == 1
    assert report["path_candidates_with_collision"] == 1
    assert report["candidate_targets_with_incoming_pdf_file_refs"] == 1
    assert report["files_with_unaudited_reference_formats"] == 1
    assert report["candidates_requiring_reference_review"] == 1
