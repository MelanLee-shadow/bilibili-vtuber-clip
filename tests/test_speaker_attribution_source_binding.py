"""Ordinary source writer/replay controls; model responses are synthetic.

No human annotations, real audio, networking, or new identity threshold.
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import wave

import pytest

from src.autoslice import speaker_attribution as sa
from src.autoslice import speaker_finalizer as f
from src.autoslice.speaker_common import DEFAULT_POLICY

SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\n她说第一句\n\n2\n00:00:01,100 --> 00:00:02,100\n她说第二句\n"
)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    media = tmp_path / "source.wav"
    with wave.open(str(media), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 48000)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"talk_speaker_policy": DEFAULT_POLICY}))
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights").write_bytes(b"synthetic weights")
    refdir = tmp_path / "references"
    refdir.mkdir()
    refs = []
    for i in range(3):
        p = refdir / f"r{i}.wav"
        p.write_bytes(bytes([i]) * 10)
        refs.append({"id": f"r{i}", "filename": p.name, "path": p, "sha256": sha(p)})
    modelhash = f._sha256_directory(model)
    monkeypatch.setattr(
        f,
        "_load_runtime",
        lambda *args: ({"talk_speaker_policy": DEFAULT_POLICY}, refs, modelhash, object()),
    )
    monkeypatch.setattr(
        f,
        "_extract_cue_wavs",
        lambda media, cues, work: (None, 16000, [tmp_path / "c1", tmp_path / "c2"]),
    )
    monkeypatch.setattr(
        f,
        "_build_embedding_similarity",
        lambda **kwargs: lambda ref, cue: 0.85 if cue.name == "c1" else 0.82,
    )
    srt = tmp_path / "source.srt"
    srt.write_text(SRT)
    return {
        "media": media,
        "profile": profile,
        "model": model,
        "reference": refdir,
        "srt": srt,
        "tmp": tmp_path,
    }


def emit(runtime, *, suffix="a"):
    out = runtime["tmp"] / (suffix + ".json")
    assert (
        sa.main(
            [
                "--media",
                str(runtime["media"]),
                "--srt",
                str(runtime["srt"]),
                "--profile",
                str(runtime["profile"]),
                "--reference-dir",
                str(runtime["reference"]),
                "--model-dir",
                str(runtime["model"]),
                "--work-dir",
                str(runtime["tmp"] / ("work-" + suffix)),
                "--output",
                str(out),
                "--runtime-location",
                "synthetic",
                "--no-context",
            ]
        )
        == 0
    )
    return json.loads(out.read_text())


def test_public_cli_distinguishes_different_media_bytes(runtime):
    first = emit(runtime)
    with runtime["media"].open("ab") as out:
        out.write(b"synthetic container metadata")
    second = emit(runtime, suffix="b")
    assert first != second  # Old normal writer emitted identical unbound receipts.
    assert first["source_binding"]["media_sha256"] != second["source_binding"]["media_sha256"]


def test_public_factory_persistence_and_exact_consumer(runtime):
    doc = emit(runtime)
    binding = doc["source_binding"]
    assert binding["media_sha256"] == sha(runtime["media"])
    assert binding["profile_sha256"] == sha(runtime["profile"])
    assert binding["srt_sha256"] == sa._sha(SRT)
    assert "1. HOST" in sa.attribution_context_for_srt(
        doc,
        SRT,
        expected_source_media_sha256=sha(runtime["media"]),
        expected_profile_sha256=sha(runtime["profile"]),
        expected_timeline_offset_ms=0,
    )


@pytest.mark.parametrize("damage", ["media", "profile", "model", "reference"])
def test_real_file_drift_during_scoring_discards_identity(runtime, damage):
    scorer, policy = sa.build_campp_cue_scorer(
        media_path=runtime["media"],
        srt_text=SRT,
        profile_path=runtime["profile"],
        reference_dir=runtime["reference"],
        model_dir=runtime["model"],
        work_dir=runtime["tmp"] / "drift-work",
    )
    target = (
        runtime[damage]
        if damage in ("media", "profile")
        else runtime["model"] / "weights"
        if damage == "model"
        else runtime["reference"] / "r0.wav"
    )
    with target.open("ab") as out:
        out.write(b"drift")
    calls = []
    doc = sa.attribute_speakers(
        SRT, scorer=scorer, policy=policy, llm_call=lambda p: calls.append(p) or "{}"
    )
    assert all(r["speaker"] == "UNKNOWN" for r in doc["cues"])
    assert calls == []
    assert "SCORER_SOURCE_DRIFT" in doc["reason_codes"]


@pytest.mark.parametrize(
    "damage", ["media", "profile", "window", "srt", "missing_source", "legacy_non_host"]
)
def test_saved_receipt_expected_source_guards(runtime, damage):
    doc = emit(runtime)
    source = sha(runtime["media"])
    profile = sha(runtime["profile"])
    offset = 0
    text = SRT
    if damage == "media":
        source = "f" * 64
    elif damage == "profile":
        profile = "e" * 64
    elif damage == "window":
        offset = 1
    elif damage == "srt":
        text = SRT.replace("第一句", "别的话")
    elif damage == "missing_source":
        doc.pop("source_binding", None)
    else:
        doc["cues"][0]["voiceprint"]["anchor"] = "NON_HOST"
    original = deepcopy(doc)
    assert (
        sa.attribution_context_for_srt(
            doc,
            text,
            expected_source_media_sha256=source,
            expected_profile_sha256=profile,
            expected_timeline_offset_ms=offset,
        )
        == ""
    )
    assert doc == original


def test_positive_legacy_shape_does_not_claim_a_current_media_binding():
    doc = sa.attribute_speakers(SRT, scorer=lambda i: 0.85)
    assert sa.attribution_context_for_srt(doc, SRT)
    assert sa.attribution_context_for_srt(doc, SRT, expected_source_media_sha256="a" * 64) == ""


@pytest.mark.parametrize("source_ok", [True, False])
def test_ordinary_final_review_passes_only_bound_attribution_to_real_pronoun_consumer(
    runtime, monkeypatch, source_ok
):
    from src.autoslice import producer_text_pipeline as p
    from tests.test_producer_text_pipeline_final_review import (
        _adapters,
        _correction_pass,
        _literal_review_llm,
    )

    doc = emit(runtime)
    prompts = []
    # Isolate unrelated scene-context formatting; keep real speaker and pronoun consumers.
    monkeypatch.setattr(p, "clip_context_prompt_text", lambda _: "Synthetic scene context")

    def pronoun(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": f"cue-{i}-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "她",
                        "referent": "UNSPECIFIED",
                        "reason": "Synthetic neutral control",
                    }
                    for i in (1, 2)
                ]
            }
        )

    adapters = replace(_adapters(), profile_asset_file=lambda _: runtime["profile"])
    receipt = p._run_exact_final_release_review(
        srt_text=SRT,
        correction_audit=_correction_pass(),
        adapters=adapters,
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verified_authority_audit={"speaker_attribution": doc},
        source_media_sha256=sha(runtime["media"]) if source_ok else "f" * 64,
        final_review_llm=_literal_review_llm(lambda _: '{"findings": []}'),
        pronoun_audit_llm=pronoun,
    )
    assert prompts, json.dumps(
        {
            k: v
            for k, v in receipt.items()
            if k in ("status", "reason_codes", "error", "detail", "discovery")
        },
        ensure_ascii=False,
    )
    assert ("1. HOST（voiceprint_anchor）" in prompts[0]) is source_ok
    assert (
        "speaker_attribution_context_sha256" in receipt["candidate_pronoun_consistency_audit"]
    ) is source_ok


def test_current_source_changes_context_fingerprint_even_with_same_labels(runtime):
    first = emit(runtime)
    first_context = sa.attribution_context_for_srt(first, SRT)
    with runtime["media"].open("ab") as out:
        out.write(b"new source bytes")
    second = emit(runtime, suffix="new-source")
    second_context = sa.attribution_context_for_srt(second, SRT)
    assert first_context != second_context
    assert "source_binding_sha256=" in second_context


def test_scorer_bound_to_different_srt_cannot_supply_positive_identities(runtime):
    scorer, policy = sa.build_campp_cue_scorer(
        media_path=runtime["media"],
        srt_text=SRT,
        profile_path=runtime["profile"],
        reference_dir=runtime["reference"],
        model_dir=runtime["model"],
        work_dir=runtime["tmp"] / "different-srt",
    )
    calls = []
    doc = sa.attribute_speakers(
        SRT.replace("第一句", "别的话"),
        scorer=scorer,
        policy=policy,
        llm_call=lambda p: calls.append(p) or "{}",
    )
    assert {x["speaker"] for x in doc["cues"]} == {"UNKNOWN"} and calls == []
    assert "SCORER_SOURCE_DRIFT" in doc["reason_codes"]


def test_unsupported_relabelling_to_shifted_clock_is_not_a_valid_conversion(runtime):
    doc = emit(runtime)
    doc["source_binding"]["timeline_offset_ms"] = 100
    assert (
        sa.attribution_context_for_srt(
            doc,
            SRT,
            expected_source_media_sha256=sha(runtime["media"]),
            expected_profile_sha256=sha(runtime["profile"]),
            expected_timeline_offset_ms=100,
        )
        == ""
    )


def test_runtime_binding_cannot_be_mutated_by_the_caller(runtime):
    scorer, _ = sa.build_campp_cue_scorer(
        media_path=runtime["media"],
        srt_text=SRT,
        profile_path=runtime["profile"],
        reference_dir=runtime["reference"],
        model_dir=runtime["model"],
        work_dir=runtime["tmp"] / "immutable-binding",
    )
    with pytest.raises(TypeError):
        scorer.source_binding["media_sha256"] = "f" * 64


def test_source_drift_while_context_runs_cannot_be_persisted_as_identity(runtime, monkeypatch):
    text = SRT + "\n3\n00:00:02,200 --> 00:00:02,900\n她又说了一句\n"
    monkeypatch.setattr(
        f,
        "_extract_cue_wavs",
        lambda media, cues, work: (
            None,
            16000,
            [runtime["tmp"] / name for name in ("c1", "c2", "c3")],
        ),
    )
    monkeypatch.setattr(
        f,
        "_build_embedding_similarity",
        lambda **kwargs: lambda ref, cue: {"c1": 0.85, "c2": 0.82, "c3": 0.55}[cue.name],
    )
    scorer, policy = sa.build_campp_cue_scorer(
        media_path=runtime["media"],
        srt_text=text,
        profile_path=runtime["profile"],
        reference_dir=runtime["reference"],
        model_dir=runtime["model"],
        work_dir=runtime["tmp"] / "context-drift",
    )
    calls = []

    def context(prompt):
        calls.append(prompt)
        with runtime["media"].open("ab") as out:
            out.write(b"changed during context")
        return json.dumps(
            {
                "labels": [
                    {
                        "n": 3,
                        "speaker": "OTHER",
                        "confidence": 0.9,
                        "reason": "Synthetic context",
                        "address": None,
                    }
                ]
            }
        )

    doc = sa.attribute_speakers(text, scorer=scorer, policy=policy, llm_call=context)
    assert len(calls) == 1
    assert {row["speaker"] for row in doc["cues"]} == {"UNKNOWN"}
    assert "SCORER_SOURCE_DRIFT" in doc["reason_codes"]
    assert "source_binding" not in doc


@pytest.mark.parametrize("profile_state", ["missing", "none"])
def test_missing_optional_profile_never_blocks_ordinary_text_review(
    runtime, monkeypatch, profile_state
):
    from src.autoslice import producer_text_pipeline as p
    from tests.test_producer_text_pipeline_final_review import (
        _adapters,
        _correction_pass,
        _literal_review_llm,
    )

    doc = emit(runtime)
    monkeypatch.setattr(p, "clip_context_prompt_text", lambda _: "Synthetic scene context")
    profile = runtime["tmp"] / "missing-profile" if profile_state == "missing" else None
    adapters = replace(_adapters(), profile_asset_file=lambda _: profile)
    prompts = []

    def pronoun(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": f"cue-{i}-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "她",
                        "referent": "UNSPECIFIED",
                        "reason": "Synthetic neutral control",
                    }
                    for i in (1, 2)
                ]
            }
        )

    receipt = p._run_exact_final_release_review(
        srt_text=SRT,
        correction_audit=_correction_pass(),
        adapters=adapters,
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verified_authority_audit={"speaker_attribution": doc},
        source_media_sha256=sha(runtime["media"]),
        final_review_llm=_literal_review_llm(lambda _: '{"findings": []}'),
        pronoun_audit_llm=pronoun,
    )
    assert prompts and "voiceprint_anchor" not in prompts[0]
    assert (
        "speaker_attribution_context_sha256" not in receipt["candidate_pronoun_consistency_audit"]
    )
