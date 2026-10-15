"""runner.py - executor and comparator (Person D).

Everything here records what happened; nothing is repaired silently. A failed install, a patch
that does not apply, or a timeout is returned as data, never raised.
POSIX only (process groups are used to kill timed-out test runs).
"""
from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TAIL = 3000


def tail(s, n=TAIL):
    s = s or ""
    return s if len(s) <= n else "..." + s[-n:]


def norm_name(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def safe_name(n: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", n)


@dataclass
class Context:
    repo: Path
    work: Path
    python: str = ""                  # interpreter used to create venvs
    test_paths: list = field(default_factory=lambda: ["tests"])
    pytest_args: list = field(default_factory=list)
    pip_args: list = field(default_factory=list)   # e.g. ["--no-index", "--find-links", "wheelhouse"]
    status_cmd: str | None = None     # e.g. "{python} -m linkwatch.status {out}"
    live: bool = False                # status run touches the network (non-deterministic)
    run_anyway: bool = False          # run baseline tests even if pins failed to install
    baseline_timeout: int = 1200
    dep_timeout: int = 900


class Budget:
    def __init__(self, seconds):
        self.deadline = time.time() + seconds if seconds else None

    def remaining(self):
        return float("inf") if self.deadline is None else self.deadline - time.time()


def clean_env():
    e = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}
    e.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PYTHONDONTWRITEBYTECODE="1",
             PYTHONHASHSEED="0")
    return e


def run_cmd(cmd, cwd, budget=None, cap=None, step=""):
    """Run a command with a time budget. Always returns a record dict."""
    timeout = budget.remaining() if budget else float("inf")
    if cap:
        timeout = min(timeout, cap)
    rec = {"step": step, "cmd": [str(c) for c in cmd], "cwd": str(cwd), "stdout_tail": "", "stderr_tail": ""}
    if timeout <= 0:
        rec.update(ok=False, returncode=None, timed_out=True, seconds=0,
                   error="time budget exhausted before this step started")
        return rec
    t0 = time.time()
    try:
        p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=clean_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        rec.update(ok=False, returncode=None, timed_out=False, seconds=0, error=str(e))
        return rec
    try:
        out, err = p.communicate(timeout=None if timeout == float("inf") else timeout)
        rec.update(returncode=p.returncode, timed_out=False, ok=p.returncode == 0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            p.kill()
        out, err = p.communicate()
        rec.update(returncode=None, timed_out=True, ok=False, error=f"timed out after {timeout:.0f}s")
    rec.update(seconds=round(time.time() - t0, 2), stdout_tail=tail(out), stderr_tail=tail(err))
    return rec


# ------------------------------------------------------------------ python / venv
def _py_version(path):
    try:
        r = subprocess.run([path, "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def find_python(preferred=None) -> dict:
    """Pick Python 3.12 or 3.13 for the venvs; record what was found."""
    cands = [preferred] if preferred else ["python3.12", "python3.13"]
    tried = []
    for c in cands:
        p = shutil.which(c) or (c if os.path.isfile(c) else None)
        v = _py_version(p) if p else None
        tried.append({"candidate": c, "path": p, "version": v})
        if v:
            ok = v.split(".")[:2] in (["3", "12"], ["3", "13"])
            return {"path": p, "version": v, "supported": ok, "tried": tried,
                    "note": None if ok else "requested interpreter is not 3.12/3.13"}
    v = _py_version(sys.executable)
    ok = sys.version_info[:2] in ((3, 12), (3, 13))
    return {"path": sys.executable, "version": v, "supported": ok, "tried": tried,
            "note": "python3.12/3.13 not found on PATH; using the interpreter running exitcapsule"
            + ("" if ok else " (NOT 3.12/3.13 - baseline results are not on the target version)")}


def venv_python(venv: Path) -> str:
    return str(venv / "bin" / "python")


def create_venv(python, venv: Path, budget):
    return run_cmd([python, "-m", "venv", str(venv)], venv.parent, budget, step="create venv")


def pip(py, sub, args, ctx, cwd, budget, step, cap=None):
    extra = list(ctx.pip_args) if sub == "install" else []
    return run_cmd([py, "-m", "pip", sub, *extra, *args], cwd, budget, cap=cap, step=step)


# ------------------------------------------------------------------ requirements
def requirement_files(repo: Path):
    files = []
    if (repo / "requirements.txt").exists():
        files.append(repo / "requirements.txt")
    files += [p for p in sorted(repo.glob("requirements*.txt")) if p not in files]
    return files


def read_requirement_lines(path: Path):
    out = []
    for raw in path.read_text(errors="replace").splitlines():
        line = re.split(r"\s+#", raw, 1)[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def req_name(line):
    if line.startswith("-"):
        return None
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
    return norm_name(m.group(1)) if m else None


def drop_requirements(repo: Path, names) -> list:
    """Remove pins for `names` from every requirements file in the copy; returns removed lines."""
    wanted = {norm_name(n) for n in names}
    removed = []
    for f in requirement_files(repo):
        keep = []
        for raw in f.read_text(errors="replace").splitlines():
            line = re.split(r"\s+#", raw, 1)[0].strip()
            if line and req_name(line) in wanted:
                removed.append(f"{f.name}: {line}")
            else:
                keep.append(raw)
        f.write_text("\n".join(keep) + "\n")
    return removed


def first_failure(steps):
    for s in steps:
        if not s.get("ok"):
            text = (s.get("stderr_tail") or s.get("stdout_tail") or s.get("error") or "").strip()
            return {"step": s.get("step"), "returncode": s.get("returncode"), "timed_out": s.get("timed_out"),
                    "message": "\n".join(text.splitlines()[-15:])}
    return None


def install_pins(py, repo: Path, ctx: Context, budget) -> dict:
    res = {"ok": True, "steps": [], "requirement_files": [], "diagnostics": [], "note": None}
    files = requirement_files(repo)
    if files:
        for f in files:
            res["requirement_files"].append(f.name)
            s = pip(py, "install", ["-r", f.name], ctx, repo, budget, f"pip install -r {f.name}")
            res["steps"].append(s)
            res["ok"] &= bool(s["ok"])
    elif (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        s = pip(py, "install", ["."], ctx, repo, budget, "pip install .")
        res["steps"].append(s)
        res["ok"] &= bool(s["ok"])
    else:
        res["note"] = "no requirements*.txt, pyproject.toml or setup.py found; nothing was installed"
    res["exact_failure"] = first_failure(res["steps"])
    timed_out = any(s.get("timed_out") for s in res["steps"])
    if not res["ok"] and files and not timed_out:
        # diagnostic only: which individual pins can be installed at all (environment may now be partial)
        lines = [l for f in files for l in read_requirement_lines(f) if not l.startswith("-")][:40]
        for l in lines:
            s = pip(py, "install", [l], ctx, repo, budget, f"diagnostic: pip install {l}", cap=180)
            res["diagnostics"].append({"requirement": l, "ok": s["ok"], "timed_out": s.get("timed_out"),
                                       "error": None if s["ok"] else tail(s.get("stderr_tail"), 600)})
    return res


def ensure_pytest(py, repo, ctx, budget):
    chk = run_cmd([py, "-c", "import pytest"], repo, budget, step="check pytest")
    if chk["ok"]:
        return {"ok": True, "installed_by_exitcapsule": False, "steps": []}
    s = pip(py, "install", ["pytest"], ctx, repo, budget, "pip install pytest (test runner)")
    return {"ok": s["ok"], "installed_by_exitcapsule": True, "steps": [s]}


# ------------------------------------------------------------------ patch / swap
def apply_patch(repo: Path, patch_path, budget) -> dict:
    res = {"present": bool(patch_path), "path": str(patch_path) if patch_path else None, "applied": False,
           "method": None, "error": None}
    if not patch_path:
        return res
    patch_path = Path(patch_path).resolve()
    if not patch_path.exists():
        res["error"] = f"patch file not found: {patch_path}"
        return res
    attempts = []
    if shutil.which("git"):
        r = run_cmd(["git", "apply", "--whitespace=nowarn", "-p1", str(patch_path)], repo, budget, step="git apply")
        attempts.append(r)
        if r["ok"]:
            res.update(applied=True, method="git apply")
            return res
    if shutil.which("patch"):
        d = run_cmd(["patch", "-p1", "--dry-run", "--batch", "-i", str(patch_path)], repo, budget, step="patch --dry-run")
        attempts.append(d)
        if d["ok"]:
            r = run_cmd(["patch", "-p1", "--batch", "-i", str(patch_path)], repo, budget, step="patch")
            attempts.append(r)
            if r["ok"]:
                res.update(applied=True, method="patch")
                return res
    if not attempts:
        res["error"] = "neither git nor patch is available to apply the patch"
    else:
        last = attempts[0] if len(attempts) == 1 else attempts[-1]
        res["error"] = "\n".join(filter(None, [(a.get("stderr_tail") or a.get("stdout_tail") or "").strip()
                                               for a in attempts]))[-2000:] or last.get("error")
    return res


def copy_repo(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".venv", "venv", "node_modules", "*.egg-info", ".pytest_cache", ".tox",
        "capsules", "dist", "build", ".mypy_cache"))


def install_char_tests(repo: Path, dep: str, src) -> str | None:
    """Copy C's characterization tests into the copy; returns the path relative to the repo."""
    if not src:
        return None
    src = Path(src)
    if not src.exists():
        return None
    rel = Path("exitcapsule_char") / safe_name(dep)
    dst = repo / rel
    dst.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        shutil.copy2(src, dst / src.name)
    return rel.as_posix()


# ------------------------------------------------------------------ pytest + junit
def _tb_info(text):
    text = text or ""
    locs = re.findall(r"^([^\s:]+\.py):(\d+):", text, re.M)
    e_lines = [l[1:].strip() for l in text.splitlines() if re.match(r"^E\s", l)]
    return {"traceback_line": f"{locs[-1][0]}:{locs[-1][1]}" if locs else None,
            "error_line": e_lines[0] if e_lines else None}


def parse_junit(path: Path):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    out = {}
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        status, text, msg = "passed", "", ""
        for tag, st in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            el = tc.find(tag)
            if el is not None:
                status, msg, text = st, el.get("message") or "", (el.text or "")
                break
        info = _tb_info(text)
        if status in ("failed", "error") and not info["error_line"] and msg:
            info["error_line"] = msg.splitlines()[0]
        out[tid] = {"status": status, "message": msg[:500], "time": float(tc.get("time") or 0),
                    "detail": text[-1500:] if status in ("failed", "error") else "", **info}
    return out


def counts_of(results):
    if results is None:
        return None
    c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for r in results.values():
        c[r["status"]] += 1
    c["total"] = sum(c.values())
    return c


def run_tests(py, repo: Path, paths, ctx: Context, budget, tag):
    junit = repo / f".exitcapsule_{tag}.xml"
    if junit.exists():
        junit.unlink()
    paths = [p for p in paths if (repo / p).exists()] or ["."]
    cmd = [py, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q", f"--junitxml={junit}", *paths, *ctx.pytest_args]
    rec = run_cmd(cmd, repo, budget, step=f"pytest ({tag})")
    results = parse_junit(junit) if junit.exists() else None
    rec["junit_found"] = results is not None
    rec["counts"] = counts_of(results)
    rec["paths"] = paths
    return rec, results


# ------------------------------------------------------------------ comparison
def compare_tests(base, cand):
    if base is None or cand is None:
        return {"comparable": False,
                "reason": "baseline results missing" if base is None else "candidate results missing"}
    cmp = {"comparable": True, "regressions": [], "fixed": [], "still_failing": [], "newly_skipped": [],
           "disappeared": [], "new_tests": [], "changes": []}
    for tid, b in base.items():
        c = cand.get(tid)
        origin = "characterization" if tid.startswith("exitcapsule_char") else "repo"
        if c is None:
            if b["status"] == "passed":
                cmp["disappeared"].append(tid)
            continue
        if b["status"] != c["status"]:
            cmp["changes"].append({"test": tid, "before": b["status"], "after": c["status"]})
        if b["status"] == "passed" and c["status"] in ("failed", "error"):
            cmp["regressions"].append({"test": tid, "origin": origin, "before": "passed", "after": c["status"],
                                       "error_line": c["error_line"], "traceback_line": c["traceback_line"],
                                       "message": c["message"]})
        elif b["status"] == "passed" and c["status"] == "skipped":
            cmp["newly_skipped"].append(tid)
        elif b["status"] in ("failed", "error") and c["status"] == "passed":
            cmp["fixed"].append(tid)
        elif b["status"] in ("failed", "error") and c["status"] in ("failed", "error"):
            cmp["still_failing"].append({"test": tid, "error_line": c["error_line"]})
    cmp["new_tests"] = [t for t in cand if t not in base]
    return cmp


# ------------------------------------------------------------------ status.html
_NORMALIZERS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TIMESTAMP>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b"), "<TIME>"),
    (re.compile(r"\b\d{1,2} [A-Z][a-z]{2} \d{4}\b|\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b"), "<DATE>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|msec|milliseconds?|µs|us)\b", re.I), "<LATENCY>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:s|sec|secs|seconds)\b", re.I), "<LATENCY>"),
]


def normalize_status(html: str) -> list:
    for rx, rep in _NORMALIZERS:
        html = rx.sub(rep, html)
    return [re.sub(r"\s+", " ", l).strip() for l in html.splitlines() if l.strip()]


def diff_status(base_html, cand_html):
    if base_html is None or cand_html is None:
        return {"compared": False,
                "reason": "status.html missing for " + ("baseline" if base_html is None else "candidate")}
    a, b = normalize_status(base_html), normalize_status(cand_html)
    d = list(difflib.unified_diff(a, b, "baseline", "candidate", lineterm="", n=1))
    return {"compared": True, "identical": not d, "diff_head": d[:40], "diff_lines": len(d),
            "normalization": "ISO timestamps, clock times, dates and latency/duration numbers replaced by placeholders"}


def collect_status_html(py, repo: Path, ctx: Context, budget):
    info = {"command": ctx.status_cmd}
    out = repo / "status.html"
    if ctx.status_cmd:
        cmd = [c.replace("{python}", py).replace("{out}", str(out)) for c in shlex.split(ctx.status_cmd)]
        rec = run_cmd(cmd, repo, budget, step="status.html generation")
        info["run"] = {k: rec.get(k) for k in ("ok", "returncode", "timed_out", "seconds", "stderr_tail")}
        if not rec["ok"]:
            info["error"] = "status command failed"
            return None, info
    for cand in (out, repo / "out" / "status.html"):
        if cand.exists():
            return cand.read_text(errors="replace"), info
    info["error"] = "no status.html produced (give --status-cmd, e.g. '{python} -m linkwatch ... {out}')"
    return None, info
