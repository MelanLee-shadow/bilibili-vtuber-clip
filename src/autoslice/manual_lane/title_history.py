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
    cover its first publication recorded."""
    candidate_id: str
    limit: NotRequired[int]
    check_published: NotRequired[bool]
    build_authority: NotRequired[dict[str, str]]
    first_digests: NotRequired[dict[str, Any]]


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


def _denied(exc: BaseException | None) -> bool:
    """Whether `exc` is, or was raised out of, a PermissionError (a reader that wraps the OSError it met keeps it as the cause)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, PermissionError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def read_through_boundary(ctx: Ctx, request_data: HistoryRequest, call: str) -> tuple[object, bool]:
    """(the answer to `request_data` (what `serve` returns), whether it came through the root boundary): read by this process
    when it can, else on the media host through the root boundary (`call` names the remote script); always there when the
    lane's remote is ssh.  RuntimeError naming both errors when this process was denied and the boundary failed too; any
    other error of reading here is raised as it is (it is not 'denied', so no root read could change it)."""
    from .adapters import SshRemote

    if isinstance(ctx.adapters.remote, SshRemote):
        return _remote_read(ctx, request_data, call), True
    try:
        return serve(request_data), False
    except (OSError, ValueError) as original:
        if not _denied(original):
            raise
        try:
            return _remote_read(ctx, request_data, call), True
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(f"PermissionError: {original}; projection: {exc}") from exc


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


def first_receipt(manuscript: str) -> Path:
    """The first-publication receipt the registry row of the published manuscript binds."""
    rows = [r for r in load_publication_registry()["entries"]
            if r["status"] == "published" and same_clip(str(r["candidate_id"]), manuscript)]
    if len(rows) != 1:
        raise ValueError(f"the registry holds {len(rows)} published rows for {manuscript} (登记里要有且只有一条)")
    binding = (rows[0].get("publication_reconciliation") or {}).get("authority")
    if not isinstance(binding, dict) or not binding.get("path"):
        raise ValueError(f"the registry row of {manuscript} binds no first-publication receipt (登记行没有首发回执)")
    return Path(str(binding["path"]))


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


def first_digests(ctx: Ctx, manuscript: str, authority: dict[str, Any]) -> tuple[str, str]:
    """(video sha256, cover sha256) the first publication's manifest recorded, after replaying the closure of `authority`
    (`through_boundary`)."""
    data = through_boundary(ctx, {"candidate_id": manuscript, "first_digests": authority}, "s8-first-digests")
    digests = data.get("digests") if isinstance(data, dict) else None
    if not isinstance(digests, dict) or not all(isinstance(digests.get(k), str) for k in ("video_sha256", "cover_sha256")):
        raise ValueError("FIRST_PUBLICATION_PROJECTION_INVALID")
    return digests["video_sha256"], digests["cover_sha256"]


def serve(request_data: HistoryRequest) -> dict[str, Any]:
    """Answer one `HistoryRequest` from the registry and the receipt: the only place they are read for the lane, in this
    process or, as a script, on the media host."""
    manuscript = request_data["candidate_id"]
    if request_data.get("check_published"):
        return {"published": is_published(manuscript)}
    if "build_authority" in request_data:
        from src.autoslice.new_bv_repair_authority import build_new_bv_repair_authority

        ask = request_data["build_authority"]
        return {"authority": build_new_bv_repair_authority(first_receipt(manuscript), candidate_id=ask["package"], title=ask["title"])}
    if "first_digests" in request_data:
        from src.autoslice.new_bv_repair_authority import replay_first_publication_manifest

        first = replay_first_publication_manifest(request_data["first_digests"])
        return {"digests": {"video_sha256": first["video"]["sha256"], "cover_sha256": first["cover"]["sha256"]}}
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
