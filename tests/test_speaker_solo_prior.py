"""Synthetic F14 portrait prior, conservative vetoes, and finalizer seam."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.autoslice.segment_scene_context import resolve_segment_scene_context
from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SpeakerFinalizationError,
    SpeakerIdentityIndeterminate,
)
from src.autoslice.speaker_finalizer import finalize_speaker_subtitles
from src.autoslice.producer_speaker import run_speaker_finalizer
from src.autoslice.speaker_solo_prior import (
    apply_portrait_solo_prior,
    build_speaker_session_context,
    write_speaker_session_context,
)


CANDIDATE_ID = "auto_230125_1157_1229"


def _scene(
    tmp_path: Path, *, orientation: str
) -> tuple[Path, dict[str, object]]:
    segment = tmp_path / "22966160_20260808-23-01-25.mp4"
    segment.write_bytes(b"synthetic source segment")
    segment.with_suffix(".meta.json").write_text(
        json.dumps({"recorder": {"title": "深夜杂谈"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    dimensions = {
        "portrait": (1080, 1920),
        "landscape": (1920, 1080),
        "unknown": (None, None),
    }[orientation]

    def probe(_segment: Path) -> dict[str, object]:
        width, height = dimensions
        if width is None:
            return {
                "status": "UNKNOWN",
                "width": None,
                "height": None,
                "orientation": "unknown",
                "reason_code": "SYNTHETIC_UNAVAILABLE",
            }
        return {"status": "PASS", "width": width, "height": height}

    return segment, resolve_segment_scene_context(segment, dimension_probe=probe)


def _context(
    tmp_path: Path,
    *,
    orientation: str = "portrait",
    relation: object = None,
    source_piece_count: int = 1,
    candidate_id: str = CANDIDATE_ID,
) -> tuple[dict[str, object], Path]:
    segment, scene = _scene(tmp_path, orientation=orientation)
    document = build_speaker_session_context(
        candidate_id=candidate_id,
        session_id="live-20260808Tunknown",
        segment_path=segment,
        start_ms=1_157_000,
        end_ms=1_229_000,
        segment_scene_context=scene,
        session_relation_authority=relation,
        source_piece_count=source_piece_count,
    )
    assert document is not None
    path = tmp_path / f"{orientation}-{source_piece_count}.speaker-context.json"
    write_speaker_session_context(path, document)
    return document, path


def _singleton_review(*, guest_confidence: float | None = None) -> dict[str, object]:
    evidence: dict[str, object] = {
        "source_cue": 1,
        "zero_based_index": 0,
        "start": "00:00:00,000",
        "end": "00:00:02,030",
        "text": "我这，哈哈",
        "seed_score": 0.33793,
        "host_bank_score": 0.28934,
        "audio_sha256": "a" * 64,
        "neighbours": [],
    }
    if guest_confidence is not None:
        evidence["context_decision"] = {
            "speaker": GUEST_SPEAKER,
            "confidence": guest_confidence,
        }
    return {
        "mode": "singleton_outlier",
        "review_required": True,
        "review_reason_codes": ["CONTEXT_REVIEW"],
        "context_unresolved_cues": [1],
        "singleton_evidence": [evidence],
        "decisions": [
            {
                "source_index": 1,
                "speaker": GUEST_SPEAKER,
                "decision_source": "speaker_review_required_singleton",
                "margin": None,
            }
        ],
    }


def _finalizer_fixture(tmp_path: Path) -> dict[str, Path]:
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"synthetic final media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,030\n我这，哈哈\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    references = tmp_path / "refs"
    model = tmp_path / "model"
    references.mkdir()
    model.mkdir()
    return {
        "media": media,
        "text_srt": text_srt,
        "profile": profile,
        "references": references,
        "model": model,
    }


def test_f14_canary_portrait_identity_review_delivers_all_host(
    tmp_path: Path,
) -> None:
    """Negative canary: disabling the prior restores SPEAKER_REVIEW_REQUIRED."""

    inputs = _finalizer_fixture(tmp_path)
    _document, context_path = _context(tmp_path)
    output_srt = tmp_path / "speaker-final.srt"
    output_ass = tmp_path / "speaker-final.ass"
    output_manifest = tmp_path / "speaker-final.json"

    manifest = finalize_speaker_subtitles(
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        work_dir=tmp_path / "speaker-work",
        candidate_id=CANDIDATE_ID,
        speaker_session_context_path=context_path,
        analyzer=lambda **_kwargs: _singleton_review(),
    )

    assert manifest["status"] == "READY"
    assert manifest["solo_prior"] == "portrait"
    assert manifest["solo_prior_receipt"]["source"] == (
        "source_segment_ffprobe_orientation"
    )
    assert manifest["analysis"]["solo_prior"] == "portrait"
    assert manifest["analysis"]["solo_prior_receipt"]["decision"] == "APPLIED"
    assert manifest["final_decisions"][0]["speaker"] == HOST_SPEAKER
    assert manifest["final_decisions"][0]["decision_source"] == "portrait_solo_prior"
    assert output_srt.is_file() and output_ass.is_file()


def test_portrait_rescues_only_typed_identity_indeterminate_failure(
    tmp_path: Path,
) -> None:
    inputs = _finalizer_fixture(tmp_path)
    _document, context_path = _context(tmp_path)

    def identity_failure(**_kwargs):
        raise SpeakerIdentityIndeterminate("CAM++ margins have no separation")

    manifest = finalize_speaker_subtitles(
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        output_srt_path=tmp_path / "speaker-final.srt",
        output_ass_path=tmp_path / "speaker-final.ass",
        output_manifest_path=tmp_path / "speaker-final.json",
        work_dir=tmp_path / "speaker-work",
        candidate_id=CANDIDATE_ID,
        speaker_session_context_path=context_path,
        analyzer=identity_failure,
    )

    assert manifest["status"] == "READY"
    assert manifest["analysis"]["mode_before_solo_prior"] == (
        "speaker_identity_indeterminate"
    )
    assert manifest["analysis"]["identity_indeterminate_reason"] == (
        "CAM++ margins have no separation"
    )
    assert manifest["final_decisions"][0]["speaker"] == HOST_SPEAKER


@pytest.mark.parametrize(
    ("orientation", "error_type"),
    [
        ("landscape", SpeakerIdentityIndeterminate),
        ("portrait", SpeakerFinalizationError),
    ],
)
def test_landscape_or_nonidentity_failure_is_never_rescued(
    tmp_path: Path, orientation: str, error_type: type[Exception]
) -> None:
    inputs = _finalizer_fixture(tmp_path)
    candidate_id = (
        "auto_200130_landscape_canary"
        if orientation == "landscape"
        else "auto_orientation_unknown_canary"
    )
    _document, context_path = _context(
        tmp_path, orientation=orientation, candidate_id=candidate_id
    )

    def failure(**_kwargs):
        raise error_type("synthetic runtime failure")

    with pytest.raises(error_type, match="synthetic runtime failure"):
        finalize_speaker_subtitles(
            media_path=inputs["media"],
            text_srt_path=inputs["text_srt"],
            profile_path=inputs["profile"],
            reference_dir=inputs["references"],
            model_dir=inputs["model"],
            output_srt_path=tmp_path / "speaker-final.srt",
            output_ass_path=tmp_path / "speaker-final.ass",
            output_manifest_path=tmp_path / "speaker-final.json",
            work_dir=tmp_path / "speaker-work",
            candidate_id=candidate_id,
            speaker_session_context_path=context_path,
            analyzer=failure,
        )


def test_hash_bound_human_speaker_override_precedes_portrait_prior(
    tmp_path: Path,
) -> None:
    inputs = _finalizer_fixture(tmp_path)
    _document, context_path = _context(tmp_path)
    automatic_guest = (
        "1\n00:00:00,000 --> 00:00:02,030\n[连线] 我这，哈哈\n".encode()
    )
    override_path = tmp_path / "speaker-overrides.json"
    override_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": CANDIDATE_ID,
                "source_media_sha256": hashlib.sha256(
                    inputs["media"].read_bytes()
                ).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(
                    inputs["text_srt"].read_bytes()
                ).hexdigest(),
                "source_srt_sha256": hashlib.sha256(automatic_guest).hexdigest(),
                "overrides": [
                    {
                        "source_cue": 1,
                        "expect": {
                            "start": "00:00:00,000",
                            "end": "00:00:02,030",
                            "text": "我这，哈哈",
                        },
                        "authority": "synthetic human speaker review",
                        "segments": [
                            {
                                "start": "00:00:00,000",
                                "end": "00:00:02,030",
                                "speaker": GUEST_SPEAKER,
                                "speaker_detail": "synthetic reviewed guest",
                                "text": "我这，哈哈",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = finalize_speaker_subtitles(
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        output_srt_path=tmp_path / "speaker-final.srt",
        output_ass_path=tmp_path / "speaker-final.ass",
        output_manifest_path=tmp_path / "speaker-final.json",
        work_dir=tmp_path / "speaker-work",
        candidate_id=CANDIDATE_ID,
        override_path=override_path,
        speaker_session_context_path=context_path,
        analyzer=lambda **_kwargs: _singleton_review(),
    )

    assert manifest["status"] == "READY"
    assert "solo_prior" not in manifest["analysis"]
    assert manifest["final_decisions"][0]["speaker"] == GUEST_SPEAKER


@pytest.mark.parametrize("orientation", ["landscape", "unknown"])
def test_landscape_or_unknown_never_activates_portrait_prior(
    tmp_path: Path, orientation: str
) -> None:
    """Locks the auto_200130_* horizontal/unknown unresolved path unchanged."""

    inputs = _finalizer_fixture(tmp_path)
    candidate_id = (
        "auto_200130_landscape_canary"
        if orientation == "landscape"
        else "auto_orientation_unknown_canary"
    )
    _document, context_path = _context(
        tmp_path, orientation=orientation, candidate_id=candidate_id
    )
    manifest = finalize_speaker_subtitles(
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        output_srt_path=tmp_path / "speaker-final.srt",
        output_ass_path=tmp_path / "speaker-final.ass",
        output_manifest_path=tmp_path / "speaker-final.json",
        work_dir=tmp_path / "speaker-work",
        candidate_id=candidate_id,
        speaker_session_context_path=context_path,
        analyzer=lambda **_kwargs: _singleton_review(),
    )

    assert manifest["status"] == "SPEAKER_REVIEW_REQUIRED"
    assert manifest["context_unresolved_cues"] == [1]
    assert "solo_prior" not in manifest["analysis"]


def test_significant_campp_two_cluster_is_the_one_percent_veto(
    tmp_path: Path,
) -> None:
    context, _path = _context(tmp_path)
    analysis = {
        "mode": "multi_speaker",
        "review_reason_codes": ["CONTEXT_INCOMPLETE"],
        "context_unresolved_cues": [2],
        "policy": {"ambiguity_band": 0.10},
        "threshold": 0.0,
        "cluster_centers": {"low": -0.20, "high": 0.20},
        "guest_anchor_groups": [[0, 1]],
        "decisions": [
            {"speaker": GUEST_SPEAKER, "margin": -0.20},
            {"speaker": GUEST_SPEAKER, "margin": -0.15},
            {"speaker": HOST_SPEAKER, "margin": 0.15},
            {"speaker": HOST_SPEAKER, "margin": 0.20},
        ],
    }

    result = apply_portrait_solo_prior(
        analysis,
        context=context,
        session_context_sha256="sha256:" + "b" * 64,
    )

    assert result.get("review_required") is not True
    assert result["context_unresolved_cues"] == [2]
    assert result["decisions"] == analysis["decisions"]
    receipt = result["solo_prior_receipt"]
    assert receipt["decision"] == "VETOED"
    acoustic = receipt["counterevidence"]["campp"]
    assert acoustic["required_center_gap"] == pytest.approx(0.20)
    assert acoustic["hard_host_cue_count"] == 2
    assert acoustic["hard_guest_cue_count"] == 2


def test_campp_below_existing_hard_margin_does_not_invent_a_veto(
    tmp_path: Path,
) -> None:
    context, _path = _context(tmp_path)
    analysis = {
        "mode": "multi_speaker",
        "review_reason_codes": ["CONTEXT_INCOMPLETE"],
        "context_unresolved_cues": [2],
        "policy": {"ambiguity_band": 0.10},
        "threshold": 0.0,
        "cluster_centers": {"low": -0.095, "high": 0.095},
        "guest_anchor_groups": [[0, 1]],
        "decisions": [
            {"speaker": GUEST_SPEAKER, "margin": -0.15},
            {"speaker": GUEST_SPEAKER, "margin": -0.12},
            {"speaker": HOST_SPEAKER, "margin": 0.12},
            {"speaker": HOST_SPEAKER, "margin": 0.15},
        ],
    }

    result = apply_portrait_solo_prior(
        analysis,
        context=context,
        session_context_sha256="sha256:" + "c" * 64,
    )

    assert result["solo_prior"] == "portrait"
    assert result["review_required"] is False
    assert {row["speaker"] for row in result["decisions"]} == {HOST_SPEAKER}


def test_confirmed_roster_relation_presence_vetoes_portrait_solo(
    tmp_path: Path,
) -> None:
    relation = {
        "schema_version": "session-relation-authority.v1",
        "relation_id": "synthetic-call",
        "state": "CONFIRMED",
        "participants": [{"name": "主播"}, {"name": "连线嘉宾"}],
        "presence_intervals": [{"start_ms": 1_100_000, "end_ms": 1_250_000}],
        "bound_recording_basename": "22966160_20260808-23-01-25.mp4",
        "ledger_sha256": "sha256:" + "f" * 64,
    }
    context, _path = _context(tmp_path, relation=relation)

    result = apply_portrait_solo_prior(
        _singleton_review(),
        context=context,
        session_context_sha256="sha256:" + "d" * 64,
    )

    receipt = result["solo_prior_receipt"]
    assert receipt["decision"] == "VETOED"
    assert receipt["counterevidence"]["session_relation"]["reason_code"] == (
        "CONFIRMED_RELATION_PRESENCE_OVERLAP"
    )
    assert result["context_unresolved_cues"] == [1]


def test_occurrence_neutral_roster_without_governed_binding_does_not_veto(
    tmp_path: Path,
) -> None:
    context, _path = _context(
        tmp_path,
        relation={
            "schema_version": "session-relation-authority.v1",
            "relation_id": "unbound-roster",
            "state": "CONFIRMED",
            "participants": [{"name": "主播"}, {"name": "名单嘉宾"}],
        },
    )

    result = apply_portrait_solo_prior(
        _singleton_review(),
        context=context,
        session_context_sha256="sha256:" + "0" * 64,
    )

    assert result["solo_prior"] == "portrait"
    assert result["review_required"] is False


def test_high_confidence_whole_clip_guest_context_vetoes_portrait_solo(
    tmp_path: Path,
) -> None:
    context, _path = _context(tmp_path)

    result = apply_portrait_solo_prior(
        _singleton_review(guest_confidence=0.99),
        context=context,
        session_context_sha256="sha256:" + "e" * 64,
    )

    receipt = result["solo_prior_receipt"]
    assert receipt["decision"] == "VETOED"
    assert receipt["counterevidence"]["whole_clip_context"]["reason_code"] == (
        "HIGH_CONFIDENCE_WHOLE_CLIP_GUEST_CONTEXT"
    )
    assert result["review_required"] is True


def test_stale_segment_stat_receipt_cannot_build_portrait_context(
    tmp_path: Path,
) -> None:
    segment, scene = _scene(tmp_path, orientation="portrait")
    segment.write_bytes(b"source changed after orientation probe")

    assert build_speaker_session_context(
        candidate_id=CANDIDATE_ID,
        session_id="live-20260808Tunknown",
        segment_path=segment,
        start_ms=1_157_000,
        end_ms=1_229_000,
        segment_scene_context=scene,
        session_relation_authority=None,
        source_piece_count=1,
    ) is None


def test_binary_wrapper_transports_and_verifies_portrait_context_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _finalizer_fixture(tmp_path)
    _document, context_path = _context(tmp_path)
    output_srt = tmp_path / "wrapped-speaker.srt"
    output_ass = tmp_path / "wrapped-speaker.ass"
    output_manifest = tmp_path / "wrapped-speaker.json"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        commands.append(command)
        output_srt.write_text("speaker output", encoding="utf-8")
        output_ass.write_text("speaker ass", encoding="utf-8")
        context_sha256 = hashlib.sha256(context_path.read_bytes()).hexdigest()
        output_manifest.write_text(
            json.dumps(
                {
                    "status": "READY",
                    "production_ready": True,
                    "source_media_sha256": hashlib.sha256(
                        inputs["media"].read_bytes()
                    ).hexdigest(),
                    "text_final_srt_sha256": hashlib.sha256(
                        inputs["text_srt"].read_bytes()
                    ).hexdigest(),
                    "profile_sha256": hashlib.sha256(
                        inputs["profile"].read_bytes()
                    ).hexdigest(),
                    "speaker_override_sha256": None,
                    "source_session_anchor_manifest_sha256": None,
                    "mixed_overlap_evidence_sha256": None,
                    "output_review_srt_sha256": hashlib.sha256(
                        output_srt.read_bytes()
                    ).hexdigest(),
                    "output_ass_sha256": hashlib.sha256(
                        output_ass.read_bytes()
                    ).hexdigest(),
                    "analysis": {
                        "solo_prior": "portrait",
                        "solo_prior_receipt": {
                            "speaker_session_context_sha256": (
                                "sha256:" + context_sha256
                            )
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("src.autoslice.producer_speaker.subprocess.run", fake_run)

    manifest = run_speaker_finalizer(
        host="localhost",
        candidate_id=CANDIDATE_ID,
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        work_dir=tmp_path / "wrapper-work",
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        speaker_session_context_path=context_path,
        speaker_python=tmp_path / "python",
    )

    assert manifest["analysis"]["solo_prior"] == "portrait"
    argument_index = commands[0].index("--speaker-session-context")
    assert commands[0][argument_index + 1] == str(context_path)
