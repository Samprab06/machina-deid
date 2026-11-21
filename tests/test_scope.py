"""Attempt 2c production shared PDF/OLE identity scope: end-to-end smoke on a synthetic workspace."""
import hashlib
import sqlite3

import pymupdf
import pytest
from ole_helpers import cfb, cstring, summary_info
from pdf_fixtures import make_corpus
from production_fixtures import make_workspace, zip_bytes

from machina_deid import ole_transform as ot
from machina_deid import production as pr
from machina_deid import scope as sc
from machina_deid import twopass

ACCOUNT = "jqdoe7"
PREVIEW = b"\x89PNG preview bytes " + "C:\\Users\\jqdoe7\\x".encode("utf-16-le")


def ole_bytes(account=ACCOUNT, extra=b""):
    body = (b"\x00" * 16 + cstring(account) + b"\x00" * 8 + f"C:\\Users\\{account}\\Parts\\a.sldprt".encode("utf-16-le")
            + b"\x00\x00" + extra)
    return cfb({"\x05SummaryInformation": summary_info({8: (0x1E, account)}), "Contents": body,
                "PreviewPNG": PREVIEW})


def corpus(tmp_path):
    src = tmp_path / "gen"
    make_corpus(src)
    files = {p.relative_to(src).as_posix(): p.read_bytes() for p in sorted(src.rglob("*.pdf"))}
    files["cad/part1.sldprt"] = ole_bytes()
    files["cad/part2.sldprt"] = ole_bytes(extra=b"\x01" * 40)
    files["cad/other.sldprt"] = cfb({"Contents": b"\x00" * 64})  # no SummaryInformation: passthrough
    files["bundle/pack.zip"] = zip_bytes([("drw/drw-002.pdf", files["famA/drw-002.pdf"]),
                                          ("cad/asm.sldasm", ole_bytes()), ("readme.txt", b"plain notes")])
    textless = pymupdf.open()
    textless.new_page(width=300, height=200).draw_line((10, 10), (200, 150))
    files["famC/vector-only.pdf"] = textless.tobytes()
    return files


def cfg(root, head, n, **kw):
    base = dict(root=root, attempt="attempt-1", expected_rows=n, expected_revision=head, workers=2, reserve_bytes=0,
                poll_seconds=0.05, batch_files=4, heavy_slots=1)
    base.update(kw)
    return pr.Config(**base)


def pdf_text(data: bytes) -> str:
    d = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\n".join(p.get_text() for p in d) + str(d.metadata)
    finally:
        d.close()


def props(data: bytes) -> list[str]:
    return [p.value for p in ot.summary_props(ot.parse(data))]


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    root = tmp_path_factory.mktemp("scope")
    files = corpus(root)
    head = make_workspace(root, files)
    out = pr.run(cfg(root, head, len(files)))
    return root, head, files, out


def test_end_to_end_pdf_ole_zip(produced):
    root, head, files, out = produced
    assert out["phase"] == "packaged"
    corp = root / "attempts/attempt-1/corpus"
    led = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    status = dict(led.execute("SELECT path, status FROM output"))
    assert pr.meta_get(led, pr.SCOPE_KEY) not in (None, pr.SCOPE_OFF)
    # PDFs: labeled + cross-document unlabeled mentions are gone; engineering text stays
    for rel in ("famA/drw-001.pdf", "famA/drw-002.pdf", "famB/sheet-3.pdf"):
        assert status[rel] == "transformed", rel
        t = pdf_text((corp / rel).read_bytes())
        for v in ("Bob Builder", "Carla Tester", "Jane Q. Doe", "Acme Widgets Ltd"):
            assert v not in t, (rel, v)
        assert ("SCALE 1:2" in t or rel == "famB/sheet-3.pdf") and "120" in t
    # OLE: one account alias everywhere (top level and ZIP member), preview untouched, size/tree identical
    a1, a2 = props((corp / "cad/part1.sldprt").read_bytes()), props((corp / "cad/part2.sldprt").read_bytes())
    assert status["cad/part1.sldprt"] == "transformed" and a1 == a2 and a1[0] != ACCOUNT and len(a1[0]) == len(ACCOUNT)
    o = (corp / "cad/part1.sldprt").read_bytes()
    assert len(o) == len(files["cad/part1.sldprt"]) and cstring(ACCOUNT) not in o
    cf = ot.parse(o)
    prev = next(e for e in cf.streams() if e.path.endswith("PreviewPNG"))
    assert cf.read(prev) == PREVIEW
    assert status["cad/other.sldprt"] == "unsupported_passthrough" or \
        (corp / "cad/other.sldprt").read_bytes() == files["cad/other.sldprt"]
    import io
    import zipfile
    with zipfile.ZipFile(corp / "bundle/pack.zip") as z:
        assert props(z.read("cad/asm.sldasm")) == a1
        assert "Jane Q. Doe" not in pdf_text(z.read("drw/drw-002.pdf"))
        assert z.read("readme.txt") == b"plain notes"
    # textless vector PDF: untouched
    assert (corp / "famC/vector-only.pdf").read_bytes() == files["famC/vector-only.pdf"]


def test_scope_mode_pinned_on_resume(produced):
    root, head, files, _ = produced
    with pytest.raises(pr.RunnerError):
        pr.run(cfg(root, head, len(files), scope=False))


def test_build_deterministic_across_workers(tmp_path):
    files = corpus(tmp_path)
    for rel, data in files.items():
        (tmp_path / "in" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "in" / rel).write_bytes(data)
    items = [(tmp_path / "in" / r, r) for r in files]
    secret = b"s" * 32
    s1 = sc.build_scope(tmp_path / "m1.sqlite", secret, items, workers=1)
    s2 = sc.build_scope(tmp_path / "m2.sqlite", secret, items, workers=2)
    assert s1["digest"] == s2["digest"] and s1["ole_equal_length_pseudonym"] >= 1
    raw = (tmp_path / "m1.sqlite").read_bytes()
    for v in ("Bob Builder", ACCOUNT):  # no raw values in the frozen map
        assert v.encode() not in raw and v.encode("utf-16-le") not in raw


def test_fail_closed_pdf_and_unresolved_ole(tmp_path, monkeypatch):
    files = corpus(tmp_path)
    for rel, data in files.items():
        (tmp_path / "in" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "in" / rel).write_bytes(data)
    secret = b"k" * 32
    st = sc.build_scope(tmp_path / "m.sqlite", secret, [(tmp_path / "in" / r, r) for r in files])
    scope = sc.Scope(tmp_path / "m.sqlite", secret, st["digest"])

    class Cat:  # no catalog approvals
        def approved_pseudonym(self, v):
            return None

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(sc.Scope, "pdf", boom)
    r = twopass.transform_file(Cat(), tmp_path / "in/famA/drw-001.pdf", tmp_path / "o.pdf", scope=scope)
    assert r.status == "failed" and r.reason.startswith("pdf_scoped_exception") and not (tmp_path / "o.pdf").exists()
    # an OLE account never seen in pass 1 is not given a per-file alias: failed, original kept
    (tmp_path / "new.sldprt").write_bytes(ole_bytes("zz9unseen"))
    r = twopass.transform_file(Cat(), tmp_path / "new.sldprt", tmp_path / "o.sldprt", scope=scope)
    assert r.status == "failed" and "not_in_frozen_map" in r.reason and not (tmp_path / "o.sldprt").exists()


def test_scope_map_unchanged_by_run(produced):
    root, *_ = produced
    p = root / "ops/attempt-1" / sc.MAP_NAME
    d = hashlib.sha256(p.read_bytes()).hexdigest()
    led = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    digest = pr.meta_get(led, pr.SCOPE_KEY)
    sc.Scope(p, (root / "ops/attempt-1/seed").read_bytes(), digest)  # verifies frozen digest
    assert hashlib.sha256(p.read_bytes()).hexdigest() == d


def test_reproducible_package_and_provenance(produced, tmp_path):
    import json
    root, head, files, out = produced
    p1 = json.loads((root / "attempts/attempt-1/code/PROVENANCE.json").read_text())
    assert p1["entity_map_digest"] and p1["source_archive_sha256"] and p1["outputs"] == len(files)
    again = pr.run(cfg(root, head, len(files)))  # complete attempt: re-package only
    assert again["provenance_sha256"] == out["provenance_sha256"]
    # an independent workspace with identical inputs/seed reproduces every output digest and the map digest
    head2 = make_workspace(tmp_path, files)
    pr.run(cfg(tmp_path, head2, len(files)))
    p2 = json.loads((tmp_path / "attempts/attempt-1/code/PROVENANCE.json").read_text())
    for k in ("entity_map_digest", "catalog_digest", "output_manifest_sha256", "outcomes"):
        assert p1[k] == p2[k], k


def test_frozen_person_alias_shares_width_constrained_ole_token(tmp_path):
    from machina_deid.pdf_entities import EntityMapBuilder, Resolver
    from machina_deid.scope import OLE_SOURCE, Obs, ole_alias_key

    secret = b"test-root-allocation-key"
    builder = EntityMapBuilder(secret, ole_alias_key=ole_alias_key(secret))
    builder.add_document("family/full.pdf", [Obs("person", "Jane Doe", "label_right")])
    builder.add_document("family/short.pdf", [Obs("person", "J. Doe", "label_right")])
    builder.add_document("family/doc.slddrw", [Obs("account", "Jane Doe", OLE_SOURCE)])
    path = tmp_path / "map.sqlite"
    builder.freeze(path)
    resolver = Resolver(secret, path)
    try:
        full, short = resolver.entry("Jane Doe"), resolver.entry("J. Doe")
        assert full.status == short.status == "ok"
        assert full.pseudonym == short.pseudonym == full.fitted == short.fitted
        assert len(full.pseudonym) == len("Jane Doe")
    finally:
        resolver.close()


def test_frozen_map_refuses_colliding_tokens_for_distinct_roots(tmp_path):
    from machina_deid.pdf_entities import EntityMapBuilder, Resolver
    from machina_deid.scope import Obs

    class Catalog:
        def approved_pseudonym(self, _value):
            return "person", "Shared Synthetic Alias"

    secret = b"test-token-collision-key"
    builder = EntityMapBuilder(secret, Catalog())
    builder.add_document("f/first.pdf", [Obs("person", "Jane Doe", "label_right")])
    builder.add_document("f/second.pdf", [Obs("person", "Bob Builder", "label_right")])
    path = tmp_path / "map.sqlite"
    assert builder.freeze(path)["pseudonym_shared_across_roots"] > 0
    resolver = Resolver(secret, path)
    try:
        assert resolver.resolve("person", "Jane Doe")[0] is None
        assert resolver.resolve("person", "Bob Builder")[0] is None
    finally:
        resolver.close()
