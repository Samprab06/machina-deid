"""Tests for audit/poppler_audit.py on SYNTHETIC fixtures only (no Poppler needed).

The parser is validated against synthetic Poppler -bbox XML; the audit rubric is
validated against a hand-built corpus where every expected count is known.
Real Poppler runs happen on the desktop machine (E:) by the orchestrator.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

AUDIT = Path(__file__).parent.parent / "audit" / "poppler_audit.py"
sys.path.insert(0, str(AUDIT.parent))
import poppler_audit as pa  # noqa: E402

# synthetic Poppler -bbox style page: one line of words with bboxes
XML_PAGE = (
    '<?xml version="1.0"?><div class="page" id="page1" bbox="0 0 400 200">'
    '<p><span class="word" bbox="10 10 40 20">DRAWN</span>'
    '<span class="word" bbox="44 10 80 20">Asha</span>'
    '<span class="word" bbox="84 10 110 20">Bhatt</span>'
    '<span class="word" bbox="114 10 140 20">DWG</span>'
    '<span class="word" bbox="144 10 190 20">77-042</span>'
    '</p></div>'
)
XML_REPL = XML_PAGE.replace(">Asha<", ">Zoe<").replace(">Bhatt<", ">Kell<")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _run_audit(tmp_path: Path, args: list[str]):
    return subprocess.run([sys.executable, str(AUDIT)] + args, capture_output=True, text=True)


def test_parse_bbox_xml_words():
    pages = pa.parse_bbox_xml(XML_PAGE)
    assert len(pages) == 1
    assert [w[0] for w in pages[0]] == ["DRAWN", "Asha", "Bhatt", "DWG", "77-042"]
    assert pages[0][1][1:] == (44.0, 10.0, 80.0, 20.0)


def test_parse_bbox_xml_multi_page():
    xml = XML_PAGE + XML_PAGE.replace('id="page1"', 'id="page2"')
    pages = pa.parse_bbox_xml(xml)
    assert len(pages) == 2 and len(pages[1]) == 5


def test_find_occurrences_boundary():
    words = pa.parse_bbox_xml(XML_PAGE)[0]
    occ = pa.find_occurrences(words, "Asha Bhatt")
    assert len(occ) == 1
    i, j, rect = occ[0]
    assert j - i == 2
    assert rect[0] <= 44 and rect[2] >= 110
    assert pa.find_occurrences(words, "Bhattt") == []
    assert pa.find_occurrences(words, "Asha")[0][:2] == (1, 2)


def test_role_field_values():
    words = pa.parse_bbox_xml(XML_PAGE)[0]
    vals = pa.role_field_values(words)
    assert vals == ["Asha Bhatt"]  # DWG number stops the value, not absorbed


def _build_corpus(tmp_path: Path):
    """Two synthetic files. Layout mirrors for bbox-dir mode: <bbox>/{src,a,b}/<rel>.xml.

    source  s1,s2: "DRAWN Asha Bhatt DWG 77-042"
    out-a   s1: region replaced (Zoe Kell); s2 byte-identical -> fallback
    out-b   s1: unchanged content -> residual
    """
    src = tmp_path / "src"; oa = tmp_path / "outa"; ob = tmp_path / "outb"
    for d in (src, oa, ob):
        d.mkdir()
    b1, b2 = b"fake-pdf-1", b"fake-pdf-2"
    (src / "s1.pdf").write_bytes(b1)
    (src / "s2.pdf").write_bytes(b2)
    (oa / "s1.pdf").write_bytes(b"fake-pdf-1-rewritten")
    (oa / "s2.pdf").write_bytes(b2)
    (ob / "s1.pdf").write_bytes(b"fake-pdf-1-residual")
    (ob / "s2.pdf").write_bytes(b"fake-pdf-2-rewritten")
    for k, s1, s2 in (("src", XML_PAGE, XML_PAGE),
                      ("a", XML_REPL, XML_PAGE),
                      ("b", XML_PAGE, XML_REPL)):
        d = tmp_path / "bbox" / k
        d.mkdir(parents=True)
        for rel, xml in (("s1.pdf", s1), ("s2.pdf", s2)):
            (d / (rel + ".xml")).write_text(xml)
    return b1, b2


def test_audit_end_to_end_synthetic(tmp_path):
    b1, b2 = _build_corpus(tmp_path)
    man = tmp_path / "split.json"
    man.write_text(json.dumps([
        {"family": "F1", "path": "s1.pdf", "sha256": _sha(b1), "split": "dev"},
        {"family": "F1", "path": "s2.pdf", "sha256": _sha(b2), "split": "dev"},
    ]))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"P1": ["Asha Bhatt"]}))
    r = _run_audit(tmp_path, [
        "audit", "--source", str(tmp_path / "src"), "--manifest", str(man),
        "--labels", str(labels),
        "--out-a", str(tmp_path / "outa"), "--out-b", str(tmp_path / "outb"),
        "--bbox-dir", str(tmp_path / "bbox"),
    ])
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    dev = rep["splits"]["dev"]
    assert dev["source"]["occ_total"] == 2
    assert dev["source"]["repeated_value_groups"] == 1
    # out-a: s1 region replaced -> removed + visible; s2 byte-identical -> fallback
    assert dev["out-a"]["fallback"] == 1
    assert dev["out-a"]["files"] == 2
    assert dev["out-a"]["occ_total"] == 1
    assert dev["out-a"]["occ_residual"] == 0
    assert dev["out-a"]["removal_rate"] == 1.0
    assert dev["out-a"]["region_visible_rate"] == 1.0
    assert dev["out-a"]["verified_alias_values"] == 1
    # out-b: s1 residual (unchanged content), s2 replaced
    assert dev["out-b"]["occ_total"] == 2
    assert dev["out-b"]["occ_residual"] == 1
    assert dev["out-b"]["removal_rate"] == 0.5
    assert dev["out-b"]["region_visible_rate"] == 0.5
    # aggregate-only: no source text, aliases, or paths leak
    for secret in ("Asha", "Bhatt", "Zoe", "s1.pdf", "fake-pdf"):
        assert secret not in r.stdout, secret


def test_inventory_mode(tmp_path):
    b1, b2 = _build_corpus(tmp_path)
    man = tmp_path / "split.json"
    man.write_text(json.dumps([
        {"family": "F1", "path": "s1.pdf", "sha256": _sha(b1), "split": "dev"},
        {"family": "F2", "path": "gone.pdf", "sha256": "0" * 64, "split": "dev"},
    ]))
    r = _run_audit(tmp_path, ["inventory", "--source", str(tmp_path / "src"),
                              "--manifest", str(man)])
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep["mode"] == "inventory"
    assert rep["manifest_rows"] == 2
    assert rep["missing_files"] == 1
    assert rep["sha_mismatch"] == 0
    assert rep["family_counts"] == {"F1": 1, "F2": 1}


def test_sample_mode_limit(tmp_path):
    b1, b2 = _build_corpus(tmp_path)
    man = tmp_path / "split.json"
    man.write_text(json.dumps([
        {"family": "F1", "path": "s1.pdf", "sha256": _sha(b1), "split": "dev"},
        {"family": "F1", "path": "s2.pdf", "sha256": _sha(b2), "split": "dev"},
    ]))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"P1": ["Asha Bhatt"]}))
    r = _run_audit(tmp_path, [
        "sample", "--source", str(tmp_path / "src"), "--manifest", str(man),
        "--labels", str(labels), "--bbox-dir", str(tmp_path / "bbox"), "--limit", "1",
    ])
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep["mode"] == "sample"
    assert rep["splits"]["dev"]["source"]["occ_total"] == 1


def test_cli_help():
    r = _run_audit(Path("/tmp"), ["audit", "--help"])
    assert r.returncode == 0
    for m in ("--poppler", "--bbox-dir", "--source", "--manifest", "--labels",
              "--out-a", "--out-b", "--limit"):
        assert m in r.stdout, m
