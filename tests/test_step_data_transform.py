"""Phase 5D: STEP DATA ORGANIZATION.name transformation (synthetic data only)."""
from __future__ import annotations

import pytest

from machina_deid import step_data_transform as sdt
from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog
from machina_deid.discovery import discover_step
from step_helpers import step_bytes

SECRET = b"s" * 32

ORG_DATA = (
    "DATA;\n"
    "#1=CARTESIAN_POINT('Acme Widgets Ltd',(0.,1.5,-2.25E-3));\n"
    "#2=DIRECTION('',(0.,0.,1.));\n"
    "#3=LINE('',#1,#2);\n"
    "#10=ORGANIZATION('ORG-7','Acme Widgets Ltd','Acme Widgets Ltd supplier');\n"
    "#11=ORGANIZATION($,\n  /* multi */ 'O''Brien \\\\ Sons',\n  $);\n"
    "#12=PERSON('p1','Acme Widgets Ltd',$,$,$,$);\n"
    "#13=PERSON_AND_ORGANIZATION(#12,#10);\n"
    "#14=ORGANIZATION('x','UNKNOWN','');\n"
    "#15=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#16);\n"
    "#16=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
    "#17=ORGANIZATION('a','\\X2\\00C4\\X0\\rger GmbH','d');\n"
    "ENDSEC;\nEND-ISO-10303-21;\n"
).encode("latin-1")


def write(tmp_path, data=ORG_DATA, name="m.stp", nl=b"\n"):
    p = tmp_path / name
    p.write_bytes(step_bytes(author="('')", org="('')", data=data, nl=nl))
    return p


def test_schema_constants():
    # ISO 10303-41: ORGANIZATION(id OPTIONAL identifier, name label, description OPTIONAL text)
    assert (sdt.ENTITY, sdt.NAME_INDEX, sdt.ARITY) == ("ORGANIZATION", 1, 3)


def test_sites_multiline_escaped_and_wrong_type(tmp_path):
    names = dict(sdt.organization_names(write(tmp_path)))
    assert names == {10: "Acme Widgets Ltd", 11: "O'Brien \\ Sons", 14: "UNKNOWN", 17: "Ärger GmbH"}
    assert 12 not in names and 1 not in names  # PERSON / geometry strings are never sites


def test_replace_only_name_parameter_exact(tmp_path):
    src, dst = write(tmp_path), tmp_path / "o.stp"
    r = sdt.transform_organizations(src, dst, {"Acme Widgets Ltd": "Organization-AAAA",
                                               "O'Brien \\ Sons": "Organization-BBBB",
                                               "Ärger GmbH": "Organization-CCCC"})
    assert r.ok and r.outcome == "transformed_validated", r.reason
    assert r.graph_identical and r.numeric_tokens_identical and r.non_target_bytes_identical
    assert r.replacements == {0: 1, 1: 1, 2: 1}
    assert "not_performed" in r.geometry_validation
    a, b = src.read_bytes(), dst.read_bytes()
    assert b"#10=ORGANIZATION('ORG-7','Organization-AAAA','Acme Widgets Ltd supplier');" in b
    assert b"#11=ORGANIZATION($,\n  /* multi */ 'Organization-BBBB',\n  $);" in b
    assert b"#17=ORGANIZATION('a','Organization-CCCC','d');" in b
    # wrong types and other parameters untouched
    assert b"#1=CARTESIAN_POINT('Acme Widgets Ltd'" in b and b"#12=PERSON('p1','Acme Widgets Ltd'" in b
    assert a.split(b"DATA;")[0] == b.split(b"DATA;")[0]
    for keep in (b"LENGTH_MEASURE(25.4),#16", b"SI_UNIT(.MILLI.,.METRE.)", b"(0.,1.5,-2.25E-3)", b"#13=PERSON_AND_ORGANIZATION(#12,#10);"):
        assert keep in b


def test_graph_preserved_on_reparse(tmp_path):
    src, dst = write(tmp_path), tmp_path / "o.stp"
    assert sdt.transform_organizations(src, dst, {"Acme Widgets Ltd": "Organization-AAAA"}).ok
    p, q = sdt.parse(src.read_bytes()), sdt.parse(dst.read_bytes())
    assert sdt.graph(p.records) == sdt.graph(q.records)
    assert sdt._skeleton(p.text, p.records) == sdt._skeleton(q.text, q.records)


def test_exact_only_no_partial_case_or_whitespace_match(tmp_path):
    src = write(tmp_path)
    for m in ({"Acme Widgets": "Organization-X"}, {"acme widgets ltd": "Organization-X"},
              {"Acme Widgets Ltd ": "Organization-X"}, {"ORG-7": "Organization-X"},
              {"Acme Widgets Ltd supplier": "Organization-X"}):
        r = sdt.transform_organizations(src, tmp_path / "o.stp", m)
        assert r.outcome == "inspected_no_identifiers" and not (tmp_path / "o.stp").exists()


def test_complex_and_wrong_arity_rejected(tmp_path):
    data = ("DATA;\n#1=(ORGANIZATION('i','Acme Widgets Ltd','d')FOO());\n"
            "#2=ORGANIZATION('i','Acme Widgets Ltd');\n#3=ORGANIZATION('i',('Acme Widgets Ltd'),'d');\n"
            "ENDSEC;\nEND-ISO-10303-21;\n").encode()
    r = sdt.transform_organizations(write(tmp_path, data), tmp_path / "o.stp", {"Acme Widgets Ltd": "Organization-X"})
    assert r.outcome == "inspected_no_identifiers" and r.organizations_in_scope == 0


def test_crlf_preserved_and_idempotent(tmp_path):
    src = write(tmp_path, nl=b"\r\n")
    o1, o2 = tmp_path / "o1.stp", tmp_path / "o2.stp"
    m = {"Acme Widgets Ltd": "Organization-AAAA"}
    assert sdt.transform_organizations(src, o1, m).ok
    b = o1.read_bytes()
    assert b.count(b"\r\n") == b.count(b"\n") and len(b) - len(src.read_bytes()) == 1
    r2 = sdt.transform_organizations(o1, o2, m)  # second application: nothing left to replace
    assert r2.outcome == "inspected_no_identifiers" and not o2.exists()
    o3 = tmp_path / "o3.stp"
    assert sdt.transform_organizations(src, o3, m).ok and o3.read_bytes() == b  # deterministic


def test_malformed_fails_closed(tmp_path):
    bad = b"DATA;\n#1=ORGANIZATION('i','Acme Widgets Ltd','d')\n#2=ORGANIZATION('i',;\nENDSEC;\nEND-ISO-10303-21;\n"
    r = sdt.transform_organizations(write(tmp_path, bad), tmp_path / "o.stp", {"Acme Widgets Ltd": "Organization-X"})
    assert r.outcome == "failed_validation" and not (tmp_path / "o.stp").exists()
    with pytest.raises(ValueError):
        sdt.transform_organizations(write(tmp_path), tmp_path / "o.stp", {"Acme Widgets Ltd": "bad\nvalue"})


def test_discovery_review_required_and_placeholders_dropped(tmp_path):
    cands = [c for c in discover_step(write(tmp_path)) if c.kind == "org_name"]
    assert sorted(c.value for c in cands) == sorted(["Acme Widgets Ltd", "O'Brien \\ Sons", "Ärger GmbH"])
    assert all(tp.auto_decision(c)[0] == "review" for c in cands)
    assert "Acme Widgets Ltd" not in repr(cands)


def test_twopass_typed_org_approval_required(tmp_path):
    src = write(tmp_path)
    with IdentityCatalog(tmp_path / "c.db", SECRET) as cat:
        res = tp.discover_batch(cat, [(src, "m.stp")], limit=10)
        assert res.decisions.get("approved", 0) == 0  # nothing auto-approved
        o = tp.transform_file(cat, src, tmp_path / "o0.stp", "m.stp")
        assert o.status == "no_approved_identities" and not (tmp_path / "o0.stp").exists()
        # approved under a non-org kind: not replaced in DATA, counted as residual
        cat.decide("text", "O'Brien \\ Sons", "approved", reason="manual test", source="manual")
        cat.approve("org", "Acme Widgets Ltd", reason="manual test")
        o = tp.transform_file(cat, src, tmp_path / "o1.stp", "m.stp")
        assert o.status == "transformed" and o.data_organizations_replaced == 1, o.reason
        assert o.residual_approved == 1 and o.source_unchanged
        b = (tmp_path / "o1.stp").read_bytes()
        pseud = cat.approved_pseudonym("Acme Widgets Ltd", kind="org")[1]
        assert f"#10=ORGANIZATION('ORG-7','{pseud}',".encode() in b
        assert b"O''Brien" in b and b"#12=PERSON('p1','Acme Widgets Ltd'" in b
        # idempotent second pass on the output
        o2 = tp.transform_file(cat, tmp_path / "o1.stp", tmp_path / "o2.stp", "m.stp")
        assert o2.data_organizations_replaced == 0 and not (tmp_path / "o2.stp").exists()


def test_twopass_header_and_data_stages_combined(tmp_path):
    src = tmp_path / "h.stp"
    src.write_bytes(step_bytes(org="('Acme Widgets Ltd')", data=ORG_DATA))
    with IdentityCatalog(tmp_path / "c.db", SECRET) as cat:
        tp.discover_batch(cat, [(src, "h.stp")], limit=10)  # FILE_NAME org auto-approved as typed org
        assert cat.approved_pseudonym("Acme Widgets Ltd", kind="org") is not None
        o = tp.transform_file(cat, src, tmp_path / "o.stp", "h.stp")
        assert o.status == "transformed", o.reason
        assert o.data_organizations_replaced == 1 and o.fields_replaced >= 2
        b = (tmp_path / "o.stp").read_bytes()
        assert b.count(b"Acme Widgets Ltd") == 3  # CARTESIAN_POINT label, PERSON, description: untouched
