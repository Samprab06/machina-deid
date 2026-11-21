#!/bin/sh
# Run RCAC frozen baseline tests against a candidate checkout using the exact
# import overrides from shared/PDF-DEADLINE.md.
# usage: run_candidate.sh <candidate_dir> <pytest_python>
set -eu
cand="$1"
py="$2"
shift 2
rcac="/home/opc/machina-orchestration/attempt-3/rcac"
cd "$cand"
PYTHONPATH="$cand/src:$cand/tests:$rcac/scripts" "$py" -m pytest \
  -o addopts='' \
  -o "pythonpath=$cand/src $cand/tests" \
  --import-mode=importlib -q -p no:cacheprovider \
  "$rcac/tests/test_attempt3_baseline.py" ${1+"$@"}
