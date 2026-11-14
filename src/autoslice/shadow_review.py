"""Review-package evidence, decision, and marker records for shadow runs.

The CLI module re-exports these names for historical callers. This module is
side-effect free apart from the explicitly requested local evidence writes.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from pathlib import Path


def _sha256(path: Path) -> str:
    # This shared reader checks current artifacts/provenance, not immutable
    # cold snapshots. Re-read bytes even when a rewrite shares the stat key.
    digest = _evidence_file_sha256(path, fresh=True)
    return digest


