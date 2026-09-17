"""Exact operator text across recued subtitles must not retain extra words.

Fixtures are synthetic. Tests use the actual truth application and independent
final-surface validator; no media, ASR, CPA or real publication is involved.
"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth
from src.autoslice.producer_text_pipeline import _defer_source_truth_failure_for_redelivery

TEXT = "先说明情况，再讨论方案"


def _stamp(ms):
    seconds, millis = divmod(ms, 1000)
    return f"00:00:{seconds:02d},{millis:03d}"


def _srt(rows):
    return "\n\n".join(f"{i}\n{_stamp(a)} --> {_stamp(b)}\n{t}" for i, (a, b, t) in enumerate(rows, 1)) + "\n"


def _fixture(tmp_path, parts, *, governed=True, state="VERIFIED_ACTIVE", start=1000, end=5000):
    row = {"knowledge_type": "SOURCE_INTERVAL_TRUTH", "truth_id": "synthetic-complete-utterance",
           "recording_basename": "synthetic.mp4", "source_start_ms": 101000, "source_end_ms": 105000,
           "action": "replace_cue", "text": TEXT, "required": True}
    document = {"schema_version": "source-subtitle-truth-ledger.v1", "entries": [row]}
    if governed:
        row.update(revision_id="test-r1", assertion_state=state,
                   authority="synthetic operator fixture, not a real user decision",
                   decision_authority="REVIEWER_OPERATOR_TRUTH", evidence_class="SYNTHETIC_FIXTURE")
        document["governance"] = {"schema_version": "source-subtitle-truth-governance.v2", "governed_entry_start_index": 0}
    path = tmp_path / "synthetic-truth.json"
    path.write_text(json.dumps(document, ensure_ascii=False))
    width = (end - start) // len(parts)
    targets = [(start + i * width, end if i == len(parts) - 1 else start + (i+1)*width, t) for i, t in enumerate(parts)]
    srt = _srt([(0, 700, "前一句保持"), *targets, (5300, 6000, "后一句保持")])
    spec = {"pieces": [{"source_media": "synthetic.mp4", "start_ms": 100000, "end_ms": 106000}]}
    return srt, spec, path


def _apply(srt, spec, ledger):
    return apply_source_subtitle_truth(srt, spec=spec, durations=[6000], ledger_path=ledger)


def _final_accepts(truth, text, speaker=None):
    audit = {"source_subtitle_truth_audit": deepcopy(truth)}
    ok = verify_chat_authority_final_surfaces(audit, final_text_srt=text,
        final_speaker_srt=text if speaker is None else speaker, delivery_start_ms=0, delivery_end_ms=6000)
    return ok, audit


def _target_text(srt):
    return "".join(c.text for c in parse_srt_cues(srt)[1:-1])


@pytest.mark.parametrize("parts", [
    ["其实先说明情况，", "再讨论方案"],
    ["先说明情况，", "再讨论方案真的"],
    ["其实先说明情况，", "再讨论方案真的"],
    ["先说明情况，", "额外再讨论方案"],
])
def test_fully_owned_operator_split_removes_extraneous_words(tmp_path, parts):
    original, spec, ledger = _fixture(tmp_path, parts)
    out, audit = _apply(original, spec, ledger)
    assert audit["status"] == "APPLIED" and not audit["failures"]
    assert _target_text(out) == TEXT
    assert _final_accepts(audit, out)[0]
    before, after = parse_srt_cues(original), parse_srt_cues(out)
    assert [(c.start_ms, c.end_ms) for c in before] == [(c.start_ms, c.end_ms) for c in after]
    assert before[0].text == after[0].text and before[-1].text == after[-1].text
    again, next_audit = _apply(out, spec, ledger)
    assert again == out and next_audit["status"] == "ALREADY_SATISFIED"


@pytest.mark.parametrize("parts", [["先说明情况，", "再讨论方案"], ["先说明情况", "再讨论方案"]])
def test_existing_correct_split_and_punctuation_variants_are_unchanged(tmp_path, parts):
    srt, spec, ledger = _fixture(tmp_path, parts)
    out, audit = _apply(srt, spec, ledger)
    assert out == srt and audit["status"] == "ALREADY_SATISFIED"
    assert _final_accepts(audit, out)[0]


@pytest.mark.parametrize("start,end", [(900, 5000), (1000, 5100)])
def test_partial_cue_scope_cannot_delete_neighbor_words(tmp_path, start, end):
    srt, spec, ledger = _fixture(tmp_path, ["其实先说明情况，", "再讨论方案真的"], start=start, end=end)
    out, audit = _apply(srt, spec, ledger)
    assert out == srt  # The new rule must not broaden the operator's time scope.
    assert not audit["applied"]


def test_legacy_containment_behavior_is_not_silently_promoted_to_operator_exactness(tmp_path):
    srt, spec, ledger = _fixture(tmp_path, ["其实先说明情况，", "再讨论方案"], governed=False)
    out, audit = _apply(srt, spec, ledger)
    assert out == srt and audit["status"] == "ALREADY_SATISFIED"


@pytest.mark.parametrize("state", ["PROPOSED", "REJECTED", "CONFLICTED", "SUPERSEDED"])
def test_inactive_assertions_never_gain_mutation_authority(tmp_path, state):
    srt, spec, ledger = _fixture(tmp_path, ["其实先说明情况，", "再讨论方案"], state=state)
    out, audit = _apply(srt, spec, ledger)
    assert out == srt and not audit["applied"]


def test_unrelated_fully_owned_extra_cue_is_not_silently_discarded(tmp_path):
    srt, spec, ledger = _fixture(tmp_path, ["另外的人", "先说明情况，", "再讨论方案"])
    out, audit = _apply(srt, spec, ledger)
    assert out == srt
    assert audit["status"] == "FAILED"
    assert any(r.get("reason_code") == "REPLACE_CUE_TARGET_NOT_UNIQUE" for r in audit["failures"])
    # A failed application is stopped before final-owner verification; that
    # verifier alone is not the orchestrator's failed-application gate.
    authority_path = tmp_path / "must-not-be-created.json"
    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        _defer_source_truth_failure_for_redelivery(
            spec=spec, source_truth_audit=audit,
            chat_authority_audit={"source_subtitle_truth_audit": audit},
            chat_authority_path=authority_path,
        )
    assert not authority_path.exists() and not audit["applied"]


@pytest.mark.parametrize("wrong_surface", ["text", "speaker", "both"])
def test_final_owner_check_still_rejects_extra_words_on_either_surface(tmp_path, wrong_surface):
    srt, spec, ledger = _fixture(tmp_path, ["先说明情况，", "再讨论方案"])
    out, audit = _apply(srt, spec, ledger)
    bad = out.replace("再讨论方案", "再讨论方案真的")
    text = bad if wrong_surface in {"text", "both"} else out
    speaker = bad if wrong_surface in {"speaker", "both"} else out
    accepted, final_audit = _final_accepts(audit, text, speaker)
    assert not accepted and final_audit["final_verification_failure"] == "SOURCE_TRUTH_FINAL_OWNER_NOT_VERIFIED"


def test_missing_words_still_use_existing_redistribution(tmp_path):
    srt, spec, ledger = _fixture(tmp_path, ["先说明情况，", "再讨论"])
    out, audit = _apply(srt, spec, ledger)
    assert _target_text(out) == TEXT and audit["status"] == "APPLIED"
    assert _final_accepts(audit, out)[0]
