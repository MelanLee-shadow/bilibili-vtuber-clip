"""Whole-conversation speaker context and singleton resolution."""

from __future__ import annotations

import math
import os
import re
import shlex
import statistics
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.llm_client import extract_json_object
from src.autoslice.speaker_common import (
    CHANNEL_PROFILE,
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE,
    SINGLETON_NONLEXICAL_RESIDUALS,
    SPEAKERS,
    SpeakerFinalizationError,
    SpeakerIdentityIndeterminate,
    milliseconds as _ms,
)
from src.autoslice.speaker_host_evidence import resolve_ambiguous_cue_speaker

def _two_means(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) < 2:
        raise SpeakerIdentityIndeterminate("not enough cue margins for two-speaker clustering")
    low, high = min(values), max(values)
    if low == high:
        raise SpeakerIdentityIndeterminate("speaker margin distribution has no separation")
    for _ in range(40):
        high_side = [value for value in values if abs(value - high) < abs(value - low)]
        low_side = [value for value in values if abs(value - high) >= abs(value - low)]
        if not high_side or not low_side:
            break
        next_low = statistics.mean(low_side)
        next_high = statistics.mean(high_side)
        if abs(next_low - low) < 1e-8 and abs(next_high - high) < 1e-8:
            low, high = next_low, next_high
            break
        low, high = next_low, next_high
    if low > high:
        low, high = high, low
    return float(low), float(high), float((low + high) / 2)


def resolve_ambiguous_labels(
    labels: Sequence[str | None],
    margins: Sequence[float],
    threshold: float,
    context_votes: Mapping[int, str] | None = None,
    *,
    band: float,
    policy: Mapping[str, object],
    context_confidences: Mapping[int, float] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve ambiguous cue labels under the asymmetric host-evidence policy.

    Default is GUEST (维护者).  HOST requires a confident acoustic
    margin or a semantic vote on the HOST-leaning half of the narrow band with
    sufficient confidence; see ``speaker_host_evidence.resolve_ambiguous_cue_
    speaker``.  The whole-clip context judge can never assign HOST on its own
    outside that half-band -- it only ever confirms GUEST or is ignored.
    ``context_votes``/``context_confidences`` use zero-based cue indices.
    """

    if len(labels) != len(margins):
        raise ValueError("labels and margins must have equal length")
    votes = context_votes or {}
    confidences = context_confidences or {}
    result = list(labels)
    sources = ["campp_audio" if label is not None else "unresolved" for label in labels]
    for index, label in enumerate(result):
        if label is not None:
            continue
        decision = resolve_ambiguous_cue_speaker(
            margin=margins[index],
            threshold=threshold,
            band=band,
            policy=policy,
            context_speaker=votes.get(index),
            context_confidence=confidences.get(index),
        )
        result[index] = decision.speaker
        sources[index] = decision.decision_source
    return [str(label) for label in result], sources


def _reviewed_context_votes(
    override_document: Mapping[str, object],
    *,
    cue_count: int,
) -> dict[int, str]:
    """Load hash-bound, human-accepted context votes from an override asset.

    The JSON uses one-based cue numbers; the analyzer uses zero-based indices.
    These votes stabilize only an already reviewed clip.  New clips continue to
    use the normal whole-clip context judge.
    """

    raw = override_document.get("reviewed_context_votes")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpeakerFinalizationError("reviewed_context_votes must be an object")
    labels = raw.get("labels")
    if not isinstance(labels, Mapping) or not labels:
        raise SpeakerFinalizationError("reviewed_context_votes.labels must be a non-empty object")
    if not str(raw.get("authority") or "").strip():
        raise SpeakerFinalizationError("reviewed_context_votes.authority must be non-empty")
    expected_source = str(override_document.get("source_srt_sha256") or "")
    bound_source = str(raw.get("source_automatic_srt_sha256") or "")
    if not expected_source or not bound_source:
        raise SpeakerFinalizationError(
            "reviewed context votes require source_srt_sha256 and source_automatic_srt_sha256"
        )
    if expected_source != bound_source:
        raise SpeakerFinalizationError("reviewed context vote source hash does not match source_srt_sha256")

    votes: dict[int, str] = {}
    for cue_number_raw, speaker_raw in labels.items():
        try:
            cue_number = int(str(cue_number_raw))
        except ValueError as exc:
            raise SpeakerFinalizationError(
                f"reviewed context cue number is invalid: {cue_number_raw!r}"
            ) from exc
        speaker = str(speaker_raw)
        if not 1 <= cue_number <= cue_count:
            raise SpeakerFinalizationError(f"reviewed context cue is out of range: {cue_number}")
        if speaker not in SPEAKERS:
            raise SpeakerFinalizationError(
                f"reviewed context speaker is invalid for cue {cue_number}: {speaker!r}"
            )
        votes[cue_number - 1] = speaker
    return votes


_SINGLETON_PUNCT_RX = re.compile(r"[\s，。！？!?、,.…~～—\-]+")
_SINGLETON_LAUGHTER_RX = re.compile(r"(?:哈{2,}|嘿{2,}|呵{2,}|嘻{2,}|(?:ha){2,}|笑死|笑)", re.IGNORECASE)
_SINGLETON_INTERJECTION_RX = re.compile(r"(?:啊|呀|哎|唉|诶|欸|嗯|呃|额|哦|噢|哼|嘛|呢|吧)+")


def _singleton_nonlexical_dominant(text: str) -> bool:
    compact = _SINGLETON_PUNCT_RX.sub("", str(text)).lower()
    without_laughter, laughter_count = _SINGLETON_LAUGHTER_RX.subn("", compact)
    residual, interjection_count = _SINGLETON_INTERJECTION_RX.subn("", without_laughter)
    return bool(laughter_count or interjection_count) and residual in SINGLETON_NONLEXICAL_RESIDUALS


def _resolve_singleton_outlier(
    *,
    cues: Sequence[TextCue],
    singleton_index: int,
    seed_scores: Sequence[float],
    host_bank_scores: Mapping[int, float],
    clip_host_indices: Sequence[int],
    policy: Mapping[str, float | int],
    cue_audio_sha256: Sequence[str],
    context_call: Callable[[str], str] | None,
    reviewed_context_votes: Mapping[int, str] | None = None,
) -> dict[str, object]:
    """Resolve one low outlier through the existing whole-clip context judge."""

    host_min = float(policy["single_host_median_seed_min"])
    bank_min = float(policy["guest_session_similarity_max"])

    def cue_evidence(index: int) -> dict[str, object]:
        return {
            "source_cue": index + 1,
            "zero_based_index": index,
            "start": cues[index].start,
            "end": cues[index].end,
            "text": cues[index].text,
            "seed_score": round(float(seed_scores[index]), 8),
            "host_bank_score": round(float(host_bank_scores[index]), 8),
            "audio_sha256": cue_audio_sha256[index],
        }

    neighbours = [
        {
            **cue_evidence(index),
            "host_supported": seed_scores[index] >= host_min
            and host_bank_scores[index] >= bank_min,
        }
        for index in (singleton_index - 1, singleton_index + 1)
        if 0 <= index < len(cues)
    ]
    gates = {
        "nonlexical_dominant": _singleton_nonlexical_dominant(cues[singleton_index].text),
        "strong_host_majority": statistics.median(seed_scores) >= host_min,
        "strong_host_anchors": len(clip_host_indices)
        >= int(policy["host_session_anchor_count"]),
        "adjacent_host": len(neighbours) == 2
        and all(bool(row["host_supported"]) for row in neighbours),
    }
    evidence = {
        **cue_evidence(singleton_index),
        "duration_ms": _ms(cues[singleton_index].end) - _ms(cues[singleton_index].start),
        "nonlexical_dominant": gates["nonlexical_dominant"],
        "clip_median_seed_score": round(float(statistics.median(seed_scores)), 8),
        "strong_host_anchor_cues": [index + 1 for index in clip_host_indices],
        "neighbours": neighbours,
    }
    reviewed = (reviewed_context_votes or {}).get(singleton_index)
    labels: list[str | None] = [HOST_SPEAKER] * len(cues)
    labels[singleton_index] = None
    context_votes, context_attempts, context_errors = _whole_clip_context_votes(
        cues,
        labels,
        [singleton_index],
        context_call,
        initial_speakers=(
            {singleton_index: str(reviewed)} if reviewed in SPEAKERS else None
        ),
        allow_review=True,
        require_confidence=True,
    )
    context_decision = context_votes.get(singleton_index)

    reviewed_ready = reviewed in SPEAKERS
    automatic_host_ready = bool(
        all(gates.values())
        and context_decision
        and context_decision.get("speaker") == HOST_SPEAKER
        and float(context_decision.get("confidence") or 0.0)
        >= SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE
    )
    gate_failures = {
        "nonlexical_dominant": "SINGLETON_LEXICAL_CONTENT",
        "strong_host_majority": "HOST_MAJORITY_INSUFFICIENT",
        "strong_host_anchors": "HOST_ANCHORS_INSUFFICIENT",
        "adjacent_host": "NEIGHBOR_HOST_EVIDENCE_INCOMPLETE",
    }
    reason_codes = [] if reviewed_ready else [
        gate_failures[name] for name, passed in gates.items() if not passed
    ]
    if not reviewed_ready:
        if context_decision is None:
            reason_codes.append("CONTEXT_INCOMPLETE")
        elif context_decision["speaker"] == GUEST_SPEAKER:
            reason_codes.append("CONTEXT_GUEST")
        elif context_decision["speaker"] == "REVIEW":
            reason_codes.append("CONTEXT_REVIEW")
        elif float(context_decision["confidence"]) < SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE:
            reason_codes.append("CONTEXT_HOST_CONFIDENCE_LOW")
        if (
            context_decision
            and context_decision["speaker"] == HOST_SPEAKER
            and not all(gates.values())
        ):
            reason_codes.append("CONTEXT_ACOUSTIC_CONFLICT")
    # 维护者: semantics alone must never assign HOST.  A context
    # vote of HOST without automatic_host_ready (acoustic gates + confidence)
    # is corroboration that failed, not evidence -- it falls through to the
    # GUEST default just like every other unresolved case, it does not adopt
    # the LLM's HOST guess.
    singleton_speaker = (
        str(reviewed) if reviewed_ready else HOST_SPEAKER if automatic_host_ready else GUEST_SPEAKER
    )
    singleton_source = (
        "accepted_context_baseline"
        if reviewed_ready
        else "whole_clip_context_singleton"
        if automatic_host_ready
        else "speaker_review_required_singleton"
    )
    review_required = not (reviewed_ready or automatic_host_ready)
    evidence.update(
        {
            "gates": gates,
            "context_decision": context_decision,
            "context_errors": context_errors,
            "review_reason_codes": reason_codes,
        }
    )
    return {
        "mode": "singleton_outlier",
        "multi_speaker_detected": singleton_speaker == GUEST_SPEAKER and not review_required,
        "context_attempts": context_attempts,
        "context_errors": context_errors,
        "context_required_cues": [singleton_index + 1],
        "context_unresolved_cues": [singleton_index + 1] if review_required else [],
        "review_required": review_required,
        "review_reason_codes": reason_codes,
        "singleton_evidence": [evidence],
        "decisions": [
            {
                "source_index": index + 1,
                "speaker": singleton_speaker if index == singleton_index else HOST_SPEAKER,
                "decision_source": singleton_source if index == singleton_index else "campp_single_host_majority",
                "seed_score": round(float(seed_scores[index]), 8),
                "host_score": round(float(host_bank_scores[index]), 8),
                "guest_score": None,
                "margin": None,
            }
            for index in range(len(cues))
        ],
    }


def _context_prompt(cues: Sequence[TextCue], labels: Sequence[str | None], ambiguous: Sequence[int]) -> str:
    rows = [
        f"{index}. [{label or '待定'}] {cue.text}"
        for index, (cue, label) in enumerate(zip(cues, labels, strict=True), start=1)
    ]
    return (
        f"这是{HOST_SPEAKER}（直播间主人）与{GUEST_SPEAKER}主播的完整切片字幕，文本、专名和代词已经最终定稿。"
        f"大部分行已经由声纹标为[{HOST_SPEAKER}]/[{GUEST_SPEAKER}]；只有[待定]行因太短或处于声纹分界带，需要根据整段问答、称呼方向和上下文判断。\n"
        f"规则：别人评价{HOST_SPEAKER}后，她的反问/自辩通常是{HOST_SPEAKER}；对{HOST_SPEAKER}使用第三人称评价的通常是{GUEST_SPEAKER}；"
        f"对话中作为名字出现的精确词 {CHANNEL_PROFILE.speaker_identity_aliases[-1]} 是{HOST_SPEAKER}的自称之一，不是第四位说话人或{GUEST_SPEAKER}嘉宾；"
        "不要修改文字，不要把相邻两个人的连续短句合成同一说话人。"
        "单个声纹离群点不能独立建立嘉宾簇；若上下文仍可能是真实嘉宾、证据冲突或无法确定，返回 REVIEW。\n"
        f"你的判断只在声学证据处于临界带时才会被采纳为{HOST_SPEAKER}；声学证据缺席或明显偏向"
        f"{GUEST_SPEAKER}时，即使你判断为{HOST_SPEAKER}也不会被采纳，默认仍是{GUEST_SPEAKER}——"
        "语义只能佐证，不能单独定案，请如实给出你的判断和置信度而不必迎合这条规则。\n"
        f"待定行号（1-based）：{[index + 1 for index in ambiguous]}\n\n"
        + "\n".join(rows)
        + f'\n\n只输出 JSON：{{"labels":[{{"n":1,"speaker":"{HOST_SPEAKER}","confidence":0.95,'
        f'"reason":"具体上下文依据"}}]}}，且只列待定行。speaker 只能是{HOST_SPEAKER}、{GUEST_SPEAKER}或 REVIEW；confidence 为 0..1。'
    )


def _whole_clip_context_votes(
    cues: Sequence[TextCue],
    labels: Sequence[str | None],
    ambiguous: Sequence[int],
    context_call: Callable[[str], str] | None,
    *,
    initial_speakers: Mapping[int, str] | None = None,
    allow_review: bool = False,
    require_confidence: bool = False,
) -> tuple[dict[int, dict[str, object]], int, list[str]]:
    """Use the one whole-clip judge/retry/schema path for all ambiguous cues."""

    votes = {
        index: {
            "n": index + 1,
            "speaker": speaker,
            "confidence": 1.0,
            "reason": "hash-bound reviewed context vote",
            "source": "hash_bound_reviewed_context",
        }
        for index, speaker in (initial_speakers or {}).items()
        if index in ambiguous and speaker in SPEAKERS
    }
    attempts = 0
    errors: list[str] = []
    if context_call is None:
        return votes, attempts, errors
    allowed = SPEAKERS | ({"REVIEW"} if allow_review else set())
    for _attempt in range(3):
        pending = [index for index in ambiguous if index not in votes]
        if not pending:
            break
        attempts += 1
        try:
            payload = extract_json_object(context_call(_context_prompt(cues, labels, pending)))
            rows = payload.get("labels", [])
            if not isinstance(rows, list):
                raise ValueError("labels must be a list")
            attempt_votes: dict[int, dict[str, object]] = {}
            for row in rows:
                cue_index = int(row["n"]) - 1
                speaker = str(row["speaker"])
                if cue_index not in pending or speaker not in allowed:
                    continue
                if cue_index in attempt_votes:
                    raise ValueError(f"duplicate context label for cue {cue_index + 1}")
                confidence: object = row.get("confidence")
                reason = str(row.get("reason") or "").strip()
                if require_confidence:
                    if isinstance(confidence, bool) or not isinstance(
                        confidence, (int, float)
                    ):
                        raise ValueError("context confidence must be a JSON number")
                    confidence = float(confidence)
                    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                        raise ValueError("context confidence must be finite within 0..1")
                    if not reason:
                        raise ValueError("context reason must be non-empty")
                attempt_votes[cue_index] = {
                    "n": cue_index + 1,
                    "speaker": speaker,
                    "confidence": confidence,
                    "reason": reason,
                    "source": "whole_clip_context",
                }
            votes.update(attempt_votes)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return votes, attempts, errors


def _speaker_context_env() -> dict[str, str]:
    """Load the fixed runtime CPA env without echoing or shell-evaluating it."""

    env = dict(os.environ)
    if env.get("CPA_BASE_URL") and env.get("CPA_API_KEY"):
        return env
    env_path = Path(os.environ.get("AUTOSLICE_CPA_ENV", "/opt/bilive/autoslice/cpa.env"))
    if not env_path.is_file():
        return env
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        raise SpeakerFinalizationError(f"CPA env permissions are too broad: {oct(mode)}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"CPA_[A-Z0-9_]+", key):
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        if len(values) != 1:
            raise SpeakerFinalizationError(f"invalid {key} entry in CPA env")
        env.setdefault(key, values[0])
    return env


def _call_context_via_cpa(prompt: str, *, repo_root: Path, work_dir: Path) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", dir=work_dir, delete=False) as handle:
        prompt_path = Path(handle.name)
        handle.write(prompt)
    output_path = prompt_path.with_suffix(".out")
    try:
        completed = subprocess.run(
            [
                "bash",
                str(repo_root / "scripts" / "llm_via_cpa.sh"),
                str(prompt_path),
                str(output_path),
                "gpt-5.6-sol gpt-5.5 gpt-5.4",
                "medium",
            ],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=_speaker_context_env(),
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise SpeakerFinalizationError(
                "speaker context judge failed: " + (completed.stderr or completed.stdout)[-800:]
            )
        return output_path.read_text(encoding="utf-8")
    finally:
        prompt_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
