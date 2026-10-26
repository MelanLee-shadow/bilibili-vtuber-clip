"""Per-cue speaker attribution (``speaker-attribution.v1``): internal text-review evidence.

维护者 (《闺蜜》two-person talk): do not rely on Gemini diarization.
Find absolute HOST anchors, or source-bound relative anchors after screening
played media and applying the existing strong two-group geometry,
then let the whole-clip text context (CPA) place the remaining cues as HOST /
OTHER (the other party, with an address such as 闺蜜) / MEDIA (played material)
/ UNKNOWN.  Anchors cannot be overturned by the context pass.

This is NOT a display or release input.  Talk subtitles stay ``uniform_host``
single-colour (docs/pipeline/40-subtitle-text.md); the result only feeds text
review (pronoun referents, who-said-what, title/cover fact attribution).
This internal step records insufficient evidence as UNKNOWN without making a
release decision: an unavailable embedder or too few anchors yield UNKNOWN;
a failed context call retains fixed anchors and leaves other cues UNKNOWN.
UNKNOWN is not a verified identity or a quality pass, and it cannot settle
unresolved subtitle or referent findings in the ordinary release review.

Thresholds are the existing talk-speaker policy constants
(``speaker_common.DEFAULT_POLICY``); nothing here introduces a new number.  The
CAM++ embedder is injected as a ``scorer`` callable so tests use a fake; the
production factory reuses ``speaker_finalizer``/``campp_embed_once`` and needs
the speaker venv (modelscope + torch + soundfile), not the main venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import (
    call_and_extract_json_with_parse_retry,
    extract_json_object,
)
from src.autoslice.speaker_common import DEFAULT_POLICY
from src.autoslice import speaker_attribution_context as context_evidence
from src.autoslice import speaker_attribution_geometry as geometry

SCHEMA = "speaker-attribution.v1"
HOST = "HOST"
OTHER = "OTHER"
MEDIA = "MEDIA"
UNKNOWN = "UNKNOWN"
LABELS = (HOST, OTHER, MEDIA, UNKNOWN)
EVIDENCE_VOICEPRINT = "voiceprint_anchor"
EVIDENCE_RELATIVE = "voiceprint_relative_anchor"
EVIDENCE_CONTEXT = "context_expansion"
EVIDENCE_UNKNOWN = "unknown"
# The existing clip-anchor rule (speaker_finalizer._prepare_campplus_anchor_state)
# refuses to build a host bank from fewer than two cues.
MIN_HOST_ANCHORS = 2
_MAX_ADDRESS_CODEPOINTS = 24

# Scorer: 1-based cue index -> median CAM++ cosine of that cue against the
# enrolled 李豆沙 references (the same ``seed_score`` the finalizer uses), or
# None when it cannot be computed.  Raising is treated as "unavailable".
Scorer = Callable[[int], "float | None"]


@dataclass(frozen=True)
class _SourceBoundScorer:
    """A normal runtime scorer plus the exact inputs it actually analyzed."""

    score: Scorer
    source_binding: Mapping[str, object]
    assert_current: Callable[[], None]
    cue_audio: tuple[dict, ...] | None = None

    def __call__(self, cue_index: int) -> float | None:
        return self.score(cue_index)


def _source_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", "", value)[:_MAX_ADDRESS_CODEPOINTS]
    return text or None


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None



def _score(value: object) -> float | None:
    """A cosine observation is a finite number, not a truthy flag or a label."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and -1.0 <= number <= 1.0 else None


def _score_partition(scores: Mapping[int, float]) -> dict[str, Any]:
    """Compute the existing two-group geometry, before any identity decision.

    Identity is authorized separately, only with the bound HOST reference,
    independent support windows, and a completed playback exclusion screen.
    """
    gap_required = 2 * float(DEFAULT_POLICY["ambiguity_band"])
    result: dict[str, Any] = {
        "status": "NO_SEPARATION", "groups": [], "identity_authorized": False,
        "method": "two_means_score_candidates", "minimum_center_gap": gap_required,
    }
    if len(scores) < 4:
        return result
    low, high = min(scores.values()), max(scores.values())
    if low == high:
        return result
    for _ in range(32):
        split = (low + high) / 2
        groups = ([i for i, v in scores.items() if v <= split],
                  [i for i, v in scores.items() if v > split])
        if any(len(group) < 2 for group in groups):
            return result
        centers = tuple(statistics.mean(scores[i] for i in group) for group in groups)
        if centers == (low, high):
            break
        low, high = centers
    if high - low < gap_required:
        return result
    result.update(status="CANDIDATES_ONLY", center_gap=high - low,
                  groups=[{"group_id": name, "cue_indexes": sorted(group),
                           "center": center, "identity": UNKNOWN}
                          for name, group, center in zip(("LOW", "HIGH"), groups, (low, high))])
    return result


def _policy_view(policy: Mapping[str, object] | None) -> dict[str, float]:
    merged = dict(DEFAULT_POLICY)
    for key, value in (policy or {}).items():
        if key in merged and isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = value
    return {
        "host_session_seed_min": float(merged["host_session_seed_min"]),
        # Retained for policy/receipt compatibility, never a negative identity rule.
        "guest_seed_max": float(merged["guest_seed_max"]),
        "guest_min_duration_ms": float(merged["guest_min_duration_ms"]),
    }


def _context_prompt(rows: list[dict[str, Any]], *, other_hint: str | None) -> str:
    def tag(row):
        if row.get("_excluded"):
            return "播放素材或声源不确定·不作身份推断"
        if row["_anchor"] == HOST and row["evidence"] == EVIDENCE_RELATIVE:
            return "李豆沙·片内相对声纹锚点"
        return "李豆沙·声纹锚点" if row["_anchor"] == HOST else "待判"

    lines = [f"{row['cue_index']}. [{tag(row)}] {row['text']}" for row in rows]
    pending = [row["cue_index"] for row in rows
               if row["_anchor"] != HOST and not row.get("_excluded")]
    hint = f"另一方的称呼提示：{other_hint}。\n" if other_hint else ""
    return (
        "# 说话人归属（只作文字审查内部证据，不改字幕、不改烧录颜色）\n"
        "这是李豆沙（直播间主人，女性）直播切片的完整字幕。方括号里是声纹结论："
        "[李豆沙·声纹锚点]=声纹高置信为李豆沙，不可推翻；[待判]=尚无确定的身份依据。"
        "[李豆沙·片内相对声纹锚点]=排除播放素材后，强双组中更接近已绑定李豆沙参考的一组，"
        "也是本轮固定HOST支持。播放素材或声源不确定行不得推断身份，也不能作为相邻身份传播的桥梁。"
        "对主播参考的低分只说明声纹支持不足，不证明不是主播；待判行可按上下文选择任一标签。"
        "这些选择只作 context_expansion 语境推断，不是声纹锚点或人审真值。\n"
        "请以锚点为基础，按整段对话的意思与轮次（问答、称呼方向、第一/第二人称、接话关系）推断每个待判句：\n"
        "- HOST：李豆沙本人；\n"
        "- OTHER：对话的另一方（真人连线者/闺蜜等），能从上下文推出称呼时在 address 给出，如“闺蜜”；\n"
        "- MEDIA：实际外部播放的视频、歌曲或录音声源；参与者现场朗读、引用或模仿仍按真实发声者判断 HOST/OTHER；\n"
        "- UNKNOWN：证据不足。宁可 UNKNOWN，不要为凑完整而猜。\n"
        "先在未被排除的连续对话段内识别完整发言轮次：字幕分行不等于换人，补充、停顿或续句可以属于同一轮发言。"
        "把相关待判句联立考虑，比较角色分配能否同时解释前后固定锚点、第一/第二人称、称呼方向与问答呼应，"
        "包括某句所承接的经历或信息属于谁；不要逐句只向前传播一个临时判断。"
        "前一句暂未确定，或短句单独没有身份词，并不意味着整轮发言都缺乏上下文依据。"
        "不能强制 HOST/OTHER 交替，也不能仅因相邻就认定同人；两种角色解释都能同样满足现有证据时仍保留 UNKNOWN。"
        "判断必须符合对话轮次：相邻两个人的连续短句不要合成同一人，除非语义明显是同一人的延续。\n"
        f"{hint}"
        f"需要返回的行号（1-based，其余行不要返回）：{pending}\n\n"
        + "\n".join(lines)
        + '\n\n只输出 JSON：{"labels":[{"n":1,"speaker":"HOST或OTHER或MEDIA或UNKNOWN",'
        '"address":"OTHER 时的称呼，否则 null","confidence":0.9,"reason":"具体上下文依据"}]}'
    )


def _unavailable_row(cue_index: int, cue: Any, text: str) -> dict[str, Any]:
    return {
        "cue_index": cue_index,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "text": text,
        "speaker": UNKNOWN,
        "other_label": None,
        "evidence": EVIDENCE_UNKNOWN,
        "voiceprint": {"score": None, "margin": None, "anchor": None},
        "confidence": None,
        "reason": "",
        "_anchor": None,
    }


def _relative_source_ready(binding: object, srt_text: str) -> bool:
    """Reuse the actual factory's enrolled-reference and extraction contract."""
    from src.autoslice.host_vocal_proof import EXPECTED_REFERENCE_COUNT

    if (not isinstance(binding, Mapping)
            or binding.get("schema_version") != geometry.SCHEMA
            or binding.get("srt_sha256") != _sha(srt_text)
            or not geometry.valid_geometry(binding, srt_text)
            or any(_source_digest(binding.get(k)) is None
                   for k in ("media_sha256", "profile_sha256", "model_tree_sha256"))):
        return False
    refs = binding.get("references")
    if not isinstance(refs, list) or len(refs) != EXPECTED_REFERENCE_COUNT:
        return False
    ids, files = set(), set()
    for ref in refs:
        if (not isinstance(ref, Mapping) or not isinstance(ref.get("id"), str)
                or not ref["id"].strip() or not isinstance(ref.get("filename"), str)
                or not ref["filename"].strip() or _source_digest(ref.get("sha256")) is None):
            return False
        path = Path(ref["filename"])
        if (path.is_absolute() or ".." in path.parts or ref["filename"] == "."
                or ref["id"] in ids or ref["filename"] in files):
            return False
        ids.add(ref["id"])
        files.add(ref["filename"])
    return True


def attribute_speakers(
    srt_text: str,
    *,
    scorer: Scorer | None,
    policy: Mapping[str, object] | None = None,
    llm_call: Callable[[str], str] | None = None,
    extract_json: Callable[[str], object] = extract_json_object,
    runtime_location: str = "UNSPECIFIED",
    other_label_hint: str | None = None,
) -> dict[str, Any]:
    """Return internal evidence; missing/invalid evidence never issues a release."""
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pol = _policy_view(policy)
    rows = [_unavailable_row(i, cue, cue.text.strip())
            for i, cue in enumerate(cues, 1)]
    status = "NO_ANCHORS"
    errors: list[str] = []
    scores: dict[int, float] = {}
    calls: list[dict] = []
    context_call = context_evidence.record_call(llm_call, calls) if llm_call else None
    binding = dict(scorer.source_binding) if isinstance(scorer, _SourceBoundScorer) else None

    if scorer is None:
        status = "EMBEDDER_UNAVAILABLE"
    else:
        for row in rows:
            try:
                value = _score(scorer(row["cue_index"]))
            except Exception as exc:
                if len(errors) < 3:
                    errors.append(type(exc).__name__)
                continue
            if value is not None:
                scores[row["cue_index"]] = value
        if not scores:
            status = "EMBEDDER_UNAVAILABLE"

    def source_drift():
        unavailable = attribute_speakers(
            srt_text, scorer=None, policy=policy, runtime_location=runtime_location,
            other_label_hint=other_label_hint)
        unavailable["reason_codes"] = ["SCORER_SOURCE_DRIFT"]
        return unavailable

    if isinstance(scorer, _SourceBoundScorer):
        try:
            scorer.assert_current()
            if binding["srt_sha256"] != _sha(srt_text):
                raise ValueError("scorer text binding drift")
        except Exception:
            return source_drift()

    source_ready = _relative_source_ready(binding, srt_text)
    if (isinstance(binding, Mapping) and binding.get("schema_version") == geometry.SCHEMA
            and not source_ready):
        return source_drift()
    media_screen = None
    excluded = {}
    media_attempted = False
    # A source-bound context run screens sound origin before either kind of
    # anchor. The acoustic-only legacy route still makes no context calls.
    if source_ready and context_call is not None and scores:
        media_attempted = True
        try:
            media_screen = context_evidence.screen_media(
                srt_text, binding, llm_call=context_call, extract_json=extract_json)
            excluded = context_evidence.excluded_cues(media_screen, binding)
        except Exception as exc:
            errors.append("SPEAKER_MEDIA_SCREEN:" + context_evidence.error_type(exc))
            # Do not use unscreened scores as an absolute fallback after the
            # requested source screen failed. This is optional UNKNOWN evidence.
            excluded = {row["cue_index"]: {"reason": "MEDIA_SCREEN_UNAVAILABLE"}
                        for row in rows}
            media_screen = {"status": "UNAVAILABLE"}

    eligible = {i: value for i, value in scores.items() if i not in excluded}
    partition = _score_partition(eligible)
    absolute_ids = {i for i, score in eligible.items() if score >= pol["host_session_seed_min"]}
    relative_ids = set()
    if len(absolute_ids) < MIN_HOST_ANCHORS and partition["status"] == "CANDIDATES_ONLY":
        if (source_ready and media_screen is not None
                and media_screen.get("status") == "COMPLETE"
                and all(geometry.independent_host_supports(binding, group["cue_indexes"])
                        >= MIN_HOST_ANCHORS for group in partition["groups"])):
            relative_ids = set(partition["groups"][1]["cue_indexes"])
            partition.update(
                status="RELATIVE_ANCHORED", identity_authorized=True,
                evidence=EVIDENCE_RELATIVE,
                source_binding_sha256=context_evidence.digest(binding),
                media_screen_sha256=context_evidence.digest(media_screen),
            )
            partition["groups"][1]["identity"] = HOST
        elif source_ready and media_screen is None:
            errors.append("SPEAKER_RELATIVE_MEDIA_SCREEN_UNAVAILABLE")
        elif not source_ready:
            errors.append("SPEAKER_RELATIVE_SOURCE_BINDING_UNAVAILABLE")
    # Excluded observations stay visible for diagnostics but cannot influence
    # group centers, independent-support counts, anchors or context propagation.
    if media_screen is not None:
        partition["excluded_cue_indexes"] = sorted(excluded)

    host_ids = absolute_ids | relative_ids
    if len(host_ids) < MIN_HOST_ANCHORS:
        host_ids = set()
    for row in rows:
        i = row["cue_index"]
        score = scores.get(i)
        row["_excluded"] = i in excluded
        if score is not None:
            row["voiceprint"] = {
                # Preserve the exact observation used by the decision, so
                # cold replay never crosses a boundary through rounding.
                "score": score, "margin": round(score - pol["host_session_seed_min"], 8),
                "anchor": HOST if i in host_ids else None,
            }
        if i in host_ids:
            row["_anchor"] = HOST
            row.update(speaker=HOST, evidence=(
                EVIDENCE_VOICEPRINT if i in absolute_ids else EVIDENCE_RELATIVE))
        elif i in excluded:
            row["reason"] = excluded[i]["reason"]

    context_attempted = False
    if host_ids:
        status = "ANCHORS_ONLY"
        pending = [r for r in rows if r["_anchor"] != HOST and not r["_excluded"]]
        if context_call is not None and pending:
            context_attempted = True
            try:
                payload = call_and_extract_json_with_parse_retry(
                    _context_prompt(rows, other_hint=_clean_address(other_label_hint)),
                    llm_call=context_call, extract_json=extract_json)
                _apply_context(rows, payload)
                status = "ATTRIBUTED"
            except Exception as exc:
                errors.insert(0, context_evidence.error_type(exc))

    if isinstance(scorer, _SourceBoundScorer):
        try:
            scorer.assert_current()
        except Exception:
            return source_drift()

    addresses = Counter(r["other_label"] for r in rows
                        if r["speaker"] == OTHER and r["other_label"])
    other_label = addresses.most_common(1)[0][0] if addresses else _clean_address(other_label_hint)
    for row in rows:
        row.pop("_anchor")
        row.pop("_excluded")
    return {
        "schema_version": SCHEMA,
        **({"source_binding": binding} if binding is not None else {}),
        **({"cue_audio": list(scorer.cue_audio)}
           if isinstance(scorer, _SourceBoundScorer) and scorer.cue_audio is not None else {}),
        "status": status, "reason_codes": errors, "srt_sha256": _sha(srt_text),
        "cue_count": len(rows), "policy": pol, "score_partition": partition,
        "media_screen": media_screen, "context_calls": calls,
        "runtime": {
            "location": runtime_location, "embedder": "campp" if scorer is not None else None,
            "context_attempted": context_attempted, "media_screen_attempted": media_attempted,
        },
        "other_label_hint": _clean_address(other_label_hint),
        "other_label": other_label, "cues": rows,
        "evidence_scope": "INTERNAL_TEXT_REVIEW_ONLY",
        "display_speaker_mode": "uniform_host", "display_authorized": False,
        "release_authorized": False, "mutation_authorized": False,
    }


def replay_attribution(attribution: Mapping, srt_text: str) -> dict:
    """Deterministically reconstruct new persisted evidence without file/provider I/O."""
    scores = {r["cue_index"]: r["voiceprint"]["score"] for r in attribution["cues"]}
    binding = attribution.get("source_binding")
    scorer = _SourceBoundScorer(
        lambda i: scores[i], binding, lambda: None,
        tuple(attribution["cue_audio"]) if "cue_audio" in attribution else None)
    replay = context_evidence.ReplayCalls(attribution.get("context_calls", []))
    runtime = attribution.get("runtime", {})
    requested = runtime.get("context_attempted") or runtime.get("media_screen_attempted")
    canonical = attribute_speakers(
        srt_text, scorer=scorer, policy=attribution.get("policy"),
        llm_call=replay if requested else None,
        runtime_location=runtime.get("location", "UNSPECIFIED"),
        other_label_hint=attribution.get("other_label_hint"))
    if not replay.complete():
        raise ValueError("SPEAKER_CONTEXT_TRACE_INVALID")
    return canonical


def _apply_context(rows: list[dict[str, Any]], payload: object) -> None:
    """Apply validated context labels; anchors are immutable.  Raises on bad shape."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("labels"), list):
        raise ValueError("labels must be a list")
    by_index = {row["cue_index"]: row for row in rows}
    seen: set[int] = set()
    updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in payload["labels"]:
        if not isinstance(item, Mapping):
            raise ValueError("label row must be an object")
        n = item.get("n")
        speaker = item.get("speaker")
        if isinstance(n, bool) or not isinstance(n, int) or n not in by_index:
            raise ValueError("label row has an invalid cue number")
        if n in seen:
            raise ValueError("duplicate cue label")
        seen.add(n)
        if speaker not in LABELS:
            raise ValueError("label row has an invalid speaker")
        row = by_index[n]
        if row["_anchor"] == HOST or row.get("_excluded"):
            continue  # Neither fixed anchors nor excluded sound can be relabelled
        if speaker == UNKNOWN:
            continue
        updates.append(
            (
                row,
                {
                    "speaker": speaker,
                    "evidence": EVIDENCE_CONTEXT,
                    "other_label": (
                        _clean_address(item.get("address")) if speaker == OTHER else None
                    ),
                    "confidence": _confidence(item.get("confidence")),
                    "reason": str(item.get("reason") or "").strip()[:240],
                },
            )
        )
    for row, values in updates:  # all-or-nothing: a malformed reply changes nothing
        row.update(values)


def attribution_context_for_srt(
    attribution: Mapping[str, object] | None, srt_text: str, *,
    expected_source_media_sha256: str | None = None,
    expected_profile_sha256: str | None = None,
    expected_timeline_offset_ms: int | None = None,
) -> str:
    """Recheck exact sources, identity derivation, media screen and cue geometry.

    Old relative-only labels are not upgraded by renaming an evidence field.
    New relative evidence must reconstruct from bound numeric observations and
    the original context responses. Legacy low-score NON_HOST remains invalid.
    """
    if (
        not isinstance(attribution, Mapping)
        or attribution.get("schema_version") != SCHEMA
        or attribution.get("srt_sha256") != _sha(srt_text)
        or not isinstance(attribution.get("cues"), list)
    ):
        return ""
    from src.autoslice import speaker_attribution_geometry as geometry

    binding = attribution.get("source_binding")
    expected = (expected_source_media_sha256, expected_profile_sha256,
                expected_timeline_offset_ms)
    if binding is not None or any(value is not None for value in expected):
        if (not isinstance(binding, Mapping)
                or binding.get("schema_version") not in ("speaker-attribution-source.v1", geometry.SCHEMA)
                or binding.get("srt_sha256") != _sha(srt_text)
                or any(_source_digest(binding.get(key)) is None
                       for key in ("media_sha256", "profile_sha256", "model_tree_sha256"))
                or type(binding.get("timeline_offset_ms")) is not int
                or (binding.get("schema_version") == "speaker-attribution-source.v1"
                    and binding["timeline_offset_ms"] != 0)
                or (binding.get("schema_version") == geometry.SCHEMA
                    and not geometry.valid_geometry(binding, srt_text))):
            return ""
        for key, current in (("media_sha256", expected_source_media_sha256),
                             ("profile_sha256", expected_profile_sha256)):
            if current is not None and (
                    _source_digest(current) is None
                    or binding[key] != _source_digest(current)):
                return ""
        if expected_timeline_offset_ms is not None and (
                type(expected_timeline_offset_ms) is not int
                or binding["timeline_offset_ms"] != expected_timeline_offset_ms):
            return ""
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    rows = attribution["cues"]
    if len(rows) != len(cues):
        return ""
    policy = attribution.get("policy")
    if not isinstance(policy, Mapping):
        return ""
    host_min = policy.get("host_session_seed_min")
    guest_max = policy.get("guest_seed_max")
    duration_min = policy.get("guest_min_duration_ms")
    if (type(host_min) not in (int, float) or not math.isfinite(host_min)
            or not float(DEFAULT_POLICY["host_session_seed_min"]) <= host_min <= 1
            or type(guest_max) not in (int, float) or not math.isfinite(guest_max)
            or not -1 <= guest_max <= float(DEFAULT_POLICY["guest_seed_max"])
            or type(duration_min) not in (int, float) or not math.isfinite(duration_min)
            or duration_min < float(DEFAULT_POLICY["guest_min_duration_ms"])):
        return ""
    new_relative = any(isinstance(row, Mapping) and row.get("evidence") == EVIDENCE_RELATIVE
                       for row in rows)
    if new_relative or attribution.get("media_screen") is not None:
        if not _relative_source_ready(binding, srt_text):
            return ""
        try:
            canonical = replay_attribution(attribution, srt_text)
            for key in ("cues", "policy", "status", "score_partition", "media_screen",
                        "context_calls", "reason_codes", "other_label", "other_label_hint",
                        "display_authorized", "release_authorized", "mutation_authorized",
                        "display_speaker_mode", "evidence_scope"):
                if attribution.get(key) != canonical[key]:
                    return ""
        except (KeyError, TypeError, ValueError):
            return ""
    seen: set[int] = set()
    positive_hosts = 0
    for row in rows:
        if not isinstance(row, Mapping):
            return ""
        index, speaker = row.get("cue_index"), row.get("speaker")
        if (type(index) is not int or not 1 <= index <= len(cues)
                or index in seen or speaker not in LABELS):
            return ""
        seen.add(index)
        cue = cues[index - 1]
        if (type(row.get("start_ms")) is not int or type(row.get("end_ms")) is not int
                or (row["start_ms"], row["end_ms"], row.get("text"))
                != (cue.start_ms, cue.end_ms, cue.text.strip())):
            return ""
        voiceprint = row.get("voiceprint")
        # Check before skipping UNKNOWN: even an unresolved legacy row may
        # have imposed the false NON_HOST premise on other context decisions.
        if isinstance(voiceprint, Mapping) and voiceprint.get("anchor") not in (None, HOST):
            return ""
        if speaker == UNKNOWN:
            continue
        if not isinstance(voiceprint, Mapping):
            return ""
        score = _score(voiceprint.get("score"))
        evidence = row.get("evidence")
        if evidence == EVIDENCE_VOICEPRINT:
            if (speaker != HOST or voiceprint.get("anchor") != HOST
                    or score is None or score < host_min):
                return ""
            positive_hosts += 1
        elif evidence == EVIDENCE_RELATIVE:
            if not new_relative or speaker != HOST or voiceprint.get("anchor") != HOST:
                return ""
            positive_hosts += 1
        elif evidence == EVIDENCE_CONTEXT:
            if (not str(row.get("reason") or "").strip()
                    or _confidence(row.get("confidence")) is None
                    or voiceprint.get("anchor") == HOST):
                return ""
        else:
            return ""  # Unknown evidence tags are not identities.
    if positive_hosts < MIN_HOST_ANCHORS:
        return ""
    if isinstance(binding, Mapping) and binding.get("schema_version") == geometry.SCHEMA:
        host_ids = [row["cue_index"] for row in rows
                    if row.get("evidence") in (EVIDENCE_VOICEPRINT, EVIDENCE_RELATIVE)]
        if geometry.independent_host_supports(binding, host_ids) < MIN_HOST_ANCHORS:
            return ""
    other_label = _clean_address(attribution.get("other_label"))
    lines = []
    for row in sorted(rows, key=lambda row: row["cue_index"]):
        speaker = row["speaker"]
        if speaker == UNKNOWN:
            continue
        name = speaker
        if speaker == OTHER:
            name = f"OTHER:{_clean_address(row.get('other_label')) or other_label or ''}".rstrip(":")
        lines.append(f"{row['cue_index']}. {name}（{row.get('evidence')}）")
    return (
        "说话人归属（内部证据，来自声纹锚点+上下文推断；UNKNOWN 行已省略；"
        "不是显示颜色，也不是逐字真值）：\n"
        + (("source_binding_sha256=" + _sha(json.dumps(dict(binding), sort_keys=True,
             separators=(",", ":"))) + "\n") if isinstance(binding, Mapping) else "")
        + (("attribution_evidence_sha256=" + context_evidence.digest({
            "partition": attribution.get("score_partition"),
            "media_screen": attribution.get("media_screen"),
            "context_calls": attribution.get("context_calls"),
        }) + "\n") if new_relative or attribution.get("media_screen") is not None else "")
        + "\n".join(lines)
    )


def _text_cues_for_wavs(srt_text: str) -> list[Any]:
    from scripts.apply_subtitle_text_overrides import TextCue

    def stamp(ms: int) -> str:
        hours, rest = divmod(ms, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    return [
        TextCue(index, stamp(cue.start_ms), stamp(cue.end_ms), cue.text.strip())
        for index, cue in enumerate(
            (cue for cue in parse_srt_cues(srt_text) if cue.text.strip()), start=1
        )
    ]


def build_campp_cue_scorer(
    *,
    media_path: Path,
    srt_text: str,
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    timeline_offset_ms: int = 0,
    source_end_ms: int | None = None,
) -> tuple[Scorer, dict[str, float | int]]:
    """Production scorer: median CAM++ cosine vs the enrolled references per cue.

    Same computation as ``speaker_finalizer`` ``seed_scores`` (hash-bound model
    and references, embed-once cache).  Needs the speaker venv; raises when the
    runtime is missing, which ``attribute_speakers`` callers map to UNKNOWN.
    """

    from src.autoslice import speaker_finalizer as finalizer

    from src.autoslice import speaker_attribution_geometry as geometry

    if source_end_ms is None and (type(timeline_offset_ms) is not int or timeline_offset_ms != 0):
        raise ValueError("SPEAKER_CLOCK_REQUIRES_EXPLICIT_SOURCE_END")
    if source_end_ms is not None:
        geometry.geometry(srt_text, timeline_offset_ms, source_end_ms, source_end_ms)
    media_hash = finalizer.sha256_file(media_path)
    profile_hash = finalizer.sha256_file(profile_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile, references, model_hash, verifier = finalizer._load_runtime(
        profile_path, reference_dir, model_dir
    )
    mapping, cue_audio = None, None
    if source_end_ms is None:
        _audio, _rate, cue_paths = finalizer._extract_cue_wavs(
            media_path, _text_cues_for_wavs(srt_text), work_dir
        )
    else:
        cue_paths, mapping, cue_audio = geometry.extract_cue_wavs(
            media_path, srt_text, work_dir, timeline_offset_ms, source_end_ms)
    similarity = finalizer._build_embedding_similarity(
        verifier=verifier, model_hash=model_hash, work_dir=work_dir
    )

    def scorer(cue_index: int) -> float:
        cue_path = cue_paths[cue_index - 1]
        return float(
            statistics.median(
                similarity(Path(reference["path"]), cue_path) for reference in references
            )
        )

    def assert_current() -> None:
        if (finalizer.sha256_file(media_path) != media_hash
                or finalizer.sha256_file(profile_path) != profile_hash):
            raise ValueError("speaker source media/profile drift")
        finalizer._assert_runtime_assets_stable(model_dir=model_dir,
            model_tree_sha256=model_hash, references=references)
        if json.dumps(binding, sort_keys=True) != original_binding:
            raise ValueError("speaker clock binding drift")
        if mapping is not None and finalizer.sha256_file(
                work_dir / "clip-16k-mono.wav") != binding["decoded_audio_sha256"]:
            raise ValueError("speaker decoded source audio drift")
        for item in cue_audio or ():
            if finalizer.sha256_file(work_dir / item["relative_path"]) != item["sha256"]:
                raise ValueError("speaker cue audio drift")

    binding = {
        "schema_version": "speaker-attribution-source.v1",
        "media_sha256": media_hash,
        "profile_sha256": profile_hash,
        "model_tree_sha256": model_hash,
        "srt_sha256": _sha(srt_text),
        # This factory analyzes SRT times directly against the supplied media.
        "timeline_offset_ms": 0,
    }
    if mapping is not None:
        binding.update(schema_version=geometry.SCHEMA, timeline_offset_ms=timeline_offset_ms,
            geometry=mapping,
            decoded_audio_sha256=finalizer.sha256_file(work_dir / "clip-16k-mono.wav"),
            references=[{k:r[k] for k in ("id", "filename", "sha256")} for r in references])
    original_binding = json.dumps(binding, sort_keys=True)
    return _SourceBoundScorer(scorer, MappingProxyType(binding), assert_current,
        tuple(cue_audio) if cue_audio is not None else None), finalizer._policy(profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit speaker-attribution.v1 for one text-final SRT.")
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-location", required=True, help="host that ran CAM++, e.g. oci3")
    parser.add_argument("--other-label-hint")
    parser.add_argument("--no-context", action="store_true")
    parser.add_argument("--timeline-offset-ms", type=int, default=0)
    parser.add_argument("--source-end-ms", type=int)
    args = parser.parse_args(argv)
    srt_text = args.srt.read_text(encoding="utf-8")
    scorer: Scorer | None = None
    policy: Mapping[str, object] | None = None
    reasons: list[str] = []
    try:
        scorer, policy = build_campp_cue_scorer(
            media_path=args.media,
            srt_text=srt_text,
            profile_path=args.profile,
            reference_dir=args.reference_dir,
            model_dir=args.model_dir,
            work_dir=args.work_dir,
            timeline_offset_ms=args.timeline_offset_ms,
            source_end_ms=args.source_end_ms,
        )
    except Exception as exc:  # unavailable runtime -> UNKNOWN document, exit 0
        reasons.append(f"EMBEDDER_RUNTIME:{type(exc).__name__}")
    llm_call = None
    if not args.no_context:
        from src.autoslice.speaker_context import _call_context_via_cpa

        repo_root = Path(__file__).resolve().parents[2]
        llm_call = lambda prompt: _call_context_via_cpa(
            prompt, repo_root=repo_root, work_dir=args.work_dir
        )
    document = attribute_speakers(
        srt_text,
        scorer=scorer,
        policy=policy,
        llm_call=llm_call,
        runtime_location=args.runtime_location,
        other_label_hint=args.other_label_hint,
    )
    document["reason_codes"] = reasons + document["reason_codes"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": document["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
