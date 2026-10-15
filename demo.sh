#!/usr/bin/env bash
# Demo for the final run. Usage:  ./demo.sh <path-to-linkwatch-repo>   (or ./demo.sh selftest)
set -u
if [ "${1:-}" = "selftest" ]; then
  exec python3 -m exitcapsule selftest --out selftest_capsules      # offline, synthetic
fi
REPO="${1:?usage: ./demo.sh <repo>}"
# 1. Person B's usage analysis (gives uncovered/unasserted sites -> weak-evidence flags)
python3 usage.py --root "$REPO" --out-dir usage_out || true
# 2. Baseline + every dependency swap -> capsules/<dep>.json|md and capsules/index.md
python3 -m exitcapsule run "$REPO" --plans plans --usage usage_out/usage.json --out capsules \
  --status-cmd '{python} -m linkwatch.status {out}' --timeout 900
echo; echo "open capsules/index.md"
