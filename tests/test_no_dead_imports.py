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
    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), tmp_path)

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
