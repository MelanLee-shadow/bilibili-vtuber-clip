"""Synthetic controls for diagnostic coverage, not new human subtitle truth."""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts import diagnose_subtitle_audio_disagreements as diagnostic


def make_srt(texts: list[str], *, offset: int = 0) -> str:
    def timestamp(ms):
        seconds, millis = divmod(ms, 1000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return (
        "\n\n".join(
            f"{i}\n{timestamp((i - 1) * 2000 + offset)} --> {timestamp((i - 1) * 2000 + 1000 + offset)}\n{text}"
            for i, text in enumerate(texts, 1)
        )
        + "\n"
    )


@pytest.fixture
def inputs(tmp_path):
    texts = [
        "opening boundary sentence",
        "original question",
        "middle anchor statement",
        "brief real answer",
        "closing distinct sentence",
    ]
    final, media, witness, provenance = [
        tmp_path / name for name in ("final.srt", "media.mp4", "witness.srt", "provenance.json")
    ]
    final.write_text(make_srt(texts))
    media.write_bytes(b"test media binding; no decoder invoked")
    witness.write_text(make_srt(texts, offset=5000))

    def bind():
        provenance.write_text(
            json.dumps(
                {
                    "schema_version": "subtitle-audio-correspondence-provenance.v1",
                    "actual_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                    "witness_srt_sha256": hashlib.sha256(witness.read_bytes()).hexdigest(),
                    "timebase": {
                        "unit": "ms",
                        "final_srt": "delivery_local_ms",
                        "witness_srt": "actual_media_local_ms",
                        "intro_offset_application": "add_once_to_final_srt",
                        "declared_intro_offset_ms": 5000,
                    },
                }
            )
        )

    bind()
    return (final, media, witness, provenance, bind)


def run(inputs):
    return diagnostic.diagnose(*inputs[:4], intro_offset_ms=5000)


def test_unanchored_wrong_sentences_survive_timing_pass(inputs):
    final = inputs[0]
    text = final.read_text().replace("original question", "unrelated invented story")
    text = text.replace("brief real answer", "some different narration")
    final.write_text(text)
    before = [p.read_bytes() for p in inputs[:4]]
    report = run(inputs)
    assert report["timing_status"] == "PASS"
    assert report["review_candidate_indexes"] == ["2", "4"]
    assert all(
        not row["used_as_timing_anchor"] for row in report["rows"] if row["cue_index"] in {"2", "4"}
    )
    assert report["mutation_authorized"] is False and report["release_authorized"] is False
    assert report["text_correctness_status"] == "UNASSESSED"
    assert report["cue_count"] == 5 and [p.read_bytes() for p in inputs[:4]] == before


def test_repair_removes_disagreement_without_semantic_pass(inputs):
    result = run(inputs)
    assert result["review_candidate_indexes"] == []
    assert result["status"] == "DIAGNOSTIC_ONLY"
    assert result["text_correctness_status"] == "UNASSESSED"
    assert result["rows"][1]["actual_media_interval_ms"] == [7000, 8000]


def test_no_overlap_is_not_deleted_or_matched_to_distant_identical_text(inputs):
    final, _, witness, _, bind = inputs
    witness.write_text(
        witness.read_text().replace(
            "00:00:07,000 --> 00:00:08,000", "00:00:08,200 --> 00:00:08,600"
        )
    )
    bind()
    result = run(inputs)
    assert result["rows"][1]["comparison"] == "NO_TEMPORAL_WITNESS"
    assert result["rows"][1]["current_text"] == "original question"


def test_tied_temporal_matches_are_not_resolved_by_convenient_text(inputs):
    _, _, witness, _, bind = inputs
    text = witness.read_text().replace(
        "2\n00:00:07,000 --> 00:00:08,000\noriginal question",
        "2\n00:00:07,000 --> 00:00:08,000\noriginal question\n\nsecond\n00:00:07,000 --> 00:00:08,000\ncontradictory witness",
    )
    witness.write_text(text)
    bind()
    result = run(inputs)
    row = result["rows"][1]
    assert row["comparison"] == "AMBIGUOUS_TEMPORAL_WITNESS"
    assert row["dominant_witness_indexes"] == ["2", "second"]


def test_short_cue_is_not_filtered_out(inputs):
    final = inputs[0]
    final.write_text(final.read_text().replace("brief real answer", "no"))
    assert "4" in run(inputs)["review_candidate_indexes"]


def test_punctuation_differences_are_descriptive_not_mutation(inputs):
    final = inputs[0]
    final.write_text(final.read_text().replace("original question", "Original, QUESTION!"))
    assert run(inputs)["review_candidate_indexes"] == []
    assert "Original, QUESTION!" in final.read_text()


@pytest.mark.parametrize("fault", ["media", "witness", "offset", "duplicate_cue"])
def test_invalid_evidence_is_not_a_diagnostic_success(inputs, fault):
    final, media, witness, provenance, _ = inputs
    if fault == "media":
        media.write_bytes(b"wrong source")
    elif fault == "witness":
        witness.write_text(witness.read_text() + "\n")
    elif fault == "offset":
        data = json.loads(provenance.read_text())
        data["timebase"]["declared_intro_offset_ms"] = 0
        provenance.write_text(json.dumps(data))
    else:
        final.write_text(final.read_text().replace("2\n00:00:02", "1\n00:00:02"))
    with pytest.raises(ValueError):
        run(inputs)


def test_timing_block_is_not_cleared(inputs):
    final = inputs[0]
    final.write_text(make_srt(["one", "two", "three", "four", "five"]))
    result = run(inputs)
    assert result["timing_status"] == "INSUFFICIENT"
    assert result["text_correctness_status"] == "UNASSESSED"


def test_drift_during_comparison_is_rejected(inputs, monkeypatch):
    real = diagnostic.check_subtitle_audio_correspondence

    def drift(*args, **kwargs):
        result = real(*args, **kwargs)
        inputs[0].write_text(inputs[0].read_text().replace("original question", "changed"))
        return result

    monkeypatch.setattr(diagnostic, "check_subtitle_audio_correspondence", drift)
    with pytest.raises(ValueError, match="changed during comparison"):
        run(inputs)


def test_cli_outputs_diagnostic_without_writing_input_files(inputs, capsys):
    before = [p.read_bytes() for p in inputs[:4]]
    argv = [
        "--final-srt",
        str(inputs[0]),
        "--actual-media",
        str(inputs[1]),
        "--witness-srt",
        str(inputs[2]),
        "--provenance",
        str(inputs[3]),
        "--intro-offset-ms",
        "5000",
    ]
    assert diagnostic.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DIAGNOSTIC_ONLY" and result["mutation_authorized"] is False
    assert [p.read_bytes() for p in inputs[:4]] == before


def test_cli_invalid_provenance_has_no_success_json(inputs, capsys):
    inputs[1].write_bytes(b"changed actual media")
    argv = [
        "--final-srt",
        str(inputs[0]),
        "--actual-media",
        str(inputs[1]),
        "--witness-srt",
        str(inputs[2]),
        "--provenance",
        str(inputs[3]),
        "--intro-offset-ms",
        "5000",
    ]
    assert diagnostic.main(argv) == 2
    output = capsys.readouterr()
    assert not output.out and "failed" in output.err
