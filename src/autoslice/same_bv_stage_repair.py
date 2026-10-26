"""Explicit, partial same-BV corrections, never normal publication authority.

The request binds the human's stage scope, the root's actual review artifacts,
and the existing archive.  The ordinary repair engine owns append/swap recovery.
Nothing here manufactures a package, final subtitle review or first-publication
receipt.  Its completed receipt is deliberately a different schema.
"""
from __future__ import annotations

import copy
import json
import re
import select
import subprocess
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from . import same_bv_repair as repair

REQUEST_SCHEMA = "same-bv-stage-request.v1"
PLAN_SCHEMA = "same-bv-stage-plan.v1"
COMPLETED_SCHEMA = "same-bv-stage-completed.v1"
SHARED_LOCK_HOST = "oci3"
SHARED_LOCK_PATH = "/opt/bilive/autoslice/upload.lock"


def require(condition: object, message: str) -> None:
    if not condition:
        raise repair.PlanInvalid(message)


def binding(path: Path) -> dict:
    path = path.resolve()
    return {"path": str(path), "sha256": repair.sha256_file(path), "bytes": path.stat().st_size}


def replay(entry: Mapping[str, Any], *, as_json: bool = True) -> Any:
    require(isinstance(entry, Mapping), "stage artifact binding missing")
    path = Path(str(entry.get("path", "")))
    require(path.is_absolute() and path.is_file(), "stage artifact missing or not absolute")
    require(binding(path) == dict(entry), f"stage artifact drift: {path}")
    if not as_json:
        return path
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise repair.PlanInvalid(f"stage artifact invalid JSON: {path}") from exc
    require(isinstance(value, dict), "stage evidence must be a JSON object")
    return value


def load_request(path: Path) -> dict:
    request = repair.load_plan(path)
    require(request.get("schema_version") == REQUEST_SCHEMA, "stage request schema mismatch")
    require(request.get("operation") in {"remove_top_strip", "title_cover_only"}, "unsupported stage operation")
    require(request.get("final_acceptance") is False and request.get("subtitles_complete") is False,
            "stage request cannot claim final/subtitle acceptance")
    require(isinstance(request.get("remaining"), list) and "final_subtitles" in request["remaining"]
            and "normal_final_package_acceptance" in request["remaining"], "stage remaining work missing")
    bvid = request.get("bvid")
    require(isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid), "invalid stage BVID")
    for key in ("aid", "old_cid", "account_mid"):
        require(type(request.get(key)) is int and request[key] > 0, f"stage {key} missing")
    source = replay(request.get("publication_record"))
    clips = [c for c in source.get("clips", []) if c.get("clip_id") == request.get("candidate_id")]
    require(len(clips) == 1 and clips[0].get("bvid") == bvid, "historical publication target mismatch")
    require(source.get("account_mid") == request["account_mid"], "historical account mismatch")
    season = request.get("season") or {}
    require(season.get("season_id") == source.get("collection_id")
            and season.get("section_id") == source.get("section_id")
            and type(season.get("season_id")) is int and type(season.get("section_id")) is int,
            "stage exact season binding missing")
    authorization = replay(request.get("authorization"))
    require(authorization.get("by") == "维护者" and authorization.get("quote")
            and authorization.get("source_thread_id"), "explicit human stage authorization missing")
    if request["operation"] == "title_cover_only":
        from .same_bv_stage_metadata import validate as validate_metadata
        delivery = validate_metadata(request)
        require(delivery["source_video"]["sha256"] == clips[0].get("video_sha256"),
                "title/cover source video differs from historical publication")
    else:
        _validate_media_request(request, authorization)
    before = replay(request.get("creator_before"))
    normalized = repair.normalise_creator_snapshot(bvid=bvid, creator_data=before)
    require(normalized.get("bvid") == bvid and normalized.get("aid") == request["aid"]
            and before.get("archive", {}).get("mid") == request["account_mid"],
            "stage Creator identity mismatch")
    require(len(normalized["videos"]) == 1 and normalized["videos"][0]["cid"] == request["old_cid"],
            "stage original single P mismatch")
    existing = request.get("existing_append")
    if existing is not None:
        require(isinstance(existing, dict), "existing append evidence invalid")
        start = replay(existing.get("started"))
        returned = replay(existing.get("returned"))
        replay(existing.get("log"), as_json=False)
        new_cid = replay(existing.get("new_cid"))
        uploaded = replay(existing.get("creator"))
        require(start.get("bvid") == bvid and start.get("old_cids") == [request["old_cid"]]
                and start.get("media_sha256") == request["video"]["sha256"]
                and start.get("approval_sha256") == request["root_review"]["sha256"]
                and Path(str(start.get("approval_file"))).resolve() == Path(request["root_review"]["path"])
                and start.get("subtitles_complete") is False, "imported append intent mismatch")
        require(returned.get("returncode") == 0
                and returned.get("log_sha256") == existing["log"]["sha256"], "append return/log mismatch")
        require(new_cid.get("bvid") == bvid and type(new_cid.get("cid")) is int
                and new_cid["cid"] != request["old_cid"], "imported append new CID invalid")
        new_snapshot = {"creator": repair.normalise_creator_snapshot(bvid=bvid, creator_data=uploaded)}
        new = repair._two_p_new_video(new_snapshot, {"before": {"creator": normalized}})
        require(new and new.get("cid") == new_cid["cid"] and new.get("filename"),
                "imported append CID/filename/topology mismatch")
    return request


def _validate_media_request(request, authorization):
    approvals = [a for a in authorization.get("root_accepted", [])
                 if a.get("bvid") == request["bvid"] and a.get("candidate_id") == request.get("candidate_id")]
    require(len(approvals) == 1, "exact root stage approval missing")
    approval = approvals[0]
    require(approval.get("scope") == request["operation"] and approval.get("final_acceptance") is False,
            "root approval is not for this partial stage")
    video = replay(request.get("video"), as_json=False)
    review = replay(request.get("root_review"))
    require(approval.get("video_sha256") == request["video"]["sha256"]
            and approval.get("review_sha256") == request["root_review"]["sha256"],
            "root approval does not bind exact media/review")
    require(review.get("sha256") == request["video"]["sha256"]
            and Path(str(review.get("file") or review.get("path"))).resolve() == video,
            "stage review media mismatch")
    # The root's two existing evidence shapes are consumed as written, without
    # inventing visual or subtitle fields in either original file.
    decode = review.get("complete_decode_exit", review.get("decode_rc"))
    require(type(decode) is int and decode == 0 and review.get("decode_stderr") == "",
            "root full decode evidence failed/missing")
    pair = review.get("original_and_new_aac_packet_sha256")
    if pair is None:
        pair = [review.get("old_audio_packets"), review.get("new_audio_packets")]
    require(isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)
            and re.fullmatch(r"(?:SHA256=)?[0-9a-f]{64}", pair[0]) and pair[0] == pair[1],
            "root audio packet equality evidence missing")
    require(approval.get("visual_acceptance_source") and approval.get("visual_acceptance_statement"),
            "root visual acceptance provenance missing")
    require(not request.get("metadata_changes") and not request.get("new_cover"),
            "remove-top-strip stage must preserve all metadata")


def _projection(request: Mapping[str, Any], snapshot: Mapping[str, Any]) -> tuple[dict, dict | None]:
    """Keep a genuine historical Creator preimage and fresh public/section truth."""
    before_creator = repair.normalise_creator_snapshot(
        bvid=request["bvid"], creator_data=replay(request["creator_before"]))
    before = copy.deepcopy(dict(snapshot))
    before["creator"] = before_creator
    problems = repair._basic_snapshot_problems(snapshot, bvid=request["bvid"], aid=request["aid"])
    require(not problems, "; ".join(problems))
    creator, public, section = snapshot["creator"], snapshot["public"], snapshot["section"]
    require(creator["metadata"] == before_creator["metadata"], "stage Creator metadata drift")
    require(public.get("available") is True and public.get("state") == 0
            and public.get("cid") == request["old_cid"]
            and public.get("metadata") == {k: v for k, v in before_creator["metadata"].items() if k != "source"},
            "stage public metadata/CID drift or unavailable")
    matches = section.get("matches", [])
    require(section.get("available") is True and section.get("section_id") == request["season"]["section_id"]
            and len(matches) == 1 and matches[0].get("bvid") in (None, request["bvid"])
            and matches[0].get("aid") == request["aid"] and matches[0].get("cid") == request["old_cid"],
            "stage exact-section identity drift or unavailable")
    new = None
    if request.get("existing_append"):
        uploaded = replay(request["existing_append"]["creator"])
        expected = repair.normalise_creator_snapshot(bvid=request["bvid"], creator_data=uploaded)["videos"][1]
        new = repair._two_p_new_video(snapshot, {"before": before}, expected_new=expected)
        require(new is not None, "fresh state is not the exact imported old+new CID/filename pair")
    else:
        require(creator == before_creator and creator.get("state") == 0,
                "stage planning requires exact original single-P Creator")
        if request["operation"] == "title_cover_only":
            new = copy.deepcopy(before_creator["videos"][0])
    return before, new


def create_plan(*, request_path: Path, snapshot: Mapping[str, Any]) -> dict:
    request = load_request(request_path)
    if request["operation"] == "title_cover_only":
        from .same_bv_stage_metadata import stage_envelope_problem
        problem = stage_envelope_problem(request)
        require(problem is None, problem or "")
    before, new = _projection(request, snapshot)
    plan = {
        "schema_version": PLAN_SCHEMA, "plan_id": uuid.uuid4().hex, "created_at": repair._utc_now(),
        "manifest": binding(request_path), "planning_snapshot": copy.deepcopy(dict(snapshot)),
        **_bound_fields(request, before, new),
    }
    validate_plan(plan, manifest=request)
    return plan


def _bound_fields(request, before, new, *, visited=None):
    raw = replay(request["creator_before"])
    target = copy.deepcopy(before["creator"]["metadata"])
    if request["operation"] == "title_cover_only":
        delivery = replay(request["title_cover_delivery"])
        target["title"] = delivery["title"]
        replacement = {"video": {"sha256": None, "preserve_cid": request["old_cid"]}, "cover": delivery["cover"]}
    else:
        cover_url = raw["archive"]["cover"]
        require(isinstance(cover_url, str) and cover_url.startswith(("http://", "https://")),
                "stage preserved cover URL missing")
        replacement = {"video": request["video"], "cover": {"sha256": None, "preserve_url": cover_url}}
    fields = {"bvid": request["bvid"], "authorization": request["authorization"], "season": request["season"],
        "replacement": replacement, "target_metadata": target, "before": before, "existing_new_video": new,
        "operation": request["operation"], "final_acceptance": False, "subtitles_complete": False,
        "remaining": request["remaining"]}
    if request.get("predecessor_stage_completed"):
        fields["stage_predecessor"] = _predecessor(request, before, visited=visited or set())
    return fields


def _predecessor(request, before, *, visited):
    reference = request["predecessor_stage_completed"]
    require(reference["path"] not in visited, "stage predecessor cycle")
    completed = replay(reference)
    require(completed.get("schema_version") == COMPLETED_SCHEMA
            and completed.get("status") == "STAGE_VERIFIED_FRESH_LIVE"
            and completed.get("bvid") == request["bvid"] and completed.get("aid") == request["aid"]
            and completed.get("candidate_id") == request["candidate_id"]
            and completed.get("new_cid") == request["old_cid"]
            and completed.get("final_acceptance") is False, "stage predecessor is not the same completed target")
    previous = replay(completed["plan"])
    validate_plan(previous, plan_path=Path(completed["plan"]["path"]),
                  _visited=visited | {reference["path"]})
    journal = Path(completed["verified_journal_row"]["path"])
    rows = repair.plan_entries(journal, Path(completed["plan"]["path"]), previous)
    require(rows and rows[-1]["state"] == "VERIFIED"
            and rows[-1]["row_sha256"] == completed["verified_journal_row"]["row_sha256"],
            "stage predecessor journal closure mismatch")
    details = rows[-1]["details"]
    snap = completed["live_snapshot"]
    require(repair.snapshots_equivalent(snap, details["live_snapshot"])
            and repair._target_creator_exact(snap, previous, new_video=details["new_video"], cover_url=details["cover_url"])
            and repair._public_section_state(snap, previous, new_video=details["new_video"], cover_url=details["cover_url"])[0] == "target"
            and repair.snapshots_equivalent(before, snap), "stage predecessor final snapshot/current before drift")
    return {"completed": reference, "plan": {**completed["plan"], "plan_id": previous["plan_id"]},
            "verified_journal_row": completed["verified_journal_row"]}


def validate_plan(plan: Mapping[str, Any], *, manifest=None, plan_path=None, _visited=None) -> None:
    request = replay(plan.get("manifest"))
    validated = load_request(Path(plan["manifest"]["path"]))
    require(manifest is None or manifest == validated, "stage runtime request drift")
    require(request == validated and plan.get("schema_version") == PLAN_SCHEMA and plan.get("plan_id"),
            "stage plan schema/identity invalid")
    before, new = _projection(request, plan.get("planning_snapshot") or {})
    expected = _bound_fields(request, before, new, visited=_visited)
    for key, value in expected.items():
        require(plan.get(key) == value, f"stage plan {key} drift")
    require(plan.get("stage_predecessor") == expected.get("stage_predecessor"), "stage predecessor drift")
    for forbidden in ("package_attestation", "recovery_publication_authority", "predecessor_completion"):
        require(forbidden not in plan, "stage cannot carry normal/final publication authority")
    if plan_path is not None:
        require(plan_path.is_file(), "stage plan file missing")


def import_existing_step(*, snapshot, plan, plan_path, journal):
    new = plan["existing_new_video"]
    require(new is not None, "no existing P in stage plan")
    if repair._pre_edit_video(snapshot, plan, expected_new=new) is None or not repair._before_public_section_exact(snapshot, plan):
        return repair._row_result(repair._block(journal, plan_path, plan,
            reason="existing append adoption found CID/metadata/section drift", snapshot=snapshot))
    details = {"new_video": new, "imported_append": plan["operation"] != "title_cover_only",
               "rule": "NEVER_APPEND_EXISTING_MEDIA", "remote_mutation": False}
    if plan["replacement"]["cover"].get("preserve_url"):
        details["cover_url"] = plan["replacement"]["cover"]["preserve_url"]
    row = repair.append_journal(journal, plan_path=plan_path, plan=plan, state="EXISTING_P_READY", details=details)
    return repair._row_result(row, "existing uploaded CID adopted; zero append")


@contextmanager
def shared_upload_lock():
    """Hold the same OCI3 flock used by the publisher throughout every write."""
    process = subprocess.Popen([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SHARED_LOCK_HOST,
        f"sudo -n flock -n {SHARED_LOCK_PATH} sh -c 'echo LOCKED; cat >/dev/null'",
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 20)
        require(ready and process.stdout.readline().strip() == "LOCKED" and process.poll() is None,
                "shared OCI3 upload lock unavailable")
        yield process
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


class StageAdapter(repair.BilibiliRepairAdapter):
    """Existing adapter with an explicit local biliup and shared-lock guard."""
    def __init__(self, *, plan, biliup_bin, lock_process=None, **kwargs):
        super().__init__(**kwargs)
        self.plan = plan
        self.biliup_bin = Path(biliup_bin).resolve()
        self.lock_process = lock_process
        self.last_snapshot = None

    def observe(self, bvid, section_id):
        snapshot = super().observe(bvid, section_id)
        self.last_snapshot = snapshot
        return snapshot

    def _guard(self):
        require(self.lock_process is not None and self.lock_process.poll() is None,
                "shared upload lock is not held; remote mutation refused")

    def append_existing(self, bvid, media_path):
        self._guard()
        require(self.plan.get("operation") == "remove_top_strip" and not self.plan.get("existing_new_video"),
                "existing/metadata stage must never append again")
        cookie = self.session.biliup_cookie_path or self.session.cookie_path
        completed = subprocess.run([str(self.biliup_bin), "-u", cookie.name, "append", "-v", bvid, str(media_path)],
            cwd=str(cookie.parent), capture_output=True, timeout=1800)
        if completed.returncode != 0:
            raise repair.RemoteMutationError(f"stage append failed rc={completed.returncode}")

    def prepare_cover(self, cover_path):
        self._guard()
        require(self.plan.get("operation") == "title_cover_only", "media-only stage cannot upload a cover")
        require(binding(cover_path) == self.plan["replacement"]["cover"], "stage cover drift before upload")
        return super().prepare_cover(cover_path)

    def swap_keep_only(self, bvid, *, keep_cid, target_metadata, cover_url):
        self._guard()
        current = self.session.archive_view(bvid)
        creator = repair.normalise_creator_snapshot(bvid=bvid, creator_data=current)
        require(not repair._basic_snapshot_problems({"creator": creator}, bvid=bvid,
                    aid=self.plan["before"]["creator"]["aid"]), "stage pre-edit identity drift")
        new = self.plan.get("existing_new_video")
        if new is None:
            require(self.last_snapshot is not None, "stage edit requires fresh observation")
            new = self.last_snapshot["creator"]["videos"][-1]
        require(keep_cid == new["cid"] and repair._pre_edit_video({"creator": creator}, self.plan, expected_new=new),
                "stage pre-edit CID/metadata drift")
        # Reuse this exact fresh response, so no unchecked second archive read
        # can replace the validated filenames or policy fields before the POST.
        payload = self.session.build_edit_payload(current, keep_only_cid=keep_cid,
            title=target_metadata["title"], video_title=target_metadata["title"],
            tag=",".join(target_metadata["tags"]), cover_url=cover_url)
        for field in ("desc", "tid", "copyright", "source"):
            payload[field] = target_metadata[field]
        self._guard()
        response = self.session.post_json("https://member.bilibili.com/x/vu/web/edit?csrf="
                                         + urllib.parse.quote(self.session.csrf), payload)
        if response.get("code") != 0:
            raise repair.RemoteMutationError(f"stage edit failed code={response.get('code')}", code=response.get("code"))
        return response

    def sync_section_title(self, *args, **kwargs):
        self._guard()
        return super().sync_section_title(*args, **kwargs)


def adapter(args, request, *, plan, lock_process=None):
    from scripts import authorized_upload as upload
    from .same_bv_live_verification import run_biliup_readonly_canary
    cookie = Path(args.cookie_json).resolve()
    uploader_cookie = Path(args.biliup_cookie_json).resolve()
    binary = Path(args.biliup_bin).resolve()
    run_biliup_readonly_canary(uploader_cookie, request["bvid"],
        validate_cookie=upload.member_api.validate_biliup_cookie_file, biliup_bin=binary,
        run=subprocess.run, repair_error=repair.RepairError)
    http, _ = upload._build_season_http(cookie)
    session = upload.member_api.BiliSession(cookie_path=cookie, biliup_cookie_path=uploader_cookie)
    nav = session.get("https://api.bilibili.com/x/web-interface/nav")
    require(nav.get("code") == 0 and nav.get("data", {}).get("isLogin") is True
            and nav["data"].get("mid") == request["account_mid"], "stage account login identity mismatch")
    return StageAdapter(session=session, http=http, view_url=upload.VIEW_API, tags_url=upload.TAGS_API,
        section_url=upload.SECTION_VIEW_API, plan=plan, biliup_bin=binary, lock_process=lock_process)


def is_stage_plan(path: Path) -> bool:
    try:
        return repair.load_plan(path).get("schema_version") == PLAN_SCHEMA
    except repair.PlanInvalid:
        # Keep the normal CLI's established error reporting/validation order.
        return False


def cli_plan(args) -> int:
    from scripts import authorized_upload as upload
    request_path = Path(args.stage_request).resolve()
    request = load_request(request_path)
    require(args.bvid == request["bvid"], "stage CLI BVID differs from request")
    require(not args.predecessor_completed and not args.preserve_existing_tags,
            "normal plan options do not apply to partial stage requests")
    journal = Path(args.journal).resolve()
    with upload.exclusive_upload_lock(journal.with_suffix(".lock")):
        live = adapter(args, request, plan={}).observe(request["bvid"], request["season"]["section_id"])
        plan = create_plan(request_path=request_path, snapshot=live)
        predecessor = plan.get("stage_predecessor") or {}
        repair.assert_bvid_unowned(journal, bvid=plan["bvid"], plan_id=plan["plan_id"],
            predecessor_plan_id=(predecessor.get("plan") or {}).get("plan_id"),
            predecessor_verified_row_sha256=(predecessor.get("verified_journal_row") or {}).get("row_sha256"))
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        plan_path = Path(args.out).resolve()
        repair.write_plan(plan_path, plan)
        repair.initialise_journal(journal, plan_path, plan)
    print(json.dumps({"status": "STAGE_PLANNED", "plan": str(plan_path), "remote_mutation": False,
                      "final_acceptance": False}, ensure_ascii=False))
    return 0


def cli_existing(args, command: str) -> int:
    from scripts import authorized_upload as upload
    plan_path, journal = Path(args.plan).resolve(), Path(args.journal).resolve()
    plan = repair.load_plan(plan_path)
    validate_plan(plan, plan_path=plan_path)
    request = load_request(Path(plan["manifest"]["path"]))
    with upload.exclusive_upload_lock(journal.with_suffix(".lock")):
        if command == "status":
            result = repair.repair_status(plan_path=plan_path, journal=journal, manifest=request)
        elif command == "run" and args.dry_run:
            # Read-only canary and fresh topology; never mutate even the journal.
            fresh = adapter(args, request, plan=plan).observe(plan["bvid"], plan["season"]["section_id"])
            result = repair.preview_repair(plan_path=plan_path, journal=journal, manifest=request)
            if result.state == "PLANNED":
                _projection(request, fresh)
            elif result.state != "BLOCKED_DRIFT":
                rows = repair.plan_entries(journal, plan_path, plan)
                new = repair._latest_detail(rows, "new_video")
                cover = repair._latest_detail(rows, "cover_url") or plan["replacement"]["cover"].get("preserve_url")
                require((new and repair._target_creator_exact(fresh, plan, new_video=new, cover_url=cover))
                        or (new and repair._pre_edit_video(fresh, plan, expected_new=new)
                            and repair._before_public_section_exact(fresh, plan))
                        or (result.state in {"APPEND_INTENT", "APPEND_AMBIGUOUS"}
                            and repair._before_creator_exact(fresh, plan)), "stage dry-run fresh topology drift")
            result.details["fresh_snapshot"] = fresh
        elif command == "run":
            with shared_upload_lock() as lock_process:
                live = adapter(args, request, plan=plan, lock_process=lock_process)
                result = repair.run_repair(plan_path=plan_path, journal=journal, manifest=request,
                    adapter=live, wait_seconds=args.wait, poll_seconds=args.poll)
        elif command == "verify":
            result = repair.repair_status(plan_path=plan_path, journal=journal, manifest=request)
            require(result.state == "VERIFIED", "stage must reach VERIFIED before fresh completion")
            fresh = adapter(args, request, plan=plan).observe(plan["bvid"], plan["season"]["section_id"])
            row = repair.plan_entries(journal, plan_path, plan)[-1]
            require(repair.snapshots_equivalent(fresh, row["details"].get("live_snapshot", {})),
                    "stage fresh verification drift")
            new, cover = row["details"]["new_video"], row["details"]["cover_url"]
            require(repair._target_creator_exact(fresh, plan, new_video=new, cover_url=cover)
                    and repair._public_section_state(fresh, plan, new_video=new, cover_url=cover)[0] == "target",
                    "stage fresh snapshot is not the exact approved target")
            completed = {"schema_version": COMPLETED_SCHEMA, "status": "STAGE_VERIFIED_FRESH_LIVE",
                "verified_at": repair._utc_now(), "plan": binding(plan_path), "request": plan["manifest"],
                "bvid": plan["bvid"], "aid": request["aid"], "candidate_id": request["candidate_id"],
                "new_cid": fresh["creator"]["videos"][0]["cid"],
                "verified_journal_row": {"path": str(journal), "row_sha256": row["row_sha256"]},
                "live_snapshot": fresh, "final_acceptance": False, "subtitles_complete": False,
                "remaining": plan["remaining"], "normal_publication_reconciliation": False}
            out = Path(args.out)
            if out.exists():
                existing = repair.load_plan(out)
                require({k: v for k, v in existing.items() if k != "verified_at"}
                        == {k: v for k, v in completed.items() if k != "verified_at"},
                        "existing stage completed receipt differs from fresh closure")
            else:
                repair.write_plan(out, completed)
        else:
            raise repair.PlanInvalid("unknown stage command")
    print(json.dumps({"state": "STAGE_VERIFIED" if result.state == "VERIFIED" else result.state,
        "changed": result.changed, "message": result.message, "details": result.details,
        "final_acceptance": False, "subtitles_complete": False, "remaining": plan["remaining"]}, ensure_ascii=False))
    return 0 if result.state == "VERIFIED" else 5 if result.state == "BLOCKED_DRIFT" else 6
