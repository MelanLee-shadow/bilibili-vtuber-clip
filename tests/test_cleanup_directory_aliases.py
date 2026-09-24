"""Reference completeness through bounded sibling aliases; synthetic files only."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
planner = importlib.import_module("cleanup_preflight_scan")


def tree(tmp_path):
    base = tmp_path / "runtime"
    parent = base / "reports" / "environment"
    library = parent / "lib"
    library.mkdir(parents=True)
    target = base / "out/2026-01-01/auto_100000_1_2/working.wav"
    doc = library / "authority.json"
    doc.write_text(json.dumps({"input": str(target)}))
    alias = parent / "lib64"
    alias.symlink_to("lib", target_is_directory=True)
    return base, parent, library, alias, doc, target


@pytest.mark.parametrize("alias_first", [True, False])
def test_sibling_alias_keeps_references_and_reads_body_once(tmp_path, monkeypatch, alias_first):
    base, parent, library, alias, doc, target = tree(tmp_path)
    real_walk, real_read = os.walk, planner._authority_text
    reads = []
    before = doc.read_bytes(), alias.lstat().st_ino, os.readlink(alias)

    def walk(*args, **kwargs):
        for root, directories, files in real_walk(*args, **kwargs):
            if root == str(parent):
                directories.sort(reverse=alias_first)
            yield root, directories, files

    def read(path, info):
        reads.append(path)
        return real_read(path, info)

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(planner, "_authority_text", read)
    refs, dirs = planner.authority_references(str(base))
    assert refs == {str(target)} and dirs == set()
    assert reads == [str(doc)]
    assert before == (doc.read_bytes(), alias.lstat().st_ino, os.readlink(alias))


def test_multiple_aliases_do_not_drop_or_duplicate_the_target(tmp_path, monkeypatch):
    base, parent, library, alias, doc, target = tree(tmp_path)
    (parent / "other-library").symlink_to("lib", target_is_directory=True)
    assert planner.authority_references(str(base))[0] == {str(target)}


@pytest.mark.parametrize("kind", ["absolute", "parent", "child", "dot", "self", "cycle", "chain", "missing", "file"])
def test_non_sibling_or_non_directory_aliases_still_fail(tmp_path, kind):
    base, parent, library, alias, doc, target = tree(tmp_path)
    alias.unlink()
    if kind == "absolute":
        link = str(library)
    elif kind == "parent":
        link = "../environment/lib"
    elif kind == "child":
        (library / "child").mkdir()
        link = "lib/child"
    elif kind == "dot":
        link = "."
    elif kind == "self":
        link = "unsafe.json"
    elif kind == "cycle":
        (parent / "other").symlink_to("unsafe.json")
        link = "other"
    elif kind == "chain":
        (parent / "other").symlink_to("lib", target_is_directory=True)
        link = "other"
    elif kind == "missing":
        link = "absent"
    else:
        (parent / "ordinary.json").write_text("{}")
        link = "ordinary.json"
    # A dangling directory link has no trustworthy directory type. Give its
    # filename a text suffix too, so existing strict file classification sees it.
    if kind in {"self", "cycle", "missing", "file"}:
        alias = parent / "unsafe.json"
    alias.symlink_to(link, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


@pytest.mark.parametrize("fault", ["invalid_utf8", "oversize", "external_file_link", "fifo"])
def test_alias_target_contents_remain_strict(tmp_path, fault):
    base, parent, library, alias, doc, target = tree(tmp_path)
    if fault == "invalid_utf8":
        doc.write_bytes(b"\xff")
    elif fault == "oversize":
        with doc.open("r+b") as f:
            f.truncate(planner.MAX_AUTHORITY_BYTES + 1)
    elif fault == "external_file_link":
        external = tmp_path / "outside.json"
        external.write_text(str(target))
        doc.unlink()
        doc.symlink_to(external)
    else:
        doc.unlink()
        os.mkfifo(doc)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


@pytest.mark.parametrize("mutation", ["link", "target_namespace", "target_identity"])
def test_alias_and_target_drift_are_rejected(tmp_path, monkeypatch, mutation):
    base, parent, library, alias, doc, target = tree(tmp_path)
    other = parent / "other"
    other.mkdir()
    real_read = planner._authority_text
    changed = False

    def read(path, info):
        nonlocal changed
        result = real_read(path, info)
        if not changed:
            changed = True
            if mutation == "link":
                alias.unlink()
                alias.symlink_to("other", target_is_directory=True)
            elif mutation == "target_namespace":
                (library / "new.json").write_text("{}")
            else:
                library.rename(parent / "old-library")
                library.mkdir()
        return result

    monkeypatch.setattr(planner, "_authority_text", read)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert changed  # The guard must read the target, not fail merely on its alias.


def test_alias_requires_actual_target_walk_not_just_a_stat(tmp_path, monkeypatch):
    base, parent, library, alias, doc, target = tree(tmp_path)
    real_walk = os.walk

    def missing_target_walk(*args, **kwargs):
        for row in real_walk(*args, **kwargs):
            if row[0] != str(library):
                yield row

    monkeypatch.setattr(os, "walk", missing_target_walk)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_unreadable_target_directory_is_not_hidden_by_alias(tmp_path, monkeypatch):
    base, parent, library, alias, doc, target = tree(tmp_path)
    real_scandir = os.scandir

    def scandir(path):
        if os.fspath(path) == str(library):
            raise PermissionError(13, "synthetic target read failure", str(library))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_alias_does_not_protect_a_cited_file_from_the_reference_matcher(tmp_path, monkeypatch):
    base, parent, library, alias, doc, target = tree(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"retained")
    state = base / "state/2026-01-01.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"picks": [{"candidate_id": "auto_100000_1_2", "status": "published"}]}))
    (base / "DISABLED").touch()
    plan = tmp_path / "plan.json"
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)])
    assert planner.main() == 0
    assert json.loads(plan.read_text()) == []
    assert target.read_bytes() == b"retained"
