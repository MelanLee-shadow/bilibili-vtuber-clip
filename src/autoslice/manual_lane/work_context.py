"""The work the host is watching or playing, read from the picture before the subtitle agent starts.

Hosts often keep a fixed text bar on the stream ("正在：某某") naming what they are watching or playing (the picture
may also show the video's own title, or be recognisable as a work; the bar is the most reliable of the three), and then say
that work's character names aloud; ASR writes them as nonsense and nothing else in the session (title, danmaku,
participants) carries them.  `build` reads that bar once from a frame in the middle of the cut (read_banner.py on the
media host, one CPA vision request), looks the work up in Bangumi, takes its lead and supporting characters and returns
the Chinese spellings a speaker could say.  Evidence for the agent, never a decision: any failure is `UNAVAILABLE` with a
reason and never reaches the caller as an exception.

Result, also saved as <clip>/work/context.json:
  status   FOUND (a Bangumi subject whose name matches the bar was found, with characters) | BANNER_ONLY (a bar, but no
           matching subject) | TITLE_ONLY (no bar and no matching subject, but the name is the watched video's own title
           as the screen writes it) | NONE (the picture has no bar, or the answer is unreadable) | UNAVAILABLE (a step failed)
  banner / work   the bar as read, and the work name taken from it (either may be empty)
  subject  {id, name, name_cn, source} or None;  characters [{name, names_cn}];  names  work_lookup.spoken_names
  reason   why, for NONE and UNAVAILABLE;  cpa  the paid vision request(s) made by this call (none when reused)
A saved FOUND / BANNER_ONLY / NONE is reused as it is (no picture read, no network); UNAVAILABLE is made again.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import channel
from . import text_overlap
from . import work_lookup
from .adapters import remote_python, root_script
from .lane_rules import rules
from .runner import Ctx
from .srt_text import _json_from_text
from .step_io import REMOTE_TOOLS, _local, _run_remote, _write_atomic
from .work_lookup import LookupProvider, LookupResult, LookupRow

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

REUSABLE = ("FOUND", "BANNER_ONLY", "TITLE_ONLY", "NONE")


class Subject(TypedDict):
    id: str | int | None
    name: str | None
    name_cn: str | None
    source: str


class Character(TypedDict):
    name: str | None
    names_cn: list[str]


class BannerRecord(TypedDict):
    """One paid vision request, as read_banner.py reported it."""
    call_id: str
    model: JsonValue
    rc: JsonValue
    elapsed_s: JsonValue
    kind: str
    usage: JsonValue


class WorkContext(TypedDict):
    status: str
    banner: str
    work: str
    subject: Subject | None
    characters: list[Character]
    names: list[str]
    reason: str
    cpa: list[BannerRecord]
    read_from: NotRequired[str]        # where the picture reader took the work name: banner, title or picture


class AgentView(TypedDict):
    status: str
    banner: NotRequired[str]
    work: NotRequired[str]
    subject: NotRequired[str | None]
    names: NotRequired[list[str]]


class LookupWithCharacters(LookupResult):
    characters: NotRequired[list[Character]]
    characters_error: NotRequired[str]


OFF: WorkContext = {"status": "OFF", "banner": "", "work": "", "subject": None, "characters": [], "names": [],
                    "reason": "lane_rules.json screen_work.enabled is false", "cpa": []}


def enabled() -> bool:
    """Whether the lane reads the picture at all (lane_rules.json `screen_work`).  A rules file without that section
    (an older or overriding one) means off: an optional step must not stop the lane because its switch is absent."""
    try:
        return bool(rules("screen_work")["enabled"])
    except KeyError:
        return False
BANGUMI_ANIME = 2
MIN_CONTAINED_CHARS = 4      # a name that only contains / is contained in the bar must be at least this long
BANNER_TIMEOUT_S = 600


def _matches(work_key: str, row: LookupRow) -> bool:
    for field in ("name_cn", "name"):
        key = text_overlap.key(str(row.get(field) or ""))
        if not key:
            continue
        if key == work_key:
            return True
        short, long_ = sorted((key, work_key), key=len)
        if len(short) >= MIN_CONTAINED_CHARS and short in long_:
            return True
    return False


def pick_subject(work: str, rows: Sequence[LookupRow]) -> LookupRow | None:
    """The Bangumi row that is this work: its Chinese or original name equals the work name once normalised, or one
    contains the other and the shorter has at least four characters.  Several matches: an anime first, else the first."""
    work_key = text_overlap.key(work)
    if not work_key:
        return None
    found = [row for row in rows if row.get("layer") == "bangumi" and _matches(work_key, row)]
    return next((row for row in found if row.get("type") == BANGUMI_ANIME), found[0] if found else None)


def characters_of(subject_id: int, web: LookupProvider) -> list[Character]:
    return [{"name": c["name"], "names_cn": c["names_cn"]} for c in web.bangumi_characters(subject_id)]


def with_characters(result: LookupResult, web: LookupProvider) -> LookupWithCharacters:
    """A work_lookup result plus the character table of the Bangumi subject it matched; a failing character request
    leaves the result as it was and says so."""
    out = cast(LookupWithCharacters, dict(result))
    subject = pick_subject(str(result.get("query") or ""), result.get("results") or [])
    if subject is not None and subject.get("id") is not None:
        try:
            out["characters"] = characters_of(int(cast("str | int", subject["id"])), web)
        except Exception as exc:  # noqa: BLE001 - the lookup tool still answers with the subject
            out["characters_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return out


def agent_view(context: WorkContext) -> AgentView:
    """What the agent is shown: only the status when there is nothing to show."""
    if context["status"] in ("NONE", "UNAVAILABLE", "OFF"):
        return {"status": context["status"]}
    subject = context.get("subject")
    return {"status": context["status"], "banner": context["banner"], "work": context["work"],
            "subject": (subject.get("name_cn") or subject.get("name")) if subject else None,
            "names": list(context.get("names") or [])}


def _record(doc: dict[str, JsonValue]) -> BannerRecord:
    return {"call_id": "work-banner-1", "model": doc.get("model"), "rc": doc.get("rc"),
            "elapsed_s": doc.get("elapsed_s"), "kind": "vision", "usage": doc.get("usage")}


def _read_banner(ctx: Ctx, cut_len: float, out: WorkContext) -> bool:
    """Run the tool, fill banner / work / cpa; False when the model's answer is not a JSON object."""
    rd, t = ctx.remote_dir, cut_len / 2
    remote = ctx.adapters.remote
    for source, name in ((REMOTE_TOOLS / "read_banner.py", "read_banner.py"), (REMOTE_TOOLS / "pick_frame.py", "pick_frame.py"),
                         (channel.prompt_file("screen_banner.md"), "screen_banner.md")):
        remote.put(source, f"{rd}/work/{name}")
    body = (f"mkdir -p {rd}/work/banner\n"
            + root_script(remote_python(f"{rd}/work/read_banner.py {rd}/cut/cut.mp4 {rd}/work/banner {t:.3f}"),
                          env=["cpa"], owned=f"{rd}/work/banner", recursive=True))
    result = _run_remote(ctx, "work-banner", body, timeout=BANNER_TIMEOUT_S)
    if result.rc != 0:
        raise RuntimeError(f"read_banner.py rc={result.rc}: {result.stderr.strip()[-200:]}")
    local = _local(ctx, "work/banner.json")
    remote.get(f"{rd}/work/banner/banner.json", local)
    doc: dict[str, JsonValue] = json.loads(local.read_text(encoding="utf-8"))
    out["cpa"].append(_record(doc))
    try:
        value = _json_from_text(str(doc.get("answer") or ""))
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return False
    out["banner"], out["work"] = (str(value.get(k) or "").strip() for k in ("banner", "work"))
    out["read_from"] = str(value.get("from") or "").strip()
    return True


def _look_up(ctx: Ctx, out: WorkContext) -> None:
    graph: work_lookup.TopicGraph = json.loads(work_lookup.GRAPH_PATH.read_text(encoding="utf-8"))
    web = ctx.adapters.lookup
    found = work_lookup.lookup(out["work"], graph, web=web)
    subject = pick_subject(out["work"], found["results"])
    if subject is None:
        errors = [str(layer["error"]) for layer in found["layers"] if layer["layer"] == "bangumi" and layer.get("error")]
        if errors:
            raise RuntimeError(f"Bangumi lookup failed: {errors[0]}")
        out["status"], out["reason"] = "BANNER_ONLY", "名称库里没有名字对得上的条目"
        return
    characters = characters_of(int(cast("str | int", subject["id"])), web)
    out["status"] = "FOUND"
    out["subject"] = {"id": subject["id"], "name": subject.get("name"), "name_cn": subject.get("name_cn"),
                      "source": "bangumi"}
    out["characters"] = characters
    out["names"] = work_lookup.spoken_names(characters)


def build(ctx: Ctx, *, cut_len: float, retry_unavailable: bool = True) -> WorkContext:
    """Read the bar and look the work up (see the module note).  `retry_unavailable` False keeps a saved UNAVAILABLE as
    it is (a run that already started on it must see the same input again)."""
    path = _local(ctx, "work/context.json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        status = saved["status"]
    except (OSError, ValueError, KeyError, TypeError):
        saved = status = None                       # nothing saved, or not readable: read the picture
    if saved is not None and (status in REUSABLE or not retry_unavailable):
        return cast(WorkContext, dict(saved, cpa=[]))
    out: WorkContext = {"status": "UNAVAILABLE", "banner": "", "work": "", "subject": None, "characters": [],
                           "names": [], "reason": "", "cpa": []}
    try:
        if not _read_banner(ctx, cut_len, out):
            out["status"], out["reason"] = "NONE", "画面文字栏的回答不是可读的 JSON"
        elif not out["banner"] and not out["work"]:
            out["status"], out["reason"] = "NONE", "画面上没有读到这类文字栏"
        elif not out["work"]:
            out["status"], out["reason"] = "BANNER_ONLY", "文字栏里没有作品名"
        else:
            _look_up(ctx, out)
            if out["status"] == "BANNER_ONLY" and not out["banner"] and out.get("read_from") == "title":
                # the watched video's own title, read as text off the screen: the library not knowing it (a fan video, a
                # show that spells its name its own way) does not make the letters on the screen less true.  The agent
                # gets the title as the screen writes it, as a name a speaker may say (owner a title that
                # deliberately miswrites an idiom was "corrected" back to the idiom).
                out["status"], out["names"] = "TITLE_ONLY", [out["work"]]
                out["reason"] = "名称库里没有这个作品；这是被看视频画面上自己的标题，按画面写法给出"
            elif out["status"] == "BANNER_ONLY" and not out["banner"]:
                # a work recognised from the picture alone, which the name library does not know: not shown to the agent
                out["status"], out["work"], out["reason"] = "NONE", "", "画面上认出的作品名在名称库里对不上，不采用"
    except Exception as exc:  # noqa: BLE001 - a lead for the agent, never a reason to stop the subtitle step
        out["status"], out["reason"] = "UNAVAILABLE", f"{type(exc).__name__}: {str(exc)[:200]}"
    _write_atomic(path, json.dumps(out, ensure_ascii=False, indent=1))
    return out
