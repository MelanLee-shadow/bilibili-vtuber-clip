"""Old writer -> sealed persistence -> current cold release consumer."""

from copy import deepcopy
import json
import socket

import pytest

from src.autoslice import span_finding_obligations as obligations
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
from src.autoslice.span_phoneme_resolution import digest, outside_question_packet
from tests.test_span_phoneme_resolution import Observers, model_for, sha, srt


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy writer controls cannot reach a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def produce_review(monkeypatch, *, offset, applied, legacy, close=True, question_end=3):
    text = srt("你想，甲0呀")
    finding = {
        "cue_index": 1, "suspect": "甲0", "suggestion": "甲零",
        "repair_class": "phonetic", "base_text_sha256": sha("你想，甲0呀"),
        "proposed_full_cue": "你想，甲零呀",
    }
    output, adj = adjudicate_context_finding(
        text, finding, entity_verifier=Observers(),
        source_media_timeline_offset_ms=offset,
        judge_llm_call=model_for(question_span=[0, question_end]),
        require_complete_utterance_support=True,
    )
    assert adj["repaired"]
    repairs = []
    unapplied = []
    if applied:
        produced, repairs = _apply_exact_final_cpa_repairs(
            text, {"findings": [{**finding, "exact_release_adjudication": adj}]}
        )
        assert produced == output and repairs
        text = produced
    else:
        packet = outside_question_packet(adj["request"], adj["verdict"], adj["witness_judge"]["judge"])
        assert packet["binding"]["source_offset_ms"] == offset
        unapplied = [{"after_repair_count": 0, "repair_prefix_sha256": digest([]), "packet": packet}]
    audit = {"reviewed_srt_sha256": "sha256:" + sha(text), "findings": []}
    closures = []
    if close:
        current = "你想，甲零呀" if applied else "你想，甲0呀"
        row = obligations.derive_obligations(repairs, unapplied=unapplied)[0]

        def keep(prompt):
            has_audio = False
            if "\nSPAN_REVIEW_INPUT\n" in prompt:
                data = json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1].split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0])
                has_audio = bool(data["observations"])
            return json.dumps({"choice": "CURRENT", "needs_audio": not has_audio,
                "current_utterance_supported": has_audio,
                "current_utterance_support_reason": "The complete current utterance is audible." if has_audio else "Audio required.",
                "reason": "Keep the source-bound complete current utterance."})

        _, closing = adjudicate_context_finding(
            text, {"cue_index": 1, "suspect": current[:question_end], "suggestion": "你很想，",
                   "repair_class": "spoken_unit", "base_text_sha256": sha(current),
                   "proposed_full_cue": "你很想，" + current[question_end:]},
            entity_verifier=Observers(transcripts={"mai": current, "moss": current}),
            source_media_timeline_offset_ms=offset, judge_llm_call=keep,
            require_complete_utterance_support=True,
        )
        assert obligations._valid_closing_adjudication(
            closing, before=current, after=current, window=[1000, 4000], keep=True
        ), closing
        closures = [{"obligation_id": row["obligation_id"], "codepoint_span": row["codepoint_span"],
                     "adjudication": closing}]
    authority = {obligations.UNAPPLIED_FIELD: unapplied, "span_obligation_closures": closures}
    if repairs:
        authority["exact_final_cpa_self_heal"] = {
            "schema_version": "exact-final-cpa-self-heal-audit.v1", "status": "REVIEW_PENDING",
            "passes": [{"schema_version": "exact-final-cpa-self-heal-pass.v1", "pass_index": 1,
                        "input_srt_sha256": "sha256:" + sha(srt("你想，甲0呀")),
                        "output_srt_sha256": "sha256:" + sha(text), "repairs": repairs}],
        }
    with monkeypatch.context() as writer:
        if legacy:
            original = obligations._born_questions

            def pre_clock_birth(*args, **kwargs):
                rows = original(*args, **kwargs)
                for row in rows.values():
                    row.pop("source_offset_ms", None)
                return rows

            writer.setattr(obligations, "_born_questions", pre_clock_birth)
        obligations.enforce_review(text, authority, audit, timeline_offset_ms=offset)
    return audit


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("offset", [0, 9800, 17340])
@pytest.mark.parametrize("legacy", [False, True])
def test_sealed_writer_roundtrip(monkeypatch, tmp_path, applied, offset, legacy):
    audit = produce_review(monkeypatch, offset=offset, applied=applied, legacy=legacy)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(audit, ensure_ascii=False))
    before = path.read_bytes()
    cold = json.loads(before)
    obligations.validate_release_obligations(cold)
    assert path.read_bytes() == before
    assert cold == audit
    assert all(row["status"] == "RESOLVED" for row in cold[obligations.REVIEW_FIELD]["states"])


@pytest.mark.parametrize("applied", [False, True])
def test_legacy_pending_stays_pending(monkeypatch, applied):
    audit = produce_review(monkeypatch, offset=9800, applied=applied, legacy=True, close=False, question_end=2)
    with pytest.raises(ValueError, match="^FINAL_REVIEW_FINDING_REVIEW_PENDING$"):
        obligations.validate_release_obligations(json.loads(json.dumps(audit)))


@pytest.mark.parametrize("applied", [False, True])
def test_no_other_state_field_or_explicit_offset_is_normalized(monkeypatch, applied):
    audit = produce_review(monkeypatch, offset=9800, applied=applied, legacy=True)
    state = audit[obligations.REVIEW_FIELD]["states"][0]
    for field, value in {**state, "source_offset_ms": 9800}.items():
        changed = deepcopy(audit)
        proof = changed[obligations.REVIEW_FIELD]
        proof["states"][0][field] = value + 1 if type(value) is int else {"tampered": True}
        proof["receipt_sha256"] = digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
        with pytest.raises(ValueError, match="^SPAN_OBLIGATION_STATES_DRIFT$"):
            obligations.validate_release_obligations(changed)
    changed = deepcopy(audit)
    changed[obligations.REVIEW_FIELD]["states"][0]["source_offset_ms"] = 0
    proof = changed[obligations.REVIEW_FIELD]
    proof["receipt_sha256"] = digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError, match="^SPAN_OBLIGATION_STATES_DRIFT$"):
        obligations.validate_release_obligations(changed)
    changed = deepcopy(audit)
    proof = changed[obligations.REVIEW_FIELD]
    proof["states"][0]["origin"]["parent_scope_sha256"] = ["invalid parent"]
    proof["receipt_sha256"] = digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError, match="^SPAN_OBLIGATION_STATES_DRIFT$"):
        obligations.validate_release_obligations(changed)


def test_outer_and_packet_seals_remain_required(monkeypatch):
    audit = produce_review(monkeypatch, offset=9800, applied=False, legacy=True)
    changed = deepcopy(audit)
    changed[obligations.REVIEW_FIELD]["states"][0]["cue_index"] += 1
    with pytest.raises(ValueError, match="^SPAN_OBLIGATION_REVIEW_DRIFT$"):
        obligations.validate_release_obligations(changed)
    changed = deepcopy(audit)
    proof = changed[obligations.REVIEW_FIELD]
    proof[obligations.UNAPPLIED_FIELD][0]["packet"]["binding"]["source_offset_ms"] += 1
    proof["receipt_sha256"] = digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
    with pytest.raises(ValueError, match="^SPAN_PENDING_PACKET_DRIFT$"):
        obligations.validate_release_obligations(changed)
