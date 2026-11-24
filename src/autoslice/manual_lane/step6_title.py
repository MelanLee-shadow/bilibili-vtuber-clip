"""Step 6 title: title checks and the publish gate, then the step body that picks the title and renders
the cover.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from src.autoslice.channel_envelope import EnvelopeError, HOST_NAMES, parse_talk_title, render_talk_title

from . import channel, collab_guests
from .publication_id import publication_cid
from . import (
    circle_context,
    cover_import,
    gates,
    graph_context,
    normalize,
    parallel,
    text_overlap,
    title_authority,
    title_history,
    title_acceptance,
    title_names,
    title_person,
    title_pick,
    title_understanding,
    title_verdicts,
    writing_canon,
)
from .cover_redo import RetryBudget
from .name_consistency import danmaku_pool
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
    cover_line,
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
from .step_io import _ask_cpa, _clip, _cpa_ok, _fail, _local, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent
    from .subtitle_track import TrackCue

# ----- step 6: title and cover (CPA picks the title; the cover is rendered from CPA's lines) ------


TITLE_MAX_REGEN = 2                              # regeneration rounds after the first candidate round
JUDGED_PER_ROUND = 3     # candidates of a round sent to the judges in turn (owner-taste order) before the round is rewritten
REWRITE_ONLY = {"cover_lines_too_long"}   # failed checks repaired after the pick (cover_retry): the last round judges them
TITLE_RULES = ("banned", "prefix", "on_stage_known", "named", "involved_named", "prefix_names", "swap", "punchline",
               "person", "quotes", "canonical_names", "publish_gate", "published_duplicate", "batch_opener",
               "label_as_name", "first_person_speaker")
OPENER = 3      # a title's first characters after the prefix; two clips of one batch do not open alike (11-23: 8 of 10 「游戏里…」)


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
    runs: list[JudgeRun]                       # the fact judge (skill 八.3)
    quality: list[JudgeRun]                    # the writing judge (skill 八.4: section 四)
    authorities: list[title_authority.SourceRecord]


class SwapCheck(TypedDict, total=False):
    holds_with_other_host: bool | None


class TitleCandidate(TypedDict, total=False):
    title: str
    title_body: str
    on_stage: list[str]
    involved: list[str]                        # the other people the main line is about, as the subtitles call them
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


SELF_REPORT = ("on_stage_known", "swap", "punchline")     # rules on the writer's own account of its candidate


def opener(title: str) -> str:
    """The first OPENER letters of a title's body (after 【…】); "" when it is shorter."""
    envelope = parse_talk_title(title)
    letters = text_overlap.letters(envelope.body if envelope is not None else title)
    return letters[:OPENER] if len(letters) >= OPENER else ""


def title_checks(cand: TitleCandidate, names: Sequence[str], subtitles: str, *,
                 envelope_error: str = "", published: tuple[str, ...] = (), batch: Sequence[str] = (),
                 non_formal: Sequence[title_names.NonFormal] = (), self_report: bool = True) -> TitleChecks:
    """The deterministic checks of skill section 4 on one candidate.  Each rule it breaks is named once in `reasons`;
    `score` counts the rules the candidate passes (len(TITLE_RULES) is perfect).  `subtitles` is the final SRT with
    the speaker labels where there are any: quoted words are checked against it by the repo's checker
    (title_cover_quotes; skill section 8 items 1-2: verbatim, and the speaker when the title names who said it).
    `non_formal` are the non-formal spellings of a name found in the body (title_names).  `batch`: the titles the
    other clips of this batch already have (title_history.recent_titles).  `self_report=False` leaves out the rules on
    the writer's own account of its candidate (SELF_REPORT): a title written without one (the review's) is judged by
    the skill's judge instead."""
    from src.autoslice.title_quote_check import check_title_quotes

    title = str(cand.get("title", "")).strip()
    failed: dict[str, str] = {}
    if title_history.duplicate_title(title, published):
        failed["published_duplicate"] = "标题与已发标题重复，请依据本片字幕重写"
    if leaked := label_as_name(title, subtitles):
        failed["label_as_name"] = LABEL_REASON.format(leaked)
    if alike := [t for t in batch if opener(t) and opener(t) == opener(title)]:
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
    left_out = [n for n in dict.fromkeys(involved) if n not in HOST_NAMES and n not in title]
    if left_out:      # skill section 1, bottom line 2 (the research's strongest title effect): name who the story involves
        failed["involved_named"] = "标题未点名这段涉及的人：" + "、".join(left_out)
    partners = tuple(dict.fromkeys(n for n in on_stage if n not in HOST_NAMES))
    if (envelope_error or (envelope is None and partners)
            or (envelope is not None and set(envelope.partners) != set(partners))):
        failed["prefix_names"] = f"前缀未写出场者或与出场名单不一致（应为【{channel.HOST}×…】）"
        if envelope_error:
            failed["prefix_names"] += "：" + envelope_error
    swap = cand.get("swap_check") or {}
    if swap.get("holds_with_other_host") is not False:
        failed["swap"] = "换主播自检未通过（或缺失）：换成别的主播仍成立，要重写"
    if not str(cand.get("punchline_person") or "").strip():
        failed["punchline"] = f"笑点未落在{channel.HOST}本人身上（punchline_person 为空）"
    mixed = title_person.mixed_person(title)
    if mixed:
        failed["person"] = title_person.reason(mixed)
    if others := title_person.misattributed(title, subtitles, host=HOST, guests=[n for n in names if n != HOST]):
        failed["first_person_speaker"] = title_person.misattributed_reason(others)
    quotes = check_title_quotes(title, subtitles)
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


ONLINE_RECHECK = ("quotes", "first_person_speaker", "person", "label_as_name")   # what a revision's subtitles decide
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
    not this round's, and the subtitles' facts are what ONLINE_RECHECK reads."""
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
                        non_formal=formal.non_formal(envelope.body if envelope is not None else title), self_report=False)


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
                cpa_calls: list[CpaEvent], *, facts: bool = True) -> TitleJudge | StepOutcome:
    """The skill's acceptance judges, the one interface every title that goes out passes (step 6's pick, a revision's
    published title it would keep, the review's rewrite): the fact judge (section 八 item 3, references/judge-prompt.md,
    JUDGE_RUNS runs) and the writing judge (八 item 4: section 四, references/quality-judge-prompt.md, QUALITY_RUNS
    runs, each verdict recomputed from its items).  PASS only when every run of both says PASS.  Returns the verdict
    (each run, the authority hashes), or the failed outcome when CPA cannot be reached.

    Owner (U471): 「你看看这标题起的，遵循了标题skill吗遵循标题原则吗？」 — the 11-23 collab titles
    passed the fact judge, the only judge then, and broke section 四 (no mishap, a narrator's 流水账, a 我 nobody could
    place).  TASK-31: the runs are independent answers (each its own cache entry, `run`), asked at once (parallel.run,
    bound lane_rules concurrency.cpa); runs and records are reported in run order.  `facts=False` asks the writing
    judge alone (a published title a revision would keep, whose facts ONLINE_RECHECK reads)."""
    prompt, sources = title_authority.judge_prompt(track_lines, title, cover_lines)
    q_prompt, q_sources = title_authority.quality_prompt(track_lines, title)
    asks = [(f"{call_id}-{run}", prompt, run, title_authority.judge_verdict)
            for run in range(1, title_authority.JUDGE_RUNS + 1) if facts]
    asks += [(f"{call_id}-q{run}", q_prompt, run, title_authority.quality_verdict)
             for run in range(1, title_authority.QUALITY_RUNS + 1)]
    records = parallel.run(asks, lambda a: _ask_cpa(ctx, a[0], a[1], run=a[2], purpose="title-judge"), provider="cpa")
    runs: list[JudgeRun] = []
    quality: list[JudgeRun] = []
    for (cid, _prompt, _run, verdict), record in zip(asks, records, strict=True):
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=cpa_calls)
        try:
            parsed = _json_from_text(record["response_text"])
        except ValueError:
            parsed = None
        (quality if verdict is title_authority.quality_verdict else runs).append(
            cast(JudgeRun, dict(verdict(parsed), call_id=cid)))
    failed = [r for r in runs if r["overall"] != "PASS"]
    q_failed = [r for r in quality if r["overall"] != "PASS"]
    reasons = [*(x for r in failed for x in r["fail_reasons"]),
               *("写法（第四节）：" + x for r in q_failed for x in r["fail_reasons"])]
    return {"overall": "FAIL" if failed or q_failed else "PASS", "fail_reasons": list(dict.fromkeys(reasons)),
            "runs": runs, "quality": quality,
            "authorities": [s.as_dict() for s in dict.fromkeys([*sources, *q_sources])]}


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


def _title_rounds(ctx: Ctx, names: Sequence[str], cpa_calls: list[CpaEvent]
                  ) -> tuple[list[TitleRound], title_authority.TitleMethod, title_history.TitleHistory,
                             title_understanding.Understanding, title_verdicts.OwnerVerdicts,
                             circle_context.CircleContext] | StepOutcome:
    """Round 0 is the first candidate round; then up to TITLE_MAX_REGEN regenerations with the failed checks and the
    judge's reasons as feedback, until a candidate passes both.  In each round the candidates that pass the deterministic
    checks go to the judges (title_judge: facts and writing) one after another in the owner-taste order of title_pick,
    leaving out one predicted 差, at most JUDGED_PER_ROUND of them, none when that order puts a predicted 差 first; a
    judged PASS ends the rounds.  Returns the rounds and the method the prompt carried (with the owner
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
    if (retitle := verdicts.retitle(cid)) and not approved:   # he said what the clip's draw is: titles are written from it
        understanding["merged"]["main_line"] = retitle["main_line"]
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
            return writing_canon.failed(exc, cpa_calls)
    basis = title_pick.PickBasis(subtitles=title_authority.subtitle_text(track),
                                 main_line=understanding["merged"]["main_line"],
                                 punchline=understanding["merged"]["punchline"], corpus=verdicts.corpus(cid),
                                 circle=circle_block)
    history = title_history.recent_titles(ctx.state.path, ctx.clip_dir, str(ctx.state.data["clip"].get("cid") or ""), ctx=ctx)
    rounds: list[TitleRound] = []
    feedback = ""
    anonymous = unnamed_guests(ctx, track)
    speaker_note = _speaker_note(track, names, anonymous=anonymous)
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
                        title = render_talk_title(title, [name for name in on_stage if name not in HOST_NAMES])
                    except EnvelopeError as exc:
                        envelope_error = str(exc)
            cand["title"] = title
            envelope = parse_talk_title(title)
            body = envelope.body if envelope is not None else title
            cand["name_fixes"] = name_fixes
            cand["prefix_repeat"] = envelope is not None and title_names.prefix_repeat(envelope.partners, body)
            cand["checks"] = title_checks(cand, names, srt_text, envelope_error=envelope_error,
                                          published=history.published_titles, batch=history.batch,
                                          non_formal=formal.non_formal(body))
            cand["round"] = round_no
            cand["accepted"] = False
        cover = [cover_line(x) for x in (parsed_answer.get("cover_lines") or [])]
        if (overflow := cover_lines_overflow(cover)):      # cover text the renderer cannot hold at the punchline floor: rewrite
            for cand in candidates:
                cand["checks"]["ok"] = False
                cand["checks"]["failed"].append("cover_lines_too_long")
                cand["checks"]["reasons"].append(f"封面字过长：{overflow}")
                cand["checks"]["score"] -= 1
        if leaked := next(filter(None, (label_as_name(line, srt_text) for line in cover)), ""):
            for cand in candidates:      # the cover text goes with every candidate of the round
                if "label_as_name" not in cand["checks"]["failed"]:
                    cand["checks"]["ok"] = False
                    cand["checks"]["failed"].append("label_as_name")
                    cand["checks"]["reasons"].append("封面字：" + LABEL_REASON.format(leaked))
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
        # The last round has no rewrite after it, so what only asks for a rewrite no longer keeps a candidate from the
        # judges: a first predicted 差 (the round is rewritten, none judged) and a cover text too long (cover_retry
        # shortens it after the pick).  Before 11-24 the best of the rounds was taken anyway (forced); now that nothing
        # is chosen by score, a clip would otherwise stop with titles no judge ever read (review).
        last = round_no == TITLE_MAX_REGEN
        tries = [] if pick.top_is_bad and not last else [
            candidates[i] for i in pick.order
            if (candidates[i]["checks"]["ok"] or (last and set(candidates[i]["checks"]["failed"]) <= REWRITE_ONLY))
            and (not approved or candidates[i].get("owner_approved"))
            and not title_pick.predicted_bad(candidates[i].get("pick"))][:JUDGED_PER_ROUND]
        for k, cand in enumerate(tries):      # in the owner-taste order; one predicted 差 is not sent to the judges
            judged = title_judge(ctx, f"title-judge-r{round_no}" + (f"c{k + 1}" if k else ""), lines, cand["title"],
                                 cover, cpa_calls)
            if isinstance(judged, StepOutcome):
                return judged
            cand["judge"] = judged
            cand["accepted"] = judged["overall"] == "PASS"
            if cand["accepted"]:
                break
        if any(c["accepted"] for c in tries):
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
    cover_lines = [cover_line(x) for x in (chosen_round["parsed"].get("cover_lines") or [])]
    try:
        title, cover_lines, canon_line = writing_canon.settle_title_cover(ctx, str(chosen["title"]).strip(), cover_lines, cpa_calls)
    except writing_canon.WritingCanonFailed as exc:
        return writing_canon.failed(exc, cpa_calls)
    from .revise_run import title_online      # that module reaches the steps through trim_run: imported when used

    retitle = verdicts.retitle(str(ctx.state.data["clip"].get("cid") or ""))
    revises = isinstance(ctx.state.data.get("revised_from"), dict)
    edited = (title_history.edited_title(ctx, publication_cid(str(ctx.state.data["clip"].get("cid") or ctx.clip_dir.name)))
              if revises and not (approved or retitle) else None)
    online = None if approved or retitle else title_online(ctx.clip_dir, ctx.state, edited)   # a revision keeps the title online
    stale: list[str] = []
    online_judge: TitleJudge | None = None
    if online:
        rechecked = online_title_breaks(ctx, names, online, cpa_calls)
        if isinstance(rechecked, StepOutcome):
            return rechecked
        stale, online_judge = rechecked
    if stale:                       # the revision's subtitles show the published title wrong: it is not kept silently
        online = None
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
    stops = forced and not online and not chosen.get("owner_approved")     # skill 八.4: no title is chosen by score
    process = [f"标题候选：{len(rounds)} 轮，共 {sum(len(r['candidates']) for r in rounds)} 个；"
               + ("没有采用（没有一条过两位判官）；自检得分最高的一条：" if stops else "采用：")
               + f"{title}（自检 {chosen['checks']['score']}/{len(TITLE_RULES)}，判官 {judge.get('overall', '未判')}）"
               + ("：" + "；".join([*chosen["checks"]["reasons"], *(judge.get("fail_reasons") or ["未送判官"])])[:600]
                  if stops else ""),
               "标题提示词读取的权威：" + "；".join(f"{s['name']} {s['sha256'][:12]}" for s in authorities)]
    process.append(canon_line)
    if online:
        process.append(f"修订沿用线上标题：{online}（已发布稿件的修订不改题；改题走改题回执，本轮候选只用来定封面字）")
    elif stale:
        process.append("线上标题对不上本轮字幕，不沿用：" + "；".join(stale)
                       + f"；改用本轮选出的标题，发布前须先把线上标题改成它（{TITLE_RECEIPT_HINT}）")
    elif retitle and not approved:
        process.append(f"频道主要求换标题（原话：{retitle['evidence']}），修订不沿用线上标题，用本轮选出的标题；"
                       f"标题与封面字按他说的看点写（笑点：{retitle['punchline']}），不按模型自己读出的主线")
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
    elif forced and online:
        process.append("本轮候选没有一条过两位判官，沿用的线上标题已重判通过；本轮候选只用来定封面字")
    if stops:
        return _fail(title_acceptance.NOT_ACCEPTED, f"{len(rounds)} 轮候选没有一条过两位判官（事实、写法），不按得分硬选："
                     + "；".join([*chosen["checks"]["reasons"], *(judge.get("fail_reasons") or ["未送判官"])])[:600],
                     cpa=cpa_calls, process=process)
    title_path = _local(ctx, "title/title.txt")
    title_path.write_text(title + "\n", encoding="utf-8")
    title_acceptance.write(ctx.clip_dir, title, "online" if online else "owner" if chosen.get("owner_approved") and forced
                           else "step6", online_judge if online else None if forced else judge)
    if not _cover_lines_shape_ok(cover_lines) and cover_import.imported_cover(ctx) is None:      # an imported cover brings its own lines
        return _fail("COVER_LINES_INVALID", f"cover_lines={cover_lines}", cpa=cpa_calls)
    process.append(f"封面文字（CPA）：{' / '.join(cover_lines)}")
    return title, cover_lines, process, cpa_calls, cands_path


def _asker(ctx: Ctx, calls: list[CpaEvent]) -> Callable[[str, str], dict | None]:
    """A CPA ask for the title step's helpers: the parsed JSON object, None when the call or its answer failed."""
    def ask(call_id: str, prompt: str) -> dict | None:
        record = _ask_cpa(ctx, call_id, prompt, purpose="title")
        calls.append(_log(record))
        if not _cpa_ok(record):
            return None
        try:
            answer = _json_from_text(record["response_text"])
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None
    return ask


def _name_guests(ctx: Ctx) -> tuple[list[str], list[CpaEvent]]:
    """collab_guests on this clip: (the guests it added to the session, its CPA call)."""
    calls: list[CpaEvent] = []
    try:
        return collab_guests.name_guests(ctx, _asker(ctx, calls)), calls
    except collab_guests.GuestsUnknown as exc:
        exc.calls = calls      # type: ignore[attr-defined]  # the failed call's record goes with the failed step
        raise


def _identify_guests(ctx: Ctx) -> tuple[dict[int, str], list[CpaEvent]]:
    """guest_identity on this clip: ({line: guest}, its CPA call)."""
    from . import guest_identity, subtitle_track

    calls: list[CpaEvent] = []
    return guest_identity.identify(ctx, subtitle_track.read_track(ctx.clip_dir), _asker(ctx, calls)), calls


def step6_title_cover(ctx: Ctx) -> StepOutcome:
    final_srt = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    cues = parse_srt(final_srt)
    try:
        guests, guest_calls = _name_guests(ctx)
    except collab_guests.GuestsUnknown as exc:
        return _fail("CPA_CALL_FAILED", str(exc), cpa=getattr(exc, "calls", []))
    identified, identity_calls = _identify_guests(ctx)
    guest_calls += identity_calls
    names = participant_names(ctx.session, visual=True)
    picked = pick_title(ctx)
    if isinstance(picked, StepOutcome):
        return picked
    title, cover_lines, process, cpa_calls, cands_path = picked
    cpa_calls[:0] = guest_calls
    if guests:
        process.insert(0, f"本场连线：{'、'.join(guests)}（场次没写、声纹听出别人，按整场弹幕认出；"
                          f"{collab_guests.CACHE.format(date=ctx.session.get('recording_date'))}）")
    if identity_calls:
        process.insert(0, f"嘉宾是谁：{len(identified)} 句连线定到具体的人（识别器分组＋对话证据，guest_identity.json）")
    if (imported := cover_import.imported_cover(ctx)) is not None:      # the decided cover: nothing is drawn or looked at
        return cover_import.adopt(ctx, imported, title=title, process=process, cpa=cpa_calls)
    if (kept := cover_import.published_cover(ctx, title)) is not None:     # a subtitle revision keeps the published cover
        return cover_import.adopt(ctx, kept, title=title, process=process, cpa=cpa_calls)
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
    plan["action_frame"] = chosen_action(ctx)
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
