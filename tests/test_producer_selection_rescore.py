import copy
import hashlib
import json
import pytest
from src.autoslice import producer_selection_rescore as producer
from src.autoslice import selection_rescore
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import review_and_repair_source_facts


def sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, monkeypatch, *, canonical=False):
    cid = "ordinary_candidate"
    root = (
        tmp_path
        / ("out/2026-10-11" if canonical else "private-runs/saved/output")
        / cid
        / "replacement_recuts"
    )
    root.mkdir(parents=True)
    srt = root / f"{cid}.recut.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n有人提议技能叫李姐拉拉\n\n2\n00:00:02,000 --> 00:00:04,000\n不行，我这个应该叫李姐网\n"
    )
    cues = [
        SourceCue("text_final_0001", 0, 2000, "有人提议技能叫李姐拉拉", "zh", "speech", 1.0),
        SourceCue("text_final_0002", 2000, 4000, "不行，我这个应该叫李姐网", "zh", "speech", 1.0),
    ]
    old = {"tier_basis": "audience_driven_performance", "tier_reason": "命名已完成"}
    hook = "技能已经被命名成李姐拉拉。"
    title = "【李豆沙】技能已经被命名成李姐拉拉"
    response = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "REPAIR",
        "final_selection_hook": "弹幕提议把技能叫李姐拉拉，主播随即拒绝。",
        "final_title": "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝",
        "supported_by": ["final_transcript"],
        "addressee_attribution": [],
        "changed_surfaces": [
            {
                "artifact": "selection_hook",
                "before": "技能已经被命名",
                "after": "弹幕提议把技能叫",
                "reason": "提议被拒绝",
                "evidence": ["不行，我这个应该叫李姐网"],
            },
            {
                "artifact": "title",
                "before": "技能已经被命名成李姐拉拉",
                "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                "reason": "提议被拒绝",
                "evidence": ["不行，我这个应该叫李姐网"],
            },
        ],
        "selection_scorecard_review": {
            "status": "INCOMPATIBLE",
            "reason": "旧卡依赖已经命名的事实",
        },
        "summary": "保留提议和拒绝",
    }
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="\n".join((c.text for c in cues)),
        clip_context_prompt="",
        selection_scorecard=old,
        llm_call=lambda _: json.dumps(response, ensure_ascii=False),
        candidate_id=cid,
        final_reviewed_srt_path=srt,
    )
    assert review["decision"] == "REPAIR_SCORECARD_STALE", review
    media = root / f"{cid}.burned.mp4"
    media.write_bytes(b"actual bound media fixture")
    ass = root / f"{cid}.ass"
    ass.write_bytes(b"bound ASS fixture")
    authority = root.parent / f"{cid}.chat-authority.json"
    authority.write_text('{"actual":"authority"}')
    record = {
        "burned_preview": {
            "status": "BURNED",
            "path": str(media),
            "burned_sha256": sha(media),
            "ass_path": str(ass),
        },
        "artifact_hashes": {"burned_video_sha256": sha(media), "ass_sha256": sha(ass)},
        "chat_authority_audit_path": str(authority),
    }
    monkeypatch.setattr(
        producer, "load_validated_subtitle_audio_witness", lambda *_a, **_k: {"status": "PASS"}
    )
    spec = {
        "candidate_id": cid,
        "date": "2026-10-11",
        "selection_hook": hook,
        "selection_scorecard": old,
        "pieces": [
            {
                "remote_media": "source.flv",
                "start_ms": 0,
                "end_ms": 4000,
                "source_media_sha256": "sha256:" + "a" * 64,
            }
        ],
    }
    kwargs = dict(
        spec=spec,
        cid=cid,
        recut_dir=root,
        record=record,
        review=review,
        cues=cues,
        context_prompt="",
        stale_card=old,
    )
    return (kwargs, media, authority)


def call_fixture(calls, *, hold=False, fail=False):

    def call(prompt):
        calls.append(prompt)
        if fail:
            raise RuntimeError("unknown transport completion")
        return json.dumps(
            {
                "status": "SUPPORTED",
                "selection_scorecard": {
                    "tier": 2,
                    "tier_basis": "personal_stance",
                    "tier_reason": "可见的拒绝反应",
                    "tier_evidence_cues": [1, 2],
                    "dimensions": {
                        k: 4
                        for k in (
                            "lidousha_centrality",
                            "stance_intensity",
                            "audience_salience",
                            "relationship_interaction",
                            "persona_reversal",
                            "comedic_payoff",
                            "self_contained",
                        )
                    },
                    "uncertainty_penalty": 0,
                    "fatigue_penalty": 0,
                    "candidate_quality": {
                        "watchability": "STRONG",
                        "differentiation": "DISTINCTIVE",
                        "production_disposition": "HOLD" if hold else "PRODUCTION",
                        "reason": "提议被明确拒绝",
                        "evidence_cues": [1, 2],
                    },
                },
            }
        )

    call.cpa_cache_identity = {
        "transport": "offline-fixture",
        "models": ["offline-model"],
        "effort": "medium",
    }
    return call


@pytest.mark.parametrize("canonical", [False, True])
def test_normal_producer_rescores_saved_output_and_reuses_response_without_media_work(
    tmp_path, monkeypatch, canonical
):
    kw, media, authority = fixture(tmp_path, monkeypatch, canonical=canonical)
    original = copy.deepcopy(kw["spec"])
    calls = []
    call = call_fixture(calls)
    bound = {
        p: p.read_bytes() for p in (media, authority, kw["recut_dir"] / f"{kw['cid']}.recut.srt")
    }
    producer.consume_producer_rescore(**kw, llm_call=call)
    assert (
        kw["spec"]["selection_hook"] == kw["review"]["rescore_candidate"]["repaired_selection_hook"]
    )
    kw["spec"] = original
    producer.consume_producer_rescore(**kw, llm_call=call)
    assert len(calls) == 1 and all((p.read_bytes() == raw for p, raw in bound.items()))


def test_interruption_after_complete_response_reuses_original_response(tmp_path, monkeypatch):
    kw, _, _ = fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(kw["spec"])
    calls = []
    call = call_fixture(calls)
    write = producer._write_json_atomic

    def interrupt(path, value):
        if (
            path.parent.name == ".source-fact-rescore-consumptions"
            and value.get("status") == "COMPLETED"
        ):
            raise SystemExit("test interruption after response")
        return write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(producer, "_write_json_atomic", interrupt)
        with pytest.raises(SystemExit, match="test interruption"):
            producer.consume_producer_rescore(**kw, llm_call=call)
    kw["spec"] = original
    producer.consume_producer_rescore(**kw, llm_call=call)
    assert len(calls) == 1


def test_unknown_rescore_is_not_blindly_dispatched_again(tmp_path, monkeypatch):
    kw, _, _ = fixture(tmp_path, monkeypatch)
    calls = []
    call = call_fixture(calls, fail=True)
    for _ in range(2):
        with pytest.raises(SystemExit, match="INFRA_UNRESOLVED"):
            producer.consume_producer_rescore(**kw, llm_call=call)
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["hook", "card", "window", "context", "media", "authority"])
def test_normal_rescore_rejects_changed_bound_subject_before_another_call(
    tmp_path, monkeypatch, damage
):
    kw, media, authority = fixture(tmp_path, monkeypatch)
    original = copy.deepcopy(kw["spec"])
    calls = []
    call = call_fixture(calls)
    producer.consume_producer_rescore(**kw, llm_call=call)
    kw["spec"] = original
    if damage == "hook":
        kw["spec"]["selection_hook"] = "不同看点"
    elif damage == "card":
        kw["stale_card"] = {"tier_basis": "different", "tier_reason": "changed"}
    elif damage == "window":
        kw["spec"]["pieces"][0]["end_ms"] = 3999
    elif damage == "context":
        kw["context_prompt"] = "changed context"
    elif damage == "media":
        media.write_bytes(b"changed media")
    else:
        authority.write_text('{"changed":"authority"}')
    with pytest.raises((ValueError, RuntimeError)):
        producer.consume_producer_rescore(**kw, llm_call=call)
    assert len(calls) == 1


def test_real_low_admission_remains_rejected(tmp_path, monkeypatch):
    kw, _, _ = fixture(tmp_path, monkeypatch)
    calls = []
    with pytest.raises(SystemExit, match="CANDIDATE_REJECTED"):
        producer.consume_producer_rescore(**kw, llm_call=call_fixture(calls, hold=True))
    receipt = json.loads((kw["recut_dir"] / f"{kw['cid']}.source-fact-rescore.json").read_bytes())
    assert receipt["status"] == "FAILED" and len(calls) == 1


def test_normal_staging_consumes_stale_review_then_rebuilds_hook_and_card_without_media(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from src.autoslice import producer_package_finalization as finalization

    kw, media, _ = fixture(tmp_path, monkeypatch)
    calls = []
    call = call_fixture(calls)
    monkeypatch.setattr(producer, "build_final_review_llm_call", lambda: call)
    monkeypatch.setattr(finalization, "load_candidate_cover_reference", lambda *_a, **_k: None)
    monkeypatch.setattr(
        finalization,
        "build_publication_llm_call",
        lambda **_: lambda _p: pytest.fail("staging fixture has no publication calls"),
    )
    stages = []

    class NextNormalStage(Exception):
        pass

    def stage(record, **_):
        stages.append(copy.deepcopy(record["story_contract"]))
        if len(stages) == 2:
            assert (
                record["story_contract"]["selection_hook"]
                == kw["review"]["rescore_candidate"]["repaired_selection_hook"]
            )
            assert record["story_contract"]["selection_scorecard"]["status"] == "VALID"
            raise NextNormalStage
        record["publish_staging"] = {
            "title_authority_status": "BLOCKED_SOURCE_FACT_REVIEW",
            "source_fact_review": kw["review"],
        }
        return record

    recut = finalization.FinalRecutArtifacts(
        kw["recut_dir"], media, kw["recut_dir"] / f"{kw['cid']}.recut.srt", None, None
    )
    with pytest.raises(NextNormalStage):
        finalization._stage_record(
            options=SimpleNamespace(reuse_cover=False),
            spec=kw["spec"],
            cid=kw["cid"],
            recut=recut,
            record=kw["record"],
            adapters=SimpleNamespace(stage_publish_draft=stage),
        )
    assert len(stages) == 2 and len(calls) == 1


@pytest.mark.parametrize("damage", [None, "quantity", "asset", "card_hash"])
def test_pending_rescore_restores_only_original_checksum_proven_editorial_input(damage):
    from src.autoslice.publish_staging import _restore_reviewed_scorecard_input

    old = {"method": "original", "asset_bindings": {"source": "sha256:" + "a" * 64}, "quantity": 10}
    reviewed = {"effective_score": 82.25, "editorial_research": old}
    current = copy.deepcopy(reviewed)
    current["editorial_research"].update(
        input_sha256="derived",
        candidate_projection={
            "schema": "normal-source-entity-candidates.v1",
            "source_kind": "publish_staging_verified_context",
        },
    )
    review = {
        "decision": "REPAIR_SCORECARD_STALE",
        "passes": [
            {
                "editorial_research": old,
                "selection_scorecard_sha256": selection_rescore._sha256_json(reviewed),
            }
        ],
    }
    if damage == "quantity":
        current["editorial_research"]["quantity"] = 11
    elif damage == "asset":
        current["editorial_research"]["asset_bindings"] = {"source": "sha256:" + "b" * 64}
    elif damage == "card_hash":
        review["passes"][0]["selection_scorecard_sha256"] = "sha256:" + "0" * 64
    story = {"selection_scorecard": current, "source_fact_review": review}
    result = _restore_reviewed_scorecard_input(story, allow_pending_rescore=True)
    assert result["selection_scorecard"] == (reviewed if damage is None else current)
