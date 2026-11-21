"""Attempt-2 synthetic benchmark (no real data). Builds representative STEP/ZIP fixtures modelled on common
exporter layouts, runs discovery + pass-2 transform, and independently counts approved values left in
outputs (raw byte scan of every output, utf-8). Usable against any checkout via PYTHONPATH.

    PYTHONPATH=src python scripts/attempt2_benchmark.py WORKDIR
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

if len(sys.argv) > 2:  # optional alternate source tree, e.g. a baseline `git archive` export
    sys.path.insert(0, sys.argv[2])
from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import IdentityCatalog  # noqa: E402

PEOPLE = ["Jane Quincy Doe", "Rafael Ortiz", "Mei Lin Zhao", "Tom Baker"]
ORGS = ["Acme Widgets Inc", "Northwind Tooling GmbH", "Blue Fjord AS"]


def header(name, author, org):
    return (f"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('CAD model'),'2;1');\n"
            f"FILE_NAME('{name}','2024-01-01T00:00:00',('{author}'),('{org}'),'PP v1','CAD X','');\n"
            "FILE_SCHEMA(('AP214'));\nENDSEC;\n")


def data(person, org, variant):
    last, first = person.split(" ")[-1], " ".join(person.split(" ")[:-1])
    pid = person if variant % 2 else last.lower()
    return ("DATA;\n"
            "#1=CARTESIAN_POINT('',(0.,1.5,-2.25E-3));\n"
            "#2=PRODUCT('P-%d','Bracket %d','',(#3));\n#3=PRODUCT_CONTEXT('',#4,'mechanical');\n"
            "#4=APPLICATION_CONTEXT('design');\n"
            f"#10=ORGANIZATION('{org if variant % 3 == 0 else '$'}','{org if variant % 4 else person}','');\n"
            f"#11=PERSON('{pid}','{last}','{first}',$,$,$);\n"
            "#13=PERSON_AND_ORGANIZATION(#11,#10);\n"
            "#15=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#16);\n"
            "#16=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\nENDSEC;\nEND-ISO-10303-21;\n"
            ).replace("%d", str(variant)).replace("'$'", "$")


def step(i):
    p, o = PEOPLE[i % len(PEOPLE)], ORGS[i % len(ORGS)]
    return (header(f"part{i}", p, o) + data(p, o, i)).encode("latin-1")


def zbytes(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return b.getvalue()


def build(root: Path):
    files = []
    for i in range(24):
        p = root / f"s{i}.stp"
        p.write_bytes(step(i))
        files.append(p)
    for i in range(6):
        p = root / f"z{i}.zip"
        inner = zbytes({f"n/{j}.step": step(100 + i * 3 + j) for j in range(2)})
        p.write_bytes(zbytes({f"m{j}.stp": step(50 + i * 3 + j) for j in range(2)} | {"inner.zip": inner}))
        files.append(p)
    return files


def leftover(path: Path, values) -> int:
    def walk(b: bytes):
        if b[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(b)) as z:
                return sum(walk(z.read(n)) for n in z.namelist())
        return sum(b.count(v.encode()) for v in values)
    return walk(path.read_bytes())


def main(work: Path):
    src, out = work / "src", work / "out"
    src.mkdir(parents=True), out.mkdir()
    files = build(src)
    cat = IdentityCatalog(work / "c.db", b"benchmark-secret-0123456789abcdef")
    tp.discover_batch(cat, [(f, f.name) for f in files], limit=5000)
    values = PEOPLE + ORGS
    before = sum(leftover(f, values) for f in files)
    res = {"files": len(files), "transformed": 0, "fields_replaced": 0, "approved_occurrences_before": before,
           "approved_occurrences_after": 0, "reported_residual_files": 0, "fully_clean": 0}
    for f in files:
        o = tp.transform_file(cat, f, out / f.name, f.name)
        dst = out / f.name if o.output_written else f
        res["approved_occurrences_after"] += leftover(dst, values)
        res["transformed"] += o.status == "transformed"
        res["fields_replaced"] += o.fields_replaced
        res["reported_residual_files"] += o.residual_approved > 0
        res["fully_clean"] += o.fully_clean
    cat.close()
    print(json.dumps(res, sort_keys=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
