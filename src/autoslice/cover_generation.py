"""Profile-driven AI cover generation, art direction, and title overlay.

Extracted from the shadow pipeline as one cohesive subsystem.  The default
profile keeps the historical Li Dousha prompts, fonts, layouts, and rendering
contracts; a selected channel profile supplies identity and asset paths.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cover_emote import (
    EmoteEntry,
    EmoteLibrary,
    emote_catalog_prompt_block,
    normalize_emote_choice,
)
from src.autoslice.llm_client import LlmCall, extract_json_object


ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


def profile_asset_text(key: str) -> str:
    try:
        return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT).read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "(资产文件缺失)"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lidousha_fontsdir(media_path: Path | None = None) -> Path | None:
    """Resolve selected-profile fonts while retaining legacy runtime fallbacks."""

    candidates: list[Path] = []
    env_value = os.environ.get("AUTOSLICE_FONTS_DIR") or os.environ.get(
        "LIDOUSHA_FONTS_DIR"
    )
    if env_value:
        candidates.append(Path(env_value))
    if media_path is not None:
        for parent in [media_path.parent, *media_path.parents]:
            candidates.append(parent / "fonts")
    candidates.extend(
        [
            CHANNEL_PROFILE.asset_directory("fonts", repo_root=ROOT),
            ROOT / "assets" / "fonts",
            ROOT / "assets",
            Path("/app/assets/fonts"),
            Path("/opt/bilive/app/assets/fonts"),
            Path("/app/assets"),
            Path("/opt/bilive/app/assets"),
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LidoushaCoverArtDirection:
    """One cover's art direction: chosen deterministically per candidate_id (so
    covers differ but are reproducible) and optionally refined by a CPA judge."""

    role: str            # persona archetype key (see persona.md 封面表情/角色映射)
    expression_en: str   # in-character English face phrase injected into the CPA prompt
    background_style: str  # a key in _COVER_BG_BUSY (talk) or _COVER_BG_CALM (song/tender)
    layout: str          # left-split | right-split | banner | song-clean
    hook_color: str      # key in _COVER_HOOK_COLORS
    is_song: bool
    hook_word: str = ""  # verbatim substring of cover_text to highlight ("" = none)
    line_breaks: tuple[str, ...] = ()  # LLM word-aware line split of cover_text ("" = balancer)
    words: tuple[str, ...] = ()  # LLM word segmentation of cover_text — wrap atoms
    #   (Ivan 2026-07-10: keep the FULL title and grow the font via MANY line
    #   breaks; only whole words / hook / proper nouns may never split)
    # Official emote sticker as the cover subject (Ivan 2026-07-19).  "" = the
    # default character redraw.  Only the strong-reason judge may set these
    # (never the deterministic baseline); "replace" swaps the subject, and
    # "companion" (sticker AND character) is reserved for 分身 memes or a
    # sticker depicting kmx (kimo熊 — her FANS' name, not a mascot).
    emote_id: str = ""
    emote_mode: str = ""    # "" | "replace" | "companion"
    emote_reason: str = ""
    # 2026-07-20 B站生态调研（李豆沙/南町/礼墨圈 20万+ 播放封面）：高播放封面
    # 的字是 2-12 字的"梗字"（她的原话/质问/反差点），从不是整条标题。
    # cover_punch 非空时叠字层只渲染它：第 1 行=主梗字（整行 hook 色、巨大），
    # 可选第 2 行副字（奶油色小一号）；整段 cover_text 退为 fallback。
    # 只有自动标题允许（Ivan 手定标题的封面仍走"每个成分都不许丢"的旧铁律，
    # 见 2026-07-06 22966160 案）；歌切 song-clean 永远不用（裸《歌名》已是终态）。
    cover_punch: tuple[str, ...] = ()


_COVER_TALK_LAYOUTS = ("left-split", "right-split", "banner")
_COVER_SONG_LAYOUT = "song-clean"
_COVER_BASE_FILL = (255, 246, 214)  # cream #FFF6D6 — approved base fill
_COVER_STROKE = (18, 36, 79)        # navy  #12244F — approved outer stroke
_COVER_WHITE = (255, 255, 255)
# Hook/accent colors rotate — NOT only yellow/pink (Ivan 2026-07-04).  All are
# SATURATED (never the cream base fill, else the hook word would be invisible).
_COVER_HOOK_COLORS = {
    "yellow": (255, 198, 41),
    "pink": (255, 92, 138),
    "purple": (150, 106, 245),
    "blue": (58, 141, 237),
    "orange": (255, 140, 60),
    "red": (233, 69, 69),
}
_COVER_BG_BUSY = (
    # These are deliberately different *visual families*, not three synonyms
    # for the same blue comic background.  The deterministic batch rotation
    # below owns this axis so a fashionable LLM preference cannot collapse a
    # whole day's covers back onto one template.
    "cobalt-comic-burst",
    "warm-scrapbook-collage",
    "violet-neon-stage",
    "mint-doodle-stickers",
    "mono-manga-panels",
    "coral-checker-pop",
)  # talk
_COVER_BG_CALM = ("soft-radial", "clean-scenic")                    # song
_COVER_BG_PHRASES = {
    "cobalt-comic-burst": (
        "a high-energy cobalt-and-navy comic-book burst with lemon-yellow accents, "
        "radial speed lines, coarse halftone shadows and a few sharp starbursts; "
        "the dominant palette must be cobalt/navy/yellow"
    ),
    "warm-scrapbook-collage": (
        "a warm handmade scrapbook collage in coral, peach, cream and dark forest-green, "
        "with torn-paper layers, masking-tape shapes and hand-cut blank stickers; "
        "no blue-dominant comic burst and no written marks"
    ),
    "violet-neon-stage": (
        "a sleek night-stage visual in deep plum and near-black with vivid magenta and cyan "
        "neon rim lights, glowing arcs and soft lens bokeh; polished luminous depth, "
        "not halftone comic art"
    ),
    "mint-doodle-stickers": (
        "a playful pastel sticker-board in mint, turquoise, warm cream and tangerine, "
        "with rounded doodle blobs, tiny flower and paw-print shapes and layered blank stickers; "
        "soft flat shapes, not a radial comic burst"
    ),
    "mono-manga-panels": (
        "a bold editorial manga-panel design in ink black, warm ivory and one vermilion-red accent, "
        "with angular panel blocks, dry-brush textures and dramatic high contrast; "
        "keep the character naturally colored while the graphic field stays mostly monochrome"
    ),
    "coral-checker-pop": (
        "a cheerful retro magazine-pop composition in coral, brick red, pale aqua and mustard, "
        "using large checkerboard blocks and clean Memphis-style circles and arches; "
        "flat geometric design, no blue comic speed lines"
    ),
    # Legacy keys remain renderable for old committed metadata and fixtures.
    "pop-art-burst": "an energetic pop-art comic background — radiating burst/speed lines, halftone dots, scattered sparkles and little stars, filling the frame",
    "halftone-dots": "a vivid halftone dot-pattern background with a few bold stars and soft sparkles, filling the frame",
    "speed-lines": "a dynamic comic speed-line / radial motion background with halftone shading and sparkles, filling the frame",
    "soft-radial": "a soft radial glow background with gentle bokeh and a few sparkles, calm and uncluttered",
    "clean-scenic": "a clean dreamy scene — a starry night sky with a crescent moon, soft bokeh and a few floating music notes, low-detail and uncluttered",
}
# Expression guardrail (Ivan — 表情永不吐舌头, never 油滑/挑衅/sexy).  Match bad
# PHRASES, not bare "tongue" (else a benign "no tongue" would be rejected); the
# global no-tongue rule is enforced unconditionally in _lidousha_cover_prompt.
_COVER_FORBIDDEN_EXPR = (
    "tongue out", "tongue-out", "tongue sticking", "sticking tongue", "stick out her tongue",
    "licking", "sexy", "seductive", "挑衅", "provocative", "油滑", "媚", "cleavage", "flirt", "吐舌",
)

# Title-keyword → in-character role/expression/background.  DEFAULT is soft/cute
# 清纯邻家女同学; 机灵/得意 is SECONDARY (only when the clip role calls for it).
# Role keywords may choose the face, but never the talk-lane visual family:
# otherwise every "惊讶" clip becomes the same speed-line cover again.
_COVER_ROLE_LEXICON: tuple[tuple[tuple[str, ...], str, str, str | None], ...] = (
    (("破防", "害怕", "好可怕", "吓", "怕", "惊", "傻眼", "？！", "!？", "遇到"), "shocked_bites_back",
     "wide-eyed startled gasp, mouth open in surprise, flushed cheeks, hands drawn up near her face, scared-but-cute", None),
    (("哭", "眼泪", "又哭", "哭哭"), "teary_cute",
     "big welling teary eyes, a cute comedic about-to-cry frown, blush, sniffly", None),
    (("拆台", "反杀", "反怼", "玩梗", "一眼AI", "得意", "整活", "谐音", "反沙", "嘴瓢", "掏兜", "买弹幕", "自封"), "witty_smug",
     "clever pleased closed-mouth grin, one eyebrow slightly raised, a little smug but cute", None),
    (("嘴硬", "澄清", "不是", "嘴犟", "才不"), "stubborn_pout",
     "pouty defiant frown, puffed cheeks, cute-stubborn hmph, arms-crossed energy", None),
    (("吃醋", "你只能", "占有", "醋"), "jealous_pout",
     "jealous puffed-cheek pout, small knit brows, clingy-cute possessive look", None),
    (("一本正经", "犯傻", "歪理", "认真", "讲道理"), "earnest_silly",
     "earnest deadpan serious face, flat calm eyes, taking herself absurdly seriously", None),
    (("看傻", "离谱", "越看越", "当场看", "越整越", "奇遇", "猴群", "见猴", "第一次见"), "dumbstruck",
     "dumbstruck frozen face, wide round sparkly eyes, small O-shaped open mouth, hands near chin", None),
    (("哄睡", "晚安", "温柔", "细声"), "tender_soft",
     "tender warm soft-smiling face, gentle half-lidded caring eyes, soothing", None),
)
_COVER_HOOK_LEXICON = (
    "反沙", "反杀", "拆台", "一群猴", "翻车", "破防", "看傻", "清唱", "一眼AI", "嘴硬", "吃醋", "哄睡",
    "犯傻", "离谱", "掏兜", "买弹幕", "回扣", f"反{CHANNEL_PROFILE.display_name}", "海王", "认输", "自封", "妈妈", "宝宝", "破大防",
    "猴群", "奇遇", "熊猫头",
)


def _cover_stable_hash(seed: str) -> int:
    return int(hashlib.sha256((seed or PROFILE_ID).encode("utf-8")).hexdigest(), 16)


def _cover_role_from_title(title: str, cover_text: str) -> tuple[str, str, str | None]:
    hay = f"{title} {cover_text}"
    for keywords, role, expr, bg in _COVER_ROLE_LEXICON:
        if any(keyword in hay for keyword in keywords):
            return role, expr, bg
    return (
        "shy_cute_default",
        "soft cute girl-next-door, shy but spirited, small closed-mouth smile, gentle blush",
        None,
    )


def _cover_default_hook_word(cover_text: str) -> str:
    # A 《song name》is the strongest hook and must stay whole on its own line
    # (Ivan 2026-07-05: 歌名不能换行). Highlight the whole 《...》.
    song = re.search(r"《[^》]*》", cover_text)
    if song:
        return song.group()
    flat = cover_text.replace("\n", "")
    for word in _COVER_HOOK_LEXICON:
        if word in flat:
            return word
    # explicit clause break (colon→newline) → highlight the last clause; a single
    # unbroken line with no lexicon hook gets NO forced highlight (don't paint the
    # whole title, which would collide with wrapping and read as monochrome).
    lines = [line.strip() for line in cover_text.splitlines() if line.strip()]
    return lines[-1] if len(lines) >= 2 else ""


def _lidousha_is_song_title(title: str) -> bool:
    return title.strip().startswith(CHANNEL_PROFILE.song_title_prefix.rstrip("，, "))


_COVER_PUNCH_CLAUSE_RX = re.compile(r"[^，,。；;：:…！!？?\n]+[！!？?]")
_COVER_PUNCH_QUOTED_RX = re.compile(r"[“‘「『]([^”’」』]{4,12})[”’」』]")
# Short quoted catchphrases are semantic and visual atoms on a cover.  The
# July 22 fallback split ``“最最最喜欢”`` in half, which looked like a typo even
# though every character survived.  Paired short quotes must stay together.
_COVER_QUOTED_SPAN_RX = re.compile(r"[“‘「『][^”’」』\n]{1,8}[”’」』]")


def _cover_default_punch(cover_text: str) -> tuple[str, ...]:
    """Deterministic ecosystem-style punch fallback chain.

    2026-07-20 调研共识：高播放封面字=短梗字。2026-07-21 二期实测 LLM 对
    cover_punch 过度保守（8 条里 5 条给 null），兜底从单一「！/？短句」扩成
    四级链——①最后一个 ≤12 字 ！/？ 分句（点睛尾）；②引号内 4-12 字梗词
    （"为礼墨做0.6"/"难绷小视频"这类通常就是本条的梗）；③最后一个 4-12 字
    普通分句；④词库钩子词。全空才返回 ()（screenshot 路线届时回落 CPA 重绘，
    绝不把整题叠上未为文字区构图的截图）。
    """

    flat = cover_text.replace("\n", "，")
    bang_clauses = [
        m.group().strip() for m in _COVER_PUNCH_CLAUSE_RX.finditer(flat)
    ]
    bang_clauses = [c for c in bang_clauses if 3 <= len(c) <= 12]
    if bang_clauses:
        return (bang_clauses[-1],)
    quoted = _COVER_PUNCH_QUOTED_RX.findall(flat)
    if quoted:
        return (quoted[-1].strip(),)
    clauses = [c.strip() for c in re.split(r"[，,。；;：:…！!？?]", flat) if c.strip()]
    for clause in reversed(clauses):
        if 4 <= len(clause) <= 12:
            return (clause,)
    hook = _cover_default_hook_word(cover_text)
    if hook and "\n" not in hook and 2 <= len(hook) <= 12:
        return (hook,)
    return ()


def _validated_cover_punch(value: object, cover_text: str) -> tuple[str, ...]:
    """Accept an LLM cover punch only when provably source-bound.

    main 必填、sub 可选；每行都必须是 cover_text 的逐字连续片段（忽略布局空
    白），2-12 字，行首禁闭标点/行末禁开标点。任何不合格 → () → 调用方回退
    确定性兜底。防的是 LLM 编造封面字（字幕/标题同源的真实性铁律）。
    """

    if not isinstance(value, Mapping):
        return ()
    haystack = _cover_lines_canon(cover_text)
    lines: list[str] = []
    for key in ("main", "sub"):
        raw = value.get(key)
        if raw is None:
            if key == "main":
                return ()
            continue
        if not isinstance(raw, str):
            return ()
        fragment = raw.strip()
        canon = _cover_lines_canon(fragment)
        if not (2 <= len(canon) <= 12) or "\n" in fragment:
            return ()
        if canon not in haystack:
            return ()
        if fragment.startswith(tuple(_COVER_CLOSING_PUNCT)) or fragment.endswith(
            tuple(_COVER_OPENING_PUNCT)
        ):
            return ()
        lines.append(fragment)
    if len(lines) == 2 and lines[0] == lines[1]:
        lines = lines[:1]
    return tuple(lines)


def _lidousha_cover_art_direction(
    *,
    candidate_id: str,
    title: str,
    cover_text: str,
    art_direction_llm_call: LlmCall | None = None,
    emote_library: EmoteLibrary | None = None,
    allow_punch: bool = False,
    diversity_slot: int | None = None,
) -> LidoushaCoverArtDirection:
    """Pick the cover's role/expression/background/layout/hook color.

    Deterministic baseline first (persona keyword lexicon + a stable per-clip
    hash for anti-monotony rotation), then optional CPA-judge refinement that is
    fail-OPEN and guard-railed (never tongue-out/油滑/sexy).  The cover IMAGE
    stays fail-closed elsewhere; only art-direction degrades gracefully.
    """

    is_song = _lidousha_is_song_title(title)
    # 歌切封面永远裸《歌名》banner，梗字模式只属于自动谈话封面。
    allow_punch = allow_punch and not is_song
    digest = _cover_stable_hash(candidate_id or title)
    hook_keys = list(_COVER_HOOK_COLORS)
    valid_diversity_slot = (
        isinstance(diversity_slot, int)
        and not isinstance(diversity_slot, bool)
        and diversity_slot >= 0
    )
    hook_color = hook_keys[
        (diversity_slot if valid_diversity_slot else digest // 31) % len(hook_keys)
    ]
    if is_song:
        layout = _COVER_SONG_LAYOUT
        role = "gentle_song"
        expression_en = "gentle serene face, eyes softly closed or half-lidded, singing calmly with a faint tender smile"
        background_style = _COVER_BG_CALM[digest % len(_COVER_BG_CALM)]
    else:
        layout = _COVER_TALK_LAYOUTS[
            (diversity_slot if valid_diversity_slot else digest)
            % len(_COVER_TALK_LAYOUTS)
        ]
        role, expression_en, forced_bg = _cover_role_from_title(title, cover_text)
        background_style = forced_bg if forced_bg is not None else _COVER_BG_BUSY[
            (diversity_slot if valid_diversity_slot else digest // 7)
            % len(_COVER_BG_BUSY)
        ]
    baseline = LidoushaCoverArtDirection(
        role=role,
        expression_en=expression_en,
        background_style=background_style,
        layout=layout,
        hook_color=hook_color,
        is_song=is_song,
        hook_word=_cover_default_hook_word(cover_text),
        cover_punch=_cover_default_punch(cover_text) if allow_punch else (),
    )
    if art_direction_llm_call is None:
        return _punch_layout_override(baseline)
    try:
        payload = extract_json_object(
            art_direction_llm_call(
                _cover_art_direction_prompt(
                    title=title,
                    cover_text=cover_text,
                    baseline=baseline,
                    emote_library=emote_library,
                    allow_punch=allow_punch,
                )
            )
        )
        return _punch_layout_override(
            _normalize_cover_art_direction(
                payload, baseline, cover_text, emote_library=emote_library, allow_punch=allow_punch
            )
        )
    except Exception:
        return _punch_layout_override(baseline)


def _punch_layout_override(direction: LidoushaCoverArtDirection) -> LidoushaCoverArtDirection:
    """梗字封面强制 banner 文字区（2026-07-20 生态调研）。

    高播放封面的大字横贯全宽——窄边栏 zone 里 9-10 字的梗字只能到 ~80px，比
    整段文案还小，梗字的意义就没了；banner 宽区里同样的字直接翻倍。CPA 构图
    prompt 与叠字 zone 都跟着 layout 走，所以必须在艺术指导终态统一改。
    """

    if direction.cover_punch and direction.layout != "banner" and not direction.is_song:
        return dataclass_replace(direction, layout="banner")
    return direction


def _cover_art_direction_prompt(
    *,
    title: str,
    cover_text: str,
    baseline: LidoushaCoverArtDirection,
    emote_library: EmoteLibrary | None = None,
    allow_punch: bool = False,
) -> str:
    persona = profile_asset_text("persona")
    # Sticker catalog only when a library is supplied AND this is a talk cover:
    # song covers never use emotes, and the no-library prompt stays byte-stable.
    emote_block = ""
    emote_output_field = ""
    if emote_library and not baseline.is_song:
        emote_block = emote_catalog_prompt_block(emote_library)
        emote_output_field = ',"emote":null|{"id":"...","mode":"replace"|"companion","reason":"..."}'
    # 梗字轴只对自动标题开放（手定标题封面守"成分不许丢"铁律，prompt 保持字节稳定）。
    punch_block = ""
    punch_output_field = ""
    if allow_punch:
        punch_block = (
            "- cover_punch: **封面主梗字（最高优先，2026-07-20 B站高播放封面调研铁律：封面上的字是 2-12 字的'梗字'，"
            "从不是整条标题）**。从封面文案里挑她最出圈的那一句：原话/口癖/质问/反差点"
            "（参考同类高播放封面：'什么是直女''给我整无语了''我是侄女啊'这种）。\n"
            "  硬约束：main 必须是封面文案里的**逐字连续片段**（不加/不减/不改字，可含标点），2-12 字；"
            "sub 可选（null 或第二行 2-12 字小字补语境，同样必须是文案原文片段）。"
            "**几乎永远都选得出来**——按优先级找：她的原话感叹句＞引号里的梗词＞最后一个短分句；"
            "只有文案完全不存在 2-12 字连续片段时才允许 main 给 null（极罕见）。梗字模式下 lines/words 仍要照常输出（作回退）。\n"
        )
        punch_output_field = ',"cover_punch":null|{"main":"...","sub":null|"..."}'
    return (
        f"你在为一条{CHANNEL_PROFILE.display_name}(B站虚拟主播)切片的封面挑选'艺术指导'。只依据人设与本条切片语义选择。\n"
        f"\n{CHANNEL_PROFILE.display_name}人设(权威):\n{persona}\n"
        "\n硬护栏:表情要贴这条切片里她扮演的角色;默认是软糯清纯邻家女同学(被欺负又软软反击);"
        "机灵鬼怪/得意只在角色需要时用(次要);**永远不要吐舌头**,不要油滑/挑衅/性感/媚。"
        "外观由参考帧决定,你不描述服装。\n"
        f"\n本切片标题: {title}\n封面文案(分行): {cover_text}\n"
        "\n系统已经为本条锁定下列三个抗同质化轴，禁止改动；它们由跨切片稳定轮换决定，而不是语义裁判决定:\n"
        f"- layout: {baseline.layout}\n"
        f"- background_style: {baseline.background_style}\n"
        f"- hook_color: {baseline.hook_color}\n"
        "\n请只选择/生成以下语义轴:\n"
        + punch_block +
        "- role: 一个简短英文角色键(如 shy_cute_default/shocked_bites_back/witty_smug/tender_soft/gentle_song)\n"
        "- expression_en: 一句英文脸部表情(贴角色,不吐舌)\n"
        "- hook_word: 封面文案里最该高亮的一个词(必须是文案里出现的原词)\n"
        "- words: 封面文案的**完整词组切分**(字符串数组)。硬约束:按顺序拼接后与封面文案一字不差(不加/不减/不改字);"
        "每个自然词语(如\"传话员\"\"熊猫头\"\"不言而喻\")、专名(礼墨Sumi/kmx)、《歌名》和 hook_word 各自必须整体是一个元素(或完整包含在一个元素里);"
        "标点跟在前一个词的元素末尾。分行器用它保证**换行永远不拆词**——除词以外任何位置都允许换行。\n"
        "- lines: 封面文案的分行方案(字符串数组)。硬约束:按顺序拼接后与封面文案一字不差(不加/不减/不改字);"
        "**绝不把一个词拆到两行**(如\"拒绝\"\"熊猫\"\"礼墨\"这类词必须整词同行);《歌名》和 hook_word 必须完整待在同一行。\n"
        "  **封面字要尽量大、填满文字区,且必须保留完整文案(这是硬要求,Ivan 反复强调:字不能小,也绝不许丢字——放大靠多换行)**:"
        "字号由最长一行的宽度决定,所以行要短。"
        "left-split/right-split/song-clean 这类竖窄文字区**必须多分几行、每行更短**(长文案 5-8 行,每行 2-4 字),"
        "让文字铺满整个竖直文字区;banner 是横宽区,行可以长一点(3-4 行)。宁可多一行也不要留一行太长把字压小。\n"
        + emote_block
        + '只输出一个 JSON 对象: {"role":"...","expression_en":"...","hook_word":"...","words":["...","..."],"lines":["...","..."]'
        + punch_output_field
        + emote_output_field
        + "}"
    )


def _cover_lines_canon(text: str) -> str:
    """Canonical form for comparing a line split against the cover text.

    Only layout whitespace may disappear.  Punctuation is visible title
    content: accepting a split that drops ``？`` or moves ``，`` onto a lonely
    line produced a visibly broken July 10 cover despite a hash-clean package.
    """
    return re.sub(r"\s+", "", text)


def _validated_cover_lines(value: object, cover_text: str, *, hook_word: str, max_lines: int) -> tuple[str, ...]:
    """Accept an LLM line split only when it is provably lossless and renderable:
    same characters in the same order, 《song》 and the hook word intact within a
    single line, sane line count/length.  Anything else → () → balancer fallback
    (word-blind, but never worse than before)."""
    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= max_lines):
        return ()
    lines = []
    closing_punctuation = tuple("，,、；;！!？?。）》】”’")
    opening_punctuation = tuple("（(《【“‘")
    for item in value:
        if not isinstance(item, str):
            return ()
        line = item.strip()
        if not line or len(line) > 12:
            return ()
        if line.startswith(closing_punctuation) or line.endswith(opening_punctuation):
            return ()
        lines.append(line)
    if _cover_lines_canon("".join(lines)) != _cover_lines_canon(cover_text):
        return ()
    for atom in [
        *re.findall(r"《[^》]*》", cover_text),
        *_COVER_QUOTED_SPAN_RX.findall(cover_text),
        *([hook_word] if hook_word else []),
    ]:
        if atom and not any(atom in line for line in lines):
            return ()  # a song name / quoted catchphrase / hook must stay whole
    return tuple(lines)


def _validated_cover_words(value: object, cover_text: str, *, hook_word: str) -> tuple[str, ...]:
    """Accept an LLM word segmentation only when it is provably lossless: same
    characters in the same order, every 《song》 and the hook word intact inside
    a single element.  These become wrap ATOMS (Ivan 2026-07-10: the full title
    stays on the cover, the font grows via MANY line breaks, and a break may
    fall anywhere EXCEPT inside a word / hook / proper noun).  Anything invalid
    → () → the balancer falls back to hook/《song》/ASCII atoms only."""
    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= 40):
        return ()
    words = []
    closing_punctuation = tuple("，,、；;！!？?。）》】”’")
    opening_punctuation = tuple("（(《【“‘")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return ()
        word = item.strip()
        if len(word) > 12:
            return ()
        if word.startswith(closing_punctuation) or word.endswith(opening_punctuation):
            return ()
        words.append(word)
    if _cover_lines_canon("".join(words)) != _cover_lines_canon(cover_text):
        return ()
    for atom in [
        *re.findall(r"《[^》]*》", cover_text),
        *_COVER_QUOTED_SPAN_RX.findall(cover_text),
        *([hook_word] if hook_word else []),
    ]:
        if atom and not any(atom in word for word in words):
            return ()
    return tuple(words)


def _normalize_cover_art_direction(
    payload: Mapping[str, object],
    baseline: LidoushaCoverArtDirection,
    cover_text: str,
    emote_library: EmoteLibrary | None = None,
    allow_punch: bool = False,
) -> LidoushaCoverArtDirection:
    # These axes are the deterministic anti-monotony schedule.  The LLM may
    # refine semantic choices below, but may not collapse a whole batch back
    # onto one fashionable layout/background/color.
    layout = baseline.layout
    hook_color = baseline.hook_color
    background_style = baseline.background_style

    expression_en = payload.get("expression_en")
    if not (isinstance(expression_en, str) and expression_en.strip()) or any(
        bad in expression_en.lower() for bad in _COVER_FORBIDDEN_EXPR
    ):
        expression_en = baseline.expression_en  # guardrail: reject tongue/油滑/sexy → safe baseline

    role = payload.get("role")
    role = role.strip() if isinstance(role, str) and role.strip() else baseline.role

    hook_word = payload.get("hook_word")
    if not (isinstance(hook_word, str) and hook_word and hook_word in cover_text.replace("\n", "")):
        hook_word = baseline.hook_word

    # Word-aware line split (Ivan 2026-07-06: 均衡分行器把"拒绝/熊猫"拆到两行) —
    # validated against the FINAL layout's line budget and the FINAL hook word.
    # Colon-derived explicit breaks ("\n" in cover_text) stay authoritative and
    # are handled in _fit_cover_lines; the LLM split only fills the no-colon case.
    max_lines = _COVER_LAYOUT_RENDER.get(layout, _COVER_LAYOUT_RENDER["left-split"])["max_lines"]
    line_breaks = _validated_cover_lines(payload.get("lines"), cover_text, hook_word=hook_word, max_lines=max_lines)
    words = _validated_cover_words(payload.get("words"), cover_text, hook_word=hook_word)

    # Emote stickers are opt-in with a strong articulated reason; anything short
    # of a valid pick (unknown id/mode, bare reason, song, no library) falls
    # back to the default character redraw — the sticker can never be forced.
    emote_id, emote_mode, emote_reason = normalize_emote_choice(
        payload.get("emote"),
        library=emote_library or EmoteLibrary(enabled=False, entries=()),
        is_song=baseline.is_song,
    )

    # 梗字：LLM 选中且逐字可溯 → 用它；null/不合格 → 确定性兜底（baseline）。
    cover_punch = baseline.cover_punch
    if allow_punch:
        validated_punch = _validated_cover_punch(payload.get("cover_punch"), cover_text)
        if validated_punch:
            cover_punch = validated_punch

    return LidoushaCoverArtDirection(
        role=role,
        expression_en=expression_en,
        background_style=background_style,
        layout=layout,
        hook_color=hook_color,
        is_song=baseline.is_song,
        hook_word=hook_word,
        line_breaks=line_breaks,
        words=words,
        emote_id=emote_id,
        emote_mode=emote_mode,
        emote_reason=emote_reason,
        cover_punch=cover_punch,
    )


def _lidousha_cover_text(title: str) -> str:
    # Cover title NEVER uses a colon (Ivan 2026-07-04): the archive/video title
    # may use "引语：反应", but on the cover the clause break is a LINE BREAK,
    # not punctuation. Strip the selected profile's talk/song prefix and turn any colon
    # into a newline so the overlay splits clauses by line.
    text = title.strip()
    for prefix in (CHANNEL_PROFILE.song_title_prefix, CHANNEL_PROFILE.talk_title_prefix):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    # 歌名的括号限定（场次/晚会）只留在归档标题里；封面上《歌名》必须短而大
    # （7/11 歌切封面被《恋爱告急 (2021浙江卫视跨年演唱会)》单原子压到 46px 保底）。
    text = re.sub(r"《([^》（(]*?)\s*[（(][^()（）》]*[)）]\s*》", r"《\1》", text)
    text = re.sub(r"\s*[：:]\s*", "\n", text)
    text = "\n".join(line.strip(" ，,") for line in text.split("\n") if line.strip(" ，,"))
    return text or title.strip()


def _lidousha_identity_descriptor() -> str:
    """Pull the selected host's visual identity descriptors from persona.md.

    The reference frame anchors identity, but CPA images.edit drifts without an
    explicit character description, so we inject the persona 身份/形象 lines
    verbatim (authoritative Chinese descriptors) alongside an English gloss.
    """

    persona = profile_asset_text("persona")
    descriptors: list[str] = []
    for line in persona.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith(("身份", "形象")):
            descriptors.append(stripped)
    return " ".join(descriptors)


_COVER_NO_TEXT_CRITICAL = (
    "CRITICAL — render ABSOLUTELY NO text of any kind: no letters, words, Chinese/Japanese/English "
    "characters, numbers, watermark, logos, UI, subtitles, or comic 'POW'/speech-bubble text anywhere. "
    "The title is added separately afterwards, so the reserved title area must be a graphic background that is "
    "COMPLETELY EMPTY of any glyphs or symbols. Keep the whole composition energetic, cute and eye-catching."
)


def _lidousha_emote_cover_prompt(
    *,
    emote: EmoteEntry,
    art_direction: LidoushaCoverArtDirection,
    background: str,
    cover_identity_prompt: str,
) -> str:
    """Replace-mode prompt: the official emote sticker IS the cover subject.

    The sticker replaces the live-frame character redraw one-for-one, so it
    follows the same rules: it owns most of the frame (same layout zones), gets
    only a LIGHT redraw (polish + background integration — Ivan: 表情包也可以
    重绘,但不能太过), and the title area stays text-free for the local overlay.
    """

    if emote.subject == "panda_creature":
        subject_line = (
            "The sticker shows her fluffy PANDA-CREATURE mascot form, NOT the human girl — keep it a cute round "
            "panda creature exactly as drawn; do NOT humanize it and do NOT add any human character. "
        )
    else:
        subject_line = f"IDENTITY (keep her instantly recognizable): {cover_identity_prompt} "
    caption_line = ""
    if emote.baked_text:
        caption_line = (
            f"The original sticker has the caption text “{emote.baked_text}” baked into the art — OMIT that "
            "caption completely and draw the character only; its emotion must read from the pose and face alone. "
        )
    identity_block = (
        f"Create a bold 16:9 (1920x1080) anime VTuber livestream cover thumbnail for {CHANNEL_PROFILE.prompt_name}. "
        "The supplied image is one of her OFFICIAL chibi emote stickers — for this clip the sticker character is "
        "the COVER SUBJECT, used INSTEAD of her regular half-body portrait. "
        "STICKER FIDELITY (redraw lightly, never reinvent): keep the sticker's EXACT pose, expression, emotion, "
        "proportions, hairstyle, outfit, props and accessories exactly as shown — do NOT restyle or redesign it, "
        "do NOT add hats/accessories/extra props, do NOT change or exaggerate the emotion; only polish it (crisp "
        "clean linework, smooth vivid shading, high resolution) and integrate it into the new background so it "
        "still reads instantly as the same official sticker. "
        + subject_line
        + caption_line
        + "The character must NEVER stick its tongue out — no tongue showing; never provocative or sexy. "
        "FEED-SAFE FRAMING: keep the sticker character's FACE and all key features within the central 4:3 portion "
        "of the frame — feed thumbnails crop the outer ~13% of the width on EACH side, so place nothing important "
        "in the far-left or far-right edges; those edges may hold only background. "
    )
    layout = art_direction.layout
    if layout == "left-split":
        composition = (
            "COMPOSITION: place the redrawn sticker character LARGE, filling the LEFT ~55% of the frame and most "
            "of its height, big and expressive, with a clean white sticker-style outline so it pops off the "
            f"background. The RIGHT ~45% is an empty graphic zone reserved for a title (keep the sticker out of "
            f"it): fill it and the whole frame with {background}. Minimal empty space, high energy. "
        )
    elif layout == "banner":
        composition = (
            "COMPOSITION: place the redrawn sticker character LARGE in the LOWER-CENTER, head around the middle "
            "of the frame, with a clean white sticker outline. Keep the TOP ~40% a clear vibrant band reserved "
            f"for a big title. Fill the whole frame with {background}. Minimal empty space. "
        )
    else:  # right-split (and the defensive song-clean case: subject right, text left)
        composition = (
            "COMPOSITION: place the redrawn sticker character LARGE, filling the RIGHT ~55% of the frame and most "
            "of its height, big and expressive, with a clean white sticker-style outline so it pops off the "
            f"background. The LEFT ~45% is an empty graphic zone reserved for a title (keep the sticker out of "
            f"it): fill it and the whole frame with {background}. Minimal empty space, high energy. "
        )
    return identity_block + composition + _COVER_NO_TEXT_CRITICAL


def _lidousha_cover_prompt(
    *,
    title: str,
    cover_text: str,
    art_direction: LidoushaCoverArtDirection | None = None,
    emote: EmoteEntry | None = None,
) -> str:
    """Text-free CPA background prompt, ART-DIRECTED per clip (Ivan 2026-07-04).

    Identity stays anchored (panda/小李/熊猫, from persona.md) but the OUTFIT/skin
    is deferred to the per-clip reference frame — she wears different costumes on
    different streams.  Layout/expression/background follow ``art_direction``; the
    title is overlaid locally so this prompt forbids any rendered text.  She must
    NEVER stick her tongue out.

    ``emote`` (Ivan 2026-07-19): when the art direction carries a strong-reason
    sticker pick, "replace" swaps the subject to the sticker (mutually exclusive
    with the character redraw) and "companion" keeps the character but adds the
    sticker as a secondary element (分身 / depicting her fans kmx).  ``emote=None`` keeps
    this prompt byte-identical to the pre-emote contract.
    """
    if art_direction is None:
        art_direction = _lidousha_cover_art_direction(candidate_id="", title=title, cover_text=cover_text)
    identity_descriptor = _lidousha_identity_descriptor()
    cover_identity_prompt = profile_asset_text("cover_identity_prompt")
    background = _COVER_BG_PHRASES.get(
        art_direction.background_style,
        _COVER_BG_PHRASES[_COVER_BG_BUSY[0]],
    )
    emote_mode = art_direction.emote_mode if emote is not None else ""
    if emote_mode == "replace":
        return _lidousha_emote_cover_prompt(
            emote=emote,
            art_direction=art_direction,
            background=background,
            cover_identity_prompt=cover_identity_prompt,
        )
    identity_block = (
        f"Create a bold 16:9 (1920x1080) anime VTuber livestream cover thumbnail for {CHANNEL_PROFILE.prompt_name}. "
        "Use the supplied image ONLY as identity/style reference. "
        f"IDENTITY (keep her instantly recognizable): {cover_identity_prompt} "
        f"Persona identity descriptors (Chinese, authoritative): {identity_descriptor} "
        "PRESERVE THE EXACT OUTFIT, skin tone, hairstyle and accessories shown in the reference frame — she wears "
        "DIFFERENT costumes on different streams, so do NOT invent or lock a fixed costume; copy what the reference shows. "
        f"EXPRESSION (must fit her in-character role for this clip): {art_direction.expression_en}. "
        "Her mouth may be open for a gasp/shout/laugh but she must NEVER stick her tongue out — no tongue showing; "
        "never look sly beyond cute, never provocative or sexy. "
        "FEED-SAFE FRAMING: keep her FACE and all key features within the central 4:3 portion of the frame — feed "
        "thumbnails crop the outer ~13% of the width on EACH side, so place nothing important (face, hands, key props) "
        "in the far-left or far-right edges; those edges may hold only background. "
    )
    # 2026-07-21 Ivan 批准加大脸部占比（B站 20万+ 播放封面共性：脸占画面 50-90%）：
    # talk 三版式从 chest-up 半身收紧到 head-and-shoulders 特写，脸≈画面高 1/3+。
    layout = art_direction.layout
    if layout == "left-split":
        composition = (
            "COMPOSITION: draw her as a VERY LARGE head-and-shoulders CLOSE-UP filling the LEFT ~55% of the frame — "
            "camera close, her FACE alone spans roughly a THIRD of the frame height, bold and expressive, "
            "with a clean white sticker-style outline so she pops off the background. "
            f"The RIGHT ~45% is an empty graphic zone reserved for a title (keep her body out of it): fill it and the "
            f"whole frame with {background}. Minimal empty space, high energy. "
        )
    elif layout == "right-split":
        composition = (
            "COMPOSITION: draw her as a VERY LARGE head-and-shoulders CLOSE-UP filling the RIGHT ~55% of the frame — "
            "camera close, her FACE alone spans roughly a THIRD of the frame height, bold and expressive, "
            "with a clean white sticker-style outline so she pops off the background. "
            f"The LEFT ~45% is an empty graphic zone reserved for a title (keep her body out of it): fill it and the "
            f"whole frame with {background}. Minimal empty space, high energy. "
        )
    elif layout == "banner":
        composition = (
            "COMPOSITION: place her as a VERY LARGE head-and-shoulders CLOSE-UP in the LOWER-CENTER — camera close, "
            "her FACE alone spans roughly a THIRD of the frame height, "
            "with a clean white sticker outline. Keep the TOP ~40% a clear vibrant band reserved for a big title. "
            f"Fill the whole frame with {background}. Minimal empty space. "
        )
    else:  # song-clean
        composition = (
            "COMPOSITION: draw her as a soft chest-up portrait on the RIGHT ~55%, optionally holding a microphone, "
            "with a clean gentle look. Keep the LEFT ~45% a CLEAN calm zone reserved for a title: fill it with "
            f"{background}. Cohesive blue / navy / cream palette, tasteful and pretty rather than loud. "
        )
    companion_block = ""
    if emote_mode == "companion" and emote is not None:
        caption_note = (
            f" (omit the sticker's baked caption text “{emote.baked_text}”)" if emote.baked_text else ""
        )
        companion_block = (
            "COMPANION STICKER: the reference image has an INSET panel at its bottom-right corner showing one of "
            "her official chibi emote stickers. Draw that sticker character ONCE as a clearly SECONDARY companion "
            "element beside her — about one third of her size, never covering her face — faithfully preserving the "
            f"sticker's pose, expression and design{caption_note}; do NOT reproduce the inset panel's frame/border "
            f"itself. Reason this companion appears (from the clip): {art_direction.emote_reason}. "
        )
    return identity_block + composition + companion_block + _COVER_NO_TEXT_CRITICAL


def _cover_screenshot_polish_prompt() -> str:
    """截图轻微调 prompt（2026-07-21 Ivan：截图微调以达到更好效果）。

    与全图重绘相反的合同：构图/姿势/表情/取景**逐像素忠实**，只做两类事——
    ①清掉直播 UI 杂物（弹幕框/SC条/歌单字/字幕字/水印）并自然补背景；
    ②画质打磨（压缩噪点/线条/色彩光感）。表情来自真实名场面，所以不注入
    role/expression 轴；no-text 铁律照常（标题仍由本地叠字）。
    """

    return (
        "Restore and enhance this EXACT livestream screenshot for a cover thumbnail. "
        "This is a faithful RETOUCH, not a redraw: keep the character's EXACT pose, facial expression, "
        "face proportions, outfit, framing and overall composition pixel-faithful — do NOT restyle her, "
        "do NOT change or exaggerate the emotion, do NOT move, resize or reinterpret anything. "
        "CLEAN-UP PASS: remove livestream overlay clutter — chat/comment boxes, superchat bars, "
        "song-list text, watermarks, UI panels and any burned-in subtitle text — and reconstruct the "
        "background naturally where they were. "
        "POLISH PASS: fix compression artifacts and banding, crisp clean linework, gently richer color "
        "and lighting, keep it looking like the same real screenshot but pristine. "
        + _COVER_NO_TEXT_CRITICAL
    )


_COVER_CANVAS = (1920, 1080)
# The CPA image gateway (new_api) rejects sizes that aren't positive multiples
# of 16 with HTTP 400 model_price_error — 1080 isn't one (first hit 2026-07-06,
# blanked a whole unattended batch's covers).  Request the nearest compliant
# size and normalize the returned image back onto the 1920x1080 overlay canvas.
_COVER_REQUEST_SIZE = "1920x1088"
_COVER_PRIMARY_MODEL = "gpt-image-2"
_COVER_COMPATIBILITY_MODEL = "gpt-image-1.5"


def _cpa_image_model_candidates() -> tuple[str, ...]:
    preferred = os.environ.get("CPA_IMAGE_MODEL", _COVER_PRIMARY_MODEL).strip()
    if not preferred:
        preferred = _COVER_PRIMARY_MODEL
    if preferred == _COVER_PRIMARY_MODEL:
        return (_COVER_PRIMARY_MODEL, _COVER_COMPATIBILITY_MODEL)
    return (preferred,)


def _explicit_cpa_model_unavailable(status_code: int, raw: str) -> bool:
    """Only explicit routing/model availability errors authorize fallback."""

    if status_code not in {400, 404}:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and error.get("code") == "client_model_unavailable":
            return True
    lowered = raw.lower()
    return "client_model_unavailable" in lowered or "可用渠道不存在" in raw


def _normalize_cover_canvas(path: Path) -> tuple[int, int]:
    """Force the AI background onto the 1920x1080 canvas the text-overlay zones
    assume: scale-to-cover + center-crop (never letterbox/stretch)."""
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        if img.size != _COVER_CANVAS:
            scale = max(_COVER_CANVAS[0] / img.width, _COVER_CANVAS[1] / img.height)
            new_w = max(_COVER_CANVAS[0], int(round(img.width * scale)))
            new_h = max(_COVER_CANVAS[1], int(round(img.height * scale)))
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - _COVER_CANVAS[0]) // 2
            top = (new_h - _COVER_CANVAS[1]) // 2
            img = resized.crop((left, top, left + _COVER_CANVAS[0], top + _COVER_CANVAS[1]))
        img.save(path)
        return img.size


def _call_cpa_image_edit(
    *,
    base_url: str,
    api_key: str,
    reference_path: Path,
    output_path: Path,
    prompt: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float = 180.0,
    model_candidates: Sequence[str] | None = None,
    normalize_canvas: Callable[[Path], tuple[int, int]] | None = None,
) -> dict[str, object]:
    endpoint = f"{base_url}/images/edits"
    candidates = tuple(
        dict.fromkeys(
            model.strip()
            for model in (model_candidates or _cpa_image_model_candidates())
            if isinstance(model, str) and model.strip()
        )
    )
    if not candidates:
        return {
            "status": "FAILED",
            "reason_code": "CPA_IMAGE_EDIT_MODEL_CONFIG_INVALID",
            "detail": "no CPA image model candidate configured",
            "attempted_models": [],
        }
    request_attempts: list[dict[str, object]] = []
    response_attempts: list[dict[str, object]] = []
    reference_sha256 = "sha256:" + _sha256(reference_path)
    reference_bytes = reference_path.read_bytes()
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)

    def persist_evidence() -> None:
        request_path.write_text(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "method": "images.edit",
                    "image_gen_model": "cpa",
                    "prompt": prompt,
                    "reference_image": str(reference_path),
                    "reference_sha256": reference_sha256,
                    "api_key": "<redacted>",
                    "attempts": request_attempts,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        response_path.write_text(
            json.dumps(
                {"attempts": response_attempts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    attempted_models: list[str] = []
    for attempt_index, model in enumerate(candidates):
        attempted_models.append(model)
        request_attempts.append(
            {"attempt": attempt_index + 1, "model": model, "size": _COVER_REQUEST_SIZE}
        )
        persist_evidence()
        try:
            body, content_type = _multipart_form_data(
                fields={"model": model, "prompt": prompt, "size": _COVER_REQUEST_SIZE},
                files={"image": (reference_path.name, reference_bytes, "image/png")},
            )
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": content_type,
                    # the CPA endpoint sits behind Cloudflare, which 403s the
                    # default Python-urllib user agent
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            unavailable = _explicit_cpa_model_unavailable(exc.code, raw)
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "status_code": exc.code,
                    "body_tail": raw[-4000:],
                    "explicit_model_unavailable": unavailable,
                }
            )
            persist_evidence()
            if unavailable and attempt_index + 1 < len(candidates):
                continue
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_HTTP_ERROR",
                "detail": f"HTTP {exc.code}: {raw[-500:]}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        except Exception as exc:
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_REQUEST_FAILED",
                "detail": f"{type(exc).__name__}: {exc}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "status_code": status_code,
                    "body_tail": raw[-4000:],
                }
            )
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_BAD_JSON",
                "detail": raw[-500:],
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        image_record = (
            (payload.get("data") or [{}])[0]
            if isinstance(payload.get("data"), list)
            else {}
        )
        if not isinstance(image_record, Mapping):
            image_record = {}
        redacted_response: dict[str, object] = {
            "attempt": attempt_index + 1,
            "model": model,
            "status_code": status_code,
            "keys": sorted(payload.keys()),
            "data_keys": sorted(image_record.keys()),
        }
        try:
            b64_json = image_record.get("b64_json")
            image_url = image_record.get("url")
            if isinstance(b64_json, str) and b64_json:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(base64.b64decode(b64_json))
                redacted_response["b64_json_bytes"] = len(b64_json)
            elif isinstance(image_url, str) and image_url:
                with urllib.request.urlopen(image_url, timeout=timeout_seconds) as image_response:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(image_response.read())
                redacted_response["url"] = image_url
            else:
                response_attempts.append(redacted_response)
                persist_evidence()
                return {
                    "status": "FAILED",
                    "reason_code": "CPA_IMAGE_EDIT_NO_IMAGE",
                    "detail": "response had no b64_json/url image",
                    "attempted_models": attempted_models,
                    "model_fallback_used": len(attempted_models) > 1,
                }
            redacted_response["canvas"] = list(
                (normalize_canvas or _normalize_cover_canvas)(output_path)
            )
        except Exception as exc:  # noqa: BLE001 — broken images must block
            response_attempts.append(redacted_response)
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_BAD_IMAGE",
                "detail": f"{type(exc).__name__}: {exc}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        redacted_response["output_path"] = str(output_path)
        redacted_response["output_sha256"] = "sha256:" + _sha256(output_path)
        response_attempts.append(redacted_response)
        persist_evidence()
        return {
            "status": "AI_BACKGROUND_READY",
            "output_path": str(output_path),
            "selected_model": model,
            "attempted_models": attempted_models,
            "model_fallback_used": len(attempted_models) > 1,
        }

    raise AssertionError("CPA image model loop exhausted without a result")


def _multipart_form_data(*, fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----HermesVtuberSliceCoverBoundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


_COVER_SCRIM_SIDE = {"color": (8, 16, 44), "alpha": 172, "pad": 48, "feather": 26}
_COVER_SCRIM_BAR = {"color": (8, 16, 44), "alpha": 168, "pad": 44, "feather": 24}
_COVER_SCRIM_SOFT = {"color": (6, 12, 34), "alpha": 140, "pad": 58, "feather": 34}
# per-layout render spec: text-block zone box, tilt, dark card, outline stack.
# song-clean stays at -4.0 (the approved default tilt; keeps the song-cover test
# deterministic) while talk layouts each get their own slight tilt for variety.
# FEED-CROP SAFE ZONE (Ivan 2026-07-05): Bilibili's feed/首页/推荐 center-crops the
# 16:9 cover to ~4:3 (height kept, width 1920→1440, cutting 240px each side); some
# surfaces go to 1:1. Text near the L/R edges gets cut ("下播" was lost). So ALL
# title text must stay inside the central ~1280-wide safe band x∈[320,1600]
# (matches Bilibili's recommended 中央 1280×720 safe area, with buffer over the
# 240px 4:3 crop). Every layout's text zone is clamped to that band.
_COVER_SAFE_X0, _COVER_SAFE_X1 = 260, 1660  # feed 4:3 crop = 240px/side; +20 buffer
_COVER_LAYOUT_RENDER = {
    # zone the text block fills, tilt, dark-card params (unused when backing=outline),
    # max_lines (wrap budget — MORE lines ⇒ shorter lines ⇒ BIGGER font in the narrow
    # half, Ivan's trick), max_size (font cap). Outer edge = feed-safe band 260/1660;
    # inner edge kept off the character; zone made tall so many big lines fit.
    # Line budgets raised 2026-07-10 (Ivan: keep the FULL title, grow the font
    # via MANY line breaks — a 30-49 char title in a narrow side zone needs 6-8
    # short lines to fill it; the old cap of 5 pinned long titles at ~73-105px).
    "left-split": {"zone": (960, 66, 1660, 1014), "angle": -4.0, "scrim": _COVER_SCRIM_SIDE, "max_lines": 8, "max_size": 360},
    "right-split": {"zone": (260, 66, 960, 1014), "angle": -3.0, "scrim": _COVER_SCRIM_SIDE, "max_lines": 8, "max_size": 360},
    "banner": {"zone": (260, 16, 1660, 486), "angle": -2.0, "scrim": _COVER_SCRIM_BAR, "max_lines": 4, "max_size": 360},
    "song-clean": {"zone": (260, 110, 1000, 940), "angle": -4.0, "scrim": _COVER_SCRIM_SOFT, "max_lines": 7, "max_size": 320},
}
_COVER_OUTLINE_NAVY_RATIO = 0.085   # outer stroke ≈ 8.5% of font size (chunky, scales up)
_COVER_OUTLINE_WHITE_RATIO = 0.042
# 最小可读强调字号（Ivan 2026-07-11 河粉封面案：整批 146-182px，唯独它 90px）。
# 字号上限 ≈ zone宽/最宽不可拆原子宽 —— 一个超宽原子（LLM 把 “要交780吗”？
# 整段当一个"词"，hook 又是其中的 780）会把强调行钉死，行数预算再大也救不回。
# fitter 在打包前把任何在 _COVER_MIN_EMPH 下都放不进 zone 的原子按词内安全点
# 再分（hook/《歌名》/ASCII 串不拆；开标点绑后、闭标点绑前，顺带满足
# 行首禁闭标点/行末禁开标点）。
COVER_MIN_TALK_FONT_SIZE = 120
_COVER_MIN_EMPH = COVER_MIN_TALK_FONT_SIZE
_COVER_OPENING_PUNCT = "“‘《〈「『（(【[｛{"
_COVER_CLOSING_PUNCT = "”’》〉」』）)】]｝}，,、；;：:！!？?。…"


def _cover_outlines_for(size):
    """Outline thickness scales WITH the font so big text keeps a chunky border
    (a fixed 20px outline looks thin under 260px text)."""
    outer = max(8, int(round(size * _COVER_OUTLINE_NAVY_RATIO)))
    inner = max(4, int(round(size * _COVER_OUTLINE_WHITE_RATIO)))
    return ((outer, _COVER_STROKE), (inner, _COVER_WHITE))
# Text backing behind the title. Ivan 2026-07-04: the reference covers use NO
# box — the thick navy+white outline alone separates the text from a bright pop
# background (the earlier dark "card" looked like an ugly rectangle and was only
# needed before the white-glyph outline bug was fixed).  "outline" = default,
# clean, reference-accurate.  "glow" = a soft dark halo hugging the glyphs (a
# sticker-shadow, NOT a box) for extra depth.  "card" = the old rounded panel.
_COVER_TEXT_BACKING = "outline"


_COVER_MISSING_CHECKERS: dict = {}


class _CoverFontChoice(NamedTuple):
    """One concrete whole-cover font face: a font file plus a TTC face index."""

    path: Path
    face_index: int = 0

    @property
    def name(self) -> str:
        if self.face_index:
            return f"{self.path.name}#{self.face_index}"
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem


def _cover_fallback_font_path():
    """A cute CJK font used for the WHOLE cover when ZCOOL fails glyph coverage
    (Ivan 2026-07-05: one cover = one uniform font — never mix fonts in a cover).
    得意黑/SmileySans preferred; the chain below adds truly complete tails."""
    candidates = _cover_fallback_font_candidates()
    return candidates[0] if candidates else None


def _cover_fallback_font_candidates() -> list[Path]:
    return [
        candidate
        for candidate in (
            CHANNEL_PROFILE.asset_directory("fonts", repo_root=ROOT) / "SmileySans-Oblique.ttf",
            Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
            CHANNEL_PROFILE.delivery_root_for(ROOT) / "2026-06-29/redone_fullsong_433_travel_meaning/fonts/msyh.ttf",
        )
        if candidate.is_file()
    ]


# Fonts that render specific glyphs as the WRONG SHAPE — a real glyph, not
# .notdef, so the raster probe can't catch them: ZCOOL's 自 comes out looking
# like 白 (自私→白私, 擅自→擅白).  Keyed by font file stem; extend per font as
# more wrong-shape glyphs surface (data-only fix; the title JSON is always
# correct — only the rendered PNG was wrong).
_COVER_WRONG_SHAPE_GLYPHS: dict[str, frozenset[str]] = {
    "ZCOOLKuaiLe-Regular": frozenset("自"),
}


def _noto_cjk_faces(collection_path: Path, *, prefer_jp: bool) -> list["_CoverFontChoice"]:
    """Locate the JP/SC faces inside a Noto CJK .ttc by family name (face order
    is not contractual across distros), regional-correct face first."""
    from PIL import ImageFont

    wanted = (
        ("Noto Sans CJK JP", "Noto Sans CJK SC")
        if prefer_jp
        else ("Noto Sans CJK SC", "Noto Sans CJK JP")
    )
    found: dict[str, int] = {}
    for index in range(12):
        try:
            family = ImageFont.truetype(str(collection_path), 24, index=index).getname()[0]
        except Exception:
            break
        if family in wanted and family not in found:
            found[family] = index
    return [_CoverFontChoice(collection_path, found[name]) for name in wanted if name in found]


def _cover_font_chain(*, prefer_jp: bool) -> list["_CoverFontChoice"]:
    """Whole-cover font candidates, cutest first, completest last.

    2026-07-16 《怪獣の花唄》 published-cover case: ZCOOL lacked 獣, the cover
    swapped to SmileySans, and SmileySans ALSO lacks 獣 — its stylised .notdef
    glyph shipped on a live cover because only ZCOOL was ever glyph-checked.
    Every chain member is now checked, and the tail members are plain but
    complete system CJK fonts (Noto CJK on free/Linux)."""
    chain: list[_CoverFontChoice] = [_CoverFontChoice(_find_cover_font())]
    chain.extend(_CoverFontChoice(path) for path in _cover_fallback_font_candidates())
    for collection in (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        if collection.is_file():
            chain.extend(_noto_cjk_faces(collection, prefer_jp=prefer_jp))
    unique: list[_CoverFontChoice] = []
    seen: set[tuple[str, int]] = set()
    for choice in chain:
        key = (str(choice.path), choice.face_index)
        if key not in seen:
            seen.add(key)
            unique.append(choice)
    return unique


def _cover_font_for_text(cover_text, selection_audit: dict | None = None):
    """Choose ONE font face for the whole cover (uniform — never mix): the first
    chain member with a REAL glyph for every char (raster .notdef probe) and no
    known wrong-shape glyph for this text.  If nothing fully covers, keep the
    least-missing member and disclose the residual glyph risk instead of
    silently shipping a .notdef (the 獣 case)."""
    prefer_jp = any("぀" <= ch <= "ヿ" for ch in cover_text)
    rejected: list[dict[str, object]] = []
    best: tuple[int, _CoverFontChoice, list[str]] | None = None
    for choice in _cover_font_chain(prefer_jp=prefer_jp):
        wrong_shape = _COVER_WRONG_SHAPE_GLYPHS.get(choice.stem, frozenset())
        wrong_hits = sorted({ch for ch in cover_text if ch in wrong_shape})
        missing = _cover_missing_checker(choice)
        missing_hits = sorted(
            {ch for ch in cover_text if (not ch.isspace()) and missing(ch)}
        )
        if not wrong_hits and not missing_hits:
            if selection_audit is not None:
                selection_audit.update(
                    {"font": choice.name, "rejected": rejected, "glyph_risk": []}
                )
            return choice
        rejected.append(
            {"font": choice.name, "wrong_shape": wrong_hits, "missing": missing_hits}
        )
        badness = len(wrong_hits) + len(missing_hits)
        if best is None or badness < best[0]:
            best = (badness, choice, [*wrong_hits, *missing_hits])
    _badness, choice, risk = best  # chain always has >=1 member (ZCOOL fail-closed)
    if selection_audit is not None:
        selection_audit.update(
            {"font": choice.name, "rejected": rejected, "glyph_risk": risk}
        )
    return choice


def _cover_missing_checker(font_choice):
    """Cached ch->bool: True if this font face is MISSING the glyph (renders its
    .notdef).  Detect by rendering the char and comparing against two codepoints
    that are never real glyphs -- a PUA char and a Unicode noncharacter (some
    fonts do map PUA, so either probe alone can miss)."""
    path, face_index = (
        (font_choice.path, font_choice.face_index)
        if isinstance(font_choice, _CoverFontChoice)
        else (font_choice, 0)
    )
    key = (str(path), face_index)
    cache_all = _COVER_MISSING_CHECKERS
    if key not in cache_all:
        from PIL import Image, ImageDraw, ImageFont
        probe = ImageFont.truetype(str(path), 100, index=face_index)
        def render(ch):
            img = Image.new("L", (160, 180), 0)
            ImageDraw.Draw(img).text((12, 12), ch, font=probe, fill=255)
            return img.tobytes()
        notdef_renders = (render("\ue000"), render("\U0001fffe"))
        seen = {}
        def missing(ch):
            if ch not in seen:
                seen[ch] = render(ch) in notdef_renders
            return seen[ch]
        cache_all[key] = missing
    return cache_all[key]


def _cover_fonts(font_choice, size):
    """The single whole-cover font at `size` (no per-glyph mixing -- the font is
    chosen once per cover by _cover_font_for_text)."""
    from PIL import ImageFont
    path, face_index = (
        (font_choice.path, font_choice.face_index)
        if isinstance(font_choice, _CoverFontChoice)
        else (font_choice, 0)
    )
    return (ImageFont.truetype(str(path), size, index=face_index), None, None)


def _cover_char_font(ch, fonts):
    return fonts[0]


def _cover_line_width(draw, segs, fonts, pad) -> float:
    total = 0.0
    for i, (text, _color) in enumerate(segs):
        total += sum(draw.textlength(ch, font=_cover_char_font(ch, fonts)) for ch in text)
        if i < len(segs) - 1:
            total += pad
    return total


def _cover_seg_outlines(fill, outlines):
    """Inner stroke MUST contrast the fill: a light/cream fill with a WHITE inner
    stroke merges adjacent glyphs into a blob, so light fills get the dark outer
    stroke ONLY; saturated fills get dark-outer + white-inner for pop."""
    luminance = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return [outlines[0]] if luminance > 200 else list(outlines)


def _cover_draw_layered(draw, x, y, segs, fonts, outlines, pad) -> None:
    """Two passes so a later segment's outline never occludes an earlier segment's
    fill: draw ALL outlines for the line first, then ALL fills on top.  ``pad`` is
    inserted between different-color segments so thick outlines don't bleed.  Draws
    char-by-char so a per-glyph fallback font (for chars ZCOOL lacks, e.g. 镚) can
    be used without breaking the cute look of the rest."""

    def seg_width(text):
        return sum(draw.textlength(ch, font=_cover_char_font(ch, fonts)) for ch in text)

    xx = x
    for i, (text, fill) in enumerate(segs):
        for width, color in _cover_seg_outlines(fill, outlines):
            cx = xx
            for ch in text:
                fnt = _cover_char_font(ch, fonts)
                draw.text((cx, y), ch, font=fnt, fill=fill, stroke_width=width, stroke_fill=color)
                cx += draw.textlength(ch, font=fnt)
        xx += seg_width(text) + (0 if i == len(segs) - 1 else pad)
    xx = x
    for i, (text, fill) in enumerate(segs):
        cx = xx
        for ch in text:
            fnt = _cover_char_font(ch, fonts)
            draw.text((cx, y), ch, font=fnt, fill=fill)
            cx += draw.textlength(ch, font=fnt)
        xx += seg_width(text) + (0 if i == len(segs) - 1 else pad)


def _build_cover_panel(size, text_bbox, *, color, alpha, pad, feather):
    """Soft-edged dark CARD sized to the text block: solid interior (so any fill,
    incl. pure white, reads on a bright pop background) with only the edges
    feathered.  Built in the text layer's coordinate space so it rotates with the
    text and stays aligned."""
    from PIL import Image, ImageDraw, ImageFilter

    left, top, right, bottom = text_bbox
    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    box = [left - pad, top - pad * 0.7, right + pad, bottom + pad * 0.7]
    radius = int(min(box[2] - box[0], box[3] - box[1]) * 0.18)
    draw.rounded_rectangle(box, radius=max(1, radius), fill=(color[0], color[1], color[2], alpha))
    return panel.filter(ImageFilter.GaussianBlur(feather))


def _build_cover_glow(layer, *, color=(6, 12, 30), grow=13, blur=13, opacity=0.9):
    """Optional soft dark glow derived from the TEXT's own alpha — a shadow that
    hugs the glyph contour (never a rectangle). Dilate (MaxFilter) + blur so it
    reads as depth, not a box."""
    from PIL import Image, ImageFilter

    alpha = layer.split()[3].filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(blur))
    alpha = alpha.point(lambda v: int(min(255, v) * opacity))
    dark = Image.new("RGBA", layer.size, (*color, 255))
    dark.putalpha(alpha)
    return dark


def _cover_segment_line(line, hook_word, base_fill, hook_rgb):
    """Split one line into colored segments, highlighting hook_word if present."""
    if hook_word and hook_word == line:
        return [(line, hook_rgb)]
    if hook_word and hook_word in line:
        before, _sep, after = line.partition(hook_word)
        segs = []
        if before:
            segs.append((before, base_fill))
        segs.append((hook_word, hook_rgb))
        if after:
            segs.append((after, base_fill))
        return segs
    return [(line, base_fill)]


def _atom_em_width(atom: str) -> float:
    """Approximate rendered width in em units: ASCII ≈ half-width, everything
    else (CJK + full-width punctuation) ≈ one em in the cover fonts."""
    return sum(0.5 if " " <= ch <= "~" else 1.0 for ch in atom)


def _bind_punctuation_atoms(units):
    """开标点绑到后一个原子、闭/终结标点绑到前一个原子 —— 任何按这些原子
    断出来的行都天然满足 行首禁闭标点 / 行末禁开标点。"""
    bound: list[str] = []
    pending_open = ""
    for unit in units:
        if len(unit) == 1 and unit in _COVER_OPENING_PUNCT:
            pending_open += unit
            continue
        if len(unit) == 1 and unit in _COVER_CLOSING_PUNCT and bound and not pending_open:
            bound[-1] += unit
            continue
        bound.append(pending_open + unit)
        pending_open = ""
    if pending_open:
        if bound:
            bound[-1] += pending_open
        else:
            bound.append(pending_open)
    return bound


def _split_wide_atom(atom: str, max_em: float, protect: str = "") -> list[str]:
    """Re-split ONE oversized wrap atom at word-safe points.

    Ivan 的排版铁律是 词/hook/专名 不可拆、其余任意断行；但 LLM 偶尔把整个
    引语从句当成一个"词"（“要交780吗”？≈7.6em），这种原子在最小可读字号下
    都放不进 zone，必须按从句内部安全点再分：hook 不拆、《歌名》不拆、
    ASCII/数字串不拆，标点按 _bind_punctuation_atoms 绑定。"""
    if _atom_em_width(atom) <= max_em:
        return [atom]
    # A short complete quote is itself the punchline.  Let the fitter reduce
    # the line font if needed instead of producing a visibly broken half-quote.
    if _COVER_QUOTED_SPAN_RX.fullmatch(atom):
        return [atom]
    pieces = [p for p in (re.split(f"({re.escape(protect)})", atom) if protect else [atom]) if p]
    units: list[str] = []
    for piece in pieces:
        if protect and piece == protect:
            units.append(piece)
        else:
            units.extend(re.findall(r"《[^》]*》|[A-Za-z0-9]+|.", piece))
    chunks: list[str] = []
    cur = ""
    for unit in _bind_punctuation_atoms(units):
        if cur and _atom_em_width(cur + unit) > max_em:
            chunks.append(cur)
            cur = unit
        else:
            cur += unit
    if cur:
        chunks.append(cur)
    return chunks


def _split_wide_atoms(atoms, max_em: float, protect: str = "") -> tuple[str, ...]:
    out: list[str] = []
    for atom in atoms:
        if atom:
            out.extend(_split_wide_atom(atom, max_em, protect))
    return tuple(out)


def _wrap_even(text, n, keep=()):
    """Wrap text into n balanced lines: prefer punctuation-delimited clauses when
    there are exactly n of them, else pack 'atoms' greedily into n length-balanced
    lines.  Atoms kept WHOLE (never split across lines): each ASCII run
    (kmx/TPL/AI/0.5), any 《song name》, a short paired Chinese quote, and any
    phrase in ``keep`` (the highlighted hook word, so its color stays intact).
    Shorter lines ⇒ bigger font."""
    text = text.strip()
    if n <= 1 or len(text) <= 1:
        return [text]
    # A 《song name》never wraps and gets its own complete line (Ivan 2026-07-05);
    # the prefix/suffix DO wrap across the remaining lines so a long tail stays big.
    song = re.search(r"《[^》]*》", text)
    if song:
        pre = text[:song.start()].strip()
        suf = text[song.end():].strip()
        name = song.group()
        # Closing punctuation immediately after 《song》 belongs to that title
        # atom, never at the start of the following line.
        while suf and suf[0] in "，,、；;！!？?。":
            name += suf[0]
            suf = suf[1:].lstrip()
        total = len(pre) + len(suf)
        if total == 0:
            return [name]
        remaining = max(1, n - 1)
        pre_n = max(1, round(remaining * len(pre) / total)) if pre else 0
        suf_n = max(1, remaining - pre_n) if suf else 0
        lines = []
        if pre:
            lines += _wrap_even(pre, pre_n)
        lines.append(name)
        if suf:
            lines += _wrap_even(suf, suf_n)
        return [ln for ln in lines if ln]
    keeps = sorted((re.escape(k) for k in keep if k), key=len, reverse=True)
    pattern = "|".join(
        [
            *keeps,
            _COVER_QUOTED_SPAN_RX.pattern,
            r"《[^》]*》",
            r"[A-Za-z0-9]+",
            r"[^A-Za-z0-9]",
        ]
    )
    raw_atoms = re.findall(pattern, text)
    atoms: list[str] = _bind_punctuation_atoms(raw_atoms)
    n = min(n, len(atoms))
    if n <= 1:
        return ["".join(atoms)]
    # BALANCED partition: break at the atom boundaries nearest the even split
    # positions, so every line is ~equal length (no long tail line that would cap
    # the font). Balanced lines ⇒ bigger font (Ivan 2026-07-05).
    cum = [0]
    for atom in atoms:
        cum.append(cum[-1] + len(atom))
    total = cum[-1]
    cuts = []
    for k in range(1, n):
        ideal = total * k / n
        best = None
        for i in range(len(atoms) - 1):
            if (cuts and i <= cuts[-1]) or i in cuts:
                continue
            dist = abs(cum[i + 1] - ideal)
            if best is None or dist < best[0]:
                best = (dist, i)
        if best is not None:
            cuts.append(best[1])
    lines, start = [], 0
    for cut in cuts:
        lines.append("".join(atoms[start:cut + 1]))
        start = cut + 1
    lines.append("".join(atoms[start:]))
    return [line for line in lines if line]


def _regroup_lines(lines: Sequence[str], k: int) -> list[str]:
    """Partition ``lines`` into k contiguous, length-balanced groups (joining each
    group's text).  Merging adjacent word-safe lines is itself word-safe, so this
    lets the fitter try FEWER lines (bigger font in wide zones) without ever
    splitting a word — the LLM/colon split is the finest (most-lines) option."""
    lines = [ln for ln in lines if ln]
    if k >= len(lines):
        return list(lines)
    if k <= 1:
        return ["".join(lines)]
    lens = [len(ln) for ln in lines]
    total = sum(lens)
    cum, running = [], 0
    for length in lens:
        running += length
        cum.append(running)
    cuts, start = [], 0
    for j in range(1, k):
        ideal = total * j / k
        best = None
        for i in range(start, len(lines) - (k - j)):
            dist = abs(cum[i] - ideal)
            if best is None or dist < best[0]:
                best = (dist, i)
        cuts.append(best[1])
        start = best[1] + 1
    groups, prev = [], 0
    for cut in cuts:
        groups.append("".join(lines[prev:cut + 1]))
        prev = cut + 1
    groups.append("".join(lines[prev:]))
    return [g for g in groups if g]


def _punch_wrap(fragment: str, *, max_em: float = 9.0, max_lines: int = 2) -> list[str]:
    """Wrap one punch fragment into 1-2 short lines.

    ≤9 em 整行不拆（banner 全宽下 9 字单行仍有 ~146px，干净最重要）；更长时
    优先在内部标点断（两半各 ≥3 字才算干净断点），否则均分并避开行首闭标点/
    行末开标点。
    """

    frag = fragment.strip()
    if not frag or _atom_em_width(frag) <= max_em:
        return [frag] if frag else []
    breaks = [
        i + 1
        for i, ch in enumerate(frag[:-1])
        if ch in "，,。！!？?；;…" and i + 1 >= 3 and len(frag) - (i + 1) >= 3
    ]
    if breaks:
        cut = min(breaks, key=lambda i: abs(i - len(frag) / 2))
    else:
        cut = len(frag) // 2
        while 0 < cut < len(frag) and (
            frag[cut] in _COVER_CLOSING_PUNCT or frag[cut - 1] in _COVER_OPENING_PUNCT
        ):
            cut += 1
    left, right = frag[:cut].strip(), frag[cut:].strip()
    return [line for line in (left, right) if line][:max_lines]


def _fit_cover_punch_lines(punch_lines, *, zone, font_path, hook_rgb, base_fill, max_size):
    """Ecosystem-style punch typesetting (2026-07-20 B站高播放封面调研)。

    主梗字整行 hook 色、越大越好；副行奶油色半号。不走通用 fitter 的候选竞争
    （它会把整行梗字当不可拆 hook 原子钉死字号），直接对固定行结构解最大字号：
    宽度 ≤ zone、总高 ≤ zone。搜索可下探到 72px 以便给出确定性排版结果，但 talk
    成品低于 COVER_MIN_TALK_FONT_SIZE 会在写盘前 fail closed。
    """

    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = zone
    zone_w = (x1 - x0) * 0.98
    zone_h = (y1 - y0) * 0.96
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    main_lines = _punch_wrap(punch_lines[0])
    sub_lines = [wrapped for frag in punch_lines[1:] for wrapped in _punch_wrap(frag)]

    def build(emph: int):
        sub_size = max(56, int(round(emph * 0.5)))
        lines = []
        for text in main_lines:
            lines.append({"segs": [(text, hook_rgb)], "size": emph, "gap": max(6, int(emph * 0.08))})
        for text in sub_lines:
            lines.append({"segs": [(text, base_fill)], "size": sub_size, "gap": max(6, int(sub_size * 0.08))})
        return lines

    def fits(emph: int) -> bool:
        total_h = 0.0
        for line in build(emph):
            fonts = _cover_fonts(font_path, line["size"])
            pad = _cover_outlines_for(line["size"])[0][0]
            if _cover_line_width(scratch, line["segs"], fonts, pad) > zone_w:
                return False
            total_h += sum(fonts[0].getmetrics()) + line["gap"]
        return total_h <= zone_h

    lo, hi = 72, max(72, int(max_size))
    if not fits(lo):
        return build(lo)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return build(lo)


def _fit_cover_lines(cover_text, *, hook_word, base_fill, hook_rgb, zone, font_path, max_lines=3, max_size=300, forced_lines=(), word_atoms=()):
    """Choose the line-wrap + font size that makes the title as BIG as possible
    while filling the zone: evaluate every line count (the LLM/colon word-safe
    wraps AND balancer wraps at 1..max_lines), binary-search the largest emph size
    that fits each (width AND height), and take the biggest — preferring a
    word-safe wrap whenever it lands within 90% of the best so bigger text never
    reintroduces a mid-word break.  The hook line is emphasized; outlines scale.

    ``word_atoms`` (Ivan 2026-07-10): the art direction's validated word
    segmentation of the FULL cover text.  When present, the balancer packs these
    atoms instead of single characters — every wrap candidate is then word-safe
    by construction (a break may fall anywhere EXCEPT inside a word / hook /
    proper noun), so the fitter simply takes the biggest font."""
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = zone
    zone_w = (x1 - x0) * 0.98
    zone_h = (y1 - y0) * 0.96
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    explicit = [line.strip() for line in cover_text.splitlines() if line.strip()] if "\n" in cover_text else None
    if explicit is None and forced_lines:
        explicit = [line for line in (raw_line.strip() for raw_line in forced_lines) if line]

    def evaluate(line_texts, emph):
        emph_idx = 0
        for i, line in enumerate(line_texts):
            if hook_word and hook_word in line:
                emph_idx = i
                break
        seg_lines = [_cover_segment_line(line, hook_word, base_fill, hook_rgb) for line in line_texts]
        connector = max(46, int(round(emph / 1.25)))
        sizes = [emph if i == emph_idx else connector for i in range(len(line_texts))]
        fonts = [_cover_fonts(font_path, s) for s in sizes]
        pads = [_cover_outlines_for(s)[0][0] for s in sizes]
        widths = [_cover_line_width(scratch, seg_lines[i], fonts[i], pads[i]) for i in range(len(line_texts))]
        gaps = [max(6, int(s * 0.08)) for s in sizes]
        total_h = sum(sum(f[0].getmetrics()) for f in fonts) + sum(gaps[:-1] or [0])
        fits = (max(widths) <= zone_w) and (total_h <= zone_h)
        return fits, sizes, seg_lines, gaps

    if word_atoms:
        # 超宽原子在最小可读字号下都放不进 zone —— 先按词内安全点再分，否则
        # 强调行被单个原子钉死（7/11 河粉封面 90px 案），行数预算全被浪费。
        word_atoms = _split_wide_atoms(word_atoms, max(3.0, zone_w / _COVER_MIN_EMPH), hook_word or "")
    keep = tuple(dict.fromkeys([*(w for w in word_atoms if w), *((hook_word,) if hook_word else ())]))
    # BIG TEXT (Ivan 2026-07-07 "字卡还是太小"): the font is capped by the longest
    # line's width, so a fixed 2-clause colon split leaves a narrow side zone's
    # font tiny with most of the height empty.  Evaluate MANY line counts and take
    # the biggest font that fills the zone — but PREFER a word-safe wrap (the LLM's
    # split, then the colon clauses) whenever it lands within 90% of the best, so
    # bigger text never costs a mid-word break (拒绝/熊猫/礼墨).  The balancer wraps
    # (word-blind, but keep hook/《song》/ASCII whole) fill the zone as the floor.
    # Word-safe base split (finest granularity): the LLM's word-aware lines, else
    # the colon clauses.  Regrouping it at every line count 1..len gives bigger
    # options (fewer lines fill wide zones) that never split a word.
    base: list[str] = []
    if forced_lines:
        base = [line for line in (raw_line.strip() for raw_line in forced_lines) if line]
    if not base and "\n" in cover_text:
        base = [
            line
            for line in (raw_line.strip() for raw_line in cover_text.splitlines())
            if line
        ]
    wordsafe = [_regroup_lines(base, k) for k in range(1, len(base) + 1)] if base else []
    flat = cover_text.replace("\n", "")  # balancer wraps the flat text (colon \n is a clause hint only)
    # With validated word atoms the balancer itself is word-safe (atoms never
    # split), so its wraps compete as first-class word-safe candidates and the
    # fitter takes the plain maximum — full title, many short lines, big font.
    balancer_wordsafe = bool(word_atoms)
    balancer = [_wrap_even(flat, n, keep=keep) for n in range(1, max_lines + 1)]
    candidates = [(True, w) for w in wordsafe] + [(balancer_wordsafe, w) for w in balancer]

    scored: list[tuple[int, bool, list, list, list]] = []
    for is_wordsafe, line_texts in candidates:
        line_texts = [line for line in line_texts if line]
        if not line_texts or len(line_texts) > max_lines:
            continue
        lo, hi, best_emph = 46, max_size, 46
        while lo <= hi:
            mid = (lo + hi) // 2
            if evaluate(line_texts, mid)[0]:
                best_emph = mid
                lo = mid + 1
            else:
                hi = mid - 1
        _, sizes, seg_lines, gaps = evaluate(line_texts, best_emph)
        scored.append((best_emph, is_wordsafe, seg_lines, sizes, gaps))
    # Honor the biggest WORD-SAFE wrap (the LLM's split / colon clauses).  Only
    # fall to a word-blind balancer wrap when word-safe leaves the zone badly
    # underfilled (< 75% of the balancer's font) — then legible size wins over an
    # intact word.  With the LLM producing enough short lines this rarely fires.
    best_ws = max((s for s in scored if s[1]), key=lambda s: s[0], default=None)
    best_bl = max((s for s in scored if not s[1]), key=lambda s: s[0], default=None)
    if best_ws is not None and (best_bl is None or best_ws[0] >= 0.75 * best_bl[0]):
        chosen = best_ws
    else:
        chosen = best_bl or best_ws
    best = (chosen[0], chosen[2], chosen[3], chosen[4])
    _, seg_lines, sizes, gaps = best
    return [{"segs": seg_lines[i], "size": sizes[i], "gap": gaps[i]} for i in range(len(seg_lines))]


def _overlay_lidousha_cover_title(
    ai_background_path: Path,
    final_cover_path: Path,
    *,
    cover_text: str,
    art_direction: LidoushaCoverArtDirection | None = None,
) -> dict[str, object]:
    """Overlay the multi-color artistic title onto the text-free CPA background.

    Layout-aware (side split / banner / song), with a highlighted hook word and
    the approved cream/navy palette.  Default backing is "outline" — the thick
    navy(+white) stroke alone lifts the text off bright pop backgrounds like the
    reference covers (the dark card/glow modes exist but Ivan rejected the card
    box look).  Font is fail-closed ZCOOLKuaiLe (whole-cover swap down a checked
    complete-font chain only
    when ZCOOL lacks a glyph).
    """
    from PIL import Image, ImageDraw, ImageOps

    if art_direction is None:
        art_direction = _lidousha_cover_art_direction(candidate_id="", title=cover_text, cover_text=cover_text)
    render = _COVER_LAYOUT_RENDER.get(art_direction.layout, _COVER_LAYOUT_RENDER["left-split"])
    zone = render["zone"]
    angle = render["angle"]
    scrim = render["scrim"]
    hook_rgb = _COVER_HOOK_COLORS.get(art_direction.hook_color, _COVER_HOOK_COLORS["yellow"])

    # 梗字模式（2026-07-20 生态调研）：cover_punch 非空时只渲染 1-2 行短梗字
    # （主行=hook 色整行、巨大；副行奶油色小一号），整段 cover_text 退为语境/
    # 回退。punch 行同时作为 wrap 原子，避免字盲均衡器把副行拆词。
    punch_lines = tuple(art_direction.cover_punch)
    render_text = "\n".join(punch_lines) if punch_lines else cover_text
    font_selection: dict[str, object] = {}
    # ZCOOL, or the first chain font whose glyph coverage is verified for this text
    font_path = _cover_font_for_text(render_text, selection_audit=font_selection)
    image = ImageOps.fit(Image.open(ai_background_path).convert("RGB"), (1920, 1080), method=Image.Resampling.LANCZOS)
    if punch_lines:
        lines = _fit_cover_punch_lines(
            punch_lines,
            zone=zone,
            font_path=font_path,
            hook_rgb=hook_rgb,
            base_fill=_COVER_BASE_FILL,
            max_size=render["max_size"],
        )
    else:
        lines = _fit_cover_lines(
            cover_text,
            hook_word=art_direction.hook_word,
            base_fill=_COVER_BASE_FILL,
            hook_rgb=hook_rgb,
            zone=zone,
            font_path=font_path,
            max_lines=render["max_lines"],
            max_size=render["max_size"],
            forced_lines=art_direction.line_breaks,
            word_atoms=art_direction.words,
        )

    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    meta = []
    total_h = 0
    max_w = 0
    for line in lines:
        fonts = _cover_fonts(font_path, line["size"])
        outlines = _cover_outlines_for(line["size"])
        width = _cover_line_width(scratch, line["segs"], fonts, outlines[0][0])
        height = sum(fonts[0].getmetrics())
        meta.append((fonts, outlines, width, height))
        max_w = max(max_w, width)
        total_h += height + line["gap"]
    pad = 90
    layer = Image.new("RGBA", (int(max(1, max_w + pad * 2)), int(max(1, total_h + pad))), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    y = pad // 2
    for line, (fonts, outlines, width, height) in zip(lines, meta):
        x = (layer.width - width) / 2
        _cover_draw_layered(layer_draw, x, y, line["segs"], fonts, outlines, outlines[0][0])
        y += height + line["gap"]
    font_size = max(line["size"] for line in lines)
    if not art_direction.is_song and font_size < COVER_MIN_TALK_FONT_SIZE:
        raise ValueError(
            "COVER_TITLE_TOO_SMALL: "
            f"talk cover emphasis is {font_size}px; minimum is "
            f"{COVER_MIN_TALK_FONT_SIZE}px. Shorten the cover hook or use a "
            "wider layout instead of shrinking the title."
        )

    # Text backing: default "outline" (none) — the thick navy+white outline alone
    # separates the text from a bright pop background, like the reference covers.
    backing = None
    bbox = layer.split()[3].getbbox()
    if _COVER_TEXT_BACKING == "card" and scrim and bbox:
        backing = _build_cover_panel(
            layer.size, bbox, color=scrim["color"], alpha=scrim["alpha"], pad=scrim["pad"], feather=scrim["feather"]
        )
    elif _COVER_TEXT_BACKING == "glow" and bbox:
        backing = _build_cover_glow(layer)
    if angle:
        layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        if backing is not None:
            backing = backing.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)

    x0, y0, x1, y1 = zone
    paste_x = int(x0 + (x1 - x0 - layer.width) / 2)
    paste_y = int(y0 + (y1 - y0 - layer.height) / 2)
    if backing is not None:
        canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
        canvas.paste(backing, (paste_x, paste_y), backing)
        image = Image.alpha_composite(image.convert("RGBA"), canvas).convert("RGB")
    image.paste(layer, (paste_x, paste_y), layer)
    final_cover_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(final_cover_path)
    return {
        "font": font_path.name if font_path is not None else "PIL-default",
        "font_selection": font_selection,
        "font_size": font_size,
        "min_talk_font_size": COVER_MIN_TALK_FONT_SIZE,
        "angle_degrees": angle,
        "overlay_position": {"x": paste_x, "y": paste_y},
        "title_band": art_direction.layout,
        "layout": art_direction.layout,
        "background_style": art_direction.background_style,
        "hook_color": art_direction.hook_color,
        "hook_word": art_direction.hook_word,
        "role": art_direction.role,
        "expression_en": art_direction.expression_en,
        "line_split": (
            "punch"
            if punch_lines
            else ("explicit" if "\n" in cover_text else ("llm_word_aware" if art_direction.line_breaks else "balancer"))
        ),
        "cover_text_mode": "punch" if punch_lines else "full",
        "cover_punch": list(punch_lines),
        "rendered_lines": ["".join(seg[0] for seg in line["segs"]) for line in lines],
        "text_backing": _COVER_TEXT_BACKING,
        "scrim": backing is not None,
    }


def _find_cover_font() -> Path:
    """Use the selected profile's ZCOOL cover title font. Fail closed:
    a silently substituted default font shipped wrong-font covers once
    (2026-07-04); a missing font must block the cover, not degrade it."""

    candidates = [
        CHANNEL_PROFILE.asset_directory("fonts", repo_root=ROOT) / "ZCOOLKuaiLe-Regular.ttf",
        Path("/opt/bilive/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
        Path("/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
    ]
    fontsdir = _lidousha_fontsdir(None)
    if fontsdir is not None:
        candidates.insert(0, fontsdir / "ZCOOLKuaiLe-Regular.ttf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "COVER_FONT_MISSING: ZCOOLKuaiLe-Regular.ttf not found in the selected profile fonts"
    )
