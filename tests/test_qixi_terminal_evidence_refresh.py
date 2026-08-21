from __future__ import annotations

import hashlib

import pytest

from src.autoslice import qixi_terminal_evidence_refresh as refresh
from src.autoslice.boundary_endpoint_binding import (
    bind_final_semantic_endpoint,
    final_delivery_review_matches_srt,
)
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_package_ass_audit import audit_review_package_ass


def _srt(*lines: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(lines, start=1)
    ) + "\n"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _correction(before: str, after: str) -> dict[str, object]:
    return {
        "before_srt_sha256": _sha(before),
        "after_srt_sha256": _sha(after),
        "set_line_operations": ["1=A", "3=C", "6=F", "27=AA"],
    }


def test_refresh_rejects_unlisted_text_and_timing_drift() -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    refresh._assert_text_and_grid_immutable(
        before_srt=before, final_srt=after, correction=_correction(before, after)
    )
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="UNLISTED_TEXT_DRIFT"):
        refresh._assert_text_and_grid_immutable(
            before_srt=before,
            final_srt=after.replace("\n7\n00:00:07,000 --> 00:00:07,900\n7", "\n7\n00:00:07,000 --> 00:00:07,900\nDRIFT"),
            correction=_correction(before, after),
        )
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="TIMING_DRIFT"):
        refresh._assert_text_and_grid_immutable(
            before_srt=before,
            final_srt=after.replace("00:00:02,900", "00:00:02,899", 1),
            correction=_correction(before, after),
        )


def test_refresh_uses_new_final_and_boundary_receipts(monkeypatch) -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    old_chat = {
        "final_text_srt_sha256": _sha(before)[7:],
        "final_review_audit": {"old": "audit"},
    }
    boundary = {"status": "PASS", "request_sha256": "sha256:" + "a" * 64}
    fresh = {
        "schema_version": "final-review-audit.v1",
        "reviewed_srt_sha256": _sha(after),
        "boundary_semantic_review": boundary,
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS"
        },
        "findings": [],
        "validated_finding_count": 0,
    }
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        refresh,
        "exact_delivery_correction_audit",
        lambda **kwargs: seen.setdefault("boundary", kwargs) and {"boundary_semantic_review": boundary},
    )
    monkeypatch.setattr(
        refresh,
        "_run_exact_final_release_review",
        lambda **kwargs: seen.setdefault("final", kwargs) and fresh,
    )
    monkeypatch.setattr(refresh, "validate_final_review_release", lambda audit, **_kwargs: audit)
    result = refresh.refresh_terminal_evidence(
        before_srt=before,
        final_srt=after,
        correction=_correction(before, after),
        old_chat_authority=old_chat,
        selection_hook="hook",
        selection_scorecard={},
        structured_context="context",
        clip_context={},
        source_final_start_ms=0,
        source_final_end_ms=27_900,
        boundary_max_forward_ms=30_000,
        adapters=object(),
        authoritative_chat=(),
        final_review_llm=lambda _prompt: '{"findings":[]}',
        boundary_review_llm=lambda _prompt: '{"decision":"PASS"}',
    )
    assert seen["boundary"]["final_srt_text"] == after
    assert seen["final"]["correction_audit"]["boundary_semantic_review"] == boundary
    assert result["chat_authority"]["final_text_srt_sha256"] == _sha(after)[7:]
    assert result["chat_authority"]["final_review_audit"] is fresh


def test_projection_dry_run_is_target_write_free(tmp_path) -> None:
    paths = {}
    before = {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role] = path
        before[role] = payload
    authority = {
        "authority_sha256": "sha256:" + "a" * 64,
        "preimage": {
            role: {
                "path": str(path),
                "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "bytes": len(before[role]),
                "mode": 0o644,
            }
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    result = refresh.apply_projection(
        authority=authority, before=before, after=after, apply=False
    )
    assert result["status"] == "DRY_RUN_PASS"
    assert {role: path.read_bytes() for role, path in paths.items()} == before
    assert not (tmp_path / "qixi-terminal-evidence-refresh").exists()


def test_projection_apply_is_cas_and_create_only(tmp_path) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "b" * 64,
        "preimage": {
            role: {
                "path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "bytes": len(before[role]), "mode": 0o644,
            }
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    result = refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert result["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="TARGET_DRIFT"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)


def test_projection_rolls_back_prior_targets_on_install_failure(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "c" * 64,
        "preimage": {
            role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._replace_exact
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise refresh.QixiTerminalEvidenceRefreshError("injected")
        return original(*args, **kwargs)

    monkeypatch.setattr(refresh, "_replace_exact", fail_second)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="injected"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert {role: path.read_bytes() for role, path in paths.items()} == before


def test_refreshed_boundary_receipt_matches_the_exact_final_grid(tmp_path) -> None:
    srt_path = tmp_path / "final.srt"
    text = _srt("第一句", "终句")
    srt_path.write_text(text, encoding="utf-8")
    cues = parse_srt_cues(text)
    review, reasons = bind_final_semantic_endpoint(
        semantic_review={
            "status": "PASS", "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": cue_grid_sha256(cues),
            "recommended_end_cue_index": 2, "recommended_end_ms": 2_900,
        },
        cues=cues, closure_cue=cues[-1], snapped_end_ms=2_900, final_start_ms=0, final_end_ms=2_900,
    )
    assert reasons == []
    assert final_delivery_review_matches_srt(review, srt_path)
    srt_path.write_text(text.replace("终句", "漂移"), encoding="utf-8")
    assert not final_delivery_review_matches_srt(review, srt_path)


def test_refreshed_chat_hash_clears_uniform_host_speaker_hash_block(tmp_path) -> None:
    srt = tmp_path / "final.srt"
    srt.write_text(_srt("第一句", "终句"), encoding="utf-8")
    ass = tmp_path / "final.ass"
    ass.write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:01.90,Default,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:02.00,0:00:02.90,Default,,0,0,0,,终句\n",
        encoding="utf-8",
    )
    srt_hash = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    item = {"ass_path": ass.name, "ass_sha256": "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest(), "speaker_srt": srt.name, "speaker_srt_sha256": srt_hash}
    record = {"artifact_hashes": {"ass_sha256": item["ass_sha256"]}}
    stale = {"speaker_ass_path": None, "speaker_ass_sha256": None, "final_text_srt_sha256": "0" * 64, "final_speaker_srt_sha256": "0" * 64}
    assert "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH" in {
        issue.code for issue in audit_review_package_ass(root=tmp_path, item=item, portable_required=True, max_visual_lines=2, max_visual_line_chars=28, record=record, chat_authority=stale).issues
    }
    fresh = dict(stale, final_text_srt_sha256=srt_hash[7:], final_speaker_srt_sha256=srt_hash[7:])
    assert "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH" not in {
        issue.code for issue in audit_review_package_ass(root=tmp_path, item=item, portable_required=True, max_visual_lines=2, max_visual_line_chars=28, record=record, chat_authority=fresh).issues
    }
