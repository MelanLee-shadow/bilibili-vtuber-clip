"""Deterministic, no-symlink source inventory shared by talk and song imports."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING
import os
from pathlib import Path

from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.package_relocation_contract import (
    JsonPointer, RECORD_PATH_POINTERS, PUBLISH_PATH_POINTERS, SPEAKER_PATH_POINTERS, get_value,
)

if TYPE_CHECKING:
    from src.autoslice.package_import import PackageDocuments


def iter_source_files(
    root: Path, *, regular_file: Callable[[Path], bool]
) -> Iterator[Path]:
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                raise PackageImportError(
                    "UNSAFE_PATH_SYMLINK",
                    f"source package contains a symlinked directory: "
                    f"{current_path / name}",
                )
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise PackageImportError(
                    "UNSAFE_PATH_SYMLINK",
                    f"source package contains a symlink: {path}",
                )
            if not regular_file(path):
                raise PackageImportError(
                    "UNSAFE_SOURCE_FILE",
                    f"source package contains a non-regular file: {path}",
                )
            yield path
        directories[:] = sorted(directories)



def _referenced_locators(
    documents: PackageDocuments,
) -> list[tuple[str, JsonPointer, str]]:
    """Every locator value the relocation contract is allowed to project."""

    found: list[tuple[str, JsonPointer, str]] = []
    for kind, document, pointers in (
        ("record", documents.record, RECORD_PATH_POINTERS),
        ("publish", documents.publish, PUBLISH_PATH_POINTERS),
        (
            "speaker",
            documents.speaker if documents.speaker is not None else {},
            SPEAKER_PATH_POINTERS,
        ),
    ):
        for pointer in sorted(pointers):
            value = get_value(document, pointer)
            if isinstance(value, str) and value.startswith("/"):
                found.append((kind, pointer, value))
    embedded = documents.record.get("speaker_finalization")
    if isinstance(embedded, Mapping):
        for pointer in sorted(SPEAKER_PATH_POINTERS):
            value = get_value(embedded, pointer)
            if isinstance(value, str) and value.startswith("/"):
                found.append(("record", ("speaker_finalization", *pointer), value))
    return found
