import pytest

from machina_deid.classify import classify_bytes, classify_path

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.mark.parametrize(
    "name,header,fmt,family,status",
    [
        ("a.pdf", b"%PDF-1.7", "pdf", "document", "verified"),
        ("a.PNG", b"\x89PNG\r\n\x1a\n", "png", "image", "verified"),
        ("a.jpg", b"\xff\xd8\xff\xe0", "jpeg", "image", "verified"),
        ("a.zip", b"PK\x03\x04", "zip", "archive", "verified"),
        ("a.7z", b"7z\xbc\xaf\x27\x1c", "7z", "archive", "verified"),
        ("a.step", b"ISO-10303-21;", "step", "neutral_cad", "verified"),
        ("a.stp", b"  ISO-10303-21;", "step", "neutral_cad", "verified"),
        ("a.dwg", b"AC1027", "dwg", "drawing", "verified"),
        ("a.dxf", b"  0\nSECTION", "dxf", "drawing", "unverified"),
        ("a.sldprt", OLE, "sldprt", "native_cad", "verified"),
        ("a.sldasm", b"\x00\x01", "sldasm", "native_cad", "unverified"),
        ("a.slddrw", OLE, "slddrw", "native_cad", "verified"),
        ("a.ipt", OLE, "ipt", "native_cad", "verified"),
        ("a.iam", OLE, "iam", "native_cad", "verified"),
        ("a.idw", OLE, "idw", "native_cad", "verified"),
        ("a.FCStd", b"PK\x03\x04", "fcstd", "native_cad", "verified"),
        ("a.stl", b"solid x", "stl", "mesh", "unverified"),
    ],
)
def test_known_formats(name, header, fmt, family, status):
    c = classify_bytes(name, header, 100)
    assert (c.format, c.family, c.status) == (fmt, family, status)


def test_mismatch_is_conservative():
    c = classify_bytes("a.pdf", b"\x89PNG\r\n\x1a\n", 100)
    assert (c.format, c.status) == ("unknown", "mismatch")
    c = classify_bytes("a.stl", b"%PDF-1.4", 100)
    assert (c.format, c.status) == ("unknown", "mismatch")
    c = classify_bytes("a.fcstd", b"not a zip", 100)
    assert c.status == "mismatch"


def test_unknown_and_empty():
    assert classify_bytes("a.xyz", b"PK\x03\x04", 10).format == "unknown"
    assert classify_bytes("noext", b"%PDF-1.4", 10).status == "unknown"
    e = classify_bytes("a.pdf", b"", 0)
    assert e.status == "empty" and e.format == "pdf"


def test_never_claims_rewrite_support():
    assert not classify_bytes("a.pdf", b"%PDF-", 5).supported_for_rewrite


def test_classify_path(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4\n" + b"x" * 100)
    assert classify_path(f).status == "verified"
    z = tmp_path / "z.pdf"
    z.write_bytes(b"")
    assert classify_path(z).status == "empty"
    assert classify_path(tmp_path / "missing.pdf").status == "unknown"
