import hashlib
import io
import json
import zipfile

import pytest
from step_helpers import step_bytes

from machina_deid.archive import ArchiveLimits, transform_zip
from machina_deid.idmap import IdentityMap

NAME, ORG = "Jane Quincy Doe", "Acme Widgets Inc"
MAP = IdentityMap(strings={NAME: "Person A", ORG: "Org B"})


def zbytes(members, **kw) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, **kw) as z:
        for name, data in members:
            if data is None:
                z.writestr(zipfile.ZipInfo(name), b"")
            else:
                z.writestr(name, data)
    return buf.getvalue()


def write(tmp_path, data, name="in.zip"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pdf_bytes():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page(width=300, height=400).insert_text((50, 100), f"Prepared by {NAME}", fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def dxf_bytes(tmp_path):
    import ezdxf
    doc = ezdxf.new("R2010")
    doc.modelspace().add_text(f"Drawn by {NAME}", height=2.5)
    p = tmp_path / "x.dxf"
    doc.saveas(p)
    return p.read_bytes()


@pytest.mark.parametrize("bad,reason", [
    ("../evil.txt", "path_traversal"),
    ("a/../../evil.txt", "path_traversal"),
    ("/etc/passwd", "absolute_member_path"),
    ("C:/x.txt", "absolute_member_path"),
    ("a\\..\\x.txt", "backslash_in_member_name"),
    ("a//b.txt", "unsafe_member_name"),
])
def test_rejects_unsafe_paths(tmp_path, bad, reason):
    src = write(tmp_path, zbytes([("ok.txt", b"x"), (bad, b"y")]))
    res = transform_zip(src, tmp_path / "out.zip")
    assert not res.ok and res.reason == reason
    assert not (tmp_path / "out.zip").exists()
    assert not list(tmp_path.glob(".*partial"))


def test_rejects_duplicates_and_collisions(tmp_path):
    for members, reason in [([("A.txt", b"1"), ("a.txt", b"2")], "duplicate_member_path"),
                            ([("d", b"1"), ("d/x.txt", b"2")], "file_directory_collision")]:
        src = write(tmp_path, zbytes(members))
        res = transform_zip(src, tmp_path / "out.zip")
        assert res.reason == reason, res.reason
    buf = io.BytesIO()
    with pytest.warns(UserWarning), zipfile.ZipFile(buf, "w") as z:
        z.writestr("same.txt", b"1")
        z.writestr("same.txt", b"2")
    res = transform_zip(write(tmp_path, buf.getvalue()), tmp_path / "out.zip")
    assert res.reason == "duplicate_member_path"


@pytest.mark.parametrize("limits,reason", [
    (ArchiveLimits(max_members=2), "member_count_limit"),
    (ArchiveLimits(max_member_bytes=500), "member_size_limit"),
    (ArchiveLimits(max_total_bytes=1500), "total_size_limit"),
    (ArchiveLimits(max_depth=0), "depth_limit"),
])
def test_limits_enforced_before_extraction(tmp_path, limits, reason):
    inner = zbytes([("i.txt", b"z" * 100)])
    src = write(tmp_path, zbytes([("a.bin", b"a" * 1000), ("b.bin", b"b" * 400), ("n.zip", inner)]))
    res = transform_zip(src, tmp_path / "out.zip", limits=limits)
    assert not res.ok and res.reason == reason
    assert not (tmp_path / "out.zip").exists()


def test_nested_limits_count_inner_members(tmp_path):
    inner = zbytes([(f"{i}.txt", b"x") for i in range(5)])
    src = write(tmp_path, zbytes([("n.zip", inner)]))
    assert transform_zip(src, tmp_path / "o.zip", limits=ArchiveLimits(max_members=5)).reason == "member_count_limit"


def test_no_map_classifies_and_preserves_bytes(tmp_path):
    members = [("docs/", None), ("docs/a.step", step_bytes()), ("docs/readme.txt", f"by {NAME}".encode()),
               ("n.zip", zbytes([("deep/b.stp", step_bytes())]))]
    src_b = zbytes(members)
    src = write(tmp_path, src_b)
    dst = tmp_path / "out" / "o.zip"
    res = transform_zip(src, dst)
    assert res.ok and res.output_written and not res.complete
    assert res.counts == {"directory_preserved": 1, "inspected_no_map": 2, "unsupported_passthrough": 1,
                          "nested_archive": 1}
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        assert a.namelist() == b.namelist()
        for n in a.namelist():
            assert a.read(n) == b.read(n)
    assert res.source_unchanged and sha(src) == hashlib.sha256(src_b).hexdigest()


def test_nested_transformation_with_map(tmp_path):
    inner = zbytes([("deep/", None), ("deep/b.stp", step_bytes()), ("deep/c.pdf", pdf_bytes())])
    members = [("a.step", step_bytes()), ("d.dxf", dxf_bytes(tmp_path)), ("notes.txt", f"{NAME}".encode()),
               ("keep.bin", b"\x00\x01binary"), ("n.zip", inner), ("Jane Quincy Doe.txt", b"hi")]
    src_b = zbytes(members)
    src = write(tmp_path, src_b)
    dst = tmp_path / "o.zip"
    res = transform_zip(src, dst, MAP)
    assert res.ok, res.to_dict()
    assert res.counts["transformed_validated"] == 4  # a.step, d.dxf, b.stp, c.pdf
    assert res.counts["unsupported_passthrough"] == 3
    assert all(res.verification.values())
    assert not res.complete
    by_idx = {r.index: r for r in res.members}
    assert by_idx[2].residual  # notes.txt reported as residual, unchanged
    assert by_idx[3].index not in res.residual_members and 3 in res.unsupported_members
    last = res.members[-1]
    assert last.name_contains_identity and last.index in res.residual_members
    assert [c.outcome for c in res.members[4].children] == ["directory_preserved", "transformed_validated",
                                                            "transformed_validated"]
    with zipfile.ZipFile(dst) as z:
        assert z.namelist() == [n for n, _ in members]
        assert z.testzip() is None
        a = z.read("a.step")
        assert b"Person A" in a and b"FILE_NAME(\n/* name */ 'Person A bracket'" in a
        assert a.count(NAME.encode()) == 1  # only the untouched DATA-section occurrence remains...
        assert by_idx[0].residual == {0: 1} and 0 in res.residual_members  # ...and it is reported
        assert z.read("keep.bin") == b"\x00\x01binary" and z.read("notes.txt") == NAME.encode()
        with zipfile.ZipFile(io.BytesIO(z.read("n.zip"))) as zi:
            assert zi.namelist() == ["deep/", "deep/b.stp", "deep/c.pdf"]
            assert zi.read("deep/b.stp").count(NAME.encode()) == 1  # header replaced, DATA untouched
            import pymupdf
            text = pymupdf.open(stream=zi.read("deep/c.pdf"), filetype="pdf")[0].get_text()
            assert NAME not in text and "Person A" in text
    assert sha(src) == hashlib.sha256(src_b).hexdigest() and res.source_unchanged
    blob = json.dumps(res.to_dict())
    assert NAME not in blob and ORG not in blob and "Person A" not in blob and "notes" not in blob


def test_unmodifiable_supported_member_kept_and_reported(tmp_path):
    src = write(tmp_path, zbytes([("broken.pdf", f"not a pdf {NAME}".encode())]))
    res = transform_zip(src, tmp_path / "o.zip", MAP)
    assert res.ok and res.counts == {"identifiers_unmodifiable": 1}
    assert res.residual_members == [0] and res.members[0].residual == {0: 1}
    assert res.members[0].reason.startswith("parse_error")


def test_refuses_same_path_and_existing_output(tmp_path):
    src = write(tmp_path, zbytes([("a.txt", b"x")]))
    with pytest.raises(ValueError):
        transform_zip(src, src)
    (tmp_path / "o.zip").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        transform_zip(src, tmp_path / "o.zip")
    assert (tmp_path / "o.zip").read_bytes() == b"keep"


def test_bad_zip_and_encrypted(tmp_path):
    res = transform_zip(write(tmp_path, b"PK not really"), tmp_path / "o.zip")
    assert res.reason == "bad_zip" and not (tmp_path / "o.zip").exists()
    raw = bytearray(zbytes([("a.txt", b"x")]))
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set encryption flag in local + central headers
        i = raw.index(sig)
        off = i + (6 if sig == b"PK\x03\x04" else 8)
        raw[off] |= 1
    res = transform_zip(write(tmp_path, bytes(raw), "e.zip"), tmp_path / "o2.zip")
    assert res.reason == "encrypted_member"


def test_zip_svg_and_stl_metadata_rewrite_preserves_geometry(tmp_path):
    from zipfile import ZipFile

    from machina_deid.archive import transform_zip
    from machina_deid.idmap import IdentityMap

    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:dc="http://purl.org/dc/elements/1.1/">'
           b'<metadata><dc:creator>Jane Doe</dc:creator></metadata><path d="M0 0 L5 5"/></svg>')
    facets = b"facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\n"
    stl = b"solid Jane Doe\n" + facets + b"endsolid Jane Doe\n"
    source = tmp_path / "input.zip"
    output = tmp_path / "output.zip"
    with ZipFile(source, "w") as z:
        z.writestr("design.svg", svg)
        z.writestr("mesh.stl", stl)
    result = transform_zip(source, output, IdentityMap(strings={"Jane Doe": "Person 123"}))
    assert result.ok
    with ZipFile(output) as z:
        assert z.read("design.svg") == svg.replace(b"Jane Doe", b"Person 123")
        assert z.read("mesh.stl") == b"solid Person 123\n" + facets + b"endsolid Person 123\n"
