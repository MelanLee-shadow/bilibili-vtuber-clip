import hashlib
import json
from pathlib import Path

from scripts.apply_speaker_turn_overrides import (
    apply_overrides,
    parse_labelled_srt,
    validate_bound_speaker_override_document,
)
from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.reviewed_speaker_baseline import load_reviewed_speaker_baseline
from src.autoslice.speaker_common import HOST_SPEAKER


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = "auto_221234_1349_1418"
AUTHORITY = "89221说话人这个是李豆沙，没问题。"
TRUTH_PATH = ROOT / "assets/lidousha/reviewed_speaker_truth" / f"{CANDIDATE}.speaker-only.v1.json"
REVIEW_BINDING_PATH = (
    ROOT
    / "assets/lidousha/reviewed_speaker_delivery_bindings"
    / f"{CANDIDATE}.reviewed-delivery.v1.json"
)
AUTOMATIC_PATH = (
    ROOT / "assets/lidousha/speaker_automatic_baselines" / f"{CANDIDATE}.automatic-labelled.srt"
)
OVERRIDE_PATH = ROOT / "assets/lidousha/speaker_overrides" / f"{CANDIDATE}.speaker.v1.json"
EXPECTED_MEDIA_SHA = "9e9032a3203025555ae4faf3cb6c35b1816984ede97287dc014af63e389e9bda"
EXPECTED_TEXT_SHA = "ca5e906e4a3b07ea93b68cfca380c1d56fdea6dd2e4663dcd8a0eeabcf0045b7"
EXPECTED_AUTOMATIC_SHA = "cb528b1e45272fb532f604a4cf7ef8c9c9b64faf7cf5e87aa7cfa27dffed4a72"
EXPECTED_REVIEWED_VIDEO_SHA = "9caf3b70c4329c19902221c719a26af5a05397f367cd4ae7c615e4c622644add"
EXPECTED_SOURCE_RECORDING_SHA = "adb42519050b15438dce5b244e2bbccfa47e0cc80024feedd1b59ab1aafbdc56"
EXPECTED_REVIEW_BINDING_SHA = "aa4ad4e56926699cc5a2394170677a3dc78e3d67484bcde6e1c55418e13c735d"
EXPECTED_CURRENT_RECORD_SHA = "7635ee9ff529a8ce9428950a51b75ee22f6e3b23a72e8f044ebd7981461e16a8"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truth() -> dict[str, object]:
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8"))


def _override() -> dict[str, object]:
    return json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))


def _source_recording() -> dict[str, object]:
    return {
        "basename": "22966160_20260809-22-12-34.mp4",
        "sha256": EXPECTED_SOURCE_RECORDING_SHA,
        "absolute_start_ms": 1348880,
        "absolute_end_ms": 1418660,
    }


def _current_cues() -> list[TextCue]:
    return [
        TextCue(
            source_index=row["cue"],
            start=row["start"],
            end=row["end"],
            text=row["text"],
        )
        for row in _truth()["cues"]
    ]


def test_221_truth_is_speaker_only_and_cannot_authorize_text_or_upload() -> None:
    truth = _truth()

    assert truth["schema_version"] == "operator-reviewed-speaker-truth.v1"
    assert truth["candidate_id"] == CANDIDATE
    assert truth["scope"] == "speaker_only"
    assert truth["authority"] == AUTHORITY
    assert truth["decision"] == "ALL_DELIVERY_CUES_HOST"
    assert truth["subtitle_text_authorized"] is False
    assert truth["upload_authorized"] is False
    assert truth["reviewed_delivery_video_sha256"] == EXPECTED_REVIEWED_VIDEO_SHA


def test_221_truth_binds_current_media_text_automatic_and_source_interval() -> None:
    truth = _truth()
    bindings = truth["bindings"]
    source = bindings["source_recording"]

    assert bindings["source_media_sha256"] == EXPECTED_MEDIA_SHA
    assert bindings["text_final_srt_sha256"] == EXPECTED_TEXT_SHA
    assert bindings["automatic_labelled_srt_sha256"] == EXPECTED_AUTOMATIC_SHA
    assert bindings["cue_count"] == 22
    assert source == _source_recording()
    clean_srt = (
        "\n\n".join(
            f"{row['cue']}\n{row['start']} --> {row['end']}\n{row['text']}" for row in truth["cues"]
        )
        + "\n"
    )
    assert hashlib.sha256(clean_srt.encode("utf-8")).hexdigest() == EXPECTED_TEXT_SHA


def test_221_frozen_automatic_grid_is_canonical_and_all_host() -> None:
    automatic = parse_labelled_srt(AUTOMATIC_PATH)

    assert _sha256(AUTOMATIC_PATH) == EXPECTED_AUTOMATIC_SHA
    assert [cue.source_index for cue in automatic] == list(range(1, 23))
    assert all(cue.speaker == HOST_SPEAKER for cue in automatic)
    assert [(cue.start, cue.end, cue.text) for cue in automatic] == [
        (cue.start, cue.end, cue.text) for cue in _current_cues()
    ]


def test_221_override_is_hash_bound_and_partitions_all_22_cues_as_host() -> None:
    document = _override()
    truth = _truth()

    validate_bound_speaker_override_document(
        OVERRIDE_PATH,
        candidate_id=CANDIDATE,
        expected_source_media_sha256=EXPECTED_MEDIA_SHA,
        expected_text_final_srt_sha256=EXPECTED_TEXT_SHA,
    )
    baseline = document["reviewed_speaker_baseline"]
    assert document["operator_review_binding"] == {
        "path": str(REVIEW_BINDING_PATH.relative_to(ROOT)),
        "sha256": EXPECTED_REVIEW_BINDING_SHA,
    }
    assert baseline["truth_input"] == {
        "path": str(TRUTH_PATH.relative_to(ROOT)),
        "sha256": _sha256(TRUTH_PATH),
    }
    assert baseline["automatic_input"] == {
        "path": str(AUTOMATIC_PATH.relative_to(ROOT)),
        "sha256": EXPECTED_AUTOMATIC_SHA,
    }
    assert baseline["machine_cues"] == []
    assert len(document["overrides"]) == len(truth["cues"]) == 22
    assert [row["source_cue"] for row in document["overrides"]] == list(range(1, 23))
    assert {segment["speaker"] for row in document["overrides"] for segment in row["segments"]} == {
        HOST_SPEAKER
    }


def test_221_reviewed_delivery_binding_seals_record_video_and_current_source() -> None:
    binding = json.loads(REVIEW_BINDING_PATH.read_text(encoding="utf-8"))

    assert _sha256(REVIEW_BINDING_PATH) == EXPECTED_REVIEW_BINDING_SHA
    assert binding["candidate_id"] == CANDIDATE
    assert binding["authority"] == AUTHORITY
    assert binding["current_record_sha256"] == EXPECTED_CURRENT_RECORD_SHA
    assert binding["reviewed_delivery_video"] == {
        "basename": ("auto_221234_1349_1418.EXACT_CURRENT_DELIVERY.speaker-hold-review.mp4"),
        "sha256": EXPECTED_REVIEWED_VIDEO_SHA,
    }
    assert binding["source_recording"] == _source_recording()
    assert binding["subtitle_text_authorized"] is False
    assert binding["upload_authorized"] is False


def test_221_override_loads_against_exact_grid_and_applies_without_text_change() -> None:
    document = _override()
    current_cues = _current_cues()

    loaded = load_reviewed_speaker_baseline(
        document,
        candidate_id=CANDIDATE,
        cues=current_cues,
        repo_root=ROOT,
        expected_source_recording=_source_recording(),
    )
    assert loaded is not None
    assert loaded.machine_cues == ()
    assert loaded.evidence["reviewed_cue_count"] == 22
    assert loaded.evidence["authority"] == AUTHORITY

    automatic = parse_labelled_srt(AUTOMATIC_PATH)
    applied = apply_overrides(automatic, document)
    assert len(applied) == 22
    assert all(cue.speaker == HOST_SPEAKER for cue in applied)
    assert [(cue.start, cue.end, cue.text) for cue in applied] == [
        (cue.start, cue.end, cue.text) for cue in automatic
    ]
