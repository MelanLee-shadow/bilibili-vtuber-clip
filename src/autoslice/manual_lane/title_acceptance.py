"""The receipt that a clip's title passed the skill's acceptance judges, and step 8's check of it.

Every writer of title/title.txt that sends a title out writes `title/accepted.json` with it: step 6's pick (`step6`), a
revision keeping the published title it re-judged (`online`; `online_kept` when the recheck or a judge found against it:
the title stays and their reasons are kept as `objections`, since only the owner gives a published manuscript another
title), the review's rewrite (`review`), or the channel owner's own title (`owner`, which no judge overrules).  Step 8 refuses a title that has no receipt, whose receipt names another title,
or whose judges did not both pass (the fact judge, skill 八.3, and the writing judge, 八.4: `writing_passed`).  A writer added later that
forgets the receipt stops at step 8 instead of publishing an unjudged title.  A receipt whose judges read another version
of the skill (`stale`) is judged again by the current one at step 8 and before step 10 sends anything
(step6_title.title_current); a title the current skill fails stops there, except a kept published title, which
becomes `online_kept`.  The objections of an `online_kept` title are shown by the clip's status and recorded by step 10
(`objections`), so they reach the owner.

the channel's request — the 11-23 collab titles
went out with only the fact judge passed, and two were chosen with no judge passed at all (`forced`)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

RECEIPT = "title/accepted.json"
BY = ("step6", "online", "online_kept", "review", "owner")
UNJUDGED = ("owner", "online_kept")      # no judge's verdict decides these: not judged again, not refused for one
NOT_ACCEPTED = "TITLE_NOT_ACCEPTED"
FRESH_TITLE_KEY = "fresh_title"
_VERDICT_VALUES = frozenset({"PASS", "FAIL"})


@dataclass(frozen=True)
class ReceiptLoad:
    """Result of loading and validating `title/accepted.json`: distinguishes missing, unaccepted, and corrupt."""

    receipt: dict[str, object] | None
    status: str                          # "ok" | "missing" | "unaccepted" | "corrupt"
    error: str                           # reason when corrupt or unaccepted; "" when ok or missing


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _judge_shape_error(judge: dict[str, object]) -> str:
    """Validate nested fields of `receipt['judge']`; returns "" when the shape is valid."""
    overall = judge.get("overall")
    if overall is not None and (not isinstance(overall, str) or overall not in _VERDICT_VALUES):
        return f"{RECEIPT} 的 judge.overall 无效（{overall!r}）"
    fail_reasons = judge.get("fail_reasons")
    if fail_reasons is not None and not _str_list(fail_reasons):
        return f"{RECEIPT} 的 judge.fail_reasons 必须是字符串列表"
    cover = judge.get("cover")
    if cover is not None:
        if not isinstance(cover, dict):
            return f"{RECEIPT} 的 judge.cover 必须是对象或 null，实际为 {type(cover).__name__}"
        c_overall = cover.get("overall")
        if not isinstance(c_overall, str) or c_overall not in _VERDICT_VALUES:
            return f"{RECEIPT} 的 judge.cover.overall 无效（{c_overall!r}）"
        if not _str_list(cover.get("fail_reasons")):
            return f"{RECEIPT} 的 judge.cover.fail_reasons 必须是字符串列表"
    for field in ("runs", "quality"):
        rows = judge.get(field)
        if rows is not None:
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                return f"{RECEIPT} 的 judge.{field} 必须是对象列表"
            for r in rows:
                r_overall = r.get("overall")
                if r_overall is not None and not isinstance(r_overall, str):
                    return f"{RECEIPT} 的 judge.{field}[].overall 必须是字符串"
                r_reasons = r.get("fail_reasons")
                if r_reasons is not None and not _str_list(r_reasons):
                    return f"{RECEIPT} 的 judge.{field}[].fail_reasons 必须是字符串列表"
    authorities = judge.get("authorities")
    if authorities is not None and (not isinstance(authorities, list) or not all(isinstance(a, dict) for a in authorities)):
        return f"{RECEIPT} 的 judge.authorities 必须是对象列表"
    fallback_models = judge.get("fallback_models")
    if fallback_models is not None and not _str_list(fallback_models):
        return f"{RECEIPT} 的 judge.fallback_models 必须是字符串列表"
    return ""


def write(clip_dir: Path, title: str, by: str, judge: Mapping[str, object] | None, *,
          objections: Sequence[str] = (), cover_lines: Sequence[str] | None = None,
          involved: Sequence[str] | None = None, retitle_evidence: str | None = None) -> None:
    """Record that `title` goes out, who chose it and the judges' verdict (None for the owner's own title, and for a
    kept published title the recheck stopped before a judge), with what was found against a kept one, plus the
    candidate metadata (`involved`, `cover_lines`, `retitle_evidence`) needed if the title re-enters step 6."""
    if by not in BY:
        raise ValueError(f"unknown title source {by!r}")
    if (by == "online_kept") != bool(objections):
        raise ValueError("objections go with online_kept and only with it")
    payload: dict[str, object] = {"title": title.strip(), "by": by, "judge": dict(judge) if judge else None}
    if objections:
        payload["objections"] = list(objections)
    if cover_lines:
        cleaned = [str(line).strip() for line in cover_lines if isinstance(line, str) and line.strip()]
        if cleaned:
            payload["cover_lines"] = cleaned
    if involved:
        cleaned_involved = list(dict.fromkeys(str(name).strip() for name in involved if isinstance(name, str) and name.strip()))
        if cleaned_involved:
            payload["involved"] = cleaned_involved
    if retitle_evidence:
        payload["retitle_evidence"] = retitle_evidence
    path = clip_dir / RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_receipt(clip_dir: Path) -> ReceiptLoad:
    """Read and validate `title/accepted.json` with one strict shape contract.  Missing files return status='missing';
    unreadable, non-JSON, or wrong-shape files (including nested `judge` / `judge.cover`) return status='corrupt' with
    a concrete reason; valid JSON whose judges did not pass returns status='unaccepted'."""
    path = clip_dir / RECEIPT
    if not path.exists():
        return ReceiptLoad(None, "missing", "")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 无法读取：{exc}")
    except ValueError as exc:
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 不是合法 JSON：{exc}")
    if not isinstance(raw, dict):
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 根节点必须是 JSON 对象，实际为 {type(raw).__name__}")
    by = raw.get("by")
    if not isinstance(by, str) or by not in BY:
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 by 字段无效（{by!r}）")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 title 必须是非空字符串，实际为 {type(title).__name__}")
    judge = raw.get("judge")
    if judge is not None and not isinstance(judge, dict):
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 judge 必须是对象或 null，实际为 {type(judge).__name__}")
    if isinstance(judge, dict):
        if shape_err := _judge_shape_error(judge):
            return ReceiptLoad(None, "corrupt", shape_err)
    for list_field in ("cover_lines", "involved", "objections"):
        val = raw.get(list_field)
        if val is not None and not _str_list(val):
            return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 {list_field} 必须是字符串列表")
    retitle_ev = raw.get("retitle_evidence")
    if retitle_ev is not None and not isinstance(retitle_ev, str):
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 retitle_evidence 必须是字符串，实际为 {type(retitle_ev).__name__}")
    if by == "owner":
        return ReceiptLoad(raw, "ok", "")
    if by == "online_kept":
        objs = raw.get("objections")
        if isinstance(objs, list) and objs and all(isinstance(x, str) and x.strip() for x in objs):
            return ReceiptLoad(raw, "ok", "")
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 online_kept 缺少有效 objections")
    if judge is None:
        return ReceiptLoad(None, "unaccepted", f"{RECEIPT} 缺少判官结论")
    if judge.get("overall") != "PASS":
        return ReceiptLoad(None, "unaccepted", f"{RECEIPT} 的判官结论未通过")
    cover = judge.get("cover")
    if isinstance(cover, dict) and cover.get("overall") != "PASS":
        return ReceiptLoad(None, "unaccepted", f"{RECEIPT} 的封面字未通过事实判官")
    quality = judge.get("quality")
    if not isinstance(quality, list):
        return ReceiptLoad(None, "corrupt", f"{RECEIPT} 的 judge.quality 必须是列表")
    if not writing_passed(quality):
        return ReceiptLoad(None, "unaccepted", f"{RECEIPT} 未通过写法判官")
    return ReceiptLoad(raw, "ok", "")


def accepted(clip_dir: Path) -> dict[str, object] | None:
    """The clip's registered title acceptance receipt (`title/accepted.json`) when valid and accepted; else None."""
    return load_receipt(clip_dir).receipt


def objections(clip_dir: Path) -> list[str]:
    """What the recheck and the judges have against the published title a revision keeps (`online_kept`), for the
    clip's status and step 10's record; [] for any other receipt, or none (problem() reports that)."""
    loaded = load_receipt(clip_dir)
    if loaded.receipt is None or loaded.receipt.get("by") != "online_kept":
        return []
    raw = loaded.receipt.get("objections")
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def writing_passed(quality: Sequence[object]) -> bool:
    """The writing judge's runs pass the title: more than half of them say PASS (two runs: both; a third is asked when
    the two differ, step6_title.title_judge).  No run is no pass."""
    return sum(1 for r in quality if isinstance(r, Mapping) and r.get("overall") == "PASS") * 2 > len(quality)


def stale(clip_dir: Path, current: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    """The receipt, when its judges read other files than `current` (the skill and its two judge prompts as they are
    now: title_authority.judge_authorities); None when it was judged by the current skill, is UNJUDGED, or
    there is no readable receipt (problem() reports that).  Owner (U471): a title goes out only if it follows
    the skill — the skill as it is when the title goes out, not the one it was judged by before the skill changed
    (316670 and 673640, redone from step 9 after skill 230a913, still carried titles judged by the version before)."""
    try:
        receipt = json.loads((clip_dir / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("by") not in BY or receipt["by"] in UNJUDGED:
        return None
    judge = receipt.get("judge")
    judged = judge.get("authorities") if isinstance(judge, dict) else None
    return receipt if not isinstance(judged, list) or _files(judged) != _files(current) else None


def _files(records: Sequence[object]) -> set[tuple[str, str]]:
    return {(str(r.get("path")), str(r.get("sha256"))) for r in records if isinstance(r, Mapping)}


def problem(clip_dir: Path) -> str:
    """Why title/title.txt may not go out, or "" when its receipt shows it accepted."""
    path = clip_dir / RECEIPT
    title_file = clip_dir / "title/title.txt"
    if not title_file.is_file():
        return "没有标题（title/title.txt）：从第 6 步重做"
    title = title_file.read_text(encoding="utf-8").strip()
    if not path.is_file():
        return f"标题没有过判记录（{RECEIPT}）：第 6 步在两位判官都过之前不交标题；从第 6 步重做"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return f"{RECEIPT} 不是 JSON"
    if not isinstance(receipt, dict) or receipt.get("by") not in BY:
        return f"{RECEIPT} 没写标题是谁定的"
    rec_title = receipt.get("title")
    if not isinstance(rec_title, str) or not rec_title.strip():
        return f"{RECEIPT} 的 title 必须是非空字符串"
    if rec_title.strip() != title:
        return f"标题「{title}」不是过判记录里的那条（{rec_title}）：改标题的那一步没过判官"
    judge = receipt.get("judge")
    if judge is not None and not isinstance(judge, dict):
        return f"{RECEIPT} 的 judge 必须是对象或 null"
    if isinstance(judge, dict):
        if shape_err := _judge_shape_error(judge):
            return shape_err
    if receipt["by"] == "owner":
        return ""
    if receipt["by"] == "online_kept":      # kept because it is online; what the judges found goes with it
        objections = receipt.get("objections")
        return "" if isinstance(objections, list) and objections and all(isinstance(x, str) and x.strip() for x in objections) else f"{RECEIPT}：沿用的线上标题没写异议"
    if not isinstance(judge, dict) or judge.get("overall") != "PASS":
        return f"标题「{title}」没过判官：" + "；".join(str(x) for x in ((judge or {}).get("fail_reasons") or []))[:300]
    cover = judge.get("cover")
    if isinstance(cover, dict) and cover.get("overall") != "PASS":
        return f"标题「{title}」的封面字没过事实判官（skill 第八节第 3 条）：从第 6 步重做"
    quality = judge.get("quality")
    if not isinstance(quality, list) or not writing_passed(quality):
        return f"标题「{title}」没过写法判官（skill 第八节第 4 条，按第四节验收）：从第 6 步重做"
    return ""
