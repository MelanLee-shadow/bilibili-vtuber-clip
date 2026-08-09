"""F12 受话人归属判项：hook/标题事实审补上"这话是对谁说的"。

出处（Ivan 2026-08-08 深夜纠错，受骗片标题案，
docs/reviews/2026-08-08-truth-harvest-forensics-synthesis.md「8/8 深夜追加:F12」）：
自动 hook「{host}刚被劝别再受骗」事实错——字幕 cue1-3 的「别再被骗」是连线主持
对**上一位选手**说的，cue4「下一位，我们的08号」之后本人才上场，她从未被劝。
`source_fact_review` 只验"这话说过没有"（词面确实在最终字幕里），**不验"对谁说"**，
所以把场上刚发生的一句话错归到主角头上是它结构上看不见的一类错。

本模块提供该判项的确定性半边：

* `build_addressee_transcripts` —— 从已产出的 speaker-final SRT 取出**带说话人
  标签**的转写（hash-bound + 与最终字幕逐条对齐才采用），作为判官的第二份文字
  authority；纯文字 `final_transcript` 逐字不变（既有回执/校验全部靠它绑定）。
* `requires_addressee_attribution` —— 文案里出现指向性言语行为标记时，判官**不准
  交空判项**（F15 盲证人同款纪律：可以答"无法判定"，不可以装作没这回事）。
* `evaluate_addressee_attribution` —— 判项形状与证据绑定校验；
  `WRONG_ADDRESSEE` 与 `status=KEEP` 互斥（fail-closed 打回重生成），
  无说话人转写时只允许 `UNVERIFIABLE`（没有标签就没有归属权威）。

刻意不做的事：不改字幕、不改说话人二分、不新增 schema 版本
（`source_fact_review` 的 SCHEMA_VERSION 是已落盘回执的相等性锚，动它等于让历史
回执整批失效）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER

SPEAKER_TRANSCRIPT_LABEL = "speaker_transcript"
ADDRESSEE_VERDICTS = ("SUPPORTED", "WRONG_ADDRESSEE", "UNVERIFIABLE")
ADDRESSEE_UNRESOLVED_REASON = "CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED"
MODE_SPEAKER_LABELLED = "speaker_labelled"
MODE_UNVERIFIABLE = "unverifiable_no_speaker_transcript"

_SPEAKER_LINE_RE = re.compile(
    r"^\[(" + "|".join(re.escape(value) for value in (HOST_SPEAKER, GUEST_SPEAKER)) + r")\]\s*(.*)\Z",
    re.S,
)
_SRT_TIMING_RE = re.compile(r"-->")
# 指向性言语行为标记：出现任一即认为文案含"谁对谁做/说了什么"的归属断言，
# 判官必须给出判项。刻意宁滥勿缺——过触发的代价只是多一条 UNVERIFIABLE，
# 漏触发的代价是受骗片那种把别人挨的话安到主角头上的事实错原样发出去。
_ATTRIBUTION_MARKERS = (
    "被", "劝", "让", "叫", "告诉", "问", "回应", "回怼", "怼", "骂", "夸",
    "催", "求", "提醒", "警告", "喊话", "喊", "教", "训", "怪", "怼回",
    "对我说", "对她说", "对他说", "跟我说", "跟她说", "跟他说", "说她", "说他",
    "点名", "邀请", "拒绝", "答应", "道歉", "表白", "吐槽", "安慰", "祝",
)


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_speaker_labelled_srt(text: str) -> list[tuple[str, str]] | None:
    """Return ``[(speaker, text)]`` for a canonical speaker-final SRT, else None."""

    rows: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or not _SRT_TIMING_RE.search(lines[1]):
            return None
        match = _SPEAKER_LINE_RE.fullmatch("\n".join(lines[2:]).strip())
        if match is None or not match.group(2).strip():
            return None
        rows.append((match.group(1), match.group(2).strip()))
    return rows or None


def render_speaker_transcript(rows: Sequence[tuple[str, str]]) -> str:
    """Numbered speaker-labelled lines: turn order is the whole point here."""

    return "\n".join(
        f"{index} [{speaker}] {text}" for index, (speaker, text) in enumerate(rows, start=1)
    )


def speaker_transcript_from_record(
    record: Mapping[str, object], cue_texts: Sequence[str]
) -> str | None:
    """Hash-bound speaker transcript, or None when it cannot be trusted.

    None is not a failure: ``uniform_host`` sessions never run speaker
    finalization at all.  It routes the judgment into the UNVERIFIABLE lane,
    which discloses instead of blocking.
    """

    raw_path = record.get("speaker_review_srt_path")
    hashes = record.get("artifact_hashes")
    expected = (hashes or {}).get("speaker_review_srt_sha256") if isinstance(hashes, Mapping) else None
    if not isinstance(raw_path, str) or not raw_path or not isinstance(expected, str):
        return None
    path = Path(raw_path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if str(expected).removeprefix("sha256:") != _sha256_file(path):
            return None
        rows = parse_speaker_labelled_srt(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if rows is None or len(rows) != len(cue_texts):
        return None
    if any(_compact(text) != _compact(cue) for (_speaker, text), cue in zip(rows, cue_texts)):
        # Drifted against the copy actually under review: refuse to reason
        # about attribution from a transcript that is not this clip's final.
        return None
    return render_speaker_transcript(rows)


def build_addressee_transcripts(
    record: Mapping[str, object], cues: Sequence[object]
) -> tuple[str, str | None]:
    """``(final_transcript, speaker_transcript)`` for the source-fact judge.

    ``final_transcript`` is byte-identical to what this call site produced
    before F12 — every persisted receipt binds its sha256.
    """

    texts = [str(getattr(cue, "text", "")).strip() for cue in cues]
    kept = [text for text in texts if text]
    return "\n".join(kept), speaker_transcript_from_record(record, kept)


def requires_addressee_attribution(*, selection_hook: str, title: str) -> bool:
    """True when the copy carries a person-directed claim the judge must rule on."""

    surface = _compact(selection_hook) + _compact(title)
    return any(marker in surface for marker in _ATTRIBUTION_MARKERS)


_RENDERED_LINE_RE = re.compile(r"(\d+)\s+\[([^\]]+)\]\s*(.*)\Z")


def _rendered_lines(speaker_transcript: str) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for line in speaker_transcript.splitlines():
        match = _RENDERED_LINE_RE.fullmatch(line.strip())
        if match is not None:
            rows.append((int(match.group(1)), match.group(2), match.group(3)))
    return rows


def _quoted_evidence(value: str) -> str | None:
    match = re.fullmatch(
        rf"\s*{SPEAKER_TRANSCRIPT_LABEL}\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _compact(match.group(1)) if match is not None else None


def speaker_evidence_row_is_bound(value: str, *, speaker_transcript: str) -> bool:
    """Accept ``speaker_transcript: <quoted line>`` bound to the real transcript."""

    quoted = _quoted_evidence(value)
    return bool(quoted and quoted in _compact(speaker_transcript))


def cited_lines(
    evidence: Sequence[str], *, speaker_transcript: str
) -> list[tuple[int, str, str]]:
    """The transcript lines an evidence list actually quotes."""

    quotes = [quote for quote in (_quoted_evidence(row) for row in evidence) if quote]
    return [
        row
        for row in _rendered_lines(speaker_transcript)
        if any(quote in _compact(f"{row[0]} [{row[1]}] {row[2]}") for quote in quotes)
    ]


def host_not_yet_on_air(
    evidence: Sequence[str], *, speaker_transcript: str
) -> bool:
    """The 受骗片 shape: every cited line is the guest, before the host's first line.

    Deterministic half of the F12 judgment — no model opinion involved.  If a
    host-subject claim is only supported by guest speech that happens *before*
    the host has a single line in this clip, the host cannot be the party the
    words were said to, whatever the judge asserts.
    """

    rows = _rendered_lines(speaker_transcript)
    cited = cited_lines(evidence, speaker_transcript=speaker_transcript)
    if not rows or not cited:
        return False
    host_indices = [index for index, speaker, _text in rows if speaker == HOST_SPEAKER]
    first_host = min(host_indices) if host_indices else None
    return all(
        speaker != HOST_SPEAKER and (first_host is None or index < first_host)
        for index, speaker, _text in cited
    )


@dataclass(frozen=True)
class AddresseeAttributionState:
    valid: bool
    mode: str
    rows: list[dict[str, object]] = field(default_factory=list)
    wrong_addressee: bool = False
    reason_code: str | None = None


def evaluate_addressee_attribution(
    value: object,
    *,
    selection_hook: str,
    title: str,
    speaker_transcript: str | None,
    status: object,
) -> AddresseeAttributionState:
    """Validate the judge's addressee rulings and apply the fail-closed rule."""

    mode = MODE_SPEAKER_LABELLED if speaker_transcript else MODE_UNVERIFIABLE
    if not isinstance(value, list):
        return AddresseeAttributionState(False, mode)
    rows = [dict(row) for row in value if isinstance(row, Mapping)]
    if len(rows) != len(value):
        return AddresseeAttributionState(False, mode)
    if (
        not rows
        and speaker_transcript
        and requires_addressee_attribution(selection_hook=selection_hook, title=title)
    ):
        # Silence is not an answer *when the judge can actually answer*: with a
        # speaker-labelled transcript in hand and a person-directed claim in the
        # copy, ducking the question is the F15 blind-witness hole again.
        # Without labels the verdict is predetermined (UNVERIFIABLE) and the
        # receipt already discloses that through addressee_attribution_mode, so
        # forcing a boilerplate row there would buy nothing.
        return AddresseeAttributionState(False, mode, rows, False, ADDRESSEE_UNRESOLVED_REASON)
    copy_surface = _compact(selection_hook) + "\n" + _compact(title)
    wrong = False
    for row in rows:
        verdict = row.get("verdict")
        assertion = row.get("assertion")
        reason = row.get("reason")
        if (
            verdict not in ADDRESSEE_VERDICTS
            or not isinstance(assertion, str)
            or not _compact(assertion)
            or _compact(assertion) not in copy_surface
            or not isinstance(reason, str)
            or len(reason.strip()) < 4
        ):
            return AddresseeAttributionState(False, mode, rows)
        if verdict == "UNVERIFIABLE":
            continue
        if speaker_transcript is None:
            # No speaker labels means no attribution authority.  A judge that
            # claims either way here is asserting what it cannot see.
            return AddresseeAttributionState(False, mode, rows)
        evidence = row.get("evidence")
        if not (
            isinstance(evidence, list)
            and evidence
            and all(
                isinstance(item, str)
                and speaker_evidence_row_is_bound(item, speaker_transcript=speaker_transcript)
                for item in evidence
            )
        ):
            return AddresseeAttributionState(False, mode, rows)
        if (
            verdict == "SUPPORTED"
            and GUEST_SPEAKER not in str(assertion)
            and host_not_yet_on_air(evidence, speaker_transcript=speaker_transcript)
        ):
            # 受骗片形状的确定性反证：断言的主语是主角（文案没点名连线），可判官
            # 引用的全是主角上场之前的连线发言。这句话不可能是对主角说的，
            # SUPPORTED 无论理由多漂亮都不成立。
            return AddresseeAttributionState(
                False, mode, rows, False, ADDRESSEE_UNRESOLVED_REASON
            )
        wrong = wrong or verdict == "WRONG_ADDRESSEE"
    if wrong and status == "KEEP":
        # fail-closed 打回：错归属绝不能以 KEEP 落地，判官必须给出去掉该断言的
        # 完整替代文案（走既有 REPAIR 复审环，理由留在 reason/summary 里）。
        return AddresseeAttributionState(
            False, mode, rows, True, ADDRESSEE_UNRESOLVED_REASON
        )
    return AddresseeAttributionState(True, mode, rows, wrong)


def addressee_prompt_block(speaker_transcript: str | None) -> str:
    """The judge-side contract for the attribution ruling."""

    if speaker_transcript:
        authority = (
            "带说话人标签的最终转写（受话人归属的唯一文字 authority；"
            f"标签只有「{HOST_SPEAKER}」和「{GUEST_SPEAKER}」两类，"
            "行号即字幕顺序，说话人切换点、称呼语、上场/介绍标记都在里面）:\n"
            + speaker_transcript
            + "\n"
        )
        rule = (
            "逐条检查两份文案里每个「主角被X」「主角对X说Y」「X劝/问/骂/夸主角」"
            "这类**归属断言**：说这句话的是谁、这句话是对谁说的、主角当时是否已经"
            "在场。判定填 SUPPORTED（说话人序列与对话结构支持该归属）、"
            "WRONG_ADDRESSEE（这话确实说过，但说话人或受话人不是文案写的那个——"
            "例如它是连线主持对上一位嘉宾说的，主角在其后才上场）或 UNVERIFIABLE"
            "（标签序列不足以判定）。SUPPORTED 与 WRONG_ADDRESSEE 必须各带至少一条"
            f"逐字证据，格式 \"{SPEAKER_TRANSCRIPT_LABEL}: <上面那份转写里的整行>\"。\n"
            "只要有任何一条 WRONG_ADDRESSEE，status 必须是 REPAIR，"
            "并给出删掉/改正该错误归属后的完整 final_selection_hook 与 final_title；"
            "带着 WRONG_ADDRESSEE 判 KEEP 一律作废重来。\n"
        )
    else:
        authority = "（本片没有可信的说话人标签转写。）\n"
        rule = (
            "没有说话人标签就没有受话人归属权威：addressee_attribution 里每条的"
            "verdict 只能是 UNVERIFIABLE，不得声称 SUPPORTED 或 WRONG_ADDRESSEE。\n"
        )
    return (
        "受话人归属判项（F12，Ivan 2026-08-08 受骗片标题案）：source-fact 复审"
        "此前只验\"这话说过没有\"，不验\"对谁说\"，于是把别人挨的话安到主角头上也能"
        "通过。现在必须额外产出 addressee_attribution 数组。\n"
        + authority
        + rule
        + "assertion 必须从 selection_hook 或 title 里逐字复制该归属断言的片段；"
        "文案里若确实没有任何归属断言，addressee_attribution 填空数组。\n"
    )
