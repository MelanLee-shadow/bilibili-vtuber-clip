"""Admission replay sees exactly what the admission saw (xiamen c73e25d8).

Rule: 41「共同核当前请求、物理源、源/交付时钟和原始音频/答复字节」— a range
hearing's binding is replayed against the chronology of its own admission. A
native row on padded media, proven through an earlier range's media binding,
was part of that admission's projection although ``evidence_projection`` lists
it only after every range. Replay must count it; a fabricated prior id, or a
native proven only by the range being judged, still drifts. Protocol stubs; no
provider, no media.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice import targeted_relisten as policy


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


RECORDING, PADDED = sha("recording"), sha("padded")
SRT = ("1\n00:00:00,100 --> 00:00:00,900\n前文\n\n"
       "2\n00:00:01,000 --> 00:00:02,000\n疑点\n\n"
       "3\n00:00:02,100 --> 00:00:02,900\n后文\n\n")
CHECK = {"matched_start_ms": 1_000, "matched_end_ms": 2_000,
         "source_media_timeline_offset_ms": 0,
         "clip_context_binding": {"candidate_id": "semantictalk_x", "source_media_sha256s": [RECORDING]},
         "whole_clip_current_srt": SRT, "current_cue": "疑点", "cue_indexes": [2]}
NATIVE = {"kind": "native_transcript", "observation_id": "catalogue-native",
          "physical_audio_window_ms": [800, 2_200], "source_media_sha256": PADDED,
          "audio_clip_sha256": sha("native clip"), "response_sha256": sha("native raw"),
          "raw_observation": {"status": "OBSERVED"}, "witness_request": {}}
FIRST_PLAN = {"decision": "OBSERVE", "purpose": "FULL_QA", "reason": "首次只听疑点句",
              "first_cue": 2, "last_cue": 2}
PLAN = {"decision": "OBSERVE", "purpose": "FULL_QA", "reason": "听前后文确认疑点",
        "first_cue": 1, "last_cue": 3}


def range_observation(label: str, window, cue_indexes):
    return {"status": "OBSERVED", "evidence_scope": "RANGE_CONTEXT_ONLY",
            "observation_class": "blind_range",
            "artifact_bindings": {"source_media_sha256": "sha256:" + PADDED,
                                  "audio_clip_sha256": "sha256:" + sha(label + " clip"),
                                  "raw_response_sha256": "sha256:" + sha(label + " raw")},
            "media_source_binding": {"role": "PRODUCER_PADDED_MEDIA_FROM_RECORDING",
                                     "media_sha256": PADDED, "recording_sha256": RECORDING},
            "range": {"cue_indexes": list(cue_indexes)},
            "source_window": {"crop_start_ms": window[0], "crop_end_ms": window[1]},
            "verdict": {}}


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setattr(policy, "observation_catalogue", lambda _check, _witness: ([deepcopy(NATIVE)], []))
    monkeypatch.setattr(policy, "validate_range_acoustic_observation",
                        lambda _srt, _check, row: (deepcopy(dict(row)), None))


def admitted_range(projection, *, label="second", window=(600, 2_400), cues=(1, 2, 3), plan=PLAN):
    admission = policy.admit_plan(plan, CHECK, projection, source_window=list(window),
                                  cue_indexes=list(cues))
    return policy.bind_observed_input(range_observation(label, window, cues),
                                      admission=admission, plan=plan, prompt="plan prompt",
                                      raw=json.dumps(plan, ensure_ascii=False))


def first_range():
    """The first hearing is admitted before the native row is provable."""

    empty = policy.evidence_projection(CHECK, witness={}, ranges=[])
    assert empty["observations"] == []
    return admitted_range(empty, label="first", window=(700, 2_300), cues=(2,), plan=FIRST_PLAN)


def test_native_proven_by_an_earlier_range_is_replayed_for_the_later_admission(stubs):
    first = first_range()
    before = policy.evidence_projection(CHECK, witness={}, ranges=[first])
    assert [row["receipt_kind"] for row in before["observations"]] == ["blind_range", "native_transcript"]
    assert before["effective_distinct_observations"] == 2
    second = admitted_range(before)
    assert len(second["targeted_relisten_input"]["admission"]["prior_observation_ids"]) == 2

    after = policy.evidence_projection(CHECK, witness={}, ranges=[first, second])

    assert after["rejected_observations"] == []
    assert after["effective_distinct_observations"] == 3
    # The native row keeps its listed place after every range.
    assert [row["receipt_kind"] for row in after["observations"]] == [
        "blind_range", "blind_range", "native_transcript"]
    # Replaying the same history twice is stable (no duplicate promotion).
    again = policy.evidence_projection(CHECK, witness={}, ranges=[first, second])
    assert [row["observation_id"] for row in again["observations"]] == [
        row["observation_id"] for row in after["observations"]]


def test_fabricated_prior_observation_still_drifts(stubs):
    first = first_range()
    before = policy.evidence_projection(CHECK, witness={}, ranges=[first])
    forged = deepcopy(before)
    ghost = deepcopy(forged["observations"][0])
    ghost["observation_id"] = sha("never observed")
    forged["observations"].append(ghost)
    second = admitted_range(forged)

    after = policy.evidence_projection(CHECK, witness={}, ranges=[first, second])

    assert [row["reason_code"] for row in after["rejected_observations"]] == ["RELISTEN_ADMISSION_REPLAY_DRIFT"]
    assert after["effective_distinct_observations"] == 2


def test_native_proven_only_by_the_judged_range_cannot_enter_its_own_admission(stubs):
    first = first_range()
    with_native = policy.evidence_projection(CHECK, witness={}, ranges=[first])
    # Claim an admission that saw the native row but not the range proving it.
    claimed = deepcopy(with_native)
    claimed["observations"] = [row for row in claimed["observations"]
                               if row["receipt_kind"] == "native_transcript"]
    claimed["effective_distinct_observations"] = 1
    second = admitted_range(claimed)

    after = policy.evidence_projection(CHECK, witness={}, ranges=[second])

    assert [row["reason_code"] for row in after["rejected_observations"]] == ["RELISTEN_ADMISSION_REPLAY_DRIFT"]
