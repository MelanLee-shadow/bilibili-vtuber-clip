import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.autoslice.exact_final_cpa_history import (
    NESTED_CAPTION_SUCCESSORS_KEY,
    build_run_audit,
    caption_successors,
    nested_caption_successors,
    with_nested_caption_successor,
)
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    _validate_exact_final_cpa_self_heal,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.nested_media_caption_dedup import POLICY, SCHEMA
from src.autoslice.producer_nested_caption import consume_nested_caption_policy
from src.autoslice import producer_package_finalization as finalization
from src.autoslice.producer_package_finalization import _run_exact_final_review_gate, _resume_source_extended_review
from tests.test_producer_current_text_continuation import (
    _caption_successor_clock,
    _valid_authority,
)
from tests.test_source_extension_final_review import case as extended_source_case


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def input_case(tmp_path, current=None):
    if current is None:
        chat, _, transcript = _caption_successor_clock(tmp_path)
    else:
        chat, transcript = current
    cue = parse_srt_cues(transcript)[0]
    source, frame = tmp_path / "unburned.mp4", tmp_path / "source-frame.jpg"
    source.write_bytes(b"independent source media")
    frame.write_bytes(b"independent source frame")
    duration = parse_srt_cues(transcript)[-1].end_ms
    evidence = {
        "schema_version": SCHEMA, "candidate_id": "watched-clip",
        "source_role": "WATCHED_MEDIA", "source_media_sha256": sha(source.read_bytes()),
        "source_interval_ms": [0, duration], "policy": POLICY,
        "input_transcript_sha256": sha(transcript.encode()),
        "observations": [{
            "observation_id": "current-caption", "cue_index": 1,
            "frame_ms": cue.start_ms, "visible_text": cue.text,
            "role": "WATCHED_MEDIA_CAPTION", "pipeline_burned_subtitle": False,
            "frame_path": str(frame), "frame_sha256": sha(frame.read_bytes()),
        }],
    }
    path = tmp_path / "caption-evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False))
    config = {"policy": POLICY, "evidence_path": str(path), "evidence_sha256": sha(path.read_bytes())}
    subtitle = tmp_path / "display.srt"
    subtitle.write_text(transcript)
    return chat, transcript, source, frame, path, config, duration, subtitle


def consume(tmp_path, case):
    chat, _, source, _, _, config, duration, subtitle = case
    return consume_nested_caption_policy(
        spec={"watched_media": True, "nested_media_caption_dedup": config},
        candidate_id="watched-clip", source_media=source,
        source_start_ms=0, source_end_ms=duration, subtitle_path=subtitle,
        evidence_root=tmp_path, chat_authority_audit=chat,
    )


def test_normal_visual_filter_retains_typed_history_and_requires_new_review(tmp_path):
    case = input_case(tmp_path)
    chat, before, _, _, _, _, _, _ = case
    original = copy.deepcopy(chat["exact_final_cpa_self_heal"])
    after = consume(tmp_path, case)
    receipt = chat["exact_final_cpa_self_heal"]
    assert receipt["passes"] == original["passes"]
    assert receipt["caption_display_successors"] == original["caption_display_successors"]
    assert receipt["status"] == "REVIEW_PENDING"
    assert len(parse_srt_cues(after)) == len(parse_srt_cues(before)) - 1
    assert [(c.start_ms, c.end_ms, c.text) for c in parse_srt_cues(after)] == [
        (c.start_ms, c.end_ms, c.text) for c in parse_srt_cues(before)[1:]
    ]
    assert receipt[NESTED_CAPTION_SUCCESSORS_KEY][0]["release_gate"] == "BLOCK"
    _validate_exact_final_cpa_self_heal(
        chat, expected_srt_sha256="sha256:" + sha(after.encode()), allow_pending=True,
    )
    with pytest.raises(FinalReviewContractError, match="AUDIT_INVALID"):
        _validate_exact_final_cpa_self_heal(chat, expected_srt_sha256="sha256:" + sha(after.encode()))
    pending = build_run_audit(
        receipt["passes"], [], caption_display_successors=receipt["caption_display_successors"],
        nested_caption_display_successors=receipt[NESTED_CAPTION_SUCCESSORS_KEY],
    )
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": pending}, expected_srt_sha256="sha256:" + sha(after.encode()),
        allow_pending=True,
    )


@pytest.mark.parametrize("drift", ["source", "frame", "evidence", "output", "duplicate", "release"])
def test_visual_successor_cannot_mask_drift_or_grant_release(tmp_path, drift):
    case = input_case(tmp_path)
    chat, _, source, frame, evidence, _, _, _ = case
    after = consume(tmp_path, case)
    row = chat["exact_final_cpa_self_heal"][NESTED_CAPTION_SUCCESSORS_KEY][0]
    if drift in {"source", "frame", "evidence"}:
        {"source": source, "frame": frame, "evidence": evidence}[drift].write_bytes(b"changed")
    elif drift == "output":
        row["output_srt_sha256"] = "sha256:" + sha(after.replace("谢", "错").encode())
    elif drift == "duplicate":
        chat["exact_final_cpa_self_heal"][NESTED_CAPTION_SUCCESSORS_KEY].append(copy.deepcopy(row))
    else:
        row["release_gate"] = "PASS"
    with pytest.raises(FinalReviewContractError):
        _validate_exact_final_cpa_self_heal(
            chat, expected_srt_sha256="sha256:" + sha(after.encode()), allow_pending=True,
        )


def test_visual_receipt_cannot_authorize_word_rewrite(tmp_path):
    case = input_case(tmp_path)
    chat, before, source, _, _, config, duration, _ = case
    original = copy.deepcopy(chat["exact_final_cpa_self_heal"])
    after = consume(tmp_path, case)
    with pytest.raises(ValueError, match="NESTED_CAPTION_SUCCESSOR_INVALID"):
        with_nested_caption_successor(
            original, before=before, after=after + "invented text",
            config=config, candidate_id="watched-clip", source_media=source,
            source_start_ms=0, source_end_ms=duration, evidence_root=tmp_path,
        )


def test_exact_gate_consumes_filtered_current_bytes_without_skipping_review(tmp_path):
    case = input_case(tmp_path)
    chat, _, _, _, _, _, duration, subtitle = case
    after = consume(tmp_path, case)

    def current_reviewer(srt_text, authority, start, end):
        assert subtitle.read_text() == after
        assert srt_text == after
        raise RuntimeError("NORMAL_CURRENT_REVIEW_REACHED")

    with pytest.raises(RuntimeError, match="NORMAL_CURRENT_REVIEW_REACHED"):
        _run_exact_final_review_gate(
            cid="watched-clip", out_root=tmp_path, final_start=0, final_end=duration,
            recut=SimpleNamespace(subtitle_path=subtitle, redelivery_baseline_audit=None,
                                  redelivery_baseline_audit_path=None),
            chat_authority_audit=chat, chat_authority_path=tmp_path / "authority.json",
            run_exact_final_review=current_reviewer,
        )
    assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"


def test_normal_gate_separates_completed_successors_from_new_typed_run(tmp_path):
    """A completed display run is historical; a pending run keeps its rows live."""
    case = input_case(tmp_path)
    chat, _, _, _, _, _, duration, subtitle = case
    filtered = consume(tmp_path, case)
    subtitle.write_text(filtered, encoding="utf-8")

    pending_after_display = chat["exact_final_cpa_self_heal"]
    old_caption_rows = copy.deepcopy(
        pending_after_display["caption_display_successors"]
    )
    old_nested_rows = copy.deepcopy(
        pending_after_display[NESTED_CAPTION_SUCCESSORS_KEY]
    )
    completed = build_run_audit(
        copy.deepcopy(pending_after_display["passes"]), [],
        final_srt_sha256="sha256:" + sha(filtered.encode()),
        caption_display_successors=old_caption_rows,
        nested_caption_display_successors=old_nested_rows,
    )
    chat["exact_final_cpa_self_heal"] = completed
    _validate_exact_final_cpa_self_heal(
        chat,
        expected_srt_sha256="sha256:" + sha(filtered.encode()),
    )

    cues = parse_srt_cues(filtered)
    original_cue = cues[0]
    repaired_text = original_cue.text + "（typed repair）"
    calls: list[str] = []

    review_template = _valid_authority(tmp_path)[0]["final_review_audit"]

    def review(text, *_args):
        calls.append(text)
        flagged = len(calls) == 1
        audit = copy.deepcopy(review_template)
        audit.pop("exact_final_cpa_self_heal", None)
        audit.update(
            reviewed_srt_sha256="sha256:" + sha(text.encode()),
            findings=[], validated_finding_count=0,
            status="CLEAN", release_gate="PASS", reason_codes=[],
            discovery={"status": "COMPLETE", "explicit_empty_findings": not flagged},
        )
        if not flagged:
            return audit
        request_payload = {
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "base_text_sha256": sha(original_cue.text.encode()),
            "current_cue": original_cue.text,
            "proposed_cue": repaired_text,
            "matched_start_ms": original_cue.start_ms,
            "matched_end_ms": original_cue.end_ms,
        }
        request = {
            **request_payload,
            "request_sha256": sha(
                json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        }
        audit.update(
            status="FLAGGED",
            release_gate="BLOCK",
            reason_codes=["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            discovery={"status": "COMPLETE", "explicit_empty_findings": False},
            findings=[
                {
                    "cue_index": int(original_cue.index),
                    "base_text_sha256": sha(original_cue.text.encode()),
                    "proposed_full_cue": repaired_text,
                    "exact_release_adjudication": {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "OBSERVED",
                        "decision_authority": "CPA_JUDGE",
                        "repaired": True,
                        "timing_immutable": True,
                        "mutation_authority": {
                            "schema_version": "subtitle-correction-mutation-authority.v1",
                            "status": "PASS",
                        },
                        "request": request,
                        "witness_judge": {
                            "judge": {
                                "status": "JUDGED",
                                "choice": "PROPOSED",
                            }
                        },
                    },
                }
            ],
            validated_finding_count=1,
        )
        return audit

    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    result = _run_exact_final_review_gate(
        cid="new-run",
        out_root=tmp_path,
        final_start=0,
        final_end=duration,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=media,
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
        ),
        chat_authority_audit=chat,
        chat_authority_path=tmp_path / "new-run.chat-authority.json",
        run_exact_final_review=review,
    )

    assert len(calls) == 2
    assert result["status"] == "CLEAN"
    current_receipt = chat["exact_final_cpa_self_heal"]
    assert current_receipt["status"] == "PASS"
    assert current_receipt["passes"][0]["pass_index"] == 1
    assert current_receipt.get("caption_display_successors", []) == []
    assert current_receipt.get(NESTED_CAPTION_SUCCESSORS_KEY, []) == []
    history = current_receipt["retained_previous_runs"]
    assert len(history) == 1
    historical = history[0]["audit"]
    assert historical["caption_display_successors"] == old_caption_rows
    assert historical[NESTED_CAPTION_SUCCESSORS_KEY] == old_nested_rows
    assert caption_successors(historical)
    assert nested_caption_successors(historical)
    assert {
        row["after_pass_index"]
        for row in historical["caption_display_successors"]
        + historical[NESTED_CAPTION_SUCCESSORS_KEY]
    } == {1}

    # A genuinely pending run owns its own successor rows, so the same normal
    # gate must carry them into the resumed current run and validate the chain.
    pending_chat = {
        "exact_final_cpa_self_heal": copy.deepcopy(completed),
    }
    pending_chat["exact_final_cpa_self_heal"]["status"] = "REVIEW_PENDING"
    pending_chat["exact_final_cpa_self_heal"].pop("final_srt_sha256")
    pending_subtitle = tmp_path / "pending.recut.srt"
    pending_subtitle.write_text(filtered, encoding="utf-8")
    pending_result = _run_exact_final_review_gate(
        cid="pending-run",
        out_root=tmp_path,
        final_start=0,
        final_end=duration,
        recut=finalization.CurrentTextReviewArtifacts(
            subtitle_path=pending_subtitle,
        ),
        chat_authority_audit=pending_chat,
        chat_authority_path=tmp_path / "pending-run.chat-authority.json",
        run_exact_final_review=review,
    )
    assert pending_result["status"] == "CLEAN"
    pending_receipt = pending_chat["exact_final_cpa_self_heal"]
    assert pending_receipt["status"] == "PASS"
    assert pending_receipt["caption_display_successors"] == old_caption_rows
    assert pending_receipt[NESTED_CAPTION_SUCCESSORS_KEY] == old_nested_rows


def test_visual_filter_after_normal_source_extension_keeps_both_receipts(tmp_path):
    chat, _, expanded, _, _ = extended_source_case(tmp_path)
    chat["exact_final_cpa_self_heal"] = _resume_source_extended_review(
        cid="candidate", out_root=tmp_path, final_text=expanded, chat_authority_audit=chat,
    )
    previous = copy.deepcopy(chat["exact_final_cpa_self_heal"])
    case = input_case(tmp_path, (chat, expanded))
    after = consume(tmp_path, case)
    assert chat["exact_final_cpa_self_heal"]["source_extension_successors"] == previous["source_extension_successors"]
    _validate_exact_final_cpa_self_heal(
        chat, expected_srt_sha256="sha256:" + sha(after.encode()), allow_pending=True,
    )
