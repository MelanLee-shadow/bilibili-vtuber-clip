"""Bound, replayable text-context screening for the optional speaker path.

A LIVE interval is a context inference, not an independently verified acoustic
source identity. MEDIA and UNKNOWN intervals exclude their full scored audio
windows from anchors. This module has no provider, file, or human-label loader.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import call_and_extract_json_with_parse_retry

MEDIA_SCHEMA = "speaker-media-screen.v1"


def digest(value):
    text = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class RecordedContextError(Exception):
    def __init__(self, error_type):
        super().__init__("Recorded context call failed")
        self.recorded_type = error_type


def error_type(exc):
    return getattr(exc, "recorded_type", type(exc).__name__)


def record_call(call, records):
    """Keep the actual response, including malformed responses, for cold replay."""
    def invoke(prompt):
        entry = {"prompt_sha256": digest(prompt)}
        try:
            response = call(prompt)
            if not isinstance(response, str):
                raise TypeError("Speaker context response must be text")
        except Exception as exc:
            entry["error_type"] = error_type(exc)
            records.append(entry)
            raise
        entry.update(response=response, response_sha256=digest(response))
        records.append(entry)
        return response
    return invoke


class ReplayCalls:
    """Accept exactly the saved prompt/response sequence; never invoke a provider."""
    def __init__(self, records):
        if not isinstance(records, list):
            raise ValueError("SPEAKER_CONTEXT_TRACE_INVALID")
        self.records = records
        self.position = 0
        self.invalid = False

    def __call__(self, prompt):
        try:
            entry = self.records[self.position]
            self.position += 1
            if not isinstance(entry, Mapping) or entry.get("prompt_sha256") != digest(prompt):
                raise ValueError("SPEAKER_CONTEXT_PROMPT_DRIFT")
            if "error_type" in entry:
                if (set(entry) != {"prompt_sha256", "error_type"}
                        or not isinstance(entry["error_type"], str)
                        or not entry["error_type"].isidentifier()):
                    raise ValueError("SPEAKER_CONTEXT_TRACE_INVALID")
                raise RecordedContextError(entry["error_type"])
            if (set(entry) != {"prompt_sha256", "response", "response_sha256"}
                    or not isinstance(entry["response"], str)
                    or entry["response_sha256"] != digest(entry["response"])):
                raise ValueError("SPEAKER_CONTEXT_RESPONSE_DRIFT")
            return entry["response"]
        except RecordedContextError:
            raise
        except (IndexError, KeyError, TypeError, ValueError):
            self.invalid = True
            raise ValueError("SPEAKER_CONTEXT_TRACE_INVALID") from None

    def complete(self):
        return not self.invalid and self.position == len(self.records)


def media_prompt(srt_text, binding):
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    windows = binding["geometry"]["cue_windows"]
    lines = [
        f"{i}. 音频窗口={windows[i - 1]['extracted_source_window_ms']}ms；字幕={cue.text.strip()}"
        for i, cue in enumerate(cues, 1)
    ]
    return (
        "# 播放素材筛查（说话人锚定之前；不判断谁是主播）\n"
        "下列是完整切片字幕和每句实际参与声纹打分的源音频窗口。"
        "你没有音频或画面，只能作文字上下文推断；不能声称直接听到或看到。\n"
        "请按场景找出正在播放的外部视频、歌曲或录音，以及无法判断声源的时段。"
        "没有提供声纹分数或任何身份答案，不要推断人物身份。\n"
        "用连续、不重叠的范围覆盖全部行号（从1到最后一行，不得遗漏）：\n"
        "- LIVE：上下文支持正在直播的参与者现场发声，包括自己读弹幕、引用、模仿、角色扮演或复述作品；\n"
        "- MEDIA：外部播放素材的声音，含可能混入参与者声音的播放时段；\n"
        "- UNKNOWN：仅凭文字无法区分直播对话与播放素材，或素材的边界不明。\n"
        "先区分说话方式与实际声源：文字像作品台词、重复台词或语气戏剧化，本身不是外部播放依据。"
        "按完整上下文建立事件顺序，结合后续解释和回顾判断前面的活动；"
        "不要把后来才打开素材的状态倒推到此前的引用或表演。"
        "有现场互动连续性、上下文支持参与者正在引用或模仿且无相反播放线索时，应标LIVE；"
        "不能仅因抽象地存在另一种声源可能，就把这种现场演绎标UNKNOWN。"
        "若来源线索相互冲突、观看动作后开播或停止的边界不明，或缺乏足够的对话衔接，仍标UNKNOWN，"
        "不得为了保留声纹锚点把实际未决声源写成LIVE。"
        "不要只标显然像台词的某一句：播放开始到返回直播对话之间的整段都须处理。"
        "例如讨论准备看视频不是已经播放，只有上下文有依据才分段；不能为留下锚点把未知写成LIVE。"
        "窗口含字幕前后的边界余量，也须考虑相邻场景的声音。"
        "每段给出具体文字依据和0到1的confidence，置信度不作新增阈值。\n\n"
        + "\n".join(lines)
        + '\n\n只输出JSON：{"segments":[{"start_cue":1,"end_cue":1,'
          '"kind":"LIVE或MEDIA或UNKNOWN","confidence":0.9,"reason":"上下文依据"}]}'
    )


def screen_media(srt_text, binding, *, llm_call, extract_json):
    prompt = media_prompt(srt_text, binding)
    payload = call_and_extract_json_with_parse_retry(
        prompt, llm_call=llm_call, extract_json=extract_json)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("segments"), list):
        raise ValueError("SPEAKER_MEDIA_SCREEN_INVALID")
    windows = binding["geometry"]["cue_windows"]
    next_cue = 1
    segments = []
    for item in payload["segments"]:
        if not isinstance(item, Mapping):
            raise ValueError("SPEAKER_MEDIA_SCREEN_INVALID")
        first, last = item.get("start_cue"), item.get("end_cue")
        confidence, reason = item.get("confidence"), item.get("reason")
        if (type(first) is not int or type(last) is not int
                or first != next_cue or not first <= last <= len(windows)
                or item.get("kind") not in ("LIVE", "MEDIA", "UNKNOWN")
                or type(confidence) not in (int, float) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1 or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("SPEAKER_MEDIA_SCREEN_INVALID")
        # The model names cues, never supplies new clocks. Include the entire
        # scored windows, so a neighbour's padding cannot bypass an exclusion.
        selected = windows[first - 1:last]
        segments.append({
            "start_cue": first, "end_cue": last, "kind": item["kind"],
            "confidence": confidence, "reason": reason.strip()[:240],
            "source_window_ms": [
                min(w["extracted_source_window_ms"][0] for w in selected),
                max(w["extracted_source_window_ms"][1] for w in selected),
            ],
        })
        next_cue = last + 1
    if next_cue != len(windows) + 1:
        raise ValueError("SPEAKER_MEDIA_SCREEN_INCOMPLETE")
    return {
        "schema_version": MEDIA_SCHEMA, "status": "COMPLETE",
        "srt_sha256": digest(srt_text), "source_binding_sha256": digest(dict(binding)),
        "prompt_sha256": digest(prompt), "evidence": "TEXT_CONTEXT_INFERENCE",
        "independent_av_verification": False, "segments": segments,
    }


def excluded_cues(screen, binding):
    """Non-live ranges win over overlapping LIVE windows, on a half-open clock."""
    excluded = {}
    for window in binding["geometry"]["cue_windows"]:
        a, b = window["extracted_source_window_ms"]
        hits = [
            s for s in screen["segments"]
            if s["kind"] != "LIVE" and a < s["source_window_ms"][1]
            and b > s["source_window_ms"][0]
        ]
        if hits:
            excluded[window["cue_index"]] = {
                "reason": "PLAYBACK_OR_UNCERTAIN_SOURCE_WINDOW",
                "kinds": sorted({s["kind"] for s in hits}),
                "source_window_ms": [a, b],
            }
    return excluded
