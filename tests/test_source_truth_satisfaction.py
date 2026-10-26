"""Ordinary local truth generation, saved response replay and final ownership."""

from copy import deepcopy
import json

import pytest

from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth
from src.autoslice.source_truth_satisfaction import review_missing_truth, valid_satisfaction
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces


def _fixture(tmp_path, *, decision="EQUIVALENT"):
    text = "1\n00:00:00,000 --> 00:00:01,000\n先喝你的水\n\n2\n00:00:01,000 --> 00:00:02,000\n我喝你水可以吗\n"
    spec = {
        "candidate_id": "fixture",
        "pieces": [
            {
                "remote_media": "/recording/source.flv",
                "source_media_sha256": "sha256:" + "a" * 64,
                "start_ms": 1000,
                "end_ms": 3000,
            }
        ],
    }
    entry = {
        "truth_id": "local-term",
        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
        "evidence_class": "HUMAN_CONFIRMED_LOCAL_TERM",
        "recording_basename": "source.flv",
        "source_media_sha256": "sha256:" + "a" * 64,
        "source_start_ms": 2000,
        "source_end_ms": 3000,
        "action": "replace_substring",
        "replacements": [{"surface": "误词", "canonical": "你的水"}],
        "required_text": "你的水",
        "required": True,
    }
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {"schema_version": "source-subtitle-truth-ledger.v1", "entries": [entry]},
            ensure_ascii=False,
        )
    )
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return json.dumps(
            {
                "decision": decision,
                "corresponding_surface": "你水",
                "reason": "本源窗整句均指喝对方的水",
                "meaning_equivalence": {
                    "status": "EQUIVALENT",
                    "current_meaning": "询问能否喝对方的水",
                    "alternative_meaning": "询问能否喝对方的水",
                    "shared_meaning": "同一喝水请求、同一施受对象",
                    "uncertain_detail": "只省略结构助词",
                    "context_basis": "当前整句和邻句对象一致",
                    "evidence_cue_ids": [2, 1],
                }
                if decision == "EQUIVALENT"
                else None,
            },
            ensure_ascii=False,
        )

    llm.cpa_cache_identity = {
        "transport": "fixture",
        "models": ["fixture-model"],
        "effort": "fixture",
    }
    llm.provider_runtime_binding = {
        "provider_transport": "fixture",
        "provider_credential_source": "fixture",
    }
    llm.completed_review_root = tmp_path / "cache"
    kwargs = dict(
        srt_text=text,
        spec=spec,
        durations=[2000],
        ledger_path=ledger,
        clip_context={"whole_clip_draft_srt": text},
        llm_call=llm,
    )
    _, audit = apply_source_subtitle_truth(text, spec=spec, durations=[2000], ledger_path=ledger)
    return text, spec, ledger, calls, kwargs, audit


def test_ordinary_generate_save_reopen_apply_and_final_owner(tmp_path):
    text, spec, ledger, calls, kwargs, audit = _fixture(tmp_path)
    assert audit["status"] == "FAILED"  # Neighbor literal must not satisfy this window.
    reviews, ids = review_missing_truth(**kwargs, audit=audit)
    again, again_ids = review_missing_truth(**kwargs, audit=audit)
    assert len(calls) == 1 and reviews == again and ids == again_ids
    output, audit = apply_source_subtitle_truth(
        text, spec=spec, durations=[2000], ledger_path=ledger, satisfaction_reviews=reviews
    )
    assert output == text and audit["status"] == "ALREADY_SATISFIED"
    row = audit["satisfied"][0]
    assert row["declared_output_contract"]["required_text"] == "你的水"
    assert row["cue_indexes"] == [2]
    authority = {"source_subtitle_truth_audit": audit}
    assert verify_chat_authority_final_surfaces(
        authority,
        final_text_srt=text,
        final_speaker_srt=text,
        delivery_start_ms=0,
        delivery_end_ms=2000,
    )
    assert authority["final_source_truth_owner_verification"]["status"] == "PASS"
    # A valid source-bound projection can be reused after a pure trim/reindex.
    trimmed = "1\n00:00:00,000 --> 00:00:01,000\n我喝你水可以吗\n"
    trimmed_spec = {"pieces": [{**spec["pieces"][0], "start_ms": 2000}]}
    _, trimmed_audit = apply_source_subtitle_truth(
        trimmed,
        spec=trimmed_spec,
        durations=[1000],
        ledger_path=ledger,
        satisfaction_reviews=reviews,
    )
    assert trimmed_audit["status"] == "ALREADY_SATISFIED"


@pytest.mark.parametrize("decision", ["CONFLICT", "UNRESOLVED"])
def test_negative_and_unknown_are_saved_and_remain_blocked(tmp_path, decision):
    text, spec, ledger, calls, kwargs, audit = _fixture(tmp_path, decision=decision)
    reviews, _ = review_missing_truth(**kwargs, audit=audit)
    review_missing_truth(**kwargs, audit=audit)
    assert len(calls) == 1
    _, result = apply_source_subtitle_truth(
        text, spec=spec, durations=[2000], ledger_path=ledger, satisfaction_reviews=reviews
    )
    assert result["status"] == "FAILED"


@pytest.mark.parametrize(
    "drift", ["wrong_word", "possessor", "window", "source", "context", "raw", "question", "legacy"]
)
def test_saved_equivalence_rejects_drift_and_old_or_forged_shells(tmp_path, drift):
    text, spec, ledger, _, kwargs, audit = _fixture(tmp_path)
    reviews, _ = review_missing_truth(**kwargs, audit=audit)
    proof = deepcopy(reviews[0])
    if drift == "wrong_word":
        text = text.replace("我喝你水", "我喝误水")
    elif drift == "possessor":
        text = text.replace("我喝你水", "我喝她水")
    elif drift == "window":
        spec["pieces"][0]["start_ms"] = 1100
    elif drift == "source":
        spec["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    elif drift == "context":
        text = text.replace("先喝你的水", "别喝他的水")
    elif drift == "raw":
        proof["raw_response"] += "changed"
    elif drift == "question":
        proof["request"]["scope"]["declared_output_contract"]["required_text"] = "她的水"
    else:
        proof = {"status": "PASS", "meaning_equivalent": True}
    _, result = apply_source_subtitle_truth(
        text, spec=spec, durations=[2000], ledger_path=ledger, satisfaction_reviews=[proof]
    )
    if drift == "source":
        assert result["status"] == "NO_RELEVANT_INTERVAL"
        assert not result["satisfied"]
    else:
        assert result["status"] == "FAILED"
    assert not valid_satisfaction(
        proof, audit["failures"][0], srt_text=text, spec=spec, durations=[2000]
    )


def test_final_owner_and_speaker_drift_still_block(tmp_path):
    text, spec, ledger, _, kwargs, audit = _fixture(tmp_path)
    reviews, _ = review_missing_truth(**kwargs, audit=audit)
    _, audit = apply_source_subtitle_truth(
        text, spec=spec, durations=[2000], ledger_path=ledger, satisfaction_reviews=reviews
    )
    for final_text, speaker_text in [
        (text.replace("我喝你水", "我喝她水"), text),
        (text, text.replace("我喝你水", "我喝她水")),
    ]:
        authority = {"source_subtitle_truth_audit": deepcopy(audit)}
        assert not verify_chat_authority_final_surfaces(
            authority,
            final_text_srt=final_text,
            final_speaker_srt=speaker_text,
            delivery_start_ms=0,
            delivery_end_ms=2000,
        )


def test_unbound_provider_never_dispatches(tmp_path):
    _, _, _, calls, kwargs, audit = _fixture(tmp_path)
    del kwargs["llm_call"].provider_runtime_binding
    with pytest.raises(ValueError, match="CHECKPOINT_UNBOUND"):
        review_missing_truth(**kwargs, audit=audit)
    assert calls == []


def test_incomplete_positive_cannot_be_saved_as_a_decision(tmp_path):
    _, _, _, _, kwargs, audit = _fixture(tmp_path)
    original = kwargs["llm_call"]

    def incomplete(_prompt):
        return json.dumps(
            {
                "decision": "EQUIVALENT",
                "reason": "no supported analysis",
                "corresponding_surface": "你水",
                "meaning_equivalence": None,
            }
        )

    incomplete.cpa_cache_identity = original.cpa_cache_identity
    incomplete.provider_runtime_binding = original.provider_runtime_binding
    incomplete.completed_review_root = original.completed_review_root
    kwargs["llm_call"] = incomplete
    with pytest.raises(ValueError, match="POSITIVE_INVALID"):
        review_missing_truth(**kwargs, audit=audit)
    retained = [json.loads(path.read_bytes()) for path in (tmp_path / "cache").rglob("*.json")]
    assert retained and all(row.get("schema_version") == "completed-review-response.v1" for row in retained)
