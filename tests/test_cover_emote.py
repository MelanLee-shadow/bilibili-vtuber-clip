"""Emote-sticker cover subject feature (Ivan 2026-07-19).

The official sticker may REPLACE the live-frame character redraw only on a
strong articulated reason from the art-direction judge (mutually exclusive
subjects); "companion" is reserved for 分身 memes / depicting her fans kmx; songs never use
emotes; every resolution failure downgrades to the default character redraw
instead of blocking the cover.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import cover_emote, publish_staging
from src.autoslice.cover_emote import (
    EMPTY_EMOTE_LIBRARY,
    EmoteEntry,
    EmoteLibrary,
    compose_companion_reference,
    emote_catalog_prompt_block,
    load_emote_library,
    normalize_emote_choice,
    resolve_emote_reference,
)
from src.autoslice.cover_generation import (
    _lidousha_cover_art_direction,
    _lidousha_cover_prompt,
)

TALK_TITLE = "【李豆沙】全场都在打call，她当场看傻"
TALK_TEXT = "全场都在打call\n她当场看傻"
SONG_TITLE = "【李豆沙】豆沙歌，《旅行的意义》"
SONG_TEXT = "《旅行的意义》"
STRONG_REASON = "全场大合唱应援气氛,打call表情包完全对上"


def _entry(**overrides) -> EmoteEntry:
    base = dict(
        id="09",
        label="打call",
        subject="lidousha",
        file="09_李豆沙_打call.png",
        hd_file="hd/09_李豆沙_打call.png",
        hd_sha256="0" * 64,
        baked_text="",
        description="双手各挥一根蓝色荧光棒应援",
        use_when="应援打call",
    )
    base.update(overrides)
    return EmoteEntry(**base)


def _library(*entries: EmoteEntry) -> EmoteLibrary:
    return EmoteLibrary(enabled=True, entries=entries or (_entry(),))


def _sticker_with_media(tmp_path: Path, monkeypatch, **overrides) -> EmoteEntry:
    """A synthetic sticker whose bytes exist under AUTOSLICE_EMOTE_DIR and whose
    manifest sha matches those bytes (tests never depend on the gitignored
    real media)."""
    from PIL import Image

    media_root = tmp_path / "emotes"
    entry = _entry(**overrides)
    target = media_root / entry.hd_file
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (256, 256), (250, 250, 250, 255)).save(target)
    entry = _entry(**{**overrides, "hd_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    monkeypatch.setenv(cover_emote.EMOTE_MEDIA_ROOT_ENV, str(media_root))
    return entry


# ---------------------------------------------------------------------------
# Committed manifest
# ---------------------------------------------------------------------------

def test_committed_emote_library_loads_with_25_valid_entries():
    library = load_emote_library(ROOT)
    assert library
    assert len(library.entries) == 25
    assert len(library.ids) == 25
    for entry in library.entries:
        assert entry.label and entry.description and entry.use_when
        assert entry.hd_file.startswith("hd/")
        assert entry.subject in ("lidousha", "panda_creature")
    # 25 只是熊猫 is the panda-creature sticker (companion stand-in for her
    # fans kmx — kimo熊 is the fans' name, not a mascot).
    assert library.get("25").subject == "panda_creature"
    # Baked captions are recorded so the prompt can order their omission.
    assert library.get("01").baked_text == "别走好吗"


@pytest.mark.skipif(
    not (ROOT / "assets" / "emote" / "hd").is_dir(),
    reason="gitignored emote media not present on this machine",
)
def test_committed_manifest_shas_match_local_media(monkeypatch):
    monkeypatch.delenv(cover_emote.EMOTE_MEDIA_ROOT_ENV, raising=False)
    library = load_emote_library(ROOT)
    for entry in library.entries:
        resolved, detail = resolve_emote_reference(entry, repo_root=ROOT)
        assert resolved is not None, f"{entry.id} {entry.label}: {detail}"


# ---------------------------------------------------------------------------
# Loader fail-open
# ---------------------------------------------------------------------------

def test_load_emote_library_fails_open_without_profile(tmp_path):
    assert not load_emote_library(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps({"schema_version": "other.v1", "emotes": []}),
        json.dumps({"schema_version": "lidousha-emote-library.v1", "emotes": "nope"}),
        # duplicate id
        json.dumps(
            {
                "schema_version": "lidousha-emote-library.v1",
                "emotes": [
                    {"id": "01", "label": "a", "file": "a.png", "hd_file": "hd/a.png", "hd_sha256": "0" * 64},
                    {"id": "01", "label": "b", "file": "b.png", "hd_file": "hd/b.png", "hd_sha256": "1" * 64},
                ],
            }
        ),
        # unsafe absolute media path
        json.dumps(
            {
                "schema_version": "lidousha-emote-library.v1",
                "emotes": [
                    {"id": "01", "label": "a", "file": "a.png", "hd_file": "/etc/passwd", "hd_sha256": "0" * 64},
                ],
            }
        ),
        # malformed sha
        json.dumps(
            {
                "schema_version": "lidousha-emote-library.v1",
                "emotes": [
                    {"id": "01", "label": "a", "file": "a.png", "hd_file": "hd/a.png", "hd_sha256": "zz"},
                ],
            }
        ),
    ],
)
def test_load_emote_library_fails_open_on_malformed_manifest(tmp_path, monkeypatch, payload):
    manifest = tmp_path / "emote_library.v1.json"
    manifest.write_text(payload, encoding="utf-8")

    class _StubProfile:
        def asset_file(self, key, repo_root=None):
            assert key == "emote_library"
            return manifest

    monkeypatch.setattr(cover_emote, "load_channel_profile", lambda root: _StubProfile())
    assert not load_emote_library(tmp_path)


def test_load_emote_library_respects_enabled_false(tmp_path, monkeypatch):
    manifest = tmp_path / "emote_library.v1.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-emote-library.v1",
                "enabled": False,
                "emotes": [
                    {"id": "01", "label": "a", "file": "a.png", "hd_file": "hd/a.png", "hd_sha256": "0" * 64},
                ],
            }
        ),
        encoding="utf-8",
    )

    class _StubProfile:
        def asset_file(self, key, repo_root=None):
            return manifest

    monkeypatch.setattr(cover_emote, "load_channel_profile", lambda root: _StubProfile())
    library = load_emote_library(tmp_path)
    assert not library                      # disabled ⇒ falsy ⇒ never offered
    assert len(library.entries) == 1        # but entries stay inspectable


# ---------------------------------------------------------------------------
# Strong-reason gating
# ---------------------------------------------------------------------------

def test_normalize_emote_choice_accepts_strong_replace_pick():
    assert normalize_emote_choice(
        {"id": "09", "mode": "replace", "reason": STRONG_REASON},
        library=_library(),
        is_song=False,
    ) == ("09", "replace", STRONG_REASON)


@pytest.mark.parametrize(
    ("value", "is_song"),
    [
        (None, False),
        ("09", False),                                                # not an object
        ({"id": "77", "mode": "replace", "reason": STRONG_REASON}, False),  # unknown id
        ({"id": "09", "mode": "overlay", "reason": STRONG_REASON}, False),  # bad mode
        ({"id": "09", "mode": "replace", "reason": "好看"}, False),         # bare reason
        ({"id": "09", "mode": "replace"}, False),                           # no reason
        ({"id": "09", "mode": "replace", "reason": STRONG_REASON}, True),   # song cover
    ],
)
def test_normalize_emote_choice_falls_back_to_default_redraw(value, is_song):
    assert normalize_emote_choice(value, library=_library(), is_song=is_song) == ("", "", "")


def test_normalize_emote_choice_requires_enabled_library():
    pick = {"id": "09", "mode": "replace", "reason": STRONG_REASON}
    assert normalize_emote_choice(pick, library=EMPTY_EMOTE_LIBRARY, is_song=False) == ("", "", "")
    disabled = EmoteLibrary(enabled=False, entries=(_entry(),))
    assert normalize_emote_choice(pick, library=disabled, is_song=False) == ("", "", "")


# ---------------------------------------------------------------------------
# Art direction integration
# ---------------------------------------------------------------------------

def test_art_direction_baseline_never_picks_an_emote():
    direction = _lidousha_cover_art_direction(
        candidate_id="cand-1", title=TALK_TITLE, cover_text=TALK_TEXT, emote_library=_library()
    )
    assert (direction.emote_id, direction.emote_mode, direction.emote_reason) == ("", "", "")


def test_art_direction_judge_emote_pick_survives_normalization():
    def judge(prompt: str) -> str:
        # The catalog and the emote output field are offered on talk covers.
        assert "表情包(可选,默认不用)" in prompt
        assert '"emote"' in prompt
        return json.dumps(
            {"emote": {"id": "09", "mode": "replace", "reason": STRONG_REASON}},
            ensure_ascii=False,
        )

    direction = _lidousha_cover_art_direction(
        candidate_id="cand-1",
        title=TALK_TITLE,
        cover_text=TALK_TEXT,
        art_direction_llm_call=judge,
        emote_library=_library(),
    )
    assert direction.emote_id == "09"
    assert direction.emote_mode == "replace"
    assert direction.emote_reason == STRONG_REASON


def test_art_direction_song_prompt_offers_no_emotes_and_strips_picks():
    def judge(prompt: str) -> str:
        assert "表情包(可选,默认不用)" not in prompt  # songs never see the catalog
        return json.dumps(
            {"emote": {"id": "09", "mode": "replace", "reason": STRONG_REASON}},
            ensure_ascii=False,
        )

    direction = _lidousha_cover_art_direction(
        candidate_id="song-1",
        title=SONG_TITLE,
        cover_text=SONG_TEXT,
        art_direction_llm_call=judge,
        emote_library=_library(),
    )
    assert direction.is_song is True
    assert direction.emote_id == ""


def test_art_direction_fail_open_keeps_no_emote():
    def raising(_prompt: str) -> str:
        raise RuntimeError("judge down")

    direction = _lidousha_cover_art_direction(
        candidate_id="cand-1",
        title=TALK_TITLE,
        cover_text=TALK_TEXT,
        art_direction_llm_call=raising,
        emote_library=_library(),
    )
    assert direction.emote_id == ""


def test_emote_catalog_prompt_block_lists_policy_and_entries():
    block = emote_catalog_prompt_block(_library(_entry(), _entry(id="25", label="只是熊猫", subject="panda_creature")))
    assert "默认必须用人物重绘" in block
    assert "- 09 打call" in block
    assert "兽形熊猫本体" in block
    assert "歌切封面永不使用表情包" in block
    assert emote_catalog_prompt_block(EMPTY_EMOTE_LIBRARY) == ""


# ---------------------------------------------------------------------------
# Prompt branches
# ---------------------------------------------------------------------------

def _talk_direction(**emote_fields):
    import dataclasses

    direction = _lidousha_cover_art_direction(
        candidate_id="cand-1", title=TALK_TITLE, cover_text=TALK_TEXT
    )
    return dataclasses.replace(direction, **emote_fields)


def test_replace_prompt_swaps_subject_to_the_sticker():
    direction = _talk_direction(emote_id="09", emote_mode="replace", emote_reason=STRONG_REASON)
    prompt = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=direction, emote=_entry()
    )
    # Sticker is the subject; the light-redraw contract is explicit.
    assert "OFFICIAL chibi emote stickers" in prompt
    assert "STICKER FIDELITY" in prompt
    assert "do NOT restyle or redesign it" in prompt
    # Mutually exclusive with the character redraw: no live-frame outfit copy.
    assert "PRESERVE THE EXACT OUTFIT" not in prompt
    # Same layout economy as the character: subject placement + reserved title zone.
    assert "reserved for a title" in prompt or "reserved for a big title" in prompt
    # The local overlay still owns all text.
    assert "ABSOLUTELY NO text" in prompt


def test_replace_prompt_orders_caption_omission_for_baked_text():
    direction = _talk_direction(emote_id="01", emote_mode="replace", emote_reason=STRONG_REASON)
    entry = _entry(id="01", label="别走好吗", baked_text="别走好吗")
    prompt = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=direction, emote=entry
    )
    assert "别走好吗" in prompt
    assert "OMIT that caption completely" in prompt


def test_replace_prompt_keeps_panda_creature_unhumanized():
    direction = _talk_direction(emote_id="25", emote_mode="replace", emote_reason=STRONG_REASON)
    entry = _entry(id="25", label="只是熊猫", subject="panda_creature")
    prompt = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=direction, emote=entry
    )
    assert "PANDA-CREATURE" in prompt
    assert "do NOT humanize" in prompt


def test_companion_prompt_keeps_character_and_adds_secondary_sticker():
    direction = _talk_direction(
        emote_id="25", emote_mode="companion", emote_reason="她全程在跟kmx对话,用只是熊猫代画kmx"
    )
    entry = _entry(id="25", label="只是熊猫", subject="panda_creature")
    prompt = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=direction, emote=entry
    )
    # The character redraw contract survives; the sticker is secondary.
    assert "PRESERVE THE EXACT OUTFIT" in prompt
    assert "COMPANION STICKER" in prompt
    assert "SECONDARY companion" in prompt
    assert "代画kmx" in prompt


def test_prompt_without_emote_entry_ignores_emote_art_direction():
    direction = _talk_direction(emote_id="09", emote_mode="replace", emote_reason=STRONG_REASON)
    with_fields = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=direction, emote=None
    )
    plain = _lidousha_cover_prompt(
        title=TALK_TITLE, cover_text=TALK_TEXT, art_direction=_talk_direction()
    )
    assert with_fields == plain  # the sticker can never appear without its resolved entry


# ---------------------------------------------------------------------------
# Media resolution + companion composite
# ---------------------------------------------------------------------------

def test_resolve_emote_reference_verifies_manifest_sha(tmp_path, monkeypatch):
    entry = _sticker_with_media(tmp_path, monkeypatch)
    resolved, detail = resolve_emote_reference(entry, repo_root=ROOT)
    assert resolved is not None and resolved.is_file()
    assert detail == ""


def test_resolve_emote_reference_rejects_drifted_bytes(tmp_path, monkeypatch):
    import dataclasses

    entry = dataclasses.replace(_sticker_with_media(tmp_path, monkeypatch), hd_sha256="f" * 64)
    resolved, detail = resolve_emote_reference(entry, repo_root=ROOT)
    assert resolved is None
    assert "EMOTE_MEDIA_SHA_MISMATCH" in detail


def test_resolve_emote_reference_reports_missing_media(tmp_path, monkeypatch):
    monkeypatch.setenv(cover_emote.EMOTE_MEDIA_ROOT_ENV, str(tmp_path / "empty"))
    resolved, detail = resolve_emote_reference(_entry(), repo_root=tmp_path)
    assert resolved is None
    assert "EMOTE_MEDIA_MISSING" in detail


def test_resolve_emote_reference_probes_manifest_runtime_roots(tmp_path, monkeypatch):
    """Free's tree-out install: no env var — the manifest-declared runtime root
    (probed after env, before the repo default) serves the verified bytes."""
    import dataclasses
    from PIL import Image

    monkeypatch.delenv(cover_emote.EMOTE_MEDIA_ROOT_ENV, raising=False)
    runtime_root = tmp_path / "opt" / "emotes"
    entry = _entry()
    target = runtime_root / entry.hd_file
    target.parent.mkdir(parents=True)
    Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(target)
    entry = dataclasses.replace(entry, hd_sha256=hashlib.sha256(target.read_bytes()).hexdigest())

    resolved, detail = resolve_emote_reference(
        entry, repo_root=tmp_path, runtime_roots=(str(runtime_root),)
    )
    assert resolved == target and detail == ""

    # The env override still wins over the declared runtime root.
    env_root = tmp_path / "override"
    env_target = env_root / entry.hd_file
    env_target.parent.mkdir(parents=True)
    env_target.write_bytes(target.read_bytes())
    monkeypatch.setenv(cover_emote.EMOTE_MEDIA_ROOT_ENV, str(env_root))
    resolved, _ = resolve_emote_reference(
        entry, repo_root=tmp_path, runtime_roots=(str(runtime_root),)
    )
    assert resolved == env_target


def test_committed_manifest_declares_free_runtime_root():
    library = load_emote_library(ROOT)
    assert "/opt/bilive/autoslice/assets/emote" in library.runtime_roots


def test_compose_companion_reference_insets_sticker(tmp_path):
    from PIL import Image

    frame = tmp_path / "frame.png"
    sticker = tmp_path / "sticker.png"
    Image.new("RGB", (640, 360), (10, 20, 30)).save(frame)
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(sticker)
    out = compose_companion_reference(frame, sticker, tmp_path / "out" / "composite.png")
    with Image.open(out) as composed:
        assert composed.size == (640, 360)
        # The inset (plus its white border) lands in the bottom-right quadrant.
        assert composed.getpixel((int(640 * 0.92), int(360 * 0.85)))[:3] != (10, 20, 30)


# ---------------------------------------------------------------------------
# Staging integration (reference swap + disclosed downgrade)
# ---------------------------------------------------------------------------

def _write_media(tmp_path: Path) -> Path:
    media = tmp_path / "recuts" / "clip.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1",
            "-pix_fmt", "yuv420p", str(media),
        ],
        check=True,
        capture_output=True,
    )
    return media


def _fake_image_edit(captured: dict):
    from PIL import Image

    def image_edit(*, output_path, request_path, response_path, reference_path, prompt, **_kwargs):
        captured["reference_path"] = Path(reference_path)
        captured["prompt"] = prompt
        request_path.write_text("{}", encoding="utf-8")
        response_path.write_text("{}", encoding="utf-8")
        Image.new("RGB", (1920, 1080), (30, 80, 140)).save(output_path)
        return {"status": "AI_BACKGROUND_READY", "output_path": str(output_path)}

    return image_edit


def _judge_pick(emote_id: str = "09", mode: str = "replace"):
    def judge(_prompt: str) -> str:
        return json.dumps(
            {"emote": {"id": emote_id, "mode": mode, "reason": STRONG_REASON}}, ensure_ascii=False
        )

    return judge


def test_stage_cover_uses_verified_sticker_as_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    entry = _sticker_with_media(tmp_path, monkeypatch)
    monkeypatch.setattr(publish_staging, "load_emote_library", lambda root: _library(entry))
    media = _write_media(tmp_path)
    captured: dict = {}

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media), "manifest_path": str(media.with_suffix(".manifest.json"))},
        media_path=media,
        candidate_id="emote-replace",
        title=TALK_TITLE,
        cover_text=TALK_TEXT,
        run_ffmpeg=True,
        art_direction_llm_call=_judge_pick(),
        image_edit=_fake_image_edit(captured),
    )

    assert result["status"] == "AI_COVER_READY"
    generation = result["cover_generation"]
    assert generation["emote"]["status"] == "EMOTE_REFERENCE_READY"
    assert generation["emote"]["mode"] == "replace"
    assert generation["art_direction"]["emote_id"] == "09"
    # The CPA reference IS the sticker (subject swap), and evidence records it.
    assert captured["reference_path"].name == "09_李豆沙_打call.png"
    assert generation["reference_image"] == str(captured["reference_path"])
    assert "OFFICIAL chibi emote stickers" in captured["prompt"]


def test_stage_cover_companion_composites_frame_plus_sticker(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    entry = _sticker_with_media(tmp_path, monkeypatch, id="25", label="只是熊猫", subject="panda_creature",
                                file="25_李豆沙_只是熊猫.png", hd_file="hd/25_李豆沙_只是熊猫.png")
    monkeypatch.setattr(publish_staging, "load_emote_library", lambda root: _library(entry))
    media = _write_media(tmp_path)
    captured: dict = {}

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media), "manifest_path": str(media.with_suffix(".manifest.json"))},
        media_path=media,
        candidate_id="emote-companion",
        title=TALK_TITLE,
        cover_text=TALK_TEXT,
        run_ffmpeg=True,
        art_direction_llm_call=_judge_pick("25", "companion"),
        image_edit=_fake_image_edit(captured),
    )

    assert result["status"] == "AI_COVER_READY"
    assert captured["reference_path"].name == "emote-companion.cover-ref.with-emote.png"
    assert "COMPANION STICKER" in captured["prompt"]
    assert "PRESERVE THE EXACT OUTFIT" in captured["prompt"]


def test_stage_cover_downgrades_to_default_redraw_on_sha_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    import dataclasses

    drifted = dataclasses.replace(_sticker_with_media(tmp_path, monkeypatch), hd_sha256="f" * 64)
    monkeypatch.setattr(publish_staging, "load_emote_library", lambda root: _library(drifted))
    media = _write_media(tmp_path)
    captured: dict = {}

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media), "manifest_path": str(media.with_suffix(".manifest.json"))},
        media_path=media,
        candidate_id="emote-drift",
        title=TALK_TITLE,
        cover_text=TALK_TEXT,
        run_ffmpeg=True,
        art_direction_llm_call=_judge_pick(),
        image_edit=_fake_image_edit(captured),
    )

    # The cover still ships — as the default character redraw, with disclosure.
    assert result["status"] == "AI_COVER_READY"
    generation = result["cover_generation"]
    assert generation["emote"]["status"] == "FALLBACK_DEFAULT_REDRAW"
    assert "EMOTE_MEDIA_SHA_MISMATCH" in generation["emote"]["detail"]
    assert generation["art_direction"]["emote_id"] == ""
    assert captured["reference_path"].name == "emote-drift.cover-ref.png"
    assert "OFFICIAL chibi emote stickers" not in captured["prompt"]
