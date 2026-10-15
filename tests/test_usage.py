import ast
import json
import sys
from pathlib import Path

import usage
import scanner
from usage import (
    _AliasCollector,
    _dependency_for_module,
    _dependency_import_roots,
    find_gaps,
    find_usage,
    list_imports,
)
from packaging.requirements import Requirement


def test_import_aliases_resolve_to_dependency_symbols():
    tree = ast.parse(
        "from requests import Session as S\n"
        "from requests.utils import _parse_content_type_header\n"
        "session = S()\n"
        "session.get('https://example.test')\n"
        "_parse_content_type_header('text/html')\n"
    )
    roots = _dependency_import_roots({"requests"})
    aliases = _AliasCollector(tree, roots, "example")

    calls = [
        aliases.resolve(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]

    assert "requests.Session" in calls
    assert "requests.Session.get" in calls
    assert "requests.utils._parse_content_type_header" in calls
    assert _dependency_for_module(calls[-1], roots) == "requests"


def test_package_root_resolves_to_distribution_name():
    roots = _dependency_import_roots({"python-dateutil"})

    assert _dependency_for_module("dateutil.parser.parse", roots) == "python-dateutil"


def test_find_usage_canonicalizes_dependency_keys_and_preserves_scanner_fields(
    tmp_path, monkeypatch
):
    package = tmp_path / "linkwatch"
    package.mkdir()
    (package / "dates.py").write_text(
        "from dateutil import parser\n"
        "def parse(value):\n"
        "    return parser.parse(value)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        usage,
        "_run_coverage",
        lambda repo: ({str((package / "dates.py").resolve()): {1, 3}}, None),
    )

    result, _ = usage._scan(
        tmp_path,
        ["python_dateutil", "python-dateutil"],
        {"python_dateutil": "dateutil"},
    )

    assert list(result) == ["python-dateutil"]
    sites = result["python-dateutil"]["usage"]
    assert any(site["symbol"] == "dateutil.parser" for site in sites)
    assert any(site["symbol"] == "dateutil.parser.parse" for site in sites)
    scanner_fields = {"file", "line", "symbol", "kind", "private_api"}
    assert all(scanner_fields <= site.keys() for site in sites)
    assert all(site["kind"] not in {"private_api"} for site in sites)


def test_list_imports_returns_canonical_distribution_names(tmp_path):
    package = tmp_path / "linkwatch"
    package.mkdir()
    (package / "report.py").write_text(
        "from jinja2 import Environment\n"
        "from dateutil import parser\n",
        encoding="utf-8",
    )

    imports = list_imports(
        str(tmp_path),
        {"python_dateutil": "dateutil"},
    )

    assert imports == [
        {"name": "jinja2", "file": "linkwatch/report.py", "line": 1},
        {"name": "python-dateutil", "file": "linkwatch/report.py", "line": 2},
    ]


def test_cli_dependency_and_import_mapping_options_write_json(tmp_path, monkeypatch):
    dependencies = [
        "pydantic", "idna", "chardet", "certifi", "appdirs", "toml",
        "nose", "setuptools", "jinja2", "six", "requests", "python-dateutil",
    ]
    captured = {}
    fake_results = {
        "python-dateutil": {
            "usage": [],
            "covered": 0,
            "asserted": 0,
            "total": 0,
        },
        "errors": [],
        "asserted_heuristic": "heuristic",
    }

    def fake_scan(repo, names, mappings):
        captured["names"] = names
        captured["mappings"] = mappings
        return fake_results.copy(), []

    output = tmp_path / "usage_out" / "usage.json"
    monkeypatch.setattr(usage, "_scan", fake_scan)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "usage.py",
            "--deps",
            ",".join(dependencies),
            "--map",
            "python-dateutil=dateutil",
            "--out",
            str(output),
        ],
    )

    usage.main()

    written = json.loads(output.read_text(encoding="utf-8"))
    assert captured["names"] == dependencies
    assert captured["mappings"] == {"python-dateutil": "dateutil"}
    assert written["python-dateutil"]["usage"] == []
    assert written["errors"] == []
    assert written["asserted_heuristic"] == usage.ASSERTED_HEURISTIC


def test_coverage_data_is_used_when_pytest_fails(tmp_path, monkeypatch):
    source = tmp_path / "linkwatch" / "client.py"
    source.parent.mkdir()
    source.write_text("import requests\n", encoding="utf-8")

    def fake_run(command, cwd, env, **kwargs):
        if command[3:5] == ["run", "-m"]:
            Path(env["COVERAGE_FILE"]).write_text("partial data", encoding="utf-8")
            return usage.subprocess.CompletedProcess(
                command, 1, stdout="1 failed", stderr=""
            )
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(
            json.dumps({
                "files": {
                    "linkwatch/client.py": {"executed_lines": [1]},
                },
            }),
            encoding="utf-8",
        )
        return usage.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(usage.subprocess, "run", fake_run)

    executed, error = usage._run_coverage(tmp_path)

    assert executed == {str(source.resolve()): {1}}
    assert error is not None
    assert "exited with code 1" in error
    assert "1 failed" in error


def test_find_usage_resolves_aliases_and_flags_private_monkeypatch_and_side_effect(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        usage, "_run_coverage", lambda repo: (None, "baseline unavailable")
    )
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "patch.py").write_text(
        "import requests\n"
        "def patched(*args, **kwargs):\n"
        "    return requests.Session.request(*args, **kwargs)\n",
        encoding="utf-8",
    )
    (package / "client.py").write_text(
        "import requests as r\n"
        "from requests import Session as S\n"
        "from requests.utils import _parse_content_type_header\n"
        "def fetch():\n"
        "    session = S()\n"
        "    response = session.get('https://example.test')\n"
        "    return response.headers\n"
        "r.Session.request = lambda *args: None\n"
        "_parse_content_type_header('text/html')\n",
        encoding="utf-8",
    )
    (package / "cli.py").write_text(
        "from . import patch\n",
        encoding="utf-8",
    )
    (tests / "test_client.py").write_text(
        "import certifi\n"
        "from requests.utils import _parse_content_type_header\n"
        "_parse_content_type_header('text/html')\n",
        encoding="utf-8",
    )

    result = find_usage(str(tmp_path), ["requests", "certifi", "unused"])

    request_usage = result["requests"]["usage"]
    assert any(site["symbol"] == "requests.Session.get" for site in request_usage)
    assert any(
        site["symbol"] == "requests.utils._parse_content_type_header"
        and site["private_api"]
        for site in request_usage
    )
    assert any(site["kind"] == "monkeypatch" for site in request_usage)
    assert any(site["kind"] == "side_effect_import" for site in request_usage)
    assert result["unused"]["usage"] == []
    assert result["unused"]["total"] == 0
    assert all(site["covered"] is None for site in request_usage)
    assert any(error.get("stage") == "coverage" for error in result["errors"])


def test_find_usage_sorts_and_records_syntax_errors_without_stopping(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        usage, "_run_coverage", lambda repo: (None, "baseline unavailable")
    )
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "z.py").write_text(
        "import requests\nrequests.get('https://z.test')\n",
        encoding="utf-8",
    )
    (package / "a.py").write_text(
        "import requests\nrequests.post('https://a.test')\n",
        encoding="utf-8",
    )
    (tests / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = find_usage(str(tmp_path), ["requests"])
    sites = result["requests"]["usage"]

    assert [
        (site["file"], site["line"], site["kind"])
        for site in sites
    ] == [
        ("linkwatch/a.py", 1, "import"),
        ("linkwatch/a.py", 2, "attribute"),
        ("linkwatch/a.py", 2, "call"),
        ("linkwatch/z.py", 1, "import"),
        ("linkwatch/z.py", 2, "attribute"),
        ("linkwatch/z.py", 2, "call"),
    ]
    assert result["errors"][0] == {
        "file": "tests/broken.py",
        "error": "SyntaxError at line 1, column 12: invalid syntax",
    }
    assert any(error.get("stage") == "coverage" for error in result["errors"])
    assert all(site["covered"] is None for site in sites)


def test_list_imports_returns_dependency_import_locations(tmp_path):
    package = tmp_path / "linkwatch"
    package.mkdir()
    (package / "module.py").write_text(
        "from dateutil import parser\n"
        "import requests as r\n",
        encoding="utf-8",
    )

    imports = list_imports(str(tmp_path))

    assert imports == [
        {"name": "python-dateutil", "file": "linkwatch/module.py", "line": 1},
        {"name": "requests", "file": "linkwatch/module.py", "line": 2},
    ]


def test_coverage_marks_executed_and_unexecuted_usage_lines(tmp_path):
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "client.py").write_text(
        "import requests\n"
        "def make_request():\n"
        "    return requests.Request('GET', 'https://example.test')\n"
        "def unused_request():\n"
        "    return requests.Session()\n",
        encoding="utf-8",
    )
    (tests / "test_client.py").write_text(
        "from linkwatch.client import make_request\n"
        "def test_request_method():\n"
        "    assert make_request().method == 'GET'\n",
        encoding="utf-8",
    )

    result = find_usage(str(tmp_path), ["requests"])
    covered_by_symbol = {
        (site["symbol"], site["line"]): site["covered"]
        for site in result["requests"]["usage"]
    }

    assert result["errors"] == []
    assert covered_by_symbol[("requests", 1)] is True
    assert covered_by_symbol[("requests.Request", 3)] is True
    assert covered_by_symbol[("requests.Session", 5)] is False


def test_asserted_requires_coverage_and_test_assert_of_function_result(
    tmp_path, monkeypatch
):
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    module = package / "client.py"
    module.write_text(
        "import requests\n"
        "\n"
        "def inner():\n"
        "    return requests.get('https://example.test')\n"
        "\n"
        "def fetch():\n"
        "    return inner()\n"
        "\n"
        "def unused():\n"
        "    return requests.post('https://unused.test')\n",
        encoding="utf-8",
    )
    (tests / "test_client.py").write_text(
        "from linkwatch.client import fetch\n"
        "\n"
        "def test_fetch():\n"
        "    result = fetch()\n"
        "    assert result.status_code == 200\n",
        encoding="utf-8",
    )
    source = str(module.resolve())
    monkeypatch.setattr(
        usage,
        "_run_coverage",
        lambda repo: ({source: {1, 4, 7}}, None),
    )

    result = find_usage(str(tmp_path), ["requests"])
    sites = result["requests"]["usage"]
    request_call = next(site for site in sites if site["line"] == 4 and site["kind"] == "call")
    request_attribute = next(
        site for site in sites if site["line"] == 4 and site["kind"] == "attribute"
    )
    unused_call = next(site for site in sites if site["line"] == 10 and site["kind"] == "call")

    assert request_call["covered"] is True
    assert request_call["asserted"] is True
    assert request_attribute["asserted"] is True
    assert unused_call["covered"] is False
    assert unused_call["asserted"] is False
    assert result["requests"]["asserted"] == 2
    assert "one direct call away" in result["asserted_heuristic"]


def test_unavailable_coverage_does_not_mark_usage_asserted(tmp_path, monkeypatch):
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "client.py").write_text(
        "import requests\n"
        "def fetch():\n"
        "    return requests.get('https://example.test')\n",
        encoding="utf-8",
    )
    (tests / "test_client.py").write_text(
        "from linkwatch.client import fetch\n"
        "def test_fetch():\n"
        "    result = fetch()\n"
        "    assert result.status_code == 200\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        usage, "_run_coverage", lambda repo: (None, "coverage unavailable")
    )

    result = find_usage(str(tmp_path), ["requests"])

    assert result["errors"] == [{
        "stage": "coverage",
        "error": "coverage unavailable",
    }]
    assert all(site["covered"] is None for site in result["requests"]["usage"])
    assert all(not site["asserted"] for site in result["requests"]["usage"])


def test_find_gaps_includes_false_flags_but_not_coverage_null_alone():
    result = {
        "requests": {
            "usage": [
                {
                    "file": "linkwatch/client.py",
                    "line": 4,
                    "covered": False,
                    "asserted": False,
                    "function_name": "fetch",
                    "function_source": "def fetch():\n    return requests.get(url)",
                    "snippet": "4 | return requests.get(url)",
                },
                {
                    "file": "linkwatch/client.py",
                    "line": 8,
                    "covered": None,
                    "asserted": True,
                    "function_name": "cached",
                    "function_source": "def cached():\n    return value",
                    "snippet": "8 | return value",
                },
                {
                    "file": "linkwatch/client.py",
                    "line": 12,
                    "covered": None,
                    "asserted": False,
                    "function_name": "unknown",
                    "function_source": "def unknown():\n    return requests.head(url)",
                    "snippet": "12 | return requests.head(url)",
                },
                {
                    "file": "linkwatch/client.py",
                    "line": 16,
                    "covered": True,
                    "asserted": True,
                    "function_name": "checked",
                    "function_source": "def checked():\n    return requests.get(url)",
                    "snippet": "16 | return requests.get(url)",
                },
                {
                    "file": "linkwatch/client.py",
                    "line": 20,
                    "covered": True,
                    "asserted": False,
                    "function_name": "unchecked",
                    "function_source": "def unchecked():\n    return requests.post(url)",
                    "snippet": "20 | return requests.post(url)",
                },
            ]
        },
        "errors": [{"stage": "coverage", "error": "tests failed"}],
        "asserted_heuristic": usage.ASSERTED_HEURISTIC,
    }

    gaps = find_gaps(result)

    assert [(gap["line"], gap["function_name"]) for gap in gaps] == [
        (4, "fetch"),
        (20, "unchecked"),
    ]
    assert set(gaps[0]) == {
        "dep", "file", "line", "function_name", "function_source", "snippet",
    }


def test_find_usage_records_function_source_for_gap_handoff(tmp_path, monkeypatch):
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    source = package / "client.py"
    source.write_text(
        "import requests\n"
        "def fetch():\n"
        "    return requests.get('https://example.test')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        usage,
        "_run_coverage",
        lambda repo: ({str(source.resolve()): {1, 3}}, None),
    )

    result = find_usage(str(tmp_path), ["requests"])
    gap = next(gap for gap in find_gaps(result) if gap["line"] == 3)

    assert gap["function_name"] == "fetch"
    assert gap["function_source"] == (
        "def fetch():\n    return requests.get('https://example.test')"
    )


def test_cli_json_and_gaps_output(capsys, monkeypatch):
    fake_result = {
        "requests": {
            "usage": [{
                "file": "linkwatch/client.py",
                "line": 3,
                "symbol": "requests.get",
                "kind": "call",
                "private_api": False,
                "snippet": "3 | requests.get(url)",
                "covered": None,
                "asserted": False,
                "function_name": "fetch",
                "function_source": "def fetch(): pass",
            }],
            "covered": 0,
            "asserted": 0,
            "total": 1,
        },
        "errors": [{"stage": "coverage", "error": "tests failed"}],
        "asserted_heuristic": usage.ASSERTED_HEURISTIC,
    }
    monkeypatch.setattr(
        usage,
        "_scan",
        lambda repo, names, mappings: (
            {"requests": fake_result["requests"]},
            fake_result["errors"],
        ),
    )
    monkeypatch.setattr(sys, "argv", ["usage.py", "--json"])

    usage.main()
    assert json.loads(capsys.readouterr().out) == fake_result

    monkeypatch.setattr(sys, "argv", ["usage.py", "--gaps"])
    usage.main()
    gaps_output = json.loads(capsys.readouterr().out)
    assert gaps_output == find_gaps(fake_result)


def _stub_scanner_metadata(monkeypatch, repo):
    monkeypatch.setattr(
        scanner,
        "_requirements",
        lambda root: {
            "requests": [(Requirement("requests==2.31.0"), "requirements.txt:1")]
        },
    )
    monkeypatch.setattr(scanner, "_import_names", lambda root: ({"requests"}, {}))
    monkeypatch.setattr(
        scanner,
        "_distribution_imports",
        lambda python: {"requests": {"requests"}},
    )
    monkeypatch.setattr(scanner, "_pip_check", lambda python: [])
    monkeypatch.setattr(scanner, "_installed_version", lambda python, name: "2.31.0")
    monkeypatch.setattr(scanner, "_required_by", lambda python, name: [])
    monkeypatch.setattr(scanner, "_metadata_v2", lambda *args: {})


def test_scanner_consumes_generated_usage_and_excludes_test_fragile_sites(
    tmp_path, monkeypatch
):
    package = tmp_path / "linkwatch"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "patch.py").write_text(
        "import requests\n"
        "requests.Session.request = lambda self, *args, **kwargs: None\n",
        encoding="utf-8",
    )
    (package / "cli.py").write_text(
        "from . import patch\n",
        encoding="utf-8",
    )
    (tests / "test_patch.py").write_text(
        "from requests.utils import _parse_content_type_header\n"
        "_parse_content_type_header('text/html')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(usage, "_run_coverage", lambda repo: (None, None))

    dependencies, errors = usage._scan(tmp_path, ["requests"])
    usage_path = tmp_path / "usage.json"
    usage_path.write_text(
        json.dumps({
            **dependencies,
            "errors": errors,
            "asserted_heuristic": usage.ASSERTED_HEURISTIC,
        }),
        encoding="utf-8",
    )
    _stub_scanner_metadata(monkeypatch, tmp_path)

    results = scanner.scan(
        tmp_path,
        venv_python=sys.executable,
        usage_path=usage_path,
    )

    requests_result = next(
        result for result in results if result["dependency"]["name"] == "requests"
    )
    fragile = next(
        risk for risk in requests_result["risk"]
        if risk["indicator"] == "fragile_usage"
    )
    assert {
        (site["file"], site["kind"])
        for site in fragile["value"]
    } == {
        ("linkwatch/cli.py", "side_effect_import"),
        ("linkwatch/patch.py", "monkeypatch"),
    }
    assert requests_result["score"] >= scanner.WEIGHTS["fragile_usage"]
    assert all(not site["file"].startswith("tests/") for site in fragile["value"])


def test_scanner_reports_missing_and_malformed_usage_without_crashing(
    tmp_path, monkeypatch
):
    _stub_scanner_metadata(monkeypatch, tmp_path)
    usage_files = {
        "missing.json": {},
        "malformed_site.json": {
            "requests": {"usage": [None, "malformed site record"]}
        },
    }

    for filename, data in usage_files.items():
        usage_path = tmp_path / filename
        usage_path.write_text(json.dumps(data), encoding="utf-8")
        results = scanner.scan(
            tmp_path,
            venv_python=sys.executable,
            usage_path=usage_path,
        )
        requests_result = next(
            result for result in results
            if result["dependency"]["name"] == "requests"
        )
        if filename == "missing.json":
            assert any(
                risk["indicator"] == "usage_data"
                and risk["value"] == "not_analyzed"
                for risk in requests_result["risk"]
            )
