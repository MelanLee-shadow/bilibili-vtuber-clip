from __future__ import annotations

import json
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, _parse_srt, run_shadow_pipeline
from src.autoslice.full_session_candidate_selector import select_full_session_candidates

BASE = Path('/app/Videos/22966160/2026-06-19/official_source')
SOURCE_VIDEO = BASE / 'sources/22966160_20260619-19-30-01.clean-single-durl720.mp4'
SOURCE_SRT = BASE / 'sources/22966160_20260619-19-30-01.srt'
OUT = Path('/app/reports/auto_review_shadow/full-live-20260619-selector-positive-02')
SOURCE_DURATION_MS = 9_308_414


def copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding='utf-8'), encoding='utf-8')
    return AgyExecutionResult(provider='agy', model='selector-copy-draft', agy_rc=0, provider_fallback_used=False)


def main() -> None:
    candidates = select_full_session_candidates(_parse_srt(SOURCE_SRT), max_candidates=10)
    candidate = candidates[0]
    job = candidate.to_source_context_job(source_duration_ms=SOURCE_DURATION_MS)
    job['job_id'] = 'full-live-20260619-selector-positive-01'
    job['room_id'] = '22966160'
    job['date'] = '2026-06-19'
    job['source_integrity'] = {
        'session_date': '2026-06-19',
        'expected_start_ms': 0,
        'expected_end_ms': SOURCE_DURATION_MS,
    }
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
