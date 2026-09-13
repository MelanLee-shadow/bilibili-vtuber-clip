"""Deterministic, no-symlink source inventory shared by talk and song imports."""

from collections.abc import Callable, Iterator
import os
from pathlib import Path

from src.autoslice.failed_pick_import import PackageImportError


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
