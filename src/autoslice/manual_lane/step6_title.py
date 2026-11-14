"""Step 6 title: title checks and the publish gate, then the step body that picks the title and renders
the cover.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from src.autoslice.channel_envelope import EnvelopeError, HOST_NAME, parse_talk_title, render_talk_title

from . import (
    circle_context,
    cover_import,
    gates,
    graph_context,
    normalize,
    parallel,
    title_authority,
    title_history,
    title_names,
    title_pick,
    title_understanding,
    title_verdicts,
    writing_canon,
)
from .cover_redo import RetryBudget
from .runner import Ctx, StepOutcome
from .config import participant_names, placeholder_as_person, unknown_participant
from .speaker_v8 import GUEST_LABEL, HOST
from .srt_text import _json_from_text, parse_srt
from .step6_cover import (
    COVER_FACE_BOX,
    COVER_PERSON_FALLBACK,
    COVER_RETRIES,
    COVER_TEXT_TOO_LONG,
    cover_line_limits,
    cover_lines_overflow,
    CoverRenderResult,
    _cover_brief,
    _compilation_context,
    _pick_cover_frame,
    _rect_overlap,
    _render_cover,
    blur_boxes,
    cover_plan,
    cover_vision_calls,
)
from .step_io import _ask_cpa, _clip, _cpa_ok, _fail, _local, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent
    from .subtitle_track import TrackCue

# ----- step 6: title and cover (CPA picks the title; the cover is rendered from CPA's lines) ------


TITLE_MAX_REGEN = 2                              # regeneration rounds after the first candidate round
TITLE_RULES = ("banned", "prefix", "on_stage_known", "named", "prefix_names", "swap", "punchline", "quotes",
               "canonical_names", "publish_gate", "published_duplicate")


class TitleChecks(TypedDict):
    ok: bool
    reasons: list[str]
    failed: list[str]
    score: int


class JudgeRun(title_authority.JudgeVerdict):
    call_id: str


class TitleJudge(TypedDict):
    overall: str
    fail_reasons: list[str]
    runs: list[JudgeRun]
    authorities: list[title_authority.SourceRecord]


class SwapCheck(TypedDict, total=False):
    holds_with_other_host: bool | None


class TitleCandidate(TypedDict, total=False):
    title: str
    title_body: str
    on_stage: list[str]
    swap_check: SwapCheck | None
    punchline_person: str
    punchline: str
    checks: TitleChecks
    round: int
    accepted: bool
    judge: TitleJudge
    name_fixes: list[title_names.NameFix]      # case-only spellings of a formal name, corrected
    prefix_repeat: bool                        # a partner named in the prefix is named again in the body
    pick: title_pick.PickItem                  # the owner-taste ranking of this candidate (title_pick)
    owner_approved: bool                       # its body is the one the owner picked for this clip (title_verdicts)


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


def title_checks(cand: TitleCandidate, names: Sequence[str], subtitles: str, *,
                 envelope_error: str = "", published: tuple[str, ...] = (),
                 non_formal: Sequence[title_names.NonFormal] = ()) -> TitleChecks:
    """The deterministic checks of skill section 4 on one candidate.  Each rule it breaks is named once in `reasons`;
    `score` counts the rules the candidate passes (len(TITLE_RULES) is perfect).  `subtitles` is the final SRT with
    the speaker labels where there are any: quoted words are checked against it by the repo's checker
    (title_cover_quotes; skill section 8 items 1-2: verbatim, and the speaker when the title names who said it).
    `non_formal` are the non-formal spellings of a name found in the body (title_names)."""
    from src.autoslice.title_quote_check import check_title_quotes

    title = str(cand.get("title", "")).strip()
    failed: dict[str, str] = {}
    if title_history.duplicate_title(title, published):
        failed["published_duplicate"] = "标题与已发标题重复，请依据本片字幕重写"
    banned = gates.title_banned(title)
    if banned:
        failed["banned"] = "禁词：" + ",".join(banned)
    envelope = parse_talk_title(title)
    if envelope is None:
        failed["prefix"] = "前缀不是【李豆沙】或【李豆沙×出场者】"
    on_stage = [str(x).strip() for x in (cand.get("on_stage") or []) if str(x).strip()]
    unknown = [n for n in on_stage if n not in names]
    if not on_stage:
        failed["on_stage_known"] = "on_stage 为空，未列出出场参与者"
    elif unknown:
        failed["on_stage_known"] = "出场者不在参与者名单：" + "、".join(unknown)
    not_named = [n for n in on_stage if n not in title]
    if not_named:
        failed["named"] = "标题未点名出场者：" + "、".join(not_named)
    partners = tuple(dict.fromkeys(n for n in on_stage if n != HOST_NAME))
    if (envelope_error or (envelope is None and partners)
            or (envelope is not None and set(envelope.partners) != set(partners))):
        failed["prefix_names"] = "前缀未写出场者或与出场名单不一致（应为【李豆沙×…】）"
        if envelope_error:
            failed["prefix_names"] += "：" + envelope_error
    swap = cand.get("swap_check") or {}
    if swap.get("holds_with_other_host") is not False:
        failed["swap"] = "换主播自检未通过（或缺失）：换成别的主播仍成立，要重写"
    if not str(cand.get("punchline_person") or "").strip():
        failed["punchline"] = "笑点未落在李豆沙本人身上（punchline_person 为空）"
    quotes = check_title_quotes(title, subtitles)
    if quotes["status"] != "PASS":
        failed["quotes"] = "引号核对未过：" + ",".join(quotes["reason_codes"])
    if non_formal:
        failed["canonical_names"] = "名字写法不是正式写法：" + "；".join(
            f"{n['written']} 应写成 {n['formal']}" for n in non_formal)
    publish = publish_gate_violations(str(cand.get("title", "")))   # the same gate as step 8, raw codes into reasons
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


def _feedback_text(candidates: Sequence[TitleCandidate]) -> str:
    lines = ["上一轮候选没有一条通过自检和验收，请针对下面的原因重写（编辑方法不变）："]
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
                cpa_calls: list[CpaEvent]) -> TitleJudge | StepOutcome:
    """The skill's acceptance judge (section 8 item 3 through references/judge-prompt.md), run JUDGE_RUNS times as
    judge-prompt.md says; PASS only when every run says PASS.  Returns the verdict (each run, the authority hashes),
    or the failed outcome when CPA cannot be reached.

    TASK-31: the runs are independent answers to the same prompt (each has its own cache entry, `run`), so they are
    asked at once (parallel.run, bound lane_rules concurrency.cpa); runs and records are reported in run order."""
    prompt, sources = title_authority.judge_prompt(track_lines, title, cover_lines)
    numbers = list(range(1, title_authority.JUDGE_RUNS + 1))
    records = parallel.run(numbers,
                           lambda run: _ask_cpa(ctx, f"{call_id}-{run}", prompt, run=run,
                                                purpose="title-judge"),
                           provider="cpa")
    runs: list[JudgeRun] = []
    for run, record in zip(numbers, records, strict=True):
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=cpa_calls)
        try:
            parsed = _json_from_text(record["response_text"])
        except ValueError:
            parsed = None
        runs.append(cast(JudgeRun, dict(title_authority.judge_verdict(parsed), call_id=f"{call_id}-{run}")))
    failed = [r for r in runs if r["overall"] != "PASS"]
    return {"overall": "FAIL" if failed else "PASS",
            "fail_reasons": list(dict.fromkeys(x for r in failed for x in r["fail_reasons"])),
            "runs": runs, "authorities": [s.as_dict() for s in sources]}


def title_inputs(ctx: Ctx, names: Sequence[str]) -> tuple[list[TrackCue], str, str]:
    """The final cues with their speakers (speaker.srt when there is one; skill section 3 step 1), as the prompt's
    numbered lines and as SRT for the quote checker.  speaker.srt's guest label stands for the one guest."""
    from . import subtitle_track   # imports stage_impl, which re-exports this module: imported here, not at the top

    track = subtitle_track.read_track(ctx.clip_dir)
    rename = title_authority.speaker_names(names, host=HOST, guest_label=GUEST_LABEL)
    if len(participant_names(ctx.session, visual=True)) > len(participant_names(ctx.session)):
        rename[GUEST_LABEL] = "未具名嘉宾"
    return track, title_authority.numbered_lines(track, rename), title_authority.speaker_srt(track, rename)


def _speaker_note(track: Sequence[TrackCue], names: Sequence[str], *, anonymous: bool = False) -> str:
    if not any(c.get("speaker") for c in track):
        return "字幕没有说话人标签：判断不了是谁说的，就不写说话人。"
    if anonymous:
        return "每句前的 [说话人] 来自 speaker.srt（已核对）；[未具名嘉宾] 是身份未知的说话角色。"
    guest = title_authority.speaker_names(names, host=HOST, guest_label=GUEST_LABEL).get(GUEST_LABEL)
    return ("每句前的 [说话人] 来自 speaker.srt（已核对），引语归属以它为准"
            + (f"；本场连线只有一位，标作 [{guest}]。" if guest else f"；[{GUEST_LABEL}] 是李豆沙以外的连线方。"))


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


def _title_rounds(ctx: Ctx, names: Sequence[str], cpa_calls: list[CpaEvent]
                  ) -> tuple[list[TitleRound], title_authority.TitleMethod, title_history.TitleHistory,
                             title_understanding.Understanding, title_verdicts.OwnerVerdicts,
                             circle_context.CircleContext] | StepOutcome:
    """Round 0 is the first candidate round; then up to TITLE_MAX_REGEN regenerations with the failed checks and the
    judge's reasons as feedback, until a candidate passes both.  In each round the first candidate (in the owner-taste
    order of title_pick) that passes the deterministic checks goes to the judge, unless that order puts a predicted 差
    first; a judged PASS ends the rounds.  Returns the rounds and the method the prompt carried (with the owner
    verdicts' hash apart), the reading of the clip the prompt was built on (title_understanding), the owner's
    verdicts the prompt carried and the circle background looked up for it (circle_context), or the failed outcome.
    When he did, the candidate whose body is that body (after canon_text and the formal-name case fix) is the only one
    tried, with no ranking call; a round without it is rewritten, and rounds used up without it fail as
    TITLE_APPROVED_NOT_REPRODUCED."""
    try:
        cid = str(ctx.state.data["clip"].get("cid") or "")
        method = title_authority.title_method(exclude_candidate=cid)
        verdicts = title_verdicts.load_verdicts()
    except title_authority.AuthorityError as exc:
        return _fail("TITLE_AUTHORITY_MISSING", str(exc), cpa=cpa_calls)
    owner_block = verdicts.title_block(cid)
    approved = verdicts.approved(cid)
    track, lines, srt_text = title_inputs(ctx, names)
    understanding = title_understanding.understand(ctx, lines, cpa_calls)
    if isinstance(understanding, StepOutcome):
        return understanding
    formal = title_names.canonical_names(title_authority.subtitle_text(track), names)
    graph = graph_context.read(ctx, [title_authority.subtitle_text(track)])
    circle = circle_context.gather(ctx, understanding["merged"], lines, cpa_calls, graph)
    circle_block = "\n\n".join(filter(None, [graph.block(), circle_context.block(circle)]))   # graph first, then the web
    mainline = "\n\n".join(filter(None, [title_understanding.mainline_block(understanding, formal.prompt_text()),
                                         circle_block]))
    approved_body = formal.fix_case(normalize.canon_text(approved["title_body"])[0])[0] if approved else ""
    if approved:      # his body is written canonically too; the candidates are compared with it in canonical form
        try:
            approved_body = writing_canon.settle_approved(ctx, approved_body, cpa_calls)
        except writing_canon.WritingCanonFailed as exc:
            return writing_canon.failed(exc, cpa_calls)
    basis = title_pick.PickBasis(subtitles=title_authority.subtitle_text(track),
                                 main_line=understanding["merged"]["main_line"],
                                 punchline=understanding["merged"]["punchline"], corpus=verdicts.corpus(cid),
                                 circle=circle_block)
    history = title_history.recent_titles(ctx.state.path, ctx.clip_dir, str(ctx.state.data["clip"].get("cid") or ""), ctx=ctx)
    rounds: list[TitleRound] = []
    feedback = ""
    anonymous = len(participant_names(ctx.session, visual=True)) > len(participant_names(ctx.session))
    speaker_note = _speaker_note(track, names, anonymous=anonymous)
    if anonymous:
        labels = [str(g[0]).strip() for g in ctx.session.get("participants") or [] if g and unknown_participant(str(g[0]))]
        speaker_note += ("「连线」是身份未知的说话人标签，不是账号名；前缀和封面署名不要写它，on_stage 只写已知正式名。"
                         if all(n == "连线" for n in labels) else
                         "身份未知的说话人标签不是账号名；前缀和封面署名不要写标签，on_stage 只写已知正式名。")
    for round_no in range(TITLE_MAX_REGEN + 1):
        call_id = "title" if round_no == 0 else f"title-regen-{round_no}"
        prompt = _render("title.md", context=lines, participants="、".join(names), feedback=feedback,
                         speaker_note=speaker_note, compilation_context=_compilation_context(ctx),
                         first_max=str(cover_line_limits()[0]), punch_max=str(cover_line_limits()[1]))
        record = _ask_cpa(ctx, call_id, prompt.replace("{method}", "\n\n".join([method.text, history.prompt(), mainline, owner_block])),
                          purpose="title")
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=cpa_calls)
        try:
            parsed = _json_from_text(record["response_text"])
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):   # not the object the title prompt asks for (a bare list happened, TASK-11)
            return _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=[*cpa_calls[:-1], record])
        parsed_answer = cast(TitleAnswer, parsed)
        try:
            candidates = _raw_title_candidates(parsed_answer)
        except ValueError as exc:
            return _fail("CPA_RESPONSE_UNPARSEABLE", f"{record['file']}: {exc}",
                         cpa=[*cpa_calls[:-1], record])
        for cand in candidates:
            value = cand["title_body"] if "title_body" in cand else cand["title"]
            raw_stage = cand.get("on_stage")
            title, name_fixes = formal.fix_case(normalize.canon_text(value)[0])
            envelope_error = ""
            if "title_body" in cand:
                on_stage = list(dict.fromkeys(name.strip() for name in (raw_stage or []) if name.strip()))
                if on_stage and all(name in names for name in on_stage):
                    try:
                        title = render_talk_title(title, [name for name in on_stage if name != HOST_NAME])
                    except EnvelopeError as exc:
                        envelope_error = str(exc)
            cand["title"] = title
            envelope = parse_talk_title(title)
            body = envelope.body if envelope is not None else title
            cand["name_fixes"] = name_fixes
            cand["prefix_repeat"] = envelope is not None and title_names.prefix_repeat(envelope.partners, body)
            cand["checks"] = title_checks(cand, names, srt_text, envelope_error=envelope_error,
                                          published=history.published_titles, non_formal=formal.non_formal(body))
            cand["round"] = round_no
            cand["accepted"] = False
        cover = [normalize.canon_text(str(x).strip())[0] for x in (parsed_answer.get("cover_lines") or [])]
        if (overflow := cover_lines_overflow(cover)):      # cover text the renderer cannot hold at the punchline floor: rewrite
            for cand in candidates:
                cand["checks"]["ok"] = False
                cand["checks"]["failed"].append("cover_lines_too_long")
                cand["checks"]["reasons"].append(f"封面字过长：{overflow}")
                cand["checks"]["score"] -= 1
        if anonymous:
            for cand in candidates:
                envelope = parse_talk_title(cand["title"])
                fake = (any(unknown_participant(n) for n in (cand.get("on_stage") or [])) or bool(
                    envelope and any(unknown_participant(n) for n in envelope.partners)) or
                    placeholder_as_person(ctx.session, cand["title"]))
                if fake or any(placeholder_as_person(ctx.session, line) for line in cover):
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
                bodies=[_title_body(c["title"]) for c in candidates], passing=[c["checks"]["ok"] for c in candidates],
                model_order=_round_order(parsed_answer, candidates), cpa_calls=cpa_calls)
        for index, item in pick.items.items():
            candidates[index]["pick"] = item
        rounds.append({"call_id": call_id, "parsed": parsed_answer, "candidates": candidates, "order": pick.order,
                       "pick_call": pick.call})
        first = next((candidates[i] for i in pick.order if candidates[i]["checks"]["ok"]
                      and (not approved or candidates[i].get("owner_approved"))), None)
        if first is not None and not pick.top_is_bad:       # a first predicted 差 is not sent to the judge: rewrite
            judged = title_judge(ctx, f"title-judge-r{round_no}", lines, first["title"], cover, cpa_calls)
            if isinstance(judged, StepOutcome):
                return judged
            first["judge"] = judged
            first["accepted"] = judged["overall"] == "PASS"
            if first["accepted"]:
                break
        feedback = _feedback_text(candidates)
        if approved:
            feedback += "\n第一条候选的 title_body 必须逐字是：" + approved["title_body"]
    if approved and not any(c.get("owner_approved") for rnd in rounds for c in rnd["candidates"]):
        return _fail("TITLE_APPROVED_NOT_REPRODUCED",
                     f"频道主选定的标题正文 {approved['title_body']} 在 {len(rounds)} 轮里没有一条候选逐字写出", cpa=cpa_calls)
    return rounds, method, history, understanding, verdicts, circle


def _choose_title(rounds: Sequence[TitleRound]) -> tuple[TitleCandidate | None, bool]:
    """Each round's candidates in its order (the owner-taste ranking, else the model's recommendation first); the first
    accepted one (the deterministic checks and the judge's PASS), else the best (forced): the checks' score, a judged
    candidate before an unjudged one, the earlier in that order before the later.  Returns (chosen or None when there is
    no candidate, forced)."""
    ordered: list[TitleCandidate] = []
    for rnd in rounds:
        ordered += [rnd["candidates"][i] for i in rnd["order"]]
    if any(c.get("owner_approved") for c in ordered):      # the owner's pick is never replaced by another title
        ordered = [c for c in ordered if c.get("owner_approved")]
    ordered = [c for c in ordered if not {"placeholder_identity", "published_duplicate"}.intersection(c["checks"]["failed"])]
    if not ordered:
        return None, True
    accepted = [c for c in ordered if c.get("accepted")]
    if accepted:
        return accepted[0], False
    return max(ordered, key=lambda c: (c["checks"]["score"], "judge" in c)), True


def _cover_lines_shape_ok(lines: Sequence[str]) -> bool:
    return 1 <= len(lines) <= 2 and all(lines)


def _cover_lines_ok(lines: Sequence[str]) -> bool:
    """The shape and the lane_rules line limits (the punchline is the last line)."""
    return _cover_lines_shape_ok(lines) and not cover_lines_overflow(lines)


def _cover_regions(boxes: Mapping[str, list[list[int]]] | None) -> tuple[list[list[int]], list[list[int]]]:
    """The regions to blur (danmaku, avoiding people and faces) and the other people's boxes to keep."""
    danmaku = boxes.get("danmaku") if boxes else None
    person = boxes.get("person") if boxes else None
    guests = boxes.get("guest") if boxes else None
    blur = blur_boxes(danmaku, list(person or []) + list(guests or []), COVER_FACE_BOX)
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
            new_lines = [normalize.canon_text(str(x).strip())[0]
                         for x in (_json_from_text(retry["response_text"]).get("cover_lines") or [])]
        except (ValueError, AttributeError):
            break
        if not _cover_lines_ok(new_lines) or any(placeholder_as_person(ctx.session, line) for line in new_lines):
            break
        new_lines, canon_line = writing_canon.settle_cover(ctx, new_lines, cpa_calls, call_id=f"writing-canon-cover-{attempts}")
        process.append(canon_line)
        process.append(f"封面文字缩短（第 {attempts} 次）：{' / '.join(new_lines)}（所有候选用同一组新字重渲）")
        cover_lines = new_lines
        cover = render(cover_lines)
    return cover, cover_lines


def pick_title(ctx: Ctx) -> tuple[str, list[str], list[str], list[CpaEvent], Path] | StepOutcome:
    """Step 6's title half: candidates from the authorities, the checks, the judge, the choice; writes
    title/candidates.json (with the authorities' hashes) and title/title.txt.  Returns (title, cover lines, process
    lines, CPA calls, candidates path), or the failed outcome."""
    names = participant_names(ctx.session)
    cpa_calls: list[CpaEvent] = []
    result = _title_rounds(ctx, names, cpa_calls)
    if isinstance(result, StepOutcome):
        return result
    rounds, method, history, understanding, verdicts, circle = result
    approved = verdicts.approved(str(ctx.state.data["clip"].get("cid") or ""))
    authorities = [*method.sources_json(), verdicts.source.as_dict()]
    chosen, forced = _choose_title(rounds)
    if chosen is None:
        return _fail("TITLE_NO_CANDIDATE", "CPA returned no title candidates", cpa=cpa_calls)
    chosen_round = rounds[chosen["round"]]
    cover_lines = [normalize.canon_text(str(x).strip())[0] for x in (chosen_round["parsed"].get("cover_lines") or [])]
    try:
        title, cover_lines, canon_line = writing_canon.settle_title_cover(ctx, str(chosen["title"]).strip(), cover_lines, cpa_calls)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls)
    from .revise_run import title_online      # that module reaches the steps through trim_run: imported when used

    online = None if approved else title_online(ctx.clip_dir, ctx.state)      # a revision keeps the title online
    title = online or title
    cands_path = _local(ctx, "title/candidates.json")
    cands_path.write_text(json.dumps({
        "candidates": [dict(c) for rnd in rounds for c in rnd["candidates"]],
        "rounds": [rnd["call_id"] for rnd in rounds], "chosen": chosen, "forced": forced,
        "recommended": [rnd["parsed"].get("recommended") for rnd in rounds],
        "pick_calls": [rnd["pick_call"] for rnd in rounds],
        "authorities": authorities, "corpus_titles": method.corpus_count,
        "understanding": understanding, "context": circle_context.summary(circle), "owner_approved_title": approved,
        "title_history": {"published": history.published_titles, "published_names": history.published,
                          "batch": history.batch, "notes": history.notes},
        "cover_lines": cover_lines}, ensure_ascii=False, indent=2), encoding="utf-8")
    judge = chosen.get("judge") or cast(TitleJudge, {})
    process = [f"标题候选：{len(rounds)} 轮，共 {sum(len(r['candidates']) for r in rounds)} 个；"
               f"采用：{title}（自检 {chosen['checks']['score']}/{len(TITLE_RULES)}，"
               f"判官 {judge.get('overall', '未判')}）",
               "标题提示词读取的权威：" + "；".join(f"{s['name']} {s['sha256'][:12]}" for s in authorities)]
    process.append(canon_line)
    if online:
        process.append(f"修订沿用线上标题：{online}（已发布稿件的修订不改题；改题走改题回执，本轮候选只用来定封面字）")
    process.append(title_understanding.process_line(understanding))
    process.extend(circle_context.process_lines(circle))
    process.extend(title_pick.process_line(rnd["pick_call"], n) for n, rnd in enumerate(rounds) if rnd["pick_call"])
    process.extend(history.notes)
    if approved:
        process.append(f"频道主选定标题正文：{approved['title_body']}（原话：{approved['evidence']}）")
    if forced and chosen.get("owner_approved"):
        process.append("频道主选定的标题未过检查：" + "；".join([*chosen["checks"]["reasons"],
                                                       *(judge.get("fail_reasons") or ["未送判官"])])
                       + "（仍采用它，行政亲审前请处理）")
    elif forced:
        process.append("候选全部未通过自检与判官（两轮重写之后），按得分取最高的一条继续；未过的项：" +
                       "；".join([*chosen["checks"]["reasons"], *(judge.get("fail_reasons") or ["未送判官"])])
                       + "（行政亲审前请处理）")
    title_path = _local(ctx, "title/title.txt")
    title_path.write_text(title + "\n", encoding="utf-8")
    if not _cover_lines_shape_ok(cover_lines) and cover_import.imported_cover(ctx) is None:      # an imported cover brings its own lines
        return _fail("COVER_LINES_INVALID", f"cover_lines={cover_lines}", cpa=cpa_calls)
    process.append(f"封面文字（CPA）：{' / '.join(cover_lines)}")
    return title, cover_lines, process, cpa_calls, cands_path


def step6_title_cover(ctx: Ctx) -> StepOutcome:
    final_srt = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    cues = parse_srt(final_srt)
    names = participant_names(ctx.session, visual=True)
    picked = pick_title(ctx)
    if isinstance(picked, StepOutcome):
        return picked
    title, cover_lines, process, cpa_calls, cands_path = picked
    if (imported := cover_import.imported_cover(ctx)) is not None:      # the decided cover: nothing is drawn or looked at
        return cover_import.adopt(ctx, imported, title=title, process=process, cpa=cpa_calls)
    title_path = ctx.clip_dir / "title/title.txt"
    clip = _clip(ctx)
    cut_len = (clip["end_ms"] - clip["start_ms"]) / 1000
    frame_started = time.monotonic()
    frame_t, frame_note, boxes = _pick_cover_frame(ctx, cues, cover_lines, cut_len)
    frame_s = time.monotonic() - frame_started
    process.append(f"封面帧：{frame_note}（{frame_s:.1f}s）")
    vision, vision_line = cover_vision_calls(ctx, 1)
    cpa_calls.extend(vision)
    process.append(vision_line)
    blur, keep = _cover_regions(boxes)
    process.append(f"模糊区域（已避开人物与人脸）：{blur}")
    brief = _cover_brief(ctx, cues, title, cover_lines, names)
    cpa_calls.append(brief.pop("_cpa"))
    if brief.get("_failed"):
        return _fail(brief["_failed"], brief.get("_detail", ""), cpa=cpa_calls, process=process)
    plan = cover_plan(ctx, cover_lines, brief, names)
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
    cands_path.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    return StepOutcome(True, products={"title": str(title_path), "candidates": str(cands_path),
                                       "cover": cover.products["cover"], "choose": cover.products["choose"]},
                       cpa=cpa_calls, note=f"采用标题 {title}；封面 {cands['cover_route']['chosen']['route']}"
                       + ("；封面素材被否决（非阻塞，见 PROCESS 与 choose.json material_flag）" if route.get("material_flag") else ""),
                       process=process)
