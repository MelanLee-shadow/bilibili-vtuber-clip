from __future__ import annotations

import hashlib
import json

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


def _live_correction(before: str, after: str) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    operations = ["1=A", "3=C", "6=F", "27=AA"]
    burn = {"path": "/runtime/final.mp4", "sha256": "sha256:" + "b" * 64}
    live = {
        "schema_version": "human-subtitle-correction.v2",
        "stage_order": "human_text_then_speaker_then_burn",
        "corrected_at": "2026-08-21T10:38:16.502731+00:00",
        "candidate_id": refresh.CANDIDATE_ID,
        "before_srt_sha256": _sha(before)[7:],
        "after_srt_sha256": _sha(after)[7:],
        "replace_operations": [],
        "set_line_operations": operations,
        "refresh_only": False,
        "text_source": None,
        "text_source_sha256": None,
        "text_override": None,
        "text_override_sha256": None,
        "text_override_manifest": None,
        "text_override_manifest_sha256": None,
        "text_override_decision_output": None,
        "text_override_decision_output_sha256": None,
        "text_override_output": None,
        "text_override_output_sha256": None,
        "timing_source": None,
        "timing_source_sha256": None,
        "speaker_mode": "uniform_host",
        "speaker_manifest": None,
        "speaker_manifest_sha256": None,
        "burned_media": burn["path"],
        "burned_media_sha256": burn["sha256"][7:],
        "delivery_branding_authority": {
            "schema_version": "sealed-subtitle-correction-delivery-authority.v1",
            "authority_path": "/repo/authority.json",
            "authority_sha256": "sha256:" + "c" * 64,
            "authority_repository_seal": {
                "mode": "DEPLOYED_MANIFEST", "deployed_commit": "d" * 40,
                "relative_path": "assets/authority.json", "sha256": "sha256:" + "e" * 64,
            },
            "branding_intro": {
                "intro_id": "intro", "intro_media_sha256": "sha256:" + "f" * 64,
                "intro_offset_ms": 6200, "status": "PREPENDED",
            },
            "record_sha256": "sha256:" + "1" * 64,
            "publish_sha256": "sha256:" + "2" * 64,
            "burned_video_sha256": "sha256:" + "3" * 64,
        },
        "upload_enabled": False,
    }
    authority = {
        "before_srt_sha256": _sha(before), "after_srt_sha256": _sha(after),
        "set_line_operations": operations,
    }
    return live, authority, {"burn": burn}


def test_live_correction_bare_hex_normalizes_only_after_full_live_schema_binding() -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    live, authority, preimage = _live_correction(before, after)
    assert refresh._normalize_live_correction(
        live, authority_correction=authority, before_srt=before, final_srt=after,
        authority_preimage=preimage,
    ) == _correction(before, after)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda value: value.__setitem__("before_srt_sha256", "sha256:" + str(value["before_srt_sha256"])), "CORRECTION_BEFORE_SHA_INVALID"),
        (lambda value: value.__setitem__("after_srt_sha256", "0" * 64), "CORRECTION_HASH_DRIFT"),
        (lambda value: value.__setitem__("replace_operations", ["1=A"]), "CORRECTION_SCHEMA_INVALID"),
        (lambda value: value.__setitem__("set_line_operations", ["1=A", "3=C", "6=F", "27=AA", "7=drift"]), "CORRECTION_HASH_DRIFT"),
    ],
)
def test_live_correction_rejects_prefix_confusion_hash_or_unlisted_operations(mutate, expected) -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    live, authority, preimage = _live_correction(before, after)
    mutate(live)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match=expected):
        refresh._normalize_live_correction(
            live, authority_correction=authority, before_srt=before, final_srt=after,
            authority_preimage=preimage,
        )


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
        clip_context={"context_sha256": "sha256:" + "e" * 64},
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
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "ALREADY_COMMITTED"


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


def test_projection_adopts_owned_stage_after_checkpoint_crash(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "d" * 64,
        "preimage": {role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644} for role, path in paths.items()},
    }
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._checkpoint
    calls = 0
    def crash_after_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("crash after staged owner")
        return original(*args, **kwargs)
    monkeypatch.setattr(refresh, "_checkpoint", crash_after_stage)
    with pytest.raises(KeyboardInterrupt, match="staged owner"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    monkeypatch.setattr(refresh, "_checkpoint", original)
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after


def test_projection_recovers_crash_after_rename_before_install_checkpoint(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "f" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._checkpoint

    def crash_after_rename(*args, **kwargs):
        if kwargs.get("phase") == "INSTALLED":
            raise KeyboardInterrupt("crash after rename")
        return original(*args, **kwargs)

    monkeypatch.setattr(refresh, "_checkpoint", crash_after_rename)
    with pytest.raises(KeyboardInterrupt, match="after rename"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert paths["chat"].read_bytes() == after["chat"]
    monkeypatch.setattr(refresh, "_checkpoint", original)
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after


def test_projection_failure_persists_recovery_journal(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "e" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    monkeypatch.setattr(refresh, "create_staged_inode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stage fail")))
    with pytest.raises(RuntimeError, match="stage fail"):
        refresh.apply_projection(authority=authority, before=before, after={role: b"after" for role in before}, apply=True)
    root = refresh._refresh_root(authority)
    assert __import__("json").loads((root / "journal.json").read_text())["status"] == "ROLLED_BACK"


def test_projection_retains_rollback_required_when_reverse_rollback_fails(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "9" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    monkeypatch.setattr(refresh, "create_staged_inode", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("stage fail")))
    monkeypatch.setattr(refresh, "_rollback_entries", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("rollback fail")))
    with pytest.raises(RuntimeError, match="stage fail"):
        refresh.apply_projection(authority=authority, before=before, after={role: b"after" for role in before}, apply=True)
    journal = json.loads((refresh._refresh_root(authority) / "journal.json").read_text())
    assert journal["status"] == "ROLLBACK_REQUIRED"


@pytest.mark.parametrize("tamper", ["journal", "receipt", "after"])
def test_committed_projection_replay_rejects_tampered_receipt_journal_or_after_bytes(tmp_path, tamper) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "8" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    after = {role: f"after-{role}".encode() for role in before}
    refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    root = refresh._refresh_root(authority)
    if tamper == "journal":
        (root / "journal.json").write_bytes(b"{}")
    elif tamper == "receipt":
        (root / "receipt.json").write_bytes(b"{}")
    else:
        paths["chat"].write_bytes(b"tampered")
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="QIXI_TERMINAL_REFRESH"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)


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
