from __future__ import annotations

import json
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, _parse_srt, run_shadow_pipeline
from src.autoslice.full_session_candidate_selector import select_full_session_candidates

BASE = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n')
SOURCE_VIDEO = BASE / 'full_replacement_source.mp4'
SOURCE_SRT = BASE / 'full_session_from_segment_srt.srt'
OUT = BASE / 'selector-positive-02'


def copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding='utf-8'), encoding='utf-8')
    return AgyExecutionResult(provider='agy', model='selector-copy-draft', agy_rc=0, provider_fallback_used=False)


def main() -> None:
    candidates = select_full_session_candidates(_parse_srt(SOURCE_SRT), max_candidates=10)
    candidate = candidates[1]
    job = candidate.to_source_context_job(source_duration_ms=11_266_416)
    job['duplicate_corpus'] = [other.anchor.candidate_id for other in candidates if other.anchor.candidate_id != candidate.anchor.candidate_id]
    summary = run_shadow_pipeline(
        source_video=SOURCE_VIDEO,
        source_srt=SOURCE_SRT,
        refined_srt=None,
        source_context_job=job,
        source_context_agy_runner=copy_draft_runner,
        room_id='22966160',
        title=candidate.text_preview[:80],
        output_dir=OUT,
        no_upload=True,
        source_context_run_ffmpeg=True,
    )
    record = summary.get('records', [{}])[0]
    print(json.dumps({
        'selected_candidate': candidate.to_manifest(),
        'output_dir': str(OUT),
        'counts': summary.get('counts'),
        'validations': summary.get('validations'),
        'decision_action': record.get('decision_action'),
        'reason_codes': record.get('reason_codes'),
        'boundary_resolution': record.get('boundary_resolution'),
        'materialized_recut': record.get('materialized_recut'),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
