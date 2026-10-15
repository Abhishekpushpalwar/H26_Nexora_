"""Observe dependency usage in LinkWatch source and tests using the Python AST."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


# Import roots map to distribution names; keep non-identical names here.
DISTRIBUTION_NAMES = {
    "appdirs": "appdirs",
    "certifi": "certifi",
    "chardet": "chardet",
    "click": "click",
    "dateutil": "python-dateutil",
    "idna": "idna",
    "jinja2": "Jinja2",
    "nose": "nose",
    "pkg_resources": "setuptools",
    "pydantic": "pydantic",
    "PIL": "Pillow",
    "pytest": "pytest",
    "requests": "requests",
    "rich": "rich",
    "setuptools": "setuptools",
    "six": "six",
    "toml": "toml",
    "yaml": "PyYAML",
    "coverage": "coverage",
}

ASSERTED_HEURISTIC = (
    "A covered usage site is asserted when its containing function is called "
    "directly by a test, or one direct call away from a function called by a "
    "test, and that test asserts a direct call result or a local variable "
    "assigned from the tested function. This is a name-based, one-call-level "
    "heuristic; it does not prove data flow or that the assertion checks the "
    "specific dependency value."
)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _canonical_distribution_name(name: str) -> str:
    return _normalize(name)


def _distribution_name(import_root: str) -> str:
    return _canonical_distribution_name(
        DISTRIBUTION_NAMES.get(import_root, import_root)
    )


def _dependency_import_roots(
    names: set[str],
    import_mappings: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    import_mappings = {
        _canonical_distribution_name(name): import_root
        for name, import_root in (import_mappings or {}).items()
    }
    roots: dict[str, set[str]] = {}
    for name in sorted({_canonical_distribution_name(name) for name in names}):
        candidates = {
            root for root, distribution in DISTRIBUTION_NAMES.items()
            if _normalize(distribution) == _normalize(name)
        }
        if name in import_mappings:
            candidates.add(import_mappings[name])
        candidates.add(name.replace("-", "_").replace(".", "_"))
        roots[name] = candidates
    return roots


def _dependency_for_module(
    module: str, roots: dict[str, set[str]]
) -> str | None:
    import_root = module.split(".", 1)[0]
    for name in sorted(roots):
        if import_root in roots[name]:
            return name
    return None


def _python_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in ("linkwatch", "tests"):
        root = repo / dirname
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _module_name(path: Path, repo: Path) -> str:
    relative = path.relative_to(repo).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_name(path: Path, repo: Path) -> str:
    name = _module_name(path, repo)
    if path.name == "__init__.py":
        return name
    return name.rpartition(".")[0]


def _resolve_relative_module(
    path: Path, repo: Path, module: str | None, level: int
) -> str:
    if not level:
        return module or ""
    parts = _package_name(path, repo).split(".")
    keep = max(0, len(parts) - level + 1)
    prefix = parts[:keep]
    if module:
        prefix.extend(module.split("."))
    return ".".join(prefix)


def _snippet(lines: list[str], line: int) -> str:
    start = max(1, line - 2)
    end = min(len(lines), line + 2)
    width = len(str(end))
    return "\n".join(
        f"{number:>{width}} | {lines[number - 1]}"
        for number in range(start, end + 1)
    )


def _read_and_parse(path: Path) -> tuple[list[str], ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source.splitlines(), ast.parse(source, filename=str(path))


def _syntax_error(path: Path, repo: Path, error: SyntaxError) -> dict[str, str]:
    line = error.lineno or 0
    offset = error.offset or 0
    return {
        "file": path.relative_to(repo).as_posix(),
        "error": f"SyntaxError at line {line}, column {offset}: {error.msg}",
    }


def _load_sources(
    repo: Path,
) -> tuple[list[tuple[Path, list[str], ast.Module]], list[dict[str, str]]]:
    sources: list[tuple[Path, list[str], ast.Module]] = []
    errors: list[dict[str, str]] = []
    for path in _python_files(repo):
        try:
            lines, tree = _read_and_parse(path)
        except SyntaxError as error:
            errors.append(_syntax_error(path, repo, error))
            continue
        except (OSError, UnicodeError) as error:
            errors.append({
                "file": path.relative_to(repo).as_posix(),
                "error": f"{type(error).__name__}: {error}",
            })
            continue
        sources.append((path, lines, tree))
    return sources, errors


def _is_private(symbol: str) -> bool:
    return any(part.startswith("_") for part in symbol.split("."))


def _loaded_names(tree: ast.Module) -> set[str]:
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


class _AliasCollector(ast.NodeVisitor):
    """Resolve dependency aliases and simple objects assigned from dependency calls."""

    def __init__(
        self,
        tree: ast.Module,
        roots: dict[str, set[str]],
        package: str,
    ) -> None:
        self.roots = roots
        self.package = package
        self.scopes: list[str] = []
        self.aliases: dict[tuple[str, ...], dict[str, str]] = defaultdict(dict)
        self.visit(tree)

    def resolve(self, node: ast.AST, scope: tuple[str, ...] | None = None) -> str | None:
        if isinstance(node, ast.Name):
            current_scope = scope if scope is not None else tuple(self.scopes)
            for depth in range(len(current_scope), -1, -1):
                aliases = self.aliases.get(current_scope[:depth], {})
                if node.id in aliases:
                    return aliases[node.id]
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self.resolve(node.value, scope)
            return f"{parent}.{node.attr}" if parent else None
        if isinstance(node, ast.Call):
            return self.resolve(node.func, scope)
        return None

    def _bind(self, name: str, target: str) -> None:
        self.aliases[tuple(self.scopes)][name] = target

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            target = alias.name if alias.asname else local
            self._bind(local, target)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            module = self._resolve_relative(module, node.level)
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            target = f"{module}.{alias.name}" if module else alias.name
            self._bind(local, target)

    def _resolve_relative(self, module: str, level: int) -> str:
        parts = self.package.split(".") if self.package else []
        keep = max(0, len(parts) - level + 1)
        prefix = parts[:keep]
        if module:
            prefix.extend(module.split("."))
        return ".".join(prefix)

    def _dependency_call_result(self, value: ast.AST) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        called = self.resolve(value.func)
        if not called or not _dependency_for_module(called, self.roots):
            return None
        # Keep request-response attribute accesses attributable to requests.
        if called.startswith("requests.") and called.rsplit(".", 1)[-1] in {
            "delete", "get", "head", "options", "patch", "post", "put", "request",
        }:
            return "requests.Response"
        return called

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._dependency_call_result(node.value)
        if value:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bind(target.id, value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = self._dependency_call_result(node.value) if node.value else None
        if value and isinstance(node.target, ast.Name):
            self._bind(node.target.id, value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()


def _assignment_attributes(tree: ast.Module) -> set[int]:
    assigned: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    assigned.add(id(target))
    return assigned


class _UsageVisitor(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        repo: Path,
        lines: list[str],
        tree: ast.Module,
        roots: dict[str, set[str]],
    ) -> None:
        self.path = path
        self.repo = repo
        self.lines = lines
        self.roots = roots
        self.aliases = _AliasCollector(tree, roots, _package_name(path, repo))
        self.assigned_attributes = _assignment_attributes(tree)
        self.loaded_names = _loaded_names(tree)
        self.scope: list[str] = []
        self.function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.usage: list[dict[str, Any]] = []
        self.dependencies: set[str] = set()
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.relative_imports: list[tuple[str, int, ast.alias, int]] = []

    def _add(
        self,
        dependency: str,
        symbol: str,
        kind: str,
        line: int,
    ) -> None:
        self.dependencies.add(dependency)
        item = {
            "file": self.path.relative_to(self.repo).as_posix(),
            "line": line,
            "symbol": symbol,
            "kind": kind,
            "private_api": _is_private(symbol),
            "snippet": _snippet(self.lines, line),
            "covered": None,
            "asserted": False,
            "function_name": (
                self.function_stack[-1].name if self.function_stack else None
            ),
            "function_source": self._current_function_source(),
            "_function": self.scope[-1] if self.scope else None,
        }
        self.usage.append(item)
        self.records.append((dependency, item))

    def _current_function_source(self) -> str:
        if not self.function_stack:
            return ""
        function = self.function_stack[-1]
        start = function.lineno - 1
        end = function.end_lineno or function.lineno
        return "\n".join(self.lines[start:end])

    def _record_import(self, dependency: str, symbol: str, local: str, line: int) -> None:
        kind = "import" if local in self.loaded_names else "side_effect_import"
        self._add(dependency, symbol, kind, line)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            dependency = _dependency_for_module(alias.name, self.roots)
            if not dependency:
                continue
            local = alias.asname or alias.name.split(".", 1)[0]
            self._record_import(dependency, alias.name, local, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            resolved = _resolve_relative_module(
                self.path, self.repo, module, node.level
            )
            for alias in node.names:
                if alias.name != "*":
                    self.relative_imports.append(
                        (resolved, node.level, alias, node.lineno)
                    )
            return

        dependency = _dependency_for_module(module, self.roots)
        if not dependency:
            return
        for alias in node.names:
            symbol = f"{module}.{alias.name}" if alias.name != "*" else module
            local = alias.asname or alias.name
            self._record_import(dependency, symbol, local, node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        symbol = self.aliases.resolve(node, tuple(self.scope))
        dependency = _dependency_for_module(symbol or "", self.roots)
        if dependency:
            kind = (
                "monkeypatch"
                if id(node) in self.assigned_attributes
                else "attribute"
            )
            self._add(dependency, symbol or node.attr, kind, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        symbol = self.aliases.resolve(node.func, tuple(self.scope))
        dependency = _dependency_for_module(symbol or "", self.roots)
        if dependency:
            self._add(dependency, symbol or "<call>", "call", node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.function_stack.append(node)
        self.generic_visit(node)
        self.function_stack.pop()
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _local_module_dependencies(
    sources: list[tuple[Path, list[str], ast.Module]],
    repo: Path,
    roots: dict[str, set[str]],
) -> dict[str, set[str]]:
    modules: dict[str, set[str]] = defaultdict(set)
    for path, lines, tree in sources:
        visitor = _UsageVisitor(path, repo, lines, tree, roots)
        visitor.visit(tree)
        modules[_module_name(path, repo)].update(visitor.dependencies)
    return modules


def _add_relative_side_effects(
    visitors: list[_UsageVisitor],
    repo: Path,
    module_dependencies: dict[str, set[str]],
) -> None:
    for visitor in visitors:
        loaded = visitor.loaded_names
        for module, level, alias, line in visitor.relative_imports:
            local_name = alias.asname or alias.name
            if local_name in loaded:
                continue
            imported_module = ".".join(
                part for part in (module, alias.name) if part
            )
            for dependency in sorted(module_dependencies.get(imported_module, ())):
                visitor._add(
                    dependency,
                    f"{imported_module} (side-effect import)",
                    "side_effect_import",
                    line,
                )


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.names.add(node.func.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _function_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    collector = _CallCollector()
    for statement in node.body:
        collector.visit(statement)
    return collector.names


def _asserted_function_sites(
    sources: list[tuple[Path, list[str], ast.Module]],
    repo: Path,
) -> set[tuple[str, str]]:
    """Find function definitions satisfying the test-assertion heuristic."""
    definitions: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]] = (
        defaultdict(list)
    )
    test_functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for path, _, tree in sources:
        relative = path.relative_to(repo).as_posix()
        is_test = relative.startswith("tests/")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if is_test and node.name.startswith("test"):
                test_functions.append(node)
            elif not is_test:
                definitions[node.name].append((relative, node))

    asserted_roots: set[str] = set()
    for test_function in test_functions:
        assigned_calls: dict[str, str] = {}
        for statement in ast.walk(test_function):
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            value = statement.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [
                statement.target
            ]
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned_calls[target.id] = value.func.id

        directly_asserted: set[str] = set()
        for statement in ast.walk(test_function):
            if not isinstance(statement, ast.Assert):
                continue
            for expression in ast.walk(statement.test):
                if isinstance(expression, ast.Call):
                    if isinstance(expression.func, ast.Name):
                        directly_asserted.add(expression.func.id)
                    elif isinstance(expression.func, ast.Attribute):
                        directly_asserted.add(expression.func.attr)
                elif isinstance(expression, ast.Name):
                    assigned_call = assigned_calls.get(expression.id)
                    if assigned_call:
                        directly_asserted.add(assigned_call)

        asserted_roots.update(directly_asserted)

    qualifying: set[tuple[str, str]] = set()
    for root in asserted_roots:
        for relative, definition in definitions.get(root, ()):
            qualifying.add((relative, definition.name))
            for called_name in _function_calls(definition):
                for called_relative, called_definition in definitions.get(
                    called_name, ()
                ):
                    qualifying.add((called_relative, called_definition.name))
    return qualifying


def _coverage_file_path(filename: str, repo: Path) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = repo / path
    return str(path.resolve())


def _run_coverage(
    repo: Path,
) -> tuple[dict[str, set[int]] | None, str | None]:
    """Run baseline tests and return executed source lines from coverage JSON."""
    with tempfile.TemporaryDirectory(prefix="linkwatch-coverage-") as temp_dir:
        coverage_file = Path(temp_dir) / ".coverage"
        json_file = Path(temp_dir) / "coverage.json"
        env = os.environ.copy()
        env["COVERAGE_FILE"] = str(coverage_file)

        run_command = [sys.executable, "-m", "coverage", "run", "-m", "pytest"]
        try:
            run = subprocess.run(
                run_command,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            return None, (
                f"Could not execute `coverage run -m pytest`: "
                f"{type(error).__name__}: {error}"
            )
        test_error = None
        if run.returncode:
            details = (run.stdout + run.stderr).strip()
            test_error = (
                f"`coverage run -m pytest` exited with code {run.returncode}."
                + (f"\n{details}" if details else "")
            )
            if not coverage_file.is_file():
                return None, test_error

        json_command = [
            sys.executable, "-m", "coverage", "json", "-o", str(json_file),
        ]
        try:
            report = subprocess.run(
                json_command,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            return None, (
                f"Could not execute `coverage json`: "
                f"{type(error).__name__}: {error}"
            )
        if report.returncode:
            details = (report.stdout + report.stderr).strip()
            return None, (
                f"`coverage json` exited with code {report.returncode}."
                + (f"\n{details}" if details else "")
            )
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return None, (
                f"Could not read coverage JSON: {type(error).__name__}: {error}"
            )
        files = data.get("files")
        if not isinstance(files, dict):
            return None, "Coverage JSON does not contain a valid `files` mapping."
        executed: dict[str, set[int]] = {}
        for filename, details in files.items():
            if isinstance(details, dict):
                executed[_coverage_file_path(filename, repo)] = set(
                    details.get("executed_lines", ())
                )
        return executed, test_error


def _scan(
    repo: Path,
    names: list[str],
    import_mappings: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sources, errors = _load_sources(repo)
    coverage, coverage_error = _run_coverage(repo)
    if coverage_error:
        errors.append({
            "stage": "baseline_tests" if coverage is not None else "coverage",
            "error": coverage_error,
        })
    canonical_names = sorted({_canonical_distribution_name(name) for name in names})
    roots = _dependency_import_roots(set(canonical_names), import_mappings)
    dependencies: dict[str, dict[str, Any]] = {
        name: {
            "usage": [],
            "covered": None,
            "asserted": 0,
            "total": 0,
        }
        for name in canonical_names
    }
    visitors: list[_UsageVisitor] = []
    for path, lines, tree in sources:
        visitor = _UsageVisitor(path, repo, lines, tree, roots)
        visitor.visit(tree)
        visitors.append(visitor)
    module_dependencies = _local_module_dependencies(sources, repo, roots)
    _add_relative_side_effects(visitors, repo, module_dependencies)
    asserted_functions = _asserted_function_sites(sources, repo)

    for visitor in visitors:
        for dependency, item in visitor.records:
            if coverage is not None:
                source_path = str((repo / item["file"]).resolve())
                item["covered"] = item["line"] in coverage.get(source_path, set())
            function_name = item.pop("_function", None)
            item["asserted"] = (
                item["covered"] is True
                and function_name is not None
                and (item["file"], function_name) in asserted_functions
            )
            dependencies[dependency]["usage"].append(item)
    for dependency in dependencies.values():
        dependency["usage"].sort(
            key=lambda item: (item["file"], item["line"], item["kind"], item["symbol"])
        )
        dependency["total"] = len(dependency["usage"])
        dependency["covered"] = sum(
            item["covered"] is True for item in dependency["usage"]
        )
        dependency["asserted"] = sum(
            item["asserted"] is True for item in dependency["usage"]
        )
    return dependencies, errors


def find_usage(repo_path: str, names: list[str]) -> dict[str, Any]:
    """Return observed AST usage for every requested dependency."""
    repo = Path(repo_path).resolve()
    dependencies, errors = _scan(repo, names)
    dependencies["errors"] = errors
    dependencies["asserted_heuristic"] = ASSERTED_HEURISTIC
    return dependencies


def find_gaps(usage_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return observed uncovered or unasserted usage sites for hand-off."""
    gaps: list[dict[str, Any]] = []
    for dependency in sorted(usage_result):
        if dependency in {"errors", "asserted_heuristic"}:
            continue
        entry = usage_result[dependency]
        for site in entry.get("usage", []):
            covered = site.get("covered")
            asserted = site.get("asserted")
            if not (covered is False or (covered is True and asserted is False)):
                continue
            gaps.append({
                "dep": dependency,
                "file": site["file"],
                "line": site["line"],
                "function_name": site.get("function_name"),
                "function_source": site.get("function_source", ""),
                "snippet": site["snippet"],
            })
    return sorted(
        gaps,
        key=lambda gap: (
            gap["dep"], gap["file"], gap["line"],
            gap["function_name"] or "", gap["snippet"],
        ),
    )


def _project_roots(repo: Path) -> set[str]:
    roots = set(sys.stdlib_module_names)
    roots.update(path.stem for path in repo.glob("*.py"))
    roots.update(path.name for path in repo.iterdir() if path.is_dir())
    roots.add("linkwatch")
    return roots


def list_imports(
    repo_path: str,
    import_mappings: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return observed third-party import locations for the scanner hand-off."""
    repo = Path(repo_path).resolve()
    sources, _ = _load_sources(repo)
    project_roots = _project_roots(repo)
    imports: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for path, _, tree in sources:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                module_names = [
                    f"{module}.{alias.name}" if alias.name != "*" else module
                    for alias in node.names
                ]
            else:
                continue
            for module in module_names:
                root = module.split(".", 1)[0]
                if root in project_roots:
                    continue
                mapping = import_mappings or {}
                reverse_mapping = {
                    import_root: distribution
                    for distribution, import_root in mapping.items()
                }
                name = _canonical_distribution_name(
                    reverse_mapping.get(root, _distribution_name(root))
                )
                key = (name, path.relative_to(repo).as_posix(), node.lineno)
                if key not in seen:
                    seen.add(key)
                    imports.append({
                        "name": name,
                        "file": key[1],
                        "line": key[2],
                    })
    return sorted(imports, key=lambda item: (item["file"], item["line"], item["name"]))


def _declared_names(
    repo: Path,
    import_mappings: dict[str, str] | None = None,
) -> list[str]:
    names: set[str] = set()
    requirement_pattern = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
    for filename in ("requirements.txt", "requirements-dev.txt"):
        path = repo / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = requirement_pattern.match(line.split("#", 1)[0])
            if match:
                names.add(_canonical_distribution_name(match.group(1)))
    for item in list_imports(str(repo), import_mappings):
        names.add(item["name"])
    return sorted(names)


def _parse_dependency_args(value: str | None) -> list[str] | None:
    if value is None:
        return None
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("--deps must list at least one dependency")
    return names


def _parse_mapping_args(values: list[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(
                f"invalid --map value {value!r}; expected distribution=import_root"
            )
        distribution, import_root = (part.strip() for part in value.split("=", 1))
        if not distribution or not import_root:
            raise argparse.ArgumentTypeError(
                f"invalid --map value {value!r}; names cannot be empty"
            )
        mappings[_canonical_distribution_name(distribution)] = import_root
    return mappings


def _print_report(results: dict[str, Any]) -> None:
    overall = 0
    overall_covered = 0
    coverage_available = not any(
        error.get("stage") == "coverage" for error in results.get("errors", [])
    )
    for name, dependency in results.items():
        if name in {"errors", "asserted_heuristic"}:
            continue
        covered_count = dependency["covered"]
        if coverage_available:
            coverage_summary = f"{covered_count}/{dependency['total']} covered"
            overall_covered += covered_count
        else:
            coverage_summary = f"coverage unavailable/{dependency['total']}"
        print(
            f"\n{name} ({coverage_summary}; "
            f"{dependency['asserted']} asserted by heuristic)"
        )
        overall += dependency["total"]
        for item in dependency["usage"]:
            covered = item["covered"]
            covered_text = "unavailable" if covered is None else str(covered).lower()
            print(
                f"{item['file']}:{item['line']} | {item['symbol']} | "
                f"covered={covered_text} | "
                f"asserted={str(item['asserted']).lower()} | "
                f"{item['snippet'].splitlines()[0]}"
            )
    if coverage_available:
        print(
            f"\nusage_sites_covered: {overall_covered}/{overall} "
            f"({overall} usage sites found)"
        )
    else:
        print(
            f"\nusage_sites_covered: unavailable "
            f"({overall} usage sites found)"
        )
    if results.get("errors"):
        print("\nAnalysis errors:")
        for error in results["errors"]:
            location = f"{error['file']}: " if "file" in error else ""
            stage = f"{error['stage']}: " if "stage" in error else ""
            print(f"{location}{stage}{error['error']}")
    print(f"\nasserted_heuristic: {results['asserted_heuristic']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo_path",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--json",
        action="store_true",
        help="print the complete usage analysis as JSON",
    )
    output_group.add_argument(
        "--gaps",
        action="store_true",
        help="print uncovered/unasserted usage for Person C",
    )
    parser.add_argument(
        "--deps",
        help="comma-separated distribution names to analyze (overrides discovered dependencies)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="DISTRIBUTION=IMPORT_ROOT",
        help="map a distribution name to its Python import root; may be repeated",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write JSON output to this path (implies --json)",
    )
    args = parser.parse_args()
    repo = args.repo_path.resolve()
    try:
        import_mappings = _parse_mapping_args(args.map)
        dependency_names = _parse_dependency_args(args.deps)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if dependency_names is None:
        dependency_names = _declared_names(repo, import_mappings)
    dependencies, errors = _scan(repo, dependency_names, import_mappings)
    dependencies["errors"] = errors
    dependencies["asserted_heuristic"] = ASSERTED_HEURISTIC
    results = dependencies
    if args.out and args.gaps:
        parser.error("--out writes JSON and cannot be combined with --gaps")
    if args.out:
        args.json = True
    if args.json:
        rendered = json.dumps(results, indent=2, sort_keys=True)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    elif args.gaps:
        print(json.dumps(find_gaps(results), indent=2, sort_keys=True))
    else:
        _print_report(results)


if __name__ == "__main__":
    main()
