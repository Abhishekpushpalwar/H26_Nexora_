"""Dependency and import scanner for LinkWatch-style Python repositories."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.requirements import Requirement
from packaging.version import Version


WEIGHTS = {
    "specifier_conflict": 30,
    "undeclared_import": 25,
    "declared_unused": 10,
    "pip_conflict": 20,
    "pinned_release_age": 10,
    "latest_release_stale": 15,
    "stale_release_active_repo": 5,
    "github_inactive": 10,
    "major_versions_behind": 10,
    "unpinned": 5,
    "development_status": 5,
    "yanked_release": 20,
    "archived_repository": 15,
    "fragile_usage": 20,
}
TWO_YEARS_DAYS = 2 * 365
ONE_YEAR_DAYS = 365
FRAGILE_KINDS = {"monkeypatch", "side_effect_import"}
PYPI_BASE = "https://pypi.org/pypi"
GITHUB_BASE = "https://api.github.com/repos"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_test_usage_path(path: str) -> bool:
    parts = [part.lower() for part in re.split(r"[\\/]+", path) if part]
    filename = parts[-1] if parts else ""
    return (
        "tests" in parts
        or "test" in parts
        or filename == "conftest.py"
        or filename.startswith("test_") and filename.endswith(".py")
        or filename.endswith("_test.py")
    )


def _load_usage(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to parse usage file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Unable to parse usage file {path}: expected a JSON object")
    return {
        _canonical(name): value
        for name, value in data.items()
        if name not in {"asserted_heuristic", "errors"}
    }


def _read_requirements(path: Path) -> list[tuple[Requirement, str]]:
    if not path.exists():
        return []
    result: list[tuple[Requirement, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        try:
            result.append((Requirement(line), f"{path.name}:{line_number}"))
        except Exception as exc:
            raise ValueError(f"Invalid requirement at {path}:{line_number}: {raw!r}") from exc
    return result


def _read_project_dependencies(path: Path) -> list[tuple[Requirement, str]]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    dependencies = data.get("project", {}).get("dependencies", [])
    result: list[tuple[Requirement, str]] = []
    for index, value in enumerate(dependencies, 1):
        try:
            result.append((Requirement(value), f"{path.name}:project.dependencies[{index}]"))
        except Exception as exc:
            raise ValueError(f"Invalid project dependency at {path}: {value!r}") from exc
    return result


def _requirements(repo: Path) -> dict[str, list[tuple[Requirement, str]]]:
    entries = (
        _read_requirements(repo / "requirements.txt")
        + _read_requirements(repo / "requirements-dev.txt")
        + _read_project_dependencies(repo / "pyproject.toml")
    )
    grouped: dict[str, list[tuple[Requirement, str]]] = defaultdict(list)
    for requirement, source in entries:
        grouped[_canonical(requirement.name)].append((requirement, source))
    return dict(grouped)


def _exact_versions(requirements: list[Requirement]) -> set[str]:
    return {
        spec.version
        for requirement in requirements
        for spec in requirement.specifier
        if spec.operator == "=="
    }


def _has_conflict(requirements: list[Requirement]) -> bool:
    exact = _exact_versions(requirements)
    if len(exact) > 1:
        return True
    if not exact:
        return False
    version = Version(next(iter(exact)))
    return any(version not in requirement.specifier for requirement in requirements)


def _import_names(repo: Path) -> tuple[set[str], dict[str, list[str]]]:
    modules: set[str] = set()
    locations: dict[str, list[str]] = defaultdict(list)
    for root_name in ("linkwatch", "tests"):
        root = repo / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imported = [node.module.split(".", 1)[0]]
                for module in imported:
                    modules.add(module)
                    locations[module].append(f"{path.relative_to(repo)}:{node.lineno}")
    return modules, dict(locations)


def _distribution_imports(python: Path) -> dict[str, set[str]]:
    code = (
        "import importlib.metadata, json; "
        "print(json.dumps(importlib.metadata.packages_distributions()))"
    )
    status, output = _run_target(python, code)
    if status == 0 and output:
        try:
            raw = json.loads(output.splitlines()[-1])
            mapping: dict[str, set[str]] = defaultdict(set)
            for import_name, distributions in raw.items():
                for distribution in distributions:
                    mapping[_canonical(distribution)].add(import_name)
            return dict(mapping)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return {}


def _run_target(python: Path, code: str, *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        [str(python), "-c", code, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def _installed_version(python: Path, name: str) -> str | None:
    code = (
        "import importlib.metadata,sys; "
        "name=sys.argv[1]; "
        "print(importlib.metadata.version(name))"
    )
    status, output = _run_target(python, code, name)
    return output.splitlines()[-1].strip() if status == 0 and output else None


def _fetch_json(url: str, token: str | None = None, cache_dir: Path | None = None) -> dict[str, Any]:
    headers = {"User-Agent": "LinkWatch-scanner"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _github_repo(urls: dict[str, str] | None) -> tuple[str, str] | None:
    if not urls:
        return None
    for url in urls.values():
        match = re.search(r"github\.com/([^/]+)/([^/#]+)", url)
        if match:
            return match.group(1), match.group(2).removesuffix(".git")
    return None


def _fetch_json_observed(
    url: str,
    token: str | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    cache_path = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", url) + ".json")
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    headers = {"User-Agent": "LinkWatch-scanner"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
            if cache_path:
                cache_path.write_text(json.dumps(data), encoding="utf-8")
            return data
        except HTTPError as exc:
            last_error = exc
            if not 500 <= exc.code < 600:
                raise
        except (TimeoutError, URLError) as exc:
            last_error = exc
        if attempt < 2:
            import time
            time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _risk(indicator: str, value: Any, source: str, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "indicator": indicator,
        "value": value,
        "source": source,
        "provenance": "observed",
    }
    item.update(extra)
    return item


def _metadata(
    name: str,
    pinned: str | None,
    installed: str | None,
    risks: list[dict[str, Any]],
) -> None:
    source = f"{PYPI_BASE}/{name}"
    try:
        latest = _fetch_json(f"{source}/json")
        latest_info = latest["info"]
        latest_version = latest_info.get("version")
        effective_version = pinned or installed
        if effective_version:
            pinned_data = _fetch_json(f"{source}/{effective_version}/json")
            pinned_info = pinned_data["info"]
            release = pinned_data.get("urls", [])
            release_date = (
                release[0].get("upload_time") if release else None
            )
            yanked = any(item.get("yanked", False) for item in release)
            risks.append(_risk("pinned_release_date", release_date, f"{source}/{effective_version}/json"))
            risks.append(_risk("yanked", yanked, f"{source}/{effective_version}/json"))
            info = pinned_info
        else:
            risks.append(_risk(name, "not_retrieved", "installed package metadata",
                               reason="No installed version found in the selected interpreter"))
            info = latest_info
            yanked = False
        risks.append(_risk("latest_version", latest_version, f"{source}/json"))
        latest_release = latest.get("urls", [])
        risks.append(_risk(
            "latest_release_date",
            latest_release[0].get("upload_time") if latest_release else None,
            f"{source}/json",
        ))
        risks.append(_risk("development_status", [
            value.removeprefix("Development Status :: ")
            for value in info.get("classifiers", [])
            if value.startswith("Development Status :: ")
        ], f"{source}/json"))
        risks.append(_risk("requires_python", info.get("requires_python"), f"{source}/json"))
        risks.append(_risk("project_urls", info.get("project_urls", {}), f"{source}/json"))
        risks.append(_risk("yanked", yanked, f"{source}/{pinned or latest_version}/json"))
        repo = _github_repo(info.get("project_urls"))
        if repo:
            owner, repository = repo
            try:
                github = _fetch_json(
                    f"{GITHUB_BASE}/{owner}/{repository}",
                    os.environ.get("GITHUB_TOKEN"),
                )
                risks.append(_risk("github_archived", github.get("archived"), f"{GITHUB_BASE}/{owner}/{repository}"))
                risks.append(_risk("github_pushed_at", github.get("pushed_at"), f"{GITHUB_BASE}/{owner}/{repository}"))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                risks.append(_risk(name, "not_retrieved", f"{GITHUB_BASE}/{owner}/{repository}", reason=str(exc)))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        risks.append(_risk(name, "not_retrieved", source, reason=str(exc)))


def _pip_check(python: Path) -> list[str]:
    completed = subprocess.run(
        [str(python), "-m", "pip", "check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return [line.strip() for line in (completed.stdout + completed.stderr).splitlines() if line.strip()]


def _release_date(data: dict[str, Any], version: str | None) -> str | None:
    if not version:
        return None
    dates = [
        item.get("upload_time")
        for item in data.get("releases", {}).get(version, [])
        if item.get("upload_time")
    ]
    return max(dates) if dates else None


def _required_by(python: Path, name: str) -> list[str]:
    code = (
        "import importlib.metadata, json, sys; "
        "from packaging.requirements import Requirement; "
        "target=sys.argv[1]; result=[]; "
        "dists=importlib.metadata.distributions(); "
        "result=[d.metadata['Name'] for d in dists for req in (d.requires or []) "
        "if 'extra' not in req.lower() and Requirement(req).name.lower().replace('_','-') == target]; "
        "print(json.dumps(sorted(set(result))))"
    )
    status, output = _run_target(python, code, _canonical(name))
    if status != 0 or not output:
        return []
    try:
        return json.loads(output.splitlines()[-1])
    except json.JSONDecodeError:
        return []


def _metadata_v2(
    name: str,
    pinned: str | None,
    installed: str | None,
    risks: list[dict[str, Any]],
    failures: list[dict[str, str]],
    cache_dir: Path | None,
) -> dict[str, Any]:
    source = f"{PYPI_BASE}/{name}"
    latest_url = f"{source}/json"
    try:
        latest = _fetch_json_observed(latest_url, cache_dir=cache_dir)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        failures.append({"indicator": "pypi_metadata", "url": latest_url, "error": str(exc)})
        risks.append(_risk("pypi_metadata", "not_retrieved", latest_url, reason=str(exc)))
        return {}
    latest_info = latest["info"]
    latest_version = latest_info.get("version")
    effective = pinned or installed
    pinned_data = None
    if effective:
        pinned_url = f"{source}/{effective}/json"
        try:
            pinned_data = _fetch_json_observed(pinned_url, cache_dir=cache_dir)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures.append({"indicator": "pypi_metadata", "url": pinned_url, "error": str(exc)})
            risks.append(_risk("pypi_metadata", "not_retrieved", pinned_url, reason=str(exc)))
    info = (pinned_data or latest)["info"]
    latest_date = _release_date(latest, latest_version)
    pinned_dates = [
        item.get("upload_time_iso_8601")
        for item in (pinned_data or {}).get("urls", [])
        if item.get("upload_time_iso_8601")
    ]
    pinned_date = min(pinned_dates) if pinned_dates else None
    risks.extend([
        _risk("latest_version", latest_version, latest_url),
        _risk("latest_release_date", latest_date, latest_url),
        _risk("latest_development_status", [
            value.removeprefix("Development Status :: ")
            for value in info.get("classifiers", [])
            if value.startswith("Development Status :: ")
        ], latest_url),
        _risk("latest_requires_python", latest_info.get("requires_python"), latest_url),
        _risk("project_urls", info.get("project_urls", {}), latest_url),
    ])
    if pinned_data:
        risks.extend([
            _risk("pinned_release_date", pinned_date, f"{source}/{effective}/json"),
            _risk("pinned_requires_python", pinned_data["info"].get("requires_python"),
                  f"{source}/{effective}/json"),
            _risk("pinned_python_3.12", "Programming Language :: Python :: 3.12" in pinned_data["info"].get("classifiers", []),
                  f"{source}/{effective}/json"),
            _risk("pinned_python_3.13", "Programming Language :: Python :: 3.13" in pinned_data["info"].get("classifiers", []),
                  f"{source}/{effective}/json"),
            _risk("pinned_yanked", any(item.get("yanked", False) for item in pinned_data.get("urls", [])),
                  f"{source}/{effective}/json"),
        ])
    risks.append(_risk("latest_yanked", any(item.get("yanked", False) for item in latest.get("urls", [])), latest_url))
    github_pushed_at = None
    repo = _github_repo(info.get("project_urls"))
    if not repo:
        risks.append(_risk("no_github_url", True, latest_url))
    else:
        owner, repository = repo
        github_url = f"{GITHUB_BASE}/{owner}/{repository}"
        try:
            github = _fetch_json_observed(github_url, os.environ.get("GITHUB_TOKEN"), cache_dir)
            risks.extend([
                _risk("github_archived", github.get("archived"), github_url),
                _risk("github_pushed_at", github.get("pushed_at"), github_url),
            ])
            github_pushed_at = github.get("pushed_at")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures.append({"indicator": "github_metadata", "url": github_url, "error": str(exc)})
            risks.append(_risk("github_metadata", "not_retrieved", github_url, reason=str(exc)))
    return {
        "latest_version": latest_version,
        "effective_version": effective,
        "pinned_release_date": pinned_date,
        "latest_release_date": latest_date,
        "development_status": [
            value.removeprefix("Development Status :: ")
            for value in latest_info.get("classifiers", [])
            if value.startswith("Development Status :: ")
        ],
        "github_pushed_at": github_pushed_at,
        "pinned_yanked": bool(pinned_data and any(item.get("yanked", False) for item in pinned_data.get("urls", []))),
        "latest_yanked": any(item.get("yanked", False) for item in latest.get("urls", [])),
    }


def scan(
    repo_path: str | Path,
    venv_python: str | Path | None = None,
    use_cache: bool = False,
    usage_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    repo = Path(repo_path).resolve()
    python = Path(venv_python or sys.executable).resolve()
    usage_data = _load_usage(Path(usage_path)) if usage_path else None
    usage_source = str(Path(usage_path)) if usage_path else None
    declared = _requirements(repo)
    imported, locations = _import_names(repo)
    distribution_imports = _distribution_imports(python)
    imported_distributions: dict[str, set[str]] = defaultdict(set)
    for module in imported:
        for distribution, modules in distribution_imports.items():
            if module in modules:
                imported_distributions[distribution].add(module)

    names = set(declared) | set(imported_distributions)
    pip_conflicts = _pip_check(python)
    cache_dir = repo / ".scan_cache" if use_cache else None
    results: list[dict[str, Any]] = []
    for name in sorted(names):
        entries = declared.get(name, [])
        requirements = [item[0] for item in entries]
        pinned_versions = _exact_versions(requirements)
        pinned = next(iter(pinned_versions), None)
        installed = _installed_version(python, name)
        risks: list[dict[str, Any]] = []
        retrieval_failures: list[dict[str, str]] = []
        reasons: list[str] = []
        score = 0
        if not entries:
            indicator = "undeclared_import"
            risks.append(_risk(indicator, sorted(imported_distributions[name]), "AST import scan"))
            score += WEIGHTS[indicator]
            reasons.append(
                f"+{WEIGHTS[indicator]} undeclared distribution imported "
                f"({sorted(imported_distributions[name])!r})"
            )
        elif name not in imported_distributions:
            indicator = "declared_unused"
            risks.append(_risk(indicator, True, "AST import scan"))
            score += WEIGHTS[indicator]
            reasons.append(f"+{WEIGHTS[indicator]} declared distribution not imported (True)")
        if _has_conflict(requirements):
            indicator = "specifier_conflict"
            sources = [source for _, source in entries]
            risks.append(_risk(indicator, sources, "requirement parsing"))
            score += WEIGHTS[indicator]
            reasons.append(
                f"+{WEIGHTS[indicator]} incompatible specifiers across sources ({sources!r})"
            )
        for line in pip_conflicts:
            if re.search(rf"\b{re.escape(name)}\b", line, re.IGNORECASE):
                indicator = "pip_conflict"
                risks.append(_risk(indicator, line, "pip check"))
                score += WEIGHTS[indicator]
                reasons.append(f"+{WEIGHTS[indicator]} pip check conflict ({line!r})")
        metadata = _metadata_v2(
            name, pinned, installed, risks, retrieval_failures, cache_dir
        )
        required_by = _required_by(python, name)
        risks.append(_risk("required_by", required_by, "selected venv installed metadata"))
        if usage_data is not None:
            usage_entry = usage_data.get(name)
            if not isinstance(usage_entry, dict):
                risks.append(_risk("usage_data", "not_analyzed", usage_source or "usage"))
            else:
                fragile_sites = [
                    {
                        key: site.get(key)
                        for key in ("file", "line", "symbol", "kind")
                    }
                    for site in usage_entry.get("usage", [])
                    if isinstance(site, dict)
                    and not _is_test_usage_path(str(site.get("file", "")))
                    and (
                        site.get("private_api") is True
                        or site.get("kind") in FRAGILE_KINDS
                    )
                ]
                if fragile_sites:
                    risks.append(_risk(
                        "fragile_usage",
                        fragile_sites[:10],
                        usage_source or "usage",
                    ))
                    score += WEIGHTS["fragile_usage"]
                    reasons.append(
                        f"+{WEIGHTS['fragile_usage']} fragile usage "
                        f"({len(fragile_sites)} sites)"
                    )
        transitive = False
        if name not in imported_distributions and required_by:
            transitive = True
            for item in risks:
                if item["indicator"] == "declared_unused":
                    item["transitive"] = True
        if not pinned:
            score += WEIGHTS["unpinned"]
            reasons.append(f"+{WEIGHTS['unpinned']} unpinned (installed version {installed!r})")
        latest_is_stale = False
        try:
            same_as_latest = (
                metadata.get("effective_version")
                and metadata.get("latest_version")
                and Version(metadata["effective_version"]) == Version(metadata["latest_version"])
            )
        except (TypeError, ValueError):
            same_as_latest = False
        date_checks = [
            ("latest_release_date", "latest_release_stale", WEIGHTS["latest_release_stale"]),
        ]
        if not same_as_latest:
            date_checks.insert(
                0,
                ("pinned_release_date", "pinned_release_age", WEIGHTS["pinned_release_age"]),
            )
        for date_key, indicator, weight in date_checks:
            raw_date = metadata.get(date_key)
            if raw_date:
                release_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if release_date.tzinfo is None:
                    release_date = release_date.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - release_date).days > TWO_YEARS_DAYS:
                    latest_is_stale = indicator == "latest_release_stale"
                    score += weight
                    reasons.append(f"+{weight} {indicator.replace('_', ' ')} ({raw_date})")
        github_metadata_failed = any(
            failure["indicator"] == "github_metadata"
            for failure in retrieval_failures
        )
        if github_metadata_failed and latest_is_stale:
            score -= (
                WEIGHTS["latest_release_stale"]
                - WEIGHTS["stale_release_active_repo"]
            )
            for index, reason in enumerate(reasons):
                if reason.startswith(f"+{WEIGHTS['latest_release_stale']} latest release stale"):
                    reasons[index] = (
                        f"+{WEIGHTS['stale_release_active_repo']} stale release active repo "
                        f"({metadata['latest_release_date']})"
                    )
                    break
            reasons.append("github data unavailable")
        pushed_at = metadata.get("github_pushed_at")
        pushed_date = None
        if pushed_at:
            pushed_date = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            if pushed_date.tzinfo is None:
                pushed_date = pushed_date.replace(tzinfo=timezone.utc)
            pushed_age_days = (datetime.now(timezone.utc) - pushed_date).days
            if pushed_age_days > TWO_YEARS_DAYS:
                score += WEIGHTS["github_inactive"]
                reasons.append(f"+{WEIGHTS['github_inactive']} github inactive ({pushed_at})")
            elif latest_is_stale and pushed_age_days <= ONE_YEAR_DAYS:
                score -= WEIGHTS["latest_release_stale"] - WEIGHTS["stale_release_active_repo"]
                reasons[-1] = (
                    f"+{WEIGHTS['stale_release_active_repo']} stale release active repo "
                    f"({metadata['latest_release_date']}, pushed_at={pushed_at})"
                )
        try:
            current_major = Version(metadata["effective_version"]).major
            latest_major = Version(metadata["latest_version"]).major
        except (KeyError, TypeError, ValueError):
            current_major = latest_major = None
        major_versions_behind = (
            current_major is not None
            and latest_major is not None
            and latest_major > current_major
        )
        if major_versions_behind:
            points = min((latest_major - current_major) * WEIGHTS["major_versions_behind"], 20)
            score += points
            reasons.append(
                f"+{points} major versions behind latest "
                f"(current={metadata['effective_version']!r}, latest={metadata['latest_version']!r})"
            )
        statuses = metadata.get("development_status", [])
        if any("Inactive" in status for status in statuses):
            score += WEIGHTS["development_status"]
            reasons.append(f"+{WEIGHTS['development_status']} development status ({statuses!r})")
        if metadata.get("pinned_yanked"):
            score += WEIGHTS["yanked_release"]
            reasons.append(f"+{WEIGHTS['yanked_release']} pinned_yanked (True)")
        if metadata.get("latest_yanked"):
            score += WEIGHTS["yanked_release"]
            reasons.append(f"+{WEIGHTS['yanked_release']} latest_yanked (True)")
        for item in risks:
            if item["indicator"] == "github_archived" and item["value"] is True:
                score += WEIGHTS["archived_repository"]
                reasons.append(f"+{WEIGHTS['archived_repository']} GitHub repository archived (True)")
        if not entries:
            change_type = "declare"
        elif major_versions_behind or _has_conflict(requirements):
            change_type = "upgrade"
        elif name not in imported_distributions and not transitive:
            change_type = "remove"
        else:
            change_type = None
        results.append({
            "dependency": {
                "name": name,
                "pinned": pinned or installed,
                "change_type": change_type,
            },
            "risk": risks,
            "retrieval_failures": retrieval_failures,
            "score": int(score),
            "score_reasons": reasons,
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--venv-python", type=Path)
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--usage", type=Path)
    args = parser.parse_args()
    try:
        output = scan(args.repo, args.venv_python, args.cache, args.usage)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
