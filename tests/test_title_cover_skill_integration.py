"""Task D regression: real Pillow pixels, deterministic fake CPA, no network."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageOps

from src.autoslice.cover_punch_semantics import _validated_punch_lines
from src.autoslice.title_cover_candidates import (
    SCHEMA,
    choose_rows,
    score_candidate,
    sha,
    text_sha,
    validate_choice_bundle,
)
from src.autoslice.title_cover_staging import stage_title_cover_candidates
from src.autoslice.cover_private_font import FILE_NAME, resolve_private_cover_font
from src.autoslice.cover_style import validate_spec

SRT = "1\n00:00:01,000 --> 00:00:03,000\n小李说什么，不许讨厌我\n\n2\n00:00:04,000 --> 00:00:06,000\n我讨厌你，你不许讨厌我\n"
LINES = ["小李：什么", "不许讨厌我"]


def verdict(*, present=None, hook=2, face=None):
    return {
        "people": {
            "expected": ["李豆沙", "kmx"],
            "present": present or ["李豆沙", "kmx"],
            "reason": "OFFLINE fixture: all specified participants are drawn",
        },
        "hook_fit": {"score": hook, "reason": "OFFLINE fixture: visible sourced reaction"},
        **{
            key: {"pass": True, "reason": "OFFLINE fixture: comparison against synthetic source"}
            for key in ("clean_layers", "identity", "source_faithful", "no_fabrication")
        },
        "face": face or [1300, 300, 1700, 850],
        "keep": [],
    }


def row(identifier="a", route="screenshot", size=200, **kwargs):
    return {
        "id": identifier,
        "route": route,
        "status": "RENDERED",
        "punch_font_size": size,
        "verdict": verdict(**kwargs),
    }


def test_skill_formula_saturation_and_realism_tie():
    recent = {"previous_route": "ai_scene"}
    a = score_candidate(row(size=200), participants=["李豆沙", "kmx"], recent=recent)
    b = score_candidate(row(size=600), participants=["李豆沙", "kmx"], recent=recent)
    assert a == b and a["total"] == 15
    rendered, selected = choose_rows(
        [row("ai", "ai_sticker", size=200), row("real", "screenshot", size=150)],
        participants=["李豆沙", "kmx"],
        recent=recent,
    )
    assert rendered[0]["score"]["total"] == rendered[1]["score"]["total"]
    assert selected == "real"


@pytest.mark.parametrize(
    "field", ["people", "hook_fit", "clean_layers", "identity", "source_faithful", "no_fabrication"]
)
def test_every_cpa_axis_needs_a_reason(field):
    candidate = row()
    candidate["verdict"][field]["reason"] = ""
    assert score_candidate(candidate, participants=["李豆沙", "kmx"], recent={})["status"] == "VETO"


def test_missing_people_and_repeated_emote_are_not_boolean_shortcuts():
    candidate = row(present=["李豆沙"])
    result = score_candidate(candidate, participants=["李豆沙", "kmx"], recent={})
    assert result["P"] == 1
    candidate["main_emote"] = "emote:08"
    assert (
        "MAIN_EMOTE_REPEATED"
        in score_candidate(
            candidate,
            participants=["李豆沙", "kmx"],
            recent={"source_session_main_emotes": ["emote:08"]},
        )["vetoes"]
    )
    candidate["verdict"]["people"]["expected"] = ["李豆沙"]
    assert (
        "STORY_PARTICIPANTS_DISAGREE"
        in score_candidate(candidate, participants=["李豆沙", "kmx"], recent={})["vetoes"]
    )


@pytest.mark.parametrize("face", [[1300, 300, 1700, 569], [1300, 300, 2000, 900]])
def test_observed_face_floor_and_bounds(face):
    assert (
        score_candidate(row(face=face), participants=["李豆沙", "kmx"], recent={})["status"]
        == "VETO"
    )


def test_final_image_face_cannot_hide_behind_false_layout_box():
    candidate = row()
    candidate["render"] = {"measured_lines": [{"x": 1300, "y": 320, "w": 250, "h": 200}]}
    assert (
        "FINAL_TEXT_OVERLAPS_OBSERVED_FACE_OR_IDENTITY"
        in score_candidate(candidate, participants=["李豆沙", "kmx"], recent={})["vetoes"]
    )


@pytest.mark.parametrize(
    "line,valid",
    [
        ("小李：什么", True),
        ("好", True),
        ("一二三四五六七八九", True),
        ("一二三四五六七八九十", False),
        ("你好，朋友", False),
    ],
)
def test_colon_and_nine_character_shape(line, valid):
    assert bool(_validated_punch_lines({"main": line}, "")) is valid


def test_layout_does_not_accept_authored_font_size_or_missing_face():
    base = {"face": [1300, 300, 1700, 850], "lines": [{"text": "怎么啦", "size": 250}]}
    with pytest.raises(ValueError, match="SIZE_IS_MEASURED"):
        validate_spec(base)
    with pytest.raises(ValueError, match="BOX_INVALID"):
        validate_spec({"lines": [{"text": "怎么啦"}]})


def test_private_font_cannot_be_claimed_by_package_or_wrong_hash(tmp_path, monkeypatch):
    private = tmp_path.resolve() / "private"
    private.mkdir()
    font = private / FILE_NAME
    font.write_bytes(b"OFFLINE TEST FONT FORMAT STUB -- not a font")
    manifest = private / "cover-font.json"
    binding = {
        "schema_version": "lidousha-private-cover-font.v1",
        "acquisition": "OFFICIAL_DOWNLOAD",
        "font_file_name": FILE_NAME,
        "source_page": "https://www.reeji.com/fixture",
        "download_url": "https://www.reeji.com/fixture.ttf",
        "font_family": "REEJI-ZhenyanGB",
        "font_style": "Regular",
        "font_sha256": "sha256:" + "0" * 64,
    }
    manifest.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        resolve_private_cover_font(binding_path=manifest)
    binding["font_sha256"] = sha(font)
    binding["download_url"] = "https://mirror.invalid/a.ttf"
    manifest.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="SOURCE_INVALID"):
        resolve_private_cover_font(binding_path=manifest)
    binding["download_url"] = "https://www.reeji.com/fixture.ttf"
    manifest.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="FORMAT_INVALID"):
        resolve_private_cover_font(binding_path=manifest)
    monkeypatch.setenv("LIDOUSHA_COVER_FONT_BINDING", str(private / "missing.json"))
    with pytest.raises(ValueError, match="BINDING_MISSING"):
        resolve_private_cover_font()


@pytest.fixture(scope="module")
def offline_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("offline-cover-candidates").resolve()
    reference = root / "synthetic-source.png"
    canvas = Image.new("RGB", (1920, 1080), "#487b92")
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((1300, 300, 1700, 850), fill="#fed6bd", outline="#242d4a", width=12)
    draw.ellipse((1390, 470, 1420, 515), fill="#242d4a")
    draw.ellipse((1540, 470, 1570, 515), fill="#242d4a")
    draw.text((25, 1020), "OFFLINE SYNTHETIC FIXTURE / NOT A PRODUCTION COVER", fill="white")
    canvas.save(reference)
    alternate = root / "synthetic-alternate.png"
    ImageOps.mirror(canvas).save(alternate)
    plan = {
        "participants": [{"name": "李豆沙", "quote": "不许讨厌我"}],
        "cover_punch": {"main": LINES[0], "sub": LINES[1]},
        "candidates": [
            {
                "id": "left",
                "route": "screenshot",
                "source_ids": ["frame"],
                "reason": "OFFLINE left layout test",
            },
            {
                "id": "top",
                "route": "screenshot",
                "source_ids": ["frame:1"],
                "reason": "OFFLINE alternate layout test",
            },
        ],
        "rejected_routes": [{"route": "emote", "reason": "OFFLINE fixture has no official assets"}],
    }
    calls = {"text": 0, "vision": 0, "image": 0}

    def llm(prompt):
        calls["text"] += 1
        if "最终文字语义裁决者" in prompt:
            return json.dumps(
                {
                    "schema_version": "lidousha-cover-punch-semantic-review.v2",
                    "status": "PASS",
                    "final_punch": {"main": LINES[0], "sub": LINES[1]},
                    "stranger_can_infer_event": True,
                    "contains_concrete_subject": True,
                    "contains_action_or_conflict": True,
                    "no_fabricated_fact": True,
                    "story_summary": "OFFLINE fixture: a reaction to a spoken line",
                    "click_motivation": "OFFLINE fixture: source-backed emotional contrast",
                },
                ensure_ascii=False,
            )
        return json.dumps(plan, ensure_ascii=False)

    def probe(paths, question, **options):
        calls["vision"] += 1
        alternate_layout = "top" in paths[0].parts
        observed_face = [220, 300, 620, 850] if alternate_layout else [1300, 300, 1700, 850]
        if "封面排版见证" in question:
            answer = {
                "face": observed_face,
                "keep": [],
                "layout": ("right" if alternate_layout else "left"),
                "crop": [0, 0, 1920],
                "reason": "OFFLINE synthetic geometry",
            }
        else:
            answer = verdict(present=["李豆沙"], face=observed_face)
            answer["people"]["expected"] = ["李豆沙"]
        raw = options["raw_response_path"]
        raw.write_text(
            json.dumps(
                {
                    "output_text": json.dumps(answer, ensure_ascii=False),
                    "fixture": "OFFLINE_TEST_NOT_A_PROVIDER_RESPONSE",
                },
                ensure_ascii=False,
            )
        )
        model = "OFFLINE_TEST_STUB"
        return {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "model": model,
            "status": "OBSERVED",
            "question": question,
            "answer": json.dumps(answer, ensure_ascii=False),
            "images": [{"image_path": str(path), "image_sha256": sha(path)} for path in paths],
            "response_sha256": sha(raw),
            "prompt_sha256": text_sha(
                json.dumps(
                    {
                        "question": question,
                        "image_sha256s": [sha(path) for path in paths],
                        "model": model,
                    },
                    sort_keys=True,
                )
            ),
        }

    def image_edit(**_):
        calls["image"] += 1
        raise AssertionError("No image model call authorized by offline screenshot fixture")

    font = Path(__file__).resolve().parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"
    generation = {
        "workflow": "OFFLINE_FIXTURE",
        "cover_text": "玩自己的语音，小李反应",
        "story_contract": {},
        "reference_selection": {
            "policy": "OFFLINE_FIXTURE",
            "media_sha256": text_sha("synthetic"),
            "best_ms": 1000,
        },
    }
    result = stage_title_cover_candidates(
        record={},
        candidate_id="offline_skill_candidate",
        title="【李豆沙】玩自己的语音：不许讨厌我",
        cover_text=generation["cover_text"],
        final_srt=SRT,
        reference_path=reference,
        artifact_root=root / "generation",
        generation=generation,
        llm_call=llm,
        image_edit=image_edit,
        emote_library=None,
        repo_root=Path(__file__).resolve().parents[1],
        history_root=root / "history",
        base_url="",
        api_key="",
        image_probe=probe,
        extra_frames=[
            {
                "path": alternate,
                "frame_ms": 2000,
                "subtitle_cue": "2",
                "intro_offset_ms": 0,
                "sha256": sha(alternate),
                "audio_used": False,
            }
        ],
        font_resolver=lambda: (font, {"status": "OFFLINE_EXISTING_FONT_FIXTURE_NOT_REEJI"}),
    )
    (root / "fixture-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result, font, root, calls


def test_offline_candidate_producer_renders_scores_and_seals(offline_bundle):
    result, font, root, calls = offline_bundle
    gen = result["cover_generation"]
    assert result["status"] == "AI_COVER_READY"
    assert calls == {"text": 2, "vision": 4, "image": 0}
    bundle = Path(gen["candidate_selection"]["bundle_root"])
    payload = json.loads((bundle / "choose.json").read_text())
    assert payload["execution_mode"] == "OFFLINE_TEST"
    assert payload["schema_version"] == SCHEMA and payload["cover_lines"] == LINES
    assert len(payload["candidates"]) == 2
    assert len({row["render"]["final_cover_sha256"] for row in payload["candidates"]}) == 2
    assert (bundle / "candidates.jpg").is_file()
    assert validate_choice_bundle(gen, font_path=font) == []
    assert validate_choice_bundle(gen) == ["COVER_OFFLINE_FIXTURE_NOT_PUBLISHABLE"]
    assert root.is_dir()


def test_scoring_font_is_recomputed_from_native_render(offline_bundle, tmp_path):
    result, font, _, _ = offline_bundle
    gen = copy.deepcopy(result["cover_generation"])
    bundle = Path(gen["candidate_selection"]["bundle_root"])
    import shutil

    copied = tmp_path.resolve() / "forged"
    shutil.copytree(bundle, copied)
    payload = json.loads((copied / "choose.json").read_text())
    selected = next(row for row in payload["candidates"] if row["id"] == payload["selected"])
    selected["punch_font_size"] += 1
    selected["render"]["punch_font_size"] += 1
    (copied / "choose.json").write_text(json.dumps(payload))
    gen["candidate_selection"]["choose_sha256"] = sha(copied / "choose.json")
    assert "COVER_CHOICE_FONT_SCORE_NOT_MEASURED" in validate_choice_bundle(
        gen, root=copied, font_path=font
    )


def test_final_cover_pixel_mutation_is_rejected(offline_bundle, tmp_path):
    result, font, _, _ = offline_bundle
    original = Path(result["cover_path"])
    target = tmp_path.resolve() / "mutated.png"
    with Image.open(original) as image:
        image.putpixel((0, 0), (255, 0, 0))
        image.save(target)
    assert validate_choice_bundle(
        result["cover_generation"], final_cover=target, font_path=font
    ) == ["COVER_CHOICE_PACKAGE_FINAL_DRIFT"]


def test_source_drift_rejected_even_with_unmodified_final(offline_bundle, tmp_path):
    result, font, _, _ = offline_bundle
    import shutil

    copied = tmp_path.resolve() / "drifted"
    shutil.copytree(Path(result["cover_generation"]["candidate_selection"]["bundle_root"]), copied)
    with (copied / "sources/frame.png").open("ab") as out:
        out.write(b"changed")
    assert validate_choice_bundle(result["cover_generation"], root=copied, font_path=font)[
        0
    ].startswith("COVER_CHOICE_ARTIFACT_HASH_MISMATCH")


def _portable_fixture(offline_bundle, tmp_path, monkeypatch):
    import shutil
    from src.autoslice import title_cover_package as package

    result, font, _, _ = offline_bundle
    generation = copy.deepcopy(result["cover_generation"])
    real_validate = validate_choice_bundle
    monkeypatch.setattr(
        package,
        "validate_choice_bundle",
        lambda value, **kwargs: real_validate(value, font_path=font, **kwargs),
    )
    destination = tmp_path.resolve() / "package"
    destination.mkdir()
    fields = package.choice_manifest_fields(generation, package_root=destination)
    shutil.copyfile(result["cover_path"], destination / "cover.png")
    (destination / "final.srt").write_text(SRT)
    item = {
        "candidate_id": generation["candidate_selection"]["candidate_id"],
        "cover": "cover.png",
        "subtitle_srt": "final.srt",
        **fields,
    }
    (destination / "review_manifest.json").write_text(json.dumps({"items": [item]}))
    assert package.package_choice_problems(destination, item, generation) == []
    return package, destination, item, generation, font


def test_portable_bundle_import_survives_absent_producer(offline_bundle, tmp_path, monkeypatch):
    from src.autoslice.package_import_candidate_cover import (
        project_candidate_cover_locators,
        package_candidate_cover,
    )

    package, destination, item, generation, _ = _portable_fixture(
        offline_bundle, tmp_path, monkeypatch
    )
    original = Path(generation["candidate_selection"]["choose_path"]).parent
    parked = original.with_name(original.name + "-temporarily-absent")
    raw_hashes = {
        str(p.relative_to(destination)): sha(p) for p in destination.rglob("*") if p.is_file()
    }
    record = {
        "subtitle_path": "/original/candidate/replacement_recuts/final.srt",
        "publish_staging": {
            "cover_generation": copy.deepcopy(generation),
            "cover_path": generation["final_cover"],
        },
    }
    publish = {
        "cover_generation": copy.deepcopy(generation),
        "cover_path": generation["final_cover"],
    }
    before = copy.deepcopy((record, publish))
    original.rename(parked)
    try:
        assert not original.exists()
        assert package.package_choice_problems(destination, item, generation) == []
        projected_record, projected_publish = project_candidate_cover_locators(
            record,
            publish,
            package_root=destination,
            candidate_id=item["candidate_id"],
        )
        projected = projected_publish["cover_generation"]
        assert projected == projected_record["publish_staging"]["cover_generation"]
        assert (
            projected["candidate_selection"]["bundle_root"]
            == generation["candidate_selection"]["bundle_root"]
        )
        assert (
            projected["candidate_selection"]["choose_path"]
            != generation["candidate_selection"]["choose_path"]
        )
        assert package_candidate_cover(destination, projected) == destination / "cover.png"
        assert (record, publish) == before
        assert {
            str(p.relative_to(destination)): sha(p) for p in destination.rglob("*") if p.is_file()
        } == raw_hashes
    finally:
        parked.rename(original)


@pytest.mark.parametrize("role", ["mask_path", "provider_response"])
def test_incomplete_package_cannot_fall_back_to_complete_producer(
    offline_bundle,
    tmp_path,
    monkeypatch,
    role,
):
    package, destination, item, generation, font = _portable_fixture(
        offline_bundle, tmp_path, monkeypatch
    )
    choose = destination / item["cover_choice"]
    payload = json.loads(choose.read_text())
    selected = next(row for row in payload["candidates"] if row["id"] == payload["selected"])
    relative = (
        selected["mask_path"]
        if role == "mask_path"
        else "candidates/" + selected["id"] + "/score.provider-response.json"
    )
    original = Path(generation["candidate_selection"]["choose_path"]).parent / relative
    assert original.is_file()
    (choose.parent / relative).unlink()
    problems = package.package_choice_problems(destination, item, generation)
    assert problems and "OFFLINE_FIXTURE_NOT_PUBLISHABLE" not in str(problems)
    assert validate_choice_bundle(generation, font_path=font) == []


def test_original_cpa_response_constrains_rehashed_scores(offline_bundle, tmp_path):
    import shutil

    result, font, _, _ = offline_bundle
    generation = copy.deepcopy(result["cover_generation"])
    original = Path(generation["candidate_selection"]["choose_path"]).parent
    copied = tmp_path.resolve() / "forged-choice"
    shutil.copytree(original, copied)
    payload = json.loads((copied / "choose.json").read_text())
    candidate = next(row for row in payload["candidates"] if row["id"] != payload["selected"])
    candidate["verdict"]["clean_layers"] = {
        "pass": False,
        "reason": "FORGED local verdict; original offline CPA fixture left untouched",
    }
    rows, selected = choose_rows(
        payload["candidates"],
        participants=payload["participants"],
        recent=payload["recent"],
    )
    assert selected == payload["selected"]
    payload["candidates"] = rows
    (copied / "choose.json").write_text(json.dumps(payload))
    generation["candidate_selection"]["choose_sha256"] = sha(copied / "choose.json")
    assert validate_choice_bundle(generation, root=copied, font_path=font) == [
        "COVER_CHOICE_VERDICT_DRIFT"
    ]


def test_same_bytes_mask_at_unmapped_locator_is_rejected(offline_bundle, tmp_path):
    import shutil

    result, font, _, _ = offline_bundle
    generation = copy.deepcopy(result["cover_generation"])
    destination = tmp_path.resolve() / "unmapped-mask.png"
    shutil.copyfile(generation["rendered_text_pixels"]["mask_path"], destination)
    generation["rendered_text_pixels"]["mask_path"] = str(destination)
    assert validate_choice_bundle(generation, font_path=font) == [
        "COVER_CHOICE_PIXEL_LOCATOR_ROLE_DRIFT"
    ]


@pytest.mark.parametrize("fault", ["duplicate_candidate", "wrong_candidate", "record_mirror"])
def test_package_import_rejects_candidate_and_mirror_ambiguity(
    offline_bundle,
    tmp_path,
    monkeypatch,
    fault,
):
    from src.autoslice.package_import_candidate_cover import project_candidate_cover_locators

    _, destination, item, generation, _ = _portable_fixture(offline_bundle, tmp_path, monkeypatch)
    record = {
        "subtitle_path": "/original/candidate/replacement_recuts/final.srt",
        "publish_staging": {"cover_generation": copy.deepcopy(generation)},
    }
    publish = {"cover_generation": copy.deepcopy(generation)}
    if fault == "record_mirror":
        record["publish_staging"]["cover_generation"]["candidate_selection"]["choose_path"] += (
            ".drift"
        )
    elif fault == "duplicate_candidate":
        (destination / "review_manifest.json").write_text(json.dumps({"items": [item, item]}))
    else:
        generation["candidate_selection"]["candidate_id"] = "other"
        record["publish_staging"]["cover_generation"] = copy.deepcopy(generation)
        publish["cover_generation"] = copy.deepcopy(generation)
    with pytest.raises(ValueError, match="COVER_CHOICE"):
        project_candidate_cover_locators(
            record, publish, package_root=destination, candidate_id=item["candidate_id"]
        )


def test_normal_dispatch_blocks_missing_private_font_before_media_or_provider(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging, title_cover_staging

    media = tmp_path / "offline-not-video.mp4"
    media.write_bytes(b"offline fixture; frame extraction must not run")
    subtitles = tmp_path / "final.srt"
    subtitles.write_text(SRT)
    monkeypatch.setenv("LIDOUSHA_COVER_FONT_BINDING", str(tmp_path / "absent-font.json"))
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "auto")
    monkeypatch.setattr(publish_staging, "_resolve_cover_cpa_credentials", lambda: ("", ""))

    def forbidden(*args, **kwargs):
        raise AssertionError("Font preflight must precede frame extraction or provider use")

    monkeypatch.setattr(title_cover_staging, "pick_final_srt_frames", forbidden)
    result = publish_staging._stage_ai_cover_unbound(
        {"subtitle_path": str(subtitles)},
        media_path=media,
        candidate_id="font-block",
        title="【李豆沙】玩自己的语音：不许讨厌我",
        cover_text="不许讨厌我",
        run_ffmpeg=True,
        image_edit=forbidden,
        art_direction_llm_call=forbidden,
        private_artifact_root=tmp_path / "generation",
    )
    assert result["reason_codes"] == [
        "TITLE_COVER_SKILL_BLOCKED",
        "COVER_PRIVATE_FONT_BINDING_MISSING",
    ]


def test_actual_package_editorial_consumer_uses_portable_choice(
    offline_bundle, tmp_path, monkeypatch
):
    import shutil
    from src.autoslice import title_cover_candidates as candidates_module
    from src.autoslice.package_cover_network import validate_package_cover_network
    from src.autoslice.publish_staging_paths import current_editorial_binding

    _, destination, item, generation, font = _portable_fixture(
        offline_bundle, tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        candidates_module,
        "validate_choice_bundle",
        lambda value, **kwargs: validate_choice_bundle(value, font_path=font, **kwargs),
    )
    cid = item["candidate_id"]
    source = Path(generation["candidate_selection"]["choose_path"]).parent
    record = {"candidate_id": cid, "subtitle_path": str(source / "final.srt")}
    staging = {
        "candidate_id": cid,
        "title": "【李豆沙】玩自己的语音：不许讨厌我",
        "title_source": "reviewer_manual_override",
        "cover_status": "AI_COVER_READY",
        "cover_path": generation["final_cover"],
        "cover_generation": copy.deepcopy(generation),
    }
    binding = current_editorial_binding(record, staging, candidate_id=cid)
    staging["current_binding"] = binding
    record["publish_staging"] = staging
    publish = {**copy.deepcopy(staging), "subtitle_sha256": binding["subtitle_sha256"]}
    item["video"] = "fixture.mp4"
    for role in ("request", "response"):
        target = destination / ("fixture.cover-" + role + ".json")
        shutil.copyfile(generation[role + "_path"], target)
        item["cover_" + role] = target.name
    parked = source.with_name(source.name + "-package-network-test")
    source.rename(parked)
    try:
        validate_package_cover_network(
            destination,
            item,
            record,
            publish,
            subtitle_path=destination / "final.srt",
            cover_path=destination / "cover.png",
        )
        (destination / item["cover_choice"]).unlink()
        with pytest.raises((ValueError, OSError)):
            validate_package_cover_network(
                destination,
                item,
                record,
                publish,
                subtitle_path=destination / "final.srt",
                cover_path=destination / "cover.png",
            )
    finally:
        parked.rename(source)


def test_readback_cli_never_promotes_offline_fixture(offline_bundle, tmp_path):
    from scripts.audit_title_cover_choice import audit

    result, _, _, _ = offline_bundle
    publish = tmp_path / "offline.publish.json"
    publish.write_text(json.dumps({"cover_generation": result["cover_generation"]}))
    receipt = audit(publish)
    assert receipt["status"] == "BLOCKED"
    assert receipt["problems"] == ["COVER_OFFLINE_FIXTURE_NOT_PUBLISHABLE"]
    assert receipt["provider_calls"] == 0 and receipt["publication_authorized"] is False


@pytest.mark.parametrize("damage", ["geometry", "render_spec", "source_path", "composition"])
def test_portable_composition_rejects_rehashed_spec_or_background_drift(
    offline_bundle, tmp_path, damage,
):
    """Source/geometry/pixels agree even if a changed artifact gets a new hash."""
    import shutil

    result, font, _, _ = offline_bundle
    generation = copy.deepcopy(result["cover_generation"])
    original = Path(generation["candidate_selection"]["choose_path"]).parent
    copied = tmp_path.resolve() / "composition-copy"
    shutil.copytree(original, copied)
    choose = copied / "choose.json"
    payload = json.loads(choose.read_text())
    selected = payload["selected"]
    prefix = "candidates/" + selected + "/"
    if damage == "composition":
        relative = prefix + "composition.png"
        path = copied / relative
        with Image.open(path) as source:
            changed = source.convert("RGB")
        changed.putpixel((0, 0), (255, 0, 255))
        changed.save(path)
    elif damage == "geometry":
        relative = prefix + "geometry.response.json"
        path = copied / relative
        witness = json.loads(path.read_text())
        geometry = json.loads(witness["answer"])
        geometry["crop"] = [0, 0, 960]
        witness["answer"] = json.dumps(geometry)
        path.write_text(json.dumps(witness))
    else:
        relative = prefix + "render-spec.json"
        path = copied / relative
        spec = json.loads(path.read_text())
        if damage == "source_path":
            spec["frame"] = "/tmp/model-selected-arbitrary.png"
        else:
            spec["crop"] = [0, 0, 960]
        path.write_text(json.dumps(spec))
    payload["artifacts"][relative] = sha(path)
    choose.write_text(json.dumps(payload))
    generation["candidate_selection"]["choose_sha256"] = sha(choose)
    problems = validate_choice_bundle(generation, root=copied, font_path=font)
    assert problems and any("COVER_COMPOSITION_" in code for code in problems)
