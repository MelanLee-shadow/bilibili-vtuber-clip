"""Private artifact creation must not depend on the caller's umask."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat

import pytest

from src.autoslice import argon_escalation_storage as store
from src.autoslice.argon_regression_comparison import frozen_copy


@contextmanager
def process_umask(value):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.parametrize("mask", [0o022, 0o077])
@pytest.mark.parametrize("order", ["deep-first", "root-first", "sibling-first"])
def test_frozen_copy_creates_every_missing_parent_privately(tmp_path, mask, order):
    source = tmp_path / "original.json"
    source.write_bytes(b'{"synthetic":"no model"}\n')
    output = tmp_path / "frozen"
    variants = [
        "baseline/sample/entity_verdicts/job/input.json",
        "baseline/sample/entity_verdicts/summary.json",
        "baseline/sample/entity_verdicts/sibling/input.json",
    ]
    if order == "root-first":
        variants = [variants[1], variants[0], variants[2]]
    elif order == "sibling-first":
        variants.reverse()
    rows = []
    with process_umask(mask):
        for path in variants:
            frozen_copy(source, output / path, rows)
    directories = [output, *(p for p in output.rglob("*") if p.is_dir())]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in directories)
    assert all(store.read(Path(row["path"])) == source.read_bytes() for row in rows)
    assert len(rows) == 3


@pytest.mark.parametrize("mask", [0o022, 0o077])
@pytest.mark.parametrize("kind", ["insecure-target", "target-symlink", "parent-symlink"])
def test_existing_untrusted_directory_is_rejected_without_mutation(tmp_path, mask, kind):
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    target = owned / "unsafe"
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    if kind == "insecure-target":
        target.mkdir(mode=0o755)
        target.chmod(0o755)  # Only deliberately construct the negative fixture.
    else:
        target.symlink_to(other, target_is_directory=True)
    before = {p: (p.lstat().st_mode, p.lstat().st_ino) for p in [owned, target, other]}
    requested = target / "child" if kind == "parent-symlink" else target
    with process_umask(mask), pytest.raises(ValueError):
        store.immutable(requested / "do-not-write.json", b"{}")
    assert {p: (p.lstat().st_mode, p.lstat().st_ino) for p in before} == before
    assert not (other / "child").exists() and list(other.iterdir()) == []
    assert not (target / "do-not-write.json").exists()
