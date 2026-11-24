"""The receipt that a clip's title passed the skill's acceptance judges, and step 8's check of it.

Every writer of title/title.txt that sends a title out writes `title/accepted.json` with it: step 6's pick (`step6`), a
revision keeping the published title it re-judged (`online`), the review's rewrite (`review`), or the channel owner's own
title (`owner`, which no judge overrules).  Step 8 refuses a title that has no receipt, whose receipt names another title,
or whose judges did not both pass (the fact judge, skill 八.3, and the writing judge, 八.4).  A writer added later that
forgets the receipt stops at step 8 instead of publishing an unjudged title.

Owner (U471): 「你看看这标题起的，遵循了标题skill吗遵循标题原则吗？昨天刚刚强调的」 — the 11-23 collab titles
went out with only the fact judge passed, and two were chosen with no judge passed at all (`forced`)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

RECEIPT = "title/accepted.json"
BY = ("step6", "online", "review", "owner")
NOT_ACCEPTED = "TITLE_NOT_ACCEPTED"


def write(clip_dir: Path, title: str, by: str, judge: Mapping[str, object] | None) -> None:
    """Record that `title` goes out, who chose it and the judges' verdict (None only for the owner's own title)."""
    if by not in BY:
        raise ValueError(f"unknown title source {by!r}")
    path = clip_dir / RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": title.strip(), "by": by, "judge": dict(judge) if judge else None},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    if str(receipt.get("title") or "").strip() != title:
        return f"标题「{title}」不是过判记录里的那条（{receipt.get('title')}）：改标题的那一步没过判官"
    if receipt["by"] == "owner":
        return ""
    judge = receipt.get("judge")
    if not isinstance(judge, dict) or judge.get("overall") != "PASS":
        return f"标题「{title}」没过判官：" + "；".join(str(x) for x in ((judge or {}).get("fail_reasons") or []))[:300]
    quality = judge.get("quality")
    if not isinstance(quality, list) or not quality or any(not isinstance(r, dict) or r.get("overall") != "PASS"
                                                           for r in quality):
        return f"标题「{title}」没过写法判官（skill 第八节第 4 条，按第四节验收）：从第 6 步重做"
    return ""
