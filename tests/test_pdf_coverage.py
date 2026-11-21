"""Attempt 2c coverage fixes: links, Info/XMP, visible residue, layouts, OCR, QA buckets (synthetic only)."""
from types import SimpleNamespace

import pymupdf
import pytest

from machina_deid.pdf_entities import Resolver
from machina_deid.pdf_links import raw_link_uri, rewrite_link_actions, verify_links
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


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_link_verifier_checks_exact_uri_action(indirect, scheme):
    pairs = {"http://www.acme.example/catalog": "replacement.example", "Jane Doe": "Person-123"}
    original = f"{scheme}://www.acme.example:8080/catalog/Jane%20Doe?part=a%2fb&view=full#top"
    expected = f"{scheme}://replacement.example:8080/catalog/Person-123?part=a%2fb&view=full#top"
    unrelated = f"{scheme}://www.acme.example:8080/catalogue/Janet%20Doe?part=a%2fb&view=full#top"

    def make_pdf(uri, control=unrelated):
        with pymupdf.open() as doc:
            page = doc.new_page()
            for y, target in ((10, uri), (30, control)):
                page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, y, 50, y + 10),
                                  "uri": target})
            if indirect:
                for xref, _, _ in doc.reload_page(page).annot_xrefs():
                    action = doc.get_new_xref()
                    doc.update_object(action, doc.xref_get_key(xref, "A")[1])
                    doc.xref_set_key(xref, "A", f"{action} 0 R")
            return doc.tobytes()

    source = make_pdf(original)
    with pymupdf.open(stream=source) as src:
        with pymupdf.open(stream=make_pdf(expected)) as out:
            assert not verify_links(src, out, pairs)
        with pymupdf.open(stream=make_pdf(expected, unrelated.replace("view=full", "view=other"))) as out:
            assert verify_links(src, out, pairs)["link_action_changed"] == 1
        for wrong_uri in (
            original,
            expected.replace("/catalog/", "/other/"),
            expected.replace("view=full", "view=other"),
            expected.replace("replacement.example", "other.example"),
            expected.replace("Person-123", "Person-456"),
            expected.replace(":8080", ":8081"),
            expected.replace("#top", "#bottom"),
            expected.replace(f"{scheme}:", "mailto:", 1),
        ):
            with pymupdf.open(stream=make_pdf(wrong_uri)) as out:
                assert verify_links(src, out, pairs)["link_action_changed"] == 1

        with pymupdf.open(stream=source) as written:
            sub = SimpleNamespace(pairs=list(pairs.items()))
            assert rewrite_link_actions(written, sub) == 2
            with pymupdf.open(stream=written.tobytes()) as out:
                assert [raw_link_uri(out, xref) for xref, _, _ in out[0].annot_xrefs()] == [expected, unrelated]
                assert not verify_links(src, out, pairs)


@pytest.mark.parametrize("indirect_action", [False, True], ids=["direct-A", "indirect-A"])
@pytest.mark.parametrize("hex_string", [False, True], ids=["literal", "hex"])
def test_link_rewrite_indirect_uri_string(indirect_action, hex_string):
    original = "mailto:jane%40example.test?subject=Parts%20(a)&body=A%2fb"
    expected = "mailto:person%40replacement.test?subject=Parts%20(a)&body=A%2fb"
    control = "mailto:janet%40example.test?subject=Keep%20this&body=B%2fc"
    pairs = {"jane@example.test": "person@replacement.test"}
    with pymupdf.open() as doc:
        page = doc.new_page()
        for y, uri in ((10, original), (30, control)):
            page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, y, 50, y + 10),
                              "uri": uri})
        link, other = [xref for xref, _, _ in doc.reload_page(page).annot_xrefs()]
        scalar = doc.get_new_xref()
        doc.update_object(scalar, f"<{original.encode('ascii').hex()}>" if hex_string
                          else r"(mailto:jane%40example.test?subject=Parts%20\(a\)&body=A%2fb)")
        control_scalar = doc.get_new_xref()
        doc.update_object(control_scalar, pymupdf.get_pdf_str(control))
        doc.xref_set_key(other, "A/URI", f"{control_scalar} 0 R")
        doc.xref_set_key(link, "A/URI", f"{scalar} 0 R")
        doc.xref_set_key(link, "A/IsMap", "false")
        catalog = doc.pdf_catalog()
        doc.xref_set_key(catalog, "FixtureSharedURI", f"{scalar} 0 R")
        action = link
        key = "A/"
        if indirect_action:
            action = doc.get_new_xref()
            doc.update_object(action, doc.xref_get_key(link, "A")[1])
            doc.xref_set_key(link, "A", f"{action} 0 R")
            doc.xref_set_key(catalog, "FixtureSharedAction", f"{action} 0 R")
            key = ""
        source = doc.tobytes()

    with pymupdf.open(stream=source) as src, pymupdf.open(stream=source) as written:
        assert src.xref_get_key(action, key + "URI") == ("xref", f"{scalar} 0 R")
        assert raw_link_uri(src, link) == original
        assert rewrite_link_actions(written, SimpleNamespace(pairs=list(pairs.items()))) == 1
        with pymupdf.open(stream=written.tobytes()) as out:
            assert not verify_links(src, out, pairs)
            assert [raw_link_uri(out, xref) for xref in (link, other)] == [expected, control]
            assert out.xref_get_key(other, "A/URI") == ("xref", f"{control_scalar} 0 R")
            assert out.xref_get_key(catalog, "FixtureSharedURI") == ("xref", f"{scalar} 0 R")
            assert out.xref_object(scalar) == src.xref_object(scalar)
            if indirect_action:
                typ, ref = out.xref_get_key(link, "A")
                assert typ == "xref"
                clone = int(ref.split()[0])
                assert clone != action
                assert out.xref_get_key(catalog, "FixtureSharedAction") == ("xref", f"{action} 0 R")
                assert out.xref_object(action) == src.xref_object(action)
                action = clone
            assert out.xref_get_key(action, key + "URI") == ("string", expected)
            assert out.xref_get_key(action, key + "IsMap") == ("bool", "false")


@pytest.mark.parametrize("invalid", ["dangling", "generation", "chain", "dict", "stream", "name"])
def test_link_rewrite_rejects_invalid_uri_xrefs(invalid):
    uri = "mailto:jane@example.test"
    pairs = {"jane@example.test": "person@replacement.test"}
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 50, 20), "uri": uri})
        link, _, _ = doc.reload_page(page).annot_xrefs()[0]
        scalar = doc.get_new_xref()
        doc.update_object(scalar, pymupdf.get_pdf_str(uri))
        target = doc.get_new_xref()
        if invalid == "dangling":
            target = doc.xref_length() + 10
        elif invalid == "generation":
            target = scalar
        elif invalid == "chain":
            doc.update_object(target, f"{scalar} 0 R")
        elif invalid in ("dict", "stream"):
            doc.update_object(target, f"<< /URI ({uri}) >>")
            if invalid == "stream":
                doc.update_stream(target, uri.encode("ascii"))
        else:
            doc.update_object(target, f"/{uri}")
        generation = 1 if invalid == "generation" else 0
        doc.xref_set_key(link, "A/URI", f"{target} {generation} R")
        before = doc.xref_object(link)
        assert raw_link_uri(doc, link) == ""
        assert rewrite_link_actions(doc, SimpleNamespace(pairs=list(pairs.items()))) == 0
        assert doc.xref_object(link) == before
        assert verify_links(doc, doc, pairs)["link_uri_xref_invalid"] == 1


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
