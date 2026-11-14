import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.huozi_luanshua import _write_plan_srt
from src.autoslice.huozi_luanshua import (
    CorpusValidationError,
    UncoveredTextError,
    VerificationError,
    apply_verification,
    build_corpus,
    build_verification,
    canonical_json_bytes,
    plan_text,
    validate_renderable_plan,
)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utterance(text):
    words = [{"label": char, "start_time": i * 120, "end_time": (i + 1) * 120}
             for i, char in enumerate(text)]
    return {"transcript": text, "start_time": 0, "end_time": len(words) * 120, "words": words}


def _inputs(tmp_path, *, written="kmx", heard_a="q为什么", heard_b="提莫熊"):
    left, right = "不是那种可以和", "亲亲的关系"
    target = left + written + right
    a_text, b_text = left + heard_a + right, left + heard_b + right
    media = tmp_path / "excerpt.mp4"
    media.write_bytes(b"immutable source excerpt")
    media_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    a_path, b_path = tmp_path / "a.json", tmp_path / "b.json"
    a_sha = _write_json(a_path, {"utterances": [_utterance(a_text)]})
    b_sha = _write_json(b_path, {"utterances": [_utterance(b_text)]})
    speaker = tmp_path / "speaker.json"
    _write_json(speaker, {"authority": "human"})
    review = tmp_path / "name-review.json"
    review_sha = _write_json(review, {
        "authority": "human confirmation", "source_excerpt_sha256": media_sha,
        "bcut_raw_sha256": a_sha, "bcut_raw_text": a_text,
        "jianying_raw_sha256": b_sha, "jianying_raw_text": b_text,
        "reviewer_text": target, "proper_name_spelling": written,
    })
    source = {
        "source_id": "test-source", "source_date": "2026-11-06",
        "media_path": str(media), "media_sha256": media_sha, "asr_json_path": str(a_path),
        "speaker": "lidousha", "speaker_authority": "reviewer_confirmed_solo_session",
        "speaker_confidence": 1.0, "speaker_evidence": [{"authority": "human", "path": str(speaker)}],
        "transcript_confidence": 0.99, "transcript_authorities": ["bcut", "jianying"],
        "transcript_evidence": [
            {"authority": "bcut", "path": str(a_path), "confidence": 0.99},
            {"authority": "jianying", "path": str(b_path), "confidence": 0.99},
        ],
        "content_kind": "talk",
    }
    return {"schema_version": "huozi-source-manifest.v1", "sources": [source]}, target, a_text, b_text, review, review_sha


@pytest.mark.parametrize("written,heard_a,heard_b", [
    ("kmx", "q为什么", "提莫熊"),
    ("莉莉", "小李", "小丽"),
])
def test_reviewed_spoken_name_reaches_renderable_plan_and_srt(tmp_path, written, heard_a, heard_b):
    manifest, target, spoken, other_spoken, review, review_sha = _inputs(
        tmp_path, written=written, heard_a=heard_a, heard_b=heard_b)
    with pytest.raises(UncoveredTextError):
        plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    source = manifest["sources"][0]
    source.update(proper_name_review_path=str(review), proper_name_review_sha256=review_sha)
    original_transcripts = [Path(row["path"]).read_bytes() for row in source["transcript_evidence"]]
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    assert len(plan["pieces"]) == 1
    assert plan["pieces"][0]["text"] == spoken
    assert plan["pieces"][0]["display_text"] == target
    verification = build_verification(plan, timing_tolerance_ms=500)
    evidence = verification["pieces"]["p001"]["evidence"]
    assert [row["observed_text"] for row in evidence] == [spoken, other_spoken]
    verified = apply_verification(plan, verification)
    validate_renderable_plan(verified)
    srt = tmp_path / "result.srt"
    _write_plan_srt(verified, [2000], srt)
    assert target in srt.read_text(encoding="utf-8")
    assert [Path(row["path"]).read_bytes() for row in source["transcript_evidence"]] == original_transcripts


def test_review_hash_and_range_are_required(tmp_path):
    manifest, target, _, _, review, review_sha = _inputs(tmp_path)
    source = manifest["sources"][0]
    source.update(proper_name_review_path=str(review), proper_name_review_sha256="0" * 64)
    with pytest.raises(CorpusValidationError, match="review hash drift"):
        build_corpus(manifest, manifest_dir=tmp_path)
    source["proper_name_review_sha256"] = review_sha
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    altered = copy.deepcopy(plan)
    altered["pieces"][0]["proper_name_review"]["source_name_span"][0] += 1
    with pytest.raises(VerificationError, match="binding or spoken range drift"):
        build_verification(altered)
    altered = copy.deepcopy(plan)
    altered["pieces"][0]["core_start_ms"] += 10
    with pytest.raises(VerificationError, match="spoken range drift"):
        build_verification(altered)
    Path(source["media_path"]).write_bytes(b"different media")
    with pytest.raises(CorpusValidationError, match="media hash drift"):
        build_corpus(manifest, manifest_dir=tmp_path)


def test_unreviewed_display_change_remains_rejected(tmp_path):
    manifest, _, spoken, _, _, _ = _inputs(tmp_path, heard_b="q为什么")
    plan = plan_text(spoken, build_corpus(manifest, manifest_dir=tmp_path))
    plan["pieces"][0]["display_text"] = "不是那种可以和kmx亲亲的关系"
    with pytest.raises(VerificationError, match="no proper-name review"):
        build_verification(plan)


def test_render_rejects_unreviewed_clause_display_even_after_rehash(tmp_path):
    manifest, _, spoken, _, _, _ = _inputs(tmp_path, heard_b="q为什么")
    plan = plan_text(spoken, build_corpus(manifest, manifest_dir=tmp_path))
    verified = apply_verification(plan, build_verification(plan))
    verified["clauses"][0]["display"] = "不是那种可以和kmx亲亲的关系"
    verified.pop("verified_plan_sha256")
    verified["verified_plan_sha256"] = hashlib.sha256(canonical_json_bytes(verified)).hexdigest()
    with pytest.raises(VerificationError, match="display text drift"):
        validate_renderable_plan(verified)


def test_review_can_select_one_of_repeated_timed_utterances(tmp_path):
    manifest, target, a_text, b_text, review_path, _ = _inputs(tmp_path)
    source = manifest["sources"][0]
    rows = []
    for spec, text, name in zip(source["transcript_evidence"], (a_text, b_text), ("q为什么", "提莫熊")):
        path = Path(spec["path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        repeated = copy.deepcopy(payload["utterances"][0])
        repeated["start_time"] += 3000
        repeated["end_time"] += 3000
        for word in repeated["words"]:
            word["start_time"] += 3000
            word["end_time"] += 3000
        payload["utterances"].append(repeated)
        rows.append({"authority": spec["authority"], "sha256": _write_json(path, payload),
                     "raw_text": text, "utterance_index": 1,
                     "name_interval_ms": [3840, 3840 + len(name) * 120]})
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["observations"] = rows
    source.update(proper_name_review_path=str(review_path), proper_name_review_sha256=_write_json(review_path, review))
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    assert plan["pieces"][0]["core_start_ms"] == 3000
    validate_renderable_plan(apply_verification(plan, build_verification(plan, timing_tolerance_ms=500)))
    review["observations"][0]["name_interval_ms"][0] += 1
    source["proper_name_review_sha256"] = _write_json(review_path, review)
    with pytest.raises(CorpusValidationError, match="reviewed range does not match"):
        build_corpus(manifest, manifest_dir=tmp_path)


def test_review_can_select_name_when_context_repeats_inside_it(tmp_path):
    names = ("小亲亲的关系熊", "小亲亲的关系虎")
    manifest, target, a_text, b_text, review_path, _ = _inputs(tmp_path, heard_a=names[0], heard_b=names[1])
    source = manifest["sources"][0]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["observations"] = [
        {"authority": spec["authority"], "sha256": hashlib.sha256(Path(spec["path"]).read_bytes()).hexdigest(),
         "raw_text": text, "utterance_index": 0, "name_char_span": [7, 7 + len(name)],
         "name_interval_ms": [840, 840 + len(name) * 120]}
        for spec, text, name in zip(source["transcript_evidence"], (a_text, b_text), names)
    ]
    source.update(proper_name_review_path=str(review_path), proper_name_review_sha256=_write_json(review_path, review))
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    assert len(plan["pieces"]) == 1 and plan["pieces"][0]["display_text"] == target
    validate_renderable_plan(apply_verification(plan, build_verification(plan, timing_tolerance_ms=500)))


def test_review_accepts_punctuation_tokens_from_asr(tmp_path):
    manifest, target, _, _, review_path, _ = _inputs(tmp_path)
    source = manifest["sources"][0]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    for spec in source["transcript_evidence"]:
        path = Path(spec["path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["utterances"][0]["words"].insert(2, {"label": "，", "start_time": 240, "end_time": 240})
        review[f"{spec['authority']}_raw_sha256"] = _write_json(path, payload)
    source.update(proper_name_review_path=str(review_path), proper_name_review_sha256=_write_json(review_path, review))
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    validate_renderable_plan(apply_verification(plan, build_verification(plan, timing_tolerance_ms=500)))


@pytest.mark.parametrize("shift_ms", [600, 3000])
def test_review_rejects_transcript_with_different_spoken_range(tmp_path, shift_ms):
    manifest, _, _, _, review_path, _ = _inputs(tmp_path)
    source = manifest["sources"][0]
    other_path = Path(source["transcript_evidence"][1]["path"])
    other = json.loads(other_path.read_text(encoding="utf-8"))
    for word in other["utterances"][0]["words"]:
        word["start_time"] += shift_ms
        word["end_time"] += shift_ms
    other_sha = _write_json(other_path, other)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["jianying_raw_sha256"] = other_sha
    source.update(proper_name_review_path=str(review_path), proper_name_review_sha256=_write_json(review_path, review))
    with pytest.raises(CorpusValidationError, match="spoken ranges do not match"):
        build_corpus(manifest, manifest_dir=tmp_path)




CUE_ID = "clip#srt-46"
CUE_TEXT = "妈妈，我要这个，我要这个，给我买"
CUE_RANGE = [380931, 385111]


def _export_inputs(tmp_path, *, heard="吃", offset=381000):
    """Both ASRs heard 我要吃 where 维护者's export cue says 我要这个; the review names that span."""
    manifest, _, _, _, review_path, _ = _inputs(tmp_path)
    target = "妈妈我要这个我要这个给我买"
    spoken = "妈妈我要这个我要" + heard + "给我买"
    source = manifest["sources"][0]
    for spec in source["transcript_evidence"]:
        _write_json(Path(spec["path"]), {"utterances": [_utterance(spoken)]})
    shas = {spec["authority"]: hashlib.sha256(Path(spec["path"]).read_bytes()).hexdigest()
            for spec in source["transcript_evidence"]}
    export = tmp_path / "reviewer_labels.json"
    export_sha = _write_json(export, {"labels": {CUE_ID: {
        "speaker": "李豆沙", "text": CUE_TEXT, "abs_start_ms": CUE_RANGE[0], "abs_end_ms": CUE_RANGE[1]}}})
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review.update(
        reviewer_text=target, written_char_span=[8, 10], proper_name_spelling="这个",
        bcut_raw_sha256=shas["bcut"], bcut_raw_text=spoken,
        jianying_raw_sha256=shas["jianying"], jianying_raw_text=spoken,
        reviewer_export_path=str(export), reviewer_export_sha256=export_sha, cue_id=CUE_ID, cue_range_ms=CUE_RANGE)
    source["excerpt_offset_ms"] = offset
    return manifest, target, spoken, review, review_path, source


def _attach(source, review, review_path):
    source.update(proper_name_review_path=str(review_path),
                  proper_name_review_sha256=_write_json(review_path, review))


def test_export_cue_review_keeps_the_original_audio_and_shows_reviewers_text(tmp_path):
    manifest, target, spoken, review, review_path, source = _export_inputs(tmp_path)
    unreviewed = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    assert len(unreviewed["pieces"]) > 1  # without the review the second 这个 can only be borrowed audio
    assert not any(row.get("proper_name_review") for row in unreviewed["pieces"])
    _attach(source, review, review_path)
    originals = [Path(row["path"]).read_bytes() for row in source["transcript_evidence"]]
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    assert len(plan["pieces"]) == 1
    piece = plan["pieces"][0]
    assert piece["text"] == spoken and piece["display_text"] == target
    assert piece["proper_name_review"]["cue_id"] == CUE_ID
    assert (piece["core_start_ms"], piece["core_end_ms"]) == (0, len(spoken) * 120)
    verified = apply_verification(plan, build_verification(plan, timing_tolerance_ms=500))
    validate_renderable_plan(verified)
    srt = tmp_path / "result.srt"
    _write_plan_srt(verified, [2000], srt)
    assert target in srt.read_text(encoding="utf-8")
    assert [Path(row["path"]).read_bytes() for row in source["transcript_evidence"]] == originals


def test_export_cue_review_without_its_record_or_with_a_changed_binding_fails(tmp_path):
    manifest, target, _, review, review_path, source = _export_inputs(tmp_path)
    _attach(source, review, review_path)
    plan = plan_text(target, build_corpus(manifest, manifest_dir=tmp_path))
    altered = copy.deepcopy(plan)
    altered["pieces"][0]["proper_name_review"]["cue_range_ms"][1] += 1
    with pytest.raises(VerificationError, match="binding or spoken range drift"):
        build_verification(altered)
    altered = copy.deepcopy(plan)
    altered["pieces"][0].pop("proper_name_review")
    with pytest.raises(VerificationError, match="no proper-name review"):
        build_verification(altered)


@pytest.mark.parametrize("change,match", [
    ({"reviewer_export_sha256": "0" * 64}, "export hash drift"),
    ({"cue_id": "clip#missing"}, "cue text mismatch"),
    ({"cue_range_ms": [380931, 385112]}, "cue range mismatch"),
    ({"reviewer_text": "妈妈我要这个我要那个给我买", "proper_name_spelling": "那个"}, "cue text mismatch"),
    ({"reviewer_export_path": None}, "export binding is incomplete"),
    ({"written_char_span": [8, 8]}, "written range is malformed"),
    ({"proper_name_spelling": "那个"}, "does not match the spelling"),
])
def test_export_cue_review_rejects_bad_records(tmp_path, change, match):
    manifest, _, _, review, review_path, source = _export_inputs(tmp_path)
    review.update(change)
    _attach(source, review, review_path)
    with pytest.raises(CorpusValidationError, match=match):
        build_corpus(manifest, manifest_dir=tmp_path)


def test_export_cue_review_rejects_wrong_review_hash_and_speech_outside_the_cue(tmp_path):
    manifest, _, _, review, review_path, source = _export_inputs(tmp_path)
    _attach(source, review, review_path)
    source["proper_name_review_sha256"] = "0" * 64
    with pytest.raises(CorpusValidationError, match="review hash drift"):
        build_corpus(manifest, manifest_dir=tmp_path)
    source["proper_name_review_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    source["excerpt_offset_ms"] = CUE_RANGE[1] - 600
    with pytest.raises(CorpusValidationError, match="spoken interval outside cue range"):
        build_corpus(manifest, manifest_dir=tmp_path)
    source.pop("excerpt_offset_ms")
    with pytest.raises(CorpusValidationError, match="recording-clock offset"):
        build_corpus(manifest, manifest_dir=tmp_path)
