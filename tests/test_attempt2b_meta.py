"""Attempt 2b: SVG Dublin Core agents and STL header text. Synthetic fixtures; existing approvals only."""
from __future__ import annotations

import struct

import pytest
from step_helpers import step_bytes

from machina_deid import stl_meta as sm
from machina_deid import svg_meta as sv
from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog

SECRET = b"attempt2b-meta-secret-0123456789"
PERSON, ORG = "Jane Quincy Doe", "Acme Widgets Inc"


@pytest.fixture
def cat(tmp_path):
    """Approvals exist only from a STEP FILE_NAME (the attempt-1 policy); nothing else is approved."""
    s = tmp_path / "seed.step"
    s.write_bytes(step_bytes(name="b", author=f"('{PERSON}')", org=f"('{ORG}')"))
    c = IdentityCatalog(tmp_path / "c.db", SECRET)
    tp.discover_batch(c, [(s, "seed.step")], limit=1)
    c.approve("text", "Bolt", reason="manual test")  # approved, but not person/org: never used here
    yield c
    c.close()


def svg(creator=PERSON, publisher=ORG, title="Jane Quincy Doe drawing", extra=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:cc="http://creativecommons.org/ns#" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
     width="100mm" height="50mm" viewBox="0 0 100 50">
  <title>{title}</title>
  <metadata><rdf:RDF><cc:Work rdf:about="">
    <dc:creator><cc:Agent><dc:title> {creator} </dc:title></cc:Agent></dc:creator>
    <dc:publisher><cc:Agent><dc:title>{publisher}</dc:title></cc:Agent></dc:publisher>
    <dc:rights>Bolt</dc:rights>{extra}
  </cc:Work></rdf:RDF></metadata>
  <path d="M 0,0 L 100.5,50.25 Z" style="stroke:#000"/>
</svg>
""".encode()


def stl_binary(header: bytes, n=2):
    tri = struct.pack("<12fH", *([0.0, 0.0, 1.0] + [1.5, 2.25, -3.0] * 3), 0)
    return header.ljust(80, b"\x00")[:80] + struct.pack("<I", n) + tri * n


def stl_ascii(name):
    facet = ("  facet normal 0 0 1\n    outer loop\n      vertex 0 0 0\n      vertex 1.5 0 0\n"
             "      vertex 0 2.25e-1 0\n    endloop\n  endfacet\n")
    return f"solid {name}\n{facet}{facet}endsolid {name}\n".encode()


def go(cat, tmp_path, name, data):
    src = tmp_path / name
    src.write_bytes(data)
    dst = tmp_path / "out" / name
    before = cat.report()
    o = tp.transform_file(cat, src, dst, name)
    assert o.source_unchanged and src.read_bytes() == data
    assert cat.report() == before  # pass-2 lookups never change the catalog
    return o, (dst.read_bytes() if dst.exists() else None)


def test_svg_agents_replaced_everything_else_identical(cat, tmp_path):
    data = svg()
    o, b = go(cat, tmp_path, "d.svg", data)
    pp, po = cat.approved_pseudonym(PERSON)[1], cat.approved_pseudonym(ORG)[1]
    assert o.status == "transformed" and o.changes.svg_metadata_replacements == 2 and o.residual_approved == 0
    assert f"<dc:title> {pp} </dc:title>".encode() in b and f"<dc:title>{po}</dc:title>".encode() in b
    assert b"<dc:rights>Bolt</dc:rights>" in b  # 'text'-kind approval never used
    assert b.replace(pp.encode(), PERSON.encode()).replace(po.encode(), ORG.encode()) == data
    # <title> free text is out of scope: left and reported, never claimed clean
    assert o.independent_residual == {"svg_raw_substring": 1} and not o.fully_clean


def test_svg_no_exact_match_and_partial_values(cat, tmp_path):
    o, b = go(cat, tmp_path, "d.svg", svg(creator=f"{PERSON} Jr", publisher="Acme", title="x"))
    assert o.status == "no_approved_identities" and b is None


@pytest.mark.parametrize("bad", [
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "Jane Quincy Doe">]><svg xmlns="http://www.w3.org/2000/svg"/>',
    b"<svg xmlns='http://www.w3.org/2000/svg'><unclosed></svg>",
    b"<html xmlns='http://www.w3.org/1999/xhtml'/>",
    b'<?xml version="1.0" encoding="ISO-8859-1"?><svg xmlns="http://www.w3.org/2000/svg"/>',
])
def test_svg_refusals(tmp_path, bad):
    p = tmp_path / "x.svg"
    p.write_bytes(bad)
    r = sv.transform_svg(p, tmp_path / "o.svg", {PERSON: "Person-AAAA"})
    assert not r.ok and r.reason and not (tmp_path / "o.svg").exists()


def test_svg_direct_text_and_entity_escaped_values(cat, tmp_path):
    data = svg(creator=PERSON, extra=f"\n    <dc:contributor>{ORG}</dc:contributor>")
    o, b = go(cat, tmp_path, "d.svg", data)
    assert o.changes.svg_metadata_replacements == 3
    assert ORG.encode() not in b


def test_stl_binary_header_boundary_and_body_identical(cat, tmp_path):
    data = stl_binary(f"Exported by {PERSON} for {ORG}".encode())
    o, b = go(cat, tmp_path, "m.stl", data)
    assert o.status == "transformed" and o.changes.mesh_header_replacements == 2 and o.fully_clean
    assert b[80:] == data[80:] and len(b) == len(data) and PERSON.encode() not in b
    # no boundary -> not replaced; not person/org -> not replaced
    o, b = go(cat, tmp_path, "n.stl", stl_binary(b"XJane Quincy Doe Bolt"))
    assert o.status == "no_approved_identities" and b is None


def test_stl_binary_header_overflow_refused(tmp_path):
    p = tmp_path / "m.stl"
    p.write_bytes(stl_binary(b"x" * 60 + b" Jane"))
    r = sm.transform_stl(p, tmp_path / "o.stl", {"Jane": "P" * 40}.get)
    assert r.outcome == "identifiers_unmodifiable" and not (tmp_path / "o.stl").exists()
    r = sm.transform_stl(p, tmp_path / "o.stl", {"Jane": "Person-ABCDEFGH"}.get)  # fits into NUL padding
    out = (tmp_path / "o.stl").read_bytes()
    assert r.ok and out[80:] == p.read_bytes()[80:] and out[:80].startswith(b"x" * 60 + b" Person-ABCDEFGH")


def test_stl_ascii_solid_names_only(cat, tmp_path):
    data = stl_ascii(f"{PERSON}_bracket")
    o, b = go(cat, tmp_path, "a.stl", data)
    pp = cat.approved_pseudonym(PERSON)[1]
    assert o.status == "transformed" and o.changes.mesh_header_replacements == 2 and o.fully_clean
    assert b == data.replace(PERSON.encode(), pp.encode())


def test_stl_spans_bounded():
    assert all(b - a >= 3 for a, b in sm.spans("ab cde f g h i j k"))
    t = "a1 b2 c3 d4 e5 f6"
    assert max(len(sm._TOK.findall(t[a:b])) for a, b in sm.spans(t)) == 4
    assert not sm.spans("12 34 5678")  # needs a letter


def test_svg_stl_candidates_never_recorded_in_pass1(tmp_path):
    p = tmp_path / "d.svg"
    p.write_bytes(svg())
    q = tmp_path / "m.stl"
    q.write_bytes(stl_binary(PERSON.encode()))
    with IdentityCatalog(tmp_path / "c.db", SECRET) as c:
        r = tp.discover_batch(c, [(p, "d.svg"), (q, "m.stl")], limit=2)
        assert r.ok == 2 and r.candidates == 0 and r.new_observations == 0
        assert c.approved_pseudonym(PERSON) is None
