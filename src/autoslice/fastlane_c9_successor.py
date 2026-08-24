"""C9-only successor package chain for the accepted watched-video correction."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


CID = "auto_143025_1112_1285"
DATE = "2026-08-15"
OLD_SUBTITLE = "sha256:4b0402dafbffbc9cf3358f21ff09a3d4aa07c052978848b70cc6b860844c2ac1"
OBSERVED_SUBTITLE = "sha256:3327cb186ba44785df255c544e5e7ac1ce989a3872ccb91958a7b43ba77823c7"
SOURCE_SRT = "sha256:65ae7dfd66348c4af7a8dbc78b1f196bd4cf354c181416a03fa346b9b4cab069"
REVIEWED_SRT = "sha256:f436e1913b8eb2bd948c37b18bce9c2a9970dd9be07cf114f309a9c162c2b51b"


class FastlaneC9SuccessorError(ValueError): pass

def _sha(p: Path) -> str: return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
def _canon(v: object) -> bytes: return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

def materialize_c9_successor(*, repo_root: Path, snapshot: Path, out: Path) -> Path:
    """Create only a local, no-upload successor package from exact snapshot bytes."""
    snapshot, out = Path(snapshot), Path(out)
    if out.exists(): raise FastlaneC9SuccessorError("C9_SUCCESSOR_OUTPUT_EXISTS")
    manifest = json.loads((snapshot / "artifact-manifest.json").read_text())
    listed = {row["path"]: row["sha256"] for row in manifest["local_files"]}
    required = ("current.record.json", "current.publish.json", "current.cover.png", "current.clip-context.json", "preview.recut.mp4")
    if any(_sha(snapshot / name) != listed.get(name) for name in required):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_SNAPSHOT_HASH_DRIFT")
    if _sha(snapshot / "current.srt") != OBSERVED_SUBTITLE:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_OBSERVED_DRIFT_CHANGED")
    record = json.loads((snapshot / "current.record.json").read_text())
    if record.get("artifact_hashes", {}).get("subtitle_sha256") != OLD_SUBTITLE:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_PREDECESSOR_RECORD_INVALID")
    base = Path(repo_root) / "assets/lidousha/fastlane_c9_private"
    if _sha(base / f"{CID}.source.speaker-final.srt") != SOURCE_SRT or _sha(base / f"{CID}.reviewed.srt") != REVIEWED_SRT:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_ACCEPTED_TEXT_DRIFT")
    out.mkdir(parents=True)
    for source, target in ((snapshot / "preview.recut.mp4", "video.mp4"), (snapshot / "current.cover.png", "cover.png"), (snapshot / "current.publish.json", "publish.json"), (snapshot / "current.clip-context.json", "clip-context.json"), (base / f"{CID}.reviewed.srt", "reviewed.srt"), (base / f"{CID}.source.speaker-final.srt", "source.speaker-final.srt"), (base / f"{CID}.foreign-video-source-action.v1.json", "source-action.json"), (base / f"{CID}.root-acceptance-envelope.v1.json", "acceptance-envelope.json")):
        shutil.copy2(source, out / target)
    successor = {"schema_version":"fastlane-c9-successor-chain.v1","candidate_id":CID,"recording_date":DATE,"upload_allowed":False,"predecessor":{"record_sha256":_sha(snapshot / "current.record.json"),"declared_subtitle_sha256":OLD_SUBTITLE,"observed_current_srt_sha256":OBSERVED_SUBTITLE,"status":"SUBTITLE_BYTES_MISSING_DRIFT_EXPLICIT"},"target":{"source_srt_sha256":SOURCE_SRT,"reviewed_srt_sha256":REVIEWED_SRT,"video_sha256":_sha(out / "video.mp4"),"publish_sha256":_sha(out / "publish.json"),"cover_sha256":_sha(out / "cover.png"),"clip_context_sha256":_sha(out / "clip-context.json"),"frozen_boundary_audit":record["boundary_audit"]},"typed_blocker":"C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE"}
    successor["self_sha256"]="sha256:"+hashlib.sha256(_canon(successor)).hexdigest()
    (out / "successor-record.json").write_text(json.dumps(successor,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    review={"schema_version":"fastlane-c9-private-successor-review.v1","candidate_id":CID,"run_mode":"MANUAL_PRODUCE_REVIEW","upload_allowed":False,"successor_record":"successor-record.json","typed_blocker":successor["typed_blocker"]}
    (out / "review_manifest.json").write_text(json.dumps(review,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    return out

def audit_c9_successor(root: Path, manifest: dict[str, Any]) -> list[dict[str, str]] | None:
    if manifest.get("schema_version") != "fastlane-c9-private-successor-review.v1": return None
    issues: list[dict[str, str]]=[]
    path=Path(root)/"successor-record.json"
    try: doc=json.loads(path.read_text())
    except Exception: return [{"code":"C9_SUCCESSOR_RECORD_UNAVAILABLE","severity":"BLOCK"}]
    unsigned=dict(doc); declared=unsigned.pop("self_sha256",None)
    if declared != "sha256:"+hashlib.sha256(_canon(unsigned)).hexdigest(): issues.append({"code":"C9_SUCCESSOR_RECORD_HASH_DRIFT","severity":"BLOCK"})
    if doc.get("candidate_id") != CID or doc.get("typed_blocker") != "C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE": issues.append({"code":"C9_SUCCESSOR_SCOPE_INVALID","severity":"BLOCK"})
    if doc.get("predecessor",{}).get("declared_subtitle_sha256") != OLD_SUBTITLE or doc.get("predecessor",{}).get("observed_current_srt_sha256") != OBSERVED_SUBTITLE: issues.append({"code":"C9_SUCCESSOR_PREDECESSOR_DRIFT_INVALID","severity":"BLOCK"})
    if doc.get("target",{}).get("reviewed_srt_sha256") != REVIEWED_SRT or doc.get("target",{}).get("source_srt_sha256") != SOURCE_SRT: issues.append({"code":"C9_SUCCESSOR_TARGET_BINDING_INVALID","severity":"BLOCK"})
    if not issues: issues.append({"code":"C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE","severity":"BLOCK"})
    return issues
