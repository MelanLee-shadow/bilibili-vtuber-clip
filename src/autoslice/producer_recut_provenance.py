"""Write the immutable provenance sidecar for a final recut."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.producer_media import RECUT_PROVENANCE_SCHEMA, _write_json_atomic
from src.autoslice.shadow_review import _sha256


def write_final_recut_provenance(
    *,
    media_path: Path,
    padded: Path,
    padded_provenance_path: Path,
    piece_provenance_rows: Sequence[Mapping[str, object]],
    final_start: int,
    final_end: int,
    absolute_source_start_ms: int | None,
    absolute_source_end_ms: int | None,
) -> Path:
    """Seal source, interval, and output bindings after the media write."""

    provenance_path = media_path.with_suffix(".provenance.json")
    _write_json_atomic(
        provenance_path,
        {
            "schema_version": RECUT_PROVENANCE_SCHEMA,
            "source_piece": (
                piece_provenance_rows[0]
                if len(piece_provenance_rows) == 1
                else list(piece_provenance_rows)
            ),
            "padded": json.loads(padded_provenance_path.read_text(encoding="utf-8")),
            "final_recut": {
                "source_path": str(padded.resolve()),
                "source_sha256": _sha256(padded),
                "start_ms": final_start,
                "end_ms": final_end,
                "absolute_source_start_ms": absolute_source_start_ms,
                "absolute_source_end_ms": absolute_source_end_ms,
                "output_path": str(media_path.resolve()),
                "output_sha256": _sha256(media_path),
            },
        },
    )
    return provenance_path
