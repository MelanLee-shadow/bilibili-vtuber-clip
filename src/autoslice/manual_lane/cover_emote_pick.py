"""Step 6 cover: the main emote chosen by the sense of the line, not by keywords (70-cover; approved batch: the emote was
read off the emote list's "适合" column against what the cover line says).

One text-model call reads each emote's description and "适合" from the emote library manifest next to the cover lines and
the story's expression and action, and ranks the ones that fit.  The recent-cover rules are unchanged: an emote among
the last five main emotes, or already the main emote of this source session, is never used.  The model's first choice
that is not excluded is taken; none left (or none given) means no emote candidate.  When the call or the manifest is
unusable the repo's keyword pick (`cover_plan`) stands and the record says so."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict

from .srt_text import _json_from_text
from .step6_cover_types import CoverEmote, CoverPlan
from .step_io import _ask_cpa, _cpa_ok, _log, _render

if TYPE_CHECKING:
    from src.autoslice.cover_emote import EmoteEntry, EmoteLibrary

    from .runner import Ctx
    from .state import CpaEvent


class EmotePick(TypedDict, total=False):
    source: str                 # "model" | "rule"
    chosen: str | None
    choices: list[str]
    reason: str
    excluded: list[str]
    call: str | None
    elapsed_s: float | None


def _emote(library: EmoteLibrary, entry: EmoteEntry) -> CoverEmote:
    root = library.runtime_roots[0] if library.runtime_roots else ""
    return {"id": entry.id, "label": entry.label, "hd": f"{root}/{entry.hd_file}", "mask": f"{root}/{entry.file}"}


def _entry_line(entry: EmoteEntry) -> str:
    return f"{entry.id} {entry.label}：{entry.description}；适合：{entry.use_when}"


def pick_emote(ctx: Ctx, plan: CoverPlan, cover_lines: Sequence[str]) -> tuple[CoverEmote | None, EmotePick, list[CpaEvent]]:
    """(emote or None, the record, the cpa records).  Never raises on a model or manifest problem: the keyword pick
    in `plan` stands then (None when it found nothing either)."""
    from src.autoslice.cover_emote import load_emote_library

    recent = plan["recent"]
    excluded = sorted(set(recent["last_five_main_emotes"]) | set(recent["source_session_main_emotes"]))
    fallback = plan.get("emote")
    library = load_emote_library()
    entries = [e for e in library.entries if e.id not in excluded and e.use_when.strip()]
    if not library or not entries:
        reason = "表情包说明（emote 清单）读不到或没有可用条目，沿用关键词规则"
        return fallback, {"source": "rule", "chosen": fallback and fallback["id"], "reason": reason, "excluded": excluded}, []
    brief = plan.get("brief") or {}
    prompt = _render("cover_emote.md", lines=" / ".join(cover_lines), quotes=" / ".join(brief.get("quotes") or []) or "（无）",
                     expression=str(brief.get("expression") or ""), action=str(brief.get("action") or ""),
                     entries="\n".join(_entry_line(e) for e in entries))
    record = _ask_cpa(ctx, "cover-emote", prompt, purpose="cover-emote")
    calls = [_log(record)]
    pick: EmotePick = {"excluded": excluded, "call": record.get("call_id"), "elapsed_s": record.get("elapsed_s")}
    answer = None
    if _cpa_ok(record):
        try:
            answer = _json_from_text(record["response_text"])
        except ValueError:
            answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("choices"), list):
        pick.update(source="rule", chosen=fallback and fallback["id"], reason="表情包选择调用失败或答案读不出，沿用关键词规则")
        return fallback, pick, calls
    allowed = {e.id: e for e in entries}                        # an excluded or unknown id is skipped, the next one is taken
    choices = [str(c) for c in answer["choices"]]
    chosen = next((c for c in choices if c in allowed), None)
    pick.update(source="model", chosen=chosen, choices=choices, reason=str(answer.get("reason") or ""))
    return (_emote(library, allowed[chosen]) if chosen else None), pick, calls
