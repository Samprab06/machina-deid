"""Attempt-2b synthetic benchmark (no real data): the attempt-1 one-hit STEP patterns + SVG/STL metadata.
Prints redacted counts only. Usable against any checkout via an alternate source tree:

    uv run python scripts/attempt2b_benchmark.py WORKDIR [ALT_SRC]
"""
from __future__ import annotations

import json
import struct
import sys
import time
from collections import Counter
from pathlib import Path

if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from step_helpers import step_bytes  # noqa: E402

from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import IdentityCatalog  # noqa: E402

PEOPLE = ["Jane Quincy Doe", "Rafael Ortiz", "Mei Lin Zhao", "Tom Baker"]
ORGS = ["Acme Widgets Inc", "Northwind Tooling GmbH", "Blue Fjord AS"]


def data(person, org, k):
    return (f"DATA;\n#1=CARTESIAN_POINT('',(0.,1.5,-2.25E-3));\n"
            f"#2=PRODUCT('{person.split()[-1].upper()}-{k}','Bracket {k} by {person}','{org} part',(#3));\n"
            f"#5=PRODUCT_DEFINITION('design','',#6,#7);\n"
            f"#10=ORGANIZATION('','{org}','');\n"
            "#90=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#91);\n"
            "#91=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\nENDSEC;\nEND-ISO-10303-21;\n").encode()


def corpus():
    files = {}
    for k in range(24):
        p, o = PEOPLE[k % 4], ORGS[k % 3]
        plain = data(p, o, k).replace(f" by {p}".encode(), b"").replace(f"'{o} part'".encode(), b"''")
        variant = k % 3
        files[f"s{k}.step"] = step_bytes(
            name=f"part {k}", author=f"('{p}')", org=f"('{o}')",
            preproc=f"{o} exporter v2" if variant == 0 else "ST-DEVELOPER v16.7",  # one-hit pattern A
            data=data(p, o, k) if variant == 1 else plain)                       # PRODUCT descriptive
    tri = struct.pack("<12fH", *([0.0, 0.0, 1.0] + [1.5, 2.25, -3.0] * 3), 0)
    for k in range(6):
        files[f"m{k}.stl"] = f"Exported by {PEOPLE[k % 4]}".encode().ljust(80, b"\0") + struct.pack("<I", 1) + tri
        files[f"d{k}.svg"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#" '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><metadata><rdf:RDF><cc:Work>'
            f"<dc:creator><cc:Agent><dc:title>{PEOPLE[k % 4]}</dc:title></cc:Agent></dc:creator>"
            "</cc:Work></rdf:RDF></metadata><path d=\"M0,0 L1,1\"/></svg>\n").encode()
    return files


def main(work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=False)
    (work / "in").mkdir()
    files = corpus()
    for n, b in files.items():
        (work / "in" / n).write_bytes(b)
    cat = IdentityCatalog(work / "c.db", b"attempt2b-bench-secret-012345678")
    tp.discover_batch(cat, [(work / "in" / n, n) for n in files if n.endswith(".step")], limit=100)
    values = [v.encode() for v in PEOPLE + ORGS]
    by = {}
    t0 = time.perf_counter()
    for n in files:
        ext = n.rsplit(".", 1)[1]
        c = by.setdefault(ext, Counter())
        o = tp.transform_file(cat, work / "in" / n, work / "out" / n, n)
        out = work / "out" / n
        raw = out.read_bytes() if out.exists() else files[n]
        c["files"] += 1
        c["approved_occurrences_before"] += sum(files[n].count(v) for v in values)
        c["approved_occurrences_after"] += sum(raw.count(v) for v in values)
        c["transformed"] += int(o.status == "transformed")
        c["reported_residual_files"] += int(o.residual_approved > 0)
        c["fully_clean"] += int(o.fully_clean)
        c["fields_replaced"] += o.fields_replaced
        c["source_preserved"] += int(o.source_unchanged)
    return {"seconds": round(time.perf_counter() - t0, 3), **{k: dict(sorted(v.items())) for k, v in sorted(by.items())}}


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), sort_keys=True))
