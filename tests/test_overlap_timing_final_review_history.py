import copy
import hashlib

import pytest

from src.autoslice.exact_final_cpa_history import (
    OVERLAP_TIMING_SUCCESSORS_KEY, build_run_audit, overlap_timing_successors,
    with_overlap_timing_successor,
)
from src.autoslice.final_review_contract import (
    FinalReviewContractError, _validate_exact_final_cpa_self_heal,
)
from src.autoslice.legacy_final_surface_supersession import (
    _normal_history_display_replays, _normal_history_display_retires_decision_row,
)
from tests.test_producer_text_finalization import _normal_history_display_case, _srt, _typed_pass


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def case():
    old, owner, _, before, after = _normal_history_display_case(path="overlap")
    before_text = _srt((1000, 2000, before), (1920, 3100, "小室回应"))
    current = _srt((1000, 2000, after), (1920, 3100, "小室回应"))
    final = _srt((1000, 1920, after), (1920, 3100, "小室回应"))
    receipt = build_run_audit([_typed_pass(
        pass_index=1, input_text=before_text, output_text=current,
        repairs=old["passes"][0]["repairs"],
    )], [], final_srt_sha256=digest(current))
    return receipt, owner, current, final


def test_overlap_clamp_keeps_typed_authority_but_cannot_inherit_release():
    old, _, current, final = case()
    baseline = copy.deepcopy(old)
    pending = with_overlap_timing_successor(old, current, final)
    assert old == baseline
    assert pending["passes"] == old["passes"]
    assert pending["status"] == "REVIEW_PENDING"
    assert pending[OVERLAP_TIMING_SUCCESSORS_KEY][0]["end_clamps"] == [
        {"cue_index": "1", "start_ms": 1000, "before_end_ms": 2000, "after_end_ms": 1920},
    ]
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": pending}, expected_srt_sha256=digest(final), allow_pending=True,
    )
    with pytest.raises(FinalReviewContractError, match="AUDIT_INVALID"):
        _validate_exact_final_cpa_self_heal(
            {"exact_final_cpa_self_heal": pending}, expected_srt_sha256=digest(final),
        )


@pytest.mark.parametrize("drift", ["text", "start", "arbitrary_end", "tail", "drop", "wrong_before"])
def test_overlap_successor_rejects_unrelated_changes(drift):
    old, _, current, final = case()
    if drift == "text":
        final = final.replace("小室", "小四")
    elif drift == "start":
        final = final.replace("00:00:01,000", "00:00:01,010")
    elif drift == "arbitrary_end":
        final = final.replace("00:00:01,920\n新文本", "00:00:01,900\n新文本")
    elif drift == "tail":
        final = final.replace("00:00:03,100", "00:00:03,090")
    elif drift == "drop":
        final = final.split("\n\n")[0] + "\n"
    else:
        current = current.replace("新文本", "无关文本")
    with pytest.raises((ValueError, FinalReviewContractError)):
        with_overlap_timing_successor(old, current, final)


@pytest.mark.parametrize("drift", ["rehashed_end", "pass_index", "claim_release"])
def test_registered_overlap_edge_replays_instead_of_trusting_fields(drift):
    old, _, current, final = case()
    pending = with_overlap_timing_successor(old, current, final)
    edge = pending[OVERLAP_TIMING_SUCCESSORS_KEY][0]
    if drift == "rehashed_end":
        edge["output_srt_text"] = final.replace("00:00:03,100", "00:00:03,090")
        edge["output_srt_sha256"] = digest(edge["output_srt_text"])
    elif drift == "pass_index":
        edge["after_pass_index"] = 2
    else:
        edge["release_gate"] = "PASS"
    with pytest.raises(ValueError):
        overlap_timing_successors(pending)


def test_current_clean_owner_replays_actual_timing_edge_and_speaker_bytes():
    old, owner, current, final = case()
    pending = with_overlap_timing_successor(old, current, final)
    sealed = build_run_audit(pending["passes"], [], final_srt_sha256=digest(final),
        overlap_timing_successors=pending[OVERLAP_TIMING_SUCCESSORS_KEY])
    audit = {"exact_final_cpa_self_heal": sealed, "final_review_audit": {
        "status": "CLEAN", "release_gate": "PASS", "reviewed_srt_sha256": digest(final),
    }}
    replays = _normal_history_display_replays(audit, final_text_srt=final)
    assert replays is not None
    assert _normal_history_display_retires_decision_row(
        owner, expected_text="新文本", audit=audit,
        final_text_srt=final, final_speaker_srt=final, delivery_start_ms=0, replays=replays,
    )
    reconciliation = owner["normal_history_display_reconciliation"]
    assert reconciliation["display_path"] == "OVERLAP_TIMING_ONLY"
    assert reconciliation["matched_end_ms"] == 2000
    assert reconciliation["final_owner_window_ms"] == [1000, 1920]
    assert reconciliation["timing_immutable"] is False
    assert not _normal_history_display_retires_decision_row(
        owner, expected_text="新文本", audit=audit,
        final_text_srt=final, final_speaker_srt=final.replace("新文本", "错误"),
        delivery_start_ms=0, replays=replays,
    )


def test_normal_gate_reviews_pending_overlap_bytes_before_any_release(tmp_path):
    from types import SimpleNamespace
    from src.autoslice.producer_package_finalization import _run_exact_final_review_gate
    old, _, current, final = case()
    pending = with_overlap_timing_successor(old, current, final)
    subtitle = tmp_path / "current.srt"
    subtitle.write_text(final)
    chat = {"exact_final_cpa_self_heal": pending}
    def reviewer(text, authority, start, end):
        assert text == final
        assert authority["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"
        assert authority["exact_final_cpa_self_heal"]["passes"] == old["passes"]
        raise RuntimeError("CURRENT_COMPLETE_REVIEW_REACHED")
    with pytest.raises(RuntimeError, match="CURRENT_COMPLETE_REVIEW_REACHED"):
        _run_exact_final_review_gate(
            cid="clip", out_root=tmp_path, final_start=0, final_end=3100,
            recut=SimpleNamespace(subtitle_path=subtitle, redelivery_baseline_audit=None,
                                  redelivery_baseline_audit_path=None),
            chat_authority_audit=chat, chat_authority_path=tmp_path / "authority.json",
            run_exact_final_review=reviewer,
        )
    assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"


@pytest.mark.parametrize("matching_input", [True, False])
@pytest.mark.parametrize("status", ["PASS", "REVIEW_PENDING"])
def test_normal_materializer_attaches_only_the_actual_terminal_timing_successor(
    tmp_path, matching_input, status,
):
    from src.autoslice.producer_package_finalization import (
        ProducerFinalizationAdapters, _materialize_final_recut,
    )
    old, _, current, final = case()
    if status == "REVIEW_PENDING":
        old["status"] = status
        old.pop("final_srt_sha256")
    chat = {"exact_final_cpa_self_heal": copy.deepcopy(old)}
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"source")
    provenance = tmp_path / "padded.provenance.json"
    provenance.write_text("{}\n")
    out = tmp_path / "out"
    out.mkdir()

    def recut(command, **_kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b"recut")

    def write_srt(_cues, _start, _end, output):
        output.write_text(final)

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter was called")

    artifacts = _materialize_final_recut(
        spec={"pieces": [{"start_ms": 0, "end_ms": 3100}]},
        cid="clip", out_root=out, padded=padded, padded_provenance_path=provenance,
        piece_provenance_rows=[], final_start=0, final_end=3100, sanitized=[],
        timing_qa={"current_delivery_input_srt_text": (
            current if matching_input else current.replace("小室", "小四")
        )},
        text_override_path=None, chat_authority_audit=chat,
        adapters=ProducerFinalizationAdapters(
            accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
            run_command=recut, write_source_range_srt=write_srt,
            apply_text_override_document=unused, run_speaker_finalization=unused,
            burn_preview_subtitles=unused, stage_publish_draft=unused,
            generate_upload_tags=unused, delivery_root=lambda: tmp_path / "delivery",
        ),
    )
    assert artifacts.subtitle_path.read_text() == final
    assert chat["exact_final_cpa_self_heal"]["passes"] == old["passes"]
    if matching_input:
        assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"
        _validate_exact_final_cpa_self_heal(
            chat, expected_srt_sha256=digest(final), allow_pending=True,
        )
    else:
        assert chat["exact_final_cpa_self_heal"] == old
        with pytest.raises(FinalReviewContractError, match=(
                "CHAIN_TERMINAL_MISMATCH" if status == "REVIEW_PENDING" else "FINAL_HASH_MISMATCH")):
            _validate_exact_final_cpa_self_heal(chat, expected_srt_sha256=digest(final), allow_pending=True)
