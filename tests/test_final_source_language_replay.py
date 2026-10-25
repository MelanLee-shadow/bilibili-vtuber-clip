import copy
import hashlib
import json

import pytest

from src.autoslice import audio_language_discovery as discovery
from src.autoslice import foreign_span_witness as foreign
from src.autoslice.cue_split_hygiene import merge_release_grade_cues
from src.autoslice.final_source_language_owner import (
    register_final_foreign_script_cpa_repairs,
    register_final_source_language_cpa_repairs,
    validate_final_cpa_owner,
)


INPUT = (
    "1\n00:00:00,000 --> 00:00:01,000\n先听这个\n\n"
    "2\n00:00:01,000 --> 00:00:01,580\n请\n\n"
    "3\n00:00:03,000 --> 00:00:04,660\n完全现场口令说\n"
)
PROPOSED = "完全に無双状態"


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _observation():
    return {
        "cue_index": 3, "start_ms": 3000, "end_ms": 4660,
        "mode": "verbatim", "witnessed": False, "audible_language": "ja",
        "exact_transcript": PROPOSED, "speaker_impression": "single_live_voice",
        "provider": "agy", "audio_sha256": "a" * 64,
    }


def _judge(_prompt):
    return json.dumps({"choice": "PROPOSED", "candidate_id": "PROPOSAL",
                       "needs_audio": False, "reason": "The supplied observation supports this candidate."})


def _save_roundtrip(tmp_path, authority):
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(authority, ensure_ascii=False), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _produced(tmp_path, monkeypatch, *, surface="discovery", predecessors=False):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setenv("AUTOSLICE_CPA_JUDGE_CACHE", "0")
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source-identity-only-no-media-processing")
    authority = {"applied": [], "entity_repairs": []}
    if predecessors:
        authority["entity_repairs"].append({
            "mode": "earlier_verified_owner", "matched_start_ms": 3000,
            "matched_end_ms": 4660, "structured_exact_text": "完全现场口令说",
            "after": ["完全现场口令说"],
        })
        authority["applied"].append({
            "matched_start_ms": 3000, "matched_end_ms": 4660, "exact_text": "口令",
        })
    observation = _observation()
    if surface == "discovery":
        monkeypatch.setattr(foreign, "_witness_rows", lambda **_: [copy.deepcopy(observation)])
        output, audit = discovery.repair_audio_language_candidates(
            media_path=media, srt_text=INPUT, out_root=tmp_path, cid="candidate",
            llm_call=_judge, authority=authority, source_srt=INPUT,
            scan_call=lambda **_: {
                "status": "OBSERVED", "foreign_spans": [
                    {"start_ms": 3000, "end_ms": 4660, "language": "ja"}],
                "semantic_anomalies": [], "speech_turns": [],
            },
        )
        authority["audio_language_discovery_audit"] = audit
    else:
        if surface == "source_language":
            operation = foreign.adjudicate_language_preservation_audit
            audit = {"status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
                     "unproven_foreign_introductions": [{
                         "cue_index": 3, "start_ms": 3000, "end_ms": 4660,
                         "draft": "完全现场口令说", "attempted": "完全现场口令说"}]}
            monkeypatch.setattr(foreign, "_witness_rows", lambda **_: [copy.deepcopy(observation)])
            kwargs = {}
        else:
            operation = foreign.adjudicate_foreign_script_audit
            audit = {"status": "BLOCKED_MIXED_CJK_LATIN_PHRASE", "mixed_cjk_latin_cues": [{
                "cue_index": 3, "start_ms": 3000, "end_ms": 4660, "text": "完全现场口令说"}]}
            kwargs = {"audio_witness_rows": [observation]}
        output, audit = operation(media_path=media, srt_text=INPUT, audit=audit,
                                  out_root=tmp_path, cid="candidate", llm_call=_judge,
                                  source_srt=INPUT, **kwargs)
        # Direct source/foreign audit support is deliberately explicit. Legacy
        # audits that did not save the complete output remain unsupported.
        audit.update(input_srt_sha256=_sha(INPUT), input_srt=INPUT, output_srt=output)
        register = (register_final_source_language_cpa_repairs if surface == "source_language"
                    else register_final_foreign_script_cpa_repairs)
        register(authority, input_srt=INPUT, output_srt=output,
                 **{("source_language_audit" if surface == "source_language"
                     else "foreign_script_audit"): audit})
        authority[("final_source_language_preservation_audit" if surface == "source_language"
                   else "foreign_script_consistency_audit")] = audit
    assert PROPOSED in output
    return _save_roundtrip(tmp_path, authority), output


def _validate(authority, current):
    index = len(authority["entity_repairs"]) - 1
    saved = copy.deepcopy(authority)
    try:
        validate_final_cpa_owner(authority["entity_repairs"][index], row_index=index,
                                 authority=authority, srt_text=current)
    finally:
        assert authority == saved


def test_normal_discovery_writer_saved_owner_replays_after_earlier_cue_merge(tmp_path, monkeypatch):
    authority, output = _produced(tmp_path, monkeypatch)
    merged, receipts = merge_release_grade_cues(output)
    assert receipts == [{"block": 2, "text": "请", "action": "MERGED_INTO_PREV"}]
    assert authority["entity_repairs"][0]["cue_indexes"] == [3]
    assert f"2\n00:00:03,000 --> 00:00:04,660\n{PROPOSED}" in merged
    _validate(authority, merged)


@pytest.mark.parametrize("surface", ["source_language", "foreign_script"])
def test_direct_original_audit_with_explicit_complete_text_replays(tmp_path, monkeypatch, surface):
    authority, output = _produced(tmp_path, monkeypatch, surface=surface)
    _validate(authority, output)
    key = "final_source_language_preservation_audit" if surface == "source_language" else "foreign_script_consistency_audit"
    authority[key].pop("output_srt")
    with pytest.raises(ValueError, match="TEXT_BINDING"):
        _validate(authority, output)


def test_exact_normal_supersessions_are_undone_only_in_copy_and_reproduced(tmp_path, monkeypatch):
    authority, output = _produced(tmp_path, monkeypatch, predecessors=True)
    owner = authority["entity_repairs"][1]
    assert owner["superseded_entity_repair_indexes"] == [0]
    assert owner["superseded_applied_indexes"] == [0]
    _validate(authority, output)
    authority["entity_repairs"][0]["reconciliation"]["before_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="WRITER_EFFECT"):
        _validate(authority, output)


@pytest.mark.parametrize("damage", [
    "raw", "raw_resealed", "receipt", "receipt_hash", "output", "output_hash",
    "input_hash", "missing_input_hash", "applied_count", "duplicate_audit",
    "registration", "missing_registration", "duplicate_registration", "owner_time",
    "owner_words", "current_words", "current_time", "current_duplicate_window",
    "missing_witness", "witness_time", "selected_candidate", "fake_mode",
    "missing_audit", "invalid_owner_index",
])
def test_replay_rejects_drift_without_mutating_originals(tmp_path, monkeypatch, damage):
    authority, current = _produced(tmp_path, monkeypatch)
    audit = authority["audio_language_discovery_audit"]
    original = audit["adjudications"][0]
    receipt = original["cpa_adjudication_rows"][0]
    owner = authority["entity_repairs"][0]
    registrations = authority["final_foreign_script_cpa_surface_registrations"]
    if damage in {"raw", "raw_resealed"}:
        original["audio_witness_rows"][0]["exact_transcript"] = "別のことば"
    elif damage == "receipt":
        receipt["adjudication"]["judge"]["choice"] = "CURRENT"
    elif damage == "receipt_hash":
        owner["final_foreign_script_receipt_sha256"] = "sha256:" + "f" * 64
    elif damage == "output":
        audit["output_srt"] += "\n"
    elif damage == "output_hash":
        audit["output_srt_sha256"] = "f" * 64
    elif damage == "input_hash":
        audit["input_srt_sha256"] = "f" * 64
    elif damage == "missing_input_hash":
        audit.pop("input_srt_sha256")
    elif damage == "applied_count":
        original["applied_count"] = 0
    elif damage == "duplicate_audit":
        audit["adjudications"].append(copy.deepcopy(original))
        audit["applied_count"] = 2
    elif damage == "registration":
        registrations[0]["receipt_sha256"] = "sha256:" + "f" * 64
    elif damage == "missing_registration":
        registrations.clear()
    elif damage == "duplicate_registration":
        registrations.append(copy.deepcopy(registrations[0]))
    elif damage == "owner_time":
        owner["matched_start_ms"] += 1
    elif damage == "owner_words":
        owner["before"] = ["不同的原话"]
    elif damage == "current_words":
        current = current.replace(PROPOSED, "別のことば")
    elif damage == "current_time":
        current = current.replace("00:00:03,000", "00:00:03,001")
    elif damage == "current_duplicate_window":
        current += f"\n4\n00:00:03,000 --> 00:00:04,660\n{PROPOSED}\n"
    elif damage == "missing_witness":
        original["audio_witness_rows"] = []
    elif damage == "witness_time":
        original["audio_witness_rows"][0]["start_ms"] += 1
    elif damage == "selected_candidate":
        receipt["adjudication"]["judge"]["selected_candidate_text"] = "別のことば"
    elif damage == "fake_mode":
        owner["mode"] = "context_audio_adjudication"
    elif damage == "missing_audit":
        authority.pop("audio_language_discovery_audit")
    else:
        with pytest.raises(ValueError, match="OWNER_BINDING"):
            validate_final_cpa_owner(owner, row_index=True, authority=authority, srt_text=current)
        return
    # Most negative controls get an internally consistent outer discovery hash;
    # inner receipts, content and the real writer must still detect the drift.
    if damage != "raw":
        audit["discovery_sha256"] = discovery._audit_sha(audit)
    with pytest.raises(ValueError):
        _validate(authority, current)


def test_unapplied_proposals_are_not_inverse_edits_or_owners(tmp_path, monkeypatch):
    authority, output = _produced(tmp_path, monkeypatch)
    audit = authority["audio_language_discovery_audit"]
    unapplied = copy.deepcopy(audit["adjudications"][0])
    unapplied.pop("applied_count")
    unapplied.pop("output_srt_sha256")
    unapplied["cpa_adjudication_rows"][0]["resolved"] = False
    unapplied["status"] = "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    audit["adjudications"].insert(0, unapplied)
    audit["discovery_sha256"] = discovery._audit_sha(audit)
    _validate(authority, output)


def test_bare_matching_owner_and_registration_without_original_audit_are_rejected(tmp_path, monkeypatch):
    authority, output = _produced(tmp_path, monkeypatch)
    authority.pop("audio_language_discovery_audit")
    with pytest.raises(ValueError, match="ORIGINAL_AUDIT_NOT_UNIQUE"):
        _validate(authority, output)


def test_resealed_discovery_cannot_change_retained_media_binding(tmp_path, monkeypatch):
    authority, output = _produced(tmp_path, monkeypatch)
    audit = authority["audio_language_discovery_audit"]
    authority["retained_source_input"] = {"padded_sha256": audit["source_media_sha256"]}
    _validate(authority, output)
    audit["source_media_sha256"] = "sha256:" + "f" * 64
    audit["discovery_sha256"] = discovery._audit_sha(audit)
    with pytest.raises(ValueError, match="SOURCE_MEDIA_DRIFT"):
        _validate(authority, output)
