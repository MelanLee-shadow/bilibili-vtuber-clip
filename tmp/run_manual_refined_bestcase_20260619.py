from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, _parse_srt, run_shadow_pipeline
from src.autoslice.cpa_semantic_qa import (
    CpaSemanticQaRequest,
    CpaSemanticQaResponse,
    CpaSemanticSourceRef,
    CpaTerminologyContext,
    write_cpa_semantic_request_artifact,
    write_cpa_semantic_response_artifact,
)

SOURCE_VIDEO = Path('/app/Videos/22966160/2026-06-19/official_source/sources/22966160_20260619-19-30-01.clean-single-durl720.mp4')
SOURCE_SRT = Path('/app/Videos/22966160/2026-06-19/official_source/sources/22966160_20260619-19-30-01.srt')
MANUAL_SRT = Path('/app/Videos/22966160/2026-06-19/official_source/manual_recuts/315s_cry_expression/315s_manual_22966160_2026-06-19-19-30-01-cry-expression.manual-edited.zh.srt')
OUT = Path('/app/reports/auto_review_shadow/backtest-20260619-manual-refined-bestcase-315-438-01')
CANDIDATE_ID = 'manual_refined_bestcase_315000_438000'
START_MS = 315_000
END_MS = 438_000


def manual_refined_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    shutil.copy2(MANUAL_SRT, output_srt_path)
    return AgyExecutionResult(provider='agy', model='manual-edited-bestcase-runner', agy_rc=0, provider_fallback_used=False)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cues = _parse_srt(SOURCE_SRT)
    source_duration_ms = max(cue.source_end_ms for cue in cues)
    manual_text = ' '.join(cue.text for cue in _parse_srt(MANUAL_SRT))

    cpa_dir = OUT / 'cpa'
    cpa_dir.mkdir(parents=True, exist_ok=True)
    request_path = cpa_dir / f'{CANDIDATE_ID}.cpa.request.json'
    response_path = cpa_dir / f'{CANDIDATE_ID}.cpa.response.json'
    request = CpaSemanticQaRequest(
        candidate_id=CANDIDATE_ID,
        room_id='22966160',
        source=CpaSemanticSourceRef(video_path=str(SOURCE_VIDEO), srt_path=str(SOURCE_SRT), start_ms=START_MS, end_ms=END_MS, review_evidence_path=str(MANUAL_SRT)),
        candidate_text=manual_text,
        normalized_text=manual_text,
        response_path=str(response_path),
        terminology=CpaTerminologyContext(schema_version='lidousha-term-lexicon.v1', applied_terms=('kmx',), evidence_paths=('/app/lidousha/term_lexicon.json', str(MANUAL_SRT))),
        metadata={'mode': 'manual-refined-bestcase-backtest'},
    )
    request = write_cpa_semantic_request_artifact(request, request_path)
    response = CpaSemanticQaResponse(
        candidate_id=CANDIDATE_ID,
        request_sha256=request.canonical_sha256(),
        release_ready=True,
        semantic_complete=True,
        terminology_ok=True,
        title_hook_score=0.9,
        context_dependency_score=0.1,
        unsafe_upload_risk_score=0.1,
        reason_codes=(),
        required_fixes=(),
        summary='manual-refined best-case semantic QA pass',
        request_path=str(request_path),
        response_path=str(response_path),
        provider='manual-bestcase-fake-cpa',
        provider_request_id='manual-refined-bestcase',
        terminology_findings=(),
        semantic_findings=(),
        metadata={'mode': 'manual-refined-bestcase'},
    )
    write_cpa_semantic_response_artifact(response, response_path)

    job = {
        'schema_version': 'source-context-job-from-manual-refined-bestcase.v1',
        'candidate_id': CANDIDATE_ID,
        'title': manual_text[:80],
        'room_id': '22966160',
        'timeline': {
            'source_duration_ms': source_duration_ms,
            'anchor_start_ms': START_MS,
            'anchor_end_ms': END_MS,
            'context_start_ms': START_MS,
            'context_end_ms': END_MS,
            'context_duration_ms': END_MS - START_MS,
        },
        'provenance': {'seed_source': 'Ivan manual approved window + manual refined SRT bestcase'},
        'cpa_semantic_request_path': str(request_path),
        'cpa_semantic_response_path': str(response_path),
        'duplicate_corpus': [],
    }
    summary = run_shadow_pipeline(
        source_video=SOURCE_VIDEO,
        source_srt=SOURCE_SRT,
        refined_srt=None,
        source_context_job=job,
        source_context_agy_runner=manual_refined_runner,
        room_id='22966160',
        title=manual_text[:80],
        output_dir=OUT,
        no_upload=True,
        source_context_run_ffmpeg=True,
    )
    record = summary.get('records', [{}])[0]
    result = {
        'out': str(OUT),
        'manual_reference': {'start_ms': START_MS, 'end_ms': END_MS},
        'manual_text': manual_text,
        'decision_action': record.get('decision_action'),
        'reason_codes': record.get('reason_codes'),
        'evidence_path': record.get('evidence_path'),
        'materialized_recut': record.get('materialized_recut'),
        'render_qa': record.get('render_qa'),
        'summary_path': str(OUT / 'summary.json'),
        'cpa_request': str(request_path),
        'cpa_response': str(response_path),
    }
    (OUT / 'manual_refined_bestcase_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
