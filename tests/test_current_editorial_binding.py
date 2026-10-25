from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import publish_staging
from src.autoslice.publish_staging_paths import current_editorial_binding, validate_current_editorial_binding
from src.autoslice.publish_staging_paths import validate_package_editorial_binding
from src.autoslice.producer_delivery_prepare import prepare_talk_delivery
from src.autoslice.producer_delivery_transaction import ProducerDeliveryTransactionError
from scripts.suggest_upload_tags import generate_upload_tags


def _record(tmp_path):
    srt = tmp_path / "candidate.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n最后一句是真实的春雪。\n", encoding="utf-8")
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"media")
    return {"status": "MATERIALIZED", "candidate_id": "candidate", "subtitle_path": str(srt), "media_path": str(media)}


@pytest.mark.parametrize('change', [None, 'boundary_request', 'source', 'title', 'hook', 'srt', 'media', 'burn'])
def test_retained_editorial_stage_preserves_original_boundary_only_for_same_material_inputs(tmp_path, monkeypatch, change):
    from src.autoslice import normal_cover_generation_checkpoint as checkpoint
    record = _record(tmp_path)
    story = {'schema_version':'lidousha-story-contract.v1', 'candidate_id':'candidate',
        'source_media_sha256s':['sha256:'+'a'*64], 'boundary_semantic_review':{
            'status':'PASS','request_sha256':'sha256:'+'b'*64,'summary':'Original description'}}
    review = {'original_title':'original title','original_selection_hook':'original hook'}
    old = {'full_story_contract':deepcopy(story)|{'source_fact_review':review},
        'media':checkpoint.file_identity(Path(record['media_path'])), 'burned_preview':None,
        'editorial_input':{'subtitle_sha256':checkpoint.file_identity(Path(record['subtitle_path']))['sha256']}}
    before = deepcopy(old)
    story['boundary_semantic_review']['summary'] = 'Different wording, identical bound observation'
    title, hook = 'original title', 'original hook'
    if change == 'boundary_request': story['boundary_semantic_review']['request_sha256']='sha256:'+'c'*64
    elif change == 'source': story['source_media_sha256s']=['sha256:'+'c'*64]
    elif change == 'title': title = 'different title'
    elif change == 'hook': hook = 'different hook'
    elif change == 'srt': Path(record['subtitle_path']).write_bytes(Path(record['subtitle_path']).read_bytes()+b'\n')
    elif change == 'media': Path(record['media_path']).write_bytes(b'different media')
    elif change == 'burn': record['burned_preview']={'changed':True}
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE',str(tmp_path/'failure.json'))
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256','a'*64)
    monkeypatch.delenv('AUTOSLICE_REUSE_COVER_RESULT',raising=False)
    monkeypatch.delenv('AUTOSLICE_REUSE_COVER_RESULT_SHA256',raising=False)
    monkeypatch.setattr(checkpoint,'retained_editorial_identity',lambda **_:old)
    kwargs=dict(record=record,story=story,candidate_id='candidate',title=title,selection_hook=hook,artifact_root=tmp_path)
    if change:
        with pytest.raises(ValueError,match='RETAINED_EDITORIAL_SOURCE_DRIFT'):
            publish_staging._retained_cover_editorial_stage(**kwargs)
    else:
        restored = publish_staging._retained_cover_editorial_stage(**kwargs)
        assert restored == old['full_story_contract']
        assert restored['boundary_semantic_review']['summary']=='Original description'
        assert story['boundary_semantic_review']['summary'].startswith('Different wording')
    assert old == before


def test_m37_retained_editorial_media_accepts_relocation(tmp_path, monkeypatch):
    from src.autoslice import normal_cover_generation_checkpoint as checkpoint
    record = _record(tmp_path)
    story = {'candidate_id': 'candidate', 'boundary_semantic_review': {'status': 'PASS'}}
    old = {'full_story_contract': deepcopy(story) | {'source_fact_review': {
        'original_title': 'title', 'original_selection_hook': 'hook'}},
        'media': checkpoint.file_identity(Path(record['media_path'])), 'burned_preview': None,
        'editorial_input': {'subtitle_sha256': checkpoint.file_identity(Path(record['subtitle_path']))['sha256']}}
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE', str(tmp_path / 'failure.json'))
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256', 'a' * 64)
    monkeypatch.delenv('AUTOSLICE_REUSE_COVER_RESULT', raising=False)
    monkeypatch.delenv('AUTOSLICE_REUSE_COVER_RESULT_SHA256', raising=False)
    monkeypatch.setattr(checkpoint, 'retained_editorial_identity', lambda **_: old)
    moved = tmp_path / 'moved.mp4'
    moved.write_bytes(Path(record['media_path']).read_bytes())
    record['media_path'] = str(moved)
    kwargs = dict(record=record, story=story, candidate_id='candidate', title='title',
                  selection_hook='hook', artifact_root=tmp_path)
    assert publish_staging._retained_cover_editorial_stage(**kwargs) == old['full_story_contract']
    moved.write_bytes(b'other')
    with pytest.raises(ValueError, match='RETAINED_EDITORIAL_SOURCE_DRIFT'):
        publish_staging._retained_cover_editorial_stage(**kwargs)


def test_normal_stage_refuses_srt_changed_by_title_provider(tmp_path):
    record = _record(tmp_path)
    def call(_prompt):
        Path(record["subtitle_path"]).write_text("changed", encoding="utf-8")
        return json.dumps({"title": "【李豆沙】读春雪时分享自己的读书感受"}, ensure_ascii=False)
    with pytest.raises(ValueError, match="SUBTITLE_CHANGED_DURING_STAGING"):
        publish_staging._stage_publish_draft(record, candidate_id="candidate", title="candidate", cues=[], run_ffmpeg=False, title_llm_call=call, skip_cover=True)
    assert not tmp_path.joinpath("candidate.publish.json").exists()


def test_comparison_cannot_claim_a_different_selected_title():
    title = "【李豆沙】读春雪时分享自己的读书感受"
    result = publish_staging._resolve_automatic_title(
        base_prompt="current input", title_llm_call=lambda _: json.dumps({
            "title": title, "candidates": [{"title": title, "status": "REJECTED"}],
            "editorial_choice": {"selected": "old title", "reason": "old result"},
        }), selection_hook="当前读书感受", initial_title_source="job_title",
    )
    assert result.title_authority_error == "title_policy_violation:title_candidate_selection_mismatch"
    assert all(row["status"] == "REJECTED_POLICY" for row in result.title_generation_attempts)


def test_first_publication_does_not_read_published_cover_sidecar(tmp_path):
    record = _record(tmp_path)
    covers = tmp_path / "covers"
    covers.mkdir()
    (covers / "candidate.ai-title.cover.png").write_bytes(b"old-cover")
    (covers / "candidate.published-cover-generation.json").write_text("invalid json")
    staged = publish_staging._stage_publish_draft(record, candidate_id="candidate", title="【李豆沙】读春雪时分享自己的读书感受", cues=[], run_ffmpeg=False, title_llm_call=None, skip_cover=True)
    assert staged["publish_staging"]["reason_codes"] == ["COVER_REUSE_PUBLICATION_AUTHORITY_MISSING"]
    assert staged["publish_staging"]["cover_path"] is None


def test_blocked_cover_director_tuples_survive_publish_round_trip_without_hiding_reason(tmp_path):
    from dataclasses import asdict
    from src.autoslice.cover_generation import LidoushaCoverArtDirection
    from src.autoslice.publish_staging_paths import validate_staged_editorial_files
    record = _record(tmp_path)
    director = asdict(LidoushaCoverArtDirection(role='host', expression_en='calm',
        background_style='cream', layout='left-split', hook_color='blue', is_song=False,
        cover_punch=('当前原话',)))
    assert isinstance(director['cover_punch'], tuple)
    failure = {'status': 'BLOCKED_AI_COVER_REQUIRED', 'cover_path': None,
        'reason_codes': ['COVER_FINAL_HOST_IDENTITY_UNVERIFIED'],
        'cover_generation': {'art_direction': director, 'status': 'BLOCKED'}}
    staged = publish_staging._stage_publish_draft(record, candidate_id='candidate',
        title='【李豆沙】读春雪时分享自己的读书感受', cues=[], run_ffmpeg=False,
        title_llm_call=None, stage_cover=lambda *args, **kwargs: failure)
    staging = staged['publish_staging']
    validate_staged_editorial_files(staged, staging, candidate_id='candidate',
                                   subtitle_path=Path(record['subtitle_path']))
    assert staging['cover_status'] == 'BLOCKED_AI_COVER_REQUIRED'
    assert staging['reason_codes'] == ['COVER_FINAL_HOST_IDENTITY_UNVERIFIED']
    assert isinstance(director['cover_punch'], tuple)
    staging['cover_generation']['art_direction']['cover_punch'] = ['different words']
    with pytest.raises(ValueError, match='CURRENT_EDITORIAL_PUBLISH_DRIFT'):
        validate_staged_editorial_files(staged, staging, candidate_id='candidate',
                                       subtitle_path=Path(record['subtitle_path']))


@pytest.mark.parametrize("changed", ["title", "subtitle", "cover", "publish", "tags", "title_original", "story", "research"])
def test_prepare_refuses_late_editorial_change_before_private_copy(tmp_path, changed):
    record = _record(tmp_path)
    cover = tmp_path / "candidate.cover.png"
    cover.write_bytes(b"current-cover")
    staging = {"candidate_id": "candidate", "title": "【李豆沙】读春雪时分享自己的读书感受", "cover_path": str(cover), "cover_status": "AI_COVER_READY", "cover_generation": {}}
    staging["current_binding"] = current_editorial_binding(record, staging, candidate_id="candidate")
    publish = {**deepcopy(staging), "subtitle_sha256": staging["current_binding"]["subtitle_sha256"]}
    if changed == "title_original":
        attempt = {"request_text": "current prompt", "request_sha256": publish_staging._json_sha256("current prompt")}
        staging["title_generation_attempts"] = [attempt]
        publish["title_generation_attempts"] = deepcopy(staging["title_generation_attempts"])
        attempt["request_text"] = "different old prompt"
        publish["title_generation_attempts"][0]["request_text"] = attempt["request_text"]
    if changed == "title":
        staging["title"] += "！"
    if changed == "subtitle":
        Path(record["subtitle_path"]).write_text("changed")
    if changed == "cover":
        cover.write_bytes(b"old-cover")
    if changed == "publish":
        publish["title"] = "old title"
    if changed == "tags":
        record["upload_tags"] = {"input_binding": {"title_sha256": "old title", "srt_sha256": publish["subtitle_sha256"]}}
    if changed == "story":
        record["story_contract"] = {"selection_hook": "different actual story"}
    if changed == "research":
        staging["editorial_research"] = {"applicable": False, "reason": "different application"}
    publish_path = tmp_path / "candidate.publish.json"
    publish_path.write_text(json.dumps(publish))
    staging["publish_json_path"] = str(publish_path)
    with pytest.raises(ProducerDeliveryTransactionError, match="CURRENT_EDITORIAL"):
        prepare_talk_delivery(
            spec={}, candidate_id="candidate", record=record, staging=staging,
            record_path=tmp_path / "record.json", subtitle_path=Path(record["subtitle_path"]),
            speaker_review_srt=None, speaker_ass=None, speaker_manifest_path=None,
            redelivery_baseline_audit_path=None, subtitle_regression_audit_path=None,
            chat_authority_path=tmp_path / "chat.json", talk_filler_audit_path=None,
            text_manifest_path=None, delivery_root=tmp_path / "delivery",
        )
    assert not tmp_path.joinpath(".prepared-deliveries").exists()


def test_tags_refuse_subtitle_changed_during_provider_call(tmp_path):
    record = _record(tmp_path)
    srt = Path(record["subtitle_path"])
    def call(_prompt):
        srt.write_text("changed")
        return '{"content_tags": []}'
    result = generate_upload_tags("【李豆沙】读春雪时分享自己的读书感受", srt, llm_call=call)
    assert result["status"] == "FAILED"
    assert "CURRENT_TAG_SUBTITLE_CHANGED_DURING_GENERATION" in result["error"]


def test_generation_response_drift_refuses_current_consumer(tmp_path):
    record = _record(tmp_path)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"pixels")
    response = tmp_path / "response.json"
    response.write_bytes(b"real saved response")
    staging = {"candidate_id": "candidate", "title": "【李豆沙】读春雪时分享自己的读书感受", "cover_path": str(cover), "cover_status": "AI_COVER_READY", "cover_generation": {"response_path": str(response), "response_sha256": "sha256:" + hashlib.sha256(response.read_bytes()).hexdigest()}}
    staging["current_binding"] = current_editorial_binding(record, staging, candidate_id="candidate")
    validate_current_editorial_binding(record, staging, candidate_id="candidate")
    response.write_bytes(b"different version")
    with pytest.raises(ValueError, match="NETWORK_EVIDENCE_DRIFT"):
        validate_current_editorial_binding(record, staging, candidate_id="candidate")


@pytest.mark.parametrize("method", [None, "screenshot_polish"])
def test_normal_cover_cannot_be_ready_without_actual_request_response(tmp_path, monkeypatch, method):
    record = _record(tmp_path)
    monkeypatch.setattr(publish_staging, "_stage_ai_cover_unbound", lambda *args, **kwargs: {
        "status": "AI_COVER_READY", "cover_generation": {"method": method, "image_generation_used": True}, "cover_path": str(tmp_path / "pixels.png"),
    })
    publish_staging._stage_ai_cover_unbound.cpa_cache_identity = {"transport": "offline-fixture", "output_root": str(tmp_path)}
    result = publish_staging._stage_ai_cover(
        record, media_path=Path(record["media_path"]), candidate_id="candidate",
        title="当前标题", cover_text="当前文案",
    )
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == ["CURRENT_EDITORIAL_NETWORK_EVIDENCE_MISSING"]


def test_portable_package_uses_current_bytes_without_old_producer_locators(tmp_path):
    record = _record(tmp_path)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"pixels")
    staging = {"candidate_id": "candidate", "title": "【李豆沙】读春雪时分享自己的读书感受", "cover_path": str(cover), "cover_status": "AI_COVER_READY", "cover_generation": {}}
    staging["current_binding"] = current_editorial_binding(record, staging, candidate_id="candidate")
    publish = {**deepcopy(staging), "subtitle_sha256": staging["current_binding"]["subtitle_sha256"]}
    record["publish_staging"] = staging
    srt = Path(record["subtitle_path"])
    record["subtitle_path"] = "/superseded/producer.srt"
    staging["cover_path"] = "/superseded/producer.cover.png"
    publish["cover_path"] = staging["cover_path"]
    validate_package_editorial_binding(record, publish, candidate_id="candidate", subtitle_path=srt, cover_path=cover)
    cover.write_bytes(b"old-cover")
    with pytest.raises(ValueError, match="BINDING_DRIFT"):
        validate_package_editorial_binding(record, publish, candidate_id="candidate", subtitle_path=srt, cover_path=cover)


@pytest.mark.parametrize("change", ["text", "cover_appeared"])
def test_cover_only_work_refuses_input_change_before_bind(tmp_path, change):
    from src.autoslice import cover_repair
    srt = tmp_path / "subtitle.srt"
    srt.write_bytes(b"frozen source")
    cover = tmp_path / "missing.cover.png"
    snapshot = cover_repair.snapshot_cover_repair_inputs([srt, cover])
    cover_repair.validate_cover_repair_inputs(snapshot)
    if change == "text":
        srt.write_bytes(b"new source")
    else:
        cover.write_bytes(b"other writer")
    with pytest.raises(ValueError, match="CURRENT_INPUT_DRIFT"):
        cover_repair.validate_cover_repair_inputs(snapshot)
