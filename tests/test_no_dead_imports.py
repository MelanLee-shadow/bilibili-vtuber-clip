"""Check even delayed local imports without invoking providers or credentials."""

import ast
import builtins
from collections import defaultdict
import importlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _local_imports():
    imports = defaultdict(list)
    for directory in (ROOT / "src", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(ROOT)
            parts = relative.with_suffix("").parts
            package = ".".join(parts[:-1])
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if node.level:
                    module = importlib.util.resolve_name("." * node.level + module, package)
                if module.split(".")[0] not in {"src", "scripts"}:
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        imports[module].append((relative.as_posix(), node.lineno, alias.name))
    return imports


LOCAL_IMPORTS = _local_imports()


@pytest.fixture
def offline_imports(monkeypatch, tmp_path):
    # Import checks run inside pytest's existing root-conftest LLM guard.
    # Hide ambient credentials and home config; forbid network and processes.
    monkeypatch.setattr(os, "environ", {"HOME": str(tmp_path)})

    def forbidden(*args, **kwargs):
        raise AssertionError("Import scan must not use network or subprocesses")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    # Permit source/package data, never private home/config/key files.
    # /System/Library/CoreServices: macOS's read-only OS version plist, which the stdlib (platform, via zoneinfo)
    # reads on import there; it is not a home/config/key file.
    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), tmp_path,
               Path("/System/Library/CoreServices"))

    def guarded_open(original):
        def open_file(file, *args, **kwargs):
            if not isinstance(file, int):
                path = Path(file).resolve()
                if (
                    not any(path.is_relative_to(base) for base in allowed)
                    or path.name == ".env"
                    or path.name.startswith(".env.")
                    or path.suffix in {".pem", ".key"}
                ):
                    raise AssertionError("Import scan attempted a private file read")
            return original(file, *args, **kwargs)

        return open_file

    monkeypatch.setattr(builtins, "open", guarded_open(builtins.open))
    monkeypatch.setattr(io, "open", guarded_open(io.open))
    monkeypatch.setattr(os, "open", guarded_open(os.open))


def _import_or_skip(module):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        # Missing project modules/names are defects, not optional dependencies.
        if exc.name and exc.name.split(".")[0] not in {"src", "scripts"}:
            pytest.skip(f"{module}: optional dependency import failed: {exc}")
        raise


@pytest.mark.parametrize("module", sorted(LOCAL_IMPORTS))
def test_no_dead_imports(module, offline_imports):
    target = _import_or_skip(module)
    missing = []
    for path, line, name in LOCAL_IMPORTS[module]:
        if hasattr(target, name):
            continue
        child = f"{module}.{name}"
        try:
            importlib.import_module(child)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name not in {module, child}:
                _import_or_skip(child)
            missing.append(f"{path}:{line}: from {module} import {name}")
        except ImportError:
            _import_or_skip(child)
    assert not missing, "Dead local imports:\n" + "\n".join(sorted(set(missing)))


def _local_module_aliases(func, package):
    aliases = {}
    for node in ast.walk(func):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            for alias in node.names:
                name = f"{base}.{alias.name}"
                if base.split(".")[0] in {"src", "scripts"} and (ROOT / (name.replace(".", "/") + ".py")).is_file():
                    aliases[alias.asname or alias.name] = name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[0] in {"src", "scripts"}:
                    aliases[alias.asname] = alias.name
    return aliases


def _module_attribute_uses():
    """`from scripts import m as a` (or `import scripts.m as a`) inside a def, then `a.name` read in that def, or in
    a def of the same file that gets the module from a function returning it (`a = helper()`): the public tree's
    reducer once kept such a module without the names read through the alias (goldfish report 7, #53)."""
    uses = defaultdict(list)
    for directory in (ROOT / "src", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(ROOT)
            package = ".".join(relative.with_suffix("").parts[:-1])
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            funcs = [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
            returns = {}                                         # helper name -> the module it returns
            for func in funcs:
                local = _local_module_aliases(func, package)
                for node in ast.walk(func):
                    if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id in local:
                        returns[func.name] = local[node.value.id]
            for func in funcs:
                aliases = _local_module_aliases(func, package)
                for node in ast.walk(func):
                    if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name) and node.value.func.id in returns):
                        aliases.update({t.id: returns[node.value.func.id] for t in node.targets if isinstance(t, ast.Name)})
                for node in ast.walk(func):
                    if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)   # a read, not a patch
                            and isinstance(node.value, ast.Name) and node.value.id in aliases):
                        uses[aliases[node.value.id]].append((relative.as_posix(), node.lineno, node.attr))
    return uses


MODULE_ATTRIBUTE_USES = _module_attribute_uses()


@pytest.mark.parametrize("module", sorted(MODULE_ATTRIBUTE_USES))
def test_names_used_through_a_locally_imported_module_exist(module, offline_imports):
    target = _import_or_skip(module)
    missing = sorted({f"{path}:{line}: {module}.{name}" for path, line, name in MODULE_ATTRIBUTE_USES[module]
                      if not hasattr(target, name)})
    assert not missing, "Names used through a module alias that the module lacks:\n" + "\n".join(missing)
