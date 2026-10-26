"""Autoslice package; deployed imports hold a shared repository-code lease."""
from pathlib import Path as _Path

_repo = _Path(__file__).resolve().parents[2]
if (_repo / "DEPLOYED_COMMIT").is_file():
    import runpy as _runpy
    _runpy.run_path(str(_repo.parent / "runtime-code-lease.py"))["ensure_import_code_lease"](_repo)
