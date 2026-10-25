import json
from pathlib import Path

import pytest

from scripts import run_auto_review_shadow_pipeline as shadow_pipeline
from src.autoslice.source_context_cpa_refinement import (
    run_source_context_cpa,
    validate_cpa_source_context_result,
)
from src.autoslice.source_context_executor import AgyRunnerError
from src.autoslice.cpa_model_ladder import CPA_MODEL_LADDER
from src.autoslice.llm_client import CpaCompletion


def _draft(path: Path) -> None:
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n第二句\n",
        encoding="utf-8",
    )


def test_fallback_keeps_request_binding_and_reports_answering_model(tmp_path, monkeypatch):
    from dataclasses import replace
    top, fallback = CPA_MODEL_LADDER
    media, draft, refined = (tmp_path / name for name in ('context.mp4', 'draft.srt', 'refined.srt'))
    media.write_bytes(b'offline media')
    _draft(draft)
    def call(_):
        return CpaCompletion(json.dumps({'cues': [{'n': 1, 'text': '第一句'},
            {'n': 2, 'text': '第二句'}]}, ensure_ascii=False), fallback)
    call.cpa_cache_identity = {'transport': 'offline', 'models': [top], 'effort': 'low'}
    monkeypatch.setattr('src.autoslice.full_session_transcription._build_full_session_cpa_llm_call',
        lambda **kwargs: call)
    result = run_source_context_cpa(media, draft, refined)
    assert result.model == fallback and result.requested_model == top
    assert result.cpa_cache_identity['models'] == [fallback]
    assert json.loads(Path(result.request_path).read_text())['model'] == top
    inputs = dict(context_media_path=media, context_draft_srt_path=draft,
        context_refined_srt_path=refined)
    assert validate_cpa_source_context_result(result, **inputs) == ()
    assert 'CPA_REQUEST_MODEL_MISMATCH' in validate_cpa_source_context_result(
        replace(result, requested_model=fallback), **inputs)


def test_cached_source_context_completion_restores_model(tmp_path):
    from types import SimpleNamespace
    from src.autoslice.source_context_cpa_refinement import _cache_completion
    media = tmp_path / 'media.mp4'
    media.with_suffix('.transcription-reuse.json').write_text(json.dumps({
        'cpa': {'served_from_cache': True, 'cache_key': 'offline'}}))
    folder = tmp_path / 'cache' / 'cpa'
    folder.mkdir(parents=True)
    fallback = CPA_MODEL_LADDER[1]
    (folder / 'offline.json').write_text(json.dumps({'payload': 'original bytes\n',
        'identity': {'model_identity': {'models': [fallback]}}}))
    result = _cache_completion(media, SimpleNamespace(root=folder.parent))
    assert result == 'original bytes\n' and result.answering_model == fallback


def test_source_context_cpa_is_numbered_text_only_and_preserves_grid(tmp_path, monkeypatch):
    media = tmp_path / "context.mp4"
    media.write_bytes(b"test media bytes")
    draft = tmp_path / "context.draft.srt"
    _draft(draft)
    refined = tmp_path / "context.refined.srt"
    seen: list[str] = []

    def fake_call(prompt: str) -> str:
        seen.append(prompt)
        return (
            "```json\n"
            + json.dumps(
                {
                    "cues": [
                        {"n": 1, "text": "第一句修正"},
                        {"n": 2, "text": ""},
                    ]
                },
                ensure_ascii=False,
            )
            + "\n```"
        )

    fake_call.cpa_cache_identity = {"transport": "test", "models": ["test-cpa"], "effort": "low"}
    monkeypatch.setattr(
        "src.autoslice.full_session_transcription._build_full_session_cpa_llm_call",
        lambda **_kwargs: fake_call,
    )

    result = run_source_context_cpa(
        media,
        draft,
        refined,
        topic_entity_context="官方名词上下文",
        screen_text_lines=("00:00 画面文字",),
        danmaku_lines=("00:00 弹幕",),
    )

    assert result.provider == "cpa"
    assert result.requested_provider == "cpa"
    assert result.executed_provider == "cpa"
    assert result.audio_input_attested is False
    assert result.timing_validated is True
    assert result.deleted_cue_indexes == (2,)
    assert seen and "[00:00:00]" in seen[0]
    assert "1. 第一句" in seen[0]
    assert "2. 第二句" in seen[0]
    assert "官方名词上下文" in seen[0]
    assert "画面文字" in seen[0]
    assert "弹幕" in seen[0]
    assert "第一句修正" in refined.read_text(encoding="utf-8")
    assert "第二句" not in refined.read_text(encoding="utf-8")
    assert validate_cpa_source_context_result(
        result,
        context_media_path=media,
        context_draft_srt_path=draft,
        context_refined_srt_path=refined,
    ) == ()


def test_source_context_cpa_rejects_partial_response_without_output(tmp_path, monkeypatch):
    media = tmp_path / "context.mp4"
    media.write_bytes(b"test media bytes")
    draft = tmp_path / "context.draft.srt"
    _draft(draft)
    refined = tmp_path / "context.refined.srt"

    def fake_call(_prompt: str) -> str:
        return '{"cues":[{"n":1,"text":"只有一条"}]}'

    fake_call.cpa_cache_identity = {"transport": "test", "models": ["test-cpa"], "effort": "low"}
    monkeypatch.setattr(
        "src.autoslice.full_session_transcription._build_full_session_cpa_llm_call",
        lambda **_kwargs: fake_call,
    )

    with pytest.raises(AgyRunnerError) as raised:
        run_source_context_cpa(media, draft, refined)
    assert raised.value.reason_code == "CPA_CORRECTION_INVALID_OUTPUT"
    assert not refined.exists()


def test_shadow_default_talk_path_selects_cpa_runner(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source bytes")
    source_srt = tmp_path / "source.srt"
    _draft(source_srt)
    captured: dict[str, object] = {}

    def fake_executor(_manifest, **kwargs):
        captured.update(kwargs)
        return shadow_pipeline.SourceContextExecutionResult(
            decision="BLOCK",
            reason_codes=("CPA_CORRECTION_UNAVAILABLE",),
            context_media_path=None,
            context_draft_srt_path=None,
            context_refined_srt_path=None,
            jingting_manifest_path=None,
            review_required_path=str(tmp_path / "review.json"),
            source_cues_path=None,
            jingting_done=False,
        )

    monkeypatch.setattr(shadow_pipeline, "execute_source_context_job", fake_executor)
    (tmp_path / "output" / "evidence").mkdir(parents=True)
    shadow_pipeline._run_live_source(
        source_video=source,
        source_srt=source_srt,
        refined_srt=None,
        source_context_job={
            "candidate_id": "talk-cpa-default",
            "timeline": {
                "context_start_ms": 0,
                "context_duration_ms": 2_000,
                "anchor_start_ms": 0,
                "anchor_end_ms": 1_000,
            },
        },
        agy_result=None,
        source_context_agy_runner=None,
        room_id=None,
        title="talk",
        output_dir=tmp_path / "output",
        no_upload=True,
        source_context_run_ffmpeg=False,
    )

    assert captured["agy_runner"] is shadow_pipeline.run_source_context_cpa
    assert captured["agy_result"] is None
