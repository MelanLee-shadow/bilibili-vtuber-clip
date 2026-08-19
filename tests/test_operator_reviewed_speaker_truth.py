import copy
import json
from pathlib import Path

import pytest

from scripts.apply_speaker_turn_overrides import Cue, sha256_file, write_srt
from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.operator_reviewed_speaker_truth import (
    current_source_recording_binding,
)
from src.autoslice.producer_speaker import (
    SpeakerFinalizationAdapters,
    run_producer_speaker_finalization,
)
from src.autoslice.reviewed_speaker_baseline import load_reviewed_speaker_baseline
from src.autoslice.speaker_common import HOST_SPEAKER, SpeakerFinalizationError


CANDIDATE = "synthetic_speaker_only_truth"
AUTHORITY = "维护者 exact speaker-only review"


def _cues() -> list[TextCue]:
    return [
        TextCue(1, "00:00:00,000", "00:00:01,500", "第一句"),
        TextCue(2, "00:00:01,500", "00:00:03,200", "第二句"),
    ]


def _override_row(cue: TextCue) -> dict[str, object]:
    return {
        "source_cue": cue.source_index,
        "expect": {"start": cue.start, "end": cue.end, "text": cue.text},
        "authority": AUTHORITY,
        "note": "维护者 reviewed speaker truth; exact full-cue interval.",
        "segments": [
            {
                "speaker": HOST_SPEAKER,
                "text": cue.text,
                "start": cue.start,
                "end": cue.end,
            }
        ],
    }


def _source_recording() -> dict[str, object]:
    return {
        "basename": "source.mp4",
        "sha256": "e" * 64,
        "absolute_start_ms": 1000,
        "absolute_end_ms": 4200,
    }


def _fixture(root: Path) -> tuple[dict[str, object], Path, dict[str, object]]:
    cues = _cues()
    automatic_path = root / "assets" / "automatic-labelled.srt"
    automatic_path.parent.mkdir(parents=True)
    write_srt(
        [
            Cue(
                source_index=cue.source_index,
                start=cue.start,
                end=cue.end,
                speaker=HOST_SPEAKER,
                text=cue.text,
                decision_source="speaker_guess_uniform_host_last_resort",
            )
            for cue in cues
        ],
        automatic_path,
    )
    automatic_sha = sha256_file(automatic_path)
    review_binding_path = root / "assets" / "reviewed-delivery-binding.json"
    review_binding_path.write_text(
        json.dumps(
            {
                "schema_version": "operator-reviewed-speaker-delivery-binding.v1",
                "candidate_id": CANDIDATE,
                "authority": AUTHORITY,
                "reviewed_at": "2026-08-13",
                "current_record_sha256": "a" * 64,
                "reviewed_delivery_video": {
                    "basename": "reviewed-delivery.mp4",
                    "sha256": "d" * 64,
                },
                "source_media_sha256": "b" * 64,
                "text_final_srt_sha256": "c" * 64,
                "automatic_labelled_srt_sha256": automatic_sha,
                "source_recording": _source_recording(),
                "subtitle_text_authorized": False,
                "upload_authorized": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": CANDIDATE,
        "source_media_sha256": "b" * 64,
        "text_final_srt_sha256": "c" * 64,
        "source_srt_sha256": automatic_sha,
        "operator_review_binding": {
            "path": str(review_binding_path.relative_to(root)),
            "sha256": sha256_file(review_binding_path),
        },
        "reviewed_speaker_baseline": {
            "schema_version": "reviewed-speaker-baseline.v2",
            "authority": AUTHORITY,
            "truth_input": {},
            "automatic_input": {
                "path": str(automatic_path.relative_to(root)),
                "sha256": automatic_sha,
            },
            "cue_count": len(cues),
            "anchor_source_cues": [1, 2],
            "machine_cues": [],
        },
        "overrides": [_override_row(cue) for cue in cues],
    }
    truth: dict[str, object] = {
        "schema_version": "operator-reviewed-speaker-truth.v1",
        "candidate_id": CANDIDATE,
        "scope": "speaker_only",
        "authority": AUTHORITY,
        "reviewed_at": "2026-08-13",
        "decision": "ALL_DELIVERY_CUES_HOST",
        "subtitle_text_authorized": False,
        "upload_authorized": False,
        "reviewed_delivery_video_sha256": "d" * 64,
        "bindings": {
            "source_media_sha256": document["source_media_sha256"],
            "text_final_srt_sha256": document["text_final_srt_sha256"],
            "automatic_labelled_srt_sha256": automatic_sha,
            "cue_count": len(cues),
            "source_recording": _source_recording(),
        },
        "cues": [
            {
                "cue": cue.source_index,
                "start": cue.start,
                "end": cue.end,
                "text": cue.text,
                "speaker": HOST_SPEAKER,
            }
            for cue in cues
        ],
    }
    truth_path = root / "assets" / "speaker-only-truth.json"
    truth_path.write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    baseline = document["reviewed_speaker_baseline"]
    assert isinstance(baseline, dict)
    baseline["truth_input"] = {
        "path": str(truth_path.relative_to(root)),
        "sha256": sha256_file(truth_path),
    }
    return document, truth_path, truth


def _rewrite_truth(document: dict[str, object], truth_path: Path, truth: dict[str, object]) -> None:
    truth_path.write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    baseline = document["reviewed_speaker_baseline"]
    assert isinstance(baseline, dict)
    truth_input = baseline["truth_input"]
    assert isinstance(truth_input, dict)
    truth_input["sha256"] = sha256_file(truth_path)


def test_speaker_only_truth_binds_complete_current_grid_without_upload_authority(
    tmp_path: Path,
) -> None:
    document, _truth_path, truth = _fixture(tmp_path)

    loaded = load_reviewed_speaker_baseline(
        document,
        candidate_id=CANDIDATE,
        cues=_cues(),
        repo_root=tmp_path,
        expected_source_recording=_source_recording(),
    )

    assert loaded is not None
    assert loaded.anchor_labels == {0: HOST_SPEAKER, 1: HOST_SPEAKER}
    assert loaded.machine_cues == ()
    assert loaded.evidence["reviewed_cue_count"] == 2
    assert truth["scope"] == "speaker_only"
    assert truth["subtitle_text_authorized"] is False
    assert truth["upload_authorized"] is False


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda truth: truth.update(scope="subtitle_and_speaker"), "scope is invalid"),
        (
            lambda truth: truth.update(subtitle_text_authorized=True),
            "must not authorize subtitle text",
        ),
        (lambda truth: truth.update(upload_authorized=True), "must not authorize upload"),
        (
            lambda truth: truth["bindings"].update(text_final_srt_sha256="f" * 64),
            "text_final_srt_sha256 drift",
        ),
        (
            lambda truth: truth.update(reviewed_delivery_video_sha256="f" * 64),
            "reviewed delivery hash drift",
        ),
        (
            lambda truth: truth["bindings"]["source_recording"].update(basename="other.mp4"),
            "source recording binding drift",
        ),
        (
            lambda truth: truth["bindings"]["source_recording"].update(sha256="f" * 64),
            "source recording binding drift",
        ),
        (
            lambda truth: truth["bindings"]["source_recording"].update(absolute_start_ms=1001),
            "source recording binding drift",
        ),
        (
            lambda truth: truth["bindings"]["source_recording"].update(absolute_end_ms=4199),
            "source recording binding drift",
        ),
        (
            lambda truth: truth["cues"][0].update(text="漂移"),
            "cue 1 text drift",
        ),
    ],
)
def test_speaker_only_truth_rejects_scope_and_current_byte_drift(
    tmp_path: Path, mutation, error: str
) -> None:
    document, truth_path, truth = _fixture(tmp_path)
    mutated = copy.deepcopy(truth)
    mutation(mutated)
    _rewrite_truth(document, truth_path, mutated)

    with pytest.raises(SpeakerFinalizationError, match=error):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=_cues(),
            repo_root=tmp_path,
            expected_source_recording=_source_recording(),
        )


def test_speaker_only_truth_fails_closed_without_current_source_binding(
    tmp_path: Path,
) -> None:
    document, _truth_path, _truth = _fixture(tmp_path)

    with pytest.raises(SpeakerFinalizationError, match="current source.*unavailable"):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=_cues(),
            repo_root=tmp_path,
        )


def test_producer_projects_exact_current_source_binding_without_source_read() -> None:
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/source.mp4",
                "source_media_sha256": "sha256:" + "e" * 64,
                "start_ms": 500,
                "end_ms": 5000,
            }
        ]
    }

    assert (
        current_source_recording_binding(
            spec=spec,
            absolute_start_ms=1000,
            absolute_end_ms=4200,
        )
        == _source_recording()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec.update(pieces=[]),
        lambda spec: spec["pieces"][0].update(source_media_sha256="f" * 63),
        lambda spec: spec["pieces"][0].update(remote_media=""),
        lambda spec: spec["pieces"][0].update(end_ms=4000),
    ],
)
def test_producer_withholds_unavailable_or_ambiguous_current_source_binding(
    mutate,
) -> None:
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/source.mp4",
                "source_media_sha256": "sha256:" + "e" * 64,
                "start_ms": 500,
                "end_ms": 5000,
            }
        ]
    }
    mutation_target = copy.deepcopy(spec)
    mutate(mutation_target)

    assert (
        current_source_recording_binding(
            spec=mutation_target,
            absolute_start_ms=1000,
            absolute_end_ms=4200,
        )
        is None
    )


def test_required_binary_finalizer_receives_current_source_binding(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def binary(**kwargs):
        captured.update(kwargs)
        return {"status": "READY", "production_ready": True}

    run_producer_speaker_finalization(
        speaker_mode="required",
        host="localhost",
        candidate_id=CANDIDATE,
        media_path=tmp_path / "recut.mp4",
        text_srt_path=tmp_path / "text.srt",
        output_srt_path=tmp_path / "speaker.srt",
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "speaker-work",
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/source.mp4",
                    "source_media_sha256": "sha256:" + "e" * 64,
                    "start_ms": 500,
                    "end_ms": 5000,
                }
            ]
        },
        spec_parent=tmp_path,
        override_path=tmp_path / "override.json",
        source_session_anchor_path=None,
        mixed_overlap_evidence_path=None,
        speaker_python=tmp_path / "python",
        final_source_start_ms=1000,
        final_source_end_ms=4200,
        adapters=SpeakerFinalizationAdapters(run_binary_finalizer=binary),
    )

    assert captured["expected_source_recording"] == _source_recording()
