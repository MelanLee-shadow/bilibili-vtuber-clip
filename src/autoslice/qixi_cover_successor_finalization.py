"""Hash-bound, create-only finalizer for the sole Qixi cover successor."""
from __future__ import annotations
import copy, hashlib, json, os, shutil, stat, tempfile
from pathlib import Path
from typing import Any, Mapping
from src.autoslice.cover_text_pixel_evidence import verify_pre_overlay_route_background, verify_rendered_text_pixel_artifacts
from src.autoslice.qixi_cover_route_displacement import CANDIDATE_ID, QixiCoverRouteDisplacementError, load_authority, validate_provider_evidence
from src.autoslice.cover_font_paths import resolve_trusted_cover_font
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.story_contract import cover_story_contract_binding

RECEIPT = "qixi-cover-successor-finalization.json"
SCHEMA = "qixi-cover-successor-finalization-receipt.v1"
_TRIAL = {"final":"qixi-cpa-redraw.png", "background":"qixi-cpa-redraw.ai-bg.png", "pre":"qixi-cpa-redraw.pre-overlay.png", "mask":"qixi-cpa-redraw.title-mask.png", "generation":"qixi-cpa-redraw.cover_generation.json", "identity":"qixi-cpa-redraw.host-identity-witness.png", "no_text":"qixi-cpa-redraw.no-model-text-witness.json", "joint":"qixi-cpa-redraw.title-cover-joint-qc.json", "request":"qixi-cpa-redraw.cpa-request.redacted.json", "response":"qixi-cpa-redraw.cpa-response.redacted.json"}

class QixiCoverSuccessorError(ValueError): pass

def validate_applied_receipt(receipt: object, *, package_root: Path) -> dict[str, Any]:
    """Replay the successor's receipt against its current portable bytes."""
    if not isinstance(receipt, Mapping): raise QixiCoverSuccessorError("successor receipt is not an object")
    value=dict(receipt); required={"schema_version","mode","candidate_id","status","upload_allowed","ready_for_serial_upload","authority_sha256","preimage_noncover_sha256","final_cover_sha256","route_background_sha256","pre_overlay_sha256","title_mask_sha256","record_sha256","publish_sha256"}
    if set(value)!=required or value.get("schema_version")!=SCHEMA or value.get("mode")!="APPLIED" or value.get("candidate_id")!=CANDIDATE_ID or value.get("status")!="finished_review_package_no_upload_pending_human_review" or value.get("upload_allowed") is not False or value.get("ready_for_serial_upload")!="NO_WAITING_FINAL_HUMAN_REVIEW": raise QixiCoverSuccessorError("successor receipt schema/status invalid")
    package_root=_root(package_root,"package")
    for name,key in ((f"{CANDIDATE_ID}.cover.png","final_cover_sha256"),(f"{CANDIDATE_ID}.cover.route-background.png","route_background_sha256"),(f"{CANDIDATE_ID}.cover.pre-overlay.png","pre_overlay_sha256"),(f"{CANDIDATE_ID}.cover.title-mask.png","title_mask_sha256"),(f"{CANDIDATE_ID}.record.json","record_sha256"),(f"{CANDIDATE_ID}.publish.json","publish_sha256")):
        if _sha(_regular(package_root,name,name))!=value.get(key): raise QixiCoverSuccessorError(f"successor receipt {name} drifts")
    return value

def _sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1<<20), b""): h.update(chunk)
    return "sha256:"+h.hexdigest()
def _canon(value: object) -> str: return "sha256:"+hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def _root(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir(): raise QixiCoverSuccessorError(f"{label} must be an absolute non-symlink directory")
    return path.resolve(strict=True)
def _regular(root: Path, relative: str, label: str) -> Path:
    rel=Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {".",".."} for p in rel.parts): raise QixiCoverSuccessorError(f"{label} path is unsafe")
    p=root
    for part in rel.parts:
        p/=part
        try: mode=os.lstat(p).st_mode
        except OSError as exc: raise QixiCoverSuccessorError(f"{label} unavailable") from exc
        if stat.S_ISLNK(mode): raise QixiCoverSuccessorError(f"{label} contains a symlink")
    if not p.is_file(): raise QixiCoverSuccessorError(f"{label} is not a regular file")
    return p.resolve(strict=True)
def _json(path: Path, label: str) -> dict[str,Any]:
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError) as exc: raise QixiCoverSuccessorError(f"{label} unreadable JSON") from exc
    if not isinstance(value,dict): raise QixiCoverSuccessorError(f"{label} is not an object")
    return value
def _write(path: Path,value: Mapping[str,object]) -> None:
    tmp=path.with_name("."+path.name+".tmp")
    with tmp.open("w",encoding="utf-8") as f: json.dump(value,f,ensure_ascii=False,indent=2,sort_keys=True); f.write("\n"); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
def _fsync(path: Path) -> None:
    d=os.open(path,os.O_RDONLY)
    try: os.fsync(d)
    finally: os.close(d)
def _snapshot(root: Path) -> dict[str,str]:
    ignored={f"replacement_recuts/{CANDIDATE_ID}.cover.png",f"replacement_recuts/{CANDIDATE_ID}.cover.pre-overlay.png",f"replacement_recuts/{CANDIDATE_ID}.cover.route-background.png",f"replacement_recuts/{CANDIDATE_ID}.cover.title-mask.png",f"replacement_recuts/{CANDIDATE_ID}.record.json",f"replacement_recuts/{CANDIDATE_ID}.publish.json",f"replacement_recuts/{RECEIPT}","replacement_recuts/review_manifest.json",f"replacement_recuts/{CANDIDATE_ID}.package-audit.json"}
    out={}
    for p in sorted(root.rglob("*")):
        if p.is_symlink(): raise QixiCoverSuccessorError("preimage contains symlink")
        if p.is_file() and (r:=p.relative_to(root).as_posix()) not in ignored and not r.startswith("replacement_recuts/evidence/qixi-cover-successor/"): out[r]=_sha(p)
    return out

def _verify_trial(root: Path, authority: Mapping[str,Any]) -> tuple[dict[str,Path],dict[str,Any]]:
    files={k:_regular(root,v,f"trial {k}") for k,v in _TRIAL.items()}; r=authority["replacement"]
    expected={"final":r["final_cover_sha256"],"background":r["route_background_sha256"],"pre":r["pre_overlay_sha256"],"mask":r["title_mask_sha256"]}
    if any(_sha(files[k])!=v for k,v in expected.items()): raise QixiCoverSuccessorError("trial PNG hash drift")
    gen=_json(files["generation"],"trial generation"); no=_json(files["no_text"],"no-text witness"); joint=_json(files["joint"],"joint QC")
    identity=gen.get("final_host_identity_verification"); pixels=gen.get("rendered_text_pixels")
    if not (gen.get("candidate_id")==CANDIDATE_ID and gen.get("final_cover_sha256")==expected["final"] and gen.get("ai_background_sha256")==expected["background"] and gen.get("pre_overlay_sha256")==expected["pre"] and gen.get("rendered_lines")==r["required_title_lines"] and isinstance(identity,Mapping) and identity.get("status")=="PASS" and identity.get("final_cover_sha256")==expected["final"] and isinstance(pixels,Mapping) and pixels.get("status")=="PASS" and pixels.get("mask_sha256")==expected["mask"] and no.get("schema_version")=="cpa-frame-witness.v1" and no.get("status")=="OBSERVED" and no.get("image_sha256")==expected["pre"][7:] and '"has_readable_text":false' in str(no.get("answer")) and '"text_fragments":[]' in str(no.get("answer")) and joint.get("status")=="PASS" and joint.get("pass") is True and joint.get("cover_sha256")==expected["final"] and isinstance(joint.get("verdict"),Mapping) and joint["verdict"].get("unrelated_or_misleading_elements")==[]): raise QixiCoverSuccessorError("trial identity/no-text/joint-QC invalid")
    return files,gen

def _generation(original: Mapping[str,Any], trial: Mapping[str,Any], package: Path, repo: Path, locator_package: Path | None = None) -> dict[str,Any]:
    g=copy.deepcopy(dict(trial)); final=package/f"{CANDIDATE_ID}.cover.png"; bg=package/f"{CANDIDATE_ID}.cover.route-background.png"; pre=package/f"{CANDIDATE_ID}.cover.pre-overlay.png"; mask=package/f"{CANDIDATE_ID}.cover.title-mask.png"; evidence=package/"evidence/qixi-cover-successor"
    g.update({"cover_origin":"AI_REDRAW","cover_status":"AI_COVER_READY","cover_treatment":{"treatment":"cpa_redraw","reason":"sealed Qixi displacement authority"},"method":"images.edit","image_gen_model":"cpa","image_generation_planned":True,"image_generation_attempted":True,"image_generation_used":True,"final_cover":str(final),"final_cover_sha256":_sha(final),"ai_background":str(bg),"ai_background_sha256":_sha(bg),"pre_overlay_path":str(pre),"pre_overlay_sha256":_sha(pre),"reference_image":original.get("reference_image"),"reference_sha256":original.get("reference_sha256"),"request_path":str(evidence/_TRIAL["request"]),"response_path":str(evidence/_TRIAL["response"])})
    pixels=g.get("rendered_text_pixels")
    if not isinstance(pixels,dict): raise QixiCoverSuccessorError("rendered text evidence missing")
    pixels["mask_path"]=str(mask); pixels["pre_overlay_path"]=str(pre)
    ident=g.get("final_host_identity_verification")
    if isinstance(ident,dict):
        ident["final_cover_path"]=str(final); ident["comparison_path"]=str(evidence/_TRIAL["identity"])
        if isinstance(ident.get("witness"),dict): ident["witness"]["image_path"]=str(evidence/_TRIAL["identity"])
    route=copy.deepcopy(original.get("route_decision"))
    if not isinstance(route,dict) or route.get("schema_version")!="lidousha-cover-route-decision.v2": raise QixiCoverSuccessorError("predecessor route decision missing")
    reason="sealed Qixi displacement: screenshot poster repair retained classroom narrative; CPA redraw passed identity, no-model-text, and joint-QC"
    rejected={
        "screenshot_direct": reason+"; direct cannot repair the failed title/cover narrative",
        "screenshot_polish": reason+"; canonical route-preserving repair e3fa419daeb5e240c8ada13338453879896a0ad6ed42b82ac73f1dd5cc21d2cf retained classroom narrative",
    }
    route.update({"selected_treatment":"cpa_redraw","actual_treatment":"cpa_redraw","reason":reason,"selected_rationale":reason,"execution_status":"READY","image_generation_planned":True,"image_generation_attempted":True,"image_generation_used":True,"alternatives":[{"treatment":"screenshot_direct","status":"REJECTED","rationale":reason,"per_frame_evidence":reason,"rejected_reason":rejected["screenshot_direct"]},{"treatment":"screenshot_polish","status":"REJECTED","rationale":reason,"per_frame_evidence":reason,"rejected_reason":rejected["screenshot_polish"]},{"treatment":"cpa_redraw","status":"SELECTED","rationale":reason,"per_frame_evidence":reason,"rejected_reason":None}],"rejected_alternatives":[{"treatment":k,"rejected_reason":v} for k,v in rejected.items()],"qixi_route_displacement":{"schema_version":"qixi-cover-route-displacement.v1","predecessor_selected_treatment":"screenshot_polish"}});g["route_decision"]=route
    font=resolve_trusted_cover_font(file_name=str(pixels.get("font_file_name") or ""),expected_sha256=str(pixels.get("font_file_sha256") or ""),channel_profile=load_channel_profile(repo),root=repo)
    if not verify_rendered_text_pixel_artifacts(pixels,final_cover_path=final,pre_overlay_path=pre,mask_path=mask,font_path=font,expected_pre_overlay_sha256=_sha(pre)): raise QixiCoverSuccessorError("title-mask recomposition fails")
    if not verify_pre_overlay_route_background(route_background_path=bg,pre_overlay_path=pre,expected_route_background_sha256=_sha(bg),text_backing=g.get("text_backing"),scrim=g.get("scrim")): raise QixiCoverSuccessorError("background recomposition fails")
    bbox=pixels.get("text_pixel_bbox")
    if not (isinstance(bbox,list) and len(bbox)==4 and 260<=bbox[0]<=bbox[2]<=1660): raise QixiCoverSuccessorError("feed-safe bbox fails")
    locator = locator_package or package
    g["final_cover"] = str(locator / f"{CANDIDATE_ID}.cover.png")
    g["ai_background"] = str(locator / f"{CANDIDATE_ID}.cover.route-background.png")
    g["pre_overlay_path"] = str(locator / f"{CANDIDATE_ID}.cover.pre-overlay.png")
    pixels["mask_path"] = str(locator / f"{CANDIDATE_ID}.cover.title-mask.png")
    pixels["pre_overlay_path"] = g["pre_overlay_path"]
    g["request_path"] = str(locator / "evidence/qixi-cover-successor" / _TRIAL["request"])
    g["response_path"] = str(locator / "evidence/qixi-cover-successor" / _TRIAL["response"])
    if isinstance(ident, dict):
        ident["final_cover_path"] = g["final_cover"]
        ident["comparison_path"] = str(locator / "evidence/qixi-cover-successor" / _TRIAL["identity"])
        if isinstance(ident.get("witness"), dict): ident["witness"]["image_path"] = ident["comparison_path"]
    return g

def finalize(*,repo_root:Path,preimage:Path,trial_root:Path,target:Path,apply:bool)->dict[str,Any]:
    repo=_root(repo_root,"repo"); preimage=_root(preimage,"preimage"); trial_root=_root(trial_root,"trial")
    if not target.is_absolute() or target.exists() or target.is_symlink(): raise QixiCoverSuccessorError("target is invalid")
    try: authority=load_authority(repo); validate_provider_evidence(authority=authority,bundle_root=repo)
    except QixiCoverRouteDisplacementError as exc: raise QixiCoverSuccessorError(str(exc)) from exc
    files,trial=_verify_trial(trial_root,authority); before=_snapshot(preimage); package=preimage/"replacement_recuts"; record=_json(_regular(package,f"{CANDIDATE_ID}.record.json","record"),"record"); publish=_json(_regular(package,f"{CANDIDATE_ID}.publish.json","publish"),"publish"); staging=record.get("publish_staging"); original=staging.get("cover_generation") if isinstance(staging,Mapping) else None
    if not isinstance(staging,dict) or not isinstance(original,Mapping) or record.get("story_contract",{}).get("candidate_id")!=CANDIDATE_ID or _sha(package/f"{CANDIDATE_ID}.cover.png")!=authority["predecessor"]["previous_cover_sha256"]: raise QixiCoverSuccessorError("predecessor record/source/cover drifts")
    dry={"schema_version":SCHEMA,"mode":"DRY_RUN","candidate_id":CANDIDATE_ID,"upload_allowed":False,"preimage_noncover_sha256":_canon(before),"replacement_final_cover_sha256":authority["replacement"]["final_cover_sha256"]}
    if not apply:return dry
    target.parent.mkdir(parents=True,exist_ok=True); stage=Path(tempfile.mkdtemp(prefix=".qixi-cover-successor-",dir=target.parent))
    try:
        candidate=stage/CANDIDATE_ID;shutil.copytree(preimage,candidate,copy_function=shutil.copy2);out=candidate/"replacement_recuts";evidence=out/"evidence/qixi-cover-successor";evidence.mkdir(parents=True)
        for src,dst in {files["final"]:out/f"{CANDIDATE_ID}.cover.png",files["background"]:out/f"{CANDIDATE_ID}.cover.route-background.png",files["pre"]:out/f"{CANDIDATE_ID}.cover.pre-overlay.png",files["mask"]:out/f"{CANDIDATE_ID}.cover.title-mask.png"}.items():shutil.copy2(src,dst)
        for k in ("generation","identity","no_text","joint","request","response"):shutil.copy2(files[k],evidence/_TRIAL[k])
        rp=out/f"{CANDIDATE_ID}.record.json";pp=out/f"{CANDIDATE_ID}.publish.json";cur=_json(rp,"staged record");gen=_generation(original,trial,out,repo,target/"replacement_recuts");story=cur.get("story_contract");
        if not isinstance(story,dict): raise QixiCoverSuccessorError("story contract missing")
        gen["story_contract"]=cover_story_contract_binding(story);cur["publish_staging"]["cover_generation"]=gen;cur["publish_staging"]["cover_text"]=gen["cover_text"];cur["publish_staging"]["cover_path"]=str(target/"replacement_recuts"/f"{CANDIDATE_ID}.cover.png");cur["publish_staging"]["cover_status"]="AI_COVER_READY";cur["cover_generation"]=gen;cur["cover_path"]=cur["publish_staging"]["cover_path"];cur["cover_status"]="AI_COVER_READY";hashes=cur.get("artifact_hashes")
        if not isinstance(hashes,dict):raise QixiCoverSuccessorError("artifact hashes missing")
        hashes["cover_sha256"]=_sha(out/f"{CANDIDATE_ID}.cover.png");hashes["ai_background_sha256"]=_sha(out/f"{CANDIDATE_ID}.cover.route-background.png");cur["qixi_cover_successor"]={"schema_version":"qixi-cover-successor.v1","authority_sha256":authority["authority_sha256"],"predecessor_cover_sha256":authority["predecessor"]["previous_cover_sha256"],"failed_joint_qc_sha256":authority["predecessor"]["failed_joint_qc_sha256"],"route_preserving_attempt":authority["route_preserving_attempt"],"provider_evidence":{k:_sha(evidence/_TRIAL[k]) for k in ("generation","identity","no_text","joint","request","response")},"upload_allowed":False};_write(rp,cur)
        pub=_json(pp,"staged publish");pub["cover_generation"]=gen;pub["cover_text"]=gen["cover_text"];pub["cover_path"]=str(target/"replacement_recuts"/f"{CANDIDATE_ID}.cover.png");pub["cover_status"]="AI_COVER_READY";
        if isinstance(pub.get("artifact_hashes"),dict):pub["artifact_hashes"]["cover_sha256"]=hashes["cover_sha256"];pub["artifact_hashes"]["ai_background_sha256"]=hashes["ai_background_sha256"]
        _write(pp,pub)
        hashes["publish_draft_sha256"]=_sha(pp);_write(rp,cur)
        if before!=_snapshot(candidate):raise QixiCoverSuccessorError("noncover preimage bytes drift")
        receipt={"schema_version":SCHEMA,"mode":"APPLIED","candidate_id":CANDIDATE_ID,"status":"finished_review_package_no_upload_pending_human_review","upload_allowed":False,"ready_for_serial_upload":"NO_WAITING_FINAL_HUMAN_REVIEW","authority_sha256":authority["authority_sha256"],"preimage_noncover_sha256":_canon(before),"final_cover_sha256":hashes["cover_sha256"],"route_background_sha256":hashes["ai_background_sha256"],"pre_overlay_sha256":gen["pre_overlay_sha256"],"title_mask_sha256":gen["rendered_text_pixels"]["mask_sha256"],"record_sha256":_sha(rp),"publish_sha256":_sha(pp)};_write(out/RECEIPT,receipt);_fsync(out);os.replace(candidate,target);_fsync(target.parent);return receipt|{"target":str(target),"package_root":str(target/"replacement_recuts")}
    finally:shutil.rmtree(stage,ignore_errors=True)
