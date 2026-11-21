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


def streams(acc: str, raw_acc: str | None = None, vt=0x1E, author=None):
    raw_acc = acc if raw_acc is None else raw_acc
    props = {8: (vt, acc), 2: (0x1E, "Bracket")}
    if author is not None:
        props[4] = (0x1E, author)
    big = (b"\x11" * 507 + cstring(acc) + b"\x22" * 3000 + U(r"C:\Users" + "\\" + acc + r"\Desktop\p.sldprt")
           + b"\0\0" + U("x" + raw_acc + "y") + b"\x33" * 700)
    return {
        "\x05SummaryInformation": summary_info(props),
        "Contents": b"\x01\x02" + cstring(acc) + b"\x00" * 10 + U(acc + "@example.test") + b"\0\0",
        "Big": big,
        "PreviewPNG": b"\x89PNG" + cstring(acc) + b"\0" * 40,
        "Packed": RANDOM,
        "Ansi": b"\x05" + raw_acc.encode() + b"\0",
    }


def expected(acc, alias, vt=0x1E):
    s = streams(alias, raw_acc=acc, vt=vt)
    s["PreviewPNG"] = streams(acc)["PreviewPNG"]
    s["Ansi"] = streams(acc)["Ansi"]
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
    assert res.data == expected(acc, alias)


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
