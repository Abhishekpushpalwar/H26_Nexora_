"""orchestrator.py - baseline run + per-dependency candidate runs; never aborts on one failure."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import __version__, capsule, runner
from .runner import Budget, Context, norm_name, safe_name

PRIORITY = ["toml", "appdirs", "chardet", "python-dateutil", "setuptools", "pkg-resources",   # easy
            "jinja2", "six", "nose", "idna",                                                 # medium
            "requests", "pydantic"]                                                          # hard


def log(*a):
    print("[exitcapsule]", *a, file=sys.stderr, flush=True)


def slim(step, keep=700):
    s = dict(step)
    for k in ("stdout_tail", "stderr_tail"):
        v = s.get(k) or ""
        s[k] = v if len(v) <= keep else "..." + v[-keep:]
    return s


def slim_install(inst):
    return {**inst, "steps": [slim(s) for s in inst["steps"]]}


# ------------------------------------------------------------------ plans
def load_plans(plans_dir, work: Path) -> dict:
    """plans/<dep>.json from the migration step. Relative paths resolve against the plans dir."""
    plans = {}
    if not plans_dir or not Path(plans_dir).is_dir():
        return plans
    base = Path(plans_dir).resolve()
    for f in sorted(base.glob("*.json")):
        try:
            p = json.loads(f.read_text())
        except Exception as e:
            log(f"skipping unreadable plan {f.name}: {e}")
            continue
        dep = p.get("dependency") or f.stem
        p["dependency"] = dep
        if p.get("patch_text"):
            pf = work / "patches" / f"{safe_name(dep)}.patch"
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(p["patch_text"])
            p["patch_path"] = str(pf)
        elif p.get("patch"):
            p["patch_path"] = str((base / p["patch"]).resolve())
        if p.get("characterization_tests"):
            p["characterization_tests_path"] = str((base / p["characterization_tests"]).resolve())
        plans[dep] = p
    return plans


def order_deps(deps):
    def key(d):
        n = norm_name(d)
        return (PRIORITY.index(n) if n in PRIORITY else len(PRIORITY), n)
    return sorted(deps, key=key)


# ------------------------------------------------------------------ baseline
def run_baseline(ctx: Context, pyinfo: dict) -> dict:
    work = ctx.work / "baseline"
    repo, venv = work / "repo", work / "venv"
    work.mkdir(parents=True, exist_ok=True)
    budget = Budget(ctx.baseline_timeout)
    b = {"python_path": pyinfo["path"], "python_version": pyinfo["version"], "python_supported": pyinfo["supported"],
         "python_note": pyinfo.get("note"), "install_ok": False, "install_failure": None, "tests_ran": False,
         "test_counts": None, "usable": False, "repo": repo, "py": None, "results": None, "status_html": None,
         "steps": []}
    runner.copy_repo(ctx.repo, repo)
    v = runner.create_venv(pyinfo["path"], venv, budget)
    b["steps"].append(slim(v))
    if not v["ok"]:
        b["install_failure"] = runner.first_failure([v])
        return b
    py = runner.venv_python(venv)
    b["py"] = py
    inst = runner.install_pins(py, repo, ctx, budget)
    b["install"] = slim_install(inst)
    b["install_ok"] = inst["ok"]
    b["install_failure"] = inst["exact_failure"]
    if not inst["ok"]:
        log("baseline pins FAILED to install:", (inst["exact_failure"] or {}).get("message", "").splitlines()[-1:] )
    if not (inst["ok"] or ctx.run_anyway):
        return b
    tool = runner.ensure_pytest(py, repo, ctx, budget)
    b["pytest_tool"] = {**tool, "steps": [slim(s) for s in tool["steps"]]}
    if not tool["ok"]:
        return b
    b["usable"] = True
    rec, results = runner.run_tests(py, repo, ctx.test_paths, ctx, budget, "baseline")
    b["tests"] = slim(rec)
    b["results"] = results
    b["tests_ran"] = results is not None
    b["test_counts"] = rec["counts"]
    b["status_html"], b["status_info"] = runner.collect_status_html(py, repo, ctx, budget)
    return b


def baseline_summary(b):
    keys = ("python_version", "python_supported", "python_note", "install_ok", "install_failure", "tests_ran",
            "test_counts", "steps", "install", "pytest_tool", "tests", "status_info")
    s = {k: b.get(k) for k in keys if k in b}
    s["results"] = {t: r["status"] for t, r in (b.get("results") or {}).items()}
    return s


# ------------------------------------------------------------------ one dependency
def _timeout_hit(steps):
    for s in steps:
        if s.get("timed_out"):
            return s.get("step")
    return None


def run_dependency(ctx: Context, dep: str, plan, baseline: dict) -> dict:
    obs = {"plan_found": plan is not None, "baseline": {k: baseline_summary(baseline).get(k) for k in
           ("python_version", "python_supported", "install_ok", "install_failure", "test_counts")}}
    if plan is None:
        return obs
    t0 = time.time()
    obs.update(timeout_seconds=ctx.dep_timeout, live_run=ctx.live, failure_stage=None)
    work = ctx.work / "cand" / safe_name(dep)
    repo, venv = work / "repo", work / "venv"
    work.mkdir(parents=True, exist_ok=True)
    try:
        runner.copy_repo(ctx.repo, repo)
        char_src = plan.get("characterization_tests_path")
        char_rel = runner.install_char_tests(repo, dep, char_src)
        obs["characterization"] = {"present": bool(char_rel), "path": char_rel, "baseline_counts": None}

        # ---- baseline run of the characterization tests (same tests on both sides)
        char_base = {}
        if char_rel and baseline.get("usable"):
            runner.install_char_tests(baseline["repo"], dep, char_src)
            rec, res = runner.run_tests(baseline["py"], baseline["repo"], [char_rel], ctx, Budget(ctx.dep_timeout),
                                        "baseline_" + safe_name(dep))
            obs["characterization"].update(baseline_counts=rec["counts"], baseline_run=slim(rec))
            char_base = res or {}

        budget = Budget(ctx.dep_timeout)   # starts after the baseline run of the characterization tests, which has its own limit
        # ---- patch
        p = runner.apply_patch(repo, plan.get("patch_path"), budget)
        obs["patch"] = p
        if p["present"] and not p["applied"]:
            obs["failure_stage"] = "timeout" if "budget exhausted" in str(p.get("error")) else "patch"
            return obs

        # ---- swap
        swap = plan.get("swap") or {}
        remove = list(swap.get("remove") or swap.get("uninstall") or [])
        install = list(swap.get("install") or [])
        after = list(swap.get("uninstall_after") or [])
        names = remove + [runner.req_name(s) or "" for s in install]
        removed = runner.drop_requirements(repo, [n for n in names if n])
        obs["swap"] = {"remove": remove, "install": install, "uninstall_after": after, "requirement_lines_removed": removed}

        steps = []
        v = runner.create_venv(ctx.python, venv, budget)
        steps.append(v)
        py = runner.venv_python(venv)
        inst = {"ok": v["ok"], "steps": steps, "diagnostics": [], "requirement_files": []}
        if v["ok"]:
            pins = runner.install_pins(py, repo, ctx, budget)
            inst["steps"] += pins["steps"]
            inst["requirement_files"] = pins["requirement_files"]
            inst["diagnostics"] = pins["diagnostics"]
            inst["ok"] = pins["ok"]
            if pins["ok"]:
                tool = runner.ensure_pytest(py, repo, ctx, budget)
                inst["steps"] += tool["steps"]
                inst["ok"] = tool["ok"]
            if inst["ok"] and install:
                s = runner.pip(py, "install", install, ctx, repo, budget, "pip install " + " ".join(install))
                inst["steps"].append(s)
                inst["ok"] = s["ok"]
            if inst["ok"] and after:
                s = runner.pip(py, "uninstall", ["-y", *after], ctx, repo, budget, "pip uninstall -y " + " ".join(after))
                inst["steps"].append(s)
                inst["ok"] = s["ok"]
        inst["exact_failure"] = runner.first_failure(inst["steps"])
        obs["install"] = slim_install(inst)
        to = _timeout_hit(inst["steps"])
        if to:
            obs.update(failure_stage="timeout", timeout_stage=to)
            return obs
        if not inst["ok"]:
            obs["failure_stage"] = "tooling" if (inst["exact_failure"] or {}).get("step", "").startswith("pip install pytest") else "install"
            return obs

        # ---- tests
        paths = list(ctx.test_paths) + ([char_rel] if char_rel else [])
        rec, cand = runner.run_tests(py, repo, paths, ctx, budget, "candidate")
        obs["tests"] = slim(rec)
        if rec.get("timed_out"):
            obs.update(failure_stage="timeout", timeout_stage=rec["step"])
            return obs
        if cand is None:
            obs["failure_stage"] = "pytest"
            return obs
        status_html, sinfo = runner.collect_status_html(py, repo, ctx, budget)
        if _timeout_hit([sinfo.get("run") or {}]):
            sinfo["note"] = "status generation timed out"

        # ---- compare (same tests on both sides)
        base = None
        if baseline.get("results") is not None:
            base = dict(baseline["results"])
            base.update(char_base)
        obs["comparison"] = runner.compare_tests(base, cand)
        obs["status_html"] = {**runner.diff_status(baseline.get("status_html"), status_html),
                              "candidate_info": sinfo}
        obs["candidate_results"] = {t: r["status"] for t, r in cand.items()}
    finally:
        obs["seconds"] = round(time.time() - t0, 1)
    return obs


# ------------------------------------------------------------------ everything
def orchestrate(ctx: Context, plans: dict, deps: list, usage_path, out_dir: Path, pyinfo: dict, keep_work=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    usages = None
    if usage_path and Path(usage_path).exists():
        try:
            usages = json.loads(Path(usage_path).read_text())["usages"]
        except Exception as e:
            log(f"could not read usage file {usage_path}: {e}")
    log(f"baseline: python {pyinfo['version']} ({'supported' if pyinfo['supported'] else 'NOT 3.12/3.13'})")
    try:
        baseline = run_baseline(ctx, pyinfo)
    except Exception:
        baseline = {"python_version": pyinfo["version"], "python_supported": pyinfo["supported"], "install_ok": False,
                    "install_failure": {"message": traceback.format_exc()[-1500:]}, "usable": False,
                    "results": None, "status_html": None, "test_counts": None}
        log("baseline crashed (recorded):", baseline["install_failure"]["message"].splitlines()[-1])
    bsum = baseline_summary(baseline)
    (out_dir / "baseline.json").write_text(json.dumps(bsum, indent=2, default=str))
    log(f"baseline: install_ok={baseline['install_ok']} tests={baseline.get('test_counts')}")

    meta = {"exitcapsule": __version__, "orchestrator_python": sys.version.split()[0]}
    caps = []
    for dep in deps:
        log(f"--- {dep}")
        plan = plans.get(dep)
        try:
            obs = run_dependency(ctx, dep, plan, baseline)
        except Exception:
            obs = {"plan_found": plan is not None, "error": traceback.format_exc()[-2000:], "failure_stage": None,
                   "baseline": {}}
            log(f"{dep}: exitcapsule error recorded; continuing")
        cap = capsule.build_capsule(dep, plan, obs, capsule.usage_evidence(usages, dep), meta)
        capsule.write_capsule(out_dir, cap)           # written immediately: partial runs keep their results
        caps.append(cap)
        log(f"{dep}: {cap['outcome']} ({cap['evidence_strength']})")
    capsule.write_index(out_dir, caps, {k: v for k, v in bsum.items() if k not in ("results", "steps", "install", "tests", "pytest_tool")})
    if not keep_work:
        shutil.rmtree(ctx.work, ignore_errors=True)
    return caps
