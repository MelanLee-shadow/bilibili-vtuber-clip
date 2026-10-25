"""Persisted CPA audio obligations survive only on their original source target."""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_auditor import (
    _candidate_blind_missing_proposal_scaffold,
    adjudicate_context_finding,
    build_context_adjudication_request,
)
from tests.test_semantic_audio_recovery import SOURCE, missing_proposal_case


OFFSET = 30000
PADDED_SOURCE = (
    "1\n00:00:00,000 --> 00:00:10,000\n更早的内容\n\n"
    "2\n00:00:31,000 --> 00:00:33,000\n前面开始解释\n\n"
    "3\n00:00:33,000 --> 00:00:33,720\n了\n\n"
    "4\n00:00:33,720 --> 00:00:36,000\n后面把话说完\n"
)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _clip_context():
    return {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "retained-audio-candidate",
        "context_sha256": "sha256:" + "c" * 64,
        "pieces": [{"source_media_sha256": "sha256:" + "d" * 64}],
        "whole_clip_draft_srt_sha256": "sha256:" + _sha(PADDED_SOURCE),
        "speech_memory": {"ledger_sha256": "sha256:" + "e" * 64},
    }


def _old_finding(*, padded=False):
    """Use the normal typed gate fixture and request builder, before saving it."""
    finding, _ = missing_proposal_case()
    srt, cue_index, offset = (PADDED_SOURCE, 3, 0) if padded else (SOURCE, 2, OFFSET)
    finding["cue_index"] = cue_index
    request = build_context_adjudication_request(
        srt, _candidate_blind_missing_proposal_scaffold(srt, finding),
        clip_context=_clip_context(), source_media_timeline_offset_ms=offset,
    )
    adjudication = finding["exact_release_adjudication"]
    adjudication["request"] = request
    gate = adjudication["cpa_missing_proposal_convergence"]
    gate["cue_index"] = cue_index
    gate["final_srt_sha256"] = "sha256:" + _sha(srt)
    return finding


def _pending_row():
    return {
        "cue_index": 2, "kind": "context", "suspect": "了",
        "why": "这处仍需完整话语取证", "base_text_sha256": _sha("了"),
        "proposed_full_cue": None, "repair_class": "phonetic",
        "exact_release_adjudication": {
            "status": "UNCERTAIN", "repaired": False,
            "reason_code": "SEMANTIC_SAME_INPUT_RETRY_BLOCKED",
        },
    }


def _persist_authority(tmp_path, old, *, channel="history", current_rows=None):
    """Save the original parent first, then cold-read the continuation artifact."""
    old_review = {"reviewed_srt_sha256": "sha256:" + _sha(SOURCE), "findings": [old]}
    parent_path = tmp_path / "original-authority.json"
    parent_path.write_text(json.dumps({"final_review_audit": old_review}, ensure_ascii=False))
    authority = {
        "final_review_audit": {
            "release_gate": "BLOCK", "reviewed_srt_sha256": "sha256:" + _sha(SOURCE),
            "findings": deepcopy(current_rows if current_rows is not None else [_pending_row()]),
        },
    }
    if channel == "history":
        authority["retained_delivery_review_history"] = {
            "parent": {"path": str(parent_path),
                       "sha256": "sha256:" + hashlib.sha256(parent_path.read_bytes()).hexdigest(),
                       "role": "PENDING_OBSERVATIONS_ONLY"},
            "review": deepcopy(old_review), "role": "HISTORY_AND_REVIEW_NOMINATIONS_ONLY",
        }
    else:
        authority["final_review_audit"]["correction_pass"] = {"findings": [{
            "cue_index": old["cue_index"],
            "context_audio_adjudication": deepcopy(old["exact_release_adjudication"]),
        }]}
    path = tmp_path / "continuation-authority.json"
    path.write_text(json.dumps(authority, ensure_ascii=False))
    return json.loads(path.read_text()), parent_path


def _restore(authority, *, srt=SOURCE, context=None, offset=OFFSET):
    return pipeline._retained_pending_audio_findings(
        srt, authority, clip_context=context if context is not None else _clip_context(),
        timeline_offset_ms=offset,
    )


@pytest.mark.parametrize("channel,padded", [("history", False), ("correction", False),
                                             ("history", True), ("correction", True)])
def test_saved_pending_request_reaches_normal_range_observer(tmp_path, monkeypatch, channel, padded):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    old = _old_finding(padded=padded)
    authority, parent = _persist_authority(tmp_path, old, channel=channel)
    frozen_authority, frozen_parent = deepcopy(authority), parent.read_bytes()
    rows = _restore(authority)
    assert len(rows) == 1
    row = rows[0]
    assert row["cue_index"] == 2 and row["proposed_full_cue"] is None
    assert row["exact_release_adjudication"] == old["exact_release_adjudication"]
    assert "mutation_authority" not in row
    observed = []

    def verifier(_):
        pytest.fail("failed range must stop before another exact witness or text decision")

    verifier.observe_range_context = lambda **kwargs: observed.append(kwargs) or [
        {"status": "UNCERTAIN", "reason_code": "EXTERNAL_AUDIO_FAILURE"}]

    def judge(_):
        return json.dumps({"decision": "OBSERVE", "first_cue": 1, "last_cue": 3,
                           "reason": "完整话语补听"})

    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    output, audit = adjudicate_context_finding(
        SOURCE, row, entity_verifier=verifier, clip_context=_clip_context(),
        source_media_timeline_offset_ms=OFFSET, judge_llm_call=judge,
    )
    assert output == SOURCE and audit["repaired"] is False
    assert len(observed) == 1
    assert observed[0]["range_witness_request"]["cue_indexes"] == [1, 2, 3]
    assert audit["semantic_audio_recovery"]["status"] == "BLOCK"
    assert audit["semantic_audio_recovery"]["mutation_authorized"] is False
    assert row["exact_release_adjudication"] == old["exact_release_adjudication"]
    assert authority == frozen_authority and parent.read_bytes() == frozen_parent


@pytest.mark.parametrize("change", ["request_hash", "request_payload", "prompt_hash", "repaired", "pass"])
def test_invalid_or_completed_old_gate_is_not_restored(tmp_path, change):
    old = _old_finding()
    adjudication = old["exact_release_adjudication"]
    if change == "request_hash":
        adjudication["request"]["request_sha256"] = "f" * 64
    elif change == "request_payload":
        adjudication["request"]["suspect"] = "另一个旧疑点"
    elif change == "prompt_hash":
        adjudication["cpa_missing_proposal_convergence"]["prompt_sha256"] = "invalid"
    elif change == "repaired":
        adjudication["repaired"] = True
    else:
        adjudication["mutation_authority"] = {"status": "PASS"}
    authority, _ = _persist_authority(tmp_path, old)
    assert _restore(authority) == []


@pytest.mark.parametrize("change", ["source", "candidate"])
def test_current_context_drift_cannot_restore_old_gate(tmp_path, change):
    authority, _ = _persist_authority(tmp_path, _old_finding())
    context = _clip_context()
    if change == "source":
        context["pieces"][0]["source_media_sha256"] = "sha256:" + "f" * 64
    else:
        context["candidate_id"] = "another-candidate"
    assert _restore(authority, context=context) == []


# Admin (G6 class ruling, 41 updated): a rebuilt whole-clip context
# (context / draft-SRT / speech-memory hashes) is a recorded delta, not another
# audio question; these variants asserted the former rule and now restore.
@pytest.mark.parametrize("padded", [False, True])
@pytest.mark.parametrize("change", ["context", "whole_draft", "speech_memory"])
def test_rebuilt_whole_clip_context_still_restores_the_old_gate(tmp_path, change, padded):
    old = _old_finding(padded=padded)
    authority, _ = _persist_authority(tmp_path, old)
    context = _clip_context()
    if change == "speech_memory":
        context["speech_memory"]["ledger_sha256"] = "sha256:" + "f" * 64
    else:
        key = {"context": "context_sha256", "whole_draft": "whole_clip_draft_srt_sha256"}[change]
        context[key] = "sha256:" + "f" * 64
    rows = _restore(authority, context=context)
    assert len(rows) == 1
    assert rows[0]["exact_release_adjudication"] == old["exact_release_adjudication"]


@pytest.mark.parametrize("change", ["word", "time", "offset", "not_pending"])
def test_old_gate_does_not_resurrect_changed_or_nonpending_cue(tmp_path, change):
    authority, _ = _persist_authority(tmp_path, _old_finding())
    srt, offset = SOURCE, OFFSET
    if change == "word":
        srt = SOURCE.replace("\n了\n", "\n说了\n")
    elif change == "time":
        srt = SOURCE.replace("00:00:03,000 --> 00:00:03,720", "00:00:03,010 --> 00:00:03,720")
    elif change == "offset":
        offset += 1
    else:
        authority["final_review_audit"]["findings"] = []
    assert _restore(authority, srt=srt, offset=offset) == []


@pytest.mark.parametrize("change", ["missing_parent", "missing_file", "parent_bytes", "embedded_review", "role"])
def test_unbound_retained_history_cannot_authorize_audio(tmp_path, change):
    authority, parent = _persist_authority(tmp_path, _old_finding())
    retained = authority["retained_delivery_review_history"]
    if change == "missing_parent":
        retained.pop("parent")
    elif change == "missing_file":
        parent.unlink()
    elif change == "parent_bytes":
        parent.write_text(parent.read_text() + "\n")
    elif change == "embedded_review":
        retained["review"]["findings"][0]["exact_release_adjudication"]["request"]["suspect"] = "别的"
    else:
        retained["role"] = "UNVERIFIED_HISTORY"
    assert _restore(authority) == []


@pytest.mark.parametrize("channel", ["current", "correction"])
def test_latest_explicit_no_audio_supersedes_old_obligation(tmp_path, channel):
    old = _old_finding()
    latest = deepcopy(old)
    latest["exact_release_adjudication"]["cpa_missing_proposal_convergence"]["needs_audio"] = False
    authority, _ = _persist_authority(tmp_path, old)
    if channel == "current":
        authority["final_review_audit"]["findings"] = [latest]
    else:
        authority["final_review_audit"]["correction_pass"] = {"findings": [{
            "context_audio_adjudication": latest["exact_release_adjudication"],
        }]}
    assert _restore(authority) == []


def test_current_pending_gate_wins_without_history_overwrite_or_duplicate(tmp_path):
    old, current = _old_finding(), _old_finding()
    current["exact_release_adjudication"]["cpa_missing_proposal_convergence"]["reason"] = "当前完整话语仍待取证"
    current["base_text_sha256"] = _sha("了")
    authority, _ = _persist_authority(tmp_path, old, current_rows=[current])
    original = deepcopy(authority)
    restored = _restore(authority)
    existing = pipeline._pending_current_exact_findings(SOURCE, authority)
    assert len(restored) == len(existing) == 1
    assert restored[0]["exact_release_adjudication"] == current["exact_release_adjudication"]
    assert existing[0]["exact_release_adjudication"] == current["exact_release_adjudication"]
    assert authority == original


def test_normal_discovery_keeps_current_gate_when_history_nominates_same_cue(tmp_path, monkeypatch):
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass

    authority, _ = _persist_authority(tmp_path, _old_finding())
    retained = _restore(authority)
    current = _old_finding()
    current["base_text_sha256"] = _sha("了")
    current["exact_release_adjudication"]["cpa_missing_proposal_convergence"]["reason"] = "更新的当前听音义务"
    authority["final_review_audit"]["findings"] = [current]
    before = deepcopy(authority)
    seen = []

    # Keep both nomination producers independent at the ordinary merge boundary.
    # The already verified history row is deliberately older than the current gate.
    monkeypatch.setattr(pipeline, "_retained_pending_audio_findings", lambda *_a, **_kw: retained)
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda *_a: "")
    monkeypatch.setattr(pipeline, "discover_candidate_pronoun_findings", lambda *_a, **_kw: ([], {}))

    def discovery(_srt, **kwargs):
        seen.extend(kwargs["extra_raw_findings"])
        raise pipeline.FinalReviewAuditError("FIXTURE_DISCOVERY_BOUNDARY")

    monkeypatch.setattr(pipeline, "audit_final_subtitles", discovery)
    receipt = pipeline._run_exact_final_release_review(
        srt_text=SOURCE, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook="", clip_context=_clip_context(),
        timeline_offset_ms=OFFSET, verified_authority_audit=authority,
        final_review_llm=lambda _: pytest.fail("merge test must not call a provider"),
    )
    assert receipt["reason_codes"] == ["FIXTURE_DISCOVERY_BOUNDARY"]
    assert receipt["pending_current_exact_review"]["cue_indexes"] == [2]
    assert len(seen) == 1
    assert seen[0]["exact_release_adjudication"] == current["exact_release_adjudication"]
    assert seen[0]["proposed_full_cue"] is None
    assert authority == before


# Admin the source binding (candidate, recordings) is the cross-clock
# identity; a missing context hash is only a recorded delta (former variant
# "context_sha256" now restores, see the test below).
@pytest.mark.parametrize("missing", ["candidate_id", "source_media_sha256s"])
def test_source_to_delivery_requires_complete_context_identity(tmp_path, missing):
    context = _clip_context()
    if missing == "source_media_sha256s":
        context["pieces"] = []
    else:
        context.pop(missing)
    old = _old_finding(padded=True)
    # Rebuild a genuine original request with the incomplete identity; a valid
    # request hash alone must not grant permission to map between local clocks.
    old["exact_release_adjudication"]["request"] = build_context_adjudication_request(
        PADDED_SOURCE, _candidate_blind_missing_proposal_scaffold(PADDED_SOURCE, old),
        clip_context=context, source_media_timeline_offset_ms=0,
    )
    authority, _ = _persist_authority(tmp_path, old)
    assert _restore(authority, context=context) == []


def test_source_to_delivery_without_a_context_hash_keeps_the_source_binding_identity(tmp_path):
    context = _clip_context()
    context.pop("context_sha256")
    old = _old_finding(padded=True)
    old["exact_release_adjudication"]["request"] = build_context_adjudication_request(
        PADDED_SOURCE, _candidate_blind_missing_proposal_scaffold(PADDED_SOURCE, old),
        clip_context=context, source_media_timeline_offset_ms=0,
    )
    authority, _ = _persist_authority(tmp_path, old)
    assert len(_restore(authority, context=_clip_context())) == 1
