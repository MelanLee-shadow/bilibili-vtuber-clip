from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import authorized_upload as upload
from src.autoslice import channel_envelope as envelope
from src.autoslice import same_bv_repair as repair
from src.autoslice import same_bv_stage_repair as stage

BVID = "BV1Hsn46YEat"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))
    return stage.binding(path)


def fixture(tmp_path, *, imported=True, desc="corrected description"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = {"archive": {"aid": 42, "bvid": BVID, "mid": 123, "state": 0, "state_desc": "开放浏览",
        "title": "old title", "desc": desc, "tag": "tag1,tag2", "tid": 27,
        "copyright": 2, "source": "https://source.example/video", "cover": "https://image.example/old.png"},
        "videos": [{"cid": 101, "filename": "old", "title": "old title"}]}
    before = write(tmp_path / "creator-before.json", raw)
    creator = repair.normalise_creator_snapshot(bvid=BVID, creator_data=raw)
    live = {"creator": creator, "public": {"available": True, "bvid": BVID, "aid": 42, "state": 0,
        "cid": 101, "metadata": {k: v for k, v in creator["metadata"].items() if k != "source"}},
        "section": {"available": True, "section_id": 8,
            "matches": [{"bvid": BVID, "aid": 42, "cid": 101, "title": "old title"}]}}
    video = tmp_path / "base.mp4"
    video.write_bytes(b"exact approved staged media")
    review = write(tmp_path / "root-review.json", {"file": str(video), "sha256": repair.sha256_file(video),
        "complete_decode_exit": 0, "decode_stderr": "", "old_audio_packets": "a" * 64,
        "new_audio_packets": "a" * 64, "visual_review": "root inspected exact start/middle/end"})
    authorization = write(tmp_path / "authorization.json", {"by": "维护者", "quote": "先去条，只能保留一P",
        "source_thread_id": "trusted-human-task", "root_accepted": [{"bvid": BVID, "candidate_id": "01-kiss",
            "scope": "remove_top_strip", "final_acceptance": False, "video_sha256": repair.sha256_file(video),
            "review_sha256": review["sha256"], "visual_acceptance_source": "root inspection",
            "visual_acceptance_statement": "root inspected exact start/middle/end"}]})
    historical = write(tmp_path / "publication.json", {"account_mid": 123, "collection_id": 7,
        "section_id": 8, "clips": [{"clip_id": "01-kiss", "bvid": BVID}]})
    request = {"schema_version": stage.REQUEST_SCHEMA, "operation": "remove_top_strip", "bvid": BVID,
        "aid": 42, "old_cid": 101, "account_mid": 123, "candidate_id": "01-kiss", "creator_before": before,
        "publication_record": historical, "authorization": authorization, "root_review": review,
        "video": stage.binding(video), "season": {"season_id": 7, "section_id": 8},
        "final_acceptance": False, "subtitles_complete": False,
        "remaining": ["final_subtitles", "normal_final_package_acceptance"]}
    if imported:
        raw["videos"].append({"cid": 202, "filename": "new", "title": "old title"})
        live["creator"] = repair.normalise_creator_snapshot(bvid=BVID, creator_data=raw)
        log = tmp_path / "append.log"
        log.write_text("actual uploader result")
        request["existing_append"] = {
            "started": write(tmp_path / "append.started.json", {"bvid": BVID, "media_sha256": repair.sha256_file(video),
                "approval_file": review["path"], "approval_sha256": review["sha256"], "old_cids": [101],
                "subtitles_complete": False}),
            "returned": write(tmp_path / "append.return.json", {"returncode": 0, "log_sha256": repair.sha256_file(log)}),
            "log": stage.binding(log), "new_cid": write(tmp_path / "new-cid.json", {"bvid": BVID, "cid": 202}),
            "creator": write(tmp_path / "creator-two.json", raw)}
    path = tmp_path / "stage-request.json"
    write(path, request)
    return path, request, live


class FakeAdapter:
    def __init__(self, live, *, retry=False):
        self.live = copy.deepcopy(live)
        self.append_calls = 0
        self.swap_calls = []
        self.retry = retry
        self.section_calls = 0

    def observe(self, *args):
        return copy.deepcopy(self.live)

    def append_existing(self, *args):
        self.append_calls += 1
        self.live["creator"]["videos"].append({"cid": 202, "filename": "new", "title": "base"})

    def prepare_cover(self, *args):
        raise AssertionError("stage must preserve cover without upload")

    def swap_keep_only(self, bvid, *, keep_cid, target_metadata, cover_url):
        self.swap_calls.append((bvid, keep_cid, copy.deepcopy(target_metadata), cover_url))
        if self.retry:
            self.retry = False
            raise repair.RemoteMutationError("request frequent", code=21540)
        video = next(v for v in self.live["creator"]["videos"] if v["cid"] == keep_cid)
        video["title"] = target_metadata["title"]
        self.live["creator"]["videos"] = [video]
        self.live["creator"]["metadata"] = {**target_metadata, "cover": repair._normalise_cover_url(cover_url)}
        self.live["public"]["metadata"] = {k: v for k, v in self.live["creator"]["metadata"].items() if k != "source"}
        self.live["public"]["cid"] = keep_cid
        self.live["section"]["matches"][0]["cid"] = keep_cid

    def sync_section_title(self, bvid, section_id, *, expected_current_title, target_title):
        assert self.live["section"]["matches"][0]["title"] == expected_current_title
        self.section_calls += 1
        self.live["section"]["matches"][0]["title"] = target_title
        return {"code": 0}


def plan_fixture(tmp_path, *, imported=True, retry=False):
    request_path, request, live = fixture(tmp_path, imported=imported)
    plan = stage.create_plan(request_path=request_path, snapshot=live)
    plan_path = tmp_path / "plan.json"
    repair.write_plan(plan_path, plan)
    journal = tmp_path / "journal.jsonl"
    return request, plan, plan_path, journal, FakeAdapter(live, retry=retry)


def test_existing_append_zero_reupload_and_retry_21540(tmp_path):
    request, plan, path, journal, adapter = plan_fixture(tmp_path, retry=True)
    first = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert first.state == "SWAP_RETRYABLE"
    assert adapter.append_calls == 0
    second = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert second.state == "VERIFIED"
    assert adapter.append_calls == 0
    assert len(adapter.swap_calls) == 2 and adapter.swap_calls[0] == adapter.swap_calls[1]
    assert [v["cid"] for v in adapter.live["creator"]["videos"]] == [202]
    assert "APPEND_INTENT" not in [r["state"] for r in repair.read_journal(journal)]
    repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert len(adapter.swap_calls) == 2


def test_other_clips_append_once_then_swap(tmp_path):
    request, _, path, journal, adapter = plan_fixture(tmp_path, imported=False)
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1 and len(adapter.swap_calls) == 1


@pytest.mark.parametrize("mutation", ["unknown_cid", "filename", "metadata", "three_p", "public", "section"])
def test_existing_append_fresh_drift_refused(tmp_path, mutation):
    request_path, _, live = fixture(tmp_path)
    if mutation == "unknown_cid":
        live["creator"]["videos"][1]["cid"] = 303
    elif mutation == "filename":
        live["creator"]["videos"][1]["filename"] = "unknown"
    elif mutation == "metadata":
        live["creator"]["metadata"]["desc"] = "drift"
    elif mutation == "three_p":
        live["creator"]["videos"].append({"cid": 303, "filename": "unknown", "title": "x"})
    elif mutation == "public":
        live["public"]["metadata"]["title"] = "drift"
    else:
        live["section"]["matches"][0]["cid"] = 303
    with pytest.raises(repair.PlanInvalid):
        stage.create_plan(request_path=request_path, snapshot=live)


def test_drift_after_plan_blocks_without_append_or_edit(tmp_path):
    request, _, path, journal, adapter = plan_fixture(tmp_path)
    adapter.live["creator"]["videos"][1]["cid"] = 303
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert result.state == "BLOCKED_DRIFT"
    assert adapter.append_calls == 0 and adapter.swap_calls == []


def test_media_review_and_append_log_hash_drift_refused(tmp_path):
    request, plan, path, _, _ = plan_fixture(tmp_path)
    Path(request["existing_append"]["log"]["path"]).write_text("changed")
    with pytest.raises(repair.PlanInvalid, match="drift"):
        repair.validate_plan(plan, manifest=request, plan_path=path)


def test_stage_never_normal_manifest_or_completed_authority(tmp_path, monkeypatch, capsys):
    request, plan, path, journal, adapter = plan_fixture(tmp_path)
    repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    _, problems = upload.load_and_verify(Path(plan["manifest"]["path"]))
    assert problems
    monkeypatch.setattr(stage, "adapter", lambda *a, **kw: adapter)
    out = tmp_path / "completed.json"
    args = SimpleNamespace(plan=str(path), journal=str(journal), out=str(out))
    assert upload.repair_verify_live(args) == 0
    completed = json.loads(out.read_text())
    assert completed["schema_version"] == stage.COMPLETED_SCHEMA
    assert completed["final_acceptance"] is False
    assert completed["normal_publication_reconciliation"] is False
    assert "normal_final_package_acceptance" in completed["remaining"]
    assert '"state": "STAGE_VERIFIED"' in capsys.readouterr().out


def test_stage_plan_cli_uses_explicit_request_without_normal_manifest(tmp_path, monkeypatch, capsys):
    path, _, live = fixture(tmp_path)
    monkeypatch.setattr(stage, "adapter", lambda *a, **kw: FakeAdapter(live))
    out = tmp_path / "plan.json"
    code = upload.main(["repair-plan", "--stage-request", str(path), "--bvid", BVID,
        "--out", str(out), "--journal", str(tmp_path / "journal.jsonl"), "--dry-run"])
    assert code == 0 and not out.exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["existing_new_video"]["cid"] == 202 and plan["final_acceptance"] is False


def test_import_adoption_survives_restart_before_swap(tmp_path):
    request, _, path, journal, adapter = plan_fixture(tmp_path)
    result = repair.repair_step(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert result.state == "EXISTING_P_READY"
    resumed = FakeAdapter(adapter.live)
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=resumed)
    assert result.state == "VERIFIED" and resumed.append_calls == 0


def test_root_alternative_decode_audio_schema_is_consumed_without_fabrication(tmp_path):
    path, request, live = fixture(tmp_path, imported=False)
    review = {"path": request["video"]["path"], "sha256": request["video"]["sha256"],
        "decode_rc": 0, "decode_stderr": "", "frame_review": "PENDING_ROOT_VISUAL",
        "original_and_new_aac_packet_sha256": ["a" * 64, "a" * 64]}
    request["root_review"] = write(Path(request["root_review"]["path"]), review)
    auth_path = Path(request["authorization"]["path"])
    auth = json.loads(auth_path.read_text())
    auth["root_accepted"][0]["review_sha256"] = request["root_review"]["sha256"]
    request["authorization"] = write(auth_path, auth)
    write(path, request)
    stage.create_plan(request_path=path, snapshot=live)
    assert json.loads(Path(request["root_review"]["path"]).read_text()) == review


def test_stage_adapter_refuses_without_shared_lock(tmp_path):
    request, plan, *_ = plan_fixture(tmp_path)
    live = stage.StageAdapter(plan=plan, biliup_bin="/bin/false", session=None, http=None,
        view_url="", tags_url="", section_url="")
    with pytest.raises(repair.PlanInvalid, match="shared upload lock"):
        live.append_existing(BVID, Path(request["video"]["path"]))


METADATA_TITLE = "【李豆沙】奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了"


def metadata_fixture(tmp_path, *, title=METADATA_TITLE, desc="corrected description"):
    path, request, live = fixture(tmp_path, imported=False, desc=desc)
    root = tmp_path / "cover-repair"
    folder = root / "01-kiss" / "stage1"
    folder.mkdir(parents=True)
    quote = "我先说的"
    images = []
    for i in range(4):
        source, sent = folder / f"source-{i}.png", folder / f"image-{i}.jpg"
        source.write_bytes(f"test pixels {i}".encode())
        sent.write_bytes(f"test JPEG {i}".encode())
        images.append({"source": str(source), "source_sha256": repair.sha256_file(source),
            "sent_path": str(sent), "sent_sha256": repair.sha256_file(sent)})
    inputs, texts = [], {}
    for stem in ("reaction.mai", "reference.mai"):
        f = folder / (stem + ".json")
        raw = {"status": "OK", "native_segments": [
            {"text": quote, "start_ms": 0, "end_ms": 1000, "speaker": "S01"}]}
        ref = write(f, raw)
        inputs.append({"path": str(f.relative_to(root)), "sha256": ref["sha256"], "status": "OK"})
        texts[stem + ".text.json"] = {"source_sha256": ref["sha256"], "segments": raw["native_segments"]}
    cover_text = ["奈奈莉娅看豆沙", "评价怎么都一样"]
    checks = write(folder / "checks.json", {"title": title, "quote": quote, "quote_source": "reaction.mai.json",
        "segment_index": 1, "segment": raw["native_segments"][0], "attribution": "奈奈莉娅", "attribution_basis": "source evidence"})
    editorial = {"title": title, "lines": cover_text, "hook": "watching the same ranking"}
    question = "Review supplied pixels and complete source.\n" + json.dumps({"editorial": editorial,
        "complete_existing_transcriptions": texts}, ensure_ascii=False)
    verdict = {"status": "PASS", "observed_cover_text": cover_text, "issues": [], **{k: True for k in (
        "identity_ok", "clean_layers", "no_fabrication", "modality_and_speaker_ok", "quote_supported",
        "story_clear", "thumbnail_readable", "face_minimum_ok", "no_text_over_face")}}
    answer = json.dumps(verdict)
    response = write(folder / "response.json", {"model": "test-cpa", "status": "completed", "error": None, "output_text": answer})
    prompt_hash = hashlib.sha256(json.dumps({"question": question, "image_sha256s": [i["sent_sha256"] for i in images],
        "model": "test-cpa"}, sort_keys=True).encode()).hexdigest()
    witness = write(folder / "witness.json", {"schema_version": "cpa-frame-witness.v1", "provider": "cpa",
        "status": "OBSERVED", "model": "test-cpa", "question": question, "answer": answer,
        "response_sha256": response["sha256"], "prompt_sha256": prompt_hash,
        "images": [{"image_path": i["sent_path"], "image_sha256": i["sent_sha256"]} for i in images]})
    delivery = {"schema": "manual-reaction-title-cover-stage.v1", "candidate_id": "01-kiss", "title": title,
        "subtitle_input_contract": "USER_AUTHORIZED_TITLE_COVER_BEFORE_FINAL_SRT", "final_srt_status": "NOT_AVAILABLE_NOT_CLAIMED",
        "cover_text": cover_text, "hook": editorial["hook"], "source_video": request["video"],
        "cover": stage.binding(Path(images[0]["source"])), "phone": stage.binding(Path(images[1]["source"])),
        "comparison": stage.binding(Path(images[2]["source"])), "spec": write(folder / "spec.json", {}),
        "pixel_replay": write(folder / "pixels.json", {}), "choice": write(folder / "choice.json", []),
        "cpa_model": "test-cpa", "cpa_witness": witness, "cpa_original_response": response,
        "cpa_request": write(folder / "request.json", {"question": question, "images": images}),
        "cpa_verdict": write(folder / "verdict.json", verdict), "subtitle_inputs": inputs,
        "quote": {"text": quote, "speaker": "奈奈莉娅", "time_ms": [0, 1000], "evidence": checks}}
    delivery_ref = write(folder / "DELIVERY.json", delivery)
    root_review = write(folder / "root-review.json", {"schema": "reaction-title-cover-root-review.v1",
        "status": "APPROVED_TITLE_COVER_STAGE_ONLY", "candidate_id": "01-kiss", "bvid": BVID,
        "subtitle_final_acceptance": False, "full_media_acceptance": False,
        "root_observations": ["root review test evidence"], "authorization": "user authorized title/cover first",
        "title": title, "cover": delivery["cover"], "delivery": {k: delivery_ref[k] for k in ("path", "sha256")}})
    historical_path = Path(request["publication_record"]["path"])
    historical = json.loads(historical_path.read_text())
    historical["clips"][0]["title"] = METADATA_TITLE
    historical["clips"][0]["video_sha256"] = request["video"]["sha256"]
    request["publication_record"] = write(historical_path, historical)
    del request["video"]
    request.update(operation="title_cover_only", root_review=root_review, title_cover_delivery=delivery_ref)
    write(path, request)
    return path, request, live


class MetadataAdapter(FakeAdapter):
    def prepare_cover(self, *args):
        return "https://image.example/new.png"


def test_metadata_only_single_p_never_appends_and_recovers_21540(tmp_path):
    request_path, request, live = metadata_fixture(tmp_path)
    plan = stage.create_plan(request_path=request_path, snapshot=live)
    path, journal = tmp_path / "metadata-plan.json", tmp_path / "journal.jsonl"
    repair.write_plan(path, plan)
    adapter = MetadataAdapter(live, retry=True)
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    assert result.state == "SWAP_RETRYABLE"
    assert adapter.append_calls == 0 and adapter.live["creator"]["videos"][0]["cid"] == 101
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter,
                              wait_seconds=1, poll_seconds=0)
    assert result.state == "VERIFIED"
    assert adapter.append_calls == 0 and adapter.swap_calls[0] == adapter.swap_calls[1]
    assert adapter.live["creator"]["videos"] == [{"cid": 101, "filename": "old", "title": plan["target_metadata"]["title"]}]
    assert adapter.section_calls == 1
    for field in ("desc", "tags", "tid", "copyright", "source"):
        assert adapter.live["creator"]["metadata"][field] == live["creator"]["metadata"][field]


def test_metadata_only_accepts_host_first_pair_prefix_without_restacking(tmp_path):


    title = "【李豆沙×奈奈莉娅】奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了"
    # The live description already carries the partner line rendered from the
    # same partner tuple (the published reaction descriptions do).
    desc = envelope.render_publish_description(upload.SUBMISSION_DESCRIPTION, ("奈奈莉娅",))
    request_path, request, live = metadata_fixture(tmp_path, title=title, desc=desc)
    plan = stage.create_plan(request_path=request_path, snapshot=live)
    assert plan["target_metadata"]["title"] == title
    path, journal = tmp_path / "metadata-plan.json", tmp_path / "journal.jsonl"
    repair.write_plan(path, plan)
    adapter = MetadataAdapter(live)
    result = repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter,
                              wait_seconds=1, poll_seconds=0)
    assert result.state == "VERIFIED"
    assert adapter.append_calls == 0
    assert adapter.live["creator"]["metadata"]["title"] == title
    assert adapter.live["section"]["matches"][0]["title"] == title


@pytest.mark.parametrize("title", [
    "【奈奈莉娅×李豆沙】奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了",
    "【李豆沙×】奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了",
    "奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了",
])
def test_metadata_only_still_rejects_malformed_or_missing_prefix(tmp_path, title):
    request_path, _, live = metadata_fixture(tmp_path, title=title)
    with pytest.raises(repair.PlanInvalid, match="stage title violates current title policy"):
        stage.create_plan(request_path=request_path, snapshot=live)


_PAIR_BODY = "奈奈莉娅看PSP排名：“我先说的！”怎么连评价都同步了"
_PARTNER_LINE_DESC = upload.SUBMISSION_DESCRIPTION + "\n相关主播：奈奈莉娅 https://space.bilibili.com/489146840"


@pytest.mark.parametrize("title,desc", [
    # Partner in the title, live description lacks its related line.
    ("【李豆沙×奈奈莉娅】" + _PAIR_BODY, upload.SUBMISSION_DESCRIPTION),
    # Related line in the description, plain title drops the partner.
    ("【李豆沙】" + _PAIR_BODY, _PARTNER_LINE_DESC),
])
def test_metadata_only_requires_title_and_description_from_one_partner_tuple(tmp_path, title, desc):
    request_path, _, live = metadata_fixture(tmp_path, title=title, desc=desc)
    with pytest.raises(repair.PlanInvalid, match="partner envelopes disagree"):
        stage.create_plan(request_path=request_path, snapshot=live)


def test_metadata_only_refuses_partner_outside_the_streamer_registry(tmp_path):
    # P_e ⊆ R_e probe: 星汐Seki is a real streamer (psplive roster alias of
    # 星汐) with no structured homepage, so no description link can be rendered.
    title = "【李豆沙×星汐Seki】" + _PAIR_BODY
    desc = upload.SUBMISSION_DESCRIPTION + "\n相关主播：星汐Seki https://space.bilibili.com/1"
    request_path, _, live = metadata_fixture(tmp_path, title=title, desc=desc)
    with pytest.raises(repair.PlanInvalid, match="stage title partner is not a registered streamer"):
        stage.create_plan(request_path=request_path, snapshot=live)


@pytest.mark.parametrize("artifact", ["root_review", "title_cover_delivery", "cpa_original_response", "cpa_request", "cpa_verdict", "cover"])
def test_metadata_hash_bound_root_and_cpa_evidence_cannot_drift(tmp_path, artifact):
    path, request, live = metadata_fixture(tmp_path)
    delivery = stage.replay(request["title_cover_delivery"])
    entry = request.get(artifact) or delivery[artifact]
    Path(entry["path"]).write_bytes(b"modified")
    with pytest.raises(repair.PlanInvalid, match="drift"):
        stage.create_plan(request_path=path, snapshot=live)


def test_metadata_approval_cannot_be_reused_for_rewritten_delivery(tmp_path):
    path, request, live = metadata_fixture(tmp_path)
    delivery = stage.replay(request["title_cover_delivery"])
    delivery["new_unreviewed_field"] = "changed after approval"
    request["title_cover_delivery"] = write(Path(request["title_cover_delivery"]["path"]), delivery)
    write(path, request)
    with pytest.raises(repair.PlanInvalid, match="root approved"):
        stage.create_plan(request_path=path, snapshot=live)


@pytest.mark.parametrize("drift", [False, True])
def test_live_metadata_adapter_preserves_p_and_refuses_last_moment_drift(tmp_path, drift):
    path, request, snapshot = metadata_fixture(tmp_path)
    plan = stage.create_plan(request_path=path, snapshot=snapshot)
    raw = stage.replay(request["creator_before"])
    calls = []
    if drift:
        raw["videos"][0]["filename"] = "unapproved-filename"
    session = SimpleNamespace(archive_view=lambda bvid: raw,
        build_edit_payload=upload.member_api.BiliSession.build_edit_payload, csrf="test")
    session.build_edit_payload = lambda current, **kw: upload.member_api.BiliSession.build_edit_payload(session, current, **kw)
    session.post_json = lambda url, payload: calls.append(copy.deepcopy(payload)) or {"code": 0}
    adapter = stage.StageAdapter(plan=plan, biliup_bin="/bin/false", session=session, http=None,
        view_url="", tags_url="", section_url="", lock_process=SimpleNamespace(poll=lambda: None))
    if drift:
        with pytest.raises(repair.PlanInvalid, match="pre-edit CID/metadata drift"):
            adapter.swap_keep_only(BVID, keep_cid=101, target_metadata=plan["target_metadata"], cover_url="https://image.example/new.png")
        assert calls == []
    else:
        adapter.swap_keep_only(BVID, keep_cid=101, target_metadata=plan["target_metadata"], cover_url="https://image.example/new.png")
        assert len(calls) == 1
        assert calls[0]["videos"] == [{"cid": 101, "filename": "old", "title": plan["target_metadata"]["title"]}]
        assert calls[0]["desc"] == raw["archive"]["desc"]
    with pytest.raises(repair.PlanInvalid, match="never append"):
        adapter.append_existing(BVID, tmp_path / "not-needed.mp4")


def test_same_journal_successor_requires_real_stage_completed_closure(tmp_path, monkeypatch):
    request, old_plan, path, journal, adapter = plan_fixture(tmp_path / "media")
    repair.run_repair(plan_path=path, journal=journal, manifest=request, adapter=adapter)
    monkeypatch.setattr(stage, "adapter", lambda *a, **kw: adapter)
    completed = tmp_path / "media-completed.json"
    assert stage.cli_existing(SimpleNamespace(plan=str(path), journal=str(journal), out=str(completed)), "verify") == 0
    meta_path, meta_request, _ = metadata_fixture(tmp_path / "metadata")
    raw = stage.replay(meta_request["creator_before"])
    raw["videos"] = copy.deepcopy(adapter.live["creator"]["videos"])
    meta_request["creator_before"] = write(Path(meta_request["creator_before"]["path"]), raw)
    meta_request["old_cid"] = 202
    meta_request["predecessor_stage_completed"] = stage.binding(completed)
    write(meta_path, meta_request)
    meta_plan = stage.create_plan(request_path=meta_path, snapshot=adapter.live)
    new_path = tmp_path / "metadata-plan.json"
    repair.write_plan(new_path, meta_plan)
    meta_adapter = MetadataAdapter(adapter.live)
    result = repair.run_repair(plan_path=new_path, journal=journal, manifest=meta_request,
        adapter=meta_adapter, wait_seconds=1, poll_seconds=0)
    assert result.state == "VERIFIED" and meta_adapter.append_calls == 0
    assert repair.read_journal(journal)[-1]["plan_id"] == meta_plan["plan_id"]
    broken = json.loads(completed.read_text())
    broken["new_cid"] = 303
    meta_request["predecessor_stage_completed"] = write(completed, broken)
    write(meta_path, meta_request)
    with pytest.raises(repair.PlanInvalid, match="same completed target"):
        stage.create_plan(request_path=meta_path, snapshot=adapter.live)


def test_media_after_metadata_preserves_new_title_cover_and_exact_single_append(tmp_path, monkeypatch):
    meta_path, meta_request, live = metadata_fixture(tmp_path / "metadata")
    meta_plan = stage.create_plan(request_path=meta_path, snapshot=live)
    plan_path, journal = tmp_path / "metadata-plan.json", tmp_path / "journal.jsonl"
    repair.write_plan(plan_path, meta_plan)
    meta_adapter = MetadataAdapter(live)
    result = repair.run_repair(plan_path=plan_path, journal=journal, manifest=meta_request,
        adapter=meta_adapter, wait_seconds=1, poll_seconds=0)
    assert result.state == "VERIFIED"
    monkeypatch.setattr(stage, "adapter", lambda *a, **kw: meta_adapter)
    completed = tmp_path / "metadata-completed.json"
    assert stage.cli_existing(SimpleNamespace(plan=str(plan_path), journal=str(journal), out=str(completed)), "verify") == 0
    request_path, request, _ = fixture(tmp_path / "media", imported=False)
    raw = stage.replay(request["creator_before"])
    raw["archive"]["title"] = meta_plan["target_metadata"]["title"]
    raw["archive"]["cover"] = "https://image.example/new.png"
    raw["videos"] = copy.deepcopy(meta_adapter.live["creator"]["videos"])
    request["creator_before"] = write(Path(request["creator_before"]["path"]), raw)
    request["predecessor_stage_completed"] = stage.binding(completed)
    write(request_path, request)
    media_plan = stage.create_plan(request_path=request_path, snapshot=meta_adapter.live)
    media_path = tmp_path / "media-plan.json"
    repair.write_plan(media_path, media_plan)
    media_adapter = FakeAdapter(meta_adapter.live)
    result = repair.run_repair(plan_path=media_path, journal=journal, manifest=request, adapter=media_adapter)
    assert result.state == "VERIFIED" and media_adapter.append_calls == 1
    assert media_adapter.live["creator"]["metadata"] == meta_adapter.live["creator"]["metadata"]
    assert media_adapter.live["creator"]["videos"][0]["cid"] == 202


def test_pair_title_successor_replays_plain_title_history_without_reimposing_the_rule(tmp_path, monkeypatch):
    # Real chain: the plain-title reaction stages were VERIFIED while
    # the live description already carried 相关主播：奈奈莉娅; the pair-title
    # successor binds that completed stage.  The envelope rule binds new stages
    # only; it is not re-imposed on the verified predecessor.
    from src.autoslice import same_bv_stage_metadata as stage_metadata

    plain = "【李豆沙】" + _PAIR_BODY
    meta_path, meta_request, live = metadata_fixture(tmp_path / "plain", title=plain, desc=_PARTNER_LINE_DESC)
    with pytest.raises(repair.PlanInvalid, match="partner envelopes disagree"):
        stage.create_plan(request_path=meta_path, snapshot=live)  # a new plain stage is refused
    plain_plan_path, journal = tmp_path / "plain-plan.json", tmp_path / "journal.jsonl"
    completed = tmp_path / "plain-completed.json"
    adapter = MetadataAdapter(live)
    with monkeypatch.context() as earlier_rule:  # the predecessor ran under the earlier rule
        earlier_rule.setattr(stage_metadata, "stage_envelope_problem", lambda *_a: None)
        plain_plan = stage.create_plan(request_path=meta_path, snapshot=live)
        repair.write_plan(plain_plan_path, plain_plan)
        assert repair.run_repair(plan_path=plain_plan_path, journal=journal, manifest=meta_request,
                                 adapter=adapter, wait_seconds=1, poll_seconds=0).state == "VERIFIED"
        earlier_rule.setattr(stage, "adapter", lambda *a, **kw: adapter)
        assert stage.cli_existing(SimpleNamespace(plan=str(plain_plan_path), journal=str(journal),
                                                  out=str(completed)), "verify") == 0

    pair = "【李豆沙×奈奈莉娅】" + _PAIR_BODY
    pair_path, pair_request, _ = metadata_fixture(tmp_path / "pair", title=pair, desc=_PARTNER_LINE_DESC)
    raw = stage.replay(meta_request["creator_before"])
    raw["archive"]["title"] = plain
    raw["archive"]["cover"] = "https://image.example/new.png"
    raw["videos"] = copy.deepcopy(adapter.live["creator"]["videos"])
    pair_request["creator_before"] = write(Path(pair_request["creator_before"]["path"]), raw)
    pair_request["predecessor_stage_completed"] = stage.binding(completed)
    write(pair_path, pair_request)
    pair_plan = stage.create_plan(request_path=pair_path, snapshot=adapter.live)
    assert pair_plan["target_metadata"]["title"] == pair
    pair_plan_path = tmp_path / "pair-plan.json"
    repair.write_plan(pair_plan_path, pair_plan)
    result = repair.run_repair(plan_path=pair_plan_path, journal=journal, manifest=pair_request,
                               adapter=MetadataAdapter(adapter.live), wait_seconds=1, poll_seconds=0)
    assert result.state == "VERIFIED"
