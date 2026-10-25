"""Strict aggregate pronoun review and private per-invocation diagnostics.

Preserve the actual prompt, each returned completion, and before/after SRT in a
separate owner-only run file. Ordinary success reuse keeps the raw answer in
the existing transcription cache; parsing still owns edits and errors.
"""
from __future__ import annotations

from src.autoslice.structured_output import call_json

from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from src.autoslice.transcription_stage_cache import _directory, _write

SCHEMA = "aggregate-pronoun-stage-trace.v1"
_TEXT_LIMIT = 256_000
_LOG = logging.getLogger(__name__)


def _retained_text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"retention": "NON_TEXT", "type": type(value).__name__}
    # Transport errors are never retained verbatim. A provider credential echo
    # is also omitted BEFORE hashing, so diagnostics cannot become a key store.
    credential = os.environ.get("CPA_API_KEY")
    if credential and credential in value:
        return {"retention": "OMITTED_CREDENTIAL_ECHO"}
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return {"retention": "OMITTED_INVALID_UNICODE"}
    result = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if len(raw) > _TEXT_LIMIT:
        return {**result, "retention": "OMITTED_SIZE"}
    return {**result, "retention": "RETAINED", "text": value}


def _model_identity(call: Callable) -> dict[str, Any]:
    declared = getattr(call, "cpa_cache_identity", None)
    if not isinstance(declared, dict):
        return {"status": "UNKNOWN"}
    # Do not copy command templates, child environments, endpoint URLs or keys.
    identity = {key: declared[key] for key in ("transport", "models", "effort") if key in declared}
    credential = os.environ.get("CPA_API_KEY")
    if credential and credential in str(identity):
        return {"status": "OMITTED_CREDENTIAL_ECHO"}
    if not (
        isinstance(identity.get("transport"), str)
        and isinstance(identity.get("models"), list)
        and all(isinstance(model, str) for model in identity["models"])
        and isinstance(identity.get("effort"), str)
    ):
        return {"status": "UNKNOWN"}
    return identity


def apply_pronoun_pass(
    srt: str, *, cpa_llm_call, display_name: str, required: bool = False,
    context_text: str = "", resume=None,
):
    """Resolve singular pronouns after every other text correction.

    This is deliberately the last text pass and works in both directions:
    existing ``TA`` can become 她/他/它 when the whole clip establishes the
    referent, while an unjustified 他/她 can become TA.  The model only returns
    occurrence-level edits; code applies them to the original cue text so the
    timeline and all non-pronoun wording remain structurally immutable.
    """

    import re

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.llm_client import LlmCallError, LlmJsonParseError, extract_json_object
    from src.autoslice.subtitle_draft_preparation import _asr_ts

    # Singular candidate tokens only.  Do not match 其他/他们/她们/它们.
    pron = re.compile(r"(?<![A-Za-z0-9_])TA(?![A-Za-z0-9_们])|(?<!其)[他她它](?!们)")
    cues = parse_srt_cues(srt)
    if not cues:
        return srt
    occurrences: dict[int, list[re.Match[str]]] = {
        i: list(pron.finditer(c.text)) for i, c in enumerate(cues, start=1)
    }
    occurrences = {i: matches for i, matches in occurrences.items() if matches}
    if not occurrences:
        return srt  # cheap gate: no personal pronoun to resolve

    numbered = "\n".join(f"{i}. {c.text}" for i, c in enumerate(cues, start=1))
    candidate_lines = []
    for cue_no, matches in occurrences.items():
        candidate_lines.append(
            f"{cue_no}: " + ", ".join(
                f"occurrence={position} token={match.group(0)}"
                for position, match in enumerate(matches, start=1)
            )
        )
    prompt = (
        f"你在给{display_name}(B站虚拟主播)切片字幕做最终定稿代词。通读整条切片，逐个判断候选代词的实际指代。\n"
 f"硬规则：已知女性用‘她’（{display_name}、礼墨Sumi、安晚Awa及其他已知女主播均如此）；已知男性用‘他’；动物/物体用‘它’；"
 "只有人的性别确实无法从全文、姓名或常识判断时才用‘TA’。不能因为草稿已经写成TA就跳过。\n"
 "同一指代对象全片必须用同一个字：已知女性（含语境确认为女性的对话参与者）一律‘她’；‘TA’只给全片都判断不出性别的对象，"
 "且该对象每一处都用‘TA’，不能同一个人时而‘她’时而‘TA’；指代弹幕、发弹幕的人或观众群体时一律用‘他’（公开规则），不用‘TA’；改写时按整条切片统一，不要只改一处。\n"
 "每个候选按 cue 编号和 occurrence(该 cue 内从左到右第几个候选)定位。只列真正需要改变的项；from 必须照抄候选 token。"
 "不要重写整句，也不要修改复数代词。\n"
 f"\n候选:\n" + "\n".join(candidate_lines) + "\n"
        f"\n字幕:\n{numbered}\n"
        '\n只输出 JSON（to 只能是 TA/他/她/它）:'
        '{"rewrites":[{"n":1,"occurrence":1,"from":"TA","to":"她"}]}'
    )
    if context_text:
        prompt += "\n\n" + context_text + "\n"

    def fetch_completion():
        # Keep the original three shape/transport attempts. Occurrence errors
        # still fail outside this loop after the first complete shaped answer.
        failure_code = "CPA_PRONOUN_INVALID_OUTPUT"
        for _attempt in range(3):
            try:
                completion = call_json(cpa_llm_call, prompt)
                payload = extract_json_object(completion)
                if not isinstance(payload["rewrites"], list):
                    raise ValueError("rewrites must be a list")
                return completion
            except Exception as exc:
                transport_error = isinstance(exc, LlmCallError) and not isinstance(exc, LlmJsonParseError)
                failure_code = "CPA_PRONOUN_UNAVAILABLE" if transport_error else "CPA_PRONOUN_INVALID_OUTPUT"
        if required:
            from src.autoslice.source_context_executor import AgyRunnerError
            raise AgyRunnerError(failure_code, "CPA pronoun review did not complete")
        return None

    def apply_completion(completion):
        if completion is None:
            return srt
        rewrites = extract_json_object(completion)["rewrites"]
        if not isinstance(rewrites, list):
            raise ValueError("rewrites must be a list")
        if not rewrites:
            return srt
        allowed = {"TA", "他", "她", "它"}
        by_cue: dict[int, list[tuple[int, int, str]]] = {}
        seen: set[tuple[int, int]] = set()
        for item in rewrites:
            try:
                if required and (
                    not isinstance(item, dict) or type(item.get("n")) is not int
                    or type(item.get("occurrence")) is not int or item["occurrence"] < 1
                ):
                    raise ValueError("invalid occurrence")
                cue_no = int(item["n"])
                occurrence = int(item["occurrence"])
                source = str(item["from"])
                target = str(item["to"])
                match = occurrences[cue_no][occurrence - 1]
            except (KeyError, IndexError, TypeError, ValueError):
                if required:
                    from src.autoslice.source_context_executor import AgyRunnerError
                    raise AgyRunnerError("CPA_PRONOUN_INVALID_OUTPUT", "CPA returned an invalid occurrence") from None
                continue
            key = (cue_no, occurrence)
            if key in seen or source not in allowed or target not in allowed or match.group(0) != source:
                if required:
                    from src.autoslice.source_context_executor import AgyRunnerError
                    raise AgyRunnerError("CPA_PRONOUN_INVALID_OUTPUT", "CPA returned a stale or conflicting occurrence")
                continue
            seen.add(key)
            if source != target:
                by_cue.setdefault(cue_no, []).append((match.start(), match.end(), target))
        blocks = []
        for index, cue in enumerate(cues, start=1):
            text = cue.text
            for start, end, target in sorted(by_cue.get(index, []), reverse=True):
                text = text[:start] + target + text[end:]
            blocks.append(f"{index}\n{_asr_ts(cue.start_ms)} --> {_asr_ts(cue.end_ms)}\n{text}")
        return "\n\n".join(blocks) + "\n"

    if resume is None or not required:
        return apply_completion(fetch_completion())
    return resume.pronoun(
        prompt=prompt, source_srt=srt, llm_call=cpa_llm_call,
        fetch=fetch_completion, validate=apply_completion,
        implementation=(apply_pronoun_pass, parse_srt_cues, extract_json_object, _asr_ts),
        retain=lambda value: _retained_text(value).get("retention") == "RETAINED",
    )


def trace_pronoun_pass(
    srt: str, *, media_path: Path, llm_call: Callable[[str], str],
    run: Callable[..., str], required: bool, context_text: str = "", resume=None,
) -> str:
    """Execute once, checkpoint its own calls, and return/raise exactly as before.

    Checkpoints only replace this invocation's newly created trace. Prior runs
    stay immutable. This is optional observability: an unsafe/unwritable sink is
    disclosed, not turned into a subtitle quality failure or a reason to retry.
    """
    started = time.monotonic()
    trace: dict[str, Any] = {
        "schema_version": SCHEMA,
        "trace_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "AGGREGATE_PRONOUN_TEXT_PASS_DIAGNOSTIC",
        "outcome": "RUNNING",
        "required": required,
        "model_identity": _model_identity(llm_call),
        "model_identity_source": "CALLABLE_DECLARED_REQUEST_CONFIGURATION",
        "backend_identity_verified": False,
        "input_srt": _retained_text(srt),
        "calls": [],
        "served_from_cache": False,
        "release_authorized": False,
    }
    path: Path | None = None
    warned = False

    def unavailable() -> None:
        nonlocal warned, path
        path = None
        if not warned:
            _LOG.warning("PRONOUN_STAGE_TRACE_UNAVAILABLE: diagnostic sink was not safely persisted")
            warned = True

    try:
        folder = Path(media_path).absolute().with_suffix(".pronoun-trace")
        _directory(folder)
        if folder.lstat().st_mode & 0o077:
            raise OSError("diagnostic directory must be private")
        candidate = folder / (trace["trace_id"] + ".json")
        if _write(candidate, trace):
            path = candidate
        else:
            unavailable()
    except (OSError, ValueError, TypeError):
        unavailable()

    def checkpoint() -> None:
        if path is None:
            return
        try:
            persisted = _write(path, trace, replace=True)
        except (OSError, ValueError, TypeError):
            persisted = False
        if not persisted:
            unavailable()

    def observed_call(prompt: str) -> str:
        call_started = time.monotonic()
        row = {"logical_call": len(trace["calls"]) + 1,
               "outcome": "STARTED", "prompt": _retained_text(prompt)}
        trace["calls"].append(row)
        checkpoint()  # Successful checkpointing preserves intent before dispatch.
        try:
            completion = llm_call(prompt)
        except BaseException as exc:
            row.update(outcome="RAISED", exception_type=type(exc).__name__,
                       elapsed_seconds=time.monotonic() - call_started)
            checkpoint()
            raise
        row.update(outcome="RETURNED", completion=_retained_text(completion),
                   elapsed_seconds=time.monotonic() - call_started)
        checkpoint()
        return completion

    # Preserve declared call identity without changing how the existing parser
    # or provider wrapper behaves. The strict pass owns success-cache validation.
    observed_call.cpa_cache_identity = getattr(llm_call, "cpa_cache_identity", None)
    from .completed_review_checkpoint import inherit_ruling_context
    inherit_ruling_context(observed_call, llm_call)
    try:
        options = {"cpa_llm_call": observed_call, "required": required}
        if context_text:
            options["context_text"] = context_text
        if resume is not None:
            options["resume"] = resume
        output = run(srt, **options)
    except BaseException as exc:
        trace.update(outcome="RAISED", exception_type=type(exc).__name__,
                     elapsed_seconds=time.monotonic() - started)
        checkpoint()
        raise
    if resume is not None and isinstance(resume.audit.get("pronoun"), dict):
        trace["cache"] = dict(resume.audit["pronoun"])
        trace["served_from_cache"] = trace["cache"].get("served_from_cache") is True
    trace.update(outcome="RETURNED", output_srt=_retained_text(output),
                 elapsed_seconds=time.monotonic() - started)
    checkpoint()
    return output
