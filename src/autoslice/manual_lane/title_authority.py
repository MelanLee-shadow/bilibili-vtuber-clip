"""The title and cover-text authorities, read where step 6 and step 9 use them (TASK-25).

Step 6 used to carry its own title rules in assets/manual_lane/prompts/title.md; they drifted from the authorities
("写出转折", "最好引她的原话").  Now the prompts carry the current text of the authorities, each with its sha256, and the
lane keeps only its interface (participants, JSON shape, the lane's inputs):

- the lidousha-title-cover skill (60-title points to it; its own header: it wins over title_style.md and the steps):
  section 四 (titles), section 五 (cover text) and section 八 item 3 (fact core and rhetoric; the judge's rule text),
  references/examples.md, and references/judge-prompt.md as the acceptance judge's template;
- assets/lidousha/title_style.md, the prompt-side integration note: its 标题 and 封面文案 sections;
- assets/lidousha/manual_title_overrides.v1.json: approved style references, with evidenced authorship kept
  separate from approval/override authority; the current clip's own row is left out;
- title_policy (title_policy.json and its code): the hard gates' numbers and banned vocabulary, rendered, not copied.

The skill's text is read from the deployed projection (assets/lidousha/title_editorial_method.json, built by
scripts/build_title_editorial_method.py), the same one the ordinary automatic title reads, so the step does not need
the skill directory on the machine it runs on.  The projection carries each source file's sha256.  A missing or
malformed projection, or a missing section marker, stops the step: a prompt is never sent without the rules it is
supposed to carry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from src.autoslice.title_editorial_method import (
    EXAMPLES_FILE as EXAMPLES_FILE,
    JUDGE_RULES_LAST as JUDGE_RULES_LAST,
    JUDGE_RULES_START as JUDGE_RULES_START,
    AuthorityError as AuthorityError,
    Source,
    SourceRecord as SourceRecord,
    _read,
    cover_text,
    format_title_editorial_method,
    load_title_editorial_method,
)

from .layout import REPO_ROOT

if TYPE_CHECKING:
    from .subtitle_track import TrackCue

TITLE_STYLE = REPO_ROOT / "assets" / "lidousha" / "title_style.md"
TITLE_CORPUS = REPO_ROOT / "assets" / "lidousha" / "manual_title_overrides.v1.json"
STYLE_SECTIONS = ("## 标题", "## 封面文案")


class JudgeVerdict(TypedDict):
    overall: str
    fail_reasons: list[str]


def _style_sections(text: str) -> str:
    parts = []
    for heading in STYLE_SECTIONS:
        lines = text.splitlines()
        try:
            i = next(n for n, ln in enumerate(lines) if ln.strip() == heading)
        except StopIteration:
            raise AuthorityError(f"title_style.md: section {heading!r} not found") from None
        j = next((n for n in range(i + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
        parts.append("\n".join(lines[i:j]).strip())
    return "\n\n".join(parts)


def _corpus_rows() -> tuple[list[dict], Source]:
    """Style references only; this does not change the publication override reader or its authority."""
    text, source = _read("manual_title_overrides.v1.json", TITLE_CORPUS)
    try:
        data = json.loads(text)
        rows = list(data["overrides"])
        rows.extend(r.get("append_to_entries", r) for r in data.get("entries", []))
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthorityError(f"manual_title_overrides.v1.json: no /overrides list: {exc}") from exc
    return rows, source


def corpus_titles(exclude_candidate: str | None = None) -> tuple[list[str], Source]:
    """All approved references, without implying authorship; callers may withhold evaluation titles."""
    rows, source = _corpus_rows()
    titles = [str(r["title"]).strip() for r in rows
              if isinstance(r, Mapping) and str(r.get("title") or "").strip()
              and str(r.get("candidate_id") or "") != (exclude_candidate or "\0")]
    return titles, source


def gate_lines() -> str:
    """The hard gates step 6 and the upload path run (title_policy, channel_envelope), rendered from their code."""
    from src.autoslice import title_policy as tp
    from src.autoslice.channel_envelope import render_talk_title_prefix

    return "\n".join([
        f"- 候选数：交付 {tp._TITLE_MAX_CANDIDATES} 个；少于 {tp._TITLE_MIN_CANDIDATES} 个不收（title_policy）。",
        f"- 长度：正文（前缀之后）不超过 {tp._TITLE_MAX_LEN} 字；整条含前缀不少于 {tp._TITLE_MIN_LEN} 字、"
        f"不超过 {tp._TITLE_PLATFORM_MAX_LEN} 字（title_policy.json）。",
        f"- 前缀（channel_envelope）：title_body 只写正文，on_stage 列实际出场者的名单正式名；脚本据此生成前缀，"
        f"如「{render_talk_title_prefix()}」或「{render_talk_title_prefix(('出场者',))}」。",
        "- " + tp._automatic_title_bans_prompt(),
        "- 括号、书名号、引号必须成对。",
    ])


@dataclass(frozen=True)
class TitleMethod:
    text: str                     # the block the title prompt carries
    sources: tuple[Source, ...]
    corpus_count: int

    def sources_json(self) -> list[SourceRecord]:
        return [s.as_dict() for s in self.sources]


def _source_line(sources: Sequence[Source]) -> str:
    return "；".join(f"{s.name} sha256={s.sha256}" for s in sources)


def title_method(exclude_candidate: str | None = None) -> TitleMethod:
    """The editorial method the step-6 title prompt carries: skill 四、五 and 八.3 and examples.md, title_style.md's
    标题 / 封面文案 sections, authorship-labelled corpus, and gates, with source hashes."""
    editorial = load_title_editorial_method()
    style, s_style = _read("assets/lidousha/title_style.md", TITLE_STYLE)
    corpus, s_corpus = corpus_titles(exclude_candidate)
    rows, _ = _corpus_rows()
    own = set()
    for row in rows:
        label = row.get("style_authorship", {})
        if label.get("kind") == "reviewer_written":
            if not label.get("evidence"):
                raise AuthorityError("维护者 authorship requires evidence")
            own.add(str(row["title"]).strip())
    sources = (*editorial.sources, s_style, s_corpus)
    text = "\n\n".join([
        "## 编辑方法（以下是权威原文，按它写；来源与 sha256：" + _source_line(sources) + "）",
        format_title_editorial_method(editorial),
        "### title_style.md（接入说明：标题、封面文案）\n" + _style_sections(style),
        "### 维护者 亲写标题（有逐条作者证据；只学写法，不是本片事实）\n"
        + ("\n".join(f"- {t}" for t in corpus if t in own) or "本列表无；skill 示范中 ★ 标记的标题有作者证据。"),
        "### 维护者 认可的参考标题（不声称由他亲写；只学写法和口吻，不搬内容）\n"
        + "\n".join(f"- {t}" for t in corpus if t not in own),
        "### 发布硬门（代码执行，候选必须先过）\n" + gate_lines(),
    ])
    return TitleMethod(text=text, sources=sources, corpus_count=len(corpus))


def cover_text_rules() -> tuple[str, Source]:
    """Skill section 五 (cover text), for prompts that only rewrite the cover lines."""
    editorial = load_title_editorial_method()
    return cover_text(editorial), editorial.sources[0]


def review_rules() -> tuple[str, tuple[Source, ...]]:
    """The title rules step 9 reviews by (U74): skill 四、五 and 八.3, with their source line."""
    editorial = load_title_editorial_method()
    source = editorial.sources[0]
    return (f"（lidousha-title-cover skill 原文，sha256={source.sha256}）\n"
            + editorial.sections + "\n\n" + editorial.judge_rules), (source,)


# ----- subtitles with speakers --------------------------------------------------------------------------------------
def speaker_names(names: Sequence[str], *, host: str, guest_label: str) -> dict[str, str]:
    """speaker.srt's guest label stands for the one other participant when the session has exactly one."""
    guests = [n for n in names if n != host]
    return {guest_label: guests[0]} if len(guests) == 1 else {}


def _mmss(seconds: float) -> str:
    m, s = divmod(max(0.0, float(seconds)), 60.0)
    return f"{int(m)}:{s:05.2f}"


def numbered_lines(track: Sequence[TrackCue], rename: Mapping[str, str]) -> str:
    """`序号 m:ss.ss [说话人] 文字` (the label only where the track has one), final.srt numbering."""
    from .subtitle_track import plain_text

    out = []
    for i, c in enumerate(track, 1):
        spk = c.get("speaker")
        label = f"[{rename.get(spk, spk)}] " if spk else ""
        out.append(f"{i} {_mmss(c['start_s'])} {label}{plain_text(str(c['text']))}")
    return "\n".join(out)


def subtitle_text(track: Sequence[TrackCue]) -> str:
    """The cues' words alone, one cue per line (no numbers, times or speaker labels): what a name is looked for in."""
    from .subtitle_track import plain_text

    return "\n".join(plain_text(str(c["text"])) for c in track)


def speaker_srt(track: Sequence[TrackCue], rename: Mapping[str, str]) -> str:
    """The track as SRT with `[说话人] ` labels, for title_cover_quotes (its quote and speaker check)."""
    from .subtitle_track import fmt_ts, plain_text

    blocks = []
    for i, c in enumerate(track, 1):
        spk = c.get("speaker")
        label = f"[{rename.get(spk, spk)}] " if spk else ""
        blocks.append(f"{i}\n{fmt_ts(float(c['start_s']))} --> {fmt_ts(float(c['end_s']))}\n{label}"
                      f"{plain_text(str(c['text']))}")
    return "\n\n".join(blocks) + "\n"


# ----- the acceptance judge (skill section 八 item 3, references/judge-prompt.md) -----------------------------------
JUDGE_RUNS = 2   # judge-prompt.md: 每条标题跑两次


def judge_prompt(track_lines: str, title: str, cover_lines: Sequence[str]) -> tuple[str, tuple[Source, ...]]:
    """judge-prompt.md's template (the text after its first `---` line) with @@RULES@@ = section 八 item 3 verbatim,
    @@SUBTITLE@@ = the final subtitles with speakers, and the title's prefix, body and two cover lines."""
    from src.autoslice.channel_envelope import parse_talk_title

    editorial = load_title_editorial_method()
    env = parse_talk_title(title)
    prefix, body = (env.prefix, env.body) if env is not None else ("", title)
    lines = [*cover_lines, "", ""][:2]
    text = editorial.judge_template
    # the rules and the subtitles go in last, so nothing inside them is taken for a placeholder
    for key, value in (("@@PREFIX@@", prefix), ("@@TITLE@@", body), ("@@COVER1@@", lines[0]),
                       ("@@COVER2@@", lines[1]), ("@@SUBTITLE@@", track_lines), ("@@RULES@@", editorial.judge_rules)):
        text = text.replace(key, value)
    return text, (editorial.sources[0], editorial.judge_source)


def judge_verdict(parsed: Any) -> JudgeVerdict:
    """PASS only when the judge's JSON says PASS; an answer without an `overall` is a FAIL with that reason."""
    if not isinstance(parsed, Mapping) or str(parsed.get("overall") or "").upper() not in {"PASS", "FAIL"}:
        return {"overall": "FAIL", "fail_reasons": ["判官回包不是规定的 JSON（缺 overall）"]}
    overall = str(parsed["overall"]).upper()
    reasons = [str(r) for r in (parsed.get("fail_reasons") or []) if str(r).strip()]
    if overall == "FAIL" and not reasons:
        reasons = ["判官判 FAIL（未写原因）"]
    return {"overall": overall, "fail_reasons": reasons if overall == "FAIL" else []}
