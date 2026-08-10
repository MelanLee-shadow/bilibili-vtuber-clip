"""歌名命名权威：判定「可能是歌」之后，命名权从 BCUT 中文 ASR / 画面 OCR
切到听音频那条链（AGY/Gemini 音频 × canonical LRC 全局位移证明）。

Ivan 2026-08-10 逐字：「之前难道不是 gemini 听音频识别歌曲吗？如果意识到了
可能是歌再从 BCUT 切换过来」。

实证背景（2026-08-08 `song_210131_1210`《心型病毒》）：BCUT 中文 ASR 把歌名
听成同音的《新型病毒》（`pypinyin` 实测两者同为 ``xin xing bing du``，纯文本
侧无论多聪明都分不开），而**同一条候选的音频链其实已经证成**——
``external_lrc=netease://song/536937431``、``matched_line_ratio=1.0``、
``song_boundary_status=FULL_SONG_READY``。缺陷不在能力，在授权：旧代码只在
整条交付门全通过时才把边界歌名扶正（``song_lane._apply_canonical_song_title``
挂在 ``song_complete`` 上），于是这条被 host-vocal 拦下的候选在 state 里只剩
BCUT 的《新型病毒》，音频证过的真名只躺在磁盘报告里，重试还会拿着错名再去
检索一遍。

本模块只回答「谁说了算」，不放宽任何判据、不动任何阈值：

* 音频链证成 → 它是**唯一**命名权威，并带可审计出处（``source_ref`` /
  provider / model / ``matched_line_ratio`` / 报告路径与 sha）；
* 音频链没证成 → **没有权威名**。BCUT/OCR 名只以 labeled candidate 留档，
  任何下游都不得把它升格成名字（宁可无名，也不拿垃圾名交付）。

命名 ≠ 授权：host-vocal 判否只说明「可能不是李豆沙在唱」，音频对齐仍然证明了
这一段**是哪首歌**。因此被 block 的行照样记命名权威（供审计与重试检索），而
``result["title"]``/交付仍由既有交付门决定，本模块不放行任何一条。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

_QUOTED_TITLE_RX = re.compile(r"[《「『]([^》」』]{1,80})[》」』]")

# 与 ``song_completion`` 的 ``is_audio_report``/``is_audio_model`` 交叉校验同源：
# 报告里的 ``evidence_source`` 与 alignment 的 model 后缀必须同真同假，这里用
# 后缀判别即可，不必再读盘。
AUDIO_EVIDENCE_SOURCE = "agy_audio_lrc"
AUDIO_ALIGNMENT_MODEL_SUFFIX = "-agy-audio-lrc-global-shift-v1"
SONG_BOUNDARY_READY_STATUS = "FULL_SONG_READY"
SONG_NAME_AUTHORITY_SCHEMA_VERSION = "song-name-authority.v1"

#: 命名权威的唯一合法出处。任何新增来源都必须先证到音频，再进这个白名单。
AUDIO_LRC_AUTHORITY = "audio_lrc"

#: 候选提示（永远不是权威）的来源标签。
VISUAL_OCR_HINT = "visual_song_ocr"
ASR_HOOK_HINT = "asr_hook_quoted"
ASR_PREVIEW_HINT = "asr_preview_quoted"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def extract_audio_song_name_authority(summary_record: Any) -> dict | None:
    """从 selector summary 提取**音频证过**的歌名身份，否则 None。

    纯文本 LRC 对齐（窄窗那一趟没有 audio aligner）同样会写出
    ``FULL_SONG_READY`` 边界，但它的 alignment model 没有音频后缀。那条路的
    歌名归根到底还是 BCUT 中文 ASR 的匹配结果，绝不能当命名权威——这正是
    《心型病毒》/《新型病毒》这种同音对不可分的那一层。
    """

    job = _mapping(_mapping(summary_record).get("source_context_job"))
    boundary = _mapping(job.get("song_boundary"))
    alignment = _mapping(job.get("lyrics_alignment"))
    song_title = _text(boundary.get("song_title"))
    model = _text(alignment.get("model"))
    if (
        not song_title
        or boundary.get("status") != SONG_BOUNDARY_READY_STATUS
        or alignment.get("status") != "READY"
        or not model.endswith(AUDIO_ALIGNMENT_MODEL_SUFFIX)
    ):
        return None
    return {
        "schema_version": SONG_NAME_AUTHORITY_SCHEMA_VERSION,
        "authority": AUDIO_LRC_AUTHORITY,
        "evidence_source": AUDIO_EVIDENCE_SOURCE,
        "song_title": song_title,
        "source_ref": _text(alignment.get("external_lrc")) or _text(alignment.get("source")) or None,
        "provider": _text(alignment.get("provider")) or None,
        "model": model,
        "matched_line_ratio": alignment.get("matched_line_ratio"),
        "alignment_report_path": _text(alignment.get("alignment_report_path")) or None,
        "alignment_report_sha256": _text(alignment.get("alignment_report_sha256")) or None,
        "song_boundary_source": _text(boundary.get("source")) or None,
    }


def authoritative_song_title(record: Any) -> str | None:
    """已记录的权威歌名；来源不是音频链的一律当作没有权威名。"""

    authority = _mapping(_mapping(record).get("song_name_authority"))
    if authority.get("authority") != AUDIO_LRC_AUTHORITY:
        return None
    if authority.get("evidence_source") != AUDIO_EVIDENCE_SOURCE:
        return None
    return _text(authority.get("song_title")) or None


def quoted_titles_in(*texts: Any, limit: int = 2) -> list[str]:
    """《…》里的歌名候选——来自 BCUT ASR 文本或它派生的 hook，只是提示。"""

    joined = "\n".join(_text(text) for text in texts)
    found = _QUOTED_TITLE_RX.findall(joined)
    deduped = list(dict.fromkeys(title.strip() for title in found if title.strip()))
    return deduped[: max(0, int(limit))]


def song_name_hint_candidates(record: Any) -> list[dict]:
    """把 BCUT/OCR 侧的歌名整理成**带来源标签的候选**，永不升格成名字。

    留档目的有两个：一是让人（和后续裁定）看得见「机器当时以为叫什么」，
    二是让「权威名 ≠ 候选名」这件事在 state 里可审计，而不是靠读代码推断。
    """

    row = _mapping(record)
    candidates: list[dict] = []
    seen: set[str] = set()

    def _add(title: Any, source: str) -> None:
        text = _text(title)
        if not text or text in seen:
            return
        seen.add(text)
        candidates.append({"song_title": text, "source": source, "authority": False})

    _add(row.get("title_hint"), VISUAL_OCR_HINT)
    for title in quoted_titles_in(row.get("hook")):
        _add(title, ASR_HOOK_HINT)
    for title in quoted_titles_in(row.get("preview")):
        _add(title, ASR_PREVIEW_HINT)
    return candidates


def ordered_song_lrc_queries(
    *,
    authority_title: str | None,
    visual_title_hint: str,
    quoted_titles: Sequence[str],
    known_song_query: str,
) -> list[str]:
    """LRC 检索的显式查询顺序（同时就是 ``preferred_title_hints``）。

    没有权威名时，顺序与历史完全一致（视觉 hint → hook/preview 引号标题 →
    演唱 ASR 文本），第一趟的召回行为一字不改。

    一旦音频链已经证出真名（重试/续跑），显式查询就只留这个真名加原始演唱
    文本：错名不能再占 ``preferred_title_hints`` 的位置去顶替一个已经被音频
    证过的身份（《新型病毒》正是这么一直被带着走的）。召回并没有被收窄成
    一条——``song_repair._discover_lrc_candidates`` 里的 LLM 猜名与逐行歌词
    两条 lane 照常轮转，排序/阈值一字未动。
    """

    if authority_title:
        ordered = [authority_title, known_song_query]
    else:
        ordered = [visual_title_hint, *list(quoted_titles)[:2], known_song_query]
    return [query for query in dict.fromkeys(_text(query) for query in ordered) if query]


def carry_song_identity_evidence(record: Any) -> dict:
    """跨 tick 重试要原样带走的歌名证据。

    视觉标题证据本来就要带（丢了就会永远重复同一次 LRC 歧义）；音频证过的
    命名权威更要带——不带就等于每次重试都回到 BCUT 的错名重新检索一遍。
    """

    row = _mapping(record)
    return {
        "title_hint": row.get("title_hint"),
        "visual_song_evidence": row.get("visual_song_evidence"),
        "song_name_authority": row.get("song_name_authority"),
    }


def record_song_naming(result: Any) -> None:
    """把命名权威与候选提示落到最终 song 记录上（就地修改）。

    权威可能是在 ``_full`` 那一趟证出来的：窄窗那趟根本没有 audio aligner。
    全源趟失败时 ``produce_song`` 只按白名单把它折进 ``full_source_retry``，
    所以这里要把权威从那层提上来——否则真名会像修复前的标题一样被埋掉。
    """

    if not isinstance(result, dict):
        return
    authority = result.get("song_name_authority")
    if not isinstance(authority, Mapping):
        retry = _mapping(result.get("full_source_retry"))
        nested = retry.get("song_name_authority")
        authority = nested if isinstance(nested, Mapping) else None
    result["song_name_authority"] = dict(authority) if authority else None
    result["song_title_candidates"] = song_name_hint_candidates(result)
