import hashlib
import json

import pytest

from machina_deid.step_analysis import AnalysisLimits, StepAnalysisError, analyze_step

SALT = b"s" * 32
SECRETS = ["Jane Quincy Doe", "Doe", "Jane", "Acme Widgets Inc", "ACME-01",
           "WIDGET-7781;#12=", "Bracket #99 v2", "CONFIDENTIAL", "jdoe@acme.example",
           "O'Brien", "Hidden Comment Name"]

STEP = """ISO-10303-21;
HEADER;
/* header comment with DATA; inside */
FILE_NAME('DATA; fake','2026',('Hidden Comment Name'),(''),'','','');
ENDSEC;
DATA;
/* #500 = PERSON('Hidden Comment Name'); */
#1 = PERSON('jdoe@acme.example','Doe','Jane',
   ('Quincy'),$,$);
#2 = ORGANIZATION('ACME-01','Acme Widgets Inc',
  'O''Brien');
#3 = PERSON_AND_ORGANIZATION(#1,#2);
#4 = PRODUCT('WIDGET-7781;#12=','Bracket #99 v2','',(#5));
#5 = PRODUCT_CONTEXT('',#6,'mechanical');
#6 = APPLICATION_CONTEXT('automotive design');
#7 = PRODUCT_DEFINITION_FORMATION('1','',#4);
#8 = PRODUCT_DEFINITION('design','',#7,#9);
#9 = PRODUCT_DEFINITION_CONTEXT('part definition',#6,'design');
#10 = SECURITY_CLASSIFICATION('CONFIDENTIAL','',#11);
#11 = SECURITY_CLASSIFICATION_LEVEL('confidential');
#12 = CARTESIAN_POINT('',(0.,1.E-3,-2.5));
#13 = ( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );
#14 = UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#13,'distance_accuracy_value','x');
ENDSEC;
END-ISO-10303-21;
"""


def run(text=STEP, **kw):
    return analyze_step(text.encode("latin-1"), salt=SALT, **kw)


def node(rep, eid):
    return next(n for n in rep["nodes"] if n["id"] == eid)


def test_graph_and_counts():
    rep = run()
    assert rep["status"] == "ok", rep["issues"]
    assert rep["entity_count"] == 14
    assert rep["source"]["sha256"] == hashlib.sha256(STEP.encode()).hexdigest()
    assert rep["source"]["size"] == len(STEP)
    c = rep["counts_by_type"]
    assert c["PERSON"] == 1 and c["PRODUCT"] == 1 and c["LENGTH_UNIT+NAMED_UNIT+SI_UNIT"] == 1
    assert 500 not in {n["id"] for n in rep["nodes"]}  # commented-out entity ignored
    edges = {(a, b) for a, b, _ in rep["edges"]}
    assert edges == {(3, 1), (3, 2), (4, 5), (5, 6), (7, 4), (8, 7), (8, 9), (9, 6),
                     (10, 11), (14, 13)}
    assert node(rep, 3)["refs"] == [1, 2] and node(rep, 3)["strings"] == []
    assert rep["dangling_ref_count"] == 0
    # '#12=' and '#99' inside strings are not references
    assert node(rep, 4)["refs"] == [5]
    assert [e[2] for e in rep["edges"] if e[0] == 4] == ["3.0"]


def test_string_fields_and_classification():
    rep = run()
    p = node(rep, 1)
    assert [f["index"] for f in p["strings"]] == ["0", "1", "2", "3.0"]
    assert [f["length"] for f in p["strings"]] == [17, 3, 4, 6]
    assert p["strings"][0]["class"] == "identity_candidate"
    org = node(rep, 2)
    assert org["strings"][2]["length"] == len("O'Brien")  # doubled quote decoded
    prod = node(rep, 4)["strings"]
    assert prod[0]["length"] == len("WIDGET-7781;#12=") and prod[0]["class"] == "identity_candidate"
    assert node(rep, 12)["strings"][0]["class"] == "engineering"
    assert node(rep, 10)["strings"][0]["class"] == "identity_candidate"
    # identical values hash identically, salt-dependent
    assert node(rep, 5)["strings"][0]["hash"] == node(rep, 4)["strings"][2]["hash"]
    other = analyze_step(STEP.encode(), salt=b"t" * 32)
    assert node(other, 1)["strings"][1]["hash"] != p["strings"][1]["hash"]
    assert "precision" in rep["classification_note"]


def test_no_raw_values_in_report():
    blob = json.dumps(run())
    for s in SECRETS + ["Quincy", "automotive design"]:
        assert s not in blob
        assert s.replace("'", "''") not in blob


def test_limits_report_truncation():
    rep = run(limits=AnalysisLimits(max_entities=3))
    assert rep["status"] == "truncated" and rep["truncation"]["entities"]
    assert rep["entity_count"] == 3
    rep = run(limits=AnalysisLimits(max_string_len=4))
    assert rep["truncation"]["strings"] > 0
    f = node(rep, 2)["strings"][1]
    assert f["truncated"] and f["length"] == len("Acme Widgets Inc")
    rep = run(limits=AnalysisLimits(max_bytes=STEP.index("#8 =")))
    assert rep["truncation"]["bytes"] and rep["status"] == "truncated"
    assert rep["source"]["size"] == len(STEP)
    rep = run(limits=AnalysisLimits(max_nodes=2, max_edges=1))
    assert len(rep["nodes"]) == 2 and len(rep["edges"]) == 1
    assert rep["truncation"]["nodes"] and rep["truncation"]["edges"]


def test_errors_and_salt():
    assert run("ISO-10303-21;\nHEADER;\nENDSEC;\n")["status"] == "error"
    rep = run("DATA;\n#1 = PERSON('unterminated);\n")
    assert rep["status"] == "error" and "unterminated_string" in rep["issues"]
    rep = run("DATA;\n#1 = PERSON('a',#7);\nENDSEC;")
    assert rep["dangling_refs"] == [7]
    with pytest.raises(StepAnalysisError):
        analyze_step(b"DATA;", salt=b"short")
    assert analyze_step(STEP.encode())["hashing"]["salt_mode"] == "ephemeral"


def test_does_not_mutate_input():
    data = bytearray(STEP.encode())
    before = bytes(data)
    analyze_step(bytes(data), salt=SALT)
    assert bytes(data) == before
