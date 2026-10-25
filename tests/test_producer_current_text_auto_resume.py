import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_text_pipeline as pipeline


@pytest.mark.parametrize('damage', [None, 'source_usage_missing', 'new_usage', 'word', 'timing', 'source_review', 'content', 'usage', 'code_error'])
def test_transport_only_delivery_can_replay_exact_completed_source_and_timing(tmp_path, damage):
    source = '1\n00:00:00,000 --> 00:00:01,000\n准备\n\n2\n00:00:01,100 --> 00:00:01,500\n懂了\n\n3\n00:00:02,100 --> 00:00:03,000\n下一话题\n'
    delivery = '1\n00:00:00,000 --> 00:00:01,000\n准备\n\n2\n00:00:01,100 --> 00:00:02,000\n懂了\n'
    if damage == 'word': delivery = delivery.replace('懂了', '新的合法文字')
    if damage == 'timing': delivery = delivery.replace('00:00:02,000', '00:00:01,900')
    path = tmp_path / 'replacement_recuts/candidate.recut.srt'; path.parent.mkdir(); path.write_text(delivery)
    digest = hashlib.sha256(delivery.encode()).hexdigest()
    stage = {'cpa_adjudication_usage': {'provider_adjudication_count': 18}}
    if damage == 'source_usage_missing': stage = {}
    live = {'final_text_srt_path': str(path), 'final_output_srt_sha256': digest,
        'cpa_adjudication_usage': {'provider_adjudication_count': 17 if damage == 'usage' else 19 if damage == 'new_usage' else 18},
        'final_review_audit': {'status': 'FLAGGED', 'release_gate': 'BLOCK',
            'reason_codes': ['FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED'],
            'reviewed_srt_sha256': 'sha256:' + digest, 'validated_finding_count': 0, 'findings': [],
            'boundary_semantic_review': {'reason_codes': ['BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:' + ('RuntimeError' if damage == 'code_error' else 'LlmCallError')]}}}
    if damage == 'content':
        live['final_review_audit']['findings'] = [{'content_risk': 'meaning_reversal'}]
        live['final_review_audit']['validated_finding_count'] = 1
    boundary = {'schema_version': 'boundary-audit.v1', 'final_start_ms': 0, 'final_end_ms': 2000,
        'boundary_semantic_review': {'status': 'BLOCK' if damage == 'source_review' else 'PASS',
            'review_scope': 'source_full_window', 'cue_grid_sha256': pipeline.cue_grid_sha256(pipeline.parse_srt_cues(source)),
            'final_endpoint_binding': {'status': 'PASS', 'final_start_ms': 0, 'final_end_ms': 2000}}}
    timing = {'schema_version': 'subtitle-timing-qa.v3', 'window': {'start_ms': 0, 'end_ms': 2000},
        'actions': [{'action': 'retime', 'cue_id': 'fresh_0002', 'text': '懂了',
            'before': {'start_ms': 1100, 'end_ms': 1500}, 'after': {'start_ms': 1100, 'end_ms': 2000}}]}
    (tmp_path / 'candidate.boundary_audit.json').write_text(json.dumps(boundary))
    (path.parent / 'candidate.recut.timing_qa.json').write_text(json.dumps(timing))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert pipeline._source_checkpoint_reproduces_delivery(out_root=tmp_path, cid='candidate',
        source_text=source, source_authority=stage, live_authority=live) is (damage in {None, 'source_usage_missing', 'new_usage'})
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('drift', [False, True])
def test_source_replay_carries_current_usage_without_importing_failed_verdict(tmp_path, monkeypatch, drift):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    kwargs = _pipeline_kwargs(checkpoint)
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=kwargs['spec'], durations=kwargs['durations'], padded=kwargs['padded'],
        padded_dur=kwargs['padded_dur'], cid=kwargs['cid'], out_root=kwargs['out_root'])
    usage = {'provider_adjudication_count': 18, 'policy': 'SOFT'}
    raw = json.dumps({'cpa_adjudication_usage': usage,
        'final_review_audit': {'status': 'FLAGGED', 'release_gate': 'BLOCK'}}).encode()
    parent = tmp_path / 'later-current.json'; parent.write_bytes(raw)
    snapshot['source_usage_parent'] = {'path': str(parent), 'sha256': 'sha256:' + _sha(raw), 'role': 'CUMULATIVE_USAGE_ONLY'}
    if drift: parent.write_bytes(raw + b' ')
    if drift:
        with pytest.raises(pipeline.CurrentTextContinuationError, match='SOURCE_REPLAY_USAGE_DRIFT'):
            pipeline._automatic_current_source_stage_checkpoint(**kwargs, merged=[], authoritative_chat=[], snapshot=snapshot)
    else:
        result = pipeline._automatic_current_source_stage_checkpoint(**kwargs, merged=[], authoritative_chat=[], snapshot=snapshot)
        assert result.chat_authority_audit['cpa_adjudication_usage'] == usage
        assert result.chat_authority_audit['final_review_audit']['cpa_adjudication_usage'] == usage
        assert result.chat_authority_audit['final_review_audit']['status'] != 'FLAGGED'
        reference = result.chat_authority_audit['source_input_usage_parent']
        assert reference['role'] == 'CUMULATIVE_USAGE_ONLY'
        assert Path(reference['path']).read_bytes() == raw


def test_resumed_source_review_advances_usage_and_input_before_finalization(tmp_path,monkeypatch):
    checkpoint=_checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch,checkpoint)
    kwargs=_pipeline_kwargs(checkpoint)
    kwargs['spec']['human_truth_mode']='withheld'
    snapshot=pipeline._automatic_current_checkpoint_snapshot(spec=kwargs['spec'],durations=[100],
        padded=kwargs['padded'],padded_dur=100,cid=kwargs['cid'],out_root=kwargs['out_root'])
    authority=snapshot['chat_authority']
    _,truth=pipeline.apply_source_subtitle_truth(checkpoint['srt'],spec=kwargs['spec'],
        durations=[100],ledger_path=None)
    authority['source_truth_preview_receipts']={stage:pipeline.build_source_truth_preview_receipt(
        input_srt_text=checkpoint['srt'],source_truth_audit=truth,stage=stage)
        for stage in ('pre_entity_arbitration','pre_correction_review')}
    authority['retained_source_input']={'schema_version':'retained-source-input-consumption.v1',
        'inherited_cpa_adjudication_count':18}
    authority['cpa_adjudication_usage']={'provider_adjudication_count':18}
    authority['final_review_audit']['context_adjudication_count']=15
    authority['final_review_audit'].pop('cpa_adjudication_usage',None)
    latest=tmp_path/'latest.json'
    latest.write_text(json.dumps({'cpa_adjudication_usage':{'provider_adjudication_count':27}}))
    snapshot['source_usage_parent']={'path':str(latest),'sha256':'sha256:'+_sha(latest.read_bytes())}
    changed=checkpoint['srt'].replace('当前文字','普通修正')
    def review(**kw):
        assert kw['initial_provider_adjudication_count']==42
        return changed,{'schema_version':'final-review-audit.v1','status':'APPLIED',
            'context_adjudication_count':2,'findings':[],'applied_count':1}
    monkeypatch.setattr(pipeline,'_run_final_review',review)
    monkeypatch.setattr(pipeline,'_source_stage_original_language_witness',lambda *a:checkpoint['srt'])
    monkeypatch.setattr(pipeline,'fidelity_kept_contexts',lambda *a:[])
    class ReachedFinalizer(Exception):
        pass
    def finalize(**kw):
        from src.autoslice.retained_source_usage import reconcile_legacy_source_usage
        current=kw['chat_authority_audit']
        assert reconcile_legacy_source_usage(current) is None
        assert current['final_review_audit'] is kw['final_review_audit']
        assert current['post_transcript_entity_output_srt']==changed
        assert current['cpa_adjudication_usage']['provider_adjudication_count']==44
        assert current['final_review_history'][-1]['final_review_audit']['context_adjudication_count']==15
        raise ReachedFinalizer()
    monkeypatch.setattr(pipeline,'_finalize_text_evidence',finalize)
    with pytest.raises(ReachedFinalizer):
        pipeline._resume_pending_source_finalization(**kwargs,merged=[],authoritative_chat=[],
            snapshot=snapshot,validated_review=authority['final_review_audit'],pending_review=True)


def test_source_context_infra_defers_only_bound_post_ceiling_cues():
    from src.autoslice.boundary_semantic_review import build_boundary_search_scope

    scope = build_boundary_search_scope(
        semantic_target_ms=1000, repair_cap_ms=30000,
        last_piece_start_ms=0, prior_piece_duration_ms=0,
    )
    srt = "1\n00:00:32,000 --> 00:00:33,000\n谢礼物疑点\n"
    review = {"infra_unresolved": [{"cue_index": 1, "suspect": "礼物疑点"}]}
    result = pipeline._source_context_infra_scope(review, srt, scope)
    assert result["blocking"] == []
    assert result["deferred"][0]["disposition"] == "UNRESOLVED_SOURCE_CONTEXT_ONLY"
    assert review == {"infra_unresolved": [{"cue_index": 1, "suspect": "礼物疑点"}]}
    # The selector endpoint alone is insufficient: the entire search budget
    # and maximum tail air remain possibly delivered.
    overlapping = srt.replace("32,000", "31,300")
    assert pipeline._source_context_infra_scope(review, overlapping, scope)["blocking"]
    for row in ({"cue_index": 2, "suspect": "礼物疑点"},
                {"cue_index": 1, "suspect": "另一句话"}):
        with pytest.raises(pipeline.CurrentTextContinuationError, match="CUE_BINDING_INVALID"):
            pipeline._source_context_infra_scope({"infra_unresolved": [row]}, srt, scope)
    drift = {**scope, "max_recommended_end_ms": 10}
    with pytest.raises(pipeline.CurrentTextContinuationError, match="SCOPE_INVALID"):
        pipeline._source_context_infra_scope(review, srt, drift)


def test_source_truth_relocation_reuses_exact_bytes_without_rebinding_original_receipt():
    original = {"ledger_path": "/old/ledger.json", "ledger_sha256": "sha256:" + "a" * 64,
                "status": "NO_RELEVANT_INTERVAL", "applied": [], "failures": []}
    replay = {**original, "ledger_path": "/isolated/ledger.json"}
    assert pipeline._source_truth_replay_matches(original, replay)
    assert original["ledger_path"] == "/old/ledger.json"
    for change in ({"ledger_sha256": "sha256:" + "b" * 64}, {"applied": [{"id": "new"}]},
                   {"status": "FAILED"}, {"ledger_sha256": None}):
        assert not pipeline._source_truth_replay_matches(original, replay | change)


@pytest.mark.parametrize('change', [None, 'applied', 'satisfied', 'failure', 'status', 'unknown_field', 'missing_digest'])
def test_fresh_empty_source_truth_scope_ignores_only_irrelevant_ledger_identity(change):
    original = {'schema_version': 'source-subtitle-truth-audit.v1', 'status': 'NO_RELEVANT_INTERVAL',
        'ledger_path': '/old/ledger.json', 'ledger_sha256': 'sha256:' + 'a' * 64,
        'applied': [], 'satisfied': [], 'failures': []}
    replay = {**original, 'ledger_path': '/new/ledger.json', 'ledger_sha256': 'sha256:' + 'b' * 64}
    if change in {'applied', 'satisfied'}: replay[change] = [{'entry_id': 'new-relevant-source-truth'}]
    elif change == 'failure': replay['failures'] = [{'reason': 'source binding failed'}]
    elif change == 'status': replay['status'] = 'APPLIED'
    elif change == 'unknown_field': replay['unrecognized_effect'] = True
    elif change == 'missing_digest': replay['ledger_sha256'] = None
    before = copy.deepcopy(original)
    assert pipeline._source_truth_replay_matches(original, replay) is (change is None)
    assert original == before


def test_source_checkpoint_replays_truth_after_the_retained_intermediate(tmp_path):
    from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth, build_source_truth_preview_receipt
    checkpoint = _checkpoint(tmp_path)
    earlier = "1\n00:00:00,000 --> 00:00:01,000\n还要谢\n\n2\n00:00:02,000 --> 00:00:03,000\n喝口水\n"
    checkpoint["srt"] = earlier
    checkpoint["spec"]["pieces"][0]["end_ms"] = 4000
    _make_source_stage_checkpoint(checkpoint)
    checkpoint["spec"].pop("human_truth_mode")
    ledger = tmp_path / "truth.json"
    ledger.write_text(json.dumps({"schema_version": "source-subtitle-truth-ledger.v1", "entries": [{
        "truth_id": "throat", "knowledge_type": "SOURCE_INTERVAL_TRUTH",
        "recording_basename": "source.flv", "source_media_sha256": "sha256:" + checkpoint["source_sha"],
        "source_start_ms": 0, "source_end_ms": 1000, "action": "drop_cue", "required": True,
    }]}))
    final, truth = apply_source_subtitle_truth(earlier, spec=checkpoint["spec"], durations=[4000], ledger_path=ledger)
    assert truth["status"] == "APPLIED" and "还要谢" not in final and "喝口水" in final
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority["final_output_srt_sha256"] = _sha(final.encode())
    authority["source_subtitle_truth_audit"] = truth
    authority["source_truth_preview_receipts"] = {stage: build_source_truth_preview_receipt(
        input_srt_text=earlier, source_truth_audit=truth, stage=stage)
        for stage in ("pre_entity_arbitration", "pre_correction_review")}
    _, authority["final_title_mark_balance_audit"] = pipeline.apply_title_mark_balance_guard(final)
    def validate(audit, text=final):
        return pipeline._validate_current_source_stage_authority(audit, spec=checkpoint["spec"],
            durations=[4000], srt_text=text, srt_sha256=_sha(text.encode()), clip_context={},
            source_truth_ledger_path=ledger)
    original = copy.deepcopy(authority)
    validate(authority)
    assert authority == original
    changed = final.replace("喝口水", "喝不喝水")
    with pytest.raises(pipeline.CurrentTextContinuationError, match="SOURCE_TRUTH_REVIEW_REQUIRED"):
        validate(authority | {"final_output_srt_sha256": _sha(changed.encode())}, changed)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="POST_TRANSCRIPT_TEXT"):
        validate(authority | {"post_transcript_entity_output_srt": earlier + "tampered"})


def test_foreign_source_stage_reindexes_only_bound_unchanged_observations():
    before = '1\n00:00:00,000 --> 00:00:00,020\n你好\n\n2\n00:00:00,020 --> 00:00:00,040\n呀\n\n3\n00:00:00,070 --> 00:00:00,090\n我可以唱you are\n'
    after = '1\n00:00:00,000 --> 00:00:00,040\n你好呀\n\n2\n00:00:00,070 --> 00:00:00,090\n我可以唱you are\n'
    spec = {'candidate_id': 'fixture', 'semantic_start_ms': 0, 'semantic_end_ms': 50,
            'pieces': [{'start_ms': 0, 'end_ms': 100}]}
    old = pipeline._foreign_scope.audit_foreign(before, spec, [100])
    authority = {'foreign_script_consistency_audit': old,
                 'post_transcript_entity_output_srt': before,
                 'post_transcript_entity_output_srt_sha256': _sha(before.encode())}
    original = copy.deepcopy(authority)
    current = pipeline._replay_source_stage_foreign_guard(authority, srt_text=after, spec=spec, durations=[100])
    assert current == pipeline._foreign_scope.audit_foreign(after, spec, [100])
    assert current['status'] == 'OUTSIDE_IMMUTABLE_STORY_SCOPE'
    assert current['mixed_cjk_latin_cues'][0]['cue_index'] == 2
    assert authority == original
    for changed, owner in ((after.replace('you are', 'we are'), spec),
                           (after.replace('00:00:00,070', '00:00:00,045'), spec),
                           (after, {**spec, 'semantic_end_ms': 100})):
        with pytest.raises(pipeline.CurrentTextContinuationError, match='FOREIGN_GUARD_DRIFT'):
            pipeline._replay_source_stage_foreign_guard(authority, srt_text=changed, spec=owner, durations=[100])
    with pytest.raises(pipeline.CurrentTextContinuationError, match='FOREIGN_PREIMAGE'):
        pipeline._replay_source_stage_foreign_guard({**authority, 'post_transcript_entity_output_srt': before+'tamper'},
                                                  srt_text=after, spec=spec, durations=[100])


def test_foreign_source_stage_replay_keeps_adjudication_and_tolerates_reindex():
    before = '1\n00:00:00,000 --> 00:00:00,020\n你好\n\n2\n00:00:00,020 --> 00:00:00,040\n呀\n\n3\n00:00:00,070 --> 00:00:00,090\n我可以 play the game 吧\n'
    after = '1\n00:00:00,000 --> 00:00:00,040\n你好呀\n\n2\n00:00:00,070 --> 00:00:00,090\n我可以 play the game 吧\n'
    spec = {'candidate_id': 'fixture', 'semantic_start_ms': 0, 'semantic_end_ms': 100,
            'pieces': [{'start_ms': 0, 'end_ms': 100}]}
    raw = pipeline._foreign_scope.audit_foreign(before, spec, [100])
    assert raw['status'].startswith('BLOCKED_')
    adjudicated = {**copy.deepcopy(raw), 'status': 'WITNESSED_MIXED_PHRASE_AUDIO',
                   'audio_witness_rows': [{'cue_index': 3, 'witnessed': True}],
                   'raw_guard': raw}
    authority = {'foreign_script_consistency_audit': adjudicated}
    original = copy.deepcopy(authority)

    replayed = pipeline._replay_source_stage_foreign_guard(authority, srt_text=after, spec=spec, durations=[100])
    assert replayed['status'] == 'WITNESSED_MIXED_PHRASE_AUDIO'
    assert replayed['audio_witness_rows'] == adjudicated['audio_witness_rows']
    assert replayed['raw_guard'] == pipeline._foreign_scope.audit_foreign(after, spec, [100])
    assert replayed['raw_guard']['mixed_cjk_latin_cues'][0]['cue_index'] == 2
    assert not pipeline._foreign_guard_blocks(replayed)
    assert authority == original
    for changed in (after.replace('play the game', 'play a game'),
                    after.replace('00:00:00,070', '00:00:00,045')):
        with pytest.raises(pipeline.CurrentTextContinuationError, match='FOREIGN_GUARD_DRIFT'):
            pipeline._replay_source_stage_foreign_guard(authority, srt_text=changed, spec=spec, durations=[100])


def test_foreign_guard_block_decision_requires_every_raw_finding_to_be_adjudicated():
    row = {'cue_index': 1, 'start_ms': 0, 'end_ms': 20, 'text': '我们 play the game', 'latin_words': ['play', 'the', 'game']}
    new = {**row, 'cue_index': 4, 'start_ms': 90, 'end_ms': 99, 'text': '然后 you know what'}
    def audit(rows, raw_rows, status='WITNESSED_MIXED_PHRASE_AUDIO', raw_status='BLOCKED_MIXED_CJK_LATIN_PHRASE'):
        return {'status': status, 'mixed_cjk_latin_cues': rows,
                'raw_guard': {'status': raw_status, 'mixed_cjk_latin_cues': raw_rows}}
    blocks = pipeline._foreign_guard_blocks
    assert not blocks(audit([row], [{**row, 'cue_index': 9}]))            # same text+time, new index
    assert not blocks(audit([row], [], raw_status='CLEAN'))               # final text no longer has the issue
    assert blocks(audit([row], [row, new]))                               # new finding
    assert blocks(audit([row], [{**row, 'end_ms': 21}]))                  # changed source time
    assert blocks(audit([row], [row], status='BLOCKED_MIXED_CJK_LATIN_PHRASE'))
    assert blocks(audit([row], [{k: v for k, v in row.items() if k != 'start_ms'}]))   # unlocatable
    assert blocks({'status': 'BLOCKED_MIXED_CJK_LATIN_PHRASE'})            # legacy record: status decides
    assert not blocks({'status': 'CLEAN'})


def _ordinary_chat_read_fixture(checkpoint, *, independent=True):
    from src.autoslice.chat_evidence import ChatEvidence
    from src.autoslice.chat_read_aloud_candidates import read_aloud_support_receipts
    from src.autoslice.read_aloud_arbitration import whole_line_exact_copy_gate

    item = ChatEvidence("danmaku", -1, "当前文字", source="source.xml", source_sha256="d" * 64)
    support_texts = [checkpoint["srt"]] if independent else []
    if independent:
        checkpoint["padded"].with_suffix(".asr_draft.srt").write_text(checkpoint["srt"])
    supports = read_aloud_support_receipts(item, support_srt_texts=support_texts, max_cues=4)
    verdict = None if independent else {
        "authority_kind": "cpa_context_only_closed_set_adjudication",
        "decision_authority": "CPA_JUDGE", "text_first_decision": True,
        "witness_reason_code": "CPA_TEXT_FIRST_NOT_REQUESTED",
        "canonical_entity": item.text, "judge_prompt_sha256": "a" * 64,
        "judge_completion_sha256": "b" * 64,
    }
    proposal = {"score": 1.0, "coverage": 1.0, "precision": 1.0, "common_chars": 4,
                "support_receipts": supports, "thread_anchored": False}
    row = {
        "evidence_id": item.evidence_id, "kind": item.kind, "sender": item.sender,
        "source": item.source, "source_sha256": item.source_sha256,
        "source_event_id": item.source_event_id, "source_offset_ms": item.offset_ms,
        "exact_text": item.text, "cue_indexes": [1], "mode": "exact_span",
        "matched_start_ms": 0, "matched_end_ms": 50, "before": ["当前文字"],
        "after": ["当前文字"], "owner_eligible": True, "thread_anchored": False,
        "audio_transcript_supports": supports,
        "read_aloud_verdict": verdict,
        "whole_line_exact_copy_gate": whole_line_exact_copy_gate(item, proposal, "当前文字", verdict=verdict),
    }
    return item, row


def _ordinary_question_patch_fixture(checkpoint):
    from src.autoslice.chat_authority import ChatEvidence, apply_authoritative_chat_evidence

    before = checkpoint["srt"].replace("当前文字", "这是 KTV 直播")
    item = ChatEvidence("danmaku", -1, "这是ktv吗？", source="source.xml", source_sha256="d" * 64)
    after, audit = apply_authoritative_chat_evidence(before, [item], support_srt_texts=[before])
    row = audit["applied"][0]
    assert row["mode"] == "question_particle_patch"
    assert row["owner_eligible"] is False
    assert row["whole_line_exact_copy_gate"]["status"] == "BLOCKED_PARTIAL_EVIDENCE"
    return item, row, before, after


def test_normal_source_checkpoint_replays_question_patch_without_whole_line_ownership(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    item, row, before, after = _ordinary_question_patch_fixture(checkpoint)
    checkpoint["srt"] = after
    (checkpoint["out_root"] / "padded.fresh.srt").write_text(after)
    checkpoint["padded"].with_suffix(".asr_draft.srt").write_text(before)
    context = json.loads(checkpoint["context_path"].read_text())
    context["whole_clip_draft_srt"] = before
    context["whole_clip_draft_srt_sha256"] = "sha256:" + _sha(before.encode())
    context.pop("context_sha256")
    context["context_sha256"] = _canonical_context_sha(context)
    checkpoint["context_path"].write_text(json.dumps(context))
    _make_source_stage_checkpoint(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority.update(applied=[row], status="APPLIED_AND_VERIFIED", final_output_srt_sha256=_sha(after.encode()))
    checkpoint["authority_path"].write_text(json.dumps(authority))
    original = checkpoint["authority_path"].read_bytes()
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], [item]))
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.srt_text == after
    assert result.chat_authority_audit["applied"][0]["owner_eligible"] is False
    assert result.chat_authority_audit["applied"][0]["whole_line_exact_copy_gate"]["status"] == "BLOCKED_PARTIAL_EVIDENCE"
    assert callable(result.review_exact_final_srt)
    assert checkpoint["authority_path"].read_bytes() == original


@pytest.mark.parametrize("drift", ["xml", "reply", "support", "missing_support", "gate", "ownership"])
def test_source_question_patch_checkpoint_rejects_drift(tmp_path, drift):
    checkpoint = _checkpoint(tmp_path)
    item, row, before, after = _ordinary_question_patch_fixture(checkpoint)
    supports = [before]
    if drift == "xml":
        row["source_sha256"] = "e" * 64
    elif drift == "reply":
        row["after"] = ["这是ktv吗？"]
        after = after.replace("，直播", "")
    elif drift == "support":
        row["audio_transcript_supports"][0]["support_sha256"] = "f" * 64
    elif drift == "missing_support":
        supports = []
        row["audio_transcript_supports"] = []
    elif drift == "gate":
        row["whole_line_exact_copy_gate"]["status"] = "PASS"
    else:
        row["owner_eligible"] = True
    with pytest.raises(pipeline.CurrentTextContinuationError, match="DRIFT"):
        pipeline._validate_source_stage_chat_reads(
            [row], srt_text=after, clip_context={"whole_clip_draft_srt": before},
            authoritative_chat=[item], independent_support_srts=supports,
        )


def test_normal_source_checkpoint_replays_actual_chat_without_promoting_release(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    item, row = _ordinary_chat_read_fixture(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority["applied"] = [row]
    authority["status"] = "APPLIED_AND_VERIFIED"
    checkpoint["authority_path"].write_text(json.dumps(authority))
    original = checkpoint["authority_path"].read_bytes()
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], [item]))
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.srt_text == checkpoint["srt"]
    assert len(result.chat_authority_audit["applied"]) == 1
    assert all(result.chat_authority_audit["applied"][0][key] == value for key, value in row.items())
    assert callable(result.review_exact_final_srt)
    assert "release_gate" not in result.chat_authority_audit["final_review_audit"]
    assert checkpoint["authority_path"].read_bytes() == original


@pytest.mark.parametrize('damage', [None, 'text', 'clock', 'ambiguous'])
def test_chat_read_replay_uses_exact_source_span_after_renumbering(tmp_path, damage):
    checkpoint = _checkpoint(tmp_path)
    item, row = _ordinary_chat_read_fixture(checkpoint)
    before = copy.deepcopy(row)
    # The normal release-hygiene writer renumbers after merging earlier cues.
    current = checkpoint['srt'].replace('1\n', '7\n', 1)
    if damage == 'text': current = current.replace('当前文字', '未经授权文字')
    if damage == 'clock': current = current.replace('00:00:00,050', '00:00:00,051')
    if damage == 'ambiguous': current += current.split('\n\n')[0]+'\n'
    kwargs = dict(srt_text=current, clip_context={'whole_clip_draft_srt':checkpoint['srt']},
                  authoritative_chat=[item], independent_support_srts=[checkpoint['srt']])
    if damage:
        with pytest.raises(pipeline.CurrentTextContinuationError, match='DRIFT'):
            pipeline._validate_source_stage_chat_reads([row], **kwargs)
    else:
        pipeline._validate_source_stage_chat_reads([row], **kwargs)
    assert row == before


@pytest.mark.parametrize("drift", ["xml", "text", "support", "gate"])
def test_source_chat_checkpoint_rejects_drift(tmp_path, drift):
    checkpoint = _checkpoint(tmp_path)
    item, row = _ordinary_chat_read_fixture(checkpoint)
    context = json.loads(checkpoint["context_path"].read_text())
    if drift == "xml":
        row["source_sha256"] = "e" * 64
    elif drift == "text":
        row["after"] = ["字幕偷偷修改"]
    elif drift == "support":
        row["audio_transcript_supports"][0]["support_sha256"] = "f" * 64
    else:
        row["whole_line_exact_copy_gate"]["primary_transcript"]["common_chars"] = 1
    with pytest.raises(pipeline.CurrentTextContinuationError, match="DRIFT"):
        pipeline._validate_source_stage_chat_reads(
            [row], srt_text=checkpoint["srt"], clip_context=context, authoritative_chat=[item],
            independent_support_srts=[checkpoint["srt"]],
        )


def test_text_first_checkpoint_keeps_primary_separate_from_independent_support(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    item, row = _ordinary_chat_read_fixture(checkpoint, independent=False)
    assert row["audio_transcript_supports"] == []
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority.update(applied=[row], status="APPLIED_AND_VERIFIED")
    checkpoint["authority_path"].write_text(json.dumps(authority))
    original = checkpoint["authority_path"].read_bytes()
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], [item]))
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.srt_text == checkpoint["srt"]
    assert result.chat_authority_audit["applied"][0]["audio_transcript_supports"] == []
    assert checkpoint["authority_path"].read_bytes() == original
    assert "release_gate" not in result.chat_authority_audit["final_review_audit"]


def test_primary_draft_cannot_replace_missing_independent_support(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    item, row = _ordinary_chat_read_fixture(checkpoint)
    context = json.loads(checkpoint["context_path"].read_text())
    with pytest.raises(pipeline.CurrentTextContinuationError, match="SUPPORT_DRIFT"):
        pipeline._validate_source_stage_chat_reads(
            [row], srt_text=checkpoint["srt"], clip_context=context, authoritative_chat=[item],
            independent_support_srts=[],
        )


def test_exact_delivery_cannot_include_deferred_unresolved_context(tmp_path):
    callback = pipeline._build_exact_final_review_callback(
        spec={}, out_root=tmp_path, cid="candidate", padded=tmp_path / "padded.mp4",
        adapters=SimpleNamespace(), authoritative_chat=[], clip_context={},
        final_review_audit={"source_context_infra_scope": {"deferred": [
            {"start_ms": 32000, "end_ms": 33000},
        ]}}, verify_confusable_entity=None, truth_ownership=None, screen_read_probe=None,
    )
    with pytest.raises(pipeline.CurrentTextContinuationError, match="INFRA_UNRESOLVED"):
        callback("", {}, 0, 32500)


def test_source_checkpoint_replays_existing_foreign_story_scope(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    checkpoint["spec"].update(semantic_start_ms=0, semantic_end_ms=50)
    authority = json.loads(checkpoint["authority_path"].read_text())
    raw_language = {
        "schema_version": "source-language-preservation-audit.v1",
        "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER", "reverted": [], "reverted_count": 0,
        "unproven_foreign_introductions": [{"cue_index": 2, "start_ms": 70, "end_ms": 90}],
    }
    authority["final_source_language_preservation_audit"] = pipeline._foreign_scope.project_foreign_audit_to_story_scope(
        raw_language, srt_text=checkpoint["srt"], spec=checkpoint["spec"], durations=[100],
    )
    checkpoint["authority_path"].write_text(json.dumps(authority))
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.chat_authority_audit["final_source_language_preservation_audit"]["status"] == (
        "OUTSIDE_IMMUTABLE_STORY_SCOPE"
    )
    authority["final_source_language_preservation_audit"]["story_scope_projection"]["scope"]["story_end_ms"] = 1
    invalid = checkpoint["out_root"] / "invalid-new-source-authority.json"
    invalid.write_text(json.dumps(authority))
    manifest = checkpoint["out_root"] / "candidate.source-text-checkpoint.json"
    native = json.loads(manifest.read_bytes())
    native["authority"] = {"path": str(invalid), "sha256": "sha256:" + _sha(invalid.read_bytes())}
    manifest.write_text(json.dumps(native))
    with pytest.raises(pipeline.CurrentTextContinuationError, match="SOURCE_LANGUAGE_SCOPE_DRIFT"):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


@pytest.mark.parametrize('damage', [None, 'foreign_words', 'foreign_clock', 'reference_sha', 'scope'])
def test_source_language_projection_survives_only_exact_foreign_surfaces_after_hygiene(tmp_path, damage):
    cp = _checkpoint(tmp_path)
    before = '1\n00:00:00,000 --> 00:00:00,400\n我\n\n2\n00:00:00,400 --> 00:00:04,000\n正在说话\n\n3\n00:00:07,000 --> 00:00:09,000\nあ\n'
    after = pipeline._post_truth_release_hygiene(before, {})
    cp['spec']['pieces'][0]['end_ms'] = 10000
    cp['spec'].update(semantic_start_ms=0, semantic_end_ms=5000)
    raw = {'schema_version':'source-language-preservation-audit.v1', 'status':'BLOCKED_UNPROVEN_FOREIGN_SPEAKER',
        'reverted':[], 'reverted_count':0, 'unproven_foreign_introductions':[{'cue_index':3,'start_ms':7000,'end_ms':9000}]}
    projection = pipeline._foreign_scope.project_foreign_audit_to_story_scope(raw,srt_text=before,spec=cp['spec'],durations=[10000])
    assert projection['status'] == 'OUTSIDE_IMMUTABLE_STORY_SCOPE'
    cp['srt'] = after
    _make_source_stage_checkpoint(cp)
    authority = json.loads(cp['authority_path'].read_text())
    authority.update(post_transcript_entity_output_srt=before,post_transcript_entity_output_srt_sha256=_sha(before.encode()),
        final_source_language_preservation_audit=projection)
    if damage == 'foreign_words': after = after.replace('あ', 'い')
    elif damage == 'foreign_clock': after = after.replace('00:00:07,000', '00:00:07,001')
    elif damage == 'reference_sha': authority['post_transcript_entity_output_srt_sha256'] = '0' * 64
    elif damage == 'scope': cp['spec']['semantic_end_ms'] = 8000
    def validate():
        pipeline._validate_current_source_stage_guards(authority,spec=cp['spec'],durations=[10000],
            srt_text=after,srt_sha256=_sha(after.encode()),source_truth_ledger_path=None)
    if damage:
        with pytest.raises(pipeline.CurrentTextContinuationError,match='SOURCE_LANGUAGE_SCOPE_DRIFT'): validate()
    else: validate()


@pytest.mark.parametrize('damage',[None,'words','clock','preimage'])
def test_kana_only_guard_replays_exact_foreign_surfaces_after_cue_merge(damage):
    before='1\n00:00:00,000 --> 00:00:00,400\n我\n\n2\n00:00:00,400 --> 00:00:04,000\n正在说话\n\n3\n00:00:07,000 --> 00:00:09,000\nありがとう\n'
    after=pipeline._post_truth_release_hygiene(before,{})
    spec={'pieces':[{'start_ms':0,'end_ms':10000}],'semantic_start_ms':0,'semantic_end_ms':5000}
    stored=pipeline._foreign_scope.audit_foreign(before,spec,[10000])
    authority={'foreign_script_consistency_audit':stored,'post_transcript_entity_output_srt':before,
        'post_transcript_entity_output_srt_sha256':_sha(before.encode())}
    assert stored['kana_cue_indexes']==[3]
    if damage=='words':after=after.replace('ありがとう','こんにちは')
    elif damage=='clock':after=after.replace('00:00:07,000','00:00:07,001')
    elif damage=='preimage':authority['post_transcript_entity_output_srt_sha256']='0'*64
    def replay():return pipeline._replay_source_stage_foreign_guard(authority,srt_text=after,spec=spec,durations=[10000])
    if damage:
        with pytest.raises(pipeline.CurrentTextContinuationError,match='DRIFT'):replay()
    else:
        result=replay();assert result['status']=='CLEAN' and result['kana_cue_indexes']==[2]
        assert authority['foreign_script_consistency_audit']==stored


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_source_checkpoint_accepts_actual_single_frame_mux_rounding(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    import subprocess
    probe = {"format": {"duration": "0.114"}, "streams": [
        {"codec_type": "video", "duration": "0.113333", "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "duration": "0.100", "avg_frame_rate": "0/0"},
    ]}
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(probe),
    ))
    before = checkpoint["context_path"].read_bytes()
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint["spec"], durations=[114], padded=checkpoint["padded"], padded_dur=114,
        cid=checkpoint["cid"], out_root=checkpoint["out_root"],
    )
    assert snapshot["srt_text"] == checkpoint["srt"]
    assert checkpoint["context_path"].read_bytes() == before
    for drift in ("stream", "container", "rate"):
        altered = json.loads(json.dumps(probe))
        if drift == "stream":
            altered["streams"][1]["duration"] = "0.200"
        elif drift == "container":
            altered["format"]["duration"] = "0.115"
        else:
            altered["streams"][0]["avg_frame_rate"] = "0/0"
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout=json.dumps(altered),
        ))
        with pytest.raises(pipeline.CurrentTextContinuationError, match="SOURCE_CLOCK_INVALID"):
            pipeline._automatic_current_checkpoint_snapshot(
                spec=checkpoint["spec"], durations=[114], padded=checkpoint["padded"], padded_dur=114,
                cid=checkpoint["cid"], out_root=checkpoint["out_root"],
            )


def test_source_piece_container_clock_rejects_more_than_one_frame(monkeypatch, tmp_path):
    import subprocess
    probe = {"format": {"duration": "0.134"}, "streams": [
        {"codec_type": "video", "duration": "0.134", "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "duration": "0.100", "avg_frame_rate": "0/0"},
    ]}
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(probe),
    ))
    assert not pipeline._source_piece_container_clock_matches(tmp_path / "piece.mp4", 100, 134)


def test_concat_checkpoint_retains_logical_clock_with_one_measured_aac_packet(tmp_path, monkeypatch):
    import subprocess
    checkpoint = _checkpoint(tmp_path)
    probe = {"format": {"duration": "0.122"}, "streams": [
        {"codec_type": "video", "duration": "0.100", "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "codec_name": "aac", "duration": "0.121333", "avg_frame_rate": "0/0"},
    ]}
    def run(args, **_kwargs):
        value = {"packets": [{"duration_time": "0.021333"}]} if "-read_intervals" in args else probe
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    monkeypatch.setattr(subprocess, "run", run)
    before = checkpoint["context_path"].read_bytes()
    durations = [100]
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint["spec"], durations=durations, padded=checkpoint["padded"], padded_dur=122,
        cid=checkpoint["cid"], out_root=checkpoint["out_root"],
    )
    assert snapshot["srt_text"] == checkpoint["srt"]
    assert durations == [100]
    assert checkpoint["context_path"].read_bytes() == before
    assert not pipeline._source_piece_container_clock_matches(checkpoint["padded"], 100, 122)


@pytest.mark.parametrize("drift", ["tail", "stream", "container", "codec", "packet"])
def test_concat_rounding_requires_actual_packet_and_matching_stream_clock(tmp_path, monkeypatch, drift):
    import subprocess
    probe = {"format": {"duration": "0.122"}, "streams": [
        {"codec_type": "video", "duration": "0.100", "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "codec_name": "aac", "duration": "0.121333", "avg_frame_rate": "0/0"},
    ]}
    duration = 122
    if drift == "tail":
        probe["format"]["duration"] = "0.124"
        duration = 124
    elif drift == "stream":
        probe["streams"][1]["duration"] = "0.200"
    elif drift == "container":
        probe["format"]["duration"] = "0.121"
    elif drift == "codec":
        probe["streams"][1]["codec_name"] = "mp3"
    def run(args, **_kwargs):
        value = {"packets": [] if drift == "packet" else [{"duration_time": "0.021333"}]} if "-read_intervals" in args else probe
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    monkeypatch.setattr(subprocess, "run", run)
    assert not pipeline._source_piece_container_clock_matches(
        tmp_path / "padded.mp4", 100, duration, allow_audio_packet_rounding=True,
    )


def _canonical_context_sha(payload: dict) -> str:
    return "sha256:" + _sha(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _checkpoint(tmp_path: Path, *, cid: str = "candidate") -> dict:
    out_root = tmp_path / "output"
    out_root.mkdir()
    padded = tmp_path / "padded_0_100.mp4"
    padded.write_bytes(b"padded-media")
    piece = out_root / "piece_0_0_100.mp4"
    piece.write_bytes(b"piece-media")
    source_sha = "a" * 64
    source_path = "/recording/source.flv"
    spec = {
        "date": "2026-10-10",
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": 100,
                "remote_media": source_path,
                "source_media_sha256": "sha256:" + source_sha,
            }
        ],
    }
    piece_provenance = {
        "source_path": source_path,
        "source_sha256": source_sha,
        "source_media_binding": "sha256:" + source_sha,
        "start_ms": 0,
        "end_ms": 100,
        "output_path": str(piece.resolve()),
        "output_sha256": _sha(piece.read_bytes()),
    }
    piece.with_suffix(".provenance.json").write_text(
        json.dumps(piece_provenance), encoding="utf-8"
    )
    padded.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "inputs": [{"path": str(piece.resolve()), "sha256": _sha(piece.read_bytes())}],
                "output_path": str(padded.resolve()),
                "output_sha256": _sha(padded.read_bytes()),
            }
        ),
        encoding="utf-8",
    )
    srt = "1\n00:00:00,000 --> 00:00:00,050\n当前文字\n"
    srt_sha = _sha(srt.encode())
    (out_root / "padded.fresh.srt").write_text(srt, encoding="utf-8")
    clip_context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": cid,
        "recording_date": spec["date"],
        "selection_hook": "",
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": 100,
                "source_media_sha256": "sha256:" + source_sha,
                "recording_basename": "source.flv",
            }
        ],
        "whole_clip_draft_srt": srt,
        "whole_clip_draft_srt_sha256": "sha256:" + srt_sha,
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {},
        "retrieval_budget": {
            "whole_clip_transcript_truncated": False,
            "structured_chat_row_cap": 240,
            "structured_chat_total_rows": 0,
            "structured_chat_selected_rows": 0,
            "structured_chat_truncated": False,
        },
        "mutation_authorized": False,
    }
    clip_context["context_sha256"] = _canonical_context_sha(clip_context)
    context_path = out_root / f"{cid}.clip-context.json"
    context_path.write_text(json.dumps(clip_context), encoding="utf-8")
    authority = {
        "schema_version": "chat-authority-audit.v2",
        "status": "NO_MATCH",
        "mutation_authorized": False,
        "final_output_srt_sha256": srt_sha,
        "post_transcript_entity_output_srt_sha256": srt_sha,
        "entity_repairs": [],
        "final_review_audit": {
            "boundary_semantic_review": {"review_scope": "source_full_window"}
        },
    }
    authority_path = out_root / f"{cid}.chat-authority.json"
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    return {
        "spec": spec,
        "padded": padded,
        "out_root": out_root,
        "cid": cid,
        "srt": srt,
        "authority_path": authority_path,
        "context_path": context_path,
        "source_sha": source_sha,
    }


def _pipeline_kwargs(checkpoint: dict, *, adapters=None) -> dict:
    return {
        "spec": checkpoint["spec"],
        "durations": [100],
        "padded": checkpoint["padded"],
        "padded_dur": 100,
        "host": "localhost",
        "text_override_path": None,
        "cid": checkpoint["cid"],
        "out_root": checkpoint["out_root"],
        "substrate": "existing_srt",
        "correct": "cpa",
        "screen_text": False,
        "adapters": adapters or SimpleNamespace(profile_asset_file=lambda _name:
            Path(__file__).resolve().parents[1] / "assets/lidousha/subtitle_truth_ledger.v1.json"),
    }


def _interrupt_partial_native_emitter(checkpoint, monkeypatch, *, unavailable_review=False, prior_usage=7):
    _make_source_stage_checkpoint(checkpoint)
    authority = json.loads(checkpoint['authority_path'].read_text())
    authority.pop('final_output_srt_sha256')
    _, truth = pipeline.apply_source_subtitle_truth(checkpoint['srt'],spec=checkpoint['spec'],durations=[100],ledger_path=None)
    authority['source_truth_preview_receipts'] = {stage:pipeline.build_source_truth_preview_receipt(
        input_srt_text=checkpoint['srt'],source_truth_audit=truth,stage=stage)
        for stage in ('pre_entity_arbitration','pre_correction_review')}
    if unavailable_review:
        authority['final_review_audit']['status'] = 'AUDITOR_UNAVAILABLE'
        if prior_usage is not None:
            authority['cpa_adjudication_usage'] = {'provider_adjudication_count':prior_usage,'policy':'SOFT'}
        else:
            authority.pop('cpa_adjudication_usage',None)
    (checkpoint['out_root']/'padded.fresh.srt').unlink()
    def interruption(*_a, **_k): raise RuntimeError('test interruption before language provider')
    with monkeypatch.context() as patch:
        patch.setattr(pipeline._foreign_scope,'guard_source_language',interruption)
        with pytest.raises(SystemExit if unavailable_review else RuntimeError,
                match='FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED' if unavailable_review else 'test interruption'):
            pipeline._finalize_text_evidence(spec=checkpoint['spec'],durations=[100],srt_text=checkpoint['srt'],
                chat_authority_audit=authority,transcript_entity_audit=authority['transcript_entity_audit'],
                referent_groups=[],final_review_audit=authority['final_review_audit'],song_name_candidates=[],
                known_songs_path=checkpoint['out_root']/'known.json',session_topic_authorities=(),
                source_language_witness_srt=checkpoint['srt'],text_override_path=None,source_truth_ledger_path=None,
                out_root=checkpoint['out_root'],cid=checkpoint['cid'],padded=checkpoint['padded'],
                clip_context=json.loads(checkpoint['context_path'].read_text()))
    return checkpoint['out_root']/f"{checkpoint['cid']}.partial-source-text-checkpoint.json"


@pytest.mark.parametrize('legacy',[False,True])
def test_normal_entry_consumes_partial_post_review_without_repeating_review_or_asr(tmp_path,monkeypatch,legacy):
    checkpoint=_checkpoint(tmp_path);pointer=_interrupt_partial_native_emitter(checkpoint,monkeypatch)
    original=pointer.read_bytes();manifest=json.loads(original)
    if legacy:
        checkpoint['authority_path'].write_bytes(Path(manifest['authority']['path']).read_bytes())
        pointer.unlink()
        monkeypatch.setattr(pipeline,'_source_stage_original_language_witness',lambda *_:checkpoint['srt'])
    _stub_source_stage_reconstruction(monkeypatch,checkpoint)
    monkeypatch.setattr(pipeline,'_run_final_review',lambda **_:pytest.fail('completed review must be reused'))
    remaining=[]
    def finalize(**kwargs):
        remaining.append(kwargs)
        assert kwargs['srt_text']==checkpoint['srt']==kwargs['source_language_witness_srt']
        (checkpoint['out_root']/'padded.fresh.srt').write_text(kwargs['srt_text'])
        checkpoint['authority_path'].write_text(json.dumps(kwargs['chat_authority_audit']))
        _make_source_stage_checkpoint(checkpoint)
        authority=json.loads(checkpoint['authority_path'].read_text());authority['final_output_srt_sha256']=_sha(checkpoint['srt'].encode())
        checkpoint['authority_path'].write_text(json.dumps(authority))
        return pipeline.TextEvidenceResult(srt_text=checkpoint['srt'],cues=pipeline.parse_srt_cues(checkpoint['srt']),chat_authority_path=checkpoint['authority_path'])
    monkeypatch.setattr(pipeline,'_finalize_text_evidence',finalize)
    result=pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.srt_text==checkpoint['srt'] and len(remaining)==1
    assert result.chat_authority_audit['final_review_audit']['boundary_semantic_review']['status']=='BLOCK'
    pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert len(remaining)==1
    if not legacy:assert pointer.read_bytes()==original


@pytest.mark.parametrize('prior_usage',[None,7])
def test_partial_unavailable_review_retries_only_review_and_keeps_cumulative_usage(tmp_path,monkeypatch,prior_usage):
    cp=_checkpoint(tmp_path);pointer=_interrupt_partial_native_emitter(cp,monkeypatch,unavailable_review=True,prior_usage=prior_usage)
    assert json.loads(pointer.read_bytes())['pending_stage']=='review_and_source_finalization'
    _stub_source_stage_reconstruction(monkeypatch,cp)
    reviews=[];finalized=[]
    def review(**kwargs):
        reviews.append(kwargs)
        assert kwargs['srt_text']==cp['srt'] and kwargs['initial_provider_adjudication_count']==(prior_usage or 0)
        assert kwargs['source_truth_protected_cue_indexes']==()
        return cp['srt'],{'schema_version':'final-review-audit.v1','status':'APPLIED',
            'applied_count':0,'findings':[],'infra_unresolved':[],'context_adjudication_count':2}
    def finalize(**kwargs):
        finalized.append(kwargs)
        assert kwargs['chat_authority_audit']['cpa_adjudication_usage']['provider_adjudication_count']==(prior_usage or 0)+2
        assert kwargs['final_review_audit']['status']=='APPLIED'
        (cp['out_root']/'padded.fresh.srt').write_text(cp['srt'])
        cp['authority_path'].write_text(json.dumps(kwargs['chat_authority_audit']))
        _make_source_stage_checkpoint(cp)
        authority=json.loads(cp['authority_path'].read_text())
        authority['final_output_srt_sha256']=_sha(cp['srt'].encode())
        cp['authority_path'].write_text(json.dumps(authority))
        return pipeline.TextEvidenceResult(srt_text=cp['srt'],cues=pipeline.parse_srt_cues(cp['srt']),chat_authority_path=cp['authority_path'])
    monkeypatch.setattr(pipeline,'_run_final_review',review)
    monkeypatch.setattr(pipeline,'_finalize_text_evidence',finalize)
    result=pipeline.run_text_pipeline(**_pipeline_kwargs(cp))
    assert result.srt_text==cp['srt'] and len(reviews)==len(finalized)==1
    assert result.chat_authority_audit['cpa_adjudication_usage']['provider_adjudication_count']==(prior_usage or 0)+2


@pytest.mark.parametrize('damage',['srt','authority','context','padded','clock'])
def test_partial_native_checkpoint_rejects_changed_dependencies_before_provider(tmp_path,monkeypatch,damage):
    cp=_checkpoint(tmp_path);pointer=_interrupt_partial_native_emitter(cp,monkeypatch);m=json.loads(pointer.read_text())
    if damage in {'srt','authority','context'}:Path(m[damage]['path']).write_bytes(b'drift')
    elif damage=='padded':cp['padded'].write_bytes(b'changed media')
    else:cp['spec']['pieces'][0]['end_ms']=99
    monkeypatch.setattr(pipeline,'_transcribe_draft',lambda **_:pytest.fail('no ASR'))
    with pytest.raises(pipeline.CurrentTextContinuationError,match='DRIFT|INVALID|NOT_REGULAR_FILE'):
        pipeline.run_text_pipeline(**_pipeline_kwargs(cp))


def test_partial_checkpoint_preserves_cumulative_unknown_budget_and_allows_append(tmp_path,monkeypatch):
    from src.autoslice.supplement_audio_budget import start_budget
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    cp=_checkpoint(tmp_path);p=cp['out_root']/'native-audio-budget.json';budget=start_budget(cp['padded'])
    def persist():persist_native_audio_budget_receipt(source_media=cp['padded'],source_media_sha256=_sha(cp['padded'].read_bytes()),receipt_path=p)
    persist();empty=p.read_bytes()
    budget.consume(provider='mai',model='MAI-Transcribe-2',start_ms=10,end_ms=20,max_attempts_per_window=3);persist()
    _interrupt_partial_native_emitter(cp,monkeypatch)
    budget.consume(provider='moss',model='moss-transcribe-diarize-pro',start_ms=20,end_ms=30,max_attempts_per_window=3);persist()
    snapshot=pipeline._automatic_current_checkpoint_snapshot(**{k:v for k,v in _pipeline_kwargs(cp).items() if k in {'spec','durations','padded','padded_dur','cid','out_root'}})
    assert snapshot['partial_source_text'] is True
    before=p.read_bytes();p.write_bytes(empty)
    with pytest.raises(pipeline.CurrentTextContinuationError,match='PARTIAL_NATIVE_BUDGET_DRIFT'):
        pipeline._automatic_current_checkpoint_snapshot(**{k:v for k,v in _pipeline_kwargs(cp).items() if k in {'spec','durations','padded','padded_dur','cid','out_root'}})
    assert before!=empty


@pytest.mark.parametrize('damage',[None,'outside_words','stage_bytes','parent','witness',
    'pending', 'pending_source_pass', 'pending_final_pass', 'pending_source_grid', 'pending_scope', 'pending_context', 'pending_binding', 'pending_no_explicit'])
def test_retained_boundary_original_requires_exact_parent_stage_projection(tmp_path, damage):
    from src.autoslice.retained_reviewed_draft import build_retained_source_boundary_context
    from src.autoslice.boundary_semantic_review import cue_grid_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_text_checkpoint import SOURCE_SCHEMA
    source_text = '1\n00:00:00,100 --> 00:00:00,150\n前边界\n\n2\n00:00:00,250 --> 00:00:00,300\n旧正文\n\n3\n00:00:00,850 --> 00:00:00,900\n后边界\n'
    stage_text = '1\n00:00:00,250 --> 00:00:00,310\n最新正文\n'
    proof = {k:'original-' + k for k in ('source_srt_sha256','parent_authority_sha256',
        'parent_clip_context_sha256','filler_plan_sha256','padded_sha256','source_timeline_offset_ms','source_endpoint')}
    retained = {'proof':proof,'source_boundary_context':{'source_srt_text':source_text,
        'source_srt_sha256':'sha256:' + _sha(source_text.encode()),
        'source_cue_grid_sha256':cue_grid_sha256(parse_srt_cues(source_text)), 'parent_context_piece_start_ms':0},
        'projection':{'source_range_ms':{'start_ms':200,'end_ms':800},
            'physical_source_range_ms':{'start_ms':200,'end_ms':800},'source_timeline_offset_ms':0,
            'piece_clock':[{'piece_index':0,'source_start_ms':0,'source_end_ms':1000,
                'concat_start_ms':0,'concat_end_ms':1000,'actual_duration_ms':1000}]}}
    cues,_ = build_retained_source_boundary_context(retained,stage_text)
    boundary = tmp_path/'candidate.source-boundary-context.srt'
    boundary.write_text(pipeline._render_cues_to_srt(cues))
    stage = {'final_output_srt_sha256':_sha(stage_text.encode()),
             'post_transcript_entity_output_srt_sha256':'original-stage','retained_reviewed_draft':proof}
    current = stage | {'final_review_audit':{'boundary_semantic_review':{
        'source_separation_witness':{'source_cue_grid_sha256':cue_grid_sha256(cues)}}}}
    pending = str(damage).startswith('pending')
    if pending:
        stage['final_review_audit'] = {
            'boundary_semantic_review': {'status':'PASS','review_scope':'source_full_window',
                'cue_grid_sha256':cue_grid_sha256(cues),'request_sha256':'sha256:' + 'a'*64},
            'source_boundary_context': {'schema_version':'source-boundary-transcript-context.v1',
                'source_srt_sha256':'sha256:' + _sha(boundary.read_bytes()),'source_cue_grid_sha256':cue_grid_sha256(cues)}}
        current['final_review_audit'] = {'status':'FLAGGED','release_gate':'BLOCK','boundary_semantic_review':{}}
        if damage in {'pending_source_pass','pending_final_pass'}:
            current['final_review_audit'] = {'status':'CLEAN','release_gate':'PASS',
                'boundary_semantic_review':dict(stage['final_review_audit']['boundary_semantic_review'])}
            if damage == 'pending_final_pass': current['final_review_audit']['boundary_semantic_review']['review_scope']='final_delivery'
        if damage == 'pending_source_grid': stage['final_review_audit']['boundary_semantic_review']['cue_grid_sha256']='different'
        elif damage == 'pending_scope': stage['final_review_audit']['boundary_semantic_review']['review_scope']='final_delivery'
        elif damage == 'pending_context': stage['final_review_audit']['source_boundary_context']['source_srt_sha256']='different'
    native = {'padded':{'path':'original-media','sha256':'bound-media'},'context':{'sha256':'bound-context'}}
    manifest = {'schema_version':SOURCE_SCHEMA,'candidate_id':'candidate','time_domain':'padded_local',
                'release_authorized':False,**native}
    for key,data in [('srt',stage_text),('authority',json.dumps(stage)),('context','{}')]:
        path=tmp_path/(key+'.txt');path.write_text(data)
        manifest[key]={'path':str(path),'sha256':'sha256:' + _sha(path.read_bytes())}
    native['context']=manifest['context']
    if pending:
        manifest['boundary_srt']={'path':str(boundary),'sha256':'sha256:' + _sha(boundary.read_bytes())}
        native['source_clock']={'boundary_srt':dict(manifest['boundary_srt'])}
        if damage == 'pending_binding': native['source_clock']['boundary_srt']['sha256']='different'
        elif damage == 'pending_no_explicit': manifest.pop('boundary_srt')
    (tmp_path/'candidate.source-text-checkpoint.json').write_text(json.dumps(manifest))
    if damage == 'outside_words': boundary.write_text(boundary.read_text().replace('前边界','伪造边界'))
    elif damage == 'stage_bytes': Path(manifest['srt']['path']).write_text(stage_text+'tamper')
    elif damage == 'parent': current['retained_reviewed_draft'] = proof | {'parent_authority_sha256':'different'}
    elif damage == 'witness': current['final_review_audit']['boundary_semantic_review']['source_separation_witness']['source_cue_grid_sha256']='different'
    kwargs=dict(out_root=tmp_path,cid='candidate',retained=retained,current_authority=current,current_native=native)
    if damage and damage not in {'pending','pending_source_pass'}:
        with pytest.raises(pipeline.CurrentTextContinuationError): pipeline._retained_source_boundary_artifact(**kwargs)
    else:
        assert pipeline._retained_source_boundary_artifact(**kwargs)['sha256'] == 'sha256:' + _sha(boundary.read_bytes())


def _fake_draft(srt: str) -> pipeline.TranscriptionDraft:
    return pipeline.TranscriptionDraft(
        song_name_candidates=[],
        transcriber=lambda *_args: "SHOULD_REMAIN_LAZY",
        spans=[SimpleNamespace(start_ms=0, end_ms=100)],
        srt_text=srt,
        code_switch_audit={},
        term_boundary_moves=[],
        support_srts=[],
        session_topic_authorities=[],
        session_topic_absorption_audits=[],
        source_language_witness_srt="",
    )


def _source_stage_row(*, status: str = "JUDGED") -> dict:
    request_sha = "b" * 64
    return {
        "cue_indexes": [1],
        "before": ["当前文字"],
        "after": ["当前文本"],
        "action": "REPLACE_CUE_TEXT",
        "mode": "final_review_context_adjudication",
        "judge": {
            "status": status,
            "choice": "PROPOSED",
            "check_request_sha256": request_sha,
        },
        "request_sha256": request_sha,
        "mutation_authority": {"status": "PASS"},
        "timing_immutable": True,
    }


@pytest.mark.parametrize('damage',[None,'media','parent_bytes','unrelated'])
def test_source_generation_requires_its_exact_current_ancestor(tmp_path,damage):
    from src.autoslice.producer_text_checkpoint import SOURCE_SCHEMA
    def bind(name,value):
        path=tmp_path/name;path.write_text(value)
        return {'path':str(path),'sha256':'sha256:'+_sha(path.read_bytes())}
    text=bind('current.srt','current-text')
    authority=bind('current.json',json.dumps({'final_output_srt_sha256':text['sha256']}))
    current={'authority':authority,'srt':text,'padded':{'path':'bound-media','sha256':'bound-hash'}}
    parent={'authority_path':authority['path'],'authority_sha256':authority['sha256'],
            'srt_path':text['path'],'srt_sha256':text['sha256']}
    if damage=='unrelated':
        # Matching subtitle words do not bind a different authority audit.
        other=bind('other.json',json.dumps({'final_output_srt_sha256':text['sha256'],'different':True}))
        parent.update(authority_path=other['path'],authority_sha256=other['sha256'])
    child_text=bind('source.srt','source-generation')
    child=bind('source.json',json.dumps({'final_output_srt_sha256':child_text['sha256'],
        'human_sequence_alignment':{'records':[{'source':'fixture'}]},'retained_reviewed_draft':{'latest_current_parent':parent}}))
    # Exercise an intermediate source generation, not only a direct child.
    grandchild=bind('source2.json',json.dumps({'human_sequence_alignment':{'records':[{'source':'fixture'}]},
        'retained_reviewed_draft':{'latest_current_parent':{'authority_path':child['path'],
            'authority_sha256':child['sha256'],'srt_path':child_text['path'],'srt_sha256':child_text['sha256']}}}))
    manifest={'schema_version':SOURCE_SCHEMA,'candidate_id':'candidate','time_domain':'padded_local',
              'release_authorized':False,'padded':current['padded'],'authority':grandchild}
    if damage=='media':manifest['padded']={'path':'other-media','sha256':'other-hash'}
    source=tmp_path/'checkpoint.json';source.write_text(json.dumps(manifest))
    if damage=='parent_bytes':
        Path(authority['path']).write_text('{}')
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._retained_source_successor(source,current,cid='candidate')
    else:
        assert pipeline._retained_source_successor(source,current,cid='candidate') is (damage is None)


@pytest.mark.parametrize('changed_foreign',[False,True])
def test_retained_language_skip_is_only_an_unchanged_foreign_input(tmp_path,changed_foreign):
    cp=_checkpoint(tmp_path)
    cp['srt']='1\n00:00:00,010 --> 00:00:00,030\n当前\n\n2\n00:00:00,050 --> 00:00:00,080\nあい\n'
    _make_source_stage_checkpoint(cp)
    authority=json.loads(cp['authority_path'].read_text())
    authority['final_source_language_preservation_audit'].update(status='SKIPPED_CUE_COUNT_MISMATCH',draft_cue_count=0)
    authority['human_sequence_alignment']={'records':[{'units':[{'start_ms':10,'end_ms':30,'text':'当前'}]}]}
    reference=cp['srt'].replace('当前','已确认原话')
    if changed_foreign:reference=reference.replace('あい','うそ')
    kwargs=dict(spec=cp['spec'],durations=[100],srt_text=cp['srt'],srt_sha256=_sha(cp['srt'].encode()),source_truth_ledger_path=None)
    with pytest.raises(pipeline.CurrentTextContinuationError,match='SOURCE_LANGUAGE_GUARD_BLOCKED'):
        pipeline._validate_current_source_stage_guards(authority,**kwargs)
    if changed_foreign:
        with pytest.raises(pipeline.CurrentTextContinuationError,match='SOURCE_LANGUAGE_GUARD_BLOCKED'):
            pipeline._validate_current_source_stage_guards(authority,**kwargs,verified_language_reference=reference)
    else:
        pipeline._validate_current_source_stage_guards(authority,**kwargs,verified_language_reference=reference)
        assert authority['final_source_language_preservation_audit']['status']=='SKIPPED_CUE_COUNT_MISMATCH'


@pytest.mark.parametrize('damage', [None, 'foreign_words', 'foreign_time', 'count', 'missing_witness', 'zero_draft'])
def test_legacy_language_count_skip_reenters_finalization_only_with_original_witness(tmp_path, monkeypatch, damage):
    witness = '1\n00:00:01,000 --> 00:00:02,000\n还记得 NUS\n\n2\n00:00:03,000 --> 00:00:04,000\n旁白\n'
    current = '1\n00:00:01,000 --> 00:00:02,000\n还记得 NUS\n'
    audit = {'schema_version': 'source-language-preservation-audit.v1',
             'status': 'SKIPPED_CUE_COUNT_MISMATCH', 'draft_cue_count': 2,
             'final_cue_count': 1, 'reverted': [], 'reverted_count': 0,
             'unproven_foreign_introductions': []}
    if damage == 'foreign_words': current = current.replace('NUS', 'NTU')
    elif damage == 'foreign_time': current = current.replace('01,000', '01,001')
    elif damage == 'count': audit['draft_cue_count'] = 3
    elif damage == 'zero_draft': audit['draft_cue_count'] = 0
    saved = copy.deepcopy(audit)
    monkeypatch.setattr(pipeline, '_source_stage_original_language_witness',
                        lambda *_: None if damage == 'missing_witness' else witness)
    pending = pipeline._source_language_count_recheck_pending(
        {'final_source_language_preservation_audit': audit}, spec={}, durations=[5000],
        padded=tmp_path/'original.mp4', padded_dur=5000, srt_text=current, clip_context={},
    )
    assert pending is (damage is None)
    assert audit == saved


def _make_source_stage_checkpoint(checkpoint: dict, *, row: dict | None = None) -> None:
    checkpoint["spec"]["human_truth_mode"] = "withheld"
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    srt_sha = _sha(checkpoint["srt"].encode())
    _, title_mark_audit = pipeline.apply_title_mark_balance_guard(checkpoint["srt"])
    authority.update(
        {
            "status": "APPLIED_AND_VERIFIED" if row is not None else "NO_MATCH",
            "post_transcript_entity_output_srt": checkpoint["srt"],
            "post_transcript_entity_output_srt_sha256": srt_sha,
            "entity_repairs": [row] if row is not None else [],
            "applied": [],
            "sender_repairs": [],
            "gift_repairs": [],
            "coreference_repairs": [],
            "final_source_language_preservation_audit": {
                "schema_version": "source-language-preservation-audit.v1",
                "status": "CLEAN",
                "reverted": [],
                "reverted_count": 0,
                "unproven_foreign_introductions": [],
            },
            "foreign_script_consistency_audit": pipeline._foreign_scope.audit_foreign(
                checkpoint["srt"], checkpoint["spec"], [100]
            ),
            "transcript_entity_audit": {
                "schema_version": "transcript-entity-audit.v1",
                "status": "NO_ENTITY",
                "input_srt_sha256": srt_sha,
                "output_srt_sha256": srt_sha,
                "confirmed": [],
                "repairs": [],
                "entity_verdict_required": [],
            },
            "source_subtitle_truth_audit": {
                "schema_version": "source-subtitle-truth-audit.v1",
                "status": "SKIPPED_NO_LEDGER",
                "ledger_path": None,
                "ledger_sha256": None,
                "applied": [],
                "satisfied": [],
                "failures": [],
            },
            "source_truth_preview_receipts": {
                "pre_entity_arbitration": {
                    "schema_version": "source-truth-deterministic-preview.v1",
                    "ledger_sha256": None,
                },
                "pre_correction_review": {
                    "schema_version": "source-truth-deterministic-preview.v1",
                    "ledger_sha256": None,
                },
            },
            "final_title_mark_balance_audit": title_mark_audit,
            "final_review_audit": {
                "schema_version": "final-review-audit.v1",
                "status": "APPLIED",
                "findings": [],
                "applied_count": 0,
                "infra_unresolved": [],
            },
        }
    )
    checkpoint["authority_path"].write_text(
        json.dumps(authority), encoding="utf-8"
    )


def _make_pending_source_review(checkpoint: dict, *, reason: str = "ENTITY_AUDIO_PROVIDER_FAILED") -> dict:
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    base_sha = _sha("当前文字".encode())
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "当前文字",
        "proposed_full_cue": None,
        "suggestion": None,
        "repair_class": "phonetic",
        "why": "需要普通复核",
        "base_text_sha256": base_sha,
        "span_start_codepoint": 0,
        "span_end_codepoint": len("当前文字"),
        "evidence_cue_ids": [],
        "routed": "disclosure",
        # This is historical evidence only; the pending conversion must strip it.
        "context_audio_adjudication": {
            "status": "UNCERTAIN",
            "repaired": False,
            "verdict": {"reason_code": reason},
        },
    }
    authority["final_review_audit"] = {
        "schema_version": "final-review-audit.v1",
        "status": "APPLIED",
        "findings": [finding],
        "applied_count": 18,
        "context_adjudication_count": 23,
        "infra_unresolved": [{
            "cue_index": 1,
            "suspect": "当前文字",
            "reason_code": reason,
        }],
        "infra_unresolved_count": 1,
    }
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    return finding


def _ordinary_context_adjudication(srt_text: str, *, repaired: bool = False) -> dict:
    cue = pipeline.parse_srt_cues(srt_text)[0]
    base_sha = _sha(cue.text.encode())
    request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "whole_clip_current_srt": srt_text,
        "current_cue": cue.text,
        "suspect": cue.text,
        "proposed_cue": cue.text,
        "base_text_sha256": base_sha,
        "cue_indexes": [1],
        "matched_start_ms": cue.start_ms,
        "matched_end_ms": cue.end_ms,
        "source_media_timeline_offset_ms": 0,
    }
    request["request_sha256"] = _sha(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    return {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "OBSERVED",
        "repaired": repaired,
        "request": request,
        "verdict": {"status": "OBSERVED", "target_audible": True},
    }


def _stub_source_stage_reconstruction(monkeypatch, checkpoint: dict) -> None:
    checkpoint["spec"].setdefault("semantic_start_ms", 0)
    checkpoint["spec"].setdefault("semantic_end_ms", 50)
    monkeypatch.setattr(pipeline, "review_source_boundary", lambda **_kwargs: (
        None, {"review_scope": "source_full_window", "status": "BLOCK", "reason_codes": ["TEST_UNREVIEWED"]}, {}, None,
    ))
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        lambda **kwargs: _fake_draft(kwargs["retained_srt_text"]),
    )
    monkeypatch.setattr(
        pipeline,
        "_build_entity_verification_context",
        lambda **_kwargs: pipeline.EntityVerificationContext(
            verify_confusable_entity=lambda *_args, **_inner: None,
            referent_groups=[],
            topic_resolution_audit={},
        ),
    )
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *_args, **_kwargs: None)


def _stub_current_validation(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "_continuation_validate_existing_authority",
        lambda authority, **_kwargs: authority["final_review_audit"],
    )
    monkeypatch.setattr(
        pipeline,
        "_continuation_source_boundary_cues",
        lambda **_kwargs: ([], {}),
    )
    monkeypatch.setattr(
        pipeline,
        "_continuation_project_delivery_to_source",
        lambda *_args, **_kwargs: ([], {}),
    )


def test_ordinary_entry_dispatches_existing_current_continuation(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    _stub_current_validation(monkeypatch)
    seen = {}

    def retain_draft(**kwargs):
        seen["draft"] = kwargs
        return _fake_draft(checkpoint["srt"])

    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        retain_draft,
    )

    def consume(**kwargs):
        seen["continuation"] = kwargs["continuation"]
        return "continued"

    monkeypatch.setattr(pipeline, "_run_current_text_continuation", consume)
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))

    assert result == "continued"
    continuation = seen["continuation"]
    assert continuation.current_srt_path == checkpoint["out_root"] / "padded.fresh.srt"
    assert continuation.source_media_sha256s == ("sha256:" + checkpoint["source_sha"],)
    assert seen["draft"]["retained_srt_text"] == checkpoint["srt"]
    assert continuation.transcriber is not None


def test_fresh_entry_without_checkpoint_keeps_existing_fresh_path(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    for path in checkpoint["out_root"].glob("*"):
        path.unlink()
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *_args, **_kwargs: None)

    class FreshPathReached(Exception):
        pass

    def fresh(**_kwargs):
        raise FreshPathReached

    monkeypatch.setattr(pipeline, "_transcribe_draft", fresh)
    with pytest.raises(FreshPathReached):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


def test_explicit_retained_current_text_keeps_existing_precedence(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    from src.autoslice import retained_reviewed_draft

    monkeypatch.setattr(
        retained_reviewed_draft,
        "load_retained_reviewed_draft",
        lambda **_kwargs: {"srt_text": checkpoint["srt"]},
    )
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *_args, **_kwargs: None)
    seen = {}

    class RetainedPathReached(Exception):
        pass

    def retained(**kwargs):
        seen.update(kwargs)
        raise RetainedPathReached

    monkeypatch.setattr(pipeline, "_transcribe_draft", retained)
    kwargs = _pipeline_kwargs(checkpoint)
    kwargs["spec"] = {**checkpoint["spec"], "retained_current_text": {"fixture": True}}
    with pytest.raises(RetainedPathReached):
        pipeline.run_text_pipeline(**kwargs)
    assert seen["retained_srt_text"] == checkpoint["srt"]


@pytest.mark.parametrize("drift", ["srt", "source", "context"])
def test_current_checkpoint_drift_blocks_before_transcriber(tmp_path, monkeypatch, drift):
    checkpoint = _checkpoint(tmp_path)
    if drift == "srt":
        (checkpoint["out_root"] / "padded.fresh.srt").write_text(
            checkpoint["srt"] + "2\n00:00:00,060 --> 00:00:00,070\n漂移\n",
            encoding="utf-8",
        )
    elif drift == "source":
        piece = checkpoint["out_root"] / "piece_0_0_100.mp4"
        piece.write_bytes(b"source-drift")
    else:
        context = json.loads(checkpoint["context_path"].read_text(encoding="utf-8"))
        context["candidate_id"] = "different-candidate"
        checkpoint["context_path"].write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(pipeline, "_transcribe_draft", lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun))
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


def test_pending_wrong_owner_is_rejected_before_lazy_transcriber(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(
        pipeline,
        "_continuation_validate_existing_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pipeline.CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_OWNER_DRIFT")
        ),
    )

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(pipeline, "_transcribe_draft", lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun))
    original_authority = checkpoint["authority_path"].read_bytes()
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_DRIFT"):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert checkpoint["authority_path"].read_bytes() == original_authority


def test_real_source_stage_checkpoint_reconstructs_text_result_without_v2_promotion(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint, row=_source_stage_row())
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    monkeypatch.setattr(
        pipeline,
        "_run_current_text_continuation",
        lambda **_kwargs: pytest.fail("source-stage checkpoint must not enter v2 continuation"),
    )

    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))

    assert isinstance(result, pipeline.TextPipelineResult)
    assert result.srt_text == checkpoint["srt"]
    assert result.chat_authority_audit["final_review_audit"]["schema_version"] == (
        "final-review-audit.v1"
    )
    assert result.chat_authority_audit["final_review_audit"]["status"] == "APPLIED"
    assert result.chat_authority_path == checkpoint["authority_path"]
    assert callable(result.review_exact_final_srt)


def test_source_stage_resume_runs_normal_source_boundary_review_on_current_grid(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    original = checkpoint["authority_path"].read_bytes()
    seen = {}

    def normal_review(**kwargs):
        seen.update(kwargs)
        return None, {"review_scope": "source_full_window", "status": "BLOCK",
                      "reason_codes": ["ACTUAL_REVIEW_BLOCK"]}, {}, None

    monkeypatch.setattr(pipeline, "review_source_boundary", normal_review)
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert [cue.text for cue in seen["cues"]] == ["当前文字"]
    assert seen["clip_context"]["whole_clip_draft_srt"] == checkpoint["srt"]
    assert seen["available_local_source_context_end_ms"] == 100
    assert seen["current_owner_contract"] == result.chat_authority_audit["frozen_boundary_owner_contract"]
    assert result.chat_authority_audit["final_review_audit"]["boundary_semantic_review"]["status"] == "BLOCK"
    assert checkpoint["authority_path"].read_bytes() == original


def _normal_transcript_entity_row():
    return {
        "mode": "transcript_entity_only", "cue_indexes": [1],
        "matched_start_ms": 0, "matched_end_ms": 50,
        "before": ["当前文子"], "after": ["当前文字"],
        "transcript_surface": "文子", "transcript_canonical": "文字",
        "resolved_canonical": "文字", "expected_entity": "文字",
        "survived": True, "request_sha256": "a" * 64,
        "verdict": {
            "schema_version": "chat-entity-verdict.v1", "status": "RESOLVED",
            "canonical_entity": "文字", "request_sha256": "a" * 64,
            "authority_kind": "cpa_context_only_closed_set_adjudication",
            "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
            "witness_status": "UNCERTAIN", "confidence": 0.95,
            "witness_request_sha256": "b" * 64,
            "judge_prompt_sha256": "c" * 64,
            "judge_completion_sha256": "d" * 64, "needs_audio": False,
        },
    }


def test_source_checkpoint_replays_normal_transcript_verdict_without_final_judge(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    row = _normal_transcript_entity_row()
    _make_source_stage_checkpoint(checkpoint, row=row)
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority["transcript_entity_audit"].update(
        status="APPLIED_AND_VERIFIED", repairs=[row]
    )
    checkpoint["authority_path"].write_text(json.dumps(authority))
    original = checkpoint["authority_path"].read_bytes()
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert result.srt_text == checkpoint["srt"]
    assert all(result.chat_authority_audit["entity_repairs"][0][key] == value
               for key, value in row.items())
    assert "judge" not in row and "mutation_authority" not in row
    assert callable(result.review_exact_final_srt)
    assert "release_gate" not in result.chat_authority_audit["final_review_audit"]
    assert checkpoint["authority_path"].read_bytes() == original


@pytest.mark.parametrize("drift", [
    "audit", "request", "verdict", "canonical", "timing", "current", "extra_rewrite",
])
def test_source_transcript_entity_replay_rejects_unbound_or_expanded_repair(tmp_path, drift):
    checkpoint = _checkpoint(tmp_path)
    row = _normal_transcript_entity_row()
    audit_row = copy.deepcopy(row)
    if drift == "audit":
        audit_row["request_sha256"] = "e" * 64
    elif drift == "request":
        row["request_sha256"] = "e" * 64
    elif drift == "verdict":
        row["verdict"]["decision_authority"] = "AUDIO_MODEL"
    elif drift == "canonical":
        row["resolved_canonical"] = "其他实体"
    elif drift == "timing":
        row["matched_start_ms"] = 1
    elif drift == "current":
        row["after"] = ["当前文字另加一句"]
    else:
        row["before"] = ["另一个文子"]
    if drift != "audit":
        audit_row = copy.deepcopy(row)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CURRENT_TRANSCRIPT_ENTITY"):
        pipeline._validate_source_stage_transcript_entity_repair(
            row, authority={"transcript_entity_audit": {"repairs": [audit_row]}},
            srt_text=checkpoint["srt"],
        )


def test_real_source_stage_pending_typed_owner_blocks_before_transcriber(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(
        checkpoint, row=_source_stage_row(status="PENDING")
    )
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    transcriber_calls = []

    def should_not_run(**_kwargs):
        transcriber_calls.append(True)
        raise AssertionError("source-stage validator must run before VAD/transcriber")

    monkeypatch.setattr(pipeline, "_transcribe_draft", should_not_run)
    original_authority = checkpoint["authority_path"].read_bytes()

    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_ENTITY_REPAIR_INVALID:0:CURRENT_ENTITY_REPAIR_JUDGE_MISSING",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))

    assert transcriber_calls == []
    assert checkpoint["authority_path"].read_bytes() == original_authority


def test_pending_source_stage_reopens_infra_rows_through_ordinary_review(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    prior = _make_pending_source_review(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    seen = {}

    def ordinary_review(**kwargs):
        seen.update(kwargs)
        row = dict(kwargs["priority_raw_findings"][0])
        assert "context_audio_adjudication" not in row
        assert "verdict" not in row
        return kwargs["srt_text"], {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [{
                **row,
                "routed": "disclosure",
                "context_audio_adjudication": _ordinary_context_adjudication(
                    kwargs["srt_text"]
                ),
            }],
            "applied_count": 0,
            "context_adjudication_count": 2,
            "infra_unresolved": [],
            "infra_unresolved_count": 0,
        }

    monkeypatch.setattr(pipeline, "_run_final_review", ordinary_review)
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    monkeypatch.setattr(
        pipeline,
        "review_source_boundary",
        lambda **_kwargs: (
            None,
            {"review_scope": "source_full_window", "status": "BLOCK", "reason_codes": ["TEST"]},
            {"status": "REPLAYED", "source_srt_sha256": "sha256:" + _sha(checkpoint["srt"].encode())},
            None,
        ),
    )

    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))

    assert seen["priority_raw_findings"][0]["cue_index"] == 1
    assert seen["priority_raw_findings"][0]["base_text_sha256"] == _sha("当前文字".encode())
    assert seen["initial_provider_adjudication_count"] == 23
    audit = result.chat_authority_audit["final_review_audit"]
    assert audit["cpa_adjudication_usage"]["provider_adjudication_count"] == 25
    assert result.chat_authority_audit["final_review_history"][0]["applied_count"] == 18
    assert result.chat_authority_audit["final_review_history"][0]["context_adjudication_count"] == 23
    assert result.chat_authority_audit["final_review_audit"]["boundary_receipt_replay"] == {
        "source_full_window": {
            "status": "REPLAYED",
            "source_srt_sha256": "sha256:" + _sha(checkpoint["srt"].encode()),
        }
    }
    assert result.srt_text == checkpoint["srt"]
    assert prior["context_audio_adjudication"]["verdict"]["reason_code"] == (
        "ENTITY_AUDIO_PROVIDER_FAILED"
    )


@pytest.mark.parametrize("review_status", ["APPLIED", "FLAGGED"])
def test_guard_rejected_source_review_restarts_from_original_input(
    tmp_path, monkeypatch, review_status
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    original_authority = checkpoint["authority_path"].read_bytes()
    review_calls = []
    replays = []
    real_loader = pipeline._load_pending_source_review_observation

    def ordinary_review(**kwargs):
        review_calls.append(kwargs["initial_provider_adjudication_count"])
        assert len(review_calls) == 1, "restart must reuse the completed observation"
        row = dict(kwargs["priority_raw_findings"][0])
        return kwargs["srt_text"], {
            "schema_version": "final-review-audit.v1",
            "status": review_status,
            "findings": [{
                **row,
                "routed": "disclosure",
                "context_audio_adjudication": _ordinary_context_adjudication(
                    kwargs["srt_text"]
                ),
            }],
            "applied_count": 0,
            "context_adjudication_count": 1,
            "infra_unresolved": [{
                "cue_index": 1, "suspect": "当前文字",
                "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
            }],
            "infra_unresolved_count": 1,
        }

    def load_saved(**kwargs):
        result = real_loader(**kwargs)
        replays.append(result)
        return result

    monkeypatch.setattr(pipeline, "_run_final_review", ordinary_review)
    monkeypatch.setattr(pipeline, "_load_pending_source_review_observation", load_saved)
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    manifest = checkpoint["out_root"] / "candidate.source-text-checkpoint.json"
    frozen_manifest = None
    saved_observations = {}
    for attempt in range(2):
        with pytest.raises(SystemExit, match="FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED"):
            pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
        current_manifest = manifest.read_bytes()
        binding = json.loads(current_manifest)
        assert Path(binding["authority"]["path"]).read_bytes() == original_authority
        assert binding["release_authorized"] is False
        if attempt == 0:
            frozen_manifest = current_manifest
            saved_observations = {
                path: path.read_bytes()
                for path in checkpoint["out_root"].glob("*.pending-source-review.*.json")
            }
            assert len(saved_observations) == 1
        else:
            assert current_manifest == frozen_manifest
            assert all(path.read_bytes() == raw for path, raw in saved_observations.items())
    assert review_calls == [23]
    assert replays[0] is None
    replay = replays[1][1]["source_pending_review_replay"]
    assert replay["provider_calls_this_replay"] == 0
    assert replay["historical_context_adjudication_count"] == 1
    assert replay["release_authorized"] is False


def test_flagged_source_review_keeps_readable_input_after_downstream_failure(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    original = checkpoint["authority_path"].read_bytes()
    calls = []

    def review(**kwargs):
        calls.append(True)
        assert len(calls) == 1
        row = dict(kwargs["priority_raw_findings"][0])
        return kwargs["srt_text"], {
            "schema_version": "final-review-audit.v1", "status": "FLAGGED",
            "findings": [{**row, "routed": "disclosure", "context_audio_adjudication":
                          _ordinary_context_adjudication(kwargs["srt_text"])}],
            "applied_count": 0, "context_adjudication_count": 1,
            "infra_unresolved": [], "infra_unresolved_count": 0,
        }

    def later_failure(**_kwargs):
        raise SystemExit("TEST_DOWNSTREAM_BOUNDARY_FAILURE")

    monkeypatch.setattr(pipeline, "_run_final_review", review)
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    monkeypatch.setattr(pipeline, "review_source_boundary", later_failure)
    for _attempt in range(2):
        with pytest.raises(SystemExit, match="TEST_DOWNSTREAM_BOUNDARY_FAILURE"):
            pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
        binding = json.loads((checkpoint["out_root"] / "candidate.source-text-checkpoint.json").read_bytes())
        assert Path(binding["authority"]["path"]).read_bytes() == original
        assert binding["release_authorized"] is False
    assert calls == [True]


def test_pending_source_stage_strict_validator_still_blocks_without_opt_in(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    checkpoint["spec"].update(semantic_start_ms=0, semantic_end_ms=50)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_FINAL_REVIEW_INFRA_UNRESOLVED",
    ):
        pipeline._validate_current_source_stage_authority(
            authority,
            spec=checkpoint["spec"],
            durations=[100],
            srt_text=checkpoint["srt"],
            srt_sha256=_sha(checkpoint["srt"].encode()),
            clip_context=json.loads(checkpoint["context_path"].read_text()),
            source_truth_ledger_path=None,
        )


def test_pending_source_stage_cannot_treat_omitted_row_as_resolved(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    original_authority = checkpoint["authority_path"].read_bytes()
    original_srt = (checkpoint["out_root"] / "padded.fresh.srt").read_bytes()
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    monkeypatch.setattr(
        pipeline,
        "_run_final_review",
        lambda **_kwargs: (
            _kwargs["srt_text"],
            {
                "schema_version": "final-review-audit.v1",
                "status": "APPLIED",
                "findings": [],
                "applied_count": 0,
                "context_adjudication_count": 0,
                "infra_unresolved": [],
            },
        ),
    )
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_PENDING_REVIEW_OMITTED",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))

    observations = list(checkpoint["out_root"].glob("*.pending-source-review.*.json"))
    assert len(observations) == 1
    observed = json.loads(observations[0].read_text())
    assert observed["pending_review_consumed"] is False
    assert observed["final_review_audit"]["findings"] == []
    assert observed["pending_review_scope"]["rows"][0]["cue_index"] == 1
    assert observed["prior_provider_adjudication_count"] == 23
    assert observed["source_authority_sha256"] == _sha(original_authority)
    assert observed["source_srt_sha256"] == _sha(original_srt)
    assert observed["mutation_authorized"] is False
    assert observed["release_authorized"] is False
    assert checkpoint["authority_path"].read_bytes() == original_authority
    assert (checkpoint["out_root"] / "padded.fresh.srt").read_bytes() == original_srt


def test_pending_source_review_preserves_distinct_rejected_outputs(tmp_path):
    snapshot = {
        "srt_sha256": _sha(b"original"), "srt_text": "original",
        "chat_authority_sha256": "a" * 64, "clip_context_sha256": "b" * 64,
        "padded_sha256": "c" * 64, "source_media_sha256s": ["d" * 64],
    }
    kwargs = dict(snapshot=snapshot, pending_scope={"rows": []},
                  prior_count=23, cid="clip", out_root=tmp_path)
    first = pipeline._persist_pending_source_review_observation(
        **kwargs, reviewed_srt="first staged output",
        resumed_review={"status": "FLAGGED", "findings": [], "context_adjudication_count": 2},
    )
    first_bytes = first.read_bytes()
    again = pipeline._persist_pending_source_review_observation(
        **kwargs, reviewed_srt="first staged output",
        resumed_review={"status": "FLAGGED", "findings": [], "context_adjudication_count": 2},
    )
    second = pipeline._persist_pending_source_review_observation(
        **kwargs, reviewed_srt="second staged output",
        resumed_review={"status": "AUDITOR_UNAVAILABLE", "findings": []},
    )
    assert first == again
    assert first.read_bytes() == first_bytes
    assert second != first
    assert json.loads(first_bytes)["final_review_audit"]["context_adjudication_count"] == 2
    assert json.loads(second.read_text())["reviewed_srt"] == "second staged output"


def test_pending_source_stage_old_request_may_bind_previous_whole_srt(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text())
    finding = authority["final_review_audit"]["findings"][0]
    old_srt = "1\n00:00:00,000 --> 00:00:00,050\n旧的其他 cue\n"
    old_request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "whole_clip_current_srt": old_srt,
        "current_cue": "当前文字",
        "proposed_cue": "当前文字",
        "base_text_sha256": _sha("当前文字".encode()),
        "cue_indexes": [1],
        "matched_start_ms": 0,
        "matched_end_ms": 50,
        "source_media_timeline_offset_ms": 0,
    }
    old_request["request_sha256"] = _sha(
        json.dumps(old_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    finding["context_audio_adjudication"]["request"] = old_request
    authority["final_review_audit"]["findings"] = [finding]
    scope = pipeline._pending_source_stage_review_scope(
        authority["final_review_audit"],
        srt_text=checkpoint["srt"],
        srt_sha256=_sha(checkpoint["srt"].encode()),
        current_authority_srt_sha256=authority["final_output_srt_sha256"],
    )
    assert scope["rows"][0]["current_srt_sha256"] == (
        "sha256:" + _sha(checkpoint["srt"].encode())
    )
    assert scope["rows"][0]["base_text_sha256"] == _sha("当前文字".encode())


def test_pending_source_stage_changed_review_uses_saved_existing_srt_witness(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    original_srt = "1\n00:00:00,000 --> 00:00:00,050\n原始初稿\n"
    original_path = tmp_path / "existing.srt"
    original_path.write_text(original_srt, encoding="utf-8")
    original_sha = _sha(original_srt.encode())
    padded_sha = _sha(checkpoint["padded"].read_bytes())
    checkpoint["spec"].update(
        candidate_id=checkpoint["cid"],
        existing_subtitle_surface={
            "schema_version": "existing-subtitle-surface.v1",
            "candidate_id": checkpoint["cid"],
            "path": str(original_path),
            "sha256": "sha256:" + original_sha,
            "source_media_sha256": "sha256:" + padded_sha,
            "time_domain": "padded_local",
            "authority_kind": "PIPELINE_REVIEWED_CPA_SURFACE_NOT_HUMAN_TRUTH",
        },
    )
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority["input_srt_sha256"] = original_sha
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    changed_srt = checkpoint["srt"].replace("当前文字", "普通修正")
    seen = {}

    def ordinary_review(**kwargs):
        row = dict(kwargs["priority_raw_findings"][0])
        row["base_text_sha256"] = _sha("普通修正".encode())
        return changed_srt, {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [{
                **row,
                "routed": "disclosure",
                "context_audio_adjudication": _ordinary_context_adjudication(
                    kwargs["srt_text"]
                ),
            }],
            "applied_count": 1,
            "context_adjudication_count": 1,
            "infra_unresolved": [],
        }

    monkeypatch.setattr(pipeline, "_run_final_review", ordinary_review)
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])

    def finalize(**kwargs):
        seen["witness"] = kwargs["source_language_witness_srt"]
        return pipeline.TextEvidenceResult(
            srt_text=kwargs["srt_text"],
            cues=[cue for cue in pipeline.parse_srt_cues(kwargs["srt_text"]) if cue.text.strip()],
            chat_authority_path=checkpoint["authority_path"],
        )

    monkeypatch.setattr(pipeline, "_finalize_text_evidence", finalize)
    adapters = SimpleNamespace(profile_asset_file=lambda _name: tmp_path / "known-songs.json")
    result = pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint, adapters=adapters))
    assert seen["witness"] == original_srt
    assert seen["witness"] != checkpoint["srt"]
    assert result.srt_text == changed_srt


def test_pending_consumption_accepts_other_cue_repairs_but_rejects_target_drift():
    original = "1\n00:00:00,000 --> 00:00:00,050\n当前文字\n\n2\n00:00:00,060 --> 00:00:00,090\n其他旧字\n"
    row = {"cue_index": 1, "suspect": "当前文字", "base_text_sha256": _sha("当前文字".encode())}
    live = original.replace("其他旧字", "其他新字")
    finding = {**row, "context_audio_adjudication": _ordinary_context_adjudication(live)}
    assert pipeline._pending_review_consumed({"rows": [row]}, {"findings": [finding]}, original)
    finding["context_audio_adjudication"]["request"]["suspect"] = "另一个疑点"
    assert not pipeline._pending_review_consumed({"rows": [row]}, {"findings": [finding]}, original)
    finding["context_audio_adjudication"] = _ordinary_context_adjudication(live.replace("当前文字", "不同目标"))
    assert not pipeline._pending_review_consumed({"rows": [row]}, {"findings": [finding]}, original)


@pytest.mark.parametrize('has_request',[False,True])
@pytest.mark.parametrize('damage',[None,'base','invalid_plan','duplicate'])
def test_pending_exact_review_carries_useful_plan_without_old_authority(has_request,damage):
    text='1\n00:00:00,000 --> 00:00:01,000\n喝口水\n'
    plan={'action':'reframe_question','question':'比较数量词是否改变当前喝水请求',
        'why_previous_failed':'旧问只逐字比较未比较整句','expected_gain':'用整句命题区分',
        'verification':'同主体同对象同动作且不改时间与原字'}
    row={'cue_index':1,'base_text_sha256':_sha('喝口水'.encode()),'suspect':'喝口水',
        'proposed_full_cue':None,'why':'需要比较整句','continuation_plan':plan,
        'mutation_authority':{'status':'PASS'}}
    if has_request:
        row['exact_release_adjudication']=_ordinary_context_adjudication(text)
    if damage=='base':
        row['base_text_sha256']='b'*64
    elif damage=='invalid_plan':
        plan['action']='try_again'
    findings=[row]
    if damage=='duplicate':
        unplanned=copy.deepcopy(row)
        unplanned.pop('continuation_plan')
        findings=[unplanned,row]
    authority={'final_review_audit':{'release_gate':'BLOCK','reviewed_srt_sha256':'sha256:'+_sha(text.encode()),'findings':findings}}
    pending=pipeline._pending_current_exact_findings(text,authority)
    if damage=='base':
        assert pending==[]
    else:
        assert len(pending)==1
        assert ('continuation_plan' in pending[0]) is (damage!='invalid_plan')
        if damage!='invalid_plan':
            assert pending[0]['continuation_plan']==plan
        assert 'mutation_authority' not in pending[0]


@pytest.mark.parametrize('damage',[None,'context','base','legacy','meaning'])
def test_pending_consumption_accepts_only_new_current_contextual_equivalence(damage):
    from src.autoslice.final_review_auditor import audit_final_subtitles
    from tests.test_final_review_meaning_policy import equivalent
    text='1\n00:00:00,000 --> 00:00:01,000\n喝口水\n'
    payload={'findings':[{'cue':1,'kind':'context','suspect':'喝口水',
        'repair_class':'disclosure_only','proposed_full_cue':None,'why':'同一喝水请求',
        'content_risk':'noncritical_uncertainty','risk_basis':'前后均暂停聊天喝水',
        'meaning_equivalence':equivalent()}]}
    findings=audit_final_subtitles(text,llm_call=lambda _:json.dumps(payload,ensure_ascii=False),
        extract_json=json.loads,candidate_context_text='前后均暂停聊天喝水')
    pending={'rows':[{'cue_index':1,'suspect':'喝口水','base_text_sha256':_sha('喝口水'.encode())}]}
    if damage=='context':
        text=text.replace('喝口水','不喝水')
    elif damage=='base':
        findings[0]['base_text_sha256']='a'*64
    elif damage=='legacy':
        findings[0].pop('current_content_risk')
    elif damage=='meaning':
        findings[0]['current_content_risk']['meaning_equivalence']=None
    assert pipeline._pending_review_consumed(pending,{'findings':findings},text) is (damage is None)


def _pending_rebuilt_span_fixture():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request

    srt = "1\n00:00:00,000 --> 00:00:00,050\n完整错字句\n"
    row = {"cue_index": 1, "suspect": "完整错字句", "base_text_sha256": _sha("完整错字句".encode()),
           "span_start_codepoint": 0, "span_end_codepoint": 5}
    finding = {**row, "suspect": "错字", "suggestion": "正确", "span_start_codepoint": 2,
               "span_end_codepoint": 4, "proposed_full_cue": "完整正确句"}
    audit = _ordinary_context_adjudication(srt, repaired=True)
    request = audit["request"]
    request.pop("request_sha256")
    request.update(suspect="错字", replacement="正确", proposed_cue="完整正确句",
                   context_start_ms=0, context_end_ms=50)
    request["request_sha256"] = _sha(json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode())
    witness = build_witness_request(request)
    geometry_keys = ("kind", "cue_indexes", "matched_start_ms", "matched_end_ms",
                     "context_start_ms", "context_end_ms", "source_media_timeline_offset_ms")
    geometry_sha = _sha(json.dumps(
        {key: witness.get(key) for key in geometry_keys},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode())
    audit.update(
        timing_immutable=True, decision_authority="CPA_JUDGE", rebuilt_finding=dict(finding),
        mutation_authority={"status": "PASS"},
        witness_judge={"judge": {"status": "JUDGED", "choice": "PROPOSED",
            "selected_candidate_text": "完整正确句", "check_request_sha256": request["request_sha256"]}},
        proposal_rebuild={"status": "PROPOSED", "witness_reuse": {
            "status": "PASS", "basis": "IDENTICAL_AUDIO_GEOMETRY",
            "geometry_sha256": "sha256:" + geometry_sha,
            "original_witness_request_sha256": "a" * 64,
            "reused_witness_request_sha256": "a" * 64,
            "rebuilt_witness_request_sha256": witness["request_sha256"],
        }},
    )
    finding["context_audio_adjudication"] = audit
    return srt, row, finding


def test_pending_consumption_accepts_normal_whole_cue_rebuilt_minimal_edit():
    srt, row, finding = _pending_rebuilt_span_fixture()
    assert pipeline._pending_review_consumed({"rows": [row]}, {"findings": [finding]}, srt)


@pytest.mark.parametrize("drift", ["missing_rebuild", "span", "proposal", "request", "geometry",
                                  "witness", "judge", "mutation", "outside_nominated_span"])
def test_pending_rebuilt_span_rejects_unbound_or_unjudged_changes(drift):
    srt, row, finding = _pending_rebuilt_span_fixture()
    audit = finding["context_audio_adjudication"]
    if drift == "missing_rebuild":
        audit.pop("proposal_rebuild")
    elif drift == "span":
        finding["span_start_codepoint"] = 1
    elif drift == "proposal":
        finding["proposed_full_cue"] = "没有裁决的新句"
    elif drift == "request":
        audit["request"]["context_end_ms"] = 60
    elif drift == "geometry":
        audit["proposal_rebuild"]["witness_reuse"]["geometry_sha256"] = "b" * 64
    elif drift == "witness":
        audit["proposal_rebuild"]["witness_reuse"]["reused_witness_request_sha256"] = "b" * 64
    elif drift == "judge":
        audit["witness_judge"]["judge"]["selected_candidate_text"] = "旧提案"
    elif drift == "mutation":
        audit["mutation_authority"]["status"] = "BLOCK"
    else:
        row.update(suspect="字", span_start_codepoint=3, span_end_codepoint=4)
    assert not pipeline._pending_review_consumed({"rows": [row]}, {"findings": [finding]}, srt)


def _saved_pending_review_fixture(tmp_path):
    srt, row, finding = _pending_rebuilt_span_fixture()
    snapshot = {"srt_sha256": _sha(srt.encode()), "srt_text": srt,
                "chat_authority_sha256": "a" * 64, "clip_context_sha256": "b" * 64,
                "padded_sha256": "c" * 64, "source_media_sha256s": ["sha256:" + "d" * 64],
                "clip_context": {}}
    scope = {"status": "PENDING_REVIEW", "rows": [row]}
    reviewed = srt.replace("完整错字句", "完整正确句")
    observation = {
        "schema_version": "source-stage-pending-review-observation.v1", "authority": "EVIDENCE_ONLY",
        "candidate_id": "candidate", "source_srt_sha256": snapshot["srt_sha256"],
        "source_srt": srt, "source_authority_sha256": snapshot["chat_authority_sha256"],
        "clip_context_sha256": snapshot["clip_context_sha256"], "padded_sha256": snapshot["padded_sha256"],
        "source_media_sha256s": snapshot["source_media_sha256s"], "pending_review_scope": scope,
        "prior_provider_adjudication_count": 23, "reviewed_srt": reviewed,
        "reviewed_srt_sha256": _sha(reviewed.encode()), "pending_review_consumed": False,
        "mutation_authorized": False, "release_authorized": False,
        "final_review_audit": {"schema_version": "final-review-audit.v1", "status": "APPLIED",
            "findings": [finding], "applied_count": 1, "context_adjudication_count": 7},
    }
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(observation), encoding="utf-8")
    kwargs = {"spec": {"source_pending_review_observation": {"path": str(path), "sha256": _sha(path.read_bytes())}},
              "snapshot": snapshot, "pending_scope": scope, "prior_count": 23, "cid": "candidate"}
    return kwargs, observation, path


def test_saved_pending_review_replays_typed_bytes_and_keeps_actual_historical_usage(tmp_path):
    kwargs, observation, path = _saved_pending_review_fixture(tmp_path)
    before = path.read_bytes()
    srt, review = pipeline._load_pending_source_review_observation(**kwargs)
    assert srt == observation["reviewed_srt"]
    assert review["context_adjudication_count"] == 7
    assert review["source_pending_review_replay"]["provider_calls_this_replay"] == 0
    assert review["source_pending_review_replay"]["release_authorized"] is False
    assert path.read_bytes() == before
    assert "source_pending_review_replay" not in observation["final_review_audit"]


def test_worker_automatically_consumes_its_saved_pending_observation(tmp_path):
    kwargs, observation, _path = _saved_pending_review_fixture(tmp_path)
    snapshot = kwargs["snapshot"]
    snapshot["chat_authority_path"] = tmp_path / "candidate.chat-authority.json"
    saved = pipeline._persist_pending_source_review_observation(
        snapshot=snapshot, pending_scope=kwargs["pending_scope"],
        reviewed_srt=observation["reviewed_srt"], resumed_review=observation["final_review_audit"],
        prior_count=kwargs["prior_count"], cid=kwargs["cid"], out_root=tmp_path,
    )
    kwargs["spec"] = {}
    srt, review = pipeline._load_pending_source_review_observation(**kwargs)
    assert srt == observation["reviewed_srt"]
    assert review["source_pending_review_replay"]["provider_calls_this_replay"] == 0
    kwargs["spec"]["title"] = "只改标题不改变语义审查输入"
    assert pipeline._load_pending_source_review_observation(**kwargs)[0] == srt
    original = saved.read_bytes()
    kwargs["pending_scope"] = {"status": "PENDING_REVIEW", "rows": []}
    assert pipeline._load_pending_source_review_observation(**kwargs) is None
    assert saved.read_bytes() == original


def test_semantic_contract_change_invalidates_only_saved_review(tmp_path, monkeypatch):
    kwargs, observation, _ = _saved_pending_review_fixture(tmp_path)
    kwargs["snapshot"]["chat_authority_path"] = tmp_path / "candidate.chat-authority.json"
    saved = pipeline._persist_pending_source_review_observation(
        snapshot=kwargs["snapshot"], pending_scope=kwargs["pending_scope"],
        reviewed_srt=observation["reviewed_srt"], resumed_review=observation["final_review_audit"],
        prior_count=kwargs["prior_count"], cid=kwargs["cid"], out_root=tmp_path,
    )
    before = saved.read_bytes()
    kwargs["spec"] = {}
    monkeypatch.setattr(pipeline, "_pending_source_review_contract_sha256", lambda: "sha256:" + "f" * 64)
    assert pipeline._load_pending_source_review_observation(**kwargs) is None
    assert saved.read_bytes() == before


def _unpointed_pending_review(tmp_path):
    kwargs, observation, _ = _saved_pending_review_fixture(tmp_path)
    kwargs["snapshot"]["checkpoint_root"] = tmp_path
    saved = pipeline._persist_pending_source_review_observation(
        snapshot=kwargs["snapshot"], pending_scope=kwargs["pending_scope"],
        reviewed_srt=observation["reviewed_srt"], resumed_review=observation["final_review_audit"],
        prior_count=kwargs["prior_count"], cid=kwargs["cid"], out_root=tmp_path,
    )
    (tmp_path / "candidate.pending-source-review.json").unlink()
    kwargs["spec"] = {}
    return kwargs, saved


def test_missing_pointer_replays_complete_immutable_review_without_writes(tmp_path):
    kwargs, saved = _unpointed_pending_review(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    srt, review = pipeline._load_pending_source_review_observation(**kwargs)
    assert srt == json.loads(saved.read_bytes())["reviewed_srt"]
    assert review["source_pending_review_replay"]["provider_calls_this_replay"] == 0
    assert review["source_pending_review_replay"]["release_authorized"] is False
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("drift", ["source", "authority", "context", "padded", "media", "scope", "usage", "contract"])
def test_unpointed_old_inputs_invalidate_only_review(tmp_path, monkeypatch, drift):
    kwargs, saved = _unpointed_pending_review(tmp_path)
    before = saved.read_bytes()
    if drift == "contract":
        monkeypatch.setattr(pipeline, "_pending_source_review_contract_sha256", lambda: "sha256:" + "f" * 64)
    elif drift == "scope":
        kwargs["pending_scope"] = {"status": "PENDING_REVIEW", "rows": []}
    elif drift == "usage":
        kwargs["prior_count"] += 1
    else:
        key = {"source": "srt_sha256", "authority": "chat_authority_sha256", "context": "clip_context_sha256",
               "padded": "padded_sha256", "media": "source_media_sha256s"}[drift]
        kwargs["snapshot"][key] = ["sha256:" + "f" * 64] if drift == "media" else "f" * 64
    assert pipeline._load_pending_source_review_observation(**kwargs) is None
    assert saved.read_bytes() == before


@pytest.mark.parametrize("damage", ["partial", "hash", "symlink", "candidate", "typed_output"])
def test_unpointed_invalid_observation_blocks_without_repairing_pointer(tmp_path, damage):
    kwargs, saved = _unpointed_pending_review(tmp_path)
    if damage == "partial":
        saved.write_bytes(b'{"incomplete":')
        new = saved.with_name("candidate.pending-source-review." + hashlib.sha256(saved.read_bytes()).hexdigest() + ".json")
        saved.rename(new)
    elif damage == "hash":
        saved.write_bytes(saved.read_bytes() + b" ")
    elif damage == "symlink":
        target = tmp_path / "original-observation.json"
        saved.rename(target)
        saved.symlink_to(target)
    else:
        observation = json.loads(saved.read_bytes())
        if damage == "candidate":
            observation["candidate_id"] = "another-candidate"
        else:
            observation["reviewed_srt"] = observation["reviewed_srt"].replace("完整正确句", "未经裁决内容")
            observation["reviewed_srt_sha256"] = hashlib.sha256(observation["reviewed_srt"].encode()).hexdigest()
        raw = json.dumps(observation, ensure_ascii=False).encode()
        new = saved.with_name("candidate.pending-source-review." + hashlib.sha256(raw).hexdigest() + ".json")
        saved.unlink()
        new.write_bytes(raw)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CURRENT_SOURCE_REVIEW_OBSERVATION"):
        pipeline._load_pending_source_review_observation(**kwargs)
    assert not (tmp_path / "candidate.pending-source-review.json").exists()


def test_unpointed_conflicting_current_reviews_are_rejected(tmp_path):
    kwargs, first = _unpointed_pending_review(tmp_path)
    observation = json.loads(first.read_bytes())
    observation["final_review_audit"]["findings"][0]["why"] = "同输入的另一份审查解释"
    raw = json.dumps(observation, ensure_ascii=False).encode()
    second = first.with_name("candidate.pending-source-review." + hashlib.sha256(raw).hexdigest() + ".json")
    second.write_bytes(raw)
    before = first.read_bytes(), second.read_bytes()
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OBSERVATION_AMBIGUOUS"):
        pipeline._load_pending_source_review_observation(**kwargs)
    assert (first.read_bytes(), second.read_bytes()) == before


def test_unpointed_equivalent_replay_receipts_remain_readable(tmp_path):
    kwargs, first = _unpointed_pending_review(tmp_path)
    srt, review = pipeline._load_pending_source_review_observation(**kwargs)
    second = pipeline._persist_pending_source_review_observation(
        snapshot=kwargs["snapshot"], pending_scope=kwargs["pending_scope"], reviewed_srt=srt,
        resumed_review=review, prior_count=kwargs["prior_count"], cid=kwargs["cid"], out_root=tmp_path,
    )
    assert second != first
    (tmp_path / "candidate.pending-source-review.json").unlink()
    assert pipeline._load_pending_source_review_observation(**kwargs)[0] == srt


def test_stale_pointer_does_not_hide_complete_current_observation(tmp_path):
    kwargs, old = _unpointed_pending_review(tmp_path)
    observation = json.loads(old.read_bytes())
    kwargs["snapshot"]["chat_authority_sha256"] = "e" * 64
    current = pipeline._persist_pending_source_review_observation(
        snapshot=kwargs["snapshot"], pending_scope=kwargs["pending_scope"],
        reviewed_srt=observation["reviewed_srt"], resumed_review=observation["final_review_audit"],
        prior_count=kwargs["prior_count"], cid=kwargs["cid"], out_root=tmp_path,
    )
    pointer = tmp_path / "candidate.pending-source-review.json"
    pointer.write_text(json.dumps({"path": str(old), "sha256": _sha(old.read_bytes())}))
    srt, review = pipeline._load_pending_source_review_observation(**kwargs)
    assert srt == observation["reviewed_srt"]
    assert review["source_pending_review_replay"]["observation_sha256"] == "sha256:" + _sha(current.read_bytes())
    assert json.loads(pointer.read_bytes())["path"] == str(old)


def _native_checkpoint(checkpoint):
    from src.autoslice.producer_text_checkpoint import write_current_text_checkpoint

    root = checkpoint["out_root"]
    current = root / "candidate.current.srt"
    current.write_text(checkpoint["srt"])
    clock = pipeline._checkpoint_current_source_clock(
        pipeline._current_checkpoint_clock_namespace({
            "current_srt_path": root / "padded.fresh.srt",
            "chat_authority_path": checkpoint["authority_path"],
        }), root,
    )
    manifest = root / "candidate.current-text-checkpoint.json"
    write_current_text_checkpoint(manifest, candidate_id=checkpoint["cid"],
        srt_path=current, authority_path=checkpoint["authority_path"],
        context_path=checkpoint["context_path"], padded=checkpoint["padded"], source_clock=clock)
    return manifest, current


def _completed_current_review(srt):
    grid = pipeline.cue_grid_sha256(pipeline.parse_srt_cues(srt))
    source = {'status':'PASS','review_scope':'source_full_window','request_sha256':'sha256:'+'a'*64,
              'cue_grid_sha256':grid}
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    review = {'schema_version':'final-review-audit.v2','meaning_policy':POLICY_VERSION,'status':'CLEAN','release_gate':'PASS',
        'reviewed_srt_sha256':'sha256:' + _sha(srt.encode()),'discovery':{'status':'COMPLETE'},
        'correction_mutation_authority':{'schema_version':'subtitle-correction-mutation-audit.v1','status':'PASS'},
        'correction_pass':{'applied_count':0},'findings':[],'validated_finding_count':0,
        'current_delivery_source_projection':{'source_final_start_ms':0,'source_final_end_ms':100},
        'boundary_semantic_review':{'status':'PASS','review_scope':'final_delivery',
            'request_sha256':'sha256:'+'b'*64,'cue_grid_sha256':grid,
            'final_endpoint_binding':{'schema_version':'talk-boundary-final-endpoint-binding.v1','status':'PASS',
                'recommended_end_cue_index':1,'recommended_end_ms':100,'final_closure_cue_index':1,
                'final_snapped_end_ms':100,'semantic_cue_grid_sha256':grid,'final_cue_grid_sha256':grid,'reason_codes':[]},
            'source_separation_witness':{'schema_version':'talk-boundary-source-separation-witness.v1','status':'PASS',
                'source_review_sha256':'sha256:'+'c'*64,'source_request_sha256':source['request_sha256'],
                'source_cue_grid_sha256':grid,'source_final_start_ms':0,'source_final_end_ms':100,'reason_codes':[]}}}
    return review, source


@pytest.mark.parametrize('damage',[None,'source_bytes','source_hash','source_request','source_grid','critical','clock','discovery'])
def test_source_scope_checkpoint_resumes_only_as_input_with_separately_validated_clock(tmp_path, damage):
    srt='1\n00:00:00,000 --> 00:00:00,050\n当前文字\n'
    clock,source = _completed_current_review(srt)
    grid=source['cue_grid_sha256']
    source_path=tmp_path/'source.srt';source_path.write_text(srt)
    review=copy.deepcopy(clock)
    review['boundary_semantic_review']={**source,'schema_version':'talk-boundary-semantic-review.v1',
        'candidate_id':'candidate','current_delivery_text_evidence':{
            'role':'CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN','delivery_cue_grid_sha256':grid,
            'source_cue_grid_sha256':grid,'projected_source_cue_grid_sha256':grid}}
    review['current_delivery_source_projection'].update(schema_version='delivery-to-source-timeline-projection.v1',
        status='PASS',delivery_srt_cue_grid_sha256=grid,projected_source_cue_grid_sha256=grid)
    review['source_boundary_context']={'schema_version':'source-boundary-transcript-context.v1','status':'PASS',
        'source_witness_request_sha256':source['request_sha256'],'source_cue_grid_sha256':grid,
        'source_srt_path':str(source_path),'source_srt_sha256':'sha256:'+_sha(srt.encode())}
    authority={'schema_version':'chat-authority-audit.v2','final_output_srt_sha256':_sha(srt.encode()),
        'final_review_audit':review,'entity_repairs':[]}
    if damage=='source_bytes':source_path.write_text(srt+'\n')
    elif damage=='source_hash':review['source_boundary_context']['source_srt_sha256']='sha256:'+'f'*64
    elif damage=='source_request':review['source_boundary_context']['source_witness_request_sha256']='sha256:'+'f'*64
    elif damage=='source_grid':review['source_boundary_context']['source_cue_grid_sha256']='sha256:'+'f'*64
    elif damage=='critical':review.update(findings=[{'content_risk':'meaning_reversal'}],validated_finding_count=1)
    elif damage=='clock':clock['boundary_semantic_review']['final_endpoint_binding'].pop('final_snapped_end_ms')
    elif damage=='discovery':review['discovery']={'status':'AUDITOR_UNAVAILABLE'}
    before=copy.deepcopy(authority)
    kwargs=dict(srt_text=srt,srt_sha256=_sha(srt.encode()),clip_context={},candidate_id='candidate',source_clock_review=clock)
    with pytest.raises(pipeline.CurrentTextContinuationError):pipeline._continuation_validate_existing_authority(authority,**kwargs)
    if damage:
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._continuation_validate_existing_authority(authority,allow_source_scope_input=True,**kwargs)
    else:
        assert pipeline._continuation_validate_existing_authority(authority,allow_source_scope_input=True,**kwargs)==review
        with pytest.raises(pipeline.FinalReviewContractError):pipeline.validate_final_review_release(review,expected_srt_sha256='sha256:'+_sha(srt.encode()))
    assert authority==before


@pytest.mark.parametrize('damage',[None,'srt','endpoint','critical','missing_parent'])
def test_current_source_scope_selects_only_valid_same_input_terminal_successor(tmp_path, monkeypatch, damage):
    checkpoint = _checkpoint(tmp_path)
    manifest,_ = _native_checkpoint(checkpoint)
    original = json.loads(manifest.read_text())
    old_authority = Path(original['authority']['path']).read_bytes()
    live = json.loads(checkpoint['authority_path'].read_text())
    review, source = _completed_current_review(checkpoint['srt'])
    live['final_review_audit'] = review
    current = checkpoint['out_root']/'candidate.final.srt'
    current.write_text(checkpoint['srt'] if damage != 'srt' else checkpoint['srt'].replace('当前','另一段'))
    live['final_text_srt_path'] = str(current)
    if damage == 'endpoint': review['boundary_semantic_review']['final_endpoint_binding'].pop('final_snapped_end_ms')
    elif damage == 'critical': review.update(findings=[{'content_risk':'meaning_reversal'}],validated_finding_count=1)
    elif damage == 'missing_parent':
        original['source_clock'].pop('authority');manifest.write_text(json.dumps(original))
    checkpoint['authority_path'].write_text(json.dumps(live))
    calls=[]
    monkeypatch.setattr(pipeline,'_continuation_source_clock_reference',lambda *a,**k:(source,{}))
    monkeypatch.setattr(pipeline,'_continuation_validate_existing_authority',
        lambda authority,**k:calls.append(k) or authority['final_review_audit'])
    kwargs=dict(spec=checkpoint['spec'],durations=[100],padded=checkpoint['padded'],padded_dur=100,
        cid=checkpoint['cid'],out_root=checkpoint['out_root'])
    if damage:
        with pytest.raises((pipeline.CurrentTextContinuationError,pipeline.FinalReviewContractError)):
            pipeline._automatic_current_checkpoint_snapshot(**kwargs)
        assert json.loads(manifest.read_text()) == original
    else:
        restored=pipeline._automatic_current_checkpoint_snapshot(**kwargs)
        assert restored['chat_authority']['final_review_audit'] == review and len(calls)==1
        promoted=json.loads(manifest.read_text())
        assert promoted['source_clock']==original['source_clock']
        assert Path(original['authority']['path']).read_bytes()==old_authority


@pytest.mark.parametrize('damage',[None,'srt','source','parent','usage'])
def test_current_checkpoint_carries_later_usage_without_importing_verdict(damage):
    proof={key:'original' for key in ('source_srt_sha256','parent_authority_sha256','parent_clip_context_sha256',
        'filler_plan_sha256','padded_sha256','source_timeline_offset_ms','source_endpoint')}
    parent={'retained_reviewed_draft':proof,'post_transcript_entity_output_srt_sha256':'a'*64,
        'cpa_adjudication_usage':{'provider_adjudication_count':84}}
    live=copy.deepcopy(parent)
    live['cpa_adjudication_usage']['provider_adjudication_count']=92
    live['final_review_audit']={'schema_version':'final-review-audit.v2','reviewed_srt_sha256':'sha256:'+'b'*64,
        'release_gate':'BLOCK','status':'FLAGGED','unknown_request':'preserved raw request'}
    if damage=='srt':live['final_review_audit']['reviewed_srt_sha256']='sha256:'+'c'*64
    elif damage=='source':live['post_transcript_entity_output_srt_sha256']='c'*64
    elif damage=='parent':live['retained_reviewed_draft']['padded_sha256']='different'
    elif damage=='usage':live['cpa_adjudication_usage']['provider_adjudication_count']=None
    before=copy.deepcopy((parent,live))
    if damage in {'source','parent','usage'}:
        with pytest.raises(pipeline.CurrentTextContinuationError):pipeline._later_current_cpa_usage(parent,live,srt_sha256='b'*64)
    else:
        usage=pipeline._later_current_cpa_usage(parent,live,srt_sha256='b'*64)
        assert usage == ({'provider_adjudication_count':92} if damage is None else None)
    assert (parent,live)==before


@pytest.mark.parametrize('damage',[None,'legacy_projection','invalid_projection','srt','clock','source','critical','postburn'])
def test_completed_current_review_replay_keeps_all_existing_quality_gates(damage):
    srt='1\n00:00:00,000 --> 00:00:00,050\n当前文字\n'
    completed,source=_completed_current_review(srt)
    if damage == 'legacy_projection': completed.pop('current_delivery_source_projection')
    elif damage == 'invalid_projection': completed['current_delivery_source_projection'] = None
    before=copy.deepcopy(completed)
    postburn={'status':'CLEAN','release_gate':'PASS'}
    if damage=='srt':srt=srt.replace('当前','不同')
    elif damage=='source':source['request_sha256']='sha256:'+'d'*64
    elif damage=='critical':completed.update(findings=[{'content_risk':'meaning_reversal'}],validated_finding_count=1)
    elif damage=='postburn':postburn={'status':'FLAGGED','release_gate':'BLOCK'}
    kwargs=dict(srt_text=srt,source_review=source,timeline_offset_ms=1 if damage=='clock' else 0,
                source_final_end_ms=100,postburn=postburn)
    if damage=='critical':
        with pytest.raises(pipeline.FinalReviewContractError):pipeline._reusable_completed_current_review(completed,**kwargs)
    else:
        result=pipeline._reusable_completed_current_review(completed,**kwargs)
        assert result==(before if damage in {None,'legacy_projection'} else None)


def test_completed_current_review_callback_does_not_dispatch_again(tmp_path):
    srt='1\n00:00:00,000 --> 00:00:00,050\n当前文字\n'
    completed,source=_completed_current_review(srt)
    before=copy.deepcopy(completed)
    callback=pipeline._build_exact_final_review_callback(spec={},out_root=tmp_path,cid='candidate',
        padded=tmp_path/'padded.mp4',adapters=None,authoritative_chat=[],clip_context={},
        final_review_audit={'boundary_semantic_review':source,'applied_count':0},
        verify_confusable_entity=None,truth_ownership=None,screen_read_probe=None,
        continuation_correction=True,completed_final_review=completed)
    assert callback(srt,{'postburn_subtitle_audio_review':{'status':'CLEAN','release_gate':'PASS'}},0,100)==before
    assert completed==before


def test_normal_snapshot_prefers_native_current_bytes_and_frozen_original_clock(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    manifest, current = _native_checkpoint(checkpoint)
    old_source = checkpoint["out_root"] / "padded.fresh.srt"
    old_source.write_text("旧文件后来改变不能被猜作当前稿")
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint["spec"], durations=[100], padded=checkpoint["padded"], padded_dur=100,
        cid=checkpoint["cid"], out_root=checkpoint["out_root"],
    )
    assert snapshot["current_srt_path"] != current
    current.write_text("下轮工作稿被部分覆写，完整 checkpoint 仍保存原件")
    assert snapshot["srt_text"] == checkpoint["srt"]
    native = json.loads(manifest.read_bytes())
    assert native["time_domain"] == "delivery_local" and native["release_authorized"] is False
    assert Path(native["source_clock"]["srt"]["path"]).read_text() == checkpoint["srt"]


@pytest.mark.parametrize('valid', [True, False])
def test_later_delivery_is_validated_before_old_source_can_be_restored(tmp_path, monkeypatch, valid):
    from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    root, cid = checkpoint['out_root'], checkpoint['cid']
    source = root / 'padded.fresh.srt'
    write_source_text_checkpoint(root / f'{cid}.source-text-checkpoint.json', candidate_id=cid,
        srt_path=source, authority_path=checkpoint['authority_path'],
        context_path=checkpoint['context_path'], padded=checkpoint['padded'])
    current = source.read_text().replace('当前文字', '合法修复后的文字')
    delivery = root / 'replacement_recuts' / f'{cid}.recut.srt'
    delivery.parent.mkdir()
    delivery.write_text(current)
    authority = json.loads(checkpoint['authority_path'].read_text())
    authority.update(final_text_srt_path=str(delivery), final_output_srt_sha256=_sha(current.encode()),
        final_review_audit={'schema_version': 'final-review-audit.v2', 'status': 'FLAGGED'},
        exact_final_cpa_self_heal={'status': 'REVIEW_PENDING', 'repairs_retained': 'original typed chain'})
    checkpoint['authority_path'].write_text(json.dumps(authority))
    before = {p:p.read_bytes() for p in (source, delivery, checkpoint['authority_path'])}
    calls = []
    def validate(actual, **kwargs):
        calls.append(actual)
        assert actual == authority and kwargs['srt_text'] == current
        if not valid:
            raise pipeline.CurrentTextContinuationError('ORIGINAL_TYPED_CHAIN_INVALID')
        return actual['final_review_audit']
    monkeypatch.setattr(pipeline, '_continuation_validate_existing_authority', validate)
    monkeypatch.setattr(pipeline, '_continuation_project_delivery_to_source', lambda *a, **k: ([], {}))
    kwargs = dict(spec=checkpoint['spec'], durations=[100], padded=checkpoint['padded'],
        padded_dur=100, cid=cid, out_root=root)
    if not valid:
        with pytest.raises(pipeline.CurrentTextContinuationError, match='ORIGINAL_TYPED_CHAIN_INVALID'):
            pipeline._automatic_current_checkpoint_snapshot(**kwargs)
        assert not (root / f'{cid}.current-text-checkpoint.json').exists()
    else:
        result = pipeline._automatic_current_checkpoint_snapshot(**kwargs)
        assert result['srt_text'] == current
        assert result['chat_authority'] == authority
        assert result['native_checkpoint']['time_domain'] == 'delivery_local'
        assert result['native_checkpoint']['release_authorized'] is False
    assert len(calls) == 1
    assert {p:p.read_bytes() for p in before} == before


@pytest.mark.parametrize("drift", ["srt", "authority", "context", "padded", "domain", "release", "candidate"])
def test_native_drift_never_falls_back_to_old_source_or_transcriber(tmp_path, drift, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    manifest, current = _native_checkpoint(checkpoint)
    native = json.loads(manifest.read_bytes())
    if drift in {"srt", "authority", "context", "padded"}:
        Path(native[drift]["path"]).write_bytes(b"drift")
    else:
        native[{"domain": "time_domain", "release": "release_authorized", "candidate": "candidate_id"}[drift]] = {
            "domain": "padded_local", "release": True, "candidate": "neighbour"}[drift]
        manifest.write_text(json.dumps(native))
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "_transcribe_draft", lambda **_kwargs: pytest.fail("transcriber ran"))
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


def test_retained_parent_is_validated_before_resuming_its_newer_delivery(tmp_path, monkeypatch):
    from src.autoslice import retained_reviewed_draft
    checkpoint = _checkpoint(tmp_path)
    checkpoint['spec']['retained_current_text'] = {'original': 'unchanged input declaration'}
    authority = json.loads(checkpoint['authority_path'].read_text())
    authority['final_review_audit']['schema_version'] = 'final-review-audit.v2'
    checkpoint['authority_path'].write_text(json.dumps(authority))
    validated, snapshot, continuation, result = {}, {}, object(), object()
    calls = []
    def load(**kwargs):
        calls.append('original parent validation')
        return validated
    def current(**kwargs):
        assert calls == ['original parent validation']
        assert kwargs['validated_retained'] is validated
        return snapshot
    monkeypatch.setattr(retained_reviewed_draft, 'load_retained_reviewed_draft', load)
    monkeypatch.setattr(pipeline, '_collect_timeline_chat', lambda *a: ([], []))
    monkeypatch.setattr(pipeline, '_automatic_current_checkpoint_snapshot', current)
    monkeypatch.setattr(pipeline, '_automatic_current_text_checkpoint', lambda **k: continuation)
    monkeypatch.setattr(pipeline, '_run_current_text_continuation', lambda **k: result)
    monkeypatch.setattr(pipeline, '_transcribe_draft', lambda **k: pytest.fail('older retained text was restored'))
    assert pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint)) is result


def test_retained_current_checkpoint_rejects_a_different_original_parent(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    _native_checkpoint(checkpoint)
    validated = {'padded_sha256':_sha(checkpoint['padded'].read_bytes()),
                 'source_media_sha256s':['sha256:' + checkpoint['source_sha']],
                 'piece_output_sha256s':[_sha((checkpoint['out_root']/'piece_0_0_100.mp4').read_bytes())],
                 'proof':{'source_srt_sha256':'sha256:' + 'f' * 64}}
    with pytest.raises(pipeline.CurrentTextContinuationError, match='RETAINED_PARENT_DRIFT'):
        pipeline._automatic_current_checkpoint_snapshot(spec=checkpoint['spec'], durations=[100],
            padded=checkpoint['padded'], padded_dur=100, cid=checkpoint['cid'],
            out_root=checkpoint['out_root'], validated_retained=validated)


@pytest.mark.parametrize('drift', [False, True])
def test_retained_clock_uses_the_hash_bound_parent_piece_path(tmp_path, monkeypatch, drift):
    checkpoint = _checkpoint(tmp_path)
    root = checkpoint['out_root']
    old_piece = root / 'piece_0_0_100.mp4'
    piece = tmp_path / 'parent-piece.mp4'
    old_piece.rename(piece)
    piece_sha = _sha(piece.read_bytes())
    provenance = json.loads(checkpoint['padded'].with_suffix('.provenance.json').read_text())
    provenance['inputs'][0]['path'] = str(piece)
    checkpoint['padded'].with_suffix('.provenance.json').write_text(json.dumps(provenance))
    fields = ('source_srt_sha256', 'parent_authority_sha256', 'parent_clip_context_sha256',
              'filler_plan_sha256', 'padded_sha256', 'source_timeline_offset_ms', 'source_endpoint')
    proof = {key:'original frozen binding' for key in fields}
    authority = json.loads(checkpoint['authority_path'].read_text())
    authority['retained_reviewed_draft'] = proof
    checkpoint['authority_path'].write_text(json.dumps(authority))
    _native_checkpoint(checkpoint)
    validated = {'padded_sha256':_sha(checkpoint['padded'].read_bytes()),
                 'source_media_sha256s':['sha256:' + checkpoint['source_sha']],
                 'piece_output_sha256s':[piece_sha], 'proof':proof}
    observed = []
    def measured(path, expected, actual, **kwargs):
        observed.append(path)
        return path == piece and (expected, actual) == (100, 101)
    monkeypatch.setattr(pipeline, '_source_piece_container_clock_matches', measured)
    if drift:
        piece.write_bytes(b'changed source media')
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._automatic_current_checkpoint_snapshot(spec=checkpoint['spec'], durations=[101],
                padded=checkpoint['padded'], padded_dur=101, cid=checkpoint['cid'],
                out_root=root, validated_retained=validated)
        assert not observed
    else:
        result = pipeline._automatic_current_checkpoint_snapshot(spec=checkpoint['spec'], durations=[101],
            padded=checkpoint['padded'], padded_dur=101, cid=checkpoint['cid'],
            out_root=root, validated_retained=validated)
        assert result['srt_text'] == checkpoint['srt']
        assert observed == [piece]


@pytest.mark.parametrize("drift", ["hash", "source", "authority", "context", "media", "scope",
                                  "usage", "grid", "output", "count", "judge"])
def test_saved_pending_review_rejects_drift_without_promoting_or_overwriting(tmp_path, drift):
    kwargs, observation, path = _saved_pending_review_fixture(tmp_path)
    if drift == "hash":
        kwargs["spec"]["source_pending_review_observation"]["sha256"] = "f" * 64
    elif drift in {"source", "authority", "context", "media"}:
        field = {"source": "source_srt_sha256", "authority": "source_authority_sha256",
                 "context": "clip_context_sha256", "media": "padded_sha256"}[drift]
        observation[field] = "f" * 64
    elif drift == "scope":
        observation["pending_review_scope"] = {"rows": []}
    elif drift == "usage":
        observation["prior_provider_adjudication_count"] = 0
    elif drift in {"grid", "output"}:
        observation["reviewed_srt"] = observation["reviewed_srt"].replace(
            "00:00:00,050" if drift == "grid" else "完整正确句",
            "00:00:00,060" if drift == "grid" else "额外手改句",
        )
        observation["reviewed_srt_sha256"] = _sha(observation["reviewed_srt"].encode())
    elif drift == "count":
        observation["final_review_audit"]["applied_count"] = 2
    else:
        observation["final_review_audit"]["findings"][0]["context_audio_adjudication"]["witness_judge"]["judge"]["selected_candidate_text"] = "没裁决过"
    if drift != "hash":
        path.write_text(json.dumps(observation), encoding="utf-8")
        kwargs["spec"]["source_pending_review_observation"]["sha256"] = _sha(path.read_bytes())
    before = path.read_bytes()
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CURRENT_SOURCE_REVIEW_OBSERVATION"):
        pipeline._load_pending_source_review_observation(**kwargs)
    assert path.read_bytes() == before


def test_pending_source_stage_changed_review_without_saved_witness_blocks(
    tmp_path, monkeypatch
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    _make_pending_source_review(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text())
    authority["input_srt_sha256"] = _sha(b"missing-original")
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    _stub_source_stage_reconstruction(monkeypatch, checkpoint)
    changed_srt = checkpoint["srt"].replace("当前文字", "普通修正")

    def ordinary_review(**kwargs):
        row = dict(kwargs["priority_raw_findings"][0])
        return changed_srt, {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [{
                **row,
                "routed": "disclosure",
                "context_audio_adjudication": _ordinary_context_adjudication(
                    kwargs["srt_text"]
                ),
            }],
            "applied_count": 1,
            "context_adjudication_count": 1,
            "infra_unresolved": [],
        }

    monkeypatch.setattr(pipeline, "_run_final_review", ordinary_review)
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_MISSING",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


@pytest.mark.parametrize(
    "field",
    [
        "final_source_language_preservation_audit",
        "foreign_script_consistency_audit",
        "transcript_entity_audit",
        "source_subtitle_truth_audit",
        "final_title_mark_balance_audit",
    ],
)
def test_source_stage_requires_each_persisted_guard_before_transcriber(
    tmp_path, monkeypatch, field
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    authority.pop(field)
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun),
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_.*(MISSING|INVALID)",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


@pytest.mark.parametrize("field", ["applied", "sender_repairs", "gift_repairs", "coreference_repairs"])
def test_source_stage_rejects_unsupported_nonempty_owner_lanes(
    tmp_path, monkeypatch, field
):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    authority[field] = [{"cue_indexes": [1]}]
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun),
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match=f"CURRENT_SOURCE_STAGE_{field.upper()}_UNSUPPORTED",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


def test_source_stage_rejects_selection_hook_drift_before_transcriber(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    spec = dict(checkpoint["spec"])
    spec["selection_hook"] = "changed selection hook"
    checkpoint["spec"] = spec
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun),
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_AUTO_RESUME_SELECTION_HOOK_DRIFT",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


def test_source_stage_rejects_changed_title_guard_before_transcriber(tmp_path, monkeypatch):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint)
    authority = json.loads(checkpoint["authority_path"].read_text(encoding="utf-8"))
    authority["final_title_mark_balance_audit"]["status"] = "UNRESOLVED_COMPLEX_IMBALANCE"
    checkpoint["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))

    class TranscriberMustNotRun(Exception):
        pass

    monkeypatch.setattr(
        pipeline,
        "_transcribe_draft",
        lambda **_kwargs: (_ for _ in ()).throw(TranscriberMustNotRun),
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_SOURCE_STAGE_TITLE_MARK_GUARD_DRIFT",
    ):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))


@pytest.mark.parametrize("drift", ["current", "source", "context"])
def test_real_source_stage_drift_blocks_before_transcriber(tmp_path, monkeypatch, drift):
    checkpoint = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(checkpoint, row=_source_stage_row())
    if drift == "current":
        (checkpoint["out_root"] / "padded.fresh.srt").write_text(
            checkpoint["srt"] + "2\n00:00:00,060 --> 00:00:00,070\n漂移\n",
            encoding="utf-8",
        )
    elif drift == "source":
        (checkpoint["out_root"] / "piece_0_0_100.mp4").write_bytes(b"source-drift")
    else:
        context = json.loads(checkpoint["context_path"].read_text(encoding="utf-8"))
        context["candidate_id"] = "different-candidate"
        checkpoint["context_path"].write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    transcriber_calls = []

    def should_not_run(**_kwargs):
        transcriber_calls.append(True)
        raise AssertionError("source-stage drift must stop before VAD/transcriber")

    monkeypatch.setattr(pipeline, "_transcribe_draft", should_not_run)
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline.run_text_pipeline(**_pipeline_kwargs(checkpoint))
    assert transcriber_calls == []
