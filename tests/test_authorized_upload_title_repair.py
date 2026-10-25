"""Offline transaction controls for the source-fact-bound title lane."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.addressee_attribution import build_addressee_evidence
from src.autoslice import authorized_upload_title_repair as title_repair
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_evidence import SourceCue
from src.autoslice.same_bv_repair import normalise_creator_snapshot
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.channel_envelope import render_talk_title_prefix


BVID = "BV1nsHJ65Ecx"
ORIGINAL_TITLE = render_talk_title_prefix() + "这是一个原始标题"
FINAL_TITLE = render_talk_title_prefix() + "游戏对白里的暧昧关系"
SELECTION_HOOK = "游戏对白展示"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path, *, review_origin=None):
    """Create a current-format proposal with a real source-fact receipt."""

    srt = tmp_path / "final.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n游戏对白\n", encoding="utf-8")
    record = {
        "story_contract": {
            "candidate_id": "semantictalk_test_1_2",
            "selection_hook": SELECTION_HOOK,
            "clip_context_prompt": "游戏对白语境",
            "selection_scorecard": {"semantic": 7, "coverage": 1},
        },
        "speaker_mode": "uniform_host",
    }
    record_path = tmp_path / "candidate.record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "title": ORIGINAL_TITLE,
        "authorization": {"by": "维护者", "quote": "按复核结果修正标题"},
        "package_attestation": {
            "record": {"path": str(record_path), "sha256": _sha(record_path)},
            "subtitle": {"path": str(srt), "sha256": _sha(srt)},
        },
    }
    manifest_path = tmp_path / "authorized-upload.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    manifest_sha = _sha(manifest_path)
    record_sha = _sha(record_path)
    srt_sha = _sha(srt)

    cues = [
        SourceCue(int(cue.index), cue.start_ms, cue.end_ms, cue.text)
        for cue in parse_srt_cues(srt.read_text(encoding="utf-8"))
    ]
    final_transcript, speaker_result = build_addressee_evidence(record, cues)
    source_inputs = {
        "final_transcript": final_transcript,
        "clip_context_prompt": "游戏对白语境",
        "selection_scorecard": {"semantic": 7, "coverage": 1},
        "speaker_evidence": speaker_result.speaker_evidence,
        "story_contract": copy.deepcopy(record["story_contract"]),
    }
    title_input = {
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "record_path": str(record_path),
        "record_sha256": record_sha,
        "srt_path": str(srt),
        "srt_sha256": srt_sha,
        "bvid": BVID,
        "candidate_id": "semantictalk_test_1_2",
        "original_title": ORIGINAL_TITLE,
        "original_selection_hook": SELECTION_HOOK,
        "source_fact_inputs": source_inputs,
    }
    title_input_path = tmp_path / "TITLE-INPUT.json"
    title_input_path.write_text(json.dumps(title_input, ensure_ascii=False), encoding="utf-8")

    review_origin = ORIGINAL_TITLE if review_origin is None else review_origin
    responses = [
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "REPAIR",
            "final_selection_hook": SELECTION_HOOK,
            "final_title": FINAL_TITLE,
            "supported_by": ["final_transcript"],
            "changed_surfaces": [
                {
                    "artifact": "title",
                    "before": review_origin,
                    "after": FINAL_TITLE,
                    "reason": "标题准确指出对白内容",
                    "evidence": ["游戏对白"],
                }
            ],
            "summary": "标题改为明确对白",
            "addressee_attribution": [],
        },
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "KEEP",
            "final_selection_hook": SELECTION_HOOK,
            "final_title": FINAL_TITLE,
            "supported_by": ["final_transcript"],
            "changed_surfaces": [],
            "summary": "标题与证据一致",
            "addressee_attribution": [],
        },
    ]

    if review_origin == FINAL_TITLE:
        responses = responses[1:]

    def llm_call(_prompt):
        return json.dumps(responses.pop(0), ensure_ascii=False)

    review = review_and_repair_source_facts(
        selection_hook=SELECTION_HOOK,
        title=review_origin,
        final_transcript=final_transcript,
        clip_context_prompt="游戏对白语境",
        llm_call=llm_call,
        selection_scorecard=source_inputs["selection_scorecard"],
        title_repair_allowed=True,
        speaker_transcript=None,
        speaker_evidence=speaker_result.speaker_evidence,
        candidate_id=record["story_contract"]["candidate_id"],
        final_reviewed_srt_path=srt,
        story_contract=record["story_contract"],
    )
    assert review["status"] == "PASS", review
    title_result = {
        "status": "OK",
        "final_title": FINAL_TITLE,
        "final_selection_hook": SELECTION_HOOK,
        "source_fact_review": review,
        "input_sha256": _sha(title_input_path),
    }
    title_result_path = tmp_path / "TITLE-RESULT.json"
    title_result_path.write_text(json.dumps(title_result, ensure_ascii=False), encoding="utf-8")

    public_proof = {
        "status": "VERIFIED_PUBLIC",
        "bvid": BVID,
        "expected": {"title": ORIGINAL_TITLE, "section_id": 7},
        "section_api": {"episode_match_count": 1},
    }
    public_proof_path = tmp_path / "public-verify.json"
    public_proof_path.write_text(json.dumps(public_proof, ensure_ascii=False), encoding="utf-8")
    uploaded = {
        "status": "VERIFIED_PUBLIC",
        "bvid": BVID,
        "aid": 11,
        "cid": 22,
        "title": ORIGINAL_TITLE,
        "manifest_sha256": manifest_sha,
        "public_verify": str(public_proof_path),
        "public_verify_sha256": _sha(public_proof_path),
    }
    (tmp_path / "authorized-upload.json.uploaded.json").write_text(
        json.dumps(uploaded, ensure_ascii=False), encoding="utf-8"
    )

    raw = {
        "archive": {
            "bvid": BVID,
            "aid": 11,
            "state": 0,
            "title": ORIGINAL_TITLE,
            "tag": "李豆沙,直播切片",
            "desc": "保留描述",
            "cover": "cover",
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "videos": [{"cid": 22, "filename": "original", "title": "P1"}],
    }

    class Session:
        def __init__(self):
            self.raw = copy.deepcopy(raw)
            self.edits = 0
            self.ambiguous = False

        def archive_view(self, _bvid):
            return copy.deepcopy(self.raw)

        def build_edit_payload(self, current, title=None, **_kwargs):
            archive = current["archive"]
            payload = {
                "title": archive["title"],
                "bvid": archive["bvid"],
                "aid": archive["aid"],
                "desc": archive["desc"],
                "tag": archive["tag"],
                "cover": archive["cover"],
                "tid": archive["tid"],
                "copyright": archive["copyright"],
                "source": archive["source"],
                "videos": copy.deepcopy(current["videos"]),
                "csrf": "fixture-csrf",
            }
            if title is not None:
                payload["title"] = title
            return payload

        def edit_archive(self, payload):
            self.edits += 1
            self.raw["archive"]["title"] = payload["title"]
            if self.ambiguous:
                raise TimeoutError("fixture response lost")
            return {"code": 0}

    class Adapter:
        def __init__(self):
            self.session = Session()

        def observe(self, bvid, section_id):
            creator = normalise_creator_snapshot(
                bvid=bvid, creator_data=self.session.raw
            )
            title = self.session.raw["archive"]["title"]
            return {
                "creator": creator,
                "public": {
                    "available": True,
                    "bvid": bvid,
                    "aid": 11,
                    "cid": 22,
                    "state": 0,
                    "metadata": copy.deepcopy(creator["metadata"]),
                },
                "section": {
                    "available": True,
                    "section_id": section_id,
                    "matches": [{"bvid": bvid, "aid": 11, "cid": 22, "title": title}],
                },
            }

        def sync_section_title(self, *_args, **_kwargs):
            raise AssertionError("fixture should converge without section sync")

    adapter = Adapter()
    args = SimpleNamespace(
        manifest=str(manifest_path),
        bvid=BVID,
        title_input=str(title_input_path),
        title_result=str(title_result_path),
        receipt=str(tmp_path / "TITLE-RECEIPT.json"),
        quote="按复核结果修正标题",
        authorized_by="维护者",
        lock=None,
        cookie_json="unused",
        execute=True,
        wait=0,
        poll=0,
    )
    dependencies = {
        "load_and_verify": lambda *_a, **_k: (manifest, []),
        "adapter_factory": lambda _p: adapter,
        "upload_lock": lambda _p: nullcontext(),
        "default_lock": tmp_path / "upload.lock",
    }
    return args, adapter, dependencies, manifest_path


@pytest.mark.parametrize("review_origin", [ORIGINAL_TITLE, FINAL_TITLE])
def test_title_edit_preserves_non_title_fields_and_safe_resume(tmp_path, review_origin):
    args, adapter, dependencies, manifest_path = _fixture(tmp_path, review_origin=review_origin)
    before_manifest = manifest_path.read_bytes()

    code, receipt = title_repair.run(args, **dependencies)
    assert code == 0
    assert receipt["status"] == "VERIFIED_TITLE_ONLY"
    assert adapter.session.edits == 1
    assert adapter.session.raw["archive"]["desc"] == "保留描述"
    assert adapter.session.raw["videos"] == [{"cid": 22, "filename": "original", "title": "P1"}]
    assert manifest_path.read_bytes() == before_manifest

    code, resumed = title_repair.run(args, **dependencies)
    assert code == 0
    assert resumed["status"] == "VERIFIED_TITLE_ONLY"
    assert adapter.session.edits == 1


def test_independently_valid_review_from_unrelated_title_cannot_authorize_edit(tmp_path):
    args, adapter, dependencies, _manifest_path = _fixture(
        tmp_path, review_origin=render_talk_title_prefix() + "另一条没有绑定的标题"
    )
    with pytest.raises(title_repair.TitleRepairError, match="REVIEW_SURFACE_INVALID"):
        title_repair.run(args, **dependencies)
    assert adapter.session.edits == 0


@pytest.mark.parametrize("mutation", [None, "pending", "cid", "tag", "title", "proposal"])
def test_completed_title_receipt_can_be_replayed_by_cover_successor(
    tmp_path, monkeypatch, mutation,
):
    import src.autoslice.new_bv_repair_authority as new_bv

    args, adapter, dependencies, manifest_path = _fixture(tmp_path)
    code, receipt = title_repair.run(args, **dependencies)
    assert code == 0
    assert adapter.session.edits == 1
    authority = {
        "bvid": BVID, "aid": 11, "cid": 22,
        "observed_public_title": ORIGINAL_TITLE, "title": FINAL_TITLE,
    }
    monkeypatch.setattr(new_bv, "validate_new_bv_repair_authority", lambda *_a, **_k: authority)
    monkeypatch.setattr(new_bv, "replay_first_publication_manifest", lambda _a: dependencies["load_and_verify"]()[0])
    receipt_path = tmp_path / "TITLE-RECEIPT.json"
    if mutation == "pending":
        receipt["status"] = "PUBLIC_PENDING"
    elif mutation == "cid":
        receipt["after"]["public"]["cid"] = 23
    elif mutation == "tag":
        receipt["after"]["creator"]["metadata"]["tag"] = "changed"
    elif mutation == "title":
        receipt["after"]["section"]["matches"][0]["title"] = ORIGINAL_TITLE
    elif mutation == "proposal":
        proposal_path = tmp_path / "TITLE-RESULT.json"
        proposal = json.loads(proposal_path.read_text())
        proposal["source_fact_review"]["summary"] = "changed"
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False))
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False))
    reference = {
        key: {"path": str(path), "sha256": _sha(path)}
        for key, path in {
            "receipt": receipt_path, "title_input": tmp_path / "TITLE-INPUT.json",
            "title_result": tmp_path / "TITLE-RESULT.json",
        }.items()
    }
    original_manifest = manifest_path.read_bytes()
    if mutation is None:
        title_repair.validate_title_repair_successor(
            reference, authority=authority, candidate_id="semantictalk_test_1_2", title=FINAL_TITLE
        )
    else:
        with pytest.raises(title_repair.TitleRepairError):
            title_repair.validate_title_repair_successor(
                reference, authority=authority, candidate_id="semantictalk_test_1_2", title=FINAL_TITLE
            )
    assert adapter.session.edits == 1
    assert manifest_path.read_bytes() == original_manifest


def test_ambiguous_title_edit_is_observation_only_on_resume(tmp_path):
    args, adapter, dependencies, _manifest_path = _fixture(tmp_path)
    adapter.session.ambiguous = True

    code, receipt = title_repair.run(args, **dependencies)
    assert code == 6
    assert receipt["status"] == "EDIT_OUTCOME_UNKNOWN"
    assert adapter.session.edits == 1

    code, receipt = title_repair.run(args, **dependencies)
    assert code == 0
    assert receipt["status"] == "VERIFIED_TITLE_ONLY"
    assert adapter.session.edits == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("missing", None, "SOURCE_FACT_INPUTS_SCHEMA_INVALID"),
        ("candidate_id", "different-candidate", "CANDIDATE_BINDING_INVALID"),
        ("final_transcript", "stale transcript", "SOURCE_FACT_FINAL_TRANSCRIPT_DRIFT"),
    ],
)
def test_missing_or_drifted_source_fact_fails_before_edit(
    tmp_path, field, value, message
):
    args, adapter, dependencies, _manifest_path = _fixture(tmp_path)
    path = tmp_path / "TITLE-INPUT.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if field == "missing":
        del payload["source_fact_inputs"]["selection_scorecard"]
    elif field == "candidate_id":
        payload[field] = value
    else:
        payload["source_fact_inputs"][field] = value
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(title_repair.TitleRepairError, match=message):
        title_repair.run(args, **dependencies)
    assert adapter.session.edits == 0


def test_non_title_live_drift_persists_a_block(tmp_path):
    args, adapter, dependencies, _manifest_path = _fixture(tmp_path)
    args.execute = False
    code, receipt = title_repair.run(args, **dependencies)
    assert code == 0 and receipt["status"] == "PLANNED"

    adapter.session.raw["archive"]["desc"] = "外部修改"
    args.execute = True
    code, receipt = title_repair.run(args, **dependencies)
    assert code == 5
    assert receipt["status"] == "BLOCKED_NON_TITLE_DRIFT"
    assert adapter.session.edits == 0


def test_title_run_can_bind_a_real_cover_completion_without_uploaded_sidecar(
    tmp_path, monkeypatch
):
    """The cover predecessor route uses the completion receipt as its proof."""

    from src.autoslice import publication_reconciliation as reconciliation

    args, adapter, dependencies, _manifest_path = _fixture(tmp_path)
    live = adapter.observe(BVID, 7)
    completed_path = tmp_path / "cover-completed.json"
    completed = {
        "schema_version": "same-bv-cover-repair-completed.v1",
        "candidate_id": "semantictalk_test_1_2",
        "live_snapshot": live,
    }
    completed_path.write_text(json.dumps(completed, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        reconciliation,
        "_validate_same_bv_cover_completed",
        lambda **_: (completed, BVID, 11, 22),
    )
    monkeypatch.setattr(
        reconciliation,
        "_candidate_and_date",
        lambda _manifest: ("semantictalk_test_1_2", "2099-01-02"),
    )
    args.predecessor_cover_completed = str(completed_path)

    code, receipt = title_repair.run(args, **dependencies)
    assert code == 0
    assert receipt["status"] == "VERIFIED_TITLE_ONLY"
    completion_sha = _sha(completed_path)
    assert receipt["binding"]["predecessor_cover_completed"] == {
        "path": str(completed_path.resolve()),
        "sha256": completion_sha,
        "bytes": completed_path.stat().st_size,
    }
    assert receipt["binding"]["published_sha256"] == completion_sha
    assert receipt["binding"]["public_verify_sha256"] == completion_sha
    assert adapter.session.edits == 1

    # Replaying the receipt still revalidates the completion binding; changing
    # the proof bytes cannot fall back to the old uploaded sidecar.
    completed_path.write_text(json.dumps({**completed, "drift": True}), encoding="utf-8")
    with pytest.raises(title_repair.TitleRepairError, match="RECEIPT_BINDING_DRIFT"):
        title_repair.run(args, **dependencies)
    assert adapter.session.edits == 1


def test_cover_successor_allows_relocated_media_but_rejects_content_drift(
    tmp_path, monkeypatch
):
    """Copied immutable media may move roots, while changed bytes still block."""

    import src.autoslice.new_bv_repair_authority as new_bv

    first_video = tmp_path / "first" / "slice.mp4"
    first_srt = tmp_path / "first" / "slice.srt"
    current_video = tmp_path / "current" / "slice.mp4"
    current_srt = tmp_path / "current" / "slice.srt"
    for path, data in (
        (first_video, b"immutable-video"),
        (first_srt, b"immutable-srt"),
        (current_video, b"immutable-video"),
        (current_srt, b"immutable-srt"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def attestation(path):
        return {
            "path": str(path),
            "sha256": _sha(path),
            "bytes": path.stat().st_size,
        }

    source_publication = {"path": "first-publication", "sha256": "a" * 64}
    current_authority = {
        "schema_version": "new-bv-same-bv-publication-authority.v1",
        "candidate_id": "candidate-1",
        "recording_date": "2099-01-02",
        "bvid": BVID,
        "aid": 11,
        "cid": 22,
        "source_publication": source_publication,
        "observed_public_title": ORIGINAL_TITLE,
        "title": "当前封面标题",
    }
    first_manifest = {
        "video": attestation(first_video),
        "package_attestation": {"subtitle": attestation(first_srt)},
    }
    current_manifest = {
        "title": current_authority["title"],
        "video": attestation(current_video),
        "package_attestation": {"subtitle": attestation(current_srt)},
        "recovery_publication_authority": copy.deepcopy(current_authority),
    }
    incoming_authority = {
        **current_authority,
        "observed_public_title": ORIGINAL_TITLE,
        "title": FINAL_TITLE,
    }
    current_record = {
        "recovery_publication_authority": copy.deepcopy(current_authority),
        "verified_title_repair": {"receipt": "previous"},
    }
    cover_proof = {
        "candidate_id": current_authority["candidate_id"],
        "recording_date": current_authority["recording_date"],
        "bvid": BVID,
        "aid": 11,
        "cid": 22,
        "live_snapshot": {},
    }
    reference = {"receipt": "second-title"}
    monkeypatch.setattr(
        new_bv,
        "validate_new_bv_repair_authority",
        lambda authority, **_: dict(authority),
    )
    monkeypatch.setattr(
        title_repair,
        "_validate_title_repair_successor",
        lambda *_args, **_kwargs: None,
    )

    title_repair._validate_cover_successor_context(
        current_manifest=current_manifest,
        current_record=current_record,
        first_manifest=first_manifest,
        incoming_authority=incoming_authority,
        candidate_id="candidate-1",
        cover_proof=cover_proof,
        reference=reference,
        visited=set(),
    )

    for label, entry in (
        ("VIDEO", "video"),
        ("SRT", "subtitle"),
    ):
        mutated = copy.deepcopy(current_manifest)
        if entry == "video":
            path = current_video
            mutated["video"] = attestation(path)
        else:
            path = current_srt
            mutated["package_attestation"]["subtitle"] = attestation(path)
        path.write_bytes(b"changed-" + entry.encode("ascii"))
        if entry == "video":
            mutated["video"] = attestation(path)
        else:
            mutated["package_attestation"]["subtitle"] = attestation(path)
        with pytest.raises(
            title_repair.TitleRepairError,
            match="TITLE_REPAIR_SUCCESSOR_MEDIA_DRIFT",
        ):
            title_repair._validate_cover_successor_context(
                current_manifest=mutated,
                current_record=current_record,
                first_manifest=first_manifest,
                incoming_authority=incoming_authority,
                candidate_id="candidate-1",
                cover_proof=cover_proof,
                reference=reference,
                visited=set(),
            )
        path.write_bytes(b"immutable-video" if entry == "video" else b"immutable-srt")


def _successor_fixture(tmp_path):
    args, adapter, dependencies, manifest_path = _fixture(tmp_path)
    code, first = title_repair.run(args, **dependencies)
    assert code == 0
    first_paths = {
        "receipt": tmp_path / "TITLE-RECEIPT.json",
        "input": tmp_path / "TITLE-INPUT.json",
        "result": tmp_path / "TITLE-RESULT.json",
    }
    title_input = json.loads(first_paths["input"].read_text())
    title_input["original_title"] = FINAL_TITLE
    second_title = render_talk_title_prefix() + "游戏对白：这段暧昧关系怎么回事？"
    input_path = tmp_path / "second-input.json"
    input_path.write_text(json.dumps(title_input, ensure_ascii=False))
    inputs = title_input["source_fact_inputs"]
    review = review_and_repair_source_facts(
        selection_hook=SELECTION_HOOK, title=second_title,
        final_transcript=inputs["final_transcript"],
        clip_context_prompt=inputs["clip_context_prompt"],
        selection_scorecard=inputs["selection_scorecard"],
        speaker_evidence=inputs["speaker_evidence"],
        candidate_id=title_input["candidate_id"],
        final_reviewed_srt_path=Path(title_input["srt_path"]),
        story_contract=inputs["story_contract"], title_repair_allowed=True,
        llm_call=lambda _p: json.dumps({
            "schema_version": "lidousha-source-fact-review.v1", "status": "KEEP",
            "final_selection_hook": SELECTION_HOOK, "final_title": second_title,
            "supported_by": ["final_transcript"], "changed_surfaces": [],
            "summary": "准确概括对白", "addressee_attribution": [],
        }, ensure_ascii=False),
    )
    assert review["status"] == "PASS"
    result_path = tmp_path / "second-result.json"
    result_path.write_text(json.dumps({
        "status": "OK", "final_title": second_title,
        "final_selection_hook": SELECTION_HOOK, "source_fact_review": review,
        "input_sha256": _sha(input_path),
    }, ensure_ascii=False))
    args.title_input = str(input_path)
    args.title_result = str(result_path)
    args.receipt = str(tmp_path / "second-receipt.json")
    for key, path in first_paths.items():
        setattr(args, "predecessor_title_" + key, str(path))
    return args, adapter, dependencies, manifest_path, first_paths, first, second_title


@pytest.mark.parametrize("mutation", [None, "missing_argument", "pending", "cid", "tags", "live_tags", "unrelated_title", "first_result"])
def test_explicit_verified_title_predecessor_keeps_original_publication_immutable(
    tmp_path, monkeypatch, mutation,
):
    args, adapter, dependencies, manifest_path, first_paths, first, second_title = _successor_fixture(tmp_path)
    immutable = {path: path.read_bytes() for path in (manifest_path, *first_paths.values())}
    if mutation == "missing_argument":
        args.predecessor_title_result = None
    elif mutation == "pending":
        first["status"] = "PUBLIC_PENDING"
    elif mutation == "cid":
        first["after"]["public"]["cid"] = 23
    elif mutation == "tags":
        first["after"]["creator"]["metadata"]["tags"] = ["changed"]
    elif mutation == "live_tags":
        adapter.session.raw["archive"]["tag"] = "changed"
    elif mutation == "unrelated_title":
        path = first_paths["input"]
        value = json.loads(path.read_text())
        value["original_title"] = "【李豆沙】没有绑定的标题"
        path.write_text(json.dumps(value, ensure_ascii=False))
    elif mutation == "first_result":
        path = first_paths["result"]
        value = json.loads(path.read_text())
        value["final_title"] = "【李豆沙】没有审查的标题"
        path.write_text(json.dumps(value, ensure_ascii=False))
    if mutation in {"pending", "cid", "tags"}:
        first_paths["receipt"].write_text(json.dumps(first, ensure_ascii=False))
    if mutation is not None:
        with pytest.raises(title_repair.TitleRepairError):
            title_repair.run(args, **dependencies)
        assert adapter.session.edits == 1
        assert not __import__("pathlib").Path(args.receipt).exists()
        return
    code, second = title_repair.run(args, **dependencies)
    assert code == 0 and second["status"] == "VERIFIED_TITLE_ONLY"
    assert adapter.session.edits == 2
    assert second["binding"]["original_title"] == FINAL_TITLE
    assert second["binding"]["final_title"] == second_title
    assert {path: path.read_bytes() for path in immutable} == immutable
    code, _ = title_repair.run(args, **dependencies)
    assert code == 0 and adapter.session.edits == 2
    import src.autoslice.new_bv_repair_authority as new_bv
    authority = {"bvid": BVID, "aid": 11, "cid": 22, "observed_public_title": ORIGINAL_TITLE, "title": second_title}
    monkeypatch.setattr(new_bv, "validate_new_bv_repair_authority", lambda *_a, **_k: authority)
    monkeypatch.setattr(new_bv, "replay_first_publication_manifest", lambda _a: dependencies["load_and_verify"]()[0])
    reference = {
        key: {"path": str(path), "sha256": _sha(path)}
        for key, path in {
            "receipt": __import__("pathlib").Path(args.receipt),
            "title_input": __import__("pathlib").Path(args.title_input),
            "title_result": __import__("pathlib").Path(args.title_result),
        }.items()
    }
    title_repair.validate_title_repair_successor(
        reference, authority=authority, candidate_id="semantictalk_test_1_2", title=second_title,
    )
    # A referenced predecessor is immutable on resume, including nonsemantic changes.
    first_paths["receipt"].write_text(first_paths["receipt"].read_text() + "\n")
    with pytest.raises(title_repair.TitleRepairError, match="HASH_DRIFT"):
        title_repair.validate_title_repair_successor(
            reference, authority=authority, candidate_id="semantictalk_test_1_2", title=second_title,
        )
    assert adapter.session.edits == 2
