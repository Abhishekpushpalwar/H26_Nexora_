## exitcapsule (executor, comparator, capsules)

```
python -m exitcapsule run <repo> --plans plans --usage usage_out/usage.json --out capsules
python -m exitcapsule selftest        # offline demo on a synthetic repo (no network)
```
One command produces `capsules/<dep>.json`, `capsules/<dep>.md`, `capsules/index.md|json` and `capsules/baseline.json`.
A capsule is written as soon as its dependency finishes; one dependency failing never stops the loop.

**Baseline**: venv on Python 3.12 (else 3.13) from every `requirements*.txt` (or `pip install .`). An install failure is
recorded verbatim (last 15 lines of pip's error, plus a per-pin diagnostic) and is not repaired. Tests then do not run
unless `--run-anyway`. **Candidate** (per dependency, fresh copy of the repo + fresh venv): apply C's patch
(`git apply`, else `patch`), drop the swapped pins from the requirements copy, install the rest, install the
replacement, run `pytest --junitxml -rA` on the repo tests plus C's characterization tests. The characterization tests also
run on the baseline so both sides ran the same tests. `status.html` (from `--status-cmd`) is normalized
(timestamps, dates, latency) and diffed.

**Outcomes** (all observed results, none are exceptions): `regressions`, `pass`, `pass_weak_evidence`
(no regressions but B's coverage join shows usage sites the baseline never executed, or no usage data), `patch_failed`,
`install_failed`, `timeout`, `tests_not_run`, `baseline_unavailable` (baseline did not install; candidate reported alone), `no_plan`.
**Gaps** are only: a test that passed before and fails or vanishes after (with error line and traceback `file:line`), and
a normalized status.html difference.

**Capsule layout**: JSON keys `observed`, `usage_evidence`, `gaps`, `ai`, `open_concerns`. In Markdown the OBSERVED and
AI-GENERATED sections are separate headings; AI text is block-quoted and labelled unverified. `open_concerns` is auto-filled from
failures, skipped tests, uncovered/unasserted sites, Gemma validation failures (`plan.ai.validation_failures`) and `--live`.

**Input from the other roles** (`plans/<dep>.json`, paths relative to the plans dir):
```json
{"dependency": "toml", "replacement": "tomllib", "kind": "replace|upgrade|remove",
 "patch": "toml.patch",                      // or "patch_text": "<unified diff, -p1>"
 "swap": {"remove": ["toml"], "install": ["tomli==2.0.1"], "uninstall_after": []},
 "characterization_tests": "toml_char/",     // dir or file, copied to exitcapsule_char/<dep>/
 "ai": {"model": "gemma", "summary": "...", "validation_failures": ["..."]}}
```
`usage.json` is the file written by `usage.py`. If the frozen team schema uses other key names, only `load_plans` and
`capsule.build_capsule` need a mapping.

**Limits**: POSIX only; every candidate reinstalls the pins (use `--pip-args` with a wheelhouse to speed up); the
per-dependency `--timeout` covers install + tests (the baseline run of the characterization tests has its own limit);
status.html comparison needs `--status-cmd` or a `status.html` written by the tests.
