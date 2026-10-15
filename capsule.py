"""capsule.py - capsule model, open-concern derivation, JSON + Markdown rendering, index (Person D).

A capsule keeps two kinds of content strictly apart:
  observed - produced by running things (installs, pytest, status.html diff, coverage join)
  ai       - model-written text (Gemma); passed through from the plan and never upgraded to evidence
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "0.1"
EXEC_KINDS = {"call", "attribute", "reference", "monkeypatch"}

OUTCOMES = {
    "no_plan": "no plan from the migration step - nothing was executed",
    "patch_failed": "patch did not apply - nothing was executed",
    "install_failed": "candidate environment failed to install",
    "timeout": "timed out before a verdict",
    "tests_not_run": "tests could not be run",
    "baseline_unavailable": "no baseline to compare against (candidate results reported alone)",
    "regressions": "previously passing tests fail after the swap",
    "pass_weak_evidence": "no regressions, but the swap touches code the baseline tests never executed",
    "pass": "no regressions observed",
    "error_in_exitcapsule": "exitcapsule itself failed on this dependency",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usage_evidence(usages, dep):
    """Summarise Person B's usage.json for one dependency (production code only)."""
    from .runner import norm_name
    if usages is None:
        return {"available": False}
    us = [u for u in usages if norm_name(u["dep"]) == norm_name(dep) and not u["in_tests"]
          and u["kind"] not in ("mock_patch", "string_ref")]
    brief = lambda u: {"site": f"{u['file']}:{u['line']}", "symbol": u["symbol"], "kind": u["kind"],
                       "private": u.get("private", False)}
    return {
        "available": True, "sites": len(us),
        "uncovered": [brief(u) for u in us if u["covered"] is False],
        "covered_unasserted": [brief(u) for u in us if u["covered"] is True and u["asserted"] is False
                               and u["kind"] in EXEC_KINDS],
        "private_api": [brief(u) for u in us if u.get("private")],
        "monkeypatch": [brief(u) for u in us if u["kind"] in ("monkeypatch", "side_effect_import")],
    }


def derive_gaps(observed):
    """Gaps come from execution evidence only."""
    gaps = []
    cmp = (observed.get("comparison") or {})
    for r in cmp.get("regressions", []):
        gaps.append({"kind": "test_regression", "test": r["test"], "origin": r["origin"],
                     "before": "passed", "after": r["after"], "error": r["error_line"],
                     "traceback_line": r["traceback_line"]})
    for t in cmp.get("disappeared", []):
        gaps.append({"kind": "test_disappeared", "test": t, "before": "passed", "after": "not collected"})
    st = observed.get("status_html") or {}
    if st.get("compared") and not st.get("identical"):
        gaps.append({"kind": "status_html_diff", "diff_lines": st["diff_lines"], "diff_head": st["diff_head"][:12]})
    return gaps


def classify(observed, usage_ev):
    if not observed.get("plan_found"):
        return "no_plan", "none"
    if observed.get("error"):
        return "error_in_exitcapsule", "none"
    stage = observed.get("failure_stage")
    if stage == "patch":
        return "patch_failed", "none"
    if stage == "timeout":
        return "timeout", "none"
    if stage == "install":
        return "install_failed", "none"
    if stage in ("pytest", "tooling"):
        return "tests_not_run", "none"
    cmp = observed.get("comparison") or {}
    if not cmp.get("comparable"):
        return "baseline_unavailable", "weak"
    if cmp.get("regressions") or cmp.get("disappeared") or (
            (observed.get("status_html") or {}).get("compared") and not observed["status_html"]["identical"]):
        return "regressions", "observed_failure"
    if usage_ev.get("available") and usage_ev["uncovered"]:
        return "pass_weak_evidence", "weak"
    if not usage_ev.get("available"):
        return "pass_weak_evidence", "weak"      # no coverage join supplied -> cannot claim strong evidence
    return "pass", "strong"


def derive_open_concerns(cap):
    obs, ev, ai = cap["observed"], cap["usage_evidence"], cap.get("ai") or {}
    out = []

    def add(source, kind, text):
        out.append({"source": source, "kind": kind, "text": text})

    if not obs.get("plan_found"):
        add("run", "no_plan", "No plan (patch/swap) exists for this dependency, so no candidate was built or run.")
    base = obs.get("baseline") or {}
    if base and not base.get("install_ok", True):
        f = base.get("install_failure") or {}
        add("observed", "baseline_install_failed",
            f"Baseline pins did not install on Python {base.get('python_version')}: "
            f"{(f.get('message') or '').splitlines()[-1] if f.get('message') else 'see observed.baseline'}")
    bc = base.get("test_counts")
    if bc and (bc["failed"] or bc["error"]):
        add("observed", "baseline_failing", f"{bc['failed'] + bc['error']} test(s) already fail on the baseline.")
    if obs.get("failure_stage") == "patch":
        add("observed", "patch_failed", "The patch did not apply: " + str((obs.get("patch") or {}).get("error"))[:300])
    if obs.get("failure_stage") == "install":
        f = (obs.get("install") or {}).get("exact_failure") or {}
        add("observed", "install_failed", "Candidate install failed: " + (f.get("message") or "see observed.install")[-300:])
    if obs.get("failure_stage") == "timeout":
        add("observed", "timeout", f"Timed out after the per-dependency limit ({obs.get('timeout_seconds')}s); no verdict.")
    for g in cap["gaps"]:
        if g["kind"] == "test_regression":
            add("observed", "regression", f"{g['test']} passed before, {g['after']} after: {g['error']} ({g['traceback_line']})")
        elif g["kind"] == "status_html_diff":
            add("observed", "status_html_diff", f"Normalized status.html differs ({g['diff_lines']} diff lines).")
    cmp = obs.get("comparison") or {}
    sk = cmp.get("newly_skipped") or []
    if sk:
        add("observed", "skipped", f"{len(sk)} previously passing test(s) are skipped on the candidate: {', '.join(sk[:5])}")
    cand_c = (obs.get("tests") or {}).get("counts") or {}
    if cand_c.get("skipped") and not sk:
        add("observed", "skipped", f"{cand_c['skipped']} test(s) skipped on the candidate run.")
    if ev.get("available"):
        if ev["uncovered"]:
            sites = ", ".join(u["site"] for u in ev["uncovered"][:6])
            add("coverage", "uncovered_sites",
                f"{len(ev['uncovered'])} usage site(s) were never executed by the baseline tests ({sites}); "
                "a pass here is weak evidence for that code.")
        if ev["covered_unasserted"]:
            add("coverage", "unasserted_sites",
                f"{len(ev['covered_unasserted'])} usage site(s) run but no test asserts on their output (heuristic).")
        if ev["private_api"]:
            add("coverage", "private_api", f"{len(ev['private_api'])} private-API usage site(s): higher migration risk.")
        if ev["monkeypatch"]:
            add("coverage", "monkeypatch", f"{len(ev['monkeypatch'])} monkeypatch/import-time patch site(s).")
    else:
        add("coverage", "no_usage_data", "No usage/coverage data was supplied; weak-evidence checks could not run.")
    if obs.get("plan_found") and not (obs.get("characterization") or {}).get("present"):
        add("observed", "no_characterization_tests", "No characterization tests were supplied for this dependency.")
    elif (obs.get("characterization") or {}).get("present") and not (obs.get("characterization") or {}).get("baseline_counts"):
        add("observed", "char_no_baseline", "Characterization tests were not run on the baseline, so they cannot show a before/after change.")
    for f in ai.get("validation_failures", []) or []:
        add("ai", "gemma_validation_failure", str(f))
    if obs.get("live_run"):
        add("observed", "non_deterministic", "status.html comes from a live run; differences may be non-deterministic.")
    return out


def build_capsule(dep, plan, observed, usage_ev, meta):
    plan = plan or {}
    outcome, strength = classify(observed, usage_ev)
    cap = {
        "schema_version": SCHEMA_VERSION,
        "dependency": dep,
        "replacement": plan.get("replacement"),
        "migration_kind": plan.get("kind"),
        "generated_at": now(),
        "tool": meta,
        "outcome": outcome,
        "outcome_meaning": OUTCOMES[outcome],
        "evidence_strength": strength,
        "observed": observed,
        "usage_evidence": usage_ev,
        "gaps": derive_gaps(observed),
        "ai": plan.get("ai"),
    }
    cap["open_concerns"] = derive_open_concerns(cap)
    return cap


# ------------------------------------------------------------------ markdown
def _bullets(items, prefix="- "):
    return "\n".join(f"{prefix}{i}" for i in items) if items else "_none_"


def _quote(text):
    return "\n".join("> " + l if l.strip() else ">" for l in str(text).splitlines())


def _ai_block(ai):
    if not ai:
        return "> _No AI-generated content was supplied for this dependency._"
    lines = [f"> **Model:** {ai.get('model', 'unspecified')}  -  **not verified by execution unless a claim below says so**", ">"]
    for k, v in ai.items():
        if k in ("model", "validation_failures"):
            continue
        lines.append(f"> **{k.replace('_', ' ').title()}**")
        if isinstance(v, str):
            lines.append(_quote(v))
        elif isinstance(v, list):
            for i in v:
                lines.append(_quote("- " + (i if isinstance(i, str) else json.dumps(i, ensure_ascii=False))))
        else:
            lines.append(_quote("```json\n" + json.dumps(v, indent=2, ensure_ascii=False) + "\n```"))
        lines.append(">")
    if ai.get("validation_failures"):
        lines.append("> **Validation failures (model output that failed checks)**")
        lines.append(_quote("\n".join("- " + str(f) for f in ai["validation_failures"])))
    return "\n".join(lines)


def render_markdown(cap):
    o, ev = cap["observed"], cap["usage_evidence"]
    t = lambda b: "yes" if b else "no"
    L = [f"# Exit capsule: `{cap['dependency']}` -> `{cap.get('replacement') or '?'}`", "",
         f"**Outcome:** `{cap['outcome']}` - {cap['outcome_meaning']}  ",
         f"**Evidence strength:** `{cap['evidence_strength']}`  ",
         f"**Generated:** {cap['generated_at']}  -  schema {cap['schema_version']}", ""]
    if cap["outcome"] == "pass_weak_evidence":
        L += ["> **Read this before trusting the pass:** the tests that passed did not execute every place this "
              "dependency is used (or no coverage data was supplied). A pass is weak evidence for those sites.", ""]
    L += ["---", "", "## OBSERVED - executed evidence (reproducible)", ""]
    b = o.get("baseline") or {}
    L += ["### Baseline", "",
          f"- Python: `{b.get('python_version')}`" + ("" if b.get("python_supported", True) else " (not 3.12/3.13!)"),
          f"- Pins installed: **{t(b.get('install_ok'))}**" if b else "- not run",
          f"- Baseline tests: {b.get('test_counts') or 'not available'}"]
    if b.get("install_failure"):
        L += ["", "Exact install failure:", "```", b["install_failure"].get("message", ""), "```"]
    L += ["", "### Candidate", ""]
    if not o.get("plan_found"):
        L += ["No plan was supplied for this dependency, so nothing was executed.", ""]
    else:
        p = o.get("patch") or {}
        sw = o.get("swap") or {}
        L += [f"- Patch: {'applied via ' + p['method'] if p.get('applied') else ('none supplied' if not p.get('present') else 'DID NOT APPLY')}",
              f"- Swap: remove {sw.get('remove') or []}, install {sw.get('install') or []}, uninstall after {sw.get('uninstall_after') or []}",
              f"- Install: **{t((o.get('install') or {}).get('ok'))}**" if o.get("install") else "- Install: not reached",
              f"- Tests: {(o.get('tests') or {}).get('counts') or 'not available'}",
              f"- Wall time: {o.get('seconds')}s (limit {o.get('timeout_seconds')}s)"]
        if p.get("error"):
            L += ["", "Patch error:", "```", str(p["error"])[:1500], "```"]
        if o.get("failure_stage") == "install":
            L += ["", "Exact install failure:", "```", ((o.get("install") or {}).get("exact_failure") or {}).get("message", ""), "```"]
        if o.get("failure_stage") == "timeout":
            L += ["", f"**Timed out** at stage `{o.get('timeout_stage')}`."]
        cmp = o.get("comparison") or {}
        if cmp.get("comparable"):
            L += ["", "### Per-test comparison (baseline vs candidate)", "",
                  f"regressions **{len(cmp['regressions'])}**, fixed {len(cmp['fixed'])}, still failing {len(cmp['still_failing'])}, "
                  f"newly skipped {len(cmp['newly_skipped'])}, disappeared {len(cmp['disappeared'])}, new {len(cmp['new_tests'])}", ""]
            if cmp["regressions"]:
                L += ["| test | before | after | error | traceback line |", "|---|---|---|---|---|"]
                for r in cmp["regressions"]:
                    L.append(f"| `{r['test']}` ({r['origin']}) | passed | {r['after']} | {(r['error_line'] or '')[:120]} | `{r['traceback_line']}` |")
        elif cmp:
            L += ["", f"Per-test comparison not possible: {cmp.get('reason')}."]
        st = o.get("status_html") or {}
        L += ["", "### status.html (normalized)", ""]
        if st.get("compared"):
            L.append("identical after normalization" if st["identical"] else f"**differs** ({st['diff_lines']} diff lines)")
            if not st["identical"]:
                L += ["```diff", *st["diff_head"][:25], "```"]
            L.append(f"_Normalization: {st['normalization']}._")
        else:
            L.append(f"not compared: {st.get('reason') or st.get('error') or 'n/a'}")
    L += ["", "### Usage and coverage evidence (from usage analysis)", ""]
    if ev.get("available"):
        L += [f"- usage sites in production code: {ev['sites']}",
              f"- never executed by baseline tests: **{len(ev['uncovered'])}**" + (": " + ", ".join(f"`{u['site']}`" for u in ev["uncovered"][:8]) if ev["uncovered"] else ""),
              f"- executed but never asserted (heuristic): {len(ev['covered_unasserted'])}",
              f"- private API: {len(ev['private_api'])}; monkeypatch/import-time patch: {len(ev['monkeypatch'])}"]
    else:
        L.append("not supplied")
    L += ["", "### Gaps (execution evidence only)", ""]
    L.append(_bullets([f"`{g['kind']}` {g.get('test', '')} {g.get('error') or ''} {('(' + g['traceback_line'] + ')') if g.get('traceback_line') else ''}".strip()
                       for g in cap["gaps"]]))
    L += ["", "---", "", "## AI-GENERATED - Gemma output (not evidence)", "", _ai_block(cap.get("ai")), "", "---", "",
          "## Open concerns (auto-filled)", ""]
    tag = {"observed": "OBSERVED", "coverage": "COVERAGE", "ai": "AI", "run": "RUN"}
    L.append(_bullets([f"**[{tag.get(c['source'], c['source'])}]** {c['text']}" for c in cap["open_concerns"]]))
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ files
def write_capsule(outdir: Path, cap):
    outdir.mkdir(parents=True, exist_ok=True)
    name = cap["dependency"]
    (outdir / f"{name}.json").write_text(json.dumps(cap, indent=2, ensure_ascii=False))
    (outdir / f"{name}.md").write_text(render_markdown(cap))


def write_index(outdir: Path, caps, baseline_summary):
    rows = [{"dependency": c["dependency"], "replacement": c.get("replacement"), "outcome": c["outcome"],
             "evidence_strength": c["evidence_strength"], "regressions": sum(1 for g in c["gaps"] if g["kind"] == "test_regression"),
             "open_concerns": len(c["open_concerns"]), "json": f"{c['dependency']}.json", "markdown": f"{c['dependency']}.md"}
            for c in caps]
    (outdir / "index.json").write_text(json.dumps({"generated_at": now(), "baseline": baseline_summary, "capsules": rows}, indent=2))
    L = ["# Exit capsules - index", "", f"Generated {now()}", "", "## Baseline", "",
         f"- Python `{baseline_summary.get('python_version')}`; pins installed: **{'yes' if baseline_summary.get('install_ok') else 'NO'}**; "
         f"tests: {baseline_summary.get('test_counts') or 'not available'}"]
    if baseline_summary.get("install_failure"):
        L += ["", "```", baseline_summary["install_failure"].get("message", ""), "```"]
    L += ["", "## Dependencies", "", "| dependency | replacement | outcome | evidence | regressions | open concerns | capsule |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['dependency']} | {r['replacement'] or ''} | `{r['outcome']}` | {r['evidence_strength']} | {r['regressions']} | "
                 f"{r['open_concerns']} | [md]({r['markdown']}) / [json]({r['json']}) |")
    L += ["", "`pass_weak_evidence` = no regressions, but the swap touches code the baseline tests never executed (or no coverage data).",
          "Sections marked AI-GENERATED inside each capsule are model output, not evidence.", ""]
    (outdir / "index.md").write_text("\n".join(L))
