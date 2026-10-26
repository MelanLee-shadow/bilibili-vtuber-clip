"""WP-A: ordinary writers, cold consumers, size growth and rollback boundaries."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import review_evidence_store as evidence


def test_envelope_and_review_share_one_lossless_representation(tmp_path):
    shared = {"request": {"whole_clip_current_srt": "真实文本\n" * 4_000},
              "raw_completion": "原回答", "status": "UNKNOWN"}
    review = {"schema_version": "final-review-audit.v2", "findings": [
        {"cue_index": 1, "exact_release_adjudication": {
            "stopped_adjudication": shared, "rebuilt_finding": shared,
            "terminal_question_originals": ["a" * 64, "b" * 64]}}]}
    original = {"final_review_audit": review,
                "span_obligation_closures": [{"obligation_id": "one", "adjudication": shared}],
                "passes": [{"prior": shared}] * 4, "unrelated_owner": {"enabled": False}}
    before = deepcopy(original)
    path = tmp_path / "authority.json"
    packed = evidence.persist_document(path, original)
    assert evidence.load_review(path) == original == before
    assert evidence.expand(packed) == original
    assert packed["unrelated_owner"] == original["unrelated_owner"]
    assert evidence.is_reference(packed["span_obligation_closures"][0]["adjudication"])
    adjudication = packed["final_review_audit"]["findings"][0]["exact_release_adjudication"]
    assert evidence.is_reference(adjudication["stopped_adjudication"])
    assert evidence.is_reference(adjudication["rebuilt_finding"])
    assert path.stat().st_size < 10_000
    legacy = tmp_path / "inline.json"
    legacy.write_text(json.dumps(original, ensure_ascii=False))
    wire = legacy.read_bytes()
    assert evidence.load_review(legacy) == original
    assert legacy.read_bytes() == wire  # Reading never migrates an in-flight file.


@pytest.mark.parametrize("damage,code", [
    ("invalid_sha", "REVIEW_EVIDENCE_REFERENCE_INVALID"),
    ("wrong_bytes", "ARTIFACT_SHA_MISMATCH"),
    ("missing_blob", "REVIEW_EVIDENCE_BLOB_MISSING"),
])
def test_reference_failures_are_independent(tmp_path, damage, code):
    packed = evidence.persist_document(tmp_path / "audit.json", {"request": {"raw": "retained"}})
    ref = packed["request"]
    if damage == "invalid_sha":
        ref["sha256"] = "not-a-sha"
    elif damage == "wrong_bytes":
        Path(ref["path"]).write_bytes(b'{}')
    else:
        Path(ref["path"]).unlink()
    with pytest.raises(ValueError, match=code):
        evidence.expand(packed)


def test_republished_references_have_a_complete_colocated_blob_closure(tmp_path):
    original = {"request": {"raw": "保存的真实原答" * 2_000}, "status": "UNKNOWN"}
    source = tmp_path / "source" / "authority.json"
    packed = evidence.persist_document(source, original)
    target = tmp_path / "successor" / "authority.json"
    current_request = {"new_material": True}
    evidence.persist_document(target, {"history": packed, "request": current_request})
    # The successor owns its whole closure. Keeping an absolute old reference
    # while adding a new local blob breaks load_review's relocation contract.
    source.with_name("evidence-store").rename(source.with_name("retired-store"))
    assert evidence.load_review(target) == {"history": original, "request": current_request}


def test_no_transaction_can_inline_a_review_or_chat_authority():
    from src.autoslice import producer_package_finalization as producer
    tree = ast.parse(Path(producer.__file__).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ("_apply_exact_caption_display_canon", "_run_exact_final_review_gate_body"):
        calls = [node for node in ast.walk(functions[name]) if isinstance(node, ast.Call)]
        assert not any(isinstance(call.func, ast.Name) and call.func.id == "_json_bytes"
                       for call in calls), name
        assert any(isinstance(call.func, ast.Name) and call.func.id == "_persist_review_document"
                   for call in calls), name
    # Representation selection belongs to the common store, not domain modules.
    for name in ("exact_final_terminal_funnel.py", "span_finding_obligations.py"):
        domain = ast.parse(Path(producer.__file__).with_name(name).read_text())
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and isinstance(node.func.value, ast.Name) and node.func.value.id == "json"
                       and node.func.attr in {"dump", "dumps"} for node in ast.walk(domain))


def _patch_run(tmp_path, monkeypatch, *, count=4, evidence_payload=None):
    from src.autoslice import producer_package_finalization as producer
    from tests import test_continuation_authority_class as fixture
    subtitle = tmp_path / "current.srt"
    words = [f"这句保留原意第{i}次" for i in range(count + 1)]
    subtitle.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{words[0]}\n\n"
                        "2\n00:00:01,100 --> 00:00:02,000\n完整收束\n")
    chat_path, review_path, baseline_path = [tmp_path / name for name in
        ("candidate.chat-authority.json", "candidate.review-flags.json", "baseline.json")]
    for path in (chat_path, review_path, baseline_path):
        path.write_bytes(b'{"previous":"exact old bytes"}\n')
    authority = {"sentinel": {"status": "UNKNOWN"}}
    if evidence_payload is not None:
        authority["retained_evidence"] = evidence_payload
    baseline = {"status": "APPLIED"}
    rounds = []
    def review(text, *_args):
        index = next(i for i, word in enumerate(words) if word in text)
        rounds.append(index)
        with monkeypatch.context() as context:
            context.setattr(fixture, "ORIGINAL", words[index] if index < count else "absent")
            result = fixture._review(text, words[min(index + 1, count)])
        if evidence_payload is not None:
            result["retained_evidence"] = evidence_payload
        return result
    result = producer._run_exact_final_review_gate(
        cid="candidate", out_root=tmp_path, final_start=0, final_end=2_400,
        recut=producer.FinalRecutArtifacts(
            recut_dir=tmp_path, media_path=tmp_path / "unused.mp4", subtitle_path=subtitle,
            text_manifest_path=None, text_manifest=None,
            redelivery_baseline_audit_path=baseline_path, redelivery_baseline_audit=baseline),
        chat_authority_audit=authority, chat_authority_path=chat_path,
        run_exact_final_review=review)
    return result, authority, rounds, (subtitle, chat_path, review_path, baseline_path)


def test_four_real_patch_transactions_grow_linearly_and_cold_validate(tmp_path, monkeypatch, record_property):
    from src.autoslice import producer_package_finalization as producer
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    writes = {}
    original = producer._persist_review_document
    def capture(path, document):
        packed = original(path, document)
        receipt = document.get("exact_final_cpa_self_heal", {})
        if Path(path).name == "candidate.chat-authority.json" and receipt.get("status") == "REVIEW_PENDING":
            n = len(receipt["passes"])
            assert evidence.load_review(path) == document
            writes[n] = Path(path).stat().st_size
        return packed
    monkeypatch.setattr(producer, "_persist_review_document", capture)
    payload = {"request": {"raw": "read-only historical evidence" * 10_000}}
    result, authority, rounds, paths = _patch_run(tmp_path, monkeypatch, evidence_payload=payload)
    assert rounds == [0, 1, 2, 3, 4]
    assert sorted(writes) == [1, 2, 3, 4]
    assert result["release_gate"] == "PASS"
    assert len(authority["exact_final_cpa_self_heal"]["passes"]) == 4
    _validate_exact_final_cpa_self_heal(evidence.load_review(paths[1]),
        expected_srt_sha256="sha256:" + hashlib.sha256(paths[0].read_bytes()).hexdigest())
    assert max(writes.values()) <= 10_000_000
    assert all(writes[n] <= n * writes[1] for n in range(2, 5))
    record_property("wpa_chat_authority_bytes", json.dumps(writes, sort_keys=True))


@pytest.mark.parametrize("failure", ["chat", "baseline", "subtitle"])
def test_transaction_publication_failure_restores_all_old_bytes(tmp_path, monkeypatch, failure):
    from src.autoslice import producer_package_finalization as producer
    persist, write = producer._persist_review_document, producer._write_bytes_atomic
    original_srt = None
    def fail_document(path, document):
        result = persist(path, document)
        if ((failure == "chat" and Path(path).name == "candidate.chat-authority.json")
                or (failure == "baseline" and Path(path).name == "baseline.json")):
            raise OSError("injected after document publication")
        return result
    def fail_subtitle(path, payload):
        nonlocal original_srt
        if Path(path).name == "current.srt":
            original_srt = Path(path).read_bytes()
            write(path, payload)
            raise OSError("injected after subtitle publication")
        return write(path, payload)
    monkeypatch.setattr(producer, "_persist_review_document", fail_document)
    if failure == "subtitle":
        monkeypatch.setattr(producer, "_write_bytes_atomic", fail_subtitle)
    with pytest.raises(OSError, match="injected after"):
        _patch_run(tmp_path, monkeypatch, count=1)
    for name in ("candidate.chat-authority.json", "candidate.review-flags.json", "baseline.json"):
        assert (tmp_path / name).read_bytes() == b'{"previous":"exact old bytes"}\n'
    assert "第0次" in (tmp_path / "current.srt").read_text()
    if original_srt is not None:
        assert (tmp_path / "current.srt").read_bytes() == original_srt


def test_real_group_projection_stores_original_digests_and_cold_keeps(tmp_path, monkeypatch):
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    from tests.test_terminal_contract_roundtrip import produce, c_reply
    from src.autoslice.exact_final_terminal_funnel import terminalize_review, review_pass_receipt
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.context_assisted_audio import digest
    rig = build_offline_rig(tmp_path / "rig", monkeypatch)
    _, captured = produce(rig, c_reply)
    finding = captured["request"]["audio_gain_termination"]["stopped_finding"]
    other = {**deepcopy(finding), "why": "同题的独立提名"}
    audit = {"findings": [finding, other], "unresolved_findings_disclosed": [],
             "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]}
    before = deepcopy(audit)
    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt, audit=audit,
                                  repaired_text=rig.srt, repairs=[])
    calls = []
    def reply(prompt):
        calls.append(prompt)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
                               if line.startswith('可引用编号')))
        return json.dumps(c_reply(data, refs), ensure_ascii=False)
    result = terminalize_review(rig.srt, audit, pass_receipts=[receipt], adapters={
        "clip_context": rig.context, "source_media_timeline_offset_ms": rig.offset_ms,
        "prior_range_acoustic_observations": captured["request"]["check_request"].get(
            "context_only_range_acoustic_observations", []), "judge_llm_call": reply})
    assert len(calls) == 1 and audit == before
    rows = result["unresolved_findings_disclosed"]
    assert len(rows) == 2
    for row in rows:
        assert row["exact_release_adjudication"]["terminal_question_originals"] == [digest(finding), digest(other)]
        assert is_release_resolved_keep(row)
    packed = evidence.persist_document(tmp_path / "group.json", result)
    assert evidence.load_review(tmp_path / "group.json") == result
    assert all(is_release_resolved_keep(row) for row in packed["unresolved_findings_disclosed"])
