"""Step 6 title: title checks and the publish gate, then the step body that picks the title and renders
the cover.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from src.autoslice.channel_envelope import EnvelopeError, HOST_NAMES, parse_talk_title, render_talk_title

from .. import cover_layout_rotation
from . import channel, collab_guests, cover_guests, cover_scope
from .publication_id import publication_cid
from . import (circle_context, cover_import, gates, graph_context, normalize, parallel, text_overlap,
               title_acceptance, title_authority, title_history, title_names, title_pick,
               title_understanding, title_verdicts, writing_canon)
from .cover_redo import RetryBudget
from .name_consistency import danmaku_pool
from .runner import Ctx, StepOutcome
from .config import participant_names, placeholder_as_person, unknown_participant
from .speaker_v8 import GUEST_LABEL, HOST
from .srt_text import SrtCue, _json_from_text, parse_srt
from .step6_cover import (
    COVER_FACE_BOX,
    COVER_PERSON_FALLBACK,
    COVER_RETRIES,
    COVER_TEXT_TOO_LONG,
    cover_line_limits,
    cover_line,
    cover_layout_table,
    cover_lines_overflow,
    CoverRenderResult,
    _cover_brief,
    _compilation_context,
    _pick_cover_frame,
    chosen_action,
    _rect_overlap,
    _render_cover,
    blur_boxes,
    cover_plan,
    cover_vision_calls,
)
from .step_io import (CpaVisionUnavailable, _ask_cpa, _clip, _cpa_ok, _fail, _local, _log, _render, ask_json,
                      judge_run_fallback, json_asker, merge_judge_fallbacks)

if TYPE_CHECKING:
    from .state import CpaEvent
    from .step6_cover_types import CoverBrief
    from .subtitle_track import TrackCue

# ----- step 6: title and cover (CPA picks the title; the cover is rendered from CPA's lines) ------


TITLE_MAX_REGEN = 2                              # regeneration rounds after the first candidate round
JUDGED_PER_ROUND = 3     # candidates of a round sent to the judges in turn (owner-taste order) before the round is rewritten
COVER_TEXT_FIXES = 2     # rewrites of a cover text the fact judge or a check on its lines refused; the title is kept
COVER_TEXT_NOT_ACCEPTED = "COVER_TEXT_NOT_ACCEPTED"
# The deterministic checks that veto a candidate: what a machine can decide and no title may break (banned words, the
# envelope, quotations, name spellings, the publish gate, a repeat of a published title or of a batch's scene opener, a
# speaker label written as a name).  How the title is written is the writing judge's (skill 八.4): on 11-23/24, of 440
# candidates, the checks that tried to read it mechanically threw out 113 on the writer's own swap test, 37 on a first
# person and 44 on a batch opener (1621740: both candidates on the line the owner then asked for opened with a guest's
# name, as another clip's title did).
TITLE_RULES = ("banned", "prefix", "on_stage_known", "named", "involved_named", "prefix_names", "quotes",
               "canonical_names", "publish_gate", "published_duplicate", "batch_opener", "label_as_name")
OPENER = 3      # a title's first characters after the prefix; two clips of one batch do not open alike (11-23: 8 of 10 「游戏里…」)


class TitleChecks(TypedDict):
    ok: bool
    reasons: list[str]
    failed: list[str]
    score: int


class JudgeRun(title_authority.JudgeVerdict):
    call_id: str
    model: NotRequired[str | None]
    fallback_from: NotRequired[str]


class TitleJudge(TypedDict):
    overall: str                               # the title's verdict: the fact judge on the title, and the writing judge
    fail_reasons: list[str]
    runs: list[JudgeRun]                       # the fact judge (skill 八.3); each run carries its cover verdict
    quality: list[JudgeRun]                    # the writing judge (skill 八.4: section 四)
    # the fact judge on the cover lines, apart from the title; absent on a receipt under an imported cover (its own text)
    cover: NotRequired[title_authority.JudgeVerdict]
    authorities: list[title_authority.SourceRecord]
    fallback_models: NotRequired[list[str]]


class TitleCandidate(TypedDict, total=False):
    title: str
    title_body: str
    landing: int                               # which landing of the clip it is written on (title_mainline.md's number)
    on_stage: list[str]
    involved: list[str]                        # the other people the title is misread without, as the title writes them
    punchline: str
    checks: TitleChecks
    round: int
    accepted: bool
    judge: TitleJudge
    name_fixes: list[title_names.NameFix]      # case-only spellings of a formal name, corrected
    prefix_repeat: bool                        # a partner named in the prefix is named again in the body
    pick: title_pick.PickItem                  # the owner-taste ranking of this candidate (title_pick)
    owner_approved: bool                       # its body is the one the owner picked for this clip (title_verdicts)
    prior_accepted: bool                       # an earlier accepted title (title/accepted.json) entered in round 0
    prior_title: str                           # the receipt's original title string before canon/case normalization
    prior_cover: list[str]                     # cover lines saved on the receipt (empty if absent)


class TitleAnswer(TypedDict, total=False):
    candidates: list[TitleCandidate]
    cover_lines: list[str]
    recommended: int | None


class TitleRound(TypedDict):
    call_id: str
    parsed: TitleAnswer
    candidates: list[TitleCandidate]
    order: list[int]                     # the candidates in the order they are tried: the owner-taste ranking, else the model's
    pick_call: title_pick.PickCall | None   # the ranking call of this round (None: nothing to rank)
    coverage: LandingCoverage            # how its candidates fall on the clip's landings


LANDING_MIN = 2     # candidates per landing (title_mainline.md)


class LandingCoverage(TypedDict):
    """How a round's candidates fall on the clip's landings (title_understanding: 1-based, as title_mainline.md numbers
    them).  On 11-23/24 the moment the selection pointed at was written by 0 of 8 to 0 of 48 candidates (标题没落在最关键的那一下，就是流水线的问题), so this is counted, not left to the prompt."""
    counts: list[int]           # candidates on landing 1, 2, ...
    stray: list[int]            # candidates (0-based) whose `landing` names none of them
    picked: list[int]           # the landings the selection pointed at (`picked`)
    short: list[int]            # landings with fewer than LANDING_MIN candidates
    picked_missing: list[int]   # picked landings no candidate is written on, when the round names any landing


def landing_coverage(candidates: Sequence[TitleCandidate], landings: Sequence[title_understanding.Landing]) -> LandingCoverage:
    counts, stray = [0] * len(landings), []
    for i, cand in enumerate(candidates):
        if cand.get("prior_accepted"):
            continue
        n = cand.get("landing")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(landings):
            counts[n - 1] += 1
        else:
            stray.append(i)
    picked = [k for k, landing in enumerate(landings, 1) if landing.get("picked")]
    return {"counts": counts, "stray": stray, "picked": picked,
            "short": [k for k, n in enumerate(counts, 1) if n < LANDING_MIN],
            # a round that names no landing at all is only noted (stray), as _other_landing takes it
            "picked_missing": [k for k in picked if not counts[k - 1]] if any(counts) else []}


def on_picked_landing_first(order: Sequence[int], candidates: Sequence[TitleCandidate], coverage: LandingCoverage) -> list[int]:
    """The judges' order: candidates on a landing the selection pointed at first, each group in the taste order (brief
    11-24 #5: in one layer of draws the landing is the one the selection pointed at)."""
    picked = set(coverage["picked"])
    return sorted(order, key=lambda i: candidates[i].get("landing") not in picked)


def coverage_feedback(coverage: LandingCoverage) -> list[str]:
    """What the rewrite is told of the landings the round did not cover."""
    out = [f"标〔选片点出〕的第 {k} 处落点一条候选也没写：这一轮不送判官，重写时先写它（至少 {LANDING_MIN} 条）"
           for k in coverage["picked_missing"]]
    out += [f"第 {k} 处落点只写了 {coverage['counts'][k - 1]} 条（每处至少 {LANDING_MIN} 条）"
            for k in coverage["short"] if k not in coverage["picked_missing"]]
    if coverage["stray"]:
        out.append("候选 " + "、".join(str(i + 1) for i in coverage["stray"])
                   + f" 的 landing 不是 1 到 {len(coverage['counts'])} 里的一处落点")
    return out


def coverage_process_line(coverage: LandingCoverage, judged: Sequence[TitleCandidate], round_no: int) -> str:
    """The PROCESS line of a round's landings: the candidates on each, and whether one on a picked landing was judged."""
    picked = set(coverage["picked"])
    each = "、".join(f"{k}{'〔选片点出〕' if k in picked else ''}×{n}" for k, n in enumerate(coverage["counts"], 1))
    sent = [c.get("landing") for c in judged]
    tail = ("；送判官的没有一条在选片点出的落点上" if picked and sent and not picked.intersection(sent) else "")
    notes = coverage_feedback(coverage)
    return (f"落点覆盖（第 {round_no + 1} 轮）：{each}；送判官的落点 {sent or '无'}{tail}"
            + ("；" + "；".join(notes) if notes else ""))


def publish_gate_violations(raw_title: str) -> list[str]:
    """The publish gate, called (not copied) for a talk title the way the upload path calls it: the title policy with
    its length gate (publish_title_policy_violations), the talk envelope's partner check (talk_envelope_violations,
    the title-only call candidate_title_policy_violations makes), and the canonical envelope the upload re-renders
    (canonicalize_publish_title, compared as final_human_review compares it).  The raw title is passed unstripped, as
    the upload passes the manifest title.  The candidate-surface check needs the candidate id, which step 6 does not
    have: it stays at step 8 and is recorded in PROCESS.  A gate that cannot judge the title blocks it, with its reason."""
    from src.autoslice.channel_envelope import talk_envelope_violations
    from src.autoslice.title_policy import (
        canonicalize_publish_title,
        publish_title_length_violations,
        publish_title_policy_violations,
    )

    codes: list[str] = []
    try:
        codes.extend(publish_title_policy_violations(raw_title, lane="talk"))
        codes.extend(c for c in publish_title_length_violations(raw_title, lane="talk") if c not in codes)
        codes.extend(c for c in talk_envelope_violations(raw_title.strip()) if c not in codes)
        if canonicalize_publish_title(raw_title, lane="talk") != raw_title.strip():
            codes.append("publish_title_not_canonical")
    except Exception as exc:  # noqa: BLE001 - the gate could not judge the title: it blocks, with the reason
        codes.append(f"publish_gate_error:{type(exc).__name__}")
    return codes


SELF_REPORT = ("on_stage_known",)     # rules on the writer's own account of its candidate


def opener(title: str) -> str:
    """The first OPENER letters of a title's body (after 【…】); "" when it is shorter."""
    envelope = parse_talk_title(title)
    letters = text_overlap.letters(envelope.body if envelope is not None else title)
    return letters[:OPENER] if len(letters) >= OPENER else ""


def _opens_with_name(title: str, names: Sequence[str]) -> bool:
    """The title's body starts with a person's name: a participant's (whole, or its Chinese part, the short form titles
    use) or the host's.  Two clips of a batch may both open with who speaks or is spoken to."""
    envelope = parse_talk_title(title)
    body = (envelope.body if envelope is not None else title).lstrip("“「『\"")
    short = [m.group() for n in names if (m := re.match(r"[一-鿿]{2,}", n))]
    return body.startswith((*names, *short, *HOST_NAMES))


def title_checks(cand: TitleCandidate, names: Sequence[str], subtitles: str, *,
                 envelope_error: str = "", published: tuple[str, ...] = (), batch: Sequence[str] = (),
                 non_formal: Sequence[title_names.NonFormal] = (), formal: Sequence[str] = (),
                 self_report: bool = True) -> TitleChecks:
    """The deterministic checks on one candidate (TITLE_RULES).  Each rule it breaks is named once in `reasons`;
    `score` counts the rules the candidate passes (len(TITLE_RULES) is perfect).  `subtitles` is the final SRT with
    the speaker labels where there are any: quoted words are checked against it by the repo's checker
    (title_quote_check; skill section 8 items 1-2: what that person said, verbatim or cut down).
    `non_formal` are the non-formal spellings of a name found in the body (title_names); `formal` the formal names the
    clip's subtitles mention.  `batch`: the titles the other clips of this batch already have
    (title_history.recent_titles).  `self_report=False` leaves out the rules on the writer's own account of its
    candidate (SELF_REPORT): a title written without one (the review's) is judged by the skill's judge instead."""
    from src.autoslice.title_quote_check import check_title_quotes

    title = str(cand.get("title", "")).strip()
    failed: dict[str, str] = {}
    if title_history.duplicate_title(title, published):
        failed["published_duplicate"] = "标题与已发标题重复，请依据本片字幕重写"
    if leaked := label_as_name(title, subtitles):
        failed["label_as_name"] = LABEL_REASON.format(leaked)
    if not _opens_with_name(title, names) and (alike := [t for t in batch if opener(t) and opener(t) == opener(title)]):
        failed["batch_opener"] = (f"开头「{opener(title)}」和本批已起的「{alike[0]}」一样；同一批的片各用各的开头和句式，"
                                  "从本片最好笑的那一下起句")
    banned = gates.title_banned(title)
    if banned:
        failed["banned"] = "禁词：" + ",".join(banned)
    envelope = parse_talk_title(title)
    if envelope is None:
        failed["prefix"] = f"前缀不是【{channel.HOST}】或【{channel.HOST}×出场者】"
    on_stage = [str(x).strip() for x in (cand.get("on_stage") or []) if str(x).strip()]
    unknown = [n for n in on_stage if n not in names]
    if not on_stage:
        failed["on_stage_known"] = "on_stage 为空，未列出出场参与者"
    elif unknown:
        failed["on_stage_known"] = "出场者不在参与者名单：" + "、".join(unknown)
    not_named = [n for n in on_stage if n not in title]
    if not_named:
        failed["named"] = "标题未点名出场者：" + "、".join(not_named)
    involved = [str(x).strip() for x in (cand.get("involved") or []) if str(x).strip()]
    # a person the writer says the title is misread without, when the clip knows a name for them (a participant or a
    # formal name its subtitles mention): 「另一位通话同伴」 names nobody and is the writing judge's to weigh
    left_out = [n for n in dict.fromkeys(involved) if n not in HOST_NAMES and n not in title
                and any(n in known or known in n for known in (*names, *formal))]
    if left_out:
        failed["involved_named"] = "标题里没有这个人的名字，少了会读歪：" + "、".join(left_out)
    partners = tuple(dict.fromkeys(n for n in on_stage if n not in HOST_NAMES))
    if (envelope_error or (envelope is None and partners)
            or (envelope is not None and set(envelope.partners) != set(partners))):
        failed["prefix_names"] = f"前缀未写出场者或与出场名单不一致（应为【{channel.HOST}×…】）"
        if envelope_error:
            failed["prefix_names"] += "：" + envelope_error
    # a guest line labelled 连线 / 未具名嘉宾 is nobody's in particular: two short names or 队友 may stand before it
    quotes = check_title_quotes(title, subtitles, unidentified=(GUEST_LABEL, UNNAMED), people=names)
    if quotes["status"] != "PASS":
        failed["quotes"] = "引号核对未过：" + ",".join(quotes["reason_codes"])
    if non_formal:
        failed["canonical_names"] = "名字写法不是正式写法：" + "；".join(
            f"{n['written']} 应写成 {n['formal']}" for n in non_formal)
    publish = publish_gate_violations(str(cand.get("title", "")))   # the same gate as step 8, raw codes into reasons
    if not self_report:
        failed = {k: v for k, v in failed.items() if k not in SELF_REPORT}
    reasons: list[str] = []
    failed_keys: list[str] = []
    for key in TITLE_RULES:
        if key == "publish_gate":
            if publish:
                reasons.extend(publish)
                failed_keys.append(key)
        elif key in failed:
            reasons.append(failed[key])
            failed_keys.append(key)
    return {"ok": not failed_keys, "reasons": reasons, "failed": failed_keys,
            "score": len(TITLE_RULES) - len(failed_keys)}


ONLINE_RECHECK = ("quotes", "label_as_name")   # what a revision's subtitles decide; who 我 is, is the writing judge's
TITLE_RECEIPT_HINT = ("lane_publish.py metadata-plan / metadata-run / metadata-verify-live（第 90 步；改后登记表改绑这条稿件），"
                      "再 manual_lane redo --from 8 重建修订依据")


def online_title_breaks(ctx: Ctx, names: Sequence[str], online: str, cpa_calls: list[CpaEvent]
                        ) -> tuple[list[str], TitleJudge | None] | StepOutcome:
    """Why the published title a revision would keep is wrong on the revision's subtitles and speakers, and the judges'
    verdict on it: the rules that read the subtitles (ONLINE_RECHECK), then the writing judge (skill 八.4) when they hold;
    ([], verdict) when it still holds.  Owner 11-23 (U467), after the speakers of three collab clips were redone from his
    labels: 「所以这个标题可能需要重起，你去看一眼」 — 625460's title quoted a line the labels gave to two people, and the
    revision had kept it without looking; 11-24 (U471): 897000's revision kept 「挖矿找不到暂停决定退出，…玩爽了」, which
    only the writing judge would have stopped.  The fact judge is not asked: the cover the title was published with is
    not this round's, and the subtitles' facts are what ONLINE_RECHECK reads.  What this finds does not replace the
    title: the revision keeps it and records these reasons with it (title_acceptance "online_kept"); only the owner
    gives a published manuscript another title (title_owner_verdicts approved / retitle)."""
    _track, lines, srt_text = title_inputs(ctx, names)
    checks = title_checks(cast(TitleCandidate, {"title": online, "on_stage": list(names)}), names, srt_text,
                          self_report=False)
    why, at = {}, 0
    for key in (k for k in TITLE_RULES if k in checks["failed"]):     # reasons are in rule order; the gate gives several
        width = len(publish_gate_violations(online)) if key == "publish_gate" else 1
        why[key] = "；".join(checks["reasons"][at:at + width])
        at += width
    breaks = [why[k] for k in ONLINE_RECHECK if k in why]
    if breaks:
        return breaks, None
    judged = title_judge(ctx, "title-judge-online", lines, online, (), cpa_calls, facts=False)
    if isinstance(judged, StepOutcome):
        return judged
    return (["线上标题不合 skill 第四节：" + "；".join(judged["fail_reasons"])] if judged["overall"] != "PASS" else []), judged


def checks_on(ctx: Ctx, title: str) -> TitleChecks:
    """Step 6's checks on a whole title written after step 6 (the review's rewrite): the same `title_checks`, on the
    chosen candidate's reading of the story (on_stage, involved, swap, punchline person) with the new title, against
    this batch's and the published titles.  11-23: the review rewrote 443680's title to open like another clip of the
    batch (「游戏里…」) and to put a guest's proposal in the host's mouth, past checks it only partly repeated."""
    names = participant_names(ctx.session)
    track, _lines, srt_text = title_inputs(ctx, names)
    path = ctx.clip_dir / "title/candidates.json"
    chosen = (json.loads(path.read_text(encoding="utf-8")).get("chosen") or {}) if path.exists() else {}
    envelope = parse_talk_title(title)
    cand = cast(TitleCandidate, {**chosen, "title": title})
    if not chosen.get("on_stage"):      # no candidate of step 6 (an owner's title): the prefix says who is on stage
        cand["on_stage"] = [HOST, *(envelope.partners if envelope is not None else ())]
    history = title_history.recent_titles(ctx.state.path, ctx.clip_dir, str(ctx.state.data["clip"].get("cid") or ""),
                                          ctx=ctx)
    formal = title_names.canonical_names(title_authority.subtitle_text(track), names)
    return title_checks(cand, names, srt_text, published=history.published_titles, batch=history.batch,
                        non_formal=formal.non_formal(envelope.body if envelope is not None else title),
                        formal=[e.canonical for e in formal.entries], self_report=False)


def _feedback_text(candidates: Sequence[TitleCandidate], coverage: LandingCoverage | None = None) -> str:
    lines = ["上一轮候选没有一条通过自检和验收，请针对下面的原因重写（编辑方法不变）：",
             *(coverage_feedback(coverage) if coverage else [])]
    for i, cand in enumerate(candidates, 1):
        reasons = list(cand["checks"]["reasons"])
        judge = cand.get("judge") or cast(TitleJudge, {})
        if judge.get("overall") == "FAIL":
            reasons.append("判官（skill 第八节第 3 条）：" + "；".join(judge.get("fail_reasons") or []))
        lines.append(f"候选 {i}（{cand.get('title', '')}）：" + ("；".join(reasons) if reasons else "通过"))
        if cand.get("pick"):
            lines.append(title_pick.feedback_line(cand["pick"]))
    return "\n".join(lines)


def _title_body(title: str) -> str:
    envelope = parse_talk_title(title)
    return envelope.body if envelope is not None else title


def _round_order(parsed: TitleAnswer, cands: Sequence[TitleCandidate]) -> list[int]:
    rec = parsed.get("recommended")
    return ([rec] if isinstance(rec, int) and 0 <= rec < len(cands) else []) + \
        [i for i in range(len(cands)) if i != rec]


def title_judge(ctx: Ctx, call_id: str, track_lines: str, title: str, cover_lines: Sequence[str],
                cpa_calls: list[CpaEvent], *, facts: bool = True, writing: bool = True) -> TitleJudge | StepOutcome:
    """The skill's acceptance judges, the one interface every title that goes out passes (step 6's pick, a revision's
    published title it would keep, the review's rewrite): the fact judge (section 八 item 3, references/judge-prompt.md,
    JUDGE_RUNS runs) and the writing judge (八 item 4: section 四, references/quality-judge-prompt.md, QUALITY_RUNS
    runs, each verdict recomputed from its items; when the two differ a third is asked and the majority counts,
    title_acceptance.writing_passed).  `overall` is the title's verdict: every fact run passes the title and the
    writing runs pass it.  `cover` is the fact judge's verdict on the cover lines, apart: step 6 rewrites a cover text
    that fails and keeps the title (11-23/24: of 8 fact failures all 8 named the cover text, and the titles went with
    it); a caller with a cover it cannot rewrite asks for both.  Returns the verdict (each run, the authority hashes),
    or the failed outcome when CPA cannot be reached.

    the channel's request — the 11-23 collab titles
    passed the fact judge, the only judge then, and broke section 四 (no mishap, a narrator's 流水账, a 我 nobody could
    place).  TASK-31: the runs are independent answers (each its own cache entry, `run`), asked at once (parallel.run,
    bound lane_rules concurrency.cpa); runs and records are reported in run order.  `facts=False` asks the writing
    judge alone (a published title a revision would keep, whose facts ONLINE_RECHECK reads); `writing=False` the fact
    judge alone (a rewritten cover text under a title already accepted)."""
    prompt, sources = title_authority.judge_prompt(track_lines, title, cover_lines)
    q_prompt, q_sources = title_authority.quality_prompt(track_lines, title)
    asks: list[tuple[str, str, int, Callable[[object], title_authority.JudgeVerdict]]] = [
        (f"{call_id}-{run}", prompt, run, title_authority.judge_verdict) for run in range(1, title_authority.JUDGE_RUNS + 1) if facts]
    asks += [(f"{call_id}-q{run}", q_prompt, run, title_authority.quality_verdict)
             for run in range(1, title_authority.QUALITY_RUNS + 1) if writing]
    records = parallel.run(asks, lambda a: _ask_cpa(ctx, a[0], a[1], run=a[2], purpose="title-judge"), provider="cpa")
    runs: list[JudgeRun] = []
    quality: list[JudgeRun] = []
    fallback_models: list[str] = []

    def read(cid: str, record: CpaEvent, verdict: Callable[[object], title_authority.JudgeVerdict]) -> StepOutcome | None:
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=cpa_calls)
        try:
            parsed = _json_from_text(record["response_text"])
        except ValueError:
            parsed = None
        (quality if verdict is title_authority.quality_verdict else runs).append(
            cast(JudgeRun, {**verdict(parsed), **judge_run_fallback(cid, record, fallback_models)}))
        return None

    for (cid, _prompt, _run, verdict), record in zip(asks, records, strict=True):
        if failed_call := read(cid, record, verdict):
            return failed_call
    if len({r["overall"] for r in quality}) == 2:      # the two writing runs differ: a third decides
        run = title_authority.QUALITY_RUNS + 1
        cid = f"{call_id}-q{run}"
        if failed_call := read(cid, _ask_cpa(ctx, cid, q_prompt, run=run, purpose="title-judge"),
                               title_authority.quality_verdict):
            return failed_call
    failed = [r for r in runs if r["overall"] != "PASS"]
    q_failed = [] if not writing or title_acceptance.writing_passed(quality) else [r for r in quality if r["overall"] != "PASS"]
    cover_failed = [cast(title_authority.FactVerdict, r)["cover"] for r in runs]
    cover_reasons = list(dict.fromkeys(x for c in cover_failed if c["overall"] != "PASS" for x in c["fail_reasons"]))
    reasons = [*(x for r in failed for x in r["fail_reasons"]),
               *("写法（第四节）：" + x for r in q_failed for x in r["fail_reasons"])]
    out: TitleJudge = {"overall": "FAIL" if failed or q_failed else "PASS", "fail_reasons": list(dict.fromkeys(reasons)),
                       "runs": runs, "quality": quality,
                       "cover": {"overall": "FAIL" if cover_reasons else "PASS", "fail_reasons": cover_reasons},
                       "authorities": [s.as_dict() for s in dict.fromkeys([*sources, *q_sources])]}
    if fallback_models:
        out["fallback_models"] = fallback_models
    return out


def cover_lines_on_cover(ctx: Ctx) -> list[str] | None:
    """The cover lines a title is judged with after step 6 (title_current, the review's rewrite): title/candidates.json's,
    or None under an imported cover, which brings its own text (cover_import)."""
    if cover_import.imported_cover(ctx) is not None:
        return None
    path = ctx.clip_dir / "title/candidates.json"
    return [str(x) for x in (json.loads(path.read_text(encoding="utf-8")).get("cover_lines") or [])] if path.exists() else []


def with_cover_of(judged: TitleJudge, cover: Sequence[str] | None) -> TitleJudge:
    """`judged` as the receipt records it: without a verdict on cover lines no cover shows (`cover` None: imported)."""
    return judged if cover is not None else cast(TitleJudge, {k: v for k, v in judged.items() if k != "cover"})


def judged_whole(judged: TitleJudge) -> list[str]:
    """Why a title does not go out with the cover text it was judged with, for the callers that cannot rewrite the
    cover (the review's rewrite, a title re-judged before packaging): the title's reasons, then the cover's; [] = both
    pass."""
    cover = judged.get("cover")
    return [*(judged["fail_reasons"] if judged["overall"] != "PASS" else []),
            *("封面字：" + x for x in (cover.get("fail_reasons", []) if cover else []))]


def title_inputs(ctx: Ctx, names: Sequence[str]) -> tuple[list[TrackCue], str, str]:
    """The final cues with their speakers (speaker.srt when there is one; skill section 3 step 1), as the prompt's
    numbered lines and as SRT for the quote checker.  speaker.srt's guest label stands for the one guest."""
    from . import subtitle_track   # imports stage_impl, which re-exports this module: imported here, not at the top

    from .guest_identity import saved_lines

    track = subtitle_track.read_track(ctx.clip_dir)
    rename = title_authority.speaker_names(names, host=HOST, guest_label=GUEST_LABEL)
    if unnamed_guests(ctx, track):
        rename[GUEST_LABEL] = UNNAMED
    known = saved_lines(ctx)      # guest lines told apart (guest_identity): labelled with her name
    labelled = [cast("TrackCue", {**c, "speaker": known[i]}) if i in known else c for i, c in enumerate(track, 1)]
    return track, title_authority.numbered_lines(labelled, rename), title_authority.speaker_srt(labelled, rename)


def title_current(ctx: Ctx, cpa_calls: list[CpaEvent]) -> StepOutcome | str:
    """The title that goes out, judged by the skill as it is now: when its receipt's judges read another version
    (title_acceptance.stale), both judges are asked again on the same title, the subtitles and the cover lines (the
    writing judge alone for a published title a revision keeps, as when it was kept).  Passing rewrites the receipt
    with the new verdict, same writer; failing stops with NOT_ACCEPTED (redo from step 6), except a kept published
    title failing on itself: it stays, the receipt becomes `online_kept` with the reasons (step 6 would only keep it
    again).  Returns that failure, or
    the PROCESS line ("" when the receipt was already the current skill's).  Step 8 and step 10 call it before a
    package is built and before anything is sent."""
    current = title_authority.judge_authorities()
    receipt = title_acceptance.stale(ctx.clip_dir, current)
    if receipt is None:
        return ""
    title, by = str(receipt.get("title") or "").strip(), str(receipt["by"])
    judge = receipt.get("judge")
    old = judge.get("authorities") if isinstance(judge, dict) else None
    versions = (" / ".join(str(a.get("sha256", ""))[:8] for a in (old if isinstance(old, list) else [])
                           if isinstance(a, Mapping)) or "没有记录") + " → " + " / ".join(str(a["sha256"])[:8] for a in current)
    _track, lines, _srt = title_inputs(ctx, participant_names(ctx.session))
    cover = cover_lines_on_cover(ctx)
    judged = title_judge(ctx, "current-title-judge", lines, title, cover or [], cpa_calls, facts=by != "online")
    if isinstance(judged, StepOutcome):
        return judged
    judged = with_cover_of(judged, cover)
    kept_cover = (cover if (by != "online" and cover and isinstance(judged.get("cover"), dict)
                            and judged["cover"].get("overall") == "PASS") else None)
    involved = cast("Sequence[str] | None", receipt.get("involved"))
    retitle_evidence = cast("str | None", receipt.get("retitle_evidence"))
    if (why := judged_whole(judged)) and by == "online" and judged["overall"] != "PASS" and why == judged["fail_reasons"]:
        # only the published title is against: it stays, as at step 6 (60-title: a revision changes its title only by
        # the owner's word); the cover text failing still sends the clip back to step 6, which rewrites it
        title_acceptance.write(ctx.clip_dir, title, "online_kept", judged, objections=why, cover_lines=kept_cover,
                               involved=involved, retitle_evidence=retitle_evidence)
        return (f"沿用的线上标题「{title}」是按另一版标题技能判过的（{versions}），按现行技能重判写法不过，照样沿用、不换题，"
                f"异议记进 {title_acceptance.RECEIPT} 交频道主：" + "；".join(why)[:300])
    if why:
        return _fail(title_acceptance.NOT_ACCEPTED,
                     f"标题「{title}」是按另一版标题技能判过的（{versions}），按现行技能重判不过："
                     + "；".join(why)[:300] + "；从第 6 步重做", cpa=cpa_calls)
    title_acceptance.write(ctx.clip_dir, title, by, judged, cover_lines=kept_cover, involved=involved,
                           retitle_evidence=retitle_evidence)
    return f"标题是按另一版标题技能判过的（{versions}），按现行技能重判，两位判官都过：{title}"


UNNAMED = "未具名嘉宾"
SPEAKER_LABELS = (GUEST_LABEL, UNNAMED)     # speaker tags, never names: a title that writes one names nobody


LABEL_REASON = "「{}」是说话人标签，不是名字；写字幕里叫出的名字，认不出就写“队友”这类泛称或不写"


def label_as_name(text: str, subtitles: str) -> str:
    """The speaker label `text` writes as if it were a word of the clip: one of SPEAKER_LABELS that the subtitles'
    lines never say (their [speaker] tags aside).  Title, cover text and the review's title fix all go out under it."""
    said = re.sub(r"^\[[^\]]+\]\s*", "", subtitles, flags=re.M)
    return next((w for w in SPEAKER_LABELS if w in text and w not in said), "")


def unnamed_guests(ctx: Ctx, track: Sequence[TrackCue]) -> bool:
    """The clip's other speakers have no name the lane knows: a participant of the session whose identity is unknown,
    or guests step 4 heard by voiceprint on a day whose session names none (guests_heard_v8; 11-23: the titles wrote
    the label 「连线」 as a person, 「连线提议反矮人联盟」)."""
    if len(participant_names(ctx.session, visual=True)) > len(participant_names(ctx.session)):
        return True
    return (not [n for n in participant_names(ctx.session) if n != HOST]
            and any(c.get("speaker") == GUEST_LABEL for c in track))


def _speaker_note(track: Sequence[TrackCue], names: Sequence[str], *, anonymous: bool = False) -> str:
    if not any(c.get("speaker") for c in track):
        return "字幕没有说话人标签：判断不了是谁说的，就不写说话人。"
    if anonymous:
        return "每句前的 [说话人] 来自 speaker.srt（已核对）；[未具名嘉宾] 是身份未知的说话角色。"
    guest = title_authority.speaker_names(names, host=HOST, guest_label=GUEST_LABEL).get(GUEST_LABEL)
    others = [n for n in names if n != HOST]
    if guest:
        tail = f"；本场连线只有一位，标作 [{guest}]。"
    elif len(others) >= 2:
        tail = (f"；标着 [{'] / ['.join(others)}] 的句子已按识别器的说话人分组和对话证据定到那位嘉宾，可以写成她说的；"
                f"[{GUEST_LABEL}] 是本场连线的{'、'.join(others)}中认不出是哪位的一句：正文可以点名本场连线的人，"
                "这样的句子只有字幕里叫出名字时才写成那个人说的。")
    else:
        tail = f"；[{GUEST_LABEL}] 是{channel.HOST}以外的连线方。"
    return "每句前的 [说话人] 来自 speaker.srt（已核对），引语归属以它为准" + tail


def _raw_title_candidates(answer: TitleAnswer) -> list[TitleCandidate]:
    """Validate the external candidate shapes before the ordinary rendering and checks."""
    raw_candidates = answer.get("candidates", [])
    if not isinstance(raw_candidates, list) or any(not isinstance(c, dict) for c in raw_candidates):
        raise ValueError("candidates must be a list of objects")
    candidates: list[TitleCandidate] = []
    for index, cand in enumerate(raw_candidates):
        if "title_body" in cand and "title" in cand:
            raise ValueError(f"candidate {index} has title_body and title")
        field = "title_body" if "title_body" in cand else "title"
        value = cand.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        raw_stage = cand.get("on_stage")
        if raw_stage is not None and (not isinstance(raw_stage, list)
                                      or any(not isinstance(name, str) for name in raw_stage)):
            raise ValueError("on_stage must be a list of names")
        candidates.append(cand)
    return candidates


def _host_stop(ctx: Ctx, understanding: title_understanding.Understanding, track: Sequence[TrackCue], *,
               owner_chose: bool, cpa_calls: list[CpaEvent]) -> StepOutcome | None:
    """The stop of a clip the reading finds is not about the host (title_understanding.not_about_host: NOT_ABOUT_HOST,
    or NO_HOST_LINES when step 4 labelled none of its lines hers), or None.  Clips the owner asked for go on: he chose
    its title or said its draw (`owner_chose`), or it was seated for his mark in the chat (clip_init.owner_marked).  So
    does the revision of a published clip (`revised_from`): it is already out and whether it stays is the owner's call,
    not this reading's (agent's choice, 11-24)."""
    from .clip_init import owner_marked

    if owner_chose or isinstance(ctx.state.data.get("revised_from"), dict):
        return None
    found = title_understanding.not_about_host(understanding, [c.get("speaker") for c in track])
    if found is None or owner_marked(ctx.state.data["clip"]):
        return None
    reason, note = found
    return _fail(reason, note, cpa=cpa_calls, process=[note])


def _title_rounds(ctx: Ctx, names: Sequence[str], cpa_calls: list[CpaEvent], *,
                  avoid_title: str = "", fresh_attempt: int = 0, fresh_request: str = "",
                  prior_notes: list[str] | None = None
                  ) -> tuple[list[TitleRound], title_authority.TitleMethod, title_history.TitleHistory,
                             title_understanding.Understanding, title_verdicts.OwnerVerdicts,
                             circle_context.CircleContext] | StepOutcome:
    """Round 0 is the first candidate round; then up to TITLE_MAX_REGEN regenerations with the failed checks and the
    judge's reasons as feedback, until a candidate passes both.  When a valid earlier acceptance receipt exists and no
    fresh title was asked (and neither an owner-approved title nor a published revision governs), that earlier title
    enters round 0 as one more candidate (`prior_accepted=True`) and goes through the exact same checks and judges.
    In each round the candidates that pass the deterministic checks go to the judges (title_judge: facts and writing)
    one after another (any passing `prior_accepted` candidate first, then generated candidates in the owner-taste order
    of title_pick, leaving out one predicted 差, at most JUDGED_PER_ROUND of them, none when that order puts a
    predicted 差 first); a judged PASS ends the rounds."""
    notes = prior_notes if prior_notes is not None else []
    want_fresh = fresh_attempt > 0 or bool(fresh_request)
    loaded = title_acceptance.load_receipt(ctx.clip_dir)
    if want_fresh and not avoid_title and loaded.receipt is not None:
        avoid_title = str(loaded.receipt["title"]).strip()
    try:
        cid = str(ctx.state.data["clip"].get("cid") or "")
        method = title_authority.title_method(exclude_candidate=cid)
        verdicts = title_verdicts.load_verdicts()
    except title_authority.AuthorityError as exc:
        return _fail("TITLE_AUTHORITY_MISSING", str(exc), cpa=cpa_calls, process=notes)
    owner_block = verdicts.title_block(cid)
    approved = verdicts.approved(cid)
    track, lines, srt_text = title_inputs(ctx, names)
    understanding_or_fail = title_understanding.understand(ctx, lines, cpa_calls)
    if isinstance(understanding_or_fail, StepOutcome):
        return StepOutcome(understanding_or_fail.ok, reason=understanding_or_fail.reason,
                           note=understanding_or_fail.note, cpa=understanding_or_fail.cpa,
                           process=[*notes, *understanding_or_fail.process])
    understanding = understanding_or_fail
    retitle = verdicts.retitle(cid)
    if stop := _host_stop(ctx, understanding, track, owner_chose=bool(approved or retitle), cpa_calls=cpa_calls):
        return StepOutcome(stop.ok, reason=stop.reason, note=stop.note, cpa=stop.cpa, process=[*notes, *stop.process])
    revises = isinstance(ctx.state.data.get("revised_from"), dict)
    prior_receipt: dict[str, object] | None = None
    if loaded.status == "corrupt":
        notes.append(f"已显式要求重新起标题（原过判记录已损坏：{loaded.error}），重新生成候选" if want_fresh
                     else f"原有过判记录损坏（{loaded.error}），不作为候选加入，正常生成候选")
    elif loaded.status == "unaccepted":
        if not want_fresh:
            notes.append(f"原有过判记录未通过验收（{loaded.error}），不作为候选加入，正常生成候选")
    elif loaded.status == "ok" and loaded.receipt is not None:
        if want_fresh:
            notes.append("已显式要求重新起标题，不沿用原有过判标题")
        elif approved or (revises and not retitle) or loaded.receipt.get("by") == "online_kept":
            prior_receipt = None
        elif retitle and loaded.receipt.get("retitle_evidence") != retitle["evidence"]:
            notes.append(f"频道主要求换标题（原话：{retitle['evidence']}），不沿用原有过判标题「{loaded.receipt['title']}」")
        else:
            prior_receipt = loaded.receipt
    if retitle and not approved:   # he said what the clip's draw is: titles are written from it
        understanding["merged"]["main_line"] = retitle["main_line"]
        understanding["merged"]["landings"] = title_understanding.owner_landing(retitle["punchline"])
        understanding["merged"]["punchline"] = retitle["punchline"]
    formal = title_names.canonical_names(title_authority.subtitle_text(track), names)
    graph = graph_context.read(ctx, [title_authority.subtitle_text(track)], chat=danmaku_pool(ctx.clip_dir))
    circle = circle_context.gather(ctx, understanding["merged"], lines, cpa_calls, graph)
    circle_block = "\n\n".join(filter(None, [graph.block(), circle_context.block(circle)]))   # graph first, then the web
    mainline = "\n\n".join(filter(None, [title_understanding.mainline_block(understanding, formal.prompt_text()),
                                         circle_block]))
    approved_body = formal.fix_case(normalize.canon_text(approved["title_body"])[0])[0] if approved else ""
    if approved:      # his body is written canonically too; the candidates are compared with it in canonical form
        try:
            approved_body = writing_canon.settle_approved(ctx, approved_body, cpa_calls)
        except writing_canon.WritingCanonFailed as exc:
            return writing_canon.failed(exc, cpa_calls, notes)
    basis = title_pick.PickBasis(subtitles=title_authority.subtitle_text(track),
                                 main_line=understanding["merged"]["main_line"],
                                 punchline=understanding["merged"]["punchline"], corpus=verdicts.corpus(cid),
                                 circle=circle_block)
    history = title_history.recent_titles(ctx.state.path, ctx.clip_dir, str(ctx.state.data["clip"].get("cid") or ""), ctx=ctx)
    known_canon = [c for c in writing_canon.load(ctx.clip_dir) if c["where"] in writing_canon.KNOWN_WHERE]
    fresh_base = ""
    if want_fresh:
        if avoid_title:
            fresh_base = (
                "## 本次重起标题要求\n"
                f"已显式要求为本片重新起新标题：不得重复或沿用旧标题「{_title_body(avoid_title)}」"
                f"（完整标题「{avoid_title}」），请换新的切入或表达写出新候选。"
            )
        else:
            fresh_base = "## 本次重起标题要求\n已显式要求为本片重新起新标题，请写出新的标题候选。"
    rounds: list[TitleRound] = []
    feedback = ""
    anonymous = unnamed_guests(ctx, track)
    speaker_note = _speaker_note(track, names, anonymous=anonymous)
    labels: list[str] = []
    if anonymous and len(participant_names(ctx.session, visual=True)) == len(participant_names(ctx.session)):
        speaker_note += (f"[{UNNAMED}] 是声纹听出的别人，不知道是谁：标题、前缀、封面都不要把这个标签当名字写；"
                         "字幕里叫出了对方的名字才写那个名字，认不出就写“队友”这类泛称或不写。")
    elif anonymous:
        labels = [str(g[0]).strip() for g in ctx.session.get("participants") or [] if g and unknown_participant(str(g[0]))]
        speaker_note += ("「连线」是身份未知的说话人标签，不是账号名；前缀和封面署名不要写它，on_stage 只写已知正式名。"
                         if all(n == "连线" for n in labels) else
                         "身份未知的说话人标签不是账号名；前缀和封面署名不要写标签，on_stage 只写已知正式名。")
    for round_no in range(TITLE_MAX_REGEN + 1):
        call_id = "title" if round_no == 0 else f"title-regen-{round_no}"
        prompt = _render("title.md", context=lines, participants="、".join(names), feedback=feedback,
                         speaker_note=speaker_note, compilation_context=_compilation_context(ctx),
                         first_max=str(cover_line_limits()[0]), punch_max=str(cover_line_limits()[1]))
        fresh_block = ""
        if fresh_base:
            tag_parts = [f"请求 {fresh_request}" if fresh_request else "",
                         f"第 {fresh_attempt} 次尝试" if fresh_attempt > 0 else "",
                         f"第 {round_no + 1} 轮"]
            fresh_block = f"{fresh_base}\n（{' · '.join(p for p in tag_parts if p)}）"
        full_prompt = prompt.replace("{method}", "\n\n".join(filter(None, [method.text, history.prompt(), mainline,
                                                                           owner_block, fresh_block])))
        record = _ask_cpa(ctx, call_id, full_prompt, purpose="title")
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=cpa_calls, process=notes)
        try:
            parsed = _json_from_text(record["response_text"])
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):   # not the object the title prompt asks for (a bare list happened, TASK-11)
            return _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=[*cpa_calls[:-1], record], process=notes)
        parsed_answer = cast(TitleAnswer, parsed)
        try:
            candidates = _raw_title_candidates(parsed_answer)
        except ValueError as exc:
            return _fail("CPA_RESPONSE_UNPARSEABLE", f"{record['file']}: {exc}",
                         cpa=[*cpa_calls[:-1], record], process=notes)
        if round_no == 0 and prior_receipt is not None:
            prior_title = str(prior_receipt["title"]).strip()
            raw_cover = prior_receipt.get("cover_lines")
            prior_cover = ([cover_line(x) for x in raw_cover if isinstance(x, str) and x.strip()]
                           if isinstance(raw_cover, list) else [])
            raw_involved = prior_receipt.get("involved")
            prior_involved = ([x.strip() for x in raw_involved if isinstance(x, str) and x.strip()]
                              if isinstance(raw_involved, list) else [])
            prior_cand: TitleCandidate = {
                "title": prior_title,
                "prior_accepted": True,
                "prior_title": prior_title,
                "prior_cover": prior_cover,
            }
            if prior_involved:
                prior_cand["involved"] = prior_involved
            candidates.append(prior_cand)
        for cand_idx, cand in enumerate(candidates):
            value = cand["title_body"] if "title_body" in cand else cand["title"]
            raw_stage = cand.get("on_stage")
            title, name_fixes = formal.fix_case(normalize.canon_text(value)[0])
            envelope_error = ""
            if "title_body" in cand:
                on_stage = list(dict.fromkeys(name.strip() for name in (raw_stage or []) if name.strip()))
                if on_stage and all(name in names for name in on_stage):
                    try:
                        title = render_talk_title(title, [name for name in on_stage if name not in HOST_NAMES])
                    except EnvelopeError as exc:
                        envelope_error = str(exc)
            cand["title"] = title
            envelope = parse_talk_title(title)
            if cand.get("prior_accepted"):
                cand["on_stage"] = [HOST, *(envelope.partners if envelope is not None else ())]
            body = envelope.body if envelope is not None else title
            cand["name_fixes"] = name_fixes
            cand["prefix_repeat"] = envelope is not None and title_names.prefix_repeat(envelope.partners, body)
            cand["checks"] = title_checks(cand, names, srt_text, envelope_error=envelope_error,
                                          published=history.published_titles, batch=history.batch,
                                          non_formal=formal.non_formal(body), formal=[e.canonical for e in formal.entries])
            if cand.get("prior_accepted"):
                if dup_fail := _check_post_canon_duplicate(ctx, cand, round_no, cand_idx, history.published_titles,
                                                           known_canon, cpa_calls, notes):
                    return dup_fail
                if _owner_rejected(verdicts, cid, cand["title"], formal):
                    cand["checks"]["ok"] = False
                    cand["checks"]["failed"].append("owner_rejected")
                    cand["checks"]["reasons"].append("该标题已被频道主判差（否决），请换其他切入重起")
                    cand["checks"]["score"] -= 1
            if (not approved and want_fresh and avoid_title
                    and _body_match_keys(cand["title"], formal).intersection(_body_match_keys(avoid_title, formal))):
                cand["checks"]["ok"] = False
                cand["checks"]["failed"].append("fresh_title_requested")
                cand["checks"]["reasons"].append("已显式要求重新起标题（a new title was asked），不能重复旧标题")
                cand["checks"]["score"] -= 1
            cand["round"] = round_no
            cand["accepted"] = False
        # The cover text is judged apart from the titles (cover_text_problems, the fact judge's cover verdict): what is
        # wrong with it is mended after the pick (_accepted_cover_text, _shortened_cover) and vetoes no title.
        cover = [cover_line(x) for x in (parsed_answer.get("cover_lines") or [])]
        if round_no == 0 and prior_receipt is not None:
            rec_idx = parsed_answer.get("recommended")
            rec_cand = (candidates[rec_idx] if isinstance(rec_idx, int) and not isinstance(rec_idx, bool)
                        and 0 <= rec_idx < len(candidates) - 1 else None)
            if (rec_cand is not None and not prior_cand.get("prior_cover")
                    and rec_cand["title"] == prior_cand["title"] and cover):
                prior_cand["prior_cover"] = list(cover)
        if anonymous:
            for cand in candidates:
                envelope = parse_talk_title(cand["title"])
                fake = (any(unknown_participant(n) for n in (cand.get("on_stage") or [])) or bool(
                    envelope and any(unknown_participant(n) for n in envelope.partners)) or
                    placeholder_as_person(ctx.session, cand["title"]))
                if fake:
                    cand["checks"]["ok"] = False
                    cand["checks"]["failed"].append("placeholder_identity")
                    cand["checks"]["reasons"].append(
                        "连线是身份未知的角色标签，不能作为合作账号或封面署名" if all(n == "连线" for n in labels)
                        else "身份未知是角色标签，不能作为合作账号或封面署名")
                    cand["checks"]["score"] -= 1
        if approved:      # the owner picked the body: the candidate that has it goes first, nothing is ranked
            hits = [i for i, c in enumerate(candidates) if writing_canon.canonical_form(_title_body(c["title"]))
                    == writing_canon.canonical_form(approved_body)]
            for i in hits:
                candidates[i]["owner_approved"] = True
            pick = title_pick.RoundPick(order=[*hits, *(i for i in _round_order(parsed_answer, candidates) if i not in hits)])
        else:
            pick = title_pick.rank_round(
                ctx, f"title-pick-r{round_no}", basis, titles=[c["title"] for c in candidates],
                bodies=[_title_body(c["title"]) for c in candidates],
                passing=[c["checks"]["ok"] and not c.get("prior_accepted") for c in candidates],
                model_order=_round_order(parsed_answer, candidates), cpa_calls=cpa_calls)
        for index, item in pick.items.items():
            candidates[index]["pick"] = item
        coverage = landing_coverage(candidates, understanding["merged"]["landings"])
        order = pick.order if approved else on_picked_landing_first(pick.order, candidates, coverage)
        rounds.append({"call_id": call_id, "parsed": parsed_answer, "candidates": candidates, "order": order,
                       "pick_call": pick.call, "coverage": coverage})
        # The last round has no rewrite after it, so a first predicted 差 (the round is rewritten, none judged) no
        # longer keeps the round from the judges.  Before 11-24 the best of the rounds was taken anyway (forced); now
        # that nothing is chosen by score, a clip would otherwise stop with titles no judge ever read (review).
        last = round_no == TITLE_MAX_REGEN
        # a round that wrote nothing on a landing the selection pointed at is rewritten unjudged (its last one is judged)
        skip = (pick.top_is_bad or (bool(coverage["picked_missing"]) and not approved)) and not last
        gen_tries = [] if skip else [
            candidates[i] for i in order
            if not candidates[i].get("prior_accepted")
            and candidates[i]["checks"]["ok"]
            and (not approved or candidates[i].get("owner_approved"))
            and not title_pick.predicted_bad(candidates[i].get("pick"))][:JUDGED_PER_ROUND]
        prior_tries = [c for c in candidates if c.get("prior_accepted") and c["checks"]["ok"]]
        tries = [*prior_tries, *gen_tries]
        for k, cand in enumerate(tries):      # prior accepted first, then owner-taste order; predicted 差 is skipped
            cand_cover = cand.get("prior_cover", []) if cand.get("prior_accepted") else cover
            judged = title_judge(ctx, f"title-judge-r{round_no}" + (f"c{k + 1}" if k else ""), lines, cand["title"],
                                 cand_cover, cpa_calls)
            if isinstance(judged, StepOutcome):
                return StepOutcome(judged.ok, reason=judged.reason, note=judged.note, cpa=judged.cpa,
                                   process=[*notes, *judged.process])
            cand["judge"] = judged
            cand["accepted"] = judged["overall"] == "PASS"
            if cand["accepted"]:
                break
        if any(c["accepted"] for c in tries):
            break
        feedback = _feedback_text(candidates, coverage)
        if approved:
            feedback += "\n第一条候选的 title_body 必须逐字是：" + approved["title_body"]
    if approved and not any(c.get("owner_approved") for rnd in rounds for c in rnd["candidates"]):
        return _fail("TITLE_APPROVED_NOT_REPRODUCED",
                     f"频道主选定的标题正文 {approved['title_body']} 在 {len(rounds)} 轮里没有一条候选逐字写出",
                     cpa=cpa_calls, process=notes)
    return rounds, method, history, understanding, verdicts, circle


def _choose_title(rounds: Sequence[TitleRound]) -> tuple[TitleCandidate | None, bool]:
    """Each round's candidates in its order (the owner-taste ranking, else the model's recommendation first); the first
    accepted one (preferring an accepted `prior_accepted` candidate after the `owner_approved` and hard-veto filters),
    else the best (forced): the checks' score, a judged candidate before an unjudged one, the earlier in that order
    before the later.  Returns (chosen or None when there is no candidate, forced)."""
    ordered: list[TitleCandidate] = []
    for rnd in rounds:
        ordered += [rnd["candidates"][i] for i in rnd["order"]]
    if any(c.get("owner_approved") for c in ordered):      # the owner's pick is never replaced by another title
        ordered = [c for c in ordered if c.get("owner_approved")]
    ordered = [c for c in ordered if not _HARD_VETO_RULES.intersection(c["checks"]["failed"])]
    if not ordered:
        return None, True
    accepted = [c for c in ordered if c.get("accepted")]
    if accepted:
        prior = [c for c in accepted if c.get("prior_accepted")]
        return (prior[0] if prior else accepted[0]), False
    return max(ordered, key=lambda c: (c["checks"]["score"], "judge" in c)), True


def _cover_lines_shape_ok(lines: Sequence[str]) -> bool:
    return 1 <= len(lines) <= 2 and all(lines)


def _cover_lines_ok(lines: Sequence[str]) -> bool:
    """The shape and the lane_rules line limits (the punchline is the last line)."""
    return _cover_lines_shape_ok(lines) and not cover_lines_overflow(lines)


def _cover_regions(boxes: Mapping[str, list[list[int]] | None] | None) -> tuple[list[list[int]], list[list[int]]]:
    """The regions to blur (danmaku, avoiding people and faces) and the other people's boxes to keep."""
    danmaku = boxes.get("danmaku") if boxes else None
    person = boxes.get("person") if boxes else None
    guests = boxes.get("guest") if boxes else None
    faces = boxes.get("face") if boxes else None
    blur = blur_boxes(danmaku, list(person or []) + list(guests or []), COVER_FACE_BOX, faces)
    keep = [b for b in (guests or []) if not any(_rect_overlap(b, h) for h in COVER_PERSON_FALLBACK)]   # the other people only
    return blur, keep


def _shortened_cover(ctx: Ctx, cover: CoverRenderResult, cover_lines: list[str],
                     render: Callable[[list[str]], CoverRenderResult], cpa_calls: list[CpaEvent],
                     process: list[str], budget: RetryBudget | None = None) -> tuple[CoverRenderResult, list[str]]:
    """While the renderer says the text does not fit, CPA shortens it, keeping the meaning (while the cover step's retry
    budget lasts: COVER_RETRIES, shared with the redo of routes vetoed on their material); every candidate is rendered
    again with the same new lines.  Returns (cover, cover lines)."""
    budget = budget or RetryBudget(COVER_RETRIES)
    while True:
        # A failed render can already have paid for earlier candidate stages; keep its receipts before retrying.
        cpa_calls.extend(cover.cpa)
        process.extend(cover.process)
        if cover.ok or cover.reason != COVER_TEXT_TOO_LONG or budget.left() <= 0:
            break
        attempts = budget.spend()
        rules, _source = title_authority.cover_text_rules()   # skill section 5, read now (the authorities go in last)
        retry = _ask_cpa(ctx, f"title-cover-retry-{attempts}",
                         _render("cover_retry.md", renderer=cover.note.strip(), lines=" / ".join(cover_lines),
                                 first_max=str(cover_line_limits()[0]), punch_max=str(cover_line_limits()[1]))
                         .replace("{cover_text_rules}", rules),
                         purpose="cover-text")
        cpa_calls.append(_log(retry))
        if not _cpa_ok(retry):
            break
        try:
            new_lines = [cover_line(x)
                         for x in (_json_from_text(retry["response_text"]).get("cover_lines") or [])]
        except (ValueError, AttributeError):
            break
        said = title_inputs(ctx, participant_names(ctx.session))[2]
        if not _cover_lines_ok(new_lines) or any(placeholder_as_person(ctx.session, line) or label_as_name(line, said)
                                                 for line in new_lines):
            break
        new_lines, canon_line = writing_canon.settle_cover(ctx, new_lines, cpa_calls, call_id=f"writing-canon-cover-{attempts}")
        process.append(canon_line)
        process.append(f"封面文字缩短（第 {attempts} 次）：{' / '.join(new_lines)}（所有候选用同一组新字重渲）")
        cover_lines = new_lines
        cover = render(cover_lines)
    return cover, cover_lines


def _short_for_many(ctx: Ctx, title: str, cover_lines: list[str], names: Sequence[str], cpa_calls: list[CpaEvent],
                    process: list[str]) -> tuple[list[str], bool] | StepOutcome:
    """(cover lines, wordy) of a cover of three or more equal people (plan T7; owner 11-02, U158: 两人地位同等，
    最好两个人同样大小, so no one big and two small panels): a punchline longer than the template table's
    `short_punch_chars` is written short first (b); when the writer says it cannot be that short without losing what it
    says, the words stay and the cover is `wordy`: the split screen with the words at the bottom is taken from its
    `wordy_punch_floor_px` (c; cover_layout_rotation.punch_floor).  Other covers are returned as they are.
    The short words go out only when the checks on the lines and the fact judge on them pass, as a rewritten cover text
    does (_accepted_cover_text); the title's receipt then carries the judge's verdict on them.  Called before the brief
    and the frame are made, which are made for the words that go out."""
    table = cover_layout_rotation.load_table(cover_layout_table())
    limit, least = int(table["short_punch_chars"]), int(table["short_punch_chars_people_min"])
    people = 1 + len(cover_guests.roster(ctx, names, None, cover_lines, title=title)["needed_on_cover"])
    if people < least or len(cover_lines[-1]) <= limit:
        return cover_lines, False
    _track, context, srt_text = title_inputs(ctx, names)
    rules, _source = title_authority.cover_text_rules()
    first_max = min(cover_line_limits()[0], len(cover_lines[0])) if len(cover_lines) > 1 else cover_line_limits()[0]
    prompt = _render("cover_short_many.md", title=title, lines=" / ".join(cover_lines), context=context, people=str(people),
                     first_max=str(first_max), punch_max=str(limit)).replace("{cover_text_rules}", rules)

    def problem(value: object) -> str:
        if isinstance(value, dict) and isinstance(value.get("keep"), str) and value["keep"].strip():
            return ""
        lines = value.get("cover_lines") if isinstance(value, dict) else None
        shaped = [cover_line(x) for x in lines] if isinstance(lines, list) else []
        if not _cover_lines_shape_ok(shaped) or len(shaped[-1]) > limit or (len(shaped) == 2 and len(shaped[0]) > first_max):
            # 11-24 298620 (replay): the first line came back 「李豆沙：红黄蓝说成红黄绿」, longer than it was, and no template took it
            return (f'either {{"keep": "why"}} or cover_lines of 1 or 2 lines, the first at most {first_max} characters '
                    f'(no longer than it was), the last at most {limit}')
        return ""
    answer, reason = ask_json(ctx, "cover-short-many", prompt, purpose="cover-text-fix", cpa_calls=cpa_calls,
                              problem=problem, shape="COVER_TEXT_SHAPE", goes_on=False)
    if reason:
        return _fail("CPA_CALL_FAILED", reason, cpa=cpa_calls, process=process)
    if answer.get("keep"):
        process.append(f"{people} 人封面：笑点行 {len(cover_lines[-1])} 字超过 {limit} 字，写字的模型说写短会丢意思"
                       f"（{str(answer['keep'])[:120]}），保留原字；字在底部的分屏按样板表的 wordy_punch_floor_px 起算")
        return cover_lines, True
    shorter, canon_line = writing_canon.settle_cover(ctx, [cover_line(x) for x in answer["cover_lines"]], cpa_calls,
                                                     call_id="writing-canon-cover-short-many")
    process.append(canon_line)
    if why := cover_text_problems(ctx, shorter, srt_text):
        process.append(f"{people} 人封面：写短的字（{' / '.join(shorter)}）不能用：{'；'.join(why)}；保留原字")
        return cover_lines, True
    judged = title_judge(ctx, "title-judge-cover-short-many", context, title, shorter, cpa_calls, writing=False)
    if isinstance(judged, StepOutcome):
        return judged
    if judged["cover"]["overall"] != "PASS":
        process.append(f"{people} 人封面：写短的字（{' / '.join(shorter)}）没过封面字的原话核对："
                       f"{'；'.join(judged['cover']['fail_reasons'])[:300]}；保留原字")
        return cover_lines, True
    receipt_path = ctx.clip_dir / title_acceptance.RECEIPT
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if isinstance(receipt, dict) and isinstance(receipt.get("judge"), dict):        # the owner's own title carries no verdict
            rec_judge = cast(TitleJudge, receipt["judge"])
            fb = merge_judge_fallbacks(rec_judge, judged)
            updated = {**rec_judge, "cover": judged["cover"], **({"fallback_models": fb} if fb else {})}
            title_acceptance.write(ctx.clip_dir, str(receipt["title"]), str(receipt["by"]), cast(TitleJudge, updated),
                                   objections=cast("Sequence[str]", receipt.get("objections") or ()),
                                   cover_lines=shorter,
                                   involved=cast("Sequence[str] | None", receipt.get("involved")),
                                   retitle_evidence=cast("str | None", receipt.get("retitle_evidence")))
    process.append(f"{people} 人封面：笑点行写短到 {limit} 字以内（原字 {' / '.join(cover_lines)} → {' / '.join(shorter)}），"
                   "过了封面字的原话核对")
    return shorter, False


def cover_text_problems(ctx: Ctx, lines: Sequence[str], subtitles: str) -> list[str]:
    """What the checks on the cover lines alone find wrong: a speaker label written as a name, an unknown participant's
    placeholder written as a person.  (A text too long is the renderer's to say: _shortened_cover.)"""
    out = []
    if leaked := next(filter(None, (label_as_name(line, subtitles) for line in lines)), ""):
        out.append(LABEL_REASON.format(leaked))
    if any(placeholder_as_person(ctx.session, line) for line in lines):
        out.append("身份未知的说话人标签不是人名，不能写进封面字")
    cues = [re.sub(r"^\[[^\]]+\]\s*", "", c["text"]) for c in parse_srt(subtitles)]
    out += title_verdicts.refused_cover_lines(str(ctx.state.data["clip"]["cid"]), lines, cues, participant_names(ctx.session))
    return out


def _cover_lines_problem(value: object) -> str:
    lines = value.get("cover_lines") if isinstance(value, dict) else None
    return "" if isinstance(lines, list) and _cover_lines_shape_ok([cover_line(x) for x in lines]) else "cover_lines must be 1 or 2 non-empty lines"


def _other_landing(chosen: TitleCandidate, round_: TitleRound) -> list[str]:
    """Why the round's cover text does not go with the chosen title as it is: it was written for the candidate the
    writer recommended, and that one is on another landing of the clip (the candidates of a round are written on
    several).  [] when they are on the same one, or the writer named no landing."""
    recommended = round_["parsed"].get("recommended")
    candidates = round_["candidates"]
    if not isinstance(recommended, int) or isinstance(recommended, bool) or not 0 <= recommended < len(candidates):
        return []
    written_for, mine = candidates[recommended].get("landing"), chosen.get("landing")
    if not isinstance(written_for, int) or not isinstance(mine, int) or written_for == mine:
        return []
    return [f"封面字是按第 {written_for} 处落点写的，采用的标题落在第 {mine} 处：按标题这一下重写，让封面字和标题说同一件事"]


def _accepted_cover_text(ctx: Ctx, names: Sequence[str], title: str, cover_lines: list[str],
                         verdict: title_authority.JudgeVerdict, cpa_calls: list[CpaEvent], process: list[str],
                         other_landing: Sequence[str] = ()
                         ) -> tuple[list[str], title_authority.JudgeVerdict, list[str]] | StepOutcome:
    """The cover lines that go out with an accepted title, and the fact judge's verdict on them.  Lines the judge
    passed and the checks have nothing against stay; else one model call rewrites the cover text alone from the
    reasons, the checks and the fact judge read the new lines (title unchanged, the judge's cover verdict), at most
    COVER_TEXT_FIXES times; then the step stops as COVER_TEXT_NOT_ACCEPTED.  A cover text written for another landing
    than the chosen title's (`other_landing`, _other_landing) is rewritten the same way.  Before a cover
    text the judge refused threw out the title judged with it (298620: the candidate on the selection's own moment
    ranked first and went with its cover line)."""
    _track, lines, srt_text = title_inputs(ctx, names)
    why = [*other_landing, *([] if _cover_lines_shape_ok(cover_lines) else [_MISSING_COVER_REASON]),
           *cover_text_problems(ctx, cover_lines, srt_text), *verdict["fail_reasons"]]
    fb_models: list[str] = []
    for attempt in range(1, COVER_TEXT_FIXES + 1):
        if not why:
            return cover_lines, verdict, fb_models
        rules, _source = title_authority.cover_text_rules()
        prompt = _render("cover_text_fix.md", title=title, lines=" / ".join(cover_lines), context=lines,
                         reasons="\n".join(f"- {x}" for x in why), first_max=str(cover_line_limits()[0]),
                         punch_max=str(cover_line_limits()[1])).replace("{cover_text_rules}", rules)
        answer, reason = ask_json(ctx, f"cover-text-fix-{attempt}", prompt, purpose="cover-text-fix", cpa_calls=cpa_calls,
                                  problem=_cover_lines_problem, shape="COVER_TEXT_SHAPE", goes_on=False)
        if reason:
            return _fail("CPA_CALL_FAILED", reason, cpa=cpa_calls, process=process)
        cover_lines, canon_line = writing_canon.settle_cover(ctx, [cover_line(x) for x in answer["cover_lines"]], cpa_calls,
                                                             call_id=f"writing-canon-cover-fix-{attempt}")
        process += [f"封面文字重写（第 {attempt} 次，标题不变）：{' / '.join(cover_lines)}；原因：{'；'.join(why)[:300]}", canon_line]
        why = cover_text_problems(ctx, cover_lines, srt_text)
        if not why:
            judged = title_judge(ctx, f"title-judge-cover{attempt}", lines, title, cover_lines, cpa_calls, writing=False)
            if isinstance(judged, StepOutcome):
                return judged
            fb_models = merge_judge_fallbacks({"fallback_models": fb_models}, judged)
            verdict, why = judged["cover"], judged["cover"]["fail_reasons"]     # title_judge always judges the lines
    if why:
        return _fail(COVER_TEXT_NOT_ACCEPTED, f"标题「{title}」过了两位判官，封面字重写 {COVER_TEXT_FIXES} 次仍不过："
                     + "；".join(why)[:500], cpa=cpa_calls, process=process)
    return cover_lines, verdict, fb_models


def _settled_cover_text(ctx: Ctx, names: Sequence[str], title: str, cover_lines: list[str], chosen: TitleCandidate,
                        chosen_round: TitleRound, cands_path: Path, cpa_calls: list[CpaEvent], process: list[str], *,
                        kept_online: bool, base_judge: TitleJudge | None = None) -> tuple[list[str], TitleJudge] | StepOutcome:
    """_accepted_cover_text for the chosen candidate, with title/candidates.json and the judge's record following the
    cover lines that go out."""
    judge = base_judge if base_judge is not None else (chosen.get("judge") or cast(TitleJudge, {}))
    if cover_import.imported_cover(ctx) is not None:
        return cover_lines, with_cover_of(judge, None)
    chosen_judge = chosen.get("judge")
    verdict = (chosen_judge.get("cover") if chosen_judge else None) or title_authority.JudgeVerdict(overall="PASS", fail_reasons=[])
    try:
        fixed = _accepted_cover_text(ctx, names, title, cover_lines, verdict, cpa_calls, process,
                                     [] if (kept_online or chosen.get("prior_accepted")) else _other_landing(chosen, chosen_round))
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls, process)
    if isinstance(fixed, StepOutcome):
        return fixed
    if fixed[0] != cover_lines:
        cands_path.write_text(json.dumps({**json.loads(cands_path.read_text(encoding="utf-8")), "cover_lines": fixed[0]},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    if not judge:
        return fixed[0], judge
    fb = merge_judge_fallbacks(judge, {"fallback_models": fixed[2]})
    return fixed[0], cast(TitleJudge, {**judge, "cover": fixed[1], **({"fallback_models": fb} if fb else {})})


def _title_goes_out(ctx: Ctx, names: Sequence[str], title: str, cover_lines: list[str], chosen: TitleCandidate,
                    chosen_round: TitleRound, cands_path: Path, cpa_calls: list[CpaEvent], process: list[str], *,
                    forced: bool, online_judge: TitleJudge | None, kept: bool,
                    objections: Sequence[str], retitle_evidence: str | None = None) -> list[str] | StepOutcome:
    """The end of step 6's title half for the title that goes out: its cover lines settled (_settled_cover_text), then
    title/title.txt and its acceptance receipt (title_acceptance) — a published title a revision keeps (`kept`;
    `online_judge` is its verdict, `objections` what the recheck and the judges have against it: `online_kept`), the
    owner's own title that no judge passed, or step 6's pick with the judges' verdict.  Returns the cover lines, or the
    failed outcome."""
    settled = _settled_cover_text(ctx, names, title, cover_lines, chosen, chosen_round, cands_path, cpa_calls, process,
                                  kept_online=kept, base_judge=online_judge)
    if isinstance(settled, StepOutcome):
        return settled
    cover_lines, judge = settled
    _local(ctx, "title/title.txt").write_text(title + "\n", encoding="utf-8")
    involved = [str(x).strip() for x in (chosen.get("involved") or []) if isinstance(x, str) and x.strip()]
    saved_cover = (list(cover_lines)
                   if (cover_import.imported_cover(ctx) is None and _cover_lines_shape_ok(cover_lines)
                       and isinstance(judge, Mapping) and isinstance(judge.get("cover"), Mapping)
                       and judge["cover"].get("overall") == "PASS")
                   else None)
    if kept:
        written_judge = judge if online_judge is not None else None
        title_acceptance.write(ctx.clip_dir, title, "online_kept" if objections else "online",
                               written_judge, objections=objections,
                               cover_lines=saved_cover if written_judge is not None else None)
    elif forced:
        title_acceptance.write(ctx.clip_dir, title, "owner" if chosen.get("owner_approved") else "step6", None,
                               involved=involved)
    else:
        title_acceptance.write(ctx.clip_dir, title, "step6", judge,
                               cover_lines=saved_cover, involved=involved, retitle_evidence=retitle_evidence)
    if not _cover_lines_shape_ok(cover_lines) and cover_import.imported_cover(ctx) is None:      # an imported cover brings its own lines
        return _fail("COVER_LINES_INVALID", f"cover_lines={cover_lines}", cpa=cpa_calls)
    process.append(f"封面文字（CPA）：{' / '.join(cover_lines)}")
    return cover_lines


_HARD_VETO_RULES = frozenset({"placeholder_identity", "published_duplicate", "owner_rejected", "fresh_title_requested"})
_MISSING_COVER_REASON = "缺少封面字：按本片标题和字幕写 1–2 行封面字"


def _body_match_keys(text: str, formal: title_names.CanonicalNames) -> set[str]:
    body = _title_body(text).strip()
    canon = normalize.canon_text(body)[0]
    fixed = formal.fix_case(canon)[0]
    return {canon.casefold(), writing_canon.canonical_form(fixed).casefold()}


def _owner_rejected(verdicts: title_verdicts.OwnerVerdicts, cid: str, title: str,
                    formal: title_names.CanonicalNames) -> str | None:
    """The rejected candidate title_body when the channel owner graded `title` bad (grade <= DISLIKED_MAX) for this
    clip or any clip in its lineage; None otherwise."""
    lineage_ids = title_verdicts.excluded_cids(cid)
    target = _body_match_keys(title, formal)
    for clip in verdicts.clips:
        if clip["cid"] not in lineage_ids:
            continue
        for cand in clip["candidates"]:
            grade = cand["grade"]
            if grade is not None and grade <= title_verdicts.DISLIKED_MAX:
                if target.intersection(_body_match_keys(cand["title_body"], formal)):
                    return cand["title_body"]
    return None


def _check_post_canon_duplicate(ctx: Ctx, cand: TitleCandidate, round_no: int, cand_idx: int,
                                published: tuple[str, ...], known_canon: Sequence[writing_canon.Conclusion],
                                cpa_calls: list[CpaEvent], process: Sequence[str]) -> StepOutcome | None:
    """If rewriting `cand['title']` under writing_canon would duplicate a published title, resolve that title's canon
    verdicts (zero CPA calls when already decided or case-only) and mark `published_duplicate` when the settled title
    still collides."""
    if "published_duplicate" in cand["checks"]["failed"]:
        return None
    raw = str(cand.get("title", "")).strip()
    if not raw or not writing_canon.find(raw):
        return None
    if not title_history.duplicate_title(writing_canon.canonical_form(raw), published):
        return None
    try:
        resolved = writing_canon.resolve(
            ctx, [writing_canon.Unit(writing_canon.TITLE, None, raw)],
            call_id=f"writing-canon-dup-r{round_no}c{cand_idx}", known=known_canon)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls, process)
    cpa_calls.extend(resolved.cpa)
    settled = resolved.texts[0]
    if title_history.duplicate_title(settled, published):
        cand["checks"]["ok"] = False
        cand["checks"]["failed"].append("published_duplicate")
        cand["checks"]["reasons"].append(f"标题经规范写法（{raw} → {settled}）后与已发标题重复，请依据本片字幕重写")
        cand["checks"]["score"] -= 1
    return None


def _fresh_title_request(ctx: Ctx) -> tuple[bool, str, int, str]:
    """(want_fresh, previous_title, attempt, request_id): read any persisted `fresh_title` request written by
    `redo --fresh-title` from clip state (memory or disk); when active, increment and persist `attempt` (and assign a
    unique `request` id if absent) so every round and every retry of the same request carries a distinct prompt and
    cannot replay an earlier round's or attempt's cached answer."""
    from .state import ClipState, StateError

    raw: object = ctx.state.data.get(title_acceptance.FRESH_TITLE_KEY)
    if not raw and ctx.state.path.is_file():
        try:
            disk_raw = json.loads(ctx.state.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk_raw = None
        if isinstance(disk_raw, dict):
            raw = disk_raw.get(title_acceptance.FRESH_TITLE_KEY)
    if not raw:
        return False, "", 0, ""
    prev = ""
    prev_attempt = 0
    req_id = ""
    if isinstance(raw, dict):
        if isinstance(raw.get("previous_title"), str):
            prev = raw["previous_title"].strip()
        if isinstance(raw.get("attempt"), int) and not isinstance(raw.get("attempt"), bool):
            prev_attempt = raw["attempt"]
        if isinstance(raw.get("request"), str):
            req_id = raw["request"].strip()
    if not prev:
        loaded = title_acceptance.load_receipt(ctx.clip_dir)
        if loaded.receipt is not None:
            prev = str(loaded.receipt["title"]).strip()
        elif (title_file := ctx.clip_dir / "title/title.txt").is_file():
            prev = title_file.read_text(encoding="utf-8").strip()
    if not req_id:
        req_id = uuid.uuid4().hex[:8]
    attempt = prev_attempt + 1
    updated: dict[str, object] = {"request": req_id, "attempt": attempt}
    if prev:
        updated["previous_title"] = prev
    with ctx.state.lock:
        cast("dict[str, object]", ctx.state.data)[title_acceptance.FRESH_TITLE_KEY] = updated
        if ctx.state.path.is_file():
            try:
                disk = ClipState.load(ctx.state.path)
            except StateError:
                disk = None
            if disk is not None:
                cast("dict[str, object]", disk.data)[title_acceptance.FRESH_TITLE_KEY] = updated
                disk.save()
    return True, prev, attempt, req_id


def _clear_fresh_title_flag(ctx: Ctx) -> None:
    """Clear the persisted `fresh_title` request once step 6 has produced a title, keeping disk state intact."""
    from .state import ClipState, StateError

    with ctx.state.lock:
        had_mem = title_acceptance.FRESH_TITLE_KEY in ctx.state.data
        if had_mem:
            cast("dict[str, object]", ctx.state.data).pop(title_acceptance.FRESH_TITLE_KEY, None)
        if ctx.state.path.is_file():
            try:
                disk = ClipState.load(ctx.state.path)
            except StateError:
                disk = None
            if disk is not None and title_acceptance.FRESH_TITLE_KEY in disk.data:
                cast("dict[str, object]", disk.data).pop(title_acceptance.FRESH_TITLE_KEY, None)
                disk.save()
                return
        if had_mem:
            ctx.state.save()


def _prior_not_kept_note(prior_cand: TitleCandidate) -> str:
    reasons = list(prior_cand["checks"]["reasons"])
    judge = prior_cand.get("judge")
    if isinstance(judge, Mapping) and judge.get("overall") == "FAIL":
        reasons.extend(str(x) for x in (judge.get("fail_reasons") or []))
    why = "；".join(reasons) if reasons else "未通过验收"
    return f"原有过判标题「{prior_cand['title']}」未沿用：{why}"


def pick_title(ctx: Ctx) -> tuple[str, list[str], list[str], list[CpaEvent], Path] | StepOutcome:
    """_pick_title while holding the batch's title lock: the clips of one batch write their titles one after another,
    so each sees the titles the others just wrote (title_history.recent_titles reads title/title.txt).  11-23: ten
    clips reached step 6 together, each saw only the batch's old titles, and ten titles opened 「游戏里」; the
    batch_opener check cannot see a title still being written."""
    import fcntl

    lock_path = ctx.state.path.parent / ".title-batch.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _pick_title(ctx)


def _pick_title(ctx: Ctx) -> tuple[str, list[str], list[str], list[CpaEvent], Path] | StepOutcome:
    """Step 6's title half: candidates from the authorities (plus any valid earlier accepted title in round 0 unless
    a fresh title was asked via `redo --fresh-title`), the checks, the judge, the choice; writes title/candidates.json
    (with the authorities' hashes), title/title.txt and title/accepted.json.  Returns (title, cover lines, process
    lines, CPA calls, candidates path), or the failed outcome."""
    names = participant_names(ctx.session)
    cpa_calls: list[CpaEvent] = []
    want_fresh, avoid_title, fresh_attempt, fresh_request = _fresh_title_request(ctx)
    prior_notes: list[str] = []
    result = _title_rounds(ctx, names, cpa_calls, avoid_title=avoid_title,
                           fresh_attempt=fresh_attempt, fresh_request=fresh_request, prior_notes=prior_notes)
    if isinstance(result, StepOutcome):
        return result
    rounds, method, history, understanding, verdicts, circle = result
    cid = str(ctx.state.data["clip"].get("cid") or "")
    approved = verdicts.approved(cid)
    authorities = [*method.sources_json(), verdicts.source.as_dict()]
    chosen, forced = _choose_title(rounds)
    if chosen is None:
        return _fail("TITLE_NO_CANDIDATE", "CPA returned no title candidates", cpa=cpa_calls, process=prior_notes)
    chosen_round = rounds[chosen["round"]]
    cover_lines = (list(chosen.get("prior_cover") or []) if chosen.get("prior_accepted")
                   else [cover_line(x) for x in (chosen_round["parsed"].get("cover_lines") or [])])
    try:
        title, cover_lines, canon_line = writing_canon.settle_title_cover(ctx, str(chosen["title"]).strip(), cover_lines, cpa_calls)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls, prior_notes)
    from .revise_run import title_online      # that module reaches the steps through trim_run: imported when used

    retitle = verdicts.retitle(cid)
    revises = isinstance(ctx.state.data.get("revised_from"), dict)
    edited = (title_history.edited_title(ctx, publication_cid(str(ctx.state.data["clip"].get("cid") or ctx.clip_dir.name)))
              if revises and not (approved or retitle) else None)
    online = None if approved or retitle else title_online(ctx.clip_dir, ctx.state, edited)   # a revision keeps the title online
    if not online and not chosen.get("owner_approved") and title != str(chosen["title"]).strip():
        track, _lines, _srt = title_inputs(ctx, names)
        formal = title_names.canonical_names(title_authority.subtitle_text(track), names)
        post_reasons: list[str] = []
        if chosen.get("prior_accepted") and title_history.duplicate_title(title, history.published_titles):
            post_reasons.append(f"标题经规范写法（{chosen['title']} → {title}）后与已发标题重复")
        if chosen.get("prior_accepted") and _owner_rejected(verdicts, cid, title, formal):
            post_reasons.append(f"标题经规范写法（{chosen['title']} → {title}）后命中频道主否决标题")
        if (not approved and want_fresh and avoid_title
                and _body_match_keys(title, formal).intersection(_body_match_keys(avoid_title, formal))):
            post_reasons.append(f"标题经规范写法（{chosen['title']} → {title}）后仍与要求替换的旧标题重复")
        if chosen.get("prior_accepted") and unnamed_guests(ctx, track) and placeholder_as_person(ctx.session, title):
            post_reasons.append("标题经规范写法后仍包含身份未知占位标签")
        if post_reasons:
            return _fail(title_acceptance.NOT_ACCEPTED, "；".join(post_reasons),
                         cpa=cpa_calls, process=[*prior_notes, canon_line, *post_reasons])
    stale: list[str] = []
    online_judge: TitleJudge | None = None
    if online:
        rechecked = online_title_breaks(ctx, names, online, cpa_calls)
        if isinstance(rechecked, StepOutcome):
            return rechecked
        stale, online_judge = rechecked
    # what the judges have against the published title is recorded with it (title_acceptance "online_kept"), not acted
    # on: a published manuscript changes its title only by the owner's word (approved / retitle above)
    pick = title
    title = online or title
    cands_path = _local(ctx, "title/candidates.json")
    cands_path.write_text(json.dumps({
        "candidates": [dict(c) for rnd in rounds for c in rnd["candidates"]],
        "rounds": [rnd["call_id"] for rnd in rounds], "chosen": chosen, "forced": forced,
        "recommended": [rnd["parsed"].get("recommended") for rnd in rounds],
        "pick_calls": [rnd["pick_call"] for rnd in rounds],
        "coverage": [rnd["coverage"] for rnd in rounds],
        "authorities": authorities, "corpus_titles": method.corpus_count,
        "understanding": understanding, "context": circle_context.summary(circle), "owner_approved_title": approved,
        "title_history": {"published": history.published_titles, "published_names": history.published,
                          "batch": history.batch, "notes": history.notes},
        "cover_lines": cover_lines}, ensure_ascii=False, indent=2), encoding="utf-8")
    judge = chosen.get("judge") or cast(TitleJudge, {})
    stops = forced and not online and not chosen.get("owner_approved")     # skill 八.4: no title is chosen by score
    prior_cand = next((c for c in rounds[0]["candidates"] if c.get("prior_accepted")), None) if rounds else None
    prior_outcome_notes: list[str] = []
    if prior_cand is not None and not online:
        if not stops and chosen.get("prior_accepted"):
            prior_outcome_notes.append(f"沿用原有过判标题：{title}")
            orig_prior = prior_cand.get("prior_title")
            if isinstance(orig_prior, str) and orig_prior and title != orig_prior:
                prior_outcome_notes.append(f"沿用标题经规范化/正名修正：{orig_prior} → {title}")
        else:
            prior_outcome_notes.append(_prior_not_kept_note(prior_cand))
    process = [*prior_notes, *prior_outcome_notes,
               f"标题候选：{len(rounds)} 轮，共 {sum(len(r['candidates']) for r in rounds)} 个；"
               + ("没有采用（没有一条过两位判官）；自检得分最高的一条：" if stops else "采用：")
               + f"{title}（自检 {chosen['checks']['score']}/{len(TITLE_RULES)}，判官 {judge.get('overall', '未判')}）"
               + ("：" + "；".join([*chosen["checks"]["reasons"], *(judge.get("fail_reasons") or ["未送判官"])])[:600]
                  if stops else ""),
               "标题提示词读取的权威：" + "；".join(f"{s['name']} {s['sha256'][:12]}" for s in authorities)]
    process.append(canon_line)
    if online:
        process.append(f"修订沿用线上标题：{online}（已发布稿件的修订不改题；改题走改题回执，本轮候选只用来定封面字）")
    if stale:
        process.append("线上标题按本轮字幕重查或重判不过，照样沿用（频道主没给新题、也没要求换题），异议记进 "
                       f"{title_acceptance.RECEIPT}：" + "；".join(stale) + f"；要换题由频道主裁定（他定了新题再走 {TITLE_RECEIPT_HINT}），本轮选出的是：{pick}")
    elif retitle and not approved:
        process.append(f"频道主要求换标题（原话：{retitle['evidence']}），修订不沿用线上标题，用本轮选出的标题；"
                       f"标题与封面字按他说的看点写（笑点：{retitle['punchline']}），不按模型自己读出的主线")
    process.append(title_understanding.process_line(understanding))
    process.extend(circle_context.process_lines(circle))
    process.extend(title_pick.process_line(rnd["pick_call"], n) for n, rnd in enumerate(rounds) if rnd["pick_call"])
    process.extend(coverage_process_line(rnd["coverage"], [c for c in rnd["candidates"] if "judge" in c], n)
                   for n, rnd in enumerate(rounds))
    process.extend(history.notes)
    if approved:
        process.append(f"频道主选定标题正文：{approved['title_body']}（原话：{approved['evidence']}）")
    if forced and chosen.get("owner_approved"):
        process.append("频道主选定的标题未过检查：" + "；".join([*chosen["checks"]["reasons"],
                                                       *(judge.get("fail_reasons") or ["未送判官"])])
                       + "（仍采用它，行政亲审前请处理）")
    elif forced and online:
        process.append("本轮候选没有一条过两位判官，沿用线上标题" + ("（重判不过，异议见上）" if stale else "（已重判通过）")
                       + "；本轮候选只用来定封面字")
    if stops:
        return _fail(title_acceptance.NOT_ACCEPTED, f"{len(rounds)} 轮候选没有一条过两位判官（事实、写法），不按得分硬选："
                     + "；".join([*chosen["checks"]["reasons"], *(judge.get("fail_reasons") or ["未送判官"])])[:600],
                     cpa=cpa_calls, process=process)
    going_out = _title_goes_out(ctx, names, title, cover_lines, chosen, chosen_round, cands_path, cpa_calls, process,
                                forced=forced, online_judge=online_judge, kept=bool(online), objections=stale,
                                retitle_evidence=retitle["evidence"] if (retitle and not approved) else None)
    if isinstance(going_out, StepOutcome):
        return going_out
    if want_fresh:
        _clear_fresh_title_flag(ctx)
    return title, going_out, process, cpa_calls, cands_path


def _name_guests(ctx: Ctx) -> tuple[list[str], list[CpaEvent]]:
    """collab_guests on this clip: (the guests it added to the session, its CPA call)."""
    calls: list[CpaEvent] = []
    try:
        return collab_guests.name_guests(ctx, json_asker(ctx, calls)), calls
    except collab_guests.GuestsUnknown as exc:
        exc.calls = calls      # type: ignore[attr-defined]  # the failed call's record goes with the failed step
        raise


# what step 6 asks to be made again when the session gained a guest: the speakers, the burn that shows them, and this
# step itself (the asking step is in its own list, as the review's are: its record would else be reused as done)
REWIND_SPEAKER = ("speaker", "burn", "title_cover")


def _voice_decided(ctx: Ctx) -> bool:
    """Step 4 told this clip's speakers apart by the voice, for the number of people the session had then (its linked
    route, or the route of a session that named no guest: step4_speaker.GUESTS_HEARD_MODE).  Not so when the speakers
    were imported, or the others are a watched video's voices read from the picture: the people count is not in those."""
    from .step4_speaker import GUESTS_HEARD_MODE

    path = ctx.clip_dir / "speaker/decision.json"
    mode = str(json.loads(path.read_text(encoding="utf-8")).get("mode") or "") if path.exists() else ""
    return mode == GUESTS_HEARD_MODE or mode.startswith("linked_v8")


def _identify_guests(ctx: Ctx) -> tuple[dict[int, str], list[CpaEvent]]:
    """guest_identity on this clip: ({line: guest}, its CPA call)."""
    from . import guest_identity, subtitle_track

    calls: list[CpaEvent] = []
    return guest_identity.identify(ctx, subtitle_track.read_track(ctx.clip_dir), json_asker(ctx, calls)), calls


def _identity_process(ctx: Ctx, identified: Mapping[int, str], *, asked: bool) -> str:
    """The PROCESS line of guest_identity: how many guest lines were named, and by which evidence."""
    from . import guest_identity

    saved = json.loads((ctx.clip_dir / guest_identity.PATH).read_text(encoding="utf-8")) \
        if (ctx.clip_dir / guest_identity.PATH).exists() else {}
    owner = len(saved.get("owner") or {})
    voice_only = len(set(saved.get("voice") or {}) - set(saved.get("owner") or {}))
    how = [f"频道主标注 {owner} 句"] if owner else []
    how += [f"声纹 {voice_only} 句"] if voice_only else []
    if saved.get("failed"):
        how.append("识别器分组＋对话证据：调用失败，没用上")
    elif saved.get("answer") is not None:
        how.append("识别器分组＋对话证据" + ("" if asked else "（沿用上次的回答）"))
    return f"嘉宾是谁：{len(identified)} 句连线定到具体的人（{'；'.join([*how, 'guest_identity.json'])}）"


def step6_title_cover(ctx: Ctx) -> StepOutcome:
    final_srt = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    cues = parse_srt(final_srt)
    try:
        guests, guest_calls = _name_guests(ctx)
    except collab_guests.GuestsUnknown as exc:
        return _fail("CPA_CALL_FAILED", str(exc), cpa=getattr(exc, "calls", []))
    if guests and _voice_decided(ctx):
        # step 4 told the speakers apart for fewer people than the session now names: its host threshold, its arrival
        # groups and the lines it gave the guests are for that count, so it is made again before anything is titled
        return StepOutcome(True, rewind=REWIND_SPEAKER, cpa=guest_calls, note=f"本场连线补进 {'、'.join(guests)}，重跑说话人",
                           process=[collab_guests.process_line(ctx, guests),
                                    "第 4 步是按补人之前的人数分的说话人，回到第 4 步按现在的名单重分，再做标题与封面"])
    identified, identity_calls = _identify_guests(ctx)
    guest_calls += identity_calls
    names = participant_names(ctx.session, visual=True)
    picked = pick_title(ctx)
    if isinstance(picked, StepOutcome):
        return picked
    title, cover_lines, process, cpa_calls, cands_path = picked
    cpa_calls[:0] = guest_calls
    if guests:
        process.insert(0, collab_guests.process_line(ctx, guests))
    if identified or identity_calls:
        process.insert(0, _identity_process(ctx, identified, asked=bool(identity_calls)))
    if (imported := cover_import.imported_cover(ctx)) is not None:      # the decided cover: nothing is drawn or looked at
        return cover_import.adopt(ctx, imported, title=title, process=process, cpa=cpa_calls)
    if (kept := cover_import.published_cover(ctx, title)) is not None:     # a subtitle revision keeps the published cover
        return cover_import.adopt(ctx, kept, title=title, process=process, cpa=cpa_calls)
    title_path = ctx.clip_dir / "title/title.txt"
    clip = _clip(ctx)
    cut_len = (clip["end_ms"] - clip["start_ms"]) / 1000
    try:
        short = _short_for_many(ctx, title, cover_lines, names, cpa_calls, process)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls, process)
    if isinstance(short, StepOutcome):
        return short
    cover_lines, wordy = short
    brief = _cover_brief(ctx, cues, title, cover_lines, names)
    cpa_calls.append(brief.pop("_cpa"))
    if brief.get("_failed"):
        return _fail(brief["_failed"], brief.get("_detail", ""), cpa=cpa_calls, process=process)
    # the clips of one run are made in threads at once (multi_run): the cover's history (the covers already chosen,
    # step6_cover._lane_covers) is read, the cover made and its record written while holding the batch's cover lock,
    # so the next clip sees this one's composition and route (review: four clips chosen within a minute of
    # each other read the same history and would all take the same template); the frame is picked under it too, so the
    # next clip's frame step sees this one's base frame (70-cover 同场不重复底帧; 11-25: three of four covers of one
    # stream were the same smile at the microphone)
    with cover_batch_lock(ctx):
        frame_started = time.monotonic()
        try:
            frame_t, frame_note, boxes = _pick_cover_frame(ctx, cues, cover_lines, cut_len)
        except CpaVisionUnavailable as exc:
            cpa_calls.extend(cover_vision_calls(ctx, 1)[0])
            failed = _fail("CPA_CALL_FAILED", str(exc), cpa=cpa_calls, process=process)
            failed.products["title"] = str(title_path)
            return failed
        frame_s = time.monotonic() - frame_started
        process.append(f"封面帧：{frame_note}（{frame_s:.1f}s）")
        vision, vision_line = cover_vision_calls(ctx, 1)
        cpa_calls.extend(vision)
        process.append(vision_line)
        blur, keep = _cover_regions(boxes)
        process.append(f"模糊区域（已避开人物与人脸）：{blur}")
        return _cover_held(ctx, cues, cover_lines, brief, names, title, title_path, cands_path,
                           frame_t, frame_s, blur, keep, boxes, cpa_calls, process, wordy=wordy)


@contextlib.contextmanager
def cover_batch_lock(ctx: Ctx) -> Iterator[None]:
    """Serialize the standard on-host task family; standalone task locations keep their batch lock."""
    import fcntl

    scope = cover_scope.for_context(ctx) or ctx.state.path.parent
    with open(scope / ".cover-batch.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _cover_held(ctx: Ctx, cues: Sequence[SrtCue], cover_lines: list[str], brief: CoverBrief, names: Sequence[str], title: str,
                title_path: Path, cands_path: Path, frame_t: float, frame_s: float, blur: list[list[int]], keep: list[list[int]],
                boxes: Mapping[str, list[list[int]] | None] | None, cpa_calls: list[CpaEvent], process: list[str], *,
                wordy: bool = False) -> StepOutcome:
    """step6_title_cover from the cover plan on, under cover_batch_lock.  `wordy`: _short_for_many's."""
    plan = cover_plan(ctx, cover_lines, brief, names)
    plan["action_frame"] = chosen_action(ctx)
    plan["wordy"] = wordy
    process.append(plan["line"])

    budget = RetryBudget(COVER_RETRIES)      # one budget for the shortenings below and the redo of routes inside the render

    def render(lines: list[str]) -> CoverRenderResult:
        return _render_cover(ctx, cues, lines, frame_t, blur=blur, keep=keep, plan=plan, boxes=boxes,
                             title=title, names=names, frame_s=frame_s, budget=budget)

    first_try = (CoverRenderResult(False, reason=COVER_TEXT_TOO_LONG, note=f"封面字过长：{too_long}")     # the existing shortening path
                 if (too_long := cover_lines_overflow(cover_lines)) else render(cover_lines))
    try:
        cover, cover_lines = _shortened_cover(ctx, first_try, cover_lines, render, cpa_calls, process, budget)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls, process)
    if not cover.ok:
        return StepOutcome(False, reason=cover.reason, cpa=cpa_calls, note=cover.note, process=process,
                           products={"title": str(title_path)})
    cands = json.loads(cands_path.read_text(encoding="utf-8"))
    cands["cover_lines"] = cover_lines
    # 70-cover: method=skill.candidates says what was actually done; a route that generated no image says so and
    # carries no generation receipt
    route = cover.products["route"]
    cands["cover_route"] = dict(route, frame_s=route.get("frame_s", frame_t), brief=brief)
    cover_scope.note(ctx, cands["cover_route"])
    cands_path.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    return StepOutcome(True, products={"title": str(title_path), "candidates": str(cands_path),
                                       "cover": cover.products["cover"], "choose": cover.products["choose"]},
                       cpa=cpa_calls, note=f"采用标题 {title}；封面 {cands['cover_route']['chosen']['route']}"
                       + ("；封面素材被否决或漏了嘉宾（上线前停下，见 PROCESS 与 choose.json material_flag）" if route.get("material_flag") else ""),
                       process=process)
