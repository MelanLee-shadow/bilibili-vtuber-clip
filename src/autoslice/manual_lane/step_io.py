"""What every step body shares: the clip's local and remote places, the CPA call with its cache and log record,
prompt rendering, and the failed-step outcome.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from src.autoslice.channel_profile import load_channel_profile

from . import channel
from . import (
    doubt_policy,
)
from .adapters import CPA_JUDGE_EFFORT, CPA_JUDGE_MODEL, CPA_TEXT_TRIES, REMOTE_REPO, RemoteResult
from .lane_rules import rules
from .layout import layout
from .reselect import load_cues
from .runner import Ctx, StepOutcome
from .srt_text import JSONMalformed, JSONNotFound, JSONTruncated, SrtCue, _json_from_text
from .state import CpaEvent, RejectedAnswer, bcut_source_zero_ms

if TYPE_CHECKING:
    from .config import SpeakerPeople
    from .state import ClipInfo

PKG_ROOT = Path(__file__).resolve().parents[3]
PROMPTS = PKG_ROOT / "assets" / "manual_lane" / "prompts"
REMOTE_TOOLS = PKG_ROOT / "assets" / "manual_lane" / "remote"
REMOTE_FONTS = f"{REMOTE_REPO}/{channel.ASSETS_REL}/fonts"
ROOM_ID = int(layout().room_id)   # the channel's live room (profile identity.room_id)


# ----- shared helpers ---------------------------------------------------------------------
def _clip(ctx: Ctx) -> ClipInfo:
    return ctx.state.data["clip"]


def _mac_bcut_cues(ctx: Ctx) -> list[SrtCue]:
    clip = _clip(ctx)
    return load_cues(Path(clip["mac_bcut_srt"]), source_zero_ms=bcut_source_zero_ms(clip))


def _local(ctx: Ctx, rel: str) -> Path:
    path = ctx.clip_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run_remote(ctx: Ctx, name: str, body: str, timeout: int = 1800) -> RemoteResult:
    return cast(RemoteResult, ctx.adapters.remote.run_script(name=name, body=body, timeout=timeout))


def _cpa_cache_path(ctx: Ctx, prompt_text: str, run: int = 0, effort: str = CPA_JUDGE_EFFORT,
                    model: str = CPA_JUDGE_MODEL, max_output_tokens: int | None = None) -> Path:
    """COST-07: the answer is keyed by the complete prompt text, the model and the effort.  An unchanged prompt is never
    sent again; a changed prompt (new candidates, new context, new work lookup) is a new request.  `run` > 0 is one of
    several independent answers an authority asks for to the same prompt (the title judge runs twice): each run is
    its own entry, and run 0 keeps the key every earlier answer was stored under.

    TASK-37: the effort is part of the key, so changing a purpose's tier never reads an answer that was made at the old
    tier.  The key keeps the `[run]` tail only when run > 0, so an entry stored before this task (medium, run 0) still
    matches when the purpose asks for medium."""
    parts = ([model, effort, prompt_text] + ([run] if run else [])
             + ([f"max_output_tokens={max_output_tokens}"] if max_output_tokens is not None else []))
    key = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()
    return ctx.clip_dir.parent.parent / "cache" / "cpa" / f"{key}.json"


def _cpa_settings_of(purpose: str | None) -> tuple[str, str, int | None, str]:
    """TASK-37 (U168): the thinking tier of one CPA text call, by what the call is for.

    Returns (model, effort, max_output_tokens, note). The tier comes from lane_rules `effort.purposes`; `note` is empty when
    the purpose is registered and is the PROCESS line to write when it is not: an unregistered purpose (or none at all)
    keeps the `effort.default` tier and says so, so a new call site is never silently left on a tier nobody chose."""
    try:
        section = rules("effort")
    except KeyError:                  # a rules file from before this task (an experiment copy)
        section = {}
    purposes = section.get("purposes") or {}
    default = str(section.get("default") or CPA_JUDGE_EFFORT)
    if purpose and purpose in purposes:
        entry = purposes[purpose] or {}
        effort = str(entry.get("effort") or default)
        return str(entry.get("model") or CPA_JUDGE_MODEL), effort, entry.get("max_output_tokens"), ""
    named = f"用途 `{purpose}`" if purpose else "调用没有写用途"
    return CPA_JUDGE_MODEL, default, None, (f"{named}没有在 lane_rules effort.purposes 登记，这次按默认档位 {default} 调用"
                                            f"（U168：请给这个用途定档并登记，别让它悄悄留在默认档）")


def _effort_of(purpose: str | None) -> tuple[str, str]:
    _, effort, _, note = _cpa_settings_of(purpose)
    return effort, note



def _write_atomic(path: Path, text: str) -> None:
    """Write beside the target and rename into place (TASK-31): a parallel reader never sees half a cache file, and
    two calls producing the same key cannot interleave half of one answer with half of another."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _cache_cpa_answer(path: Path, answer: CpaEvent, *, keep_http_log: bool = False) -> None:
    omitted = {"call_id"} if keep_http_log else {"call_id", "http_log"}
    _write_atomic(path, json.dumps({k: v for k, v in answer.items() if k not in omitted}, ensure_ascii=False))


def _record_cpa_files(ctx: Ctx, call_id: str, prompt_text: str, record: CpaEvent, cache: Path,
                      *, only_missing: bool = False) -> CpaEvent:
    rel = f"cpa/{call_id}"
    files = [(ctx.clip_dir / f"{rel}.prompt.txt", prompt_text),
             (ctx.clip_dir / f"{rel}.completion.txt", record.get("response_text", ""))]
    if only_missing:
        for path, text in files:
            if path.exists() and path.read_bytes() != text.encode("utf-8"):
                raise ValueError(f"existing CPA record differs: {path.name}")
    for path, text in files:
        if not only_missing or not path.exists():
            _write_atomic(path, text)
    record["file"] = str(ctx.clip_dir / rel)
    record["cache_path"] = str(cache)
    return record


def _answer_problem(text: str, accepts: Callable[[Any], str] | None) -> str:
    """Why a whole-looking answer is not usable, or "" when it is.

    TASK-35: a cut answer raises JSONTruncated — it is asked again.  A first value that closes but does not parse
    (JSONMalformed) and an answer with no JSON at all (JSONNotFound) are different failure classes and are left to
    the caller's own read, as before: re-sending the same prompt does not turn broken JSON or prose into JSON, and
    the caller already names those (CPA_RESPONSE_UNPARSEABLE).  `accepts` is the caller's own contract on the parsed
    value (the adjudication's batch ids)."""
    try:
        value = _json_from_text(text)
    except JSONTruncated as exc:
        return f"CPA_ANSWER_INCOMPLETE ({exc})"
    except (JSONMalformed, JSONNotFound):
        return ""
    return accepts(value) if accepts else ""


def _ask_cpa(ctx: Ctx, call_id: str, prompt_text: str, *, run: int = 0, purpose: str | None = None,
             accepts: Callable[[Any], str] | None = None, max_attempts: int = CPA_TEXT_TRIES,
             transport_tries: int | None = None) -> CpaEvent:
    """Full record (with response_text) for parsing; _log() strips it before it reaches the state file.
    The prompt and the completion are kept under clip_dir/cpa/ as the record of what CPA was asked and said.
    A successful answer to an identical prompt is read from the cache: no CPA call.

    TASK-37 (U168): `purpose` names what this call is for (lane_rules effort.purposes: boundary, adjudicate,
    concat-check, speaker-review, title, title-judge, cover-brief, cover-text, review, review-c, import-meaning).  The
    thinking tier follows the purpose, so a judge or a whole-clip review thinks and a confirmation does not.  The tier
    is written into the record (`effort`), into the cache key (a new tier never reads an old tier's answer) and, for an
    unregistered purpose, into `effort_note`, which runner.write_process prints as a PROCESS line.

    TASK-31: safe to call from several threads at once.  The call itself runs outside any lock; the cache file is
    written to a temporary name and renamed into place; each call's prompt/completion files carry its own call_id.
    Every record a step keeps is ordered by the caller's input order, not by which call finished first (parallel.run).

    TASK-35: an answer counts only when its text holds one whole JSON value.  A cut answer — the provider said
    `complete`, the stream ended mid-value, which TASK-26's completion word cannot see — is not an answer: it is not
    cached, a cached copy of it is dropped, and the same prompt is asked again while CPA_TEXT_TRIES lasts (the number
    cpa_text.py itself retries with).  When the tries run out the record is a failed call (rc != 0, no text, the
    reason leading stderr_tail), so every caller stops on it as on any other tool failure instead of reading half an
    answer as a smaller answer.  `accepts` is the caller's contract on a whole answer (the adjudication requires
    every doubt id of its batch); an answer that breaks it is asked again exactly like a cut one.

    A call that failed at the provider (rc != 0) is not asked again here: the adapter's own tries already ran.
    """
    model, effort, max_output_tokens, effort_note = _cpa_settings_of(purpose)
    cache = _cpa_cache_path(ctx, prompt_text, run, effort, model, max_output_tokens)
    rel = f"cpa/{call_id}"
    rejected: list[RejectedAnswer] = []
    record: CpaEvent | None = None
    spent = 0.0
    requests = 0       # every HTTP request this call made, across the provider's tries and the re-asks below
    for attempt in range(1, max_attempts + 1):
        cached = cache.exists()
        if cached:
            answer = cast(CpaEvent, dict(json.loads(cache.read_text(encoding="utf-8")), call_id=call_id, cached=True))
        else:
            reply_id = call_id if attempt == 1 else f"{call_id}--reply-{attempt}"
            extra = {"tries": transport_tries} if transport_tries is not None else {}
            if max_output_tokens is not None:
                extra["max_output_tokens"] = max_output_tokens
            answer = cast(CpaEvent, ctx.adapters.cpa.ask(call_id=reply_id, prompt_text=prompt_text, model=model,
                                                         effort=effort, remote_dir=ctx.remote_dir, **extra))
            answer["call_id"] = call_id
        spent += float(answer.get("elapsed_s") or 0.0)
        requests += 1 if cached else max(1, answer.get("attempts") or 1)
        if not _cpa_ok(answer):
            record = answer          # the call itself failed: the provider's own retry already ran
            break
        problem = _answer_problem(answer.get("response_text", ""), accepts)
        if not problem:
            record = answer
            if not cached:
                _cache_cpa_answer(cache, answer)
            break
        _write_atomic(_local(ctx, f"{rel}.rejected-{attempt}.completion.txt"), answer.get("response_text", ""))
        rejected.append({"attempt": attempt, "reason": problem, "chars": len(answer.get("response_text", "")),
                         "usage": answer.get("usage")})
        if cached:
            Path(cache).unlink(missing_ok=True)   # void: no rerun may read this answer again
    if record is None:
        last = rejected[-1] if rejected else {"reason": "CPA_ANSWER_INCOMPLETE", "chars": 0}
        reason = last["reason"].split(" (", 1)[0]    # the failure the last try ended on (what the caller can act on)
        record = {"call_id": call_id, "model": model, "effort": effort, "stream": True,
                  "rc": 1, "complete": False, "reason": reason, "elapsed_s": round(spent, 1),
                  "response_text": "", "rejected_answers": rejected,
                  "stderr_tail": (f"CPA_ANSWER_INCOMPLETE: {len(rejected)} answers in a row were not whole "
                                  f"(last: {last['chars']} chars, {last['reason']}); "
                                  f"originals under cpa/{call_id}.rejected-*.completion.txt")}
    elif rejected:
        record = cast(CpaEvent, dict(record, rejected_answers=rejected))   # answered whole on a later try
    # the tier this call was actually made at, whatever the adapter echoed back (a cached answer kept the old record's)
    record["effort"] = effort
    record["requests"] = requests
    if max_output_tokens is not None:
        record["max_output_tokens"] = max_output_tokens
    if purpose:
        record["purpose"] = purpose
    if effort_note:
        record["effort_note"] = effort_note
    return _record_cpa_files(ctx, call_id, prompt_text, record, cache)


def _log(record: CpaEvent) -> CpaEvent:
    return cast(CpaEvent, {k: v for k, v in record.items()
                           if k != "response_text" and (k != "http_log" or record.get("cached") is True)})


def _cpa_ok(record: CpaEvent) -> bool:
    return record.get("rc") == 0 and bool(record.get("response_text", "").strip())


def failure_line(record: CpaEvent) -> str:
    """Why a call that is not `_cpa_ok` failed, in one line."""
    return str(record.get("reason") or record.get("stderr_tail") or "no answer").strip().splitlines()[0][:200]


def ask_json(ctx: Ctx, call_id: str, prompt_text: str, *, purpose: str, problem: Callable[[Any], str], shape: str,
             cpa_calls: list[CpaEvent], unparseable: str = "CPA_RESPONSE_UNPARSEABLE") -> tuple[Any, str]:
    """One model call whose answer must be a JSON value `problem` has nothing against (`problem` returns "" for a usable
    value, else what is wrong with it).  Returns (value, "") or (None, why there is no value).  An answer outside the
    contract is asked again while the tries last (the `accepts` of _ask_cpa; the failure then begins with `shape`).  The
    call's record is appended to cpa_calls whether or not it gave a value."""
    record = _ask_cpa(ctx, call_id, prompt_text, purpose=purpose,
                      accepts=lambda value: f"{shape} ({found})" if (found := problem(value)) else "")
    cpa_calls.append(_log(record))
    if not _cpa_ok(record):
        return None, failure_line(record)
    try:
        value = _json_from_text(record["response_text"])
    except ValueError as exc:
        return None, f"{unparseable} ({str(exc)[:160]})"
    found = problem(value)
    return (None, found) if found else (value, "")


def _principle_section(heading: str, what: str) -> str:
    path = load_channel_profile(PKG_ROOT).asset_file("subtitle_correction_principles")
    text = path.read_text(encoding="utf-8")
    _, found, section = text.partition(heading)
    body, end, _ = section.partition("\n## ")
    if not found or not end or not body.strip() or heading in section:
        raise ValueError(f"{path}: missing or invalid {what} section")
    return heading + body.rstrip()


def _pronoun_rule() -> str:
    return _principle_section("## 六、代词与指代对象一致", "pronoun")


# The principle sections that decide how the subtitle agent writes characters, taken from the active channel
# profile in the same way as the pronoun rule: foreign words keep their language; SC/gift context and thanks.
AGENT_PRINCIPLE_HEADINGS = ("## 五、外来词保留原文", "## 七、superchat / SC 语境")


def _agent_principles() -> str:
    return "\n\n".join(_principle_section(heading, "agent principle") for heading in AGENT_PRINCIPLE_HEADINGS)


PRONOUN_RULE = _pronoun_rule()
AGENT_PRINCIPLES = _agent_principles()


def _render(prompt_name: str, **fields: str) -> str:
    text = channel.fill((PROMPTS / prompt_name).read_text(encoding="utf-8"))
    fields.setdefault("pronoun_rule", PRONOUN_RULE)
    fields.setdefault("strict_rules", "")   # optional block: empty unless a variant asks for it
    fields.setdefault("window_asr_doc", "")   # the window ASR sources, only where a doubt carries one
    for key, value in doubt_policy.vote_prompt_fields().items():   # the vote scheme's blocks (adjudication prompt)
        fields.setdefault(key, value)
    # Separating words already present in the evidence is not the experimental extra listening pass.
    fields.setdefault("two_speaker_rules", TWO_SPEAKER_RULES)
    fields.setdefault("two_speaker_fields", TWO_SPEAKER_FIELDS)
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text


def _fail(reason: str, note: str, *, cpa: Sequence[CpaEvent] = (), process: Sequence[str] = ()) -> StepOutcome:
    if reason == "CPA_RESPONSE_UNPARSEABLE" and cpa and isinstance(cpa[-1], Mapping) and cpa[-1].get("cache_path"):
        # an answer the step cannot read is not kept as the answer to that prompt: the rerun asks again (TASK-11:
        # a truncated adjudication was cached and every rerun stopped on it)
        Path(str(cpa[-1]["cache_path"])).unlink(missing_ok=True)
    return StepOutcome(False, reason=reason, note=note, cpa=[_log(c) for c in cpa], process=list(process))


HOST_LABEL = channel.HOST_LABEL   # the canonical host label (the channel profile's host_speaker_label); the lane's A3 check needs it


# Word separation is available whenever existing evidence identifies two voices.  The optional extra per-speaker
# listening pass remains controlled by two_speaker_doubts(); it must not disable this output contract.
TWO_SPEAKER_RULES = "\n- speaker_note（若有）：声纹按约 1.2 秒分段的结果，显示这一句里可能有两个人说话（例如一个人说话时另一个人插了一两个字）。readings 里的 speakers:* 是这句按「分人」要求的盲听：每行前的「甲：」「乙：」区分两个人，甲先开口。\n- 一句里有两个人时：只有分人盲听（speakers:*）里确实出现了第二个人（「乙：」的行），或别的来源写出了另一个人的话，才拆开：final_text 写说话较多的那个人在这句里说的话，另一个人在这句时间里插的话写进 other_speaker_text（会作为时间重叠的另一条字幕），并用 other_first 说明另一个人的话是不是在前面（true 表示另一个人先说）。两个人的话都受单来源新增规则约束；另一人的话也必须有不止一家独立来源给出，同家多个入口不能相互作证。听写或识别里没有的不要补，也不要把同一个人的一句话拆成两半。不拆时 other_speaker_text 写空字符串、other_first 写 false。"
TWO_SPEAKER_FIELDS = "\"other_speaker_text\": \"同一句里另一个人插的话，没有则空字符串\", \"other_first\": true 或 false, "


def two_speaker_doubts() -> bool:
    """MANUAL_LANE_TWO_SPEAKER_DOUBTS=1: a sentence where the voiceprint shows two people becomes a doubt, is heard once
    more with the per-speaker prompt, and CPA may split off the other person's line.  Off by default: on the training
    set it lowered the score twice (TASK-6 round2r-C5 70 -> 67, C5b -> 65, scorer v2): the per-speaker Low dictation
    recovered none of the missing replies and misled CPA.  The chunk voiceprint itself is always computed and step 4
    uses it (speaker 107 -> 110 of 125)."""
    return os.environ.get("MANUAL_LANE_TWO_SPEAKER_DOUBTS") == "1"


def linked_session(session: SpeakerPeople) -> bool:
    """U110: a session with two or more participants is a linked clip and always separates the speakers.  The one
    predicate for step 3's chunk voiceprint, step 4 and the review prompt (session.speaker_mode is not consulted here:
    the live states say uniform_host for these linked sessions)."""
    return len(session.get("participants") or []) >= 2


def superseded(task_dir: Path, cid: str) -> bool:
    """Whether candidate `cid` of the task was replaced by a successor (a candidate without a state file was not)."""
    path = task_dir / "state" / f"{cid}.json"
    return path.is_file() and json.loads(path.read_text(encoding="utf-8"))["clip"].get("status") == "SUPERSEDED"


def _other_live(clip_dir: Path, rel: str) -> list[Path]:
    """`rel` under every other clip directory of the task, in name order, leaving out replaced candidates."""
    return [path for path in sorted(clip_dir.parent.glob(f"*/{rel}"))
            if (other := path.parents[len(Path(rel).parts) - 1]) != clip_dir
            and not superseded(clip_dir.parent.parent, other.name)]
