"""Attempt 2c coverage fixes: links, Info/XMP, visible residue, layouts, OCR, QA buckets (synthetic only)."""
import pymupdf
import pytest

from machina_deid.pdf_entities import Resolver
from machina_deid.pdf_links import verify_links
from machina_deid.pdf_residual import inspect_pdf
from machina_deid.pdf_scoped import transform_pdf_scoped

SECRET = b"synthetic-test-secret-0123456789"
XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">'
       '<dc:creator><rdf:Seq><rdf:li>{who}</rdf:li></rdf:Seq></dc:creator><pdf:Author>{who}</pdf:Author>'
       '</rdf:Description></rdf:RDF></x:xmpmeta>')


def _resolver():
    return Resolver(SECRET, derive=True)


def _title(p, label="DRAWN", value="Jane Doe", x=500, y=500):
    p.insert_text((x, y), label, fontsize=7)
    p.insert_text((x + 60, y), value, fontsize=8)


def _links(path):
    d = pymupdf.open(path)
    try:
        return [(ln.get("uri"), ln.get("page"), tuple(round(c, 1) for c in ln["from"])) for ln in d[0].get_links()]
    finally:
        d.close()


# ------------------------------------------------------------------ links


def test_overlapping_link_restored_and_identity_uri_rewritten(tmp_path):
    d = pymupdf.open()
    d.new_page(width=842, height=595)
    d.new_page()
    p = d[0]
    _title(p)
    p.insert_text((500, 520), "mail jane.doe@acme.example", fontsize=8)
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(520, 512, 640, 522),
                   "uri": "mailto:jane.doe@acme.example?subject=Bracket"})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(555, 492, 600, 502), "uri": "https://example.org/x"})
    p.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(560, 493, 590, 501), "page": 1,
                   "to": pymupdf.Point(10, 20)})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 50, 20), "uri": "https://keep.example/a%20b"})
    d.save(tmp_path / "a.pdf")
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", _resolver())
    assert r.ok, (r.reason, dict(r.checks))
    assert r.links_restored >= 2 and r.link_replacements == 1
    before, after = _links(tmp_path / "a.pdf"), _links(tmp_path / "b.pdf")
    assert sorted(x[2] for x in before) == sorted(x[2] for x in after)  # geometry preserved
    keep = {x for x in before if "jane" not in str(x[0])}
    assert keep <= set(after)  # untouched destinations identical (URI, GoTo page)
    mail = [x[0] for x in after if str(x[0]).startswith("mailto:")]
    assert mail and "jane" not in mail[0].lower() and mail[0].endswith("?subject=Bracket")
    assert inspect_pdf(tmp_path / "b.pdf", r.resolved_values).clean


def test_link_verifier_is_independent_of_writer(tmp_path):
    d = pymupdf.open()
    p = d.new_page()
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 50, 20), "uri": "https://keep.example/"})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 30, 50, 40), "uri": "mailto:jd@acme.example"})
    d.save(tmp_path / "a.pdf")
    src = pymupdf.open(tmp_path / "a.pdf")
    assert not verify_links(src, src, [])  # identical, nothing resolved
    assert verify_links(src, src, ["jd@acme.example"])["link_action_changed"] == 1
    for mutate, code in ((lambda pg: pg.delete_link(pg.get_links()[0]), "link_count_changed"),
                         (lambda pg: pg.update_link({**pg.get_links()[0], "uri": "https://other.example/"}),
                          "link_action_changed")):
        o = pymupdf.open(tmp_path / "a.pdf")
        mutate(o[0])
        o.save(tmp_path / "o.pdf")
        o.close()
        o = pymupdf.open(tmp_path / "o.pdf")
        assert verify_links(src, o, [])[code] >= 1
        o.close()


# ------------------------------------------------------------------ Info / XMP


def test_custom_info_keys_and_every_xmp_packet(tmp_path):
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    _title(p)
    d.set_metadata({"author": "Jane Doe", "title": "Bracket by Jane Doe", "keywords": "x_Jane Doe_y"})
    info = int(d.xref_get_key(-1, "Info")[1].split()[0])
    d.xref_set_key(info, "Company", pymupdf.get_pdf_str("Jane Doe Engineering"))
    d.set_xml_metadata(XMP.format(who="Jane Doe"))
    # a second (page-level) metadata stream
    x = d.get_new_xref()
    d.update_object(x, "<< /Type /Metadata /Subtype /XML >>")
    d.update_stream(x, XMP.format(who="JANE  DOE").encode())
    d.xref_set_key(p.xref, "Metadata", f"{x} 0 R")
    d.save(tmp_path / "a.pdf")
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", _resolver())
    assert r.ok, (r.reason, dict(r.residual))
    o = pymupdf.open(tmp_path / "b.pdf")
    info = int(o.xref_get_key(-1, "Info")[1].split()[0])
    assert o.xref_get_key(info, "Company")[1].startswith("Person-")
    assert "jane" not in (o.metadata["keywords"] + o.get_xml_metadata()).lower()
    mx = int(o.xref_get_key(o[0].xref, "Metadata")[1].split()[0])
    assert b"JANE" not in o.xref_stream(mx) and b"Person-" in o.xref_stream(mx)
    assert o.metadata["creationDate"] == pymupdf.open(tmp_path / "a.pdf").metadata["creationDate"]


def test_metadata_only_author_resolved_before_xmp(tmp_path):
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    _title(p, value="Bob Builder")
    d.set_metadata({"author": "Carla Tester"})
    d.set_xml_metadata(XMP.format(who="Carla Tester"))
    d.save(tmp_path / "a.pdf")
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", _resolver())
    assert r.ok, (r.reason, dict(r.residual))
    o = pymupdf.open(tmp_path / "b.pdf")
    assert "carla" not in (o.metadata["author"] + o.get_xml_metadata()).lower()
    assert o.metadata["author"] in o.get_xml_metadata()  # same pseudonym everywhere


def test_non_person_author_left_alone(tmp_path):
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    _title(p, value="Bob Builder")
    d.set_metadata({"author": "ACME-CAD 2021 SP1"})
    d.save(tmp_path / "a.pdf")
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", _resolver())
    assert r.ok and pymupdf.open(tmp_path / "b.pdf").metadata["author"] == "ACME-CAD 2021 SP1"


def test_all_failing_checks_counted_and_edits_still_classified(tmp_path, monkeypatch):
    import machina_deid.pdf_scoped as ps
    d = pymupdf.open()
    _title(d.new_page(width=842, height=595))
    d.save(tmp_path / "a.pdf")
    monkeypatch.setattr(ps, "verify_links", lambda *a: __import__("collections").Counter(link_count_changed=1))
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", _resolver())
    assert not r.ok and r.reason == "links_changed" and not (tmp_path / "b.pdf").exists()
    assert r.edits_verified == r.edits_planned == 1  # edits are still verified, not silently "unverified"
    assert r.checks["link_count_changed"] == 1
