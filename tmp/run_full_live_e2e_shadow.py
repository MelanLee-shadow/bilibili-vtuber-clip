from __future__ import annotations

import json
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, run_shadow_pipeline

OUT = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n/e2e-shadow-04')
SOURCE_VIDEO = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n/full_replacement_source.mp4')
SOURCE_SRT = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n/full_session_from_segment_srt.srt')


def main() -> None:
    anchor_start_ms = 3_668_000
    anchor_end_ms = 3_728_000
    summary = run_shadow_pipeline(
        source_video=SOURCE_VIDEO,
        source_srt=SOURCE_SRT,
        refined_srt=SOURCE_SRT,
        source_context_job={
            'candidate_id': 'full-live-20260617-anchor-21-01-manual',
            'title': 'full live acceptance 2026-06-17 21:01 anchor',
            'timeline': {
                'anchor_start_ms': anchor_start_ms,
                'anchor_end_ms': anchor_end_ms,
                'context_start_ms': max(0, anchor_start_ms - 30_000),
                'context_duration_ms': 180_000,
            },
        },
        agy_result=AgyExecutionResult(
            provider='agy',
            model='preexisting-full-session-srt',
            agy_rc=0,
            provider_fallback_used=False,
        ),
        room_id='22966160',
        title='full live acceptance 2026-06-17 21:01 anchor',
        output_dir=OUT,
        no_upload=True,
        source_context_run_ffmpeg=True,
    )
    record = summary.get('records', [{}])[0] if summary.get('records') else {}
    print(json.dumps({
        'output_dir': str(OUT),
        'counts': summary.get('counts'),
        'gap_summary': summary.get('gap_summary'),
        'validations': summary.get('validations'),
        'record_action': record.get('action'),
        'record_reason_codes': record.get('reason_codes'),
        'materialized_recut': record.get('materialized_recut'),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
