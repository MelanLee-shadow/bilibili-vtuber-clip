"""After a clip is sealed: what it shows of the host that stays true in other clips.

Owner ("人设资料我记得应该也有个机制，就是随着切片来更新吧") and ("流水线最好是能够自己在做切片
的时候，顺带的就把人物性格给补完，都放在……Persona那里").  The accepted persona (persona.md, rendered from the fact
ledger) changes only by a commit; what the clips show in between is kept here, with the lane's task:

- `after_run` (every run of a clip ends in cli._finish_run): once the review is done, one model call reads the final
  subtitles and names at most `observe_max` stable traits the persona does not have yet, each with her own words.
  A trait is kept only when its quotes stand, letter for letter, in lines the host speaks.  A trait already observed
  gets this clip as another witness.
- `lines`: the traits witnessed in at least `observed_min_sessions` live sessions, for every reader of the channel's
  facts (channel_facts), marked as observed and not yet accepted.

Nothing here stops or changes a clip: a failed call is written down and the next clip tries again.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import channel_facts, speaker_v8
from .lane_rules import rules
from .runner import Ctx
from .srt_text import _json_from_text, parse_srt
from .step_io import _ask_cpa, _cpa_ok, _render

FILE = "persona/observations.json"      # under the lane's task directory: every clip of the task adds to it
MARK = "persona/observed.json"          # under the clip: this clip was read
CALL = "persona-observe"


def _letters(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum())


def _same(a: str, b: str) -> bool:
    x, y = _letters(a), _letters(b)
    return bool(x and y) and SequenceMatcher(None, x, y, autojunk=False).ratio() >= float(rules("persona")["same_statement_ratio"])


def task_of(ctx: Ctx) -> Path:
    return ctx.clip_dir.parent.parent


def load(task: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads((task / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def lines(task: Path) -> list[str]:
    """The observed traits several sessions bear out, one line each, for a prompt."""
    least = int(rules("persona")["observed_min_sessions"])
    out = []
    for row in load(task):
        sessions = {w.get("recording_date") for w in row.get("witnesses", [])}
        if len(sessions) >= least:
            out.append(f"- {row['statement']}（{len(sessions)} 场直播的切片里出现过，尚未审定）")
    return out


def host_text(ctx: Ctx) -> tuple[str, str]:
    """(the final subtitles with speakers, the letters of the host's own lines)."""
    speaker = ctx.clip_dir / "speaker/speaker.srt"
    cues = parse_srt((speaker if speaker.is_file() else ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    guest = f"[{speaker_v8.GUEST_LABEL}]"
    own = [c for c in cues if not str(c["text"]).startswith(guest)]
    return ("\n".join(f"{c['id']} {' '.join(str(c['text']).split())}" for c in cues),
            _letters("".join(re.sub(r"^\[[^\]]+\]\s*", "", str(c["text"])) for c in own)))


def _kept(answer: object, own: str, known: str) -> list[dict[str, Any]]:
    """The answer's traits that pass: a statement, quotes that are her own words, not something the persona says."""
    rows = []
    for item in (answer.get("traits") if isinstance(answer, dict) else None) or []:
        if not (isinstance(item, dict) and isinstance(item.get("statement"), str) and isinstance(item.get("quotes"), list)):
            continue
        quotes = [str(q).strip() for q in item["quotes"] if _letters(q) and _letters(q) in own]
        statement = item["statement"].strip()
        if statement and quotes and not any(_same(statement, line) for line in known.splitlines()):
            rows.append({"statement": statement, "kind": str(item.get("kind") or ""), "quotes": quotes})
    return rows[:int(rules("persona")["observe_max"])]


def after_run(ctx: Ctx) -> str:
    """Read the sealed clip once.  Returns a line for the run's output ("" when there was nothing to do)."""
    mark = ctx.clip_dir / MARK
    if ctx.state.step("review").get("status") != "DONE" or mark.is_file():
        return ""
    try:
        text, own = host_text(ctx)
        known = "\n".join([channel_facts.spoken(), *(row["statement"] for row in load(task_of(ctx)))])
        record = _ask_cpa(ctx, CALL, _render("persona_observe.md", persona=known, lines=text,
                                             most=str(rules("persona")["observe_max"])), purpose=CALL)
        found = _kept(_json_from_text(record["response_text"]), own, channel_facts.spoken()) if _cpa_ok(record) else None
    except Exception as exc:  # noqa: BLE001 - an observation is never a reason to fail a clip that is done
        return f"人设观察：未完成（{type(exc).__name__}: {str(exc)[:120]}），下次运行再读"
    if found is None:
        return "人设观察：模型没有回答，下次运行再读"
    task = task_of(ctx)
    rows = load(task)
    witness = {"cid": ctx.state.data["clip"]["cid"], "recording_date": ctx.session.get("recording_date")}
    for trait in found:
        row = next((r for r in rows if _same(r["statement"], trait["statement"])), None)
        if row is None:
            row = {"statement": trait["statement"], "kind": trait["kind"], "witnesses": []}
            rows.append(row)
        if all(w.get("cid") != witness["cid"] for w in row["witnesses"]):
            row["witnesses"].append({**witness, "quotes": trait["quotes"]})
    for path, doc in ((task / FILE, rows), (mark, {"found": found, "model": record.get("model"), "effort": record.get("effort")})):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return "人设观察：" + ("；".join(t["statement"] for t in found) if found else "这条片没有人设里还没有的稳定特点")
