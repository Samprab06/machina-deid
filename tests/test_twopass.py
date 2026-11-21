"""Phase 5C two-pass workflow: discovery -> catalog approvals -> in-memory map -> transform."""
import io
import json
import sqlite3
import zipfile

import ezdxf
import pymupdf
import pytest
from step_helpers import step_bytes

from machina_deid import cli
from machina_deid import twopass as tp
from machina_deid.catalog import CatalogError, IdentityCatalog
from machina_deid.discovery import Candidate

SECRET = b"unit-test-secret-0123456789"
ORG = "Acme Widgets Inc"
PERSON = "Jane Quincy Doe"
ALIAS = "ACME Widgets Incorporated"
SIMILAR = "Acme Widgets Inc."
RAW = [ORG, PERSON, ALIAS, SIMILAR, "Acme Widget Inc"]


def pdf_bytes(author=ORG, body=None):
    doc = pymupdf.open()
    doc.new_page(width=300, height=400).insert_text((50, 100), body or f"Prepared for {author}", fontsize=12)
    doc.set_metadata({"author": author, "title": "Bracket drawing"})
    out = doc.tobytes()
    doc.close()
    return out


def dxf_file(path, company=ORG, extra_text=None):
    doc = ezdxf.new("R2010")
    blk = doc.blocks.new("TB")
    blk.add_attdef("COMPANY", (0, 0), height=2.5)
    msp = doc.modelspace()
    msp.add_line((0, 0), (100.25, 50.5))
    ins = msp.add_blockref("TB", (0, 0))
    ins.add_auto_attribs({"COMPANY": company})
    if extra_text:
        msp.add_text(extra_text, height=2, dxfattribs={"insert": (5, 5)})
    doc.saveas(path)
    return path


def zip_file(path, members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def corpus(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(pdf_bytes())
    (src / "b.step").write_bytes(step_bytes(name="bracket", author=f"('{PERSON}')", org=f"('{ORG}')"))
    dxf_file(src / "c.dxf")
    inner = zip_file(tmp_path / "inner.zip", [("docs/d.pdf", pdf_bytes(author=PERSON, body=f"Owner {ORG}"))])
    zip_file(src / "e.zip", [("readme.txt", b"hello"), ("nested/inner.zip", inner.read_bytes())])
    return src


def items(src):
    return [(p, p.name) for p in sorted(src.iterdir())]


@pytest.fixture
def cat(tmp_path):
    c = IdentityCatalog(tmp_path / "cat" / "cat.sqlite", SECRET)
    yield c
    c.close()


def assert_no_raw(root):
    for p in root.rglob("*"):
        if p.is_file():
            low = p.read_bytes().lower()
            for raw in RAW:
                assert raw.lower().encode() not in low, p


def pdf_text(path_or_bytes):
    doc = pymupdf.open(stream=path_or_bytes) if isinstance(path_or_bytes, bytes) else pymupdf.open(path_or_bytes)
    try:
        return "".join(p.get_text() for p in doc) + json.dumps(doc.metadata)
    finally:
        doc.close()


def dxf_text(path):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace():
        if e.dxftype() == "INSERT":
            out += [a.dxf.text for a in e.attribs]
        elif e.dxftype() == "TEXT":
            out.append(e.dxf.text)
    return "\n".join(out)


# ---------------------------------------------------------------- catalog approval state


def test_approval_lookup_exact_normalized_only(cat):
    assert cat.approve("org", ORG) == "approved"
    kind, p = cat.approved_pseudonym("  acme   WIDGETS inc ")
    assert kind == "org" and p == cat.lookup("org", ORG) and p.startswith("Organization-")
    assert cat.approved_pseudonym(SIMILAR) is None
    assert cat.approved_pseudonym("Acme Widget Inc") is None
    assert cat.approved_pseudonym(ORG, kind="person") is None
    assert cat.approved_pseudonym("") is None


def test_approval_idempotent_and_restart(tmp_path):
    db = tmp_path / "cat.sqlite"
    with IdentityCatalog(db, SECRET) as c:
        assert c.approve("org", ORG) == "approved"
        assert c.approve("org", ORG) == "approved"
        p = c.approved_pseudonym(ORG)
        n = c.approvals_report()["decisions"]
        assert n == 1
    with IdentityCatalog(db, SECRET) as c:
        assert c.approved_pseudonym(ORG) == p
        assert c.approve("org", ORG) == "approved"
        assert c.approvals_report()["decisions"] == 1
        assert c.reject("org", ORG) == "rejected"
        assert c.approved_pseudonym(ORG) is None
    with IdentityCatalog(db, SECRET) as c:
        assert c.approval_status("org", ORG) == "rejected"
        assert c.approve("org", ORG) == "approved" and c.approved_pseudonym(ORG) == p
        assert c.approvals_report()["decisions"] == 3
    assert_no_raw(tmp_path)


@pytest.mark.parametrize("args,match", [
    (("nope", ORG, "approved"), "unknown kind"),
    (("org", "   ", "approved"), "non-empty"),
    (("org", ORG, "maybe"), "status"),
    (("org", ORG, "approved", "Acme Widgets Inc approved"), "must not contain"),
    (("org", ORG, "approved", "bad\nreason"), "printable"),
])
def test_invalid_approvals_rejected_without_side_effects(cat, args, match):
    kind, value, status, *reason = args
    with pytest.raises(CatalogError, match=match) as ei:
        cat.decide(kind, value, status, reason=reason[0] if reason else "ok")
    assert ORG.lower() not in str(ei.value).lower()
    rep = cat.report()
    assert rep["approvals"]["decisions"] == 0 and rep["summary"]["identities"] == 0
    with pytest.raises(CatalogError, match="source"):
        cat.decide("org", ORG, "approved", reason="x", source="robot")


def test_kind_conflict_recorded_and_existing_kept(cat):
    p = cat.pseudonym("org", ORG)
    cat.approve("org", ORG)
    assert cat.approve("person", ORG) == "conflict"
    assert cat.approved_pseudonym(ORG) == ("org", p)
    rep = cat.report()
    assert rep["approvals"]["counts"]["conflict"] == 1
    assert any(c["type"] == "kind_conflict" for c in rep["conflicts"])


def test_auto_never_overrides_manual(cat):
    cat.reject("text", ORG)
    assert cat.decide("text", ORG, "approved", reason="auto", source="auto") == "rejected"
    assert cat.decide("org", ORG, "approved", reason="auto", source="auto") == "review"  # blocked by rejection
    assert cat.approved_pseudonym(ORG) is None
    # manual can still override
    assert cat.approve("text", ORG) == "approved"


def test_alias_approval_and_conflict(cat):
    p = cat.approve_alias("org", ORG, ALIAS)
    assert cat.approved_pseudonym(ALIAS) == ("org", p) == cat.approved_pseudonym(ORG)
    with pytest.raises(CatalogError, match="conflicts"):
        cat.approve_alias("org", PERSON, ALIAS)
    assert cat.approved_pseudonym(PERSON) is None  # nothing approved on a failed alias
    assert cat.approved_pseudonym(ALIAS) == ("org", p)


# ---------------------------------------------------------------- auto policy


@pytest.mark.parametrize("cand,expected", [
    (Candidate("person_or_org", ORG, "pdf:metadata.author", "high", "x"), "approved"),
    (Candidate("person_or_org", ORG, "step:FILE_NAME.organization[0]", "high", "x"), "approved"),
    (Candidate("person_or_org", ORG, "step:FILE_NAME.author[2]", "high", "x"), "approved"),
    (Candidate("person_or_org", ORG, "dxf:header.$LASTSAVEDBY", "high", "x"), "review"),
    (Candidate("person_or_org", ORG, "pdf:metadata.author", "medium", "x"), "review"),
    (Candidate("person_or_org", ORG, "pdf:page[0].title_block[0]", "high", "x"), "review"),
] + [(Candidate(k, ORG, "pdf:metadata.author", "high", "x"), "review")
     for k in ("file_name", "project", "part", "description", "text_label_value", "path_component",
               "software_or_authorization", "unknown", "timestamp")])
def test_auto_policy(cand, expected):
    assert tp.auto_decision(cand)[0] == expected


# ---------------------------------------------------------------- discovery dispatch


def test_discover_file_dispatch_and_zip_members(corpus):
    found = {d.path: d for f in sorted(corpus.iterdir()) for d in tp.discover_file(f, f.name)}
    assert found["a.pdf"].format == "pdf" and found["c.dxf"].format == "dxf" and found["b.step"].format == "step"
    assert found["e.zip!readme.txt"].status == "unsupported"
    member = found["e.zip!nested/inner.zip!docs/d.pdf"]
    assert member.status == "ok" and {c.value for c in member.candidates} >= {PERSON, ORG}
    assert all(PERSON not in repr(d) and ORG not in repr(d) for d in found.values())
    assert "c.dxf" in found and ORG in {c.value for c in found["c.dxf"].candidates}


def test_discover_unsupported_and_corrupt(tmp_path):
    (tmp_path / "x.SLDPRT").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.7\nnot really")
    (tmp_path / "bad.zip").write_bytes(b"PK\x03\x04garbage")
    assert tp.discover_file(tmp_path / "x.SLDPRT")[0].status == "unsupported"
    assert tp.discover_file(tmp_path / "bad.pdf")[0].status == "failed"
    assert tp.discover_file(tmp_path / "bad.zip")[0].status == "failed"


# ---------------------------------------------------------------- end to end


def run_pass2(cat, src, out):
    return {f.name: tp.transform_file(cat, f, out / f.name, f.name) for f in sorted(src.iterdir())}


def test_cross_format_same_identity_same_pseudonym(tmp_path, corpus, cat):
    before = {p.name: p.read_bytes() for p in corpus.iterdir()}
    res = tp.discover_batch(cat, items(corpus), limit=10)
    assert res.files == 4 and res.failed == 0 and res.unsupported == 1
    assert res.decisions["approved"] >= 1
    cat.approve("org", ORG, reason="explicit test approval")
    _, porg = cat.approved_pseudonym(ORG)
    _, pper = cat.approved_pseudonym(PERSON)
    assert porg.startswith("Organization-") and pper.startswith("Person-") and porg != pper

    out = tmp_path / "out"
    r = run_pass2(cat, corpus, out)
    assert all(o.status == "transformed" and o.source_unchanged and o.residual_approved == 0 for o in r.values()), \
        {k: v.to_dict() for k, v in r.items()}
    assert {p.name: p.read_bytes() for p in corpus.iterdir()} == before

    t = pdf_text(out / "a.pdf")
    assert ORG not in t and porg in t
    stp = (out / "b.step").read_bytes()
    head = stp[:stp.index(b"DATA;")].decode("latin-1")  # DATA is analysis-only: never rewritten
    assert ORG not in head and PERSON not in head and porg in head and pper in head
    assert (out / "b.step").read_bytes().endswith(before["b.step"][before["b.step"].index(b"DATA;"):])
    d = dxf_text(out / "c.dxf")
    assert ORG not in d and porg in d  # DXF attribute value: review-kind, matched by exact approval
    with zipfile.ZipFile(out / "e.zip") as z, zipfile.ZipFile(io.BytesIO(z.read("nested/inner.zip"))) as zi:
        t = pdf_text(zi.read("docs/d.pdf"))
        assert z.read("readme.txt") == b"hello"
    assert ORG not in t and PERSON not in t and porg in t  # PDF Info is cleared by the existing transformer
    assert_no_raw(tmp_path / "cat")  # catalog + pseudonym store: digests only


def test_alias_cross_format(tmp_path, corpus, cat):
    tp.discover_batch(cat, items(corpus), limit=10)
    p = cat.approve_alias("text", ORG, ALIAS)
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / "alias.pdf").write_bytes(pdf_bytes(author=ALIAS))
    o = tp.transform_file(cat, src2 / "alias.pdf", tmp_path / "o" / "alias.pdf")
    assert o.status == "transformed"
    t = pdf_text(tmp_path / "o" / "alias.pdf")
    assert ALIAS not in t and p in t and p == cat.approved_pseudonym(ORG)[1]


def test_similar_unlinked_remain_distinct(tmp_path, cat):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(pdf_bytes(author=ORG))
    (src / "b.pdf").write_bytes(pdf_bytes(author=SIMILAR))
    dxf_file(src / "c.dxf", company="Acme Widget Inc")  # DXF only -> review, never auto-approved
    tp.discover_batch(cat, items(src), limit=10)
    cat.approve("org", ORG, reason="explicit test approval")
    cat.approve("org", SIMILAR, reason="explicit test approval")
    a, b = cat.approved_pseudonym(ORG), cat.approved_pseudonym(SIMILAR)
    assert a and b and a[1] != b[1]
    assert cat.approved_pseudonym("Acme Widget Inc") is None
    assert cat.approval_status("text", "Acme Widget Inc") == "review"
    o = tp.transform_file(cat, src / "c.dxf", tmp_path / "o" / "c.dxf")
    assert o.status == "no_approved_identities" and not (tmp_path / "o" / "c.dxf").exists()


def test_restart_duplicates_and_partial_batch(tmp_path, corpus):
    (corpus / "z_bad.pdf").write_bytes(b"%PDF-1.7\ncorrupt")
    db = tmp_path / "cat" / "cat.sqlite"
    with IdentityCatalog(db, SECRET) as c:
        first = tp.discover_batch(c, items(corpus), limit=2)  # "interrupted" after 2 files
        assert first.files == 2
        rep1 = c.report()
    with IdentityCatalog(db, SECRET) as c:
        full = tp.discover_batch(c, items(corpus), limit=10)
        assert full.files == 5 and full.failed == 1 and full.failures
        again = tp.discover_batch(c, items(corpus), limit=10)
        assert again.new_observations == 0
        rep = c.report()
        assert rep["summary"]["observations"] == rep1["summary"]["observations"] + full.new_observations
        n_dec = rep["approvals"]["decisions"]
        tp.discover_batch(c, items(corpus), limit=10)
        assert c.report()["approvals"]["decisions"] == n_dec
        c.approve("org", ORG, reason="explicit test approval")
        assert c.approved_pseudonym(ORG) is not None
    rows = sqlite3.connect(db).execute("SELECT source, status, reason FROM approvals").fetchall()
    assert rows and all(r[2] for r in rows) and {r[0] for r in rows} == {"auto", "manual"}
    assert_no_raw(tmp_path / "cat")


def test_budget_bounds(corpus, cat):
    with pytest.raises(ValueError):
        tp.discover_batch(cat, items(corpus), limit=0)
    r = tp.discover_batch(cat, items(corpus), limit=10, max_bytes=1)
    assert r.files == 0 and r.skipped_over_budget == 4


def test_map_builder_only_approved(cat):
    cat.approve("person", PERSON)
    cat.mark_review("text", ORG)
    cands = [Candidate("person_or_org", PERSON, "pdf:metadata.author", "high", "x"),
             Candidate("text_label_value", ORG, "dxf:attrib[0]", "high", "x"),
             Candidate("text_label_value", PERSON, "dxf:attrib[1]", "high", "x")]
    m, st = tp.build_identity_map(cat, cands)
    assert list(m.strings) == [PERSON] and m.strings[PERSON].startswith("Person-")
    assert not m.layers and not m.blocks and st.approved_values == 1 and st.unapproved_values == 1
    assert PERSON not in repr(m)


def test_cli_roundtrip(tmp_path, corpus, monkeypatch, capsys):
    secret = tmp_path / "secret"
    secret.write_bytes(SECRET)
    base = ["catalog", "--catalog", str(tmp_path / "cat" / "c.sqlite"), "--secret-file", str(secret)]
    assert cli.main([base[0], "discover", *[str(p) for p in sorted(corpus.iterdir())], *base[1:],
                     "--source-root", str(corpus)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("Acme Widget Inc\n"))
    assert cli.main([base[0], "approve", *base[1:], "--kind", "org"]) == 0
    assert cli.main([base[0], "report", *base[1:]]) == 0
    assert cli.main([base[0], "transform", str(corpus / "a.pdf"), str(tmp_path / "o.pdf"), *base[1:]]) == 0
    out = capsys.readouterr().out
    for raw in RAW:
        assert raw not in out
    assert_no_raw(tmp_path / "cat")


def test_step_org_also_in_preprocessor_transformed_but_residual_reported(tmp_path, cat):
    src = tmp_path / "p.step"
    src.write_bytes(step_bytes(name="bracket", author="('')", org=f"('{ORG}')", preproc=f"{ORG} exporter"))
    before = src.read_bytes()
    tp.discover_batch(cat, [(src, src.name)], limit=1)
    assert cat.approved_pseudonym(ORG, kind="org") is not None  # approved from FILE_NAME.organization
    dst = tmp_path / "out" / "p.step"
    o = tp.transform_file(cat, src, dst, src.name)
    assert o.status == "transformed" and o.output_written and o.source_unchanged
    assert o.fields_replaced >= 1 and o.residual_approved >= 1
    assert o.fully_clean is False
    head = dst.read_bytes().split(b"DATA;")[0].decode("latin-1")
    org_field = head.split("/* organization */")[1].split("/* preprocessor_version */")[0]
    pre_field = head.split("/* preprocessor_version */")[1].split("/* originating_system */")[0]
    assert ORG not in org_field and cat.approved_pseudonym(ORG)[1] in org_field
    assert ORG in pre_field  # unsupported field left untouched, surfaced as residual
    assert src.read_bytes() == before


@pytest.mark.parametrize("nested", [False, True])
def test_pdf_metadata_only_counts_validated_fields(tmp_path, cat, nested):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Drawing dimensions")
    doc.set_metadata({"author": PERSON, "subject": f"{PERSON}; {PERSON}", "creator": "CAD"})
    doc.set_xml_metadata(f"<metadata><author>{PERSON}</author></metadata>")
    doc.embfile_add("notes.txt", b"notes")
    pdf = doc.tobytes()
    doc.close()

    src = tmp_path / ("in.zip" if nested else "in.pdf")
    dst = tmp_path / ("out.zip" if nested else "out.pdf")
    if nested:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("sample.pdf", pdf)
        with zipfile.ZipFile(src, "w") as archive:
            archive.writestr("inner.zip", inner.getvalue())
    else:
        src.write_bytes(pdf)
    cat.approve("person", PERSON)
    outcome = tp.transform_file(cat, src, dst)
    counts = outcome.replacement_counts()
    assert outcome.fully_clean, outcome.to_dict()
    assert counts["pdf_visible_text_replacements"] == 0
    assert counts["pdf_metadata_identities_changed"] == 3
    assert counts["pdf_attachment_changes"] == counts["pdf_form_changes"] == counts["pdf_annotation_changes"] == 0
    assert counts["archive_replacements"] == counts["nested_archive_replacements"] == (3 if nested else 0)
    assert counts["total_verified_identity_fields_changed"] == outcome.fields_replaced == 3
    assert PERSON not in json.dumps(outcome.to_dict())


def test_failed_pdf_validation_reports_no_verified_changes(tmp_path, cat):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), "Drawing dimensions")
        doc.set_metadata({"author": PERSON})
        doc.embfile_add("notes.txt", PERSON.encode())
        doc.save(src)
    cat.approve("person", PERSON)
    outcome = tp.transform_file(cat, src, dst)
    assert outcome.status == "failed" and not dst.exists()
    assert outcome.independent_residual["embedded_files"] > 0
    assert not any(outcome.replacement_counts().values())
