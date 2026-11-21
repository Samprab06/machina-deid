"""Attempt 2: STEP DATA person/organization coverage, incl. STEP members of (nested) ZIPs. Synthetic only."""
from __future__ import annotations

import io
import zipfile

from step_helpers import step_bytes

from machina_deid import step_data_transform as sdt
from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog

SECRET = b"attempt2-unit-secret-0123456789"
PERSON, ORG = "Jane Quincy Doe", "Acme Widgets Inc"

DATA = (
    "DATA;\n"
    "#1=CARTESIAN_POINT('Acme Widgets Inc',(0.,1.5,-2.25E-3));\n"
    "#2=PRODUCT('BRK-100','Bracket 100','',(#3));\n"
    "#3=PRODUCT_CONTEXT('',#4,'mechanical');\n"
    "#4=APPLICATION_CONTEXT('design');\n"
    "#10=ORGANIZATION('Acme Widgets Inc','Jane Quincy Doe','');\n"
    "#11=PERSON('Jane Quincy Doe','Doe','Jane Quincy',$,$,$);\n"
    "#12=PERSON('12345','Smith','Al',$,$,$);\n"
    "#13=PERSON_AND_ORGANIZATION(#11,#10);\n"
    "#15=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#16);\n"
    "#16=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
    "ENDSEC;\nEND-ISO-10303-21;\n"
)


def stp(author=f"('{PERSON}')", org=f"('{ORG}')"):
    return step_bytes(name="bracket", author=author, org=org, data=DATA.encode("latin-1"))


def _approved(tmp_path, src, rel):
    cat = IdentityCatalog(tmp_path / "c.db", SECRET)
    tp.discover_batch(cat, [(src, rel)], limit=10)
    return cat


def _check_invariants(a: bytes, b: bytes):
    pa, pb = sdt.parse(a, sdt.IDENTITY_SPEC), sdt.parse(b, sdt.IDENTITY_SPEC)
    assert sdt.graph(pa.records) == sdt.graph(pb.records)
    assert sdt._skeleton(pa.text, pa.records)[1] == sdt._skeleton(pb.text, pb.records)[1]


def test_top_level_step_person_org_positions_replaced(tmp_path):
    src = tmp_path / "a.stp"
    src.write_bytes(stp())
    with _approved(tmp_path, src, "a.stp") as cat:
        o = tp.transform_file(cat, src, tmp_path / "o.stp", "a.stp")
        assert o.status == "transformed", o.reason
        b = (tmp_path / "o.stp").read_bytes()
        pp, po = cat.approved_pseudonym(PERSON)[1], cat.approved_pseudonym(ORG)[1]
        # ORGANIZATION.id/name and PERSON.id + split first/last (joined match) all replaced
        assert f"#10=ORGANIZATION('{po}','{pp}','');".encode() in b
        first, _, last = pp.partition(" ")
        if not _:
            first, last = "", pp
        assert f"#11=PERSON('{pp}','{last}','{first}',$,$,$);".encode() in b
        # unrelated person / numeric id / geometry label / product data untouched
        assert b"#12=PERSON('12345','Smith','Al',$,$,$);" in b
        assert b"CARTESIAN_POINT('Acme Widgets Inc'" in b and b"PRODUCT('BRK-100','Bracket 100'" in b
        _check_invariants(src.read_bytes(), b)
        assert o.residual_approved == 0
        assert o.independent_residual == {"step_data_literal_substring": 1}  # geometric label, kept visible
        assert src.read_bytes() == stp()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_zip_and_nested_zip_step_members_get_data_stage(tmp_path):
    inner = _zip({"deep/b.stp": stp()})
    src = tmp_path / "pkg.zip"
    src.write_bytes(_zip({"a.stp": stp(), "inner.zip": inner, "notes.bin": b"\x00\x01"}))
    with _approved(tmp_path, src, "pkg.zip") as cat:
        o = tp.transform_file(cat, src, tmp_path / "out.zip", "pkg.zip")
        assert o.status in ("transformed", "failed")
        assert o.output_written, o.reason
        pp = cat.approved_pseudonym(PERSON)[1]
        with zipfile.ZipFile(tmp_path / "out.zip") as z:
            a = z.read("a.stp")
            b = zipfile.ZipFile(io.BytesIO(z.read("inner.zip"))).read("deep/b.stp")
        for m in (a, b):
            assert f"'{pp}'".encode() in m and b"'Doe','Jane Quincy'" not in m
            assert b"#10=ORGANIZATION('Acme Widgets Inc'" not in m
            _check_invariants(stp(), m)
        assert o.changes.step_data_replacements >= 8 and o.nested_archive_replacements > 0


def test_unapproved_values_never_replaced(tmp_path):
    src = tmp_path / "n.stp"
    src.write_bytes(stp(author="('')", org="('')"))
    with _approved(tmp_path, src, "n.stp") as cat:
        o = tp.transform_file(cat, src, tmp_path / "o.stp", "n.stp")
        assert o.status == "no_approved_identities" and not (tmp_path / "o.stp").exists()


def test_numeric_and_short_ids_are_not_candidates(tmp_path):
    src = tmp_path / "n.stp"
    src.write_bytes(stp())
    from machina_deid.discovery import discover_step
    vals = {c.value for c in discover_step(src) if c.kind == "org_name"}
    assert "12345" not in vals and all(tp.auto_decision(c)[0] == "review"
                                       for c in discover_step(src) if c.kind == "org_name")


def test_pass2_only_candidates_do_not_change_pass1_approvals(tmp_path):
    # file A carries the person only in DATA PERSON; file B has it as FILE_NAME author. In pass 1, A's
    # DATA candidate must not create a first decision that blocks B's author approval.
    a, b = tmp_path / "a.stp", tmp_path / "b.stp"
    # (ORGANIZATION.name is an attempt-1 pass-1 candidate, so keep the person only in PERSON positions)
    a.write_bytes(stp(author="('')", org="('')").replace(b"'Acme Widgets Inc','Jane Quincy Doe'",
                                                         b"$,'Other Org Name'"))
    b.write_bytes(stp())
    with IdentityCatalog(tmp_path / "c.db", SECRET) as cat:
        tp.discover_batch(cat, [(a, "a.stp"), (b, "b.stp")], limit=10)
        assert cat.approved_pseudonym(PERSON) is not None
        o = tp.transform_file(cat, a, tmp_path / "oa.stp", "a.stp")  # cross-file corroborated in pass 2
        assert o.status == "transformed" and o.changes.step_data_replacements >= 3, o.reason
