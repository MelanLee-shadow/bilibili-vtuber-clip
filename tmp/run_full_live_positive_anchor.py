from __future__ import annotations

import json
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, run_shadow_pipeline

BASE = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n')
SOURCE_VIDEO = BASE / 'full_replacement_source.mp4'
SOURCE_SRT = BASE / 'full_session_from_segment_srt.srt'
OUT = BASE / 'positive-anchor-05'


def copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding='utf-8'), encoding='utf-8')
    return AgyExecutionResult(provider='agy', model='screen-copy-draft', agy_rc=0, provider_fallback_used=False)


def main() -> None:
    anchor_start_ms = 4_363_000
    anchor_end_ms = 4_386_740
    summary = run_shadow_pipeline(
        source_video=SOURCE_VIDEO,
        source_srt=SOURCE_SRT,
        refined_srt=None,
        source_context_job={
            'candidate_id': 'full-live-20260617-positive-4364',
            'title': '有没有人写精彩剧情？李豆沙现场点梗同人写手',
            'duplicate_corpus': [],
            'timeline': {
                'anchor_start_ms': anchor_start_ms,
                'anchor_end_ms': anchor_end_ms,
                'context_start_ms': anchor_start_ms,
                'context_duration_ms': 120_000,
            },
        },
        source_context_agy_runner=copy_draft_runner,
        room_id='22966160',
        title='有没有人写精彩剧情？李豆沙现场点梗同人写手',
        output_dir=OUT,
        no_upload=True,
        source_context_run_ffmpeg=True,
    )
    record = summary.get('records', [{}])[0]
    print(json.dumps({
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
