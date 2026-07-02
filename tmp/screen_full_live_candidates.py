from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, run_shadow_pipeline

BASE = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n')
SOURCE_VIDEO = BASE / 'full_replacement_source.mp4'
SOURCE_SRT = BASE / 'full_session_from_segment_srt.srt'
DATE_DIR = Path('/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-06-17')
OUT_ROOT = BASE / 'screen-candidates-03'
BASE_CLOCK = datetime(2026, 6, 17, 20, 1, 17)
STEM_RE = re.compile(r'(?P<prefix>\d+)s_(?P<kind>semantic|manual)_22966160_(?P<date>\d{4}-\d{2}-\d{2})-(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})-')

PREFERRED_STEMS = [
    '222s_semantic_22966160_2026-06-17-21-01-14-',
    '81s_semantic_22966160_2026-06-17-21-01-14-',
    '1002s_semantic_22966160_2026-06-17-21-01-14-',
    '71s_manual_22966160_2026-06-17-21-01-14-',
    '194s_semantic_22966160_2026-06-17-22-58-56-',
    '255s_semantic_22966160_2026-06-17-22-58-56-',
    '35s_semantic_22966160_2026-06-17-22-28-57-',
    '452s_semantic_22966160_2026-06-17-22-28-57-',
]


def parse_ms(ts: str) -> int:
    return ((int(ts[0:2]) * 60 + int(ts[3:5])) * 60 + int(ts[6:8])) * 1000 + int(ts[9:12])


def last_srt_end_ms(path: Path) -> int:
    times = [line for line in path.read_text(encoding='utf-8', errors='replace').splitlines() if '-->' in line]
    if not times:
        return 60_000
    return parse_ms(times[-1].split('-->', 1)[1].strip())


def anchor_for_stem(stem: str) -> tuple[int, int] | None:
    match = STEM_RE.match(stem)
    if match is None:
        return None
    start_clock = datetime.strptime(
        f"{match.group('date')} {match.group('h')}:{match.group('m')}:{match.group('s')}",
        '%Y-%m-%d %H:%M:%S',
    )
    segment_offset_ms = int((start_clock - BASE_CLOCK).total_seconds() * 1000)
    prefix_ms = int(match.group('prefix')) * 1000
    srt_path = DATE_DIR / 'subtitles' / f'{stem}.srt'
    if not srt_path.is_file():
        return None
    start_ms = segment_offset_ms + prefix_ms
    end_ms = start_ms + last_srt_end_ms(srt_path)
    return max(0, start_ms), max(start_ms + 1000, end_ms)


def copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding='utf-8'), encoding='utf-8')
    return AgyExecutionResult(provider='agy', model='screen-copy-draft', agy_rc=0, provider_fallback_used=False)


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for stem in PREFERRED_STEMS:
        anchors = anchor_for_stem(stem)
        if anchors is None:
            rows.append({'stem': stem, 'status': 'SKIPPED_NO_ANCHOR'})
            continue
        anchor_start_ms, anchor_end_ms = anchors
        context_start_ms = max(0, anchor_start_ms - 30_000)
        context_end_ms = min(anchor_end_ms + 90_000, context_start_ms + 240_000)
        out = OUT_ROOT / stem
        summary = run_shadow_pipeline(
            source_video=SOURCE_VIDEO,
            source_srt=SOURCE_SRT,
            refined_srt=None,
            source_context_job={
                'candidate_id': stem,
                'title': stem,
                'duplicate_corpus': [other for other in PREFERRED_STEMS if other != stem],
                'timeline': {
                    'anchor_start_ms': anchor_start_ms,
                    'anchor_end_ms': anchor_end_ms,
                    'context_start_ms': context_start_ms,
                    'context_duration_ms': context_end_ms - context_start_ms,
                },
            },
            source_context_agy_runner=copy_draft_runner,
            room_id='22966160',
            title=stem,
            output_dir=out,
            no_upload=True,
            source_context_run_ffmpeg=True,
        )
        record = summary.get('records', [{}])[0] if summary.get('records') else {}
        rows.append({
            'stem': stem,
            'output_dir': str(out),
            'anchor_start_ms': anchor_start_ms,
            'anchor_end_ms': anchor_end_ms,
            'decision_action': record.get('decision_action'),
            'reason_codes': record.get('reason_codes'),
            'boundary_resolution': record.get('boundary_resolution'),
            'materialized_recut': record.get('materialized_recut'),
            'counts': summary.get('counts'),
            'validations': summary.get('validations'),
        })
    report = {'schema_version': 'full-live-screen-candidates.v1', 'rows': rows}
    (OUT_ROOT / 'screen_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])


if __name__ == '__main__':
    main()
