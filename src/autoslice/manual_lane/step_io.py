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
from src.autoslice.knowledge_graph import NODES_FILE, load_graph

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
REMOTE_LANE_FONTS = f"{REMOTE_REPO}/assets/manual_lane/fonts"   # fallback fonts of every channel (monochrome emoji)
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

    Returns (model, effort, max_output_tokens, note). The default model comes from lane_rules `cpa.model`
    (CPA_JUDGE_MODEL when an older rules file has no `cpa` section), and the tier comes from `effort.purposes`; `note`
    is empty when the purpose is registered and is the PROCESS line to write when it is not."""
    try:
        cpa_sec = rules("cpa")
    except KeyError:
        cpa_sec = {}
    default_model = str(cpa_sec.get("model") or CPA_JUDGE_MODEL)
    try:
        section = rules("effort")
    except KeyError:                  # a rules file from before this task (an experiment copy)
        section = {}
    purposes = section.get("purposes") or {}
    default = str(section.get("default") or CPA_JUDGE_EFFORT)
    if purpose and purpose in purposes:
        entry = purposes[purpose] or {}
        effort = str(entry.get("effort") or default)
        return str(entry.get("model") or default_model), effort, entry.get("max_output_tokens"), ""
    named = f"用途 `{purpose}`" if purpose else "调用没有写用途"
    return default_model, default, None, (f"{named}没有在 lane_rules effort.purposes 登记，这次按默认档位 {default} 调用"
                                          f"（U168：请给这个用途定档并登记，别让它悄悄留在默认档）")


def _cpa_fallback_of(primary_model: str, primary_effort: str) -> tuple[str, str, int | None] | None:
    """The configured quota-fallback (model, effort, min_output_tokens) from lane_rules `cpa`, or None."""
    try:
        cpa_sec = rules("cpa")
    except KeyError:
        return None
    fb_model = str(cpa_sec.get("fallback_model") or "").strip()
    if not fb_model or fb_model == primary_model:
        return None
    efforts = cpa_sec.get("fallback_efforts")
    fb_effort = str(efforts.get(primary_effort) or primary_effort) if isinstance(efforts, Mapping) else primary_effort
    min_tok_raw = cpa_sec.get("fallback_min_output_tokens")
    fb_min_tok = int(min_tok_raw) if isinstance(min_tok_raw, int) and min_tok_raw > 0 else None
    return fb_model, fb_effort, fb_min_tok


def vision_models() -> tuple[str, str, str]:
    """(vision_model, fallback_model, fallback_low_effort) from lane_rules `cpa`."""
    try:
        cpa_sec = rules("cpa")
    except KeyError:
        return CPA_JUDGE_MODEL, "", "low"
    primary = str(cpa_sec.get("vision_model") or cpa_sec.get("model") or CPA_JUDGE_MODEL)
    fb_model = str(cpa_sec.get("fallback_model") or "").strip()
    if fb_model == primary:
        fb_model = ""
    efforts = cpa_sec.get("fallback_efforts")
    fb_low = str(efforts.get("low") or "low") if isinstance(efforts, Mapping) else "low"
    return primary, fb_model, fb_low


def _is_quota_refused(record: Mapping[str, object]) -> bool:
    """True only when a failed CPA call ended on a definite quota refusal (cpa_text.py's CPA_QUOTA_REFUSED)."""
    return not _cpa_ok(cast("CpaEvent", record)) and str(record.get("reason") or "").startswith("CPA_QUOTA_REFUSED")


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
    omitted = {"call_id", "fallback_from", "fallback_reason", "fallback_note"}
    if not keep_http_log:
        omitted.add("http_log")
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


def _ask_cpa_model(
    ctx: Ctx, call_id: str, prompt_text: str, *, active_model: str, active_effort: str, run: int,
    max_output_tokens: int | None, accepts: Callable[[Any], str] | None, max_attempts: int,
    transport_tries: int | None, suffix: str = "",
) -> tuple[CpaEvent, Path, float, int]:
    active_cache = _cpa_cache_path(ctx, prompt_text, run, active_effort, active_model, max_output_tokens)
    active_rel = f"cpa/{call_id}{suffix}"
    rejected: list[RejectedAnswer] = []
    chosen: CpaEvent | None = None
    elapsed = 0.0
    reqs = 0       # every HTTP request this model made, across the provider's tries and the re-asks below
    # the answers an earlier run already turned down for this call: the media host gives a request id its finished
    # answer again (cpa_text.py, REUSED), so the asking goes on after them instead of being turned the same answers
    # down every run (a labelling answer outside the contract left its recording failing for good, selection-bursts 5.1)
    before = len(list((ctx.clip_dir / "cpa").glob(f"{call_id}{suffix}.rejected-*.completion.txt")))
    for attempt in range(before + 1, before + max_attempts + 1):
        cached = active_cache.exists()
        if cached:
            answer = cast(CpaEvent, dict(json.loads(active_cache.read_text(encoding="utf-8")),
                                         call_id=call_id, cached=True))
        else:
            base_id = f"{call_id}{suffix}"
            reply_id = base_id if attempt == 1 else f"{base_id}--reply-{attempt}"
            extra: dict[str, Any] = {"tries": transport_tries} if transport_tries is not None else {}
            if max_output_tokens is not None:
                extra["max_output_tokens"] = max_output_tokens
            answer = cast(CpaEvent, ctx.adapters.cpa.ask(call_id=reply_id, prompt_text=prompt_text,
                                                         model=active_model, effort=active_effort,
                                                         remote_dir=ctx.remote_dir, **extra))
            answer["call_id"] = call_id
        elapsed += float(answer.get("elapsed_s") or 0.0)
        reqs += 1 if cached else max(1, answer.get("attempts") or 1)
        if not _cpa_ok(answer):
            chosen = answer          # the call itself failed: the provider's own retry already ran
            break
        problem = _answer_problem(answer.get("response_text", ""), accepts)
        if not problem:
            chosen = answer
            if not cached:
                _cache_cpa_answer(active_cache, answer)
            break
        _write_atomic(_local(ctx, f"{active_rel}.rejected-{attempt}.completion.txt"), answer.get("response_text", ""))
        rejected.append({"attempt": attempt, "reason": problem,
                         "chars": len(answer.get("response_text", "")), "usage": answer.get("usage")})
        if cached:
            Path(active_cache).unlink(missing_ok=True)   # void: no rerun may read this answer again
    if chosen is None:
        last = rejected[-1] if rejected else {"reason": "CPA_ANSWER_INCOMPLETE", "chars": 0}
        why = last["reason"].split(" (", 1)[0]
        chosen = {"call_id": call_id, "model": active_model, "effort": active_effort, "stream": True,
                  "rc": 1, "complete": False, "reason": why, "elapsed_s": round(elapsed, 1),
                  "response_text": "", "rejected_answers": rejected,
                  "stderr_tail": (f"CPA_ANSWER_INCOMPLETE: {len(rejected)} answers in a row were not whole "
                                  f"(last: {last['chars']} chars, {last['reason']}); "
                                  f"originals under {active_rel}.rejected-*.completion.txt")}
    elif rejected:
        chosen = cast(CpaEvent, dict(chosen, rejected_answers=rejected))
    return chosen, active_cache, elapsed, reqs


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
    record, cache, spent, requests = _ask_cpa_model(
        ctx, call_id, prompt_text, active_model=model, active_effort=effort, run=run,
        max_output_tokens=max_output_tokens, accepts=accepts, max_attempts=max_attempts,
        transport_tries=transport_tries,
    )
    fallback = _cpa_fallback_of(model, effort) if _is_quota_refused(record) else None
    if fallback is not None:
        orig_model = model
        orig_reason = str(record.get("reason") or "CPA_QUOTA_REFUSED")
        fb_model, fb_effort, fb_min_tok = fallback
        fb_max_tokens = max(max_output_tokens or 0, fb_min_tok) if fb_min_tok else max_output_tokens
        fb_record, fb_cache, fb_spent, fb_requests = _ask_cpa_model(
            ctx, call_id, prompt_text, active_model=fb_model, active_effort=fb_effort, run=run,
            max_output_tokens=fb_max_tokens, accepts=accepts, max_attempts=max_attempts,
            transport_tries=transport_tries, suffix="--fallback",
        )
        spent += fb_spent
        requests += fb_requests
        model, effort, max_output_tokens, cache = fb_model, fb_effort, fb_max_tokens, fb_cache
        fb_record["elapsed_s"] = round(spent, 1)
        fb_record["fallback_from"] = orig_model
        fb_record["fallback_reason"] = orig_reason
        if _cpa_ok(fb_record):
            fb_record["fallback_note"] = f"原模型 {orig_model} 额度不足（{orig_reason}），改用 {fb_model}"
        else:
            fb_why = failure_line(fb_record)
            dual = f"CPA_FALLBACK_FAILED: 原模型 {orig_model} 额度不足（{orig_reason}）；回退模型 {fb_model} 也不可用（{fb_why}）"
            fb_record["fallback_note"] = f"原模型 {orig_model} 额度不足（{orig_reason}），改用 {fb_model} 仍失败（{fb_why}）"
            fb_record["reason"] = dual
            fb_record["stderr_tail"] = f"{dual}\n{fb_record.get('stderr_tail') or ''}".strip()[:800]
        record = fb_record
    # the model and tier this call was actually answered at (a cached answer kept its own model/tier)
    record["model"] = model
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
             cpa_calls: list[CpaEvent], goes_on: bool, unparseable: str = "CPA_RESPONSE_UNPARSEABLE") -> tuple[Any, str]:
    """One model call whose answer must be a JSON value `problem` has nothing against (`problem` returns "" for a usable
    value, else what is wrong with it).  Returns (value, "") or (None, why there is no value).  An answer outside the
    contract is asked again while the tries last (the `accepts` of _ask_cpa; the failure then begins with `shape`).  The
    call's record is appended to cpa_calls whether or not it gave a value.

    `goes_on` (no default: every caller says it): True when the step carries on without the answer.  The record is then
    marked `tolerated`, so a later stop of the step for a reason of its own (step 6's TITLE_NOT_ACCEPTED) is not read by
    auto_run.provider_failed as waiting for the provider: a clip stopped that way was run again every round and never
    listed as stuck (review)."""
    record = _ask_cpa(ctx, call_id, prompt_text, purpose=purpose,
                      accepts=lambda value: f"{shape} ({found})" if (found := problem(value)) else "")
    logged = _log(record)
    if goes_on:
        logged["tolerated"] = True
    cpa_calls.append(logged)
    if not _cpa_ok(record):
        return None, failure_line(record)
    try:
        value = _json_from_text(record["response_text"])
    except ValueError as exc:
        return None, f"{unparseable} ({str(exc)[:160]})"
    found = problem(value)
    return (None, found) if found else (value, "")


def json_asker(ctx: Ctx, calls: list[CpaEvent], *, purpose: str = "title") -> Callable[[str, str], dict[str, Any] | None]:
    """A CPA ask for a step's helpers (collab_guests, guest_identity): ask(call_id, prompt) gives the parsed JSON object,
    None when the call or its answer failed; each call's record is appended to `calls`."""
    def ask(call_id: str, prompt: str) -> dict[str, Any] | None:
        record = _ask_cpa(ctx, call_id, prompt, purpose=purpose)
        calls.append(_log(record))
        if not _cpa_ok(record):
            return None
        try:
            answer = _json_from_text(record["response_text"])
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None
    return ask


def _principle_section(heading: str, what: str) -> str:
    path = load_channel_profile(PKG_ROOT).asset_file("subtitle_correction_principles")
    text = path.read_text(encoding="utf-8")
    _, found, section = text.partition(heading)
    body, end, _ = section.partition("\n## ")
    if not found or not end or not body.strip() or heading in section:
        raise ValueError(f"{path}: missing or invalid {what} section")
    return heading + body.rstrip()


def _pronoun_rule() -> str:
    """The channel's pronoun section, then the people whose pronoun the channel knowledge graph records (accepted
    nodes with `pronoun`): one fact for every writer of the subtitles (agent, adjudication, review)."""
    rule = _principle_section("## 六、代词与指代对象一致", "pronoun")
    root = load_channel_profile(PKG_ROOT).asset_root
    if not (root / NODES_FILE).is_file():
        return rule
    known = [n for n in load_graph(root).nodes.values() if n.status == "accepted" and n.pronoun]
    if not known:
        return rule
    rows = "；".join(f"{n.names['display']}" + (f"（{'、'.join(n.aliases)}{'；' + n.summary if n.summary else ''}）"
                                              if n.aliases or n.summary else "") + f"→{n.pronoun}" for n in known)
    return f"{rule}\n- **知识图记了代词的人，指的是这位时按图写（优先于上面的默认）**：{rows}"


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


def _mark_unparseable_rejected(record: Mapping[str, object]) -> None:
    """Record a CPA answer refused after rc=0 as `.rejected-N.completion.txt` so the next run advances reply_id."""
    if record.get("rc") != 0 or not record.get("file") or not record.get("call_id"):
        return
    base = f"{record['call_id']}--fallback" if record.get("fallback_from") else str(record["call_id"])
    prefix = Path(str(record["file"]))
    if not prefix.parent.is_dir():
        return
    text = record.get("response_text")
    if text is None:
        comp = prefix.with_name(f"{prefix.name}.completion.txt")
        text = comp.read_text(encoding="utf-8") if comp.is_file() else ""
    before = len(list(prefix.parent.glob(f"{base}.rejected-*.completion.txt")))
    _write_atomic(prefix.parent / f"{base}.rejected-{before + 1}.completion.txt", str(text))


def _fail(reason: str, note: str, *, cpa: Sequence[CpaEvent] = (), process: Sequence[str] = ()) -> StepOutcome:
    if reason == "CPA_RESPONSE_UNPARSEABLE" and cpa and isinstance(cpa[-1], Mapping):
        # an answer the step cannot read is not kept as the answer to that prompt: the rerun asks again (TASK-11:
        # a truncated adjudication was cached and every rerun stopped on it), and `.rejected-*` advances reply_id
        if cpa[-1].get("cache_path"):
            Path(str(cpa[-1]["cache_path"])).unlink(missing_ok=True)
        _mark_unparseable_rejected(cpa[-1])
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


class CpaVisionUnavailable(Exception):
    """Raised when both primary and quota-fallback vision routes fail (never swallowed as a per-frame miss)."""


def vision_fatal_detail(text: str) -> str:
    for line in reversed(text.splitlines()):
        for marker in ("CPA_FALLBACK_FAILED:", "CPA_QUOTA_REFUSED:"):
            if marker in line:
                return line[line.index(marker):].strip()[:400]
    return ""


def raise_if_vision_unavailable(text: str) -> None:
    if detail := vision_fatal_detail(text):
        raise CpaVisionUnavailable(detail)


def fallback_judged_purposes(state: Any) -> list[str]:
    """Judge purposes (`lane_rules` `cpa.judge_purposes`: title-judge, review) answered by a quota-fallback model."""
    try:
        judges = set(rules("cpa").get("judge_purposes") or ("title-judge", "review"))
    except KeyError:
        judges = {"title-judge", "review"}
    steps = state.data.get("steps") or {}
    events = [call for step in steps.values() if isinstance(step, Mapping) for call in (step.get("cpa") or [])]
    events.extend(((state.data.get("review") or {}).get("cpa")) or [])
    seen: list[str] = []
    for call in events:
        if not isinstance(call, Mapping):
            continue
        purpose, model = str(call.get("purpose") or ""), str(call.get("model") or "")
        if purpose in judges and call.get("fallback_from") and call.get("rc") == 0 and model:
            label = f"{purpose}→{model}"
            if label not in seen:
                seen.append(label)
    return seen


def merge_judge_fallbacks(base: Mapping[str, Any] | None, *others: Mapping[str, Any] | None) -> list[str]:
    merged: list[str] = []
    for item in (base, *others):
        for model in (item.get("fallback_models") or [] if isinstance(item, Mapping) else []):
            if str(model) and str(model) not in merged:
                merged.append(str(model))
    return merged


def review_effective_model(cpa: Sequence[Mapping[str, Any]]) -> str | None:
    review_calls = [c for c in cpa if c.get("rc") == 0 and c.get("model")
                    and (c.get("purpose") == "review" or str(c.get("call_id") or "").startswith("review"))]
    fb = next((str(c["model"]) for c in review_calls if c.get("fallback_from")), None)
    return fb or next((str(c["model"]) for c in review_calls if c.get("call_id") == "review"), None)


def review_fallback_lines(cpa: Sequence[Mapping[str, Any]]) -> list[str]:
    notes: list[str] = []
    for c in cpa:
        if c.get("rc") == 0 and c.get("fallback_from") and c.get("fallback_note") and (
                c.get("purpose") == "review" or str(c.get("call_id") or "").startswith("review")):
            line = f"> 回退模型复审说明（`{c.get('call_id')}`）：{c['fallback_note']}（待人工抽查）"
            if line not in notes:
                notes.append(line)
    return [*notes, ""] if notes else []


def vision_cpa_event(call_id: str, call: Mapping[str, Any], default_model: str | None = None) -> CpaEvent:
    ev: dict[str, Any] = {"call_id": call_id, "kind": "vision", "model": call.get("model", default_model),
                          "rc": call.get("rc"), "elapsed_s": call.get("elapsed_s"), "usage": call.get("usage")}
    for key in ("fallback_from", "fallback_reason", "fallback_note"):
        if call.get(key):
            ev[key] = call[key]
    return cast(CpaEvent, ev)


def focused_route_valid(ctx: Ctx, event: Mapping[str, Any], prompt: str, cache_path: str,
                        primary_model: str, effort: str) -> bool:
    orig_model = str(event.get("fallback_from") or event.get("model") or "")
    expected = {
        str(_cpa_cache_path(ctx, prompt, effort=effort, model=primary_model)),
        str(_cpa_cache_path(ctx, prompt, effort=str(event.get("effort") or effort),
                            model=str(event.get("model") or primary_model),
                            max_output_tokens=cast("int | None", event.get("max_output_tokens")))),
    }
    return (orig_model == primary_model and (bool(event.get("fallback_from")) or event.get("effort") == effort)
            and event.get("stream") is True and cache_path in expected)


def judge_run_fallback(cid: str, record: CpaEvent, fallback_models: list[str]) -> dict[str, object]:
    extra: dict[str, object] = {"call_id": cid}
    if record.get("fallback_from") and record.get("model"):
        extra.update(model=record.get("model"), fallback_from=record["fallback_from"])
        if str(record["model"]) not in fallback_models:
            fallback_models.append(str(record["model"]))
    return extra
