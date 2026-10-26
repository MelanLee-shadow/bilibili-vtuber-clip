"""G2/G3: the same physical target under two local clocks is one target.

Rule: 41「共同核当前请求、物理源、源/交付时钟和原始音频/答复字节」; guimi 69eb9ae
cue38 (padded-clock audit offset 0 vs delivery request offset 9800, one physical
target) and cue36 (native row proven only through another observation). A
local-millisecond representation is not identity; a genuinely different
physical target still drifts. Synthetic WAV and protocol stubs; no provider.
"""
from copy import deepcopy

from src.autoslice import final_review_auditor as auditor
from src.autoslice import targeted_relisten as policy
from src.autoslice.independent_context_adjudication import (
    same_physical_cue_grid, validate_range_acoustic_observation,
)
from test_targeted_relisten_public_contract import (  # noqa: F401  (rig fixture)
    FINDING, SRT, acquire_narrow, acquire_range, observe_plan, pending_finding, rig,
)


def _shift(srt, delta_ms):
    from src.autoslice.jingting_chunker import parse_srt_cues
    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{stamp(c.start_ms + delta_ms)} --> {stamp(c.end_ms + delta_ms)}\n{c.text}\n\n"
                   for i, c in enumerate(parse_srt_cues(srt), 1))


def _range_observation(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    _, audit, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert audit["status"] == "OBSERVED", audit.get("reason_code")
    return audit


def test_physical_grid_is_clock_independent():
    delivery = _shift(SRT, -500)
    assert same_physical_cue_grid(SRT, 0, delivery, 500)
    assert not same_physical_cue_grid(SRT, 0, delivery, 400)
    assert not same_physical_cue_grid(SRT, 0, SRT, 500)


def test_range_hearing_on_the_padded_clock_is_valid_for_the_delivery_request(rig):
    audit = _range_observation(rig)
    row = audit["observations"][0]
    delivery = _shift(SRT, -500)
    same = auditor.build_context_adjudication_request(delivery, deepcopy(FINDING), clip_context=rig.context,
                                                      source_media_timeline_offset_ms=500)
    valid, reason = validate_range_acoustic_observation(delivery, same, row)
    assert valid is not None, reason
    assert valid["clock_rebase"]["original_offset_ms"] == 0 and valid["clock_rebase"]["current_offset_ms"] == 500
    other = auditor.build_context_adjudication_request(delivery, deepcopy(FINDING), clip_context=rig.context,
                                                       source_media_timeline_offset_ms=400)
    assert validate_range_acoustic_observation(delivery, other, row)[0] is None  # Different physical target.
    # The retained-replay gate agrees: same physical target, one grid. The
    # earlier narrow hearing keeps its own offset-0 request binding.
    from src.autoslice.independent_context_adjudication import bind_request
    narrow = audit["consumer_check_request"]["prior_acoustic_observations"]
    assert narrow and narrow[0]["witness_request"]["source_media_timeline_offset_ms"] == 0
    same = bind_request(same, prior_acoustic_observations=narrow)
    other = bind_request(other, prior_acoustic_observations=narrow)
    _, report = policy.replay_retained_range_observations(same, audit)
    assert report["status"] == "VERIFIED_RETAINED_OBSERVATION", report.get("reason_code")
    _, rejected = policy.replay_retained_range_observations(other, audit)
    assert rejected["status"] == "REJECTED"


def test_native_row_is_proven_by_its_media_provenance_from_the_same_projection():
    import inspect
    body = inspect.getsource(policy.evidence_projection)
    # The blind row's own media provenance proves the media for every row heard on it.
    assert "proven_media.setdefault(str(row[\"source_media_sha256\"]).removeprefix(\"sha256:\")," in body


def _native_mai_receipt(check, media_sha):
    """A complete candidate-blind MAI exact-crop receipt heard on ``media_sha``."""
    import hashlib
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_final_witness_authority import MODELS, NATIVE_SCHEMA, _sha_json
    request = build_witness_request(check)
    start = request["matched_start_ms"] + request["source_media_timeline_offset_ms"]
    end = request["matched_end_ms"] + request["source_media_timeline_offset_ms"]
    clip = hashlib.sha256(b"synthetic exact crop").hexdigest()
    response = hashlib.sha256(b"synthetic MAI response").hexdigest()
    native = {"schema_version": NATIVE_SCHEMA, "status": "OBSERVED", "candidate_exposure": "none",
              "authority": "EVIDENCE_ONLY", "mutation_authorized": False, "crop_is_exact_target": True,
              "observation_scope": "complete_exact_cue_no_padding", "transcript": "我听到了",
              "provider": "mai", "model": MODELS["mai"], "target_start_ms": start, "target_end_ms": end,
              "source_media_sha256": media_sha, "input_audio_sha256": clip, "response_sha256": response}
    native["receipt_sha256"] = _sha_json(native)
    witness = {"schema_version": "subtitle-span-acoustic-witness.v1",
               "witness_protocol": "candidate_blind_transcript", "status": "OBSERVED",
               "target_audible": True, "audibility_basis": "nonempty_provider_transcript",
               "request_sha256": request["request_sha256"], "candidate_exposure": "none",
               "authority": "EVIDENCE_ONLY", "mutation_authorized": False, "exact_transcript": "我听到了",
               "provider": "mai", "model": MODELS["mai"], "audio_start_ms": start, "audio_end_ms": end,
               "source_media_sha256": media_sha, "audio_clip_sha256": clip, "response_sha256": response,
               "native_observation": native}
    return request, witness


def test_native_row_on_the_candidates_own_producer_media_is_proven_by_its_provenance(rig, tmp_path):
    """41:1631-1634 (guimi b4cd6dc cue46, F5): a MAI row heard on this
    candidate's piece/padded output, with no other row on that media, is
    proven by the candidate's own producer provenance re-opened through the
    ordinary validator. A path whose provenance does not reach the target
    recording still drifts.
    """
    import hashlib
    from src.autoslice.clip_context import build_clip_context
    from test_independent_context_adjudication import _padded_recording_provenance
    recording_sha, *_ = _padded_recording_provenance(tmp_path, rig.source)
    context = build_clip_context(candidate_id=rig.context["candidate_id"],
        spec={"date": "2026-10-19", "pieces": [{"start_ms": 438350, "end_ms": 617760,
              "source_media_sha256": "sha256:" + recording_sha}]}, draft_srt=rig.srt,
        authoritative_chat=(), topic_resolution={}, session_topic_authorities=(),
        speech_memory_ledger_path=tmp_path / "empty-speech-memory.json", source_truth_ledger_path=None)
    check = auditor.build_context_adjudication_request(rig.srt, rig.finding, clip_context=context,
                                                       source_media_timeline_offset_ms=0)
    media_sha = hashlib.sha256(rig.source.read_bytes()).hexdigest()
    assert media_sha != recording_sha
    request, witness = _native_mai_receipt(check, media_sha)

    def projection(entry_extra):
        current = deepcopy(check)
        current["prior_acoustic_observations"] = [{"witness_request": request, "verdict": witness, **entry_extra}]
        return policy.evidence_projection(current)

    unproven = projection({})
    assert [r["reason_code"] for r in unproven["rejected_observations"]] == ["RELISTEN_SOURCE_OR_CLOCK_DRIFT"]
    proven = projection({"media_source_path": str(rig.source)})
    assert proven["rejected_observations"] == []
    assert [r["receipt_kind"] for r in proven["observations"]] == ["native_transcript"]
    stranger = tmp_path / "unrelated.mp4"
    stranger.write_bytes(rig.source.read_bytes())  # Same bytes, but no producer provenance.
    refused = projection({"media_source_path": str(stranger)})
    assert [r["reason_code"] for r in refused["rejected_observations"]] == ["RELISTEN_SOURCE_OR_CLOCK_DRIFT"]
