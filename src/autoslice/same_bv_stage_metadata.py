"""Replay an explicitly root-approved title/cover stage and its real CPA input.

This consumes the manual reaction delivery as a partial artifact.  It does not
upgrade transcripts into final subtitles or claim whole-media acceptance.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .channel_envelope import parse_publish_description, talk_envelope_violations, talk_title_partners
from .same_bv_stage_repair import replay, require
from .same_bv_repair import sha256_file, PlanInvalid
from .title_cover_candidates import parse_answer
from .title_policy import publish_title_lane, publish_title_policy_violations


def _sha(path, expected):
    path = Path(path)
    require(path.is_absolute() and path.is_file() and sha256_file(path) == expected,
            f"title/cover stage artifact drift: {path}")
    return path


def _publication_title_lane(request):
    source = replay(request.get("publication_record"))
    clips = [c for c in source.get("clips", []) if c.get("clip_id") == request.get("candidate_id")]
    require(len(clips) == 1 and clips[0].get("bvid") == request.get("bvid"),
            "historical publication target mismatch")
    observed_public_title = clips[0].get("title")
    require(isinstance(observed_public_title, str) and observed_public_title.strip(),
            "historical publication title missing")
    # As in normal same-BV authority, the bound original title fixes the lane;
    # an approved successor title cannot select a different title policy.
    return publish_title_lane(observed_public_title)


def stage_envelope_problem(request):
    """Envelope rule for a NEW title/cover stage plan (维护者).

    The deliverer's prefix declares the partners; both envelope outputs come
    from that one tuple: each partner resolves to the structured registry and
    the preserved live description carries exactly their related lines.  It is
    applied when a plan is created, not when ``validate`` replays an already
    planned or VERIFIED stage (status/verify, or a successor's predecessor).
    """
    title = str(replay(request.get("title_cover_delivery")).get("title") or "")
    if talk_envelope_violations(title):
        return "stage title partner is not a registered streamer"
    live = parse_publish_description((replay(request.get("creator_before")).get("archive") or {}).get("desc"))
    if live is None or live[1] != talk_title_partners(title):
        return "stage title and description partner envelopes disagree"
    return None


def validate(request):
    review = replay(request.get("root_review"))
    delivery = replay(request.get("title_cover_delivery"))
    require(review.get("schema") == "reaction-title-cover-root-review.v1"
            and review.get("status") == "APPROVED_TITLE_COVER_STAGE_ONLY"
            and review.get("candidate_id") == request.get("candidate_id")
            and review.get("bvid") == request.get("bvid"), "root title/cover approval identity mismatch")
    require(review.get("subtitle_final_acceptance") is False
            and review.get("full_media_acceptance") is False
            and review.get("root_observations") and review.get("authorization"),
            "root title/cover approval scope missing")
    require(review.get("title") == delivery.get("title") and review.get("cover") == delivery.get("cover")
            and review.get("delivery") == {k: request["title_cover_delivery"][k] for k in ("path", "sha256")},
            "root approved title/cover/DELIVERY binding drift")
    require(delivery.get("schema") == "manual-reaction-title-cover-stage.v1"
            and delivery.get("candidate_id") == request["candidate_id"]
            and delivery.get("subtitle_input_contract") == "USER_AUTHORIZED_TITLE_COVER_BEFORE_FINAL_SRT"
            and delivery.get("final_srt_status") == "NOT_AVAILABLE_NOT_CLAIMED",
            "manual title/cover stage contract mismatch")
    require(not request.get("video") and not request.get("existing_append")
            and not request.get("metadata_changes") and not request.get("new_cover"),
            "metadata-only stage cannot change media or unapproved metadata")
    title = delivery.get("title")
    require(isinstance(title, str) and title and not publish_title_policy_violations(
        title, lane=_publication_title_lane(request), enforce_automatic_style=True), "stage title violates current title policy")
    for key in ("cover", "phone", "comparison", "source_video"):
        replay(delivery.get(key), as_json=False)
    for key in ("spec", "pixel_replay"):
        replay(delivery.get(key))
    replay(delivery.get("choice"), as_json=False)  # legacy manual choice is a list
    witness = replay(delivery.get("cpa_witness"))
    cpa_request = replay(delivery.get("cpa_request"))
    response = replay(delivery.get("cpa_original_response"))
    verdict = replay(delivery.get("cpa_verdict"))
    question = cpa_request.get("question")
    require(witness.get("schema_version") == "cpa-frame-witness.v1"
            and witness.get("provider") == "cpa" and witness.get("status") == "OBSERVED"
            and witness.get("model") == response.get("model") == delivery.get("cpa_model")
            and response.get("status") == "completed" and response.get("error") is None,
            "real completed CPA observation missing")
    require(question and witness.get("question") == question
            and witness.get("response_sha256", "").removeprefix("sha256:") == delivery["cpa_original_response"]["sha256"],
            "CPA question/response binding drift")
    images = cpa_request.get("images") or []
    require(len(images) >= 4 and len(witness.get("images", [])) == len(images),
            "CPA final/phone/source/identity images missing")
    for index, (image, sent) in enumerate(zip(images, witness["images"])):
        _sha(image.get("source"), image.get("source_sha256"))
        _sha(image.get("sent_path"), image.get("sent_sha256"))
        require(sent.get("image_path") == image["sent_path"]
                and sent.get("image_sha256") == image["sent_sha256"], "CPA sent image identity drift")
        if index < 2:
            target = delivery["cover" if index == 0 else "phone"]
            require(image["source"] == target["path"] and image["source_sha256"] == target["sha256"],
                    "CPA did not review exact selected cover/phone")
    prompt = json.dumps({"question": question, "image_sha256s": [i["image_sha256"] for i in witness["images"]],
                         "model": witness["model"]}, sort_keys=True)
    require(hashlib.sha256(prompt.encode()).hexdigest() == witness.get("prompt_sha256", "").removeprefix("sha256:"),
            "CPA prompt hash drift")
    answer = response.get("output_text") or "".join(
        str(chunk["text"]) for item in response.get("output", []) if item.get("type") == "message"
        for chunk in item.get("content", []) if chunk.get("type") in {"output_text", "text"} and chunk.get("text"))
    require(answer.strip() == witness.get("answer") and parse_answer(answer) == verdict,
            "CPA verdict is not the original provider response")
    require(verdict.get("status") == "PASS" and verdict.get("observed_cover_text") == delivery.get("cover_text")
            and all(verdict.get(k) is True for k in ("identity_ok", "clean_layers", "no_fabrication",
                "modality_and_speaker_ok", "quote_supported", "story_clear", "thumbnail_readable",
                "face_minimum_ok", "no_text_over_face"))
            and not any(i.get("severity") in {"critical", "major"} for i in verdict.get("issues", [])),
            "CPA title/cover stage review did not pass")
    try:
        material = json.loads(question[question.index('{"editorial":'):])
    except (ValueError, TypeError) as exc:
        raise PlanInvalid("CPA complete editorial/transcript material missing") from exc
    editorial = material.get("editorial") or {}
    require(editorial.get("title") == title and editorial.get("lines") == delivery["cover_text"]
            and editorial.get("hook") == delivery.get("hook"), "CPA reviewed a different title/story")
    inputs = delivery.get("subtitle_inputs") or []
    names = {Path(i.get("path", "")).name for i in inputs}
    require({"reaction.mai.json", "reference.mai.json"}.issubset(names),
            "complete reaction/reference transcriptions missing")
    root = Path(request["title_cover_delivery"]["path"]).parents[2]
    originals = {}
    for entry in inputs:
        path = (root / entry["path"]).resolve()
        require(path.is_relative_to(root), "title/cover input escaped delivery root")
        _sha(path, entry.get("sha256"))
        raw = json.loads(path.read_text())
        require(entry.get("status") == raw.get("status") == "OK" and raw.get("native_segments"),
                "title/cover transcription is not a complete supplied OK response")
        projected = [{k: s[k] for k in ("start_ms", "end_ms", "speaker", "text")} for s in raw["native_segments"]]
        expected = {"source_sha256": entry["sha256"], "segments": projected}
        require(material.get("complete_existing_transcriptions", {}).get(path.with_suffix(".text.json").name) == expected,
                "CPA full transcription material drift")
        originals[path.name] = raw
    quote = delivery.get("quote") or {}
    checks = replay(quote.get("evidence"))
    source = originals.get(checks.get("quote_source"))
    index = checks.get("segment_index")
    require(source and type(index) is int and 1 <= index <= len(source["native_segments"]),
            "quoted source segment missing")
    segment = source["native_segments"][index - 1]
    require(checks.get("title") == title and checks.get("quote") == quote.get("text")
            and quote.get("text") in segment["text"]
            and checks.get("segment") == {k: segment[k] for k in ("text", "speaker", "start_ms", "end_ms")}
            and quote.get("time_ms") == [segment["start_ms"], segment["end_ms"]]
            and checks.get("attribution") == quote.get("speaker") and checks.get("attribution_basis"),
            "stage quote text/time/speaker evidence drift")
    return delivery
