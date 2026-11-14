"""Recent title context for the editor; never publication authority or a naming quota.

It is also the lane's one reader of the publication registry and of the first-publication receipt the registry row binds
(both are root-only files on the media host, where the lane runs as an ordinary user): every question the lane asks of
them (is this manuscript published, what are the published titles, the same-BV repair authority of a published manuscript
and the hashes its first publication recorded) is a `HistoryRequest` answered by `serve`.  `through_boundary` runs `serve`
here when this process can read, and on the media host under `sudo -n` (this file run as a script) when it cannot or when
the lane itself is remote; only the answer comes back, never the registry."""

from __future__ import annotations

import json
import re
import shlex
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if __name__ == "__main__":
    sys.path.insert(0, sys.argv[2])

from src.autoslice.manual_lane.publication_id import publication_cid
from src.autoslice.publication_registry import load_publication_registry
from src.autoslice.channel_envelope import parse_talk_title

if TYPE_CHECKING:
    from typing import NotRequired      # this file also runs as a script on the media host: no runtime need

    from .runner import Ctx

# Existing forms in skill section 四; this is recognition, not a scene-to-name mapping or quota.
_NAME_FORMS = re.compile(r"李豆沙|小李|熊猫头|李姐|(?<![李礼室悠璃恬晚])豆沙(?!歌)|(?i:\blds\b)|白色奶龙")


def title_names(title: str) -> tuple[str, ...]:
    envelope = parse_talk_title(title)
    body = title[len(envelope.prefix):] if envelope else title
    return tuple(dict.fromkeys(m.group() for m in _NAME_FORMS.finditer(body)))


class HistoryRequest(TypedDict):
    """What the media-host reader is asked about the manuscript `candidate_id`: the titles of its neighbours (`limit`), or
    with `check_published` only whether it is published, or with `build_authority` (the successor's package name and final
    title) its same-BV repair authority, or with `first_digests` (an authority built before) the sha256 of the video and the
    cover online now and the latest completion that changed the video, or with `online_tags` the final tags online now, or with `used_packages` the packages that went online for it (receipt_chain.used_packages), or with `title_receipt` (the receipt's path, the package name and the title it
    must give) the full replay of that title-only receipt against the manuscript's first publication (`verify_title_receipt`), or with
    `registry_row` the published row of the manuscript as `published_row` projects it."""
    candidate_id: str
    limit: NotRequired[int]
    check_published: NotRequired[bool]
    build_authority: NotRequired[dict[str, str]]
    first_digests: NotRequired[dict[str, Any]]
    online_tags: NotRequired[bool]
    used_packages: NotRequired[bool]
    title_receipt: NotRequired[dict[str, str]]
    registry_row: NotRequired[bool]


@dataclass(frozen=True)
class TitleHistory:
    published: tuple[str, ...]
    batch: tuple[str, ...]
    notes: tuple[str, ...]
    published_titles: tuple[str, ...] = ()

    def prompt(self) -> str:
        return "\n".join([
            "## 最近标题的称呼（仅用于轮换，不是本片事实；从旧到新）",
            "按 skill 第四节结合场景选已有称呼；也看最近和本批的用法，避免相邻连续使用同一个称呼。",
            "小李、李豆沙、熊猫头都是已有称呼，应在适合的场景来回换；不要因不默认小李而一直避用它。",
            "一条仍只用一种，主语清楚可不写名字；不为了轮换编造内容，不改引语中的称呼。",
            "已发：\n" + ("\n".join(f"- {t}" for t in self.published) or "无可读标题"),
            "本批已起：\n" + ("\n".join(f"- {t}" for t in self.batch) or "暂无"),
        ])


def same_clip(registry_id: str, candidate_id: str) -> bool:
    """The registry names a lane clip by its publication id `fm-<YYYYMMDD>-<slug>`, the lane by its clip id: one
    clip.  A clip published once and made again (a same-BV replacement) must not meet its own title as a duplicate.  A
    trimmed successor `<cid>_trim` is the same manuscript as `<cid>`: its titles are its original's."""
    candidate_id = publication_cid(candidate_id)
    slug = re.escape(candidate_id.lower().replace("_", "-"))
    return registry_id == candidate_id or re.fullmatch(rf"fm-\d{{8}}-{slug}", registry_id) is not None


def published_titles(candidate_id: str, *, limit: int = 12) -> tuple[list[str], str]:
    """Read only published titles, excluding this candidate; no other registry fields leave this process."""
    published: list[tuple[str, int, str]] = []
    missing = 0
    for i, row in enumerate(load_publication_registry()["entries"]):
        if row["status"] != "published" or same_clip(str(row["candidate_id"]), candidate_id):
            continue
        reconciliation = row.get("publication_reconciliation") or {}
        title = row.get("title") or reconciliation.get("title")
        if not title:
            missing += 1
            continue
        stamp = (reconciliation.get("published_at") or reconciliation.get("reconciled_at")
                 or row.get("recording_date") or "")
        published.append((str(stamp), i, str(title)))
    return [t for _, _, t in sorted(published)], f"已发标题：可读 {len(published)} 条，登记缺标题 {missing} 条"


def _remote_read(ctx: Ctx, request_data: HistoryRequest, call: str) -> object:
    """Run this file on the media host with the lane's root boundary, on `request_data`; returns the JSON it printed."""
    from .adapters import REMOTE_PY, REMOTE_REPO
    from .step_io import _run_remote

    script = f"{ctx.remote_dir}/title/history_reader.py"
    request = f"{ctx.remote_dir}/title/history_request.json"
    with tempfile.TemporaryDirectory() as scratch:        # the request is not left in the clip's own tree
        local = Path(scratch) / "history_request.json"
        local.write_text(json.dumps(request_data), encoding="utf-8")
        ctx.adapters.remote.put(Path(__file__), script)
        ctx.adapters.remote.put(local, request)
    command = " ".join(shlex.quote(part) for part in ("sudo", "-n", REMOTE_PY, "-B", script, request, REMOTE_REPO))
    result = _run_remote(ctx, call, command + "\n", timeout=120)
    if result.rc:
        raise RuntimeError(f"registry projection failed: rc={result.rc}: {result.stderr[-300:]}")
    return json.loads(result.stdout)


def _unreadable(exc: BaseException | None) -> bool:
    """Whether `exc` is, or was raised out of, an OSError: the reader could not read a file (a reader that wraps what it met keeps
    it as the cause).  A root-only file is refused as PermissionError by one reader, but another reports it as a plain OSError
    (evidence_identity.file_sha256: 'unreadable, non-regular or changed') or wraps it in a ValueError, so every OSError counts."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, OSError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def read_through_boundary(ctx: Ctx, request_data: HistoryRequest, call: str) -> tuple[object, bool]:
    """(the answer to `request_data` (what `serve` returns), whether it came through the root boundary): read by this process
    when it can, else on the media host through the root boundary (`call` names the remote script); always there when the
    lane's remote is ssh.  RuntimeError naming both errors when this process could not read (any OSError of reading a file of
    the registry or the receipt chain, however it is reported) and the boundary failed too (a file that does not exist is not
    found there either); any other error of reading here is raised as it is (no root read could change it)."""
    from .adapters import SshRemote

    if isinstance(ctx.adapters.remote, SshRemote):
        return _remote_read(ctx, request_data, call), True
    try:
        return serve(request_data), False
    except (OSError, ValueError) as original:
        if not _unreadable(original):
            raise
        try:
            return _remote_read(ctx, request_data, call), True
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"{type(original).__name__}: {original}; projection: {exc}") from exc


def through_boundary(ctx: Ctx, request_data: HistoryRequest, call: str) -> object:
    """The answer of `read_through_boundary`, without where it was read."""
    return read_through_boundary(ctx, request_data, call)[0]


def _titles(data: object) -> tuple[list[str], str]:
    if not isinstance(data, dict) or not isinstance(data.get("titles"), list) or not all(
            isinstance(t, str) for t in data["titles"]) or not isinstance(data.get("note"), str):
        raise ValueError("PUBLISHED_TITLE_PROJECTION_INVALID")
    return data["titles"], data["note"]


def is_published(candidate_id: str) -> bool:
    """Whether the registry holds a `published` row for this manuscript (same_clip); the only thing it answers.  Raises
    when the registry cannot be read."""
    return any(row["status"] == "published" and same_clip(str(row["candidate_id"]), candidate_id)
               for row in load_publication_registry()["entries"])


def published_status(ctx: Ctx, candidate_id: str) -> bool:
    """`is_published` where the registry is readable (`through_boundary`).  OSError / ValueError / RuntimeError when it cannot
    be read: the caller must not take that for 'no'."""
    data = through_boundary(ctx, {"candidate_id": candidate_id, "check_published": True}, "s8-published-status")
    published = data.get("published") if isinstance(data, dict) else None
    if not isinstance(published, bool):
        raise ValueError("PUBLISHED_STATUS_PROJECTION_INVALID")
    return published


def published_row(ctx: Ctx, manuscript: str) -> dict[str, Any] | None:
    """The registry row of the published `manuscript` as {status, bvid, recording_date, reconciliation_status} (`through_boundary`), or None
    when it has none.  OSError / ValueError / RuntimeError when the registry cannot be read: the caller must not take that for 'none'."""
    data = through_boundary(ctx, {"candidate_id": manuscript, "registry_row": True}, "s10-registry-row")
    row = data.get("row") if isinstance(data, dict) else None
    if row is not None and not (isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("status", "bvid"))):
        raise ValueError("REGISTRY_ROW_PROJECTION_INVALID")
    return row


def _row_projection(manuscript: str) -> dict[str, Any] | None:
    rows = [r for r in load_publication_registry()["entries"]
            if r["status"] == "published" and same_clip(str(r["candidate_id"]), manuscript)]
    if len(rows) > 1:
        raise ValueError(f"the registry holds {len(rows)} published rows for {manuscript} (登记里要有且只有一条)")
    if not rows:
        return None
    row = rows[0]
    return {"status": row["status"], "bvid": str(row.get("bvid") or ""), "recording_date": str(row.get("recording_date") or ""),
            "reconciliation_status": str((row.get("publication_reconciliation") or {}).get("status") or "")}


def bound_receipt(manuscript: str) -> dict[str, Any]:
    """The {path, sha256, bytes} binding of the receipt the registry row of the published manuscript carries: the
    first-publication receipt, or after a repair the completion receipt of the latest one (receipt_chain walks back from it)."""
    rows = [r for r in load_publication_registry()["entries"]
            if r["status"] == "published" and same_clip(str(r["candidate_id"]), manuscript)]
    if len(rows) != 1:
        raise ValueError(f"the registry holds {len(rows)} published rows for {manuscript} (登记里要有且只有一条)")
    binding = (rows[0].get("publication_reconciliation") or {}).get("authority")
    if not isinstance(binding, dict) or not binding.get("path"):
        raise ValueError(f"the registry row of {manuscript} binds no first-publication receipt (登记行没有首发回执)")
    return binding


def used_packages(ctx: Ctx, manuscript: str) -> list[dict[str, Any]]:
    """The packages that went online for the published `manuscript` (the first publication's and each completed repair's), each
    {package_root, review_manifest: {path, sha256} | None}, read the chain's way (`through_boundary`).  OSError / ValueError /
    RuntimeError when the registry or the chain cannot be read: the caller must not take that for 'none'."""
    data = through_boundary(ctx, {"candidate_id": manuscript, "used_packages": True}, "s8-used-packages")
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list) or not packages or not all(
            isinstance(p, dict) and isinstance(p.get("package_root"), str) and (
                p.get("review_manifest") is None or (isinstance(p["review_manifest"], dict) and all(
                    isinstance(p["review_manifest"].get(k), str) for k in ("path", "sha256")))) for p in packages):
        raise ValueError("USED_PACKAGES_PROJECTION_INVALID")
    return packages


def build_authority(ctx: Ctx, manuscript: str, *, package: str, title: str) -> tuple[dict[str, Any], bool]:
    """(the same-BV repair authority of the successor package `package` (final title `title`) of the published `manuscript`,
    whether its first-publication closure was replayed and checked inside the root boundary rather than by this process):
    built from the receipt its registry row binds (`read_through_boundary`)."""
    data, via_boundary = read_through_boundary(
        ctx, {"candidate_id": manuscript, "build_authority": {"package": package, "title": title}}, "s8-first-publication")
    authority = data.get("authority") if isinstance(data, dict) else None
    if not isinstance(authority, dict):
        raise ValueError("FIRST_PUBLICATION_PROJECTION_INVALID")
    return authority, via_boundary


def online_version(ctx: Ctx, manuscript: str, authority: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]:
    """(video sha256, cover sha256 of the version online now, the latest completion that changed the video as {path, new_cid, authority_equal} or
    None, the {tags, desc} online now or None when the chain does not say), after replaying the closure of `authority` (`through_boundary`).  Online now is the first publication's until a
    repair completed (receipt_chain)."""
    data = through_boundary(ctx, {"candidate_id": manuscript, "first_digests": authority}, "s8-first-digests")
    digests = data.get("digests") if isinstance(data, dict) else None
    if not isinstance(digests, dict) or not all(isinstance(digests.get(k), str) for k in ("video_sha256", "cover_sha256")):
        raise ValueError("FIRST_PUBLICATION_PROJECTION_INVALID")
    predecessor = data.get("predecessor") if isinstance(data, dict) else None
    if predecessor is not None and not (isinstance(predecessor, dict) and isinstance(predecessor.get("path"), str)
                                        and type(predecessor.get("new_cid")) is int
                                        and type(predecessor.get("authority_equal")) is bool):
        raise ValueError("FIRST_PUBLICATION_PROJECTION_INVALID")
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if metadata is not None and not (isinstance(metadata, dict) and isinstance(metadata.get("tags"), list)):
        raise ValueError("FIRST_PUBLICATION_PROJECTION_INVALID")
    return digests["video_sha256"], digests["cover_sha256"], predecessor, metadata


def online_tags(ctx: Ctx, manuscript: str) -> list[str]:
    """The final tags the published `manuscript` carries online now (`through_boundary`): the latest completion's, else the first
    publication's.  A successor keeps them: a revision changes what it is for, not the tags."""
    data = through_boundary(ctx, {"candidate_id": manuscript, "online_tags": True}, "s7-online-tags")
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t for t in tags):
        raise ValueError("ONLINE_TAGS_PROJECTION_INVALID")
    return tags


def verify_title_receipt(ctx: Ctx, manuscript: str, *, package: str, title: str, path: str) -> tuple[dict[str, Any], bool]:
    """(the answer of the full replay of the `VERIFIED_TITLE_ONLY` receipt at `path` as the edit that gave the manuscript the title
    `title`, whether it was made inside the root boundary), `through_boundary`'s way.  The answer says `refused` (why) or carries
    `receipt` ({path, sha256}), `content_b64` (the receipt's bytes: this process may not be able to read the file), `bvid`,
    `final_title`.  RuntimeError / OSError / ValueError as for the other questions."""
    data, via_boundary = read_through_boundary(
        ctx, {"candidate_id": manuscript, "title_receipt": {"path": path, "package": package, "title": title}}, "s8-title-receipt")
    if not isinstance(data, dict) or not (isinstance(data.get("refused"), str) or (
            isinstance(data.get("receipt"), dict) and isinstance(data.get("content_b64"), str))):
        raise ValueError("TITLE_RECEIPT_PROJECTION_INVALID")
    return data, via_boundary


def _title_receipt_answer(manuscript: str, ask: dict[str, str]) -> dict[str, Any]:
    """The full replay behind `verify_title_receipt`, where the first-publication closure is readable: the receipt must be a
    verified title-only edit of this manuscript's BV that gave the title `ask["title"]`, and replay through the shared
    title-chain validator against the authority built from the first receipt (the one the upload tool and the reconciliation
    run on the successor's record)."""
    import base64
    import hashlib

    from src.autoslice.authorized_upload_title_repair import TitleRepairError, validate_title_repair_successor
    from src.autoslice.manual_lane.receipt_chain import resolve
    from src.autoslice.new_bv_repair_authority import build_new_bv_repair_authority

    path = Path(ask["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return {"refused": f"{path}: is not an absolute regular file (不是绝对路径的普通文件)"}
    content = path.read_bytes()
    try:
        receipt = json.loads(content.decode("utf-8"))
    except ValueError:
        return {"refused": f"{path}: is not readable JSON (不是 JSON)"}
    binding = receipt.get("binding") if isinstance(receipt, dict) else None
    if not isinstance(binding, dict):
        return {"refused": f"{path}: is no title-repair receipt (没有 binding)"}
    if receipt.get("status") != "VERIFIED_TITLE_ONLY":
        return {"refused": f"the receipt's status is {receipt.get('status')!r}, not VERIFIED_TITLE_ONLY (回执状态不对)"}
    if binding.get("final_title") != ask["title"]:
        return {"refused": f"the receipt's title after the edit is {binding.get('final_title')!r}, the manuscript's title now "
                           f"(its clip's title/title.txt) is {ask['title']!r} (改后标题与稿件现标题不同)"}
    authority = build_new_bv_repair_authority(resolve(bound_receipt(manuscript)).first_receipt,
                                              candidate_id=ask["package"], title=ask["title"])
    if binding.get("bvid") != authority["bvid"]:
        return {"refused": f"the receipt is for {binding.get('bvid')}, the manuscript {manuscript} is {authority['bvid']} (BV 不符)"}
    reference = {"path": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest()}
    try:
        validate_title_repair_successor({"receipt": reference}, authority=authority, candidate_id=ask["package"], title=ask["title"])
    except TitleRepairError as exc:
        return {"refused": f"the receipt does not replay as the verified title edit of {manuscript}: {exc}"}
    return {"receipt": reference, "content_b64": base64.b64encode(content).decode("ascii"),
            "bvid": authority["bvid"], "final_title": ask["title"]}


def _online_meta(chain: Any) -> tuple[list[str], str | None]:
    """(final tags, final description) online now: the latest completion's plan, else the first publication's manifest."""
    from src.autoslice.manual_lane.receipt_chain import ReceiptChainError, first_manifest_meta

    if chain.bound == chain.first_receipt:
        tags, desc = first_manifest_meta(chain.first_receipt)
        return list(tags), desc
    if chain.online_tags is None:
        raise ReceiptChainError(f"{chain.bound}: the plan of the latest completion records no final tags, so the tags online are not known")
    return list(chain.online_tags), chain.online_desc


def _online_meta_or_none(chain: Any) -> dict[str, Any] | None:
    try:
        tags, desc = _online_meta(chain)
    except ValueError:       # only a comparison in revision.json needs it: its absence is said there, it stops nothing
        return None
    return {"tags": tags, "desc": desc}


def serve(request_data: HistoryRequest) -> dict[str, Any]:
    """Answer one `HistoryRequest` from the registry and the receipt: the only place they are read for the lane, in this
    process or, as a script, on the media host."""
    manuscript = request_data["candidate_id"]
    if request_data.get("check_published"):
        return {"published": is_published(manuscript)}
    if request_data.get("registry_row"):
        return {"row": _row_projection(manuscript)}
    if "build_authority" in request_data:
        from src.autoslice.manual_lane.receipt_chain import resolve
        from src.autoslice.new_bv_repair_authority import build_new_bv_repair_authority

        ask = request_data["build_authority"]
        chain = resolve(bound_receipt(manuscript))       # a repaired manuscript's row binds a completion: back to the first receipt
        return {"authority": build_new_bv_repair_authority(chain.first_receipt, candidate_id=ask["package"], title=ask["title"])}
    if request_data.get("used_packages"):
        from src.autoslice.manual_lane.receipt_chain import resolve, used_packages

        return {"packages": used_packages(resolve(bound_receipt(manuscript)))}
    if "title_receipt" in request_data:
        return _title_receipt_answer(manuscript, request_data["title_receipt"])
    if request_data.get("online_tags"):
        from src.autoslice.manual_lane.receipt_chain import resolve

        tags, _desc = _online_meta(resolve(bound_receipt(manuscript)))
        return {"tags": tags}
    if "first_digests" in request_data:
        from src.autoslice.manual_lane.receipt_chain import resolve
        from src.autoslice.new_bv_repair_authority import replay_first_publication_manifest

        first = replay_first_publication_manifest(request_data["first_digests"])
        chain = resolve(bound_receipt(manuscript))
        done = chain.predecessor_completed
        return {"digests": {"video_sha256": chain.online_video_sha256 or first["video"]["sha256"],
                            "cover_sha256": chain.online_cover_sha256 or first["cover"]["sha256"]},
                "predecessor": ({"path": str(done), "new_cid": chain.predecessor_new_cid,
                                 "authority_equal": chain.predecessor_plan_authority == request_data["first_digests"]}
                                if done is not None else None),
                "metadata": _online_meta_or_none(chain)}
    titles, note = published_titles(manuscript, limit=request_data["limit"])
    return {"titles": titles, "note": note}


def recent_titles(state_path: Path, clip_dir: Path, candidate_id: str, *, limit: int = 12,
                  ctx: Ctx | None = None) -> TitleHistory:
    from .reselect import SUPERSEDED   # lazy: this file also runs as a script on the media host

    notes: list[str] = []
    titles: list[str] = []
    try:
        if ctx is None:
            titles, note = published_titles(candidate_id, limit=limit)
        else:
            titles, note = _titles(through_boundary(ctx, {"candidate_id": candidate_id, "limit": limit}, "s6-published-titles"))
        notes.append(note)
    except (OSError, ValueError, RuntimeError) as exc:
        notes.append(f"已发标题读取不可用：{type(exc).__name__}: {exc}；继续按本批历史起标题")
    batch: list[tuple[int, str, str]] = []
    # The ordinary batch boundary is the state directory, matching the lane's clip directory layout.
    for other in state_path.parent.glob("*.json"):
        if other == state_path:
            continue
        try:
            data = json.loads(other.read_text(encoding="utf-8"))
            cid = data["clip"]["cid"]
            if cid == candidate_id or data["clip"].get("status") == SUPERSEDED:
                continue      # a replaced candidate's title was never going to be published
            title_path = clip_dir.parent / cid / "title/title.txt"
            if title_path.is_file():
                batch.append((title_path.stat().st_mtime_ns, cid, title_path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            notes.append(f"本批标题读取不可用 {other.name}：{type(exc).__name__}: {exc}")
    # Historical facts and wording never enter the generation prompt; only the names do.
    names = tuple("、".join(title_names(title)) or "不写名" for title in titles[-limit:])
    return TitleHistory(names, tuple(t for _, _, t in sorted(batch)[-limit:]), tuple(notes), tuple(titles))


def duplicate_title(title: str, published: tuple[str, ...]) -> bool:
    """Compare full published titles without giving their wording to the generation model."""
    def key(value: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())
    value = key(title)
    return bool(value) and any(value == key(previous) for previous in published)


if __name__ == "__main__":
    print(json.dumps(serve(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))), ensure_ascii=False))
