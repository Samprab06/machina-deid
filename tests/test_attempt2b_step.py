"""Attempt 2b: the attempt-1 "exactly one residual per STEP file" effect and its targeted fixes.
Synthetic fixtures only."""
from __future__ import annotations

import io
import zipfile

from step_helpers import step_bytes

from machina_deid import step_data_transform as sdt
from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog

SECRET = b"attempt2b-unit-secret-0123456789"
PERSON, ORG = "Jane Quincy Doe", "Acme Widgets Inc"


def data(*lines: str) -> bytes:
    body = "".join(line + "\n" for line in lines)
    return ("DATA;\n#1=CARTESIAN_POINT('Jane Quincy Doe pt',(0.,1.5,-2.25E-3));\n" + body +
            "#90=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#91);\n"
            "#91=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\nENDSEC;\nEND-ISO-10303-21;\n").encode("latin-1")


def run(tmp_path, raw: bytes, name="a.stp"):
    src = tmp_path / name
    src.write_bytes(raw)
    cat = IdentityCatalog(tmp_path / "c.db", SECRET)
    tp.discover_batch(cat, [(src, name)], limit=10)
    dst = tmp_path / "out" / name
    o = tp.transform_file(cat, src, dst, name)
    assert o.source_unchanged and src.read_bytes() == raw
    return cat, o, (dst.read_bytes() if dst.exists() else b"")


def invariants(a: bytes, b: bytes):
    pa, pb = sdt.parse(a, sdt.DESCRIPTIVE_SPEC), sdt.parse(b, sdt.DESCRIPTIVE_SPEC)
    assert sdt.graph(pa.records) == sdt.graph(pb.records)
    assert sdt._skeleton(pa.text, pa.records) == sdt._skeleton(pb.text, pb.records)


def test_preprocessor_repeat_was_the_single_hit_and_is_now_replaced(tmp_path):
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org=f"('{ORG}')", preproc=f"{ORG} STEP exporter",
                     data=data("#2=PRODUCT('BRK','Bracket','',(#3));"))
    cat, o, b = run(tmp_path, raw)
    assert o.status == "transformed" and o.residual_approved == 0 and o.changes.step_header_replacements == 3
    assert ORG.encode() not in b.split(b"DATA;")[0]
    assert b.split(b"DATA;")[1] == raw.split(b"DATA;")[1]  # DATA untouched (no descriptive hit)


def test_timestamp_hit_never_edited_still_counted(tmp_path):
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org="('')", ts=f"{PERSON} 2024",
                     data=data("#2=PRODUCT('BRK','Bracket','',(#3));"))
    _, o, b = run(tmp_path, raw)
    assert o.status == "transformed" and o.residual_approved == 1 and not o.fully_clean
    assert f"'{PERSON} 2024'".encode() in b


def test_product_fields_boundary_aware_and_geometry_label_untouched(tmp_path):
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org=f"('{ORG}')", data=data(
        f"#2=PRODUCT('{PERSON}_BRK','Bracket by {PERSON}','{ORG} part',(#3));",
        f"#4=PRODUCT('X{PERSON}','{ORG}s','',(#3));",  # no boundary: not touched
        f"#5=PRODUCT_DEFINITION('design','{PERSON} rev A',#6,#7);",
        f"#8=NEXT_ASSEMBLY_USAGE_OCCURRENCE('NAUO1','{PERSON}_BRK:1','',#5,#5,$);",
        f"#9=SHAPE_REPRESENTATION('{PERSON} body',(#1),#91);"))  # not in spec: untouched
    cat, o, b = run(tmp_path, raw)
    pp, po = cat.approved_pseudonym(PERSON)[1], cat.approved_pseudonym(ORG)[1]
    assert o.status == "transformed" and o.changes.step_descriptive_replacements == 5
    assert f"#2=PRODUCT('{pp}_BRK','Bracket by {pp}','{po} part',(#3));".encode() in b
    assert f"#4=PRODUCT('X{PERSON}','{ORG}s','',(#3));".encode() in b
    assert f"#5=PRODUCT_DEFINITION('design','{pp} rev A',#6,#7);".encode() in b
    assert f"#8=NEXT_ASSEMBLY_USAGE_OCCURRENCE('NAUO1','{pp}_BRK:1','',#5,#5,$);".encode() in b
    assert f"#9=SHAPE_REPRESENTATION('{PERSON} body'".encode() in b
    assert b"CARTESIAN_POINT('Jane Quincy Doe pt',(0.,1.5,-2.25E-3))" in b
    invariants(raw, b)
    # geometry/representation labels remain visible to the independent scan; never claimed clean
    assert o.independent_residual["step_data_literal_substring"] == 4 and not o.fully_clean


def test_directive_or_8bit_descriptive_literal_left_and_counted(tmp_path):
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org="('')", data=data(
        f"#2=PRODUCT('BRK','{PERSON} \\X2\\00C4\\X0\\','',(#3));"))
    p = tmp_path / "s.stp"
    p.write_bytes(raw)
    r = sdt.transform_descriptive(p, tmp_path / "o.stp", {PERSON: "Person-AAAA"})
    assert r.outcome == "identifiers_unmodifiable" and r.unmodifiable == 1 and not (tmp_path / "o.stp").exists()


def test_undecodable_unapproved_identity_literal_not_counted(tmp_path):
    bad = "#20=ORGANIZATION('x','M\xfcller \xff\xfe GmbH','');"  # raw 8-bit, not UTF-8: undecodable
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org="('')",
                     data=data(f"#10=ORGANIZATION('o','{PERSON}','');", bad))
    _, o, b = run(tmp_path, raw)
    assert o.status == "transformed" and o.residual_approved == 0  # attempt 1 counted 1 here
    assert bad.encode("latin-1") in b


def test_undecodable_literal_carrying_approved_value_still_counted(tmp_path):
    raw = step_bytes(name="bracket", author=f"('{PERSON}')", org="('')",
                     data=data(f"#10=ORGANIZATION('o','{PERSON}','');", f"#20=ORGANIZATION('x','{PERSON} \xff','');"))
    _, o, _ = run(tmp_path, raw)
    assert o.residual_approved == 1 and not o.fully_clean


def test_zip_member_gets_preprocessor_and_descriptive_stages(tmp_path):
    member = step_bytes(name="bracket", author=f"('{PERSON}')", org=f"('{ORG}')", preproc=f"{ORG} exporter",
                        data=data(f"#2=PRODUCT('BRK','Bracket by {PERSON}','',(#3));"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m/part.stp", member)
    _, o, b = run(tmp_path, buf.getvalue(), "a.zip")
    assert o.status == "transformed", o.reason
    out = zipfile.ZipFile(io.BytesIO(b)).read("m/part.stp")
    assert ORG.encode() not in out.split(b"DATA;")[0] and b"Bracket by Jane" not in out
    assert o.changes.step_descriptive_replacements == 1
    # the geometry label stays and the independent raw rescan still reports it
    assert o.residual_approved == 1
