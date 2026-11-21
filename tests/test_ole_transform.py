"""Synthetic-only tests for the standalone OLE core. Expected outputs are built independently by the
fixture writer (same layout, alias placed only where verified), then compared byte-for-byte."""
from __future__ import annotations

import json
import os
import struct

import pytest

from machina_deid import ole_cli
from machina_deid import ole_transform as ot
from ole_helpers import cfb, cstring, summary_info

ACC, ALIAS = "jdoe7", "u4k2q"
U = lambda s: s.encode("utf-16-le")  # noqa: E731
RANDOM = bytes((i * 131 + 7) % 251 for i in range(5000))  # stand-in for a compressed payload


def preview(acc: str) -> bytes:
    """PreviewPNG payload; independent of property vt/code page (CString is always UTF-16)."""
    return b"\x89PNG\r\n\x1a\n" + cstring(acc) + b"\0" * 40


def streams(acc: str, raw_acc: str | None = None, vt=0x1E, author=None, cp=1252, path_acc: str | None = None):
    raw_acc = acc if raw_acc is None else raw_acc
    path_acc = acc if path_acc is None else path_acc
    props = {8: (vt, acc), 2: (0x1E, "Bracket")}
    if author is not None:
        props[4] = (0x1E, author)
    big = (b"\x11" * 507 + cstring(acc) + b"\x22" * 3000 + U(r"C:\Users" + "\\" + path_acc + r"\Desktop\p.sldprt")
           + b"\0\0" + U("x" + raw_acc + "y") + b"\x33" * 700)
    return {
        "\x05SummaryInformation": summary_info(props, cp),
        "Contents": b"\x01\x02" + cstring(acc) + b"\x00" * 10 + U(path_acc + "@example.test") + b"\0\0",
        "Big": big,
        "PreviewPNG": preview(acc),
        "Packed": RANDOM,
        "Ansi": b"\x05" + raw_acc.encode() + b"\0",
    }


def expected(acc, alias, vt=0x1E, cp=1252, keep_path=False):
    s = streams(alias, raw_acc=acc, vt=vt, cp=cp, path_acc=acc if keep_path else None)
    s["PreviewPNG"] = preview(acc)
    s["Ansi"] = b"\x05" + acc.encode() + b"\0"
    return cfb(s)


def test_parse_reads_every_stream():
    s = streams(ACC)
    c = ot.parse(cfb(s))
    assert {e.path: c.read(e) for e in c.streams()} == s


def test_cstring_crosses_sector_boundary_in_fixture():
    c = ot.parse(cfb(streams(ACC)))
    big = next(e for e in c.streams() if e.path == "Big")
    (fo, n), = ot._slice(c.chunks(big), 511, 10)
    assert fo // 512 != (fo + n - 1) // 512  # value straddles a 512-byte sector boundary


@pytest.mark.parametrize("vt", [0x1E, 0x1F])
def test_transform_matches_independent_expected_bytes(vt):
    src = cfb(streams(ACC, vt=vt))
    res = ot.transform(src, {ACC: ALIAS})
    assert res.reason is None
    assert res.data == expected(ACC, ALIAS, vt)
    kinds = sorted(w.kind for w in res.writes)
    assert kinds == ["cstring", "cstring", "path_component", "path_component", "property"]
    assert res.counts["raw_utf16"] == 2  # embedded substring + preview CString, never written
    assert res.counts["raw_ascii"] == 1


def test_short_account_with_derived_alias():
    acc = "jd"
    alias = ot.equal_length_alias(acc, b"k" * 32)
    assert len(alias) == 2 and alias != acc and alias == ot.equal_length_alias(acc, b"k" * 32)
    res = ot.transform(cfb(streams(acc)), {acc: alias})
    assert res.reason is None
    # 2-char accounts: exact property + exact-length MFC CStrings rewritten; bare path/contact tokens retained.
    assert res.data == expected(acc, alias, keep_path=True)
    assert sorted(w.kind for w in res.writes) == ["cstring", "cstring", "property"]
    c = ot.parse(res.data)
    si = c.read(next(e for e in c.streams() if e.path == "\x05SummaryInformation"))
    assert alias.encode("cp1252") + b"\0" in si and acc.encode("cp1252") + b"\0" not in si
    contents = c.read(next(e for e in c.streams() if e.path == "Contents"))
    assert cstring(alias) in contents and cstring(acc) not in contents


def test_unequal_alias_refused_and_bytes_unchanged():
    src = cfb(streams(ACC))
    res = ot.transform(src, {ACC: "toolong"})
    assert res.reason == "alias_length_mismatch" and res.data == src and not res.writes


def test_value_not_in_parsed_property_is_never_written():
    src = cfb(streams(ACC))
    res = ot.transform(src, {"Bracket": "Xxxxxxx", "zzzzz": "yyyyy"})  # title pid not scoped; absent value
    assert res.data == src and res.reason == "no_mapped_property"


def test_wrong_cstring_length_prefix_is_raw_only():
    s = streams(ACC)
    s["Contents"] = b"\xFF\xFE\xFF\x09" + U(ACC) + b"\x01" * 20
    res = ot.transform(cfb(s), {ACC: ALIAS})
    c = ot.parse(res.data)
    contents = next(e for e in c.streams() if e.path == "Contents")
    assert U(ACC) in c.read(contents)


def test_author_pid_only_when_scoped():
    src = cfb(streams(ACC, author=ACC))
    assert ot.transform(src, {ACC: ALIAS}).reason is None  # AUTHOR stays (out of scope) without failing
    res = ot.transform(src, {ACC: ALIAS}, pids=(4, 8))
    assert res.reason is None and sum(w.kind == "property" for w in res.writes) == 2


def test_independent_inspection_detects_tampering():
    src = cfb(streams(ACC))
    res = ot.transform(src, {ACC: ALIAS})
    assert ot.inspect_result(src, res.data, {ACC: ALIAS}, res.writes)["ok"]
    c = ot.parse(res.data)
    packed_off = c.chunks(next(e for e in c.streams() if e.path == "Packed"))[0][0]
    bad = bytearray(res.data)
    bad[packed_off + 100] ^= 1
    assert ot.inspect_result(src, bytes(bad), {ACC: ALIAS}, res.writes)["reason"] == "unrecorded_change"
    assert ot.inspect_result(src, res.data, {ACC: ALIAS}, res.writes[:-1])["reason"] == "unrecorded_change"
    assert ot.inspect_result(src, src, {ACC: ALIAS}, [])["reason"] == "verified_residue"


def test_non_cfb_and_corrupt_inputs_refused():
    assert ot.transform(b"PK\3\4" + b"\0" * 600, {ACC: ALIAS}).reason == "not_cfb"
    src = bytearray(cfb(streams(ACC)))
    struct.pack_into("<I", src, 0x30, 10_000)  # directory start out of range
    assert ot.transform(bytes(src), {ACC: ALIAS}).reason in ("bad_chain", "sector_out_of_range")


def test_cli_is_redacted(tmp_path, capsys):
    src = tmp_path / "a.sldprt"
    src.write_bytes(cfb(streams(ACC)))
    assert ole_cli.main(["inspect", str(src)]) == 0
    out = capsys.readouterr().out
    assert ACC not in out and "\"verified\": 5" in out
    key = tmp_path / "k"
    key.write_bytes(os.urandom(32))
    dst = tmp_path / "b.sldprt"
    assert ole_cli.main(["transform", str(src), str(dst), "--alias-key-file", str(key)]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["writes"]["property"] == 1 and ACC not in json.dumps(rep)
    assert U(ACC) not in ot.parse(dst.read_bytes()).read(
        next(e for e in ot.parse(dst.read_bytes()).streams() if e.path == "Contents"))


# ---------------------------------------------------------------- follow-up: width-preserving aliases

KEY = b"k" * 32
NON_ASCII = ["Müller", "jöe", "Ša", "Ýxé", "Дима", "山田太郎", "a\U00020000b", "ŁÓDŹ"]


@pytest.mark.parametrize("orig", NON_ASCII + ["jd", "x", "jdoe7", "j.doe"])
def test_alias_preserves_every_width(orig):
    a = ot.equal_length_alias(orig, KEY)
    assert a != orig and a == ot.equal_length_alias(orig, KEY)
    assert ot.widths_match(orig, a)
    assert len(a.encode("utf-8")) == len(orig.encode("utf-8"))
    assert len(a.encode("utf-16-le")) == len(orig.encode("utf-16-le"))
    for enc in ot.LEGACY_CODECS:
        try:
            n = len(orig.encode(enc))
        except UnicodeEncodeError:
            continue
        assert len(a.encode(enc)) == n, enc


@pytest.mark.parametrize("cp", [65001, 1252, 1250])
@pytest.mark.parametrize("vt", [0x1E, 0x1F])
def test_non_ascii_account_round_trip_under_codepages(cp, vt):
    acc = "Müller" if cp != 1250 else "Ślęk"
    alias = ot.equal_length_alias(acc, KEY)
    res = ot.transform(cfb(streams(acc, vt=vt, cp=cp)), {acc: alias})
    assert res.reason is None, res.reason
    assert res.data == expected(acc, alias, vt, cp)
    assert sum(w.kind == "cstring" for w in res.writes) == 2


def test_ascii_alias_for_non_ascii_original_refused_under_utf8():
    acc = "Müller"
    src = cfb(streams(acc, cp=65001))
    res = ot.transform(src, {acc: "abcdef"})  # same chars, but the UTF-8 property needs 7 bytes
    assert res.reason == "alias_length_mismatch" and res.data == src


def test_astral_cstring_length_counts_utf16_units():
    acc = "a\U00020000bc"  # 4 code points, 5 UTF-16 units
    alias = ot.equal_length_alias(acc, KEY)
    res = ot.transform(cfb(streams(acc, vt=0x1F)), {acc: alias})
    assert res.reason is None, res.reason
    assert res.data == expected(acc, alias, 0x1F)
    assert sum(w.kind == "cstring" for w in res.writes) == 2


def test_short_account_not_rewritten_as_bare_path_token():
    acc = "jd"  # still written via the exact property and exact-length CStrings
    res = ot.transform(cfb(streams(acc)), {acc: ot.equal_length_alias(acc, KEY)})
    assert res.reason is None
    assert not any(w.kind == "path_component" for w in res.writes)
    assert sorted(w.kind for w in res.writes) == ["cstring", "cstring", "property"]


def test_registry_detects_collision_and_is_deterministic():
    reg = ot.AliasRegistry(KEY, {"alice": "bobby"})
    assert reg.get("alice") == "bobby"
    with pytest.raises(ot.OleError, match="alias_collision"):
        ot.AliasRegistry(KEY, {"alice": "bobby", "carol": "bobby"})
    with pytest.raises(ot.OleError, match="alias_length_mismatch"):
        ot.AliasRegistry(KEY, {"Müller": "abcdef"})
    assert ot.AliasRegistry(KEY).get("Müller") == reg.get("Müller")


def test_cli_source_read_only_and_atomic(tmp_path, capsys):
    src = tmp_path / "a.sldprt"
    src.write_bytes(cfb(streams(ACC)))
    os.chmod(src, 0o444)
    before = src.read_bytes()
    key = tmp_path / "k"
    key.write_bytes(KEY)
    assert ole_cli.main(["transform", str(src), str(src), "--alias-key-file", str(key)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "output_exists"
    dst = tmp_path / "b.sldprt"
    dst.write_bytes(b"keep")
    assert ole_cli.main(["transform", str(src), str(dst), "--alias-key-file", str(key)]) == 1
    capsys.readouterr()
    assert dst.read_bytes() == b"keep"
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"PK\3\4" + b"\0" * 600)
    out = tmp_path / "c.sldprt"
    assert ole_cli.main(["transform", str(bad), str(out), "--alias-key-file", str(key)]) == 1
    capsys.readouterr()
    assert not out.exists()
    cfg = tmp_path / "aliases.json"
    cfg.write_text(json.dumps({"key_file": "k", "fixed": {ACC: ALIAS}}))
    assert ole_cli.main(["transform", str(src), str(out), "--alias-config", str(cfg)]) == 0
    capsys.readouterr()
    assert out.read_bytes() == expected(ACC, ALIAS)
    assert src.read_bytes() == before and not list(tmp_path.glob(".ole-*"))
