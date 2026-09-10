"""Story composition and legacy fallback presets for cover art direction."""
from __future__ import annotations

from dataclasses import replace
import json
import re

from src.autoslice.llm_client import extract_json_object


def final_copy_image_prompt(direction, cover_text: str) -> str:
    lines = list(direction.cover_punch) or cover_text.splitlines()
    return (
        "FINAL EDITORIAL COPY (meaning only; do NOT draw these words): "
        + json.dumps(lines, ensure_ascii=False)
        + ". The picture and this copy must tell the same selected story beat. "
        "Do not add a different climax merely because it appears in the full story. "
    )


def refresh_visual_brief(direction, *, original_punch, title, story_hook, llm_call):
    """Re-draft only when semantic review changes the selected story wording.

    No image has been generated yet. Whitespace, punctuation and line-break
    changes reuse the art; a changed event cannot consume the earlier brief.
    """
    def meaning_key(lines):
        return re.sub(r"[\W_]+", "", "".join(lines)).casefold()

    if (
        not direction.visual_brief or not direction.cover_punch
        or meaning_key(original_punch) == meaning_key(direction.cover_punch)
    ):
        return direction
    previous = direction.visual_brief
    direction = replace(direction, visual_brief="")
    prompt = (
        "The cover's final semantic review changed its copy. The previous visual brief is now invalid. "
        "Write one replacement English visual_brief (80-1200 characters) for the FINAL copy below. "
        "Keep the selected layout and art medium where compatible, but replace any action or story beat "
        "that conflicts with the final copy. Do not change the final copy or pick another climax. "
        "You have not seen the identity reference: do not describe hair, clothing, accessories or face marks. "
        "The image generator will preserve the supplied reference. Do not invent a second participant.\n"
        f"Full source title: {title}\nFull story: {story_hook}\n"
        f"Final copy: {json.dumps(list(direction.cover_punch), ensure_ascii=False)}\n"
        f"Reviewed story meaning: {direction.cover_punch_semantic_review.get('story_summary', '')}\n"
        f"Final layout: {direction.layout}\nPrevious draft (superseded): {previous}\n"
        'Return JSON only: {"visual_brief":"..."}'
    )
    try:
        payload = extract_json_object(llm_call(prompt))
        brief = payload.get("visual_brief")
        if isinstance(brief, str) and 40 <= len(brief.strip()) <= 1600:
            return replace(direction, visual_brief=brief.strip())
    except Exception:
        pass
    # Art direction remains a soft refinement. Fall back to final-copy-driven
    # generation, never to the superseded visual brief.
    return direction

_COVER_BG_BUSY = (
    # Legacy metadata remains renderable; new drafts do not rotate this pool.
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
# Expression guardrail (维护者 — 表情永不吐舌头, never 油滑/挑衅/sexy).  Match bad
# PHRASES, not bare "tongue" (else a benign "no tongue" would be rejected); the
# global no-tongue rule is enforced unconditionally in _cover_prompt.
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
def story_composition_prompt(art_direction, title_zone) -> str:
    brief = art_direction.visual_brief or (
        "Choose a concrete scene and camera distance that explain the final editorial copy. "
        "Keep the subject recognizable and the story action clear; do not invent a new event."
    )
    background = _COVER_BG_PHRASES.get(art_direction.background_style, art_direction.background_style)
    return (
        f"STORY ART DIRECTION: {brief} "
        + (f"Background suggestion: {background}. " if background != "source-led" else "") +
        "This direction owns the camera, staging and surface treatment; the default decorative background "
        "family is not a requirement. It has NO authority over identity or clothing: ignore any hair, ear, eye, "
        "face-marking, outfit or accessory description in this text that is not present in the image reference. "
        "Every temporal copy must keep the reference's SAME hair, ears, face details and COMPLETE outfit layers; "
        "a different persona means a different expression or gesture, never an unreferenced alternate skin. "
        "No automatic sticker outline, giant floating head, halftone or speed lines. "
        "If the same person appears in multiple panels, make the temporal/editorial comparison explicit; "
        "do not imply a second real participant. Do not add an unsupported event or costume. "
        f"TITLE RESERVATION: on the 1920x1080 canvas keep x={title_zone[0]}..{title_zone[2]}, "
        f"y={title_zone[1]}..{title_zone[3]} low-detail for the deterministic title. "
        "Keep complete heads, ears and story-critical details outside that rectangle. Size and position the "
        "figures to fit the remaining area; do not enlarge them into the reserved title space. Continue the scene naturally "
        "through it; do not draw a box, label, fake text or a solid banner. "
    )


def image_identity_prompt(*, profile, identity_descriptor, identity_asset, art_direction) -> str:
    introduction = (
        f"Create a 16:9 (1920x1080) editorial cover illustration for {profile.prompt_name}. "
        if not art_direction.is_song
        else f"Create a bold 16:9 (1920x1080) anime VTuber livestream cover thumbnail for {profile.prompt_name}. "
    )
    return (
        introduction +
        "Use the supplied image ONLY as identity/style reference. "
        f"IDENTITY (keep her instantly recognizable): {identity_asset} "
        f"Persona identity descriptors (Chinese, authoritative): {identity_descriptor} "
        "MULTI-PERSON REFERENCE RULE: if the supplied image contains several people, the ONLY protagonist is "
        f"the source person visibly labelled {profile.display_name}, or the one matching the {profile.prompt_name} {profile.cover_identity.prompt_tag_en} identity when "
        "no label is visible. Never copy another participant's face, hair, outfit, horns or accessories into the "
        f"protagonist, and never treat adding {profile.cover_identity.feature_en} to another participant as identity preservation. Other "
        "participants may appear only as clearly secondary figures when the reference supports them. "
        "PRESERVE THE EXACT OUTFIT, skin tone, hairstyle and accessories shown in the reference frame — she wears "
        "DIFFERENT costumes on different streams, so do NOT invent or lock a fixed costume; copy what the reference shows. "
        f"EXPRESSION (must fit her in-character role for this clip): {art_direction.expression_en}. "
        "Her mouth may be open for a gasp/shout/laugh but she must NEVER stick her tongue out — no tongue showing; "
        "never look sly beyond cute, never provocative or sexy. "
        f"SUBJECT READABILITY: {profile.prompt_name} must be easy to identify and carry the story reaction. "
        "Choose the camera distance and subject hierarchy for this story, keeping her complete face clear and "
        "expressive at feed-thumbnail size. A story object or a sequence may share focus with her; unrelated decoration "
        "must not compete with the event. A clean title zone is intentional, but it must not become vast "
        "dead space or a meaningless solid-color strip; every non-title element must support this clip's story. "
        "FEED-SAFE FRAMING: keep her FACE and all key features within the central 4:3 portion of the frame — feed "
        "thumbnails crop the outer ~13% of the width on EACH side, so place nothing important (face, hands, key props) "
        "in the far-left or far-right edges; those edges may hold only background. "
    )
