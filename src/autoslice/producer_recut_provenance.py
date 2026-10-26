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


def prepared_boundary_clock_binding(
    *, media_path: Path, completed_record: Mapping, clip_context: Mapping, candidate_id: str,
) -> dict[str, object]:
    """Use the native source-chain validator to map only source-boundary time.

    A matching duration is never a source proof. The physical audio clock stays
    on the supplied media; this offset applies solely to the original source
    review's endpoint comparison, before a prepared reviewer makes any call.
    """
    from src.autoslice.independent_context_adjudication import (
        _file_sha256, _range_media_source_binding,
    )
    from src.autoslice.boundary_semantic_review import semantic_review_sha256
    from src.autoslice.local_asr_target_evidence import digest

    media = str(media_path)
    media_sha = _file_sha256(media)
    sources = sorted({str(p['source_media_sha256'])
                      for p in clip_context.get('pieces', [])
                      if isinstance(p, Mapping) and isinstance(p.get('source_media_sha256'), str)})
    if not media_sha or not sources:
        raise ValueError('PREPARED_BOUNDARY_SOURCE_BINDING_REQUIRED')
    source = _range_media_source_binding(
        {'source_media_sha256': media_sha, 'candidate_id': candidate_id,
         'clip_context_binding': {'source_media_sha256s': sources}}, media, media_sha,
    )
    if source is None:
        raise ValueError('PREPARED_BOUNDARY_SOURCE_BINDING_INVALID')
    boundary = completed_record.get('boundary_audit') or {}
    review = boundary.get('boundary_semantic_review') or {}
    endpoint = review.get('final_endpoint_binding') or {}
    start, end = endpoint.get('final_start_ms'), endpoint.get('final_end_ms')
    if (not isinstance(candidate_id, str) or not candidate_id
            or clip_context.get('candidate_id') != candidate_id
            or ('candidate_id' in completed_record and completed_record['candidate_id'] != candidate_id)):
        raise ValueError('PREPARED_BOUNDARY_CANDIDATE_IDENTITY_DRIFT')
    if source['role'] == 'PRODUCER_FINAL_RECUT_FROM_RECORDING':
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or endpoint.get('status') != 'PASS'
                or (boundary.get('final_start_ms'), boundary.get('final_end_ms')) != (start, end)
                or review.get('candidate_id') != candidate_id):
            raise ValueError('PREPARED_BOUNDARY_SOURCE_ENDPOINT_INVALID')
        interval = source['final_recut_interval_ms']
        if interval != [start, end]:
            raise ValueError('PREPARED_BOUNDARY_RECUT_ENDPOINT_DRIFT')
        offset = interval[0]
    elif source['role'] in {'DIRECT_RECORDING_MEDIA', 'PRODUCER_PADDED_MEDIA_FROM_RECORDING',
                            'PRODUCER_PADDED_MEDIA_FROM_RECORDING_CHAIN'}:
        offset = 0  # Verified unrecut media: source and physical origins coincide.
    else:
        raise ValueError('PREPARED_BOUNDARY_SOURCE_CLOCK_UNKNOWN')
    materialization = source.get('materialization')
    if materialization is not None:
        from src.autoslice.media_copy_provenance import prepared_identity
        identity = prepared_identity(completed_record=completed_record,
                                     clip_context=clip_context, candidate_id=candidate_id)
        original_clock = materialization['source_clock']
        if (materialization['prepared_identity'] != identity
                or original_clock['source_endpoint_ms'] != [start, end]
                or original_clock['source_review_sha256'] != semantic_review_sha256(review)
                or original_clock['source_boundary_timeline_offset_ms'] != offset
                or original_clock['acoustic_timeline_offset_changed'] is not False):
            raise ValueError('PREPARED_MATERIALIZED_SOURCE_IDENTITY_DRIFT')
    receipt = {
        'schema_version': 'prepared-source-boundary-clock.v1',
        'candidate_id': candidate_id,
        'media_path': media, 'media_sha256': media_sha,
        'source_binding': source,
        'source_review_sha256': semantic_review_sha256(review),
        'source_endpoint_ms': [start, end],
        'source_boundary_timeline_offset_ms': offset,
        'acoustic_timeline_offset_changed': False,
        'authority': 'SOURCE_CLOCK_ONLY', 'release_authorized': False,
    }
    receipt['receipt_sha256'] = digest(receipt)
    return receipt
