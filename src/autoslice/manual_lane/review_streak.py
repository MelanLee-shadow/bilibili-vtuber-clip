"""How many broadcasts in a row went online with nothing to change: what earns unattended upload (owner, U452).

Owner's words: 「仍然需要你看过后才能上传，不过我需要知道最近你看过后遇到了哪些问题，是否严重，我想的一个事是，如果10次直播你都
看过后没有任何要改的，就可以撤掉你了直接自动上传。如果还有错其实说明自动切片仍然需要改进」.

- The account is the channel's `review_findings.jsonl` (one row per change a clip needed after the lane had passed it):
  changed before upload by whoever looked at it, or after it was public by anyone who found it.  A row also says why the
  lane let it through and what check it became (`landed`); a row whose check is not made yet is `status: open`.
- A row is `landed` only when it shows the repair covers the class, not the one clip (admin U453,): `class`
  (the relation that failed, over which members), `generator` (the step, rule or data that produces them and was
  changed), `unseen_members` (two or more members not used to design the repair, at least one entering at the ordinary
  upstream input, each with its result), `guard` (the test that fails on the old generator and passes on the new) and
  `controls` (what must not change and was checked).  A row that cannot say these is `open`: the fix is not proved for
  the class yet.  `withdrawn` is a row found not to be a defect; `landed` then says why.
- A broadcast is a broadcast day (discover_media.broadcast_days: the day the stream started, 4:00 Beijing) on which at
  least one clip was uploaded.  It is clean when the account has no row for it and none of its clips needed a revision
  (a `_rN` clip that went online: a revision is itself a change, so a revision the account forgot still makes the day
  unclean, and it is listed as `unrecorded` so the row gets written).
- The streak is the clean broadcasts counted back from the latest one, up to the first unclean one.  At TARGET the
  owner is told the number; the hold on unattended upload (PUBLISH_PAUSED on the host) is lifted only on his word, never
  by this module.
- Counting starts at COUNT_FROM: before it the account recorded only what was found after publication, so an earlier day
  without a row is not known to be clean.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

LEDGER = "review_findings.jsonl"
STREAK_FILE = "review_streak.json"
SCHEMA = "manual-lane.review-streak.v1"
COUNT_FROM = "2026-11-15"
TARGET = 10
FOUND = ("before_upload", "after_publish")
STATUS = ("landed", "open", "withdrawn")
PROOF = ("class", "generator", "unseen_members", "guard", "controls")
ENTERED = ("existing", "upstream")
REQUIRED = ("day", "clips", "found", "by", "noticed", "problem", "why_passed", "landed", "status")
_REVISION = re.compile(r"_r\d+$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LedgerError(ValueError):
    """review_findings.jsonl has a row that is not a finding."""


def load(path: Path) -> list[dict[str, Any]]:
    """The account's rows; a malformed row stops the reading (the count must not run on a part of the account)."""
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise LedgerError(f"{path}:{n}: not JSON: {exc}") from exc
        missing = [k for k in REQUIRED if k not in row] if isinstance(row, dict) else list(REQUIRED)
        problem = (f"missing {missing}" if missing else
                   "day is not YYYY-MM-DD" if not _DAY.match(str(row["day"])) else
                   "clips is not a non-empty list" if not isinstance(row["clips"], list) or not row["clips"] else
                   f"found is not one of {FOUND}" if row["found"] not in FOUND else
                   f"status is not one of {STATUS}" if row["status"] not in STATUS else
                   "a landed row says what it became" if row["status"] != "open" and not str(row["landed"]).strip() else
                   _proof(row) if row["status"] == "landed" and _proof(row) else
                   "counts is not true/false" if not isinstance(row.get("counts", True), bool) else "")
        if problem:
            raise LedgerError(f"{path}:{n}: {problem}")
        rows.append(row)
    return rows


def _proof(row: Mapping[str, Any]) -> str:
    """Why a `landed` row does not show the class repaired ("" when it does)."""
    blank = [k for k in PROOF if k != "unseen_members" and not str(row.get(k) or "").strip()]
    if blank:
        return f"a landed row needs {blank}"
    members = row.get("unseen_members")
    if not isinstance(members, list) or len(members) < 2:
        return "a landed row needs unseen_members: two or more"
    for m in members:
        if not isinstance(m, dict) or m.get("entered") not in ENTERED or not str(m.get("member") or "").strip() \
                or not str(m.get("result") or "").strip():
            return f"an unseen member needs member, entered ({'/'.join(ENTERED)}) and result: {m}"
    if not any(m["entered"] == "upstream" for m in members):
        return "a landed row needs an unseen member entering at the upstream input"
    return ""


def uploaded(task_dir: Path) -> dict[str, list[str]]:
    """The clips that went online (step 10 DONE), by the broadcast day the lane recorded for them."""
    days: dict[str, list[str]] = {}
    for path in sorted((Path(task_dir) / "state").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if (doc.get("steps", {}).get("upload") or {}).get("status") == "DONE":
            days.setdefault(str(doc["clip"]["recording_date"]), []).append(path.stem)
    return days


def tally(rows: Sequence[Mapping[str, Any]], days: Mapping[str, Iterable[str]], *, count_from: str = COUNT_FROM,
          target: int = TARGET) -> dict[str, Any]:
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("counts", True):
            by_day.setdefault(str(row["day"]), []).append(row)
    out = []
    for day in sorted(d for d in days if d >= count_from):
        clips = sorted(days[day])
        revisions = [c for c in clips if _REVISION.search(c)]
        found = by_day.get(day, [])
        out.append({"day": day, "clips": len(clips) - len(revisions), "revisions": revisions,
                    "before_upload": sum(r["found"] == "before_upload" for r in found),
                    "after_publish": sum(r["found"] == "after_publish" for r in found),
                    "clean": not found and not revisions,
                    "unrecorded": bool(revisions) and not any(r["found"] == "after_publish" for r in found)})
    streak = 0
    for entry in reversed(out):
        if not entry["clean"]:
            break
        streak += 1
    return {"schema": SCHEMA, "count_from": count_from, "target": target, "streak": streak, "reached": streak >= target,
            "latest_unclean": next((e["day"] for e in reversed(out) if not e["clean"]), None),
            "rows": len(rows), "open": sum(r["status"] == "open" for r in rows),
            "unrecorded": [e["day"] for e in out if e["unrecorded"]], "days": out}


def line(report: Mapping[str, Any]) -> str:
    """One line for status and step 10's note."""
    gaps = f"；账里漏记的修订：{'、'.join(report['unrecorded'])}" if report["unrecorded"] else ""
    return (f"连续无改场次 {report['streak']}/{report['target']}（自 {report['count_from']} 起按直播日计，"
            f"最近一场有改：{report['latest_unclean'] or '无'}；账 {report['rows']} 条，未落成检查 {report['open']} 条{gaps}）")


def markdown(rows: Sequence[Mapping[str, Any]]) -> str:
    """The account as a table for people (the handoff's POST-PASS-FINDINGS.md is this, made again after each new row)."""
    where = {"before_upload": "传前", "after_publish": "发布后"}
    head = ["| 直播日 | 片 | 问题 | 谁发现、何时 | 流水线当时为什么放行 | 落成了什么 | 这一类、没用来设计的片 | 状态 |",
            "|---|---|---|---|---|---|---|---|"]
    state = {"landed": "已落成", "withdrawn": "不是缺陷", "open": "**未落成**"}
    cell = lambda v: str(v).replace("|", "／").replace("\n", " ")
    body = [f"| {r['day']}{'' if r.get('counts', True) else '（不计）'} | {cell('、'.join(r['clips']))} | {cell(r['problem'])} | "
            f"{cell(r['by'])}，{where[r['found']]}，{r['noticed']} | {cell(r['why_passed'])} | {cell(r['landed'])} | "
            f"{cell(_members(r))} | {state[r['status']]} |" for r in sorted(rows, key=lambda r: (r["day"], r["noticed"]))]
    return "\n".join(head + body) + "\n"


def _members(row: Mapping[str, Any]) -> str:
    out = str(row.get("class") or "")
    for m in row.get("unseen_members") or []:
        if isinstance(m, dict):
            out += f"；{m.get('member')}（{'上游新进' if m.get('entered') == 'upstream' else '已有'}）：{m.get('result')}"
    return out or "—"


def write(task_dir: Path, ledger: Path, *, online: tuple[str, str] | None = None,
          now: dt.datetime | None = None) -> dict[str, Any]:
    """Count from the account and the task's uploads and keep the count beside the task's state (review_streak.json).
    `online` is (broadcast day, clip) of a clip that has just gone online and whose step 10 is not marked done yet."""
    days = uploaded(task_dir)
    if online is not None and online[1] not in days.get(online[0], []):
        days.setdefault(online[0], []).append(online[1])
    report = {**tally(load(ledger), days), "ledger": str(ledger),
              "written_at": (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    path = Path(task_dir) / STREAK_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return report
