"""Regression tests for archive._scan raw residual counting (synthetic values only)."""
import io
import zipfile

from machina_deid import archive as ar
from machina_deid.idmap import IdentityMap


def m(*vals):
    return IdentityMap(strings={v: f"P{i}x" for i, v in enumerate(vals)})


def test_value_not_encodable_in_cp1252_never_matches_empty_bytes():
    # legacy: 'ignore' turned this into b"" and data.count(b"") == len(data) + 1
    data = b"plain ascii content with nothing in it"
    assert ar._scan_legacy(data, m("张伟")) == {0: len(data) + 1}
    assert ar._scan(data, m("张伟")) == {}


def test_partial_drop_does_not_match_degraded_value():
    # cp1252 cannot encode U+0141; legacy searched for 'ukasz', matching an unrelated name
    data = "Lukasz".encode()
    assert ar._scan_legacy(data, m("Łukasz")) == {0: 1}
    assert ar._scan(data, m("Łukasz")) == {}
    assert ar._scan("x Łukasz y".encode("utf-8"), m("Łukasz")) == {0: 1}


def test_encodings_and_no_double_count_across_encodings():
    v = "Jörg Doe"
    for enc in ("utf-8", "cp1252", "utf-16-le"):
        assert ar._scan(b"..." + v.encode(enc) + b"...", m(v)) == {0: 1}
    assert ar._scan(b"Jane Doe Jane Doe", m("Jane Doe")) == {0: 2}  # ascii: utf-8 == cp1252, counted once


def test_case_and_exactness():
    assert ar._scan(b"JANE DOE", m("Jane Doe")) == {}  # exact only (diagnostic tracks casefold separately)


def test_entry_name_flag_not_triggered_by_unencodable_value(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dir/readme.txt", b"hello")
    src = tmp_path / "a.zip"
    src.write_bytes(buf.getvalue())
    r = ar.transform_zip(src, tmp_path / "o.zip", m("张伟"))
    assert r.ok
    assert not any(x.name_contains_identity for x in r.members)
    assert not any(x.residual for x in r.members)


def test_scan_encodings_helper():
    assert ar.scan_encodings("abc") == {"utf-8": b"abc", "utf-16-le": "abc".encode("utf-16-le")}
    assert set(ar.scan_encodings("é")) == {"utf-8", "cp1252", "utf-16-le"}
    assert set(ar.scan_encodings("张")) == {"utf-8", "utf-16-le"}
    assert ar.scan_encodings("") == {}
