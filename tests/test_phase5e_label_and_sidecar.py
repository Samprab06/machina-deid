"""Phase 5E regressions: structural-label values never become candidates; AppleDouble sidecars in ZIPs."""
import io
import json
import zipfile

import ezdxf
import pytest
from step_helpers import step_bytes

from machina_deid import archive as ar
from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog
from machina_deid.discovery import discover_dxf, discover_pdf, discover_step, report
from machina_deid.idmap import IdentityMap
from test_twopass import ORG, PERSON, SECRET, pdf_bytes, zip_file

LABELS = ["author", "Author", "organization", "ORGANIZATION", "name", "title", "subject", "company",
          "creator", "producer", "Author:"]


@pytest.fixture
def cat(tmp_path):
    c = IdentityCatalog(tmp_path / "cat.sqlite", SECRET)
    yield c
    c.close()


def _public(cands, extra=()):
    blob = json.dumps(report(cands)) + "".join(repr(c) for c in cands)
    blob += "".join(c.location + c.reason for c in cands) + "".join(json.dumps(x) for x in extra)
    return blob.lower()


def _dxf(path, company):
    doc = ezdxf.new("R2010")
    blk = doc.blocks.new("TB")
    blk.add_attdef("COMPANY", (0, 0), height=2.5)
    blk.add_attdef("AUTHOR", (0, 5), height=2.5)
    ins = doc.modelspace().add_blockref("TB", (0, 0))
    ins.add_auto_attribs({"COMPANY": company, "AUTHOR": "author"})
    doc.saveas(path)
    return path


def _pdf_meta(**meta):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    doc.set_metadata(meta)
    b = doc.tobytes()
    doc.close()
    return b


@pytest.mark.parametrize("label", LABELS)
def test_step_file_name_label_values_dropped_and_catalog_accepts(tmp_path, cat, label):
    p = tmp_path / "x.step"
    p.write_bytes(step_bytes(author=f"('{label}','{PERSON}')", org=f"('{label}','{ORG}')"))
    cands = discover_step(p)
    vals = {c.value for c in cands}
    assert label.strip() not in vals and PERSON in vals and ORG in vals
    res = tp.discover_batch(cat, [(p, "x.step")], limit=1)  # previously CatalogError
    assert res.failed == 0 and res.ok == 1
    assert PERSON.lower() not in _public(cands) and ORG.lower() not in _public(cands)


def test_pdf_metadata_label_values_dropped_real_author_kept(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(_pdf_meta(author="author", title="title", subject="subject", keywords="keywords"))
    assert discover_pdf(p) == []
    p.write_bytes(_pdf_meta(author=PERSON, title="Bracket drawing", subject="subject"))
    vals = {c.value for c in discover_pdf(p)}
    assert vals == {PERSON, "Bracket drawing"}


def test_dxf_attribute_label_values_dropped_real_company_kept(tmp_path, cat):
    p = _dxf(tmp_path / "c.dxf", ORG)
    cands = discover_dxf(p)
    assert {c.value for c in cands} == {ORG}
    _dxf(tmp_path / "d.dxf", "company")
    assert discover_dxf(tmp_path / "d.dxf") == []
    res = tp.discover_batch(cat, [(tmp_path / "c.dxf", "c.dxf"), (tmp_path / "d.dxf", "d.dxf")], limit=2)
    assert res.failed == 0 and res.ok == 2 and res.candidates == len(cands)


def test_nested_zip_label_values_dropped_and_no_raw_in_public_output(tmp_path, cat):
    step = step_bytes(author=f"('author','{PERSON}')", org="('organization')")
    inner = zip_file(tmp_path / "inner.zip", [("d.pdf", _pdf_meta(author="author")), ("p.stp", step)])
    z = zip_file(tmp_path / "e.zip", [("nested/inner.zip", inner.read_bytes()), ("x.pdf", pdf_bytes(author=PERSON))])
    found = tp.discover_file(z, "e.zip")
    assert all(d.status == "ok" for d in found)
    cands = [c for d in found for c in d.candidates]
    assert {c.value for c in cands} >= {PERSON} and not {"author", "organization"} & {c.value for c in cands}
    res = tp.discover_batch(cat, [(z, "e.zip")], limit=1)
    assert res.failed == 0
    pub = _public(cands, [res.to_dict(), [d.__repr__() for d in found]])
    assert PERSON.lower() not in pub
    blob = (tmp_path / "cat.sqlite").read_bytes().lower()
    assert PERSON.lower().encode() not in blob


@pytest.mark.parametrize("name", ["__MACOSX/._a.pdf", "__MACOSX/sub/._b.step", "._c.dxf", "dir/._d.stp",
                                  "__MACOSX/inner.zip"])
def test_is_metadata_sidecar(name):
    assert ar.is_metadata_sidecar(name)


@pytest.mark.parametrize("name", ["a.pdf", "dir/b._step.step", "x/MACOSX/a.pdf", "a._b.pdf"])
def test_not_metadata_sidecar(name):
    assert not ar.is_metadata_sidecar(name)


def _sidecar_zip(tmp_path):
    junk = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 40
    return zip_file(tmp_path / "s.zip", [("a.pdf", pdf_bytes(author=PERSON)), ("__MACOSX/._a.pdf", junk),
                                         ("._b.step", junk), ("dir/._c.dxf", junk)]), junk


def test_zip_sidecars_reported_unsupported_not_failed(tmp_path, cat):
    z, _ = _sidecar_zip(tmp_path)
    found = {d.path: d for d in tp.discover_file(z, "s.zip")}
    assert found["s.zip!a.pdf"].status == "ok"
    for n in ("__MACOSX/._a.pdf", "._b.step", "dir/._c.dxf"):
        d = found[f"s.zip!{n}"]
        assert (d.status, d.reason, d.format, d.candidates) == ("unsupported", "metadata_sidecar", None, [])
    res = tp.discover_batch(cat, [(z, "s.zip")], limit=1)
    assert res.failed == 0 and res.unsupported == 3 and not res.failures


def test_zip_sidecars_preserved_unchanged_on_transform(tmp_path):
    z, junk = _sidecar_zip(tmp_path)
    out = tmp_path / "o.zip"
    r = ar.transform_zip(z, out, IdentityMap(strings={PERSON: "Person-0001"}))
    assert r.ok
    side = [m for m in ar._walk(r.members) if m.reason == "metadata_sidecar"]
    assert len(side) == 3 and all(m.outcome == "unsupported_passthrough" for m in side)
    with zipfile.ZipFile(out) as zo, zipfile.ZipFile(z) as zi:
        for n in ("__MACOSX/._a.pdf", "._b.step", "dir/._c.dxf"):
            assert zo.read(n) == zi.read(n) == junk
