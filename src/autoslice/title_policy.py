"""Profile-owned deterministic title gates and selection-hook binding."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from src.autoslice.candidate_entity_projection import (
    CandidateEntityProjectionError,
    load_candidate_entity_projection,
)
from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthorityError,
    load_candidate_public_text_surface_authority,
)
from src.autoslice.channel_profile import load_channel_profile


TITLE_POLICY_SCHEMA = "vtuber-slice.title-policy.v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)


class TitlePolicyError(ValueError):
    pass


def _load_title_policy() -> Mapping[str, object]:
    path = CHANNEL_PROFILE.asset_file("title_policy")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TitlePolicyError(f"cannot read title policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TitlePolicyError("title policy must be an object")
    expected = {
        "schema_version",
        "banned_hype_words",
        "suffix_only_hype_words",
        "banned_filler_words",
        "banned_regexes",
        "min_length",
        "max_length",
        "max_attempts",
        "generic_hook_words",
        "meaningless_particles",
    }
    if set(payload) != expected:
        raise TitlePolicyError(
            "title policy keys mismatch: "
            f"missing={sorted(expected - set(payload))}, "
            f"unknown={sorted(set(payload) - expected)}"
        )
    if payload.get("schema_version") != TITLE_POLICY_SCHEMA:
        raise TitlePolicyError(f"unsupported title policy schema {payload.get('schema_version')!r}")
    return payload


def _strings(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = payload.get(key)
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise TitlePolicyError(f"title policy {key} must be a non-empty string list")
    if len(values) != len(set(values)):
        raise TitlePolicyError(f"title policy {key} must not contain duplicates")
    return tuple(values)


def _positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TitlePolicyError(f"title policy {key} must be a positive integer")
    return value


_POLICY = _load_title_policy()
_TITLE_PREFIX = CHANNEL_PROFILE.talk_title_prefix
_TITLE_BANNED_HYPE_WORDS = _strings(_POLICY, "banned_hype_words")
_TITLE_SUFFIX_ONLY_HYPE_WORDS = _strings(_POLICY, "suffix_only_hype_words")
_TITLE_BANNED_FILLER_WORDS = _strings(_POLICY, "banned_filler_words")
_TITLE_BANNED_REGEXES = tuple(
    re.compile(pattern) for pattern in _strings(_POLICY, "banned_regexes")
)
# Backwards-compatible name used by existing callers/tests.  The asset can add
# further patterns without forcing the shadow runner to know about them.
_TITLE_BANNED_MIAO_RE = _TITLE_BANNED_REGEXES[0]
_TITLE_BANNED_SUFFIX_RE = re.compile(
    "到(?:" + "|".join(_TITLE_BANNED_HYPE_WORDS + _TITLE_SUFFIX_ONLY_HYPE_WORDS) + ")"
)
_TITLE_MIN_LEN = _positive_int(_POLICY, "min_length")
_TITLE_MAX_LEN = _positive_int(_POLICY, "max_length")
_TITLE_MAX_ATTEMPTS = _positive_int(_POLICY, "max_attempts")
if _TITLE_MIN_LEN > _TITLE_MAX_LEN:
    raise TitlePolicyError("title policy min_length exceeds max_length")
_SELECTION_HOOK_GENERIC_WORDS = (
    CHANNEL_PROFILE.display_name,
    CHANNEL_PROFILE.short_name,
    *_strings(_POLICY, "generic_hook_words"),
)
_SELECTION_HOOK_GENERIC_ANCHORS = set(_SELECTION_HOOK_GENERIC_WORDS)
_SELECTION_HOOK_MEANINGLESS_RE = re.compile(
    "(?:"
    + "|".join(
        re.escape(value)
        for value in (
            *_SELECTION_HOOK_GENERIC_WORDS,
            *_strings(_POLICY, "meaningless_particles"),
        )
    )
    + ")"
)

_TITLE_MARK_PAIRS = {
    "（": "）",
    "(": ")",
    "【": "】",
    "[": "]",
    "《": "》",
    "“": "”",
    "‘": "’",
}
_TITLE_MARK_OPENERS = frozenset(_TITLE_MARK_PAIRS)
_TITLE_MARK_CLOSERS = frozenset(_TITLE_MARK_PAIRS.values())


_CANDIDATE_RECUT_SUFFIX_RX = re.compile(r"r\d+$")


def _candidate_family(candidate_id: str) -> str:
    """auto_225942_698_931r2 与 auto_225942_698_931 是同一内容家族。"""

    return _CANDIDATE_RECUT_SUFFIX_RX.sub("", str(candidate_id or "").strip())


def validate_candidate_title_surface(
    candidate_id: str,
    title: str,
    *,
    artifact_kind: str = "title",
) -> dict[str, object] | None:
    """Enforce an optional candidate's reviewed or public-text-only surface.

    The full projection is bound to exact reviewed-SRT bytes.  The narrower
    public-text authority instead binds one exact candidate/source/context and
    explicitly grants no subtitle or speaker review.  Either form is optional
    for legacy candidates and fail-closed once present; identity-equivalent
    aliases never become interchangeable title spellings.
    """

    if artifact_kind not in {"title", "cover"}:
        raise TitlePolicyError(
            f"unsupported candidate entity projection artifact {artifact_kind!r}"
        )
    family = _candidate_family(candidate_id)
    if not family:
        return None
    projection_path = (
        CHANNEL_PROFILE.asset_root
        / "candidate_entity_projections"
        / f"{family}.entity-projection.v1.json"
    )
    try:
        public_authority = load_candidate_public_text_surface_authority(family, root=REPO_ROOT)
    except CandidatePublicTextSurfaceAuthorityError as exc:
        raise TitlePolicyError(f"candidate public text surface failed for {family}: {exc}") from exc
    if projection_path.exists() and public_authority is not None:
        raise TitlePolicyError("candidate entity title authorities are ambiguous")
    if not projection_path.exists():
        if public_authority is None:
            return None
        try:
            public_authority.require_artifact_text(
                artifact_kind=artifact_kind,
                text=title,
            )
        except CandidatePublicTextSurfaceAuthorityError as exc:
            raise TitlePolicyError(
                f"candidate public text surface failed for {family}: {exc}"
            ) from exc
        return {
            "schema_version": "candidate-public-text-surface-audit.v1",
            "status": "PASS",
            "candidate_id": family,
            "authority_sha256": public_authority.authority_sha256,
            "authority_scope": "GENERATED_PUBLIC_TEXT_ONLY_NO_SUBTITLE_OR_SPEAKER_REVIEW",
            "surface_type": "title_cover",
            "artifact_kind": artifact_kind,
        }
    reviewed_srt_path = (
        CHANNEL_PROFILE.asset_directory("reviewed_subtitle_baselines") / f"{family}.reviewed.srt"
    )
    try:
        projection = load_candidate_entity_projection(
            projection_path=projection_path,
            candidate_id=family,
            reviewed_srt_path=reviewed_srt_path,
        )
        projection.require_text_surfaces(
            surface_type="title_cover",
            text=title,
        )
    except CandidateEntityProjectionError as exc:
        raise TitlePolicyError(
            f"candidate entity title projection failed for {family}: {exc}"
        ) from exc
    return {
        "schema_version": "candidate-entity-surface-audit.v1",
        "status": "PASS",
        "candidate_id": family,
        "reviewed_srt_sha256": projection.binding.reviewed_srt_sha256,
        "projection_sha256": projection.projection_sha256,
        "surface_type": "title_cover",
        "artifact_kind": artifact_kind,
    }


def manual_title_override(candidate_id: str) -> str | None:
    """维护者 手定标题按 candidate 注入（7/18 五件套定版）。

    返回值是 维护者 手定的标题正文 authority，不是可绕过发布契约的完整
    archive title。正文保持原样；共同发布 choke point 仍会补频道前缀并校验
    12–48 字、成对符号与歌切目录式。加载失败只会让结果为 None（回落自动
    标题），绝不抛错。
    """

    family = _candidate_family(candidate_id)
    if not family:
        return None
    try:
        import json

        path = CHANNEL_PROFILE.asset_file("manual_title_overrides")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != (
            f"{CHANNEL_PROFILE.profile_id}-manual-title-overrides.v1"
        ):
            return None
        for row in payload.get("overrides") or []:
            if not isinstance(row, dict):
                continue
            if _candidate_family(str(row.get("candidate_id") or "")) == family:
                title = str(row.get("title") or "").strip()
                if title:
                    return title
    except Exception:
        return None
    return None


def _title_policy_violations(title: str) -> list[str]:
    """Return deterministic policy codes tripped by an automatic title."""

    violations: list[str] = []
    if _TITLE_BANNED_SUFFIX_RE.search(title):
        violations.append("banned_universal_suffix")
    if any(word in title for word in _TITLE_BANNED_HYPE_WORDS):
        violations.append("banned_hype_word")
    if any(word in title for word in _TITLE_BANNED_FILLER_WORDS):
        violations.append("banned_filler_word")
    if any(pattern.search(title) for pattern in _TITLE_BANNED_REGEXES):
        violations.append("banned_filler_word")
    stack: list[str] = []
    for char in title:
        if char in _TITLE_MARK_OPENERS:
            stack.append(char)
            continue
        if char not in _TITLE_MARK_CLOSERS:
            continue
        if not stack or _TITLE_MARK_PAIRS[stack[-1]] != char:
            violations.append("unbalanced_title_marks")
            break
        stack.pop()
    else:
        if stack:
            violations.append("unbalanced_title_marks")
    return violations


def canonicalize_automatic_title_fillers(title: str) -> str:
    """Remove only profile-declared filler words from an automatic title.

    The profile already declares these exact words semantically disposable.
    Broader hype/regex violations remain model-owned and fail closed.
    """

    repaired = str(title or "").strip()
    for word in _TITLE_BANNED_FILLER_WORDS:
        repaired = repaired.replace(word, "")
    return repaired


def build_automatic_talk_title_prompt(
    *,
    selection_hook: str,
    transcript_sample: str,
    important_ip_prompt: str,
    clip_context_prompt: str,
    persona_asset: str,
    style_asset: str,
) -> str:
    """Build the bounded Talk title prompt from profile-owned policy assets."""

    hook_contract = ""
    output_contract = '{"title": "标题"}'
    if selection_hook:
        hook_contract = (
            f"\n选片主钩子（这是为什么选中本片，权威高于后续陪衬话题）: {selection_hook}\n"
            f"标题必须保留第一分句的核心事件: {_selection_hook_first_clause(selection_hook)}\n"
            "同时输出 selection_hook_anchor：从该第一分句原样复制的 2–12 字具体短语，"
            f"避开‘{CHANNEL_PROFILE.display_name}/{CHANNEL_PROFILE.short_name}/主播/直播/弹幕/观众/自己/这个/那个/然后/时候/表演’等泛词；"
            "该短语必须逐字出现在标题里。不得把片段后半段的陪衬话题偷换成主标题。\n"
        )
        output_contract = '{"title": "标题", "selection_hook_anchor": "第一分句中的具体短语"}'
    context_contract = (
        "\n同一份 hash-bound 长程语境（用于整片回指、口癖和专名候选；"
        "它本身不授权改字幕）：\n" + clip_context_prompt + "\n"
        if clip_context_prompt
        else ""
    )
    return (
        f"为一条{CHANNEL_PROFILE.display_name}(B站虚拟主播)的直播切片起中文标题。\n"
        f"最重要的原则：观众是因为'这是{CHANNEL_PROFILE.display_name}'才点进来的,不是因为内容——标题必须围绕{CHANNEL_PROFILE.display_name}本人"
        "(她的反应、气质、口癖、梗、名字谐音),切片内容只是辅助素材。引人注目为先。\n"
        f"\n{CHANNEL_PROFILE.display_name}特质:\n{persona_asset}\n"
        f"\n标题风格规范与历史标题范例(严格模仿这个风格):\n{style_asset}\n"
        f"\n本切片转写内容节选(辅助素材): {transcript_sample}\n"
        f"{important_ip_prompt}{hook_contract}{context_contract}"
        f"硬性要求：含{CHANNEL_PROFILE.talk_title_prefix}前缀后 {_TITLE_MIN_LEN}–{_TITLE_MAX_LEN} 字"
        "（维护者 手定语料的主力带是 25–45 字的三拍叙事，不要为了凑短把梗压没；"
        "只有梗足够硬的短爆点才走 20 字以下）；"
        "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
        "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
        "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
        f"只输出一个 JSON 对象：{output_contract}"
    )


def publish_title_lane(title: str, *, explicit_lane: str | None = None) -> str:
    """Return the deterministic talk/song lane for one publish title."""

    if explicit_lane not in (None, "talk", "song"):
        raise TitlePolicyError(f"unsupported publish title lane {explicit_lane!r}")
    if explicit_lane is not None:
        return explicit_lane
    return (
        "song" if str(title or "").strip().startswith(CHANNEL_PROFILE.song_title_prefix) else "talk"
    )


_SONG_NAME_IN_TITLE_RX = re.compile(r"《([^《》]{1,80})》")


def canonicalize_song_catalog_title(title: str) -> str:
    """Collapse an automatic song draft to the fixed catalog form.

    维护者 铁律：歌切标题 = 歌切前缀 + 《歌名》，前缀与
    《歌名》之间、《歌名》之后都不允许任何字符（含「｜副标题」/hook 尾巴）。
    这是所有自动标题的最终 choke point——无论标题来自 LLM、兜底模板还是恢复
    路径，只要带歌切前缀就在这里折叠定形。谈话标题与不含《歌名》的标题原样
    通过。人工正文可以不经过这个 draft helper，但所有人工/自动最终标题都会再走
    ``canonicalize_publish_title`` 与 ``publish_title_policy_violations``。
    """

    stripped = title.strip()
    prefix = CHANNEL_PROFILE.song_title_prefix
    if not stripped.startswith(prefix):
        return stripped
    match = _SONG_NAME_IN_TITLE_RX.search(stripped[len(prefix) :])
    if match is None:
        return stripped
    return f"{prefix}《{match.group(1)}》"


def _ensure_title_prefix(title: str) -> str:
    """Guarantee the selected profile's publish prefix on an automatic title."""

    stripped = title.strip()
    return (
        stripped
        if stripped.startswith(_TITLE_PREFIX)
        else _TITLE_PREFIX + stripped
    )


def canonicalize_publish_title(title: str, *, lane: str | None = None) -> str:
    """Apply only the channel-owned archive envelope.

    A human title owns its body, not the Bilibili archive envelope. Talk titles
    always carry ``【李豆沙】``. Song titles always collapse to the catalog form
    ``【李豆沙】豆沙歌，《歌名》``. The function never rewrites a talk body.
    """

    stripped = str(title or "").strip()
    resolved_lane = publish_title_lane(stripped, explicit_lane=lane)
    if resolved_lane == "talk":
        return _ensure_title_prefix(stripped)
    match = _SONG_NAME_IN_TITLE_RX.search(stripped)
    if match is None:
        return stripped
    return f"{CHANNEL_PROFILE.song_title_prefix}《{match.group(1)}》"


def publish_title_policy_violations(
    title: str,
    *,
    lane: str | None = None,
    enforce_automatic_style: bool = False,
) -> list[str]:
    """Validate the final archive title shared by every producer/uploader path."""

    raw = str(title or "")
    stripped = raw.strip()
    resolved_lane = publish_title_lane(stripped, explicit_lane=lane)
    violations: list[str] = []
    if not stripped:
        return ["publish_title_empty"]
    if raw != stripped:
        violations.append("publish_title_outer_whitespace")
    if not _TITLE_MIN_LEN <= len(stripped) <= _TITLE_MAX_LEN:
        violations.append("publish_title_length_out_of_bounds")
    if resolved_lane == "talk":
        if not stripped.startswith(_TITLE_PREFIX):
            violations.append("talk_title_prefix_missing")
        if stripped.startswith(CHANNEL_PROFILE.song_title_prefix):
            violations.append("talk_title_uses_song_prefix")
    else:
        expected = canonicalize_publish_title(stripped, lane="song")
        song_body = stripped[len(CHANNEL_PROFILE.song_title_prefix) :]
        if (
            not stripped.startswith(CHANNEL_PROFILE.song_title_prefix)
            or _SONG_NAME_IN_TITLE_RX.fullmatch(song_body) is None
            or stripped != expected
        ):
            violations.append("song_catalog_title_not_exact")
    structural = [
        code
        for code in _title_policy_violations(stripped)
        if code == "unbalanced_title_marks" or enforce_automatic_style
    ]
    violations.extend(code for code in structural if code not in violations)
    return violations


def _selection_hook_first_clause(selection_hook: str | None) -> str:
    return re.split(
        r"[，,。.!！?？；;：:\n…]",
        str(selection_hook or "").strip(),
        maxsplit=1,
    )[0].strip()


def _selection_hook_anchor_valid(*, anchor: object, selection_hook: str, title: str) -> bool:
    """Require an automatic title to retain a concrete main-event phrase."""

    if not isinstance(anchor, str):
        return False
    anchor = anchor.strip()
    first_clause = _selection_hook_first_clause(selection_hook)
    title_body = str(title).removeprefix(_TITLE_PREFIX).strip()
    title_lead_clause = re.split(r"[，,。.!！?？；;：:\n…]", title_body, maxsplit=1)[0].strip()
    meaningful = _SELECTION_HOOK_MEANINGLESS_RE.sub("", anchor).strip()
    return bool(
        2 <= len(anchor) <= 12
        and anchor not in _SELECTION_HOOK_GENERIC_ANCHORS
        and len(meaningful) >= 2
        and anchor in first_clause
        and anchor in title_lead_clause
        and title_lead_clause.find(anchor) <= 10
    )


def _selection_hook_fallback_title(selection_hook: str | None) -> str | None:
    """Build a conservative source-bound title after all model attempts fail."""

    raw = str(selection_hook or "").strip().rstrip("。；; ")
    if not raw:
        return None
    clauses = [part.strip() for part in re.split(r"[，,。；;：:\n…]", raw) if part.strip()]
    if not clauses:
        return None
    first = clauses[0].replace(CHANNEL_PROFILE.display_name, CHANNEL_PROFILE.short_name)
    body = first
    if len(clauses) > 1:
        second = clauses[1].replace(CHANNEL_PROFILE.display_name, CHANNEL_PROFILE.short_name)
        if second.startswith("她"):
            second = "结果" + second[1:]
        candidate = f"{first}，{second}"
        if len(_ensure_title_prefix(candidate)) <= _TITLE_MAX_LEN:
            body = candidate
    available = _TITLE_MAX_LEN - len(_TITLE_PREFIX)
    body = body[:available].rstrip("，,、；;：: ")
    title = _ensure_title_prefix(body)
    return title if _TITLE_MIN_LEN <= len(title) <= _TITLE_MAX_LEN else None
