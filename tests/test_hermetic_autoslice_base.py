"""No test reads or writes the production base, on any host (root conftest; src/autoslice/base_default.py).

the suite unsets AUTOSLICE_BASE per test; the code then defaulted to /opt/bilive/autoslice, which is absent
on a developer machine and present on the production host, where 80-odd tests of a full run read the production runtime
publication registry, receipts and lock files.  The default now has one definition, and the suite points it at a path
that does not exist and cannot be created."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from src.autoslice import base_default, publication_registry

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = Path("/opt/bilive")


def test_an_unset_base_falls_to_a_path_that_is_not_there_and_cannot_be_made():
    assert "AUTOSLICE_BASE" not in os.environ                      # unset stays unset: no cache, no persistence
    base = Path(base_default.default_base())
    assert not base.exists()                                       # as /opt/bilive/autoslice on a developer machine
    assert PRODUCTION not in base.resolve().parents and base.resolve() != PRODUCTION
    if os.geteuid() != 0:                                          # root writes anywhere; the suite is not run as root
        with pytest.raises(OSError):
            (base / "state").mkdir(parents=True)


def test_a_reader_of_the_default_base_does_not_reach_production():
    path = publication_registry._runtime_registry_path()
    assert PRODUCTION not in path.resolve().parents and not path.exists()
    assert publication_registry.load_publication_registry()["entries"]     # the committed registry alone, on every host


def test_production_sets_neither_variable_and_gets_the_production_base(monkeypatch):
    monkeypatch.delenv(base_default.DEFAULT_BASE_ENV)
    assert base_default.default_base() == "/opt/bilive/autoslice"


def test_no_code_spells_the_production_default_of_the_base_itself():
    spelled = re.compile(r"""AUTOSLICE_BASE["']\s*,\s*["']/opt/bilive""")
    hits = [str(p.relative_to(ROOT)) for folder in ("src", "scripts") for p in (ROOT / folder).rglob("*.py")
            if spelled.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert not hits, f"use base_default.default_base() as the default of AUTOSLICE_BASE: {hits}"
