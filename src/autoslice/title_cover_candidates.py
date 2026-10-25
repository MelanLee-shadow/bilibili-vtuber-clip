"""Portable candidate comparison for the approved lidousha-title-cover skill."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from src.autoslice.evidence_identity import same_sha256, sha256_ref
import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

from PIL import Image, ImageOps

SCHEMA = "lidousha-title-cover-choose.v1"
ROUTE_SCHEMA = "lidousha-title-cover-route.v1"
ROUTES = ("screenshot", "screenshot_collage", "emote", "cutout", "ai_sticker", "ai_scene")
REALISM = dict(zip(ROUTES, (2, 2, 2, 1, 0, 0)))
SKILL_COMMIT = "2cabd07361dd4e18bfead8105eb304a6c5368ae4"
AXES = ("people", "hook_fit", "clean_layers", "identity", "source_faithful", "no_fabrication")
SAFE_FLAGS = ("clean_layers", "identity", "source_faithful", "no_fabrication")


def sha(path: Path) -> str:
    return sha256_ref(_evidence_file_sha256(path))


def text_sha(text: str) -> str:
    return sha256_ref(hashlib.sha256(text.encode()).hexdigest())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def strict_file(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("COVER_CHOICE_PATH_INVALID")
    if ".." in Path(relative).parts:
        raise ValueError("COVER_CHOICE_PATH_INVALID")
    candidate = root / relative
    if any(p.is_symlink() for p in (candidate, *candidate.parents)):
        raise ValueError("COVER_CHOICE_SYMLINK")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_file():
        raise ValueError("COVER_CHOICE_PATH_ESCAPE")
    return resolved


def score_candidate(row: Mapping, *, participants: list[str], recent: Mapping) -> dict:
    """Skill choose_cover formula: T saturates at 200; C breaks score ties."""
    vetoes = []
    route = row.get("route")
    if route not in ROUTES:
        raise ValueError("COVER_CANDIDATE_ROUTE_INVALID")
    verdict = row.get("verdict")
    if not isinstance(verdict, Mapping):
        return {"status": "VETO", "vetoes": ["CPA_IMAGE_VERDICT_MISSING"]}
    for key in AXES:
        axis = verdict.get(key)
        if (
            not isinstance(axis, Mapping)
            or not isinstance(axis.get("reason"), str)
            or not axis["reason"].strip()
        ):
            vetoes.append("CPA_REASON_MISSING:" + key)
    for key in SAFE_FLAGS:
        if not isinstance(verdict.get(key), Mapping) or verdict[key].get("pass") is not True:
            vetoes.append("CPA_VETO:" + key)
    if verdict.get("people", {}).get("expected") != participants:
        vetoes.append("STORY_PARTICIPANTS_DISAGREE")
    present = verdict.get("people", {}).get("present")
    if (
        not isinstance(present, list)
        or not all(isinstance(p, str) for p in present)
        or len(set(present)) != len(present)
    ):
        vetoes.append("CPA_PEOPLE_INVALID")
        present = []
    if set(present) - set(participants):
        vetoes.append("UNSOURCED_PARTICIPANT")
    if "李豆沙" not in present:
        vetoes.append("HOST_MISSING")
    face = verdict.get("face")
    if not isinstance(face, list) or len(face) != 4 or any(type(v) is not int for v in face):
        vetoes.append("CPA_FACE_BOX_INVALID")
    elif not (0 <= face[0] < face[2] <= 1920 and 0 <= face[1] < face[3] <= 1080):
        vetoes.append("CPA_FACE_BOX_INVALID")
    elif face[3] - face[1] < 270:
        vetoes.append("MAIN_FACE_BELOW_25_PERCENT")
    keep = verdict.get("keep", [])
    if not isinstance(keep, list):
        vetoes.append("CPA_KEEP_BOX_INVALID")
        keep = []
    protected = ([face] if "CPA_FACE_BOX_INVALID" not in vetoes else []) + keep
    measured = (row.get("render") or {}).get("measured_lines", [])
    for box in protected:
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(v) is not int for v in box)
            or not (0 <= box[0] < box[2] <= 1920 and 0 <= box[1] < box[3] <= 1080)
        ):
            vetoes.append("CPA_KEEP_BOX_INVALID")
            continue
        for line in measured:
            if max(line["x"], box[0]) < min(line["x"] + line["w"], box[2]) and max(
                line["y"], box[1]
            ) < min(line["y"] + line["h"], box[3]):
                vetoes.append("FINAL_TEXT_OVERLAPS_OBSERVED_FACE_OR_IDENTITY")
    size = row.get("punch_font_size")
    if type(size) is not int or size < 120:
        vetoes.append("PUNCH_BELOW_120")
        size = 0
    hook = verdict.get("hook_fit", {}).get("score")
    if type(hook) is not int or hook not in (0, 1, 2):
        vetoes.append("CPA_HOOK_SCORE_INVALID")
        hook = 0
    emote = row.get("main_emote")
    last_five = recent.get("last_five_main_emotes", [])
    if emote and (emote in last_five or emote in recent.get("source_session_main_emotes", [])):
        vetoes.append("MAIN_EMOTE_REPEATED")
    people = 2 if set(participants) <= set(present) else 1 if "李豆沙" in present else 0
    typography = 2 if size >= 200 else 1 if size >= 150 else 0
    fresh = int(route != recent.get("previous_route") and (not emote or emote not in last_five))
    realism = REALISM[route]
    return {
        "status": "VETO" if vetoes else "ELIGIBLE",
        "vetoes": vetoes,
        "P": people,
        "T": typography,
        "H": hook,
        "C": realism,
        "V": fresh,
        "total": 3 * people + 2 * typography + hook + realism + fresh,
    }


def choose_rows(
    rows: list[dict], *, participants: list[str], recent: Mapping
) -> tuple[list[dict], str]:
    if not 2 <= len(rows) <= 4 or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("COVER_REQUIRES_TWO_TO_FOUR_CANDIDATES")
    evaluated = []
    for row in rows:
        current = dict(row)
        current["score"] = (
            score_candidate(row, participants=participants, recent=recent)
            if row.get("status") == "RENDERED"
            else {"status": "VETO", "vetoes": [str(row.get("failure") or "RENDER_FAILED")]}
        )
        evaluated.append(current)
    eligible = [row for row in evaluated if row["score"]["status"] == "ELIGIBLE"]
    if not eligible:
        raise ValueError("COVER_NO_ELIGIBLE_CANDIDATE")
    winner = max(eligible, key=lambda row: (row["score"]["total"], row["score"]["C"]))
    return evaluated, winner["id"]


def jpeg_snapshot(path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as source:
        source.convert("RGB").save(target, format="JPEG", quality=95, subsampling=0)


def parse_answer(text: str) -> dict:
    text = text.strip()
    if text.startswith("~~~") or text.startswith(chr(96) * 3):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("COVER_CPA_VERDICT_INVALID")
    return value


def witness_verdict(
    witness: Mapping, image_paths: list[Path], *, provider_response: Path | None = None
) -> dict:
    if witness.get("provider") != "cpa" or witness.get("status") != "OBSERVED":
        raise ValueError("COVER_CPA_IMAGE_OBSERVATION_REQUIRED")
    images = witness.get("images")
    expected = [sha(path) for path in image_paths]
    if (
        not isinstance(images, list)
        or len(images) != len(expected)
        or any(
            not isinstance(row, Mapping)
            or not same_sha256(row.get("image_sha256"), digest)
            for row, digest in zip(images, expected)
        )
    ):
        raise ValueError("COVER_CPA_IMAGE_HASH_MISMATCH")
    digest = witness.get("response_sha256")
    if not same_sha256(digest, digest):
        raise ValueError("COVER_CPA_RESPONSE_HASH_REQUIRED")
    question = witness.get("question")
    model = witness.get("model")
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(model, str)
        or not model.strip()
    ):
        raise ValueError("COVER_CPA_REQUEST_IDENTITY_REQUIRED")
    prompt_binding = json.dumps(
        # Hash the recorded representation, not a reformatted historical record.
        {"question": question, "image_sha256s": [row["image_sha256"] for row in images], "model": model}, sort_keys=True
    )
    if not same_sha256(text_sha(prompt_binding), witness.get("prompt_sha256")):
        raise ValueError("COVER_CPA_PROMPT_HASH_MISMATCH")
    if provider_response is None:
        raise ValueError("COVER_CPA_ORIGINAL_RESPONSE_REQUIRED")
    if not same_sha256(sha(provider_response), witness["response_sha256"]):
        raise ValueError("COVER_CPA_ORIGINAL_RESPONSE_HASH_DRIFT")
    payload = json.loads(provider_response.read_bytes())
    answer = payload.get("output_text")
    if not answer:
        answer = "".join(
            str(chunk["text"])
            for item in payload.get("output") or []
            if isinstance(item, Mapping) and item.get("type") == "message"
            for chunk in item.get("content") or []
            if isinstance(chunk, Mapping)
            and chunk.get("type") in {"output_text", "text"}
            and chunk.get("text")
        )
    if not isinstance(answer, str) or answer.strip() != witness.get("answer"):
        raise ValueError("COVER_CPA_ANSWER_NOT_ORIGINAL_RESPONSE")
    return parse_answer(str(witness.get("answer") or ""))


def validate_choice_bundle(
    generation: Mapping,
    *,
    root: Path | None = None,
    final_cover: Path | None = None,
    font_path: Path | None = None,
) -> list[str]:
    """Recompute scores and native pixels using portable hash-bound files."""
    try:
        binding = generation["candidate_selection"]
        if binding["schema_version"] != SCHEMA:
            raise ValueError("COVER_CHOICE_SCHEMA_INVALID")
        bundle = root or Path(binding["choose_path"]).parent
        choose = strict_file(bundle, "choose.json")
        if sha(choose) != binding["choose_sha256"]:
            raise ValueError("COVER_CHOOSE_HASH_MISMATCH")
        payload = json.loads(choose.read_text())
        if payload["schema_version"] != SCHEMA or payload["skill_commit"] != SKILL_COMMIT:
            raise ValueError("COVER_CHOICE_AUTHORITY_INVALID")
        if font_path is None and payload.get("execution_mode") != "LIVE_CPA":
            raise ValueError("COVER_OFFLINE_FIXTURE_NOT_PUBLISHABLE")
        if payload["candidate_id"] != binding["candidate_id"]:
            raise ValueError("COVER_CHOICE_CANDIDATE_MISMATCH")
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError("COVER_CHOICE_ARTIFACTS_MISSING")
        for relative, digest in artifacts.items():
            if sha(strict_file(bundle, relative)) != digest:
                raise ValueError("COVER_CHOICE_ARTIFACT_HASH_MISMATCH:" + relative)

        def bound(relative):
            if relative not in artifacts:
                raise ValueError("COVER_CHOICE_UNLISTED_ARTIFACT")
            return strict_file(bundle, relative)

        srt = bound("final.srt").read_text()
        if (
            text_sha(srt) != payload["final_srt_sha256"]
            or payload["final_srt_sha256"] != binding["final_srt_sha256"]
        ):
            raise ValueError("COVER_CHOICE_FINAL_SRT_DRIFT")
        from .title_cover_quotes import check_cover_quotes

        if payload["cover_quotes"] != check_cover_quotes(payload["cover_lines"], srt):
            raise ValueError("COVER_CHOICE_QUOTE_EVIDENCE_DRIFT")
        if any(row["status"] == "MISSING_QUOTE" for row in payload["cover_quotes"]):
            raise ValueError("COVER_CHOICE_QUOTE_NOT_IN_FINAL_SUBTITLES")
        review = json.loads(bound("semantic-review.json").read_text())
        if review.get("final_srt_sha256") != payload[
            "final_srt_sha256"
        ] or review != generation.get("art_direction", {}).get("cover_punch_semantic_review"):
            raise ValueError("COVER_CHOICE_SEMANTIC_SOURCE_DRIFT")
        from .cover_punch_semantics import SKILL_PUNCH_SHAPE, validate_cover_punch_semantic_review

        if not validate_cover_punch_semantic_review(
            review,
            rendered_lines=payload["cover_lines"],
            cover_text=generation["cover_text"],
            story_hook=str(generation.get("story_contract", {}).get("selection_hook") or ""),
            shape_contract=SKILL_PUNCH_SHAPE,
        ):
            raise ValueError("COVER_CHOICE_SEMANTIC_REVIEW_INVALID")
        sources = json.loads(bound("sources.json").read_text())
        for source in sources.values():
            if source.get("kind") not in {"stream_frame", "official_emote", "shabutton_emote"}:
                raise ValueError("COVER_UNKNOWN_SOURCE")
            if sha(bound(source["file"])) != source["sha256"]:
                raise ValueError("COVER_SOURCE_HASH_DRIFT")
            if source["kind"] == "shabutton_emote":
                from .title_cover_shabutton import validate_manifest

                manifest_path = bound("shabutton-source-manifest.json")
                if sha(manifest_path) != source["source_manifest_sha256"]:
                    raise ValueError("SHABUTTON_SOURCE_MANIFEST_DRIFT")
                catalog = validate_manifest(json.loads(manifest_path.read_text()))
                if not any(
                    entry["source_url"] == source["source_url"]
                    and entry["sha256"] == source["sha256"]
                    for entry in catalog
                ):
                    raise ValueError("SHABUTTON_SOURCE_PROVENANCE_DRIFT")
            if source["kind"] == "official_emote":
                from .cover_emote import load_emote_library

                entry = load_emote_library().get(source["official_id"])
                if entry is None or not same_sha256(entry.hd_sha256, source["sha256"]):
                    raise ValueError("COVER_OFFICIAL_EMOTE_IDENTITY_DRIFT")
                if sha(bound(source["mask_file"])) != source["mask_sha256"]:
                    raise ValueError("COVER_OFFICIAL_EMOTE_MASK_DRIFT")
        if sources["frame"]["sha256"] != generation["reference_sha256"]:
            raise ValueError("COVER_SOURCE_REFERENCE_DRIFT")
        if payload["source_frame_selection"] != generation.get("reference_selection"):
            raise ValueError("COVER_SOURCE_TIMING_DRIFT")
        rows, selected = choose_rows(
            payload["candidates"], participants=payload["participants"], recent=payload["recent"]
        )
        if (
            rows != payload["candidates"]
            or selected != payload["selected"]
            or selected != binding["selected_id"]
        ):
            raise ValueError("COVER_CHOICE_SCORE_OR_SELECTION_DRIFT")
        from .cover_private_font import resolve_private_cover_font
        from .cover_text_pixel_evidence import (
            verify_rendered_text_pixel_artifacts,
            verify_pre_overlay_route_background,
        )
        from .cover_route_evidence import validate_rendered_text_pixel_evidence

        if font_path is None:
            font_path, authority = resolve_private_cover_font()
            if authority != payload["font_authority"]:
                raise ValueError("COVER_CHOICE_FONT_AUTHORITY_DRIFT")
        for row in rows:
            if row.get("status") != "RENDERED":
                continue
            meta = row["render"]
            if (
                meta["rendered_lines"] != payload["cover_lines"]
                or row["punch_font_size"] != meta["punch_font_size"]
            ):
                raise ValueError("COVER_CHOICE_TEXT_OR_FONT_SIZE_DRIFT")
            pixels = meta["rendered_text_pixels"]
            if pixels["render_spec"]["lines"][-1]["font_size"] != row["punch_font_size"]:
                raise ValueError("COVER_CHOICE_FONT_SCORE_NOT_MEASURED")
            if any(key not in sources for key in row["source_ids"]):
                raise ValueError("COVER_UNKNOWN_SOURCE")
            if row["route"] in {"cutout", "ai_sticker", "ai_scene"}:
                image_result = json.loads(
                    bound("candidates/" + row["id"] + "/image-result.json").read_text()
                )
                if (
                    image_result != row.get("image_generation")
                    or image_result.get("status") != "AI_BACKGROUND_READY"
                    or image_result.get("selected_model")
                    not in image_result.get("attempted_models", [])
                ):
                    raise ValueError("COVER_IMAGE_GENERATION_LINEAGE_INVALID")
                bound("candidates/" + row["id"] + "/image-request.json")
                bound("candidates/" + row["id"] + "/image-response.json")
                if row["route"] == "ai_scene" and not str(row.get("scene_reason") or "").strip():
                    raise ValueError("COVER_AI_SCENE_STORY_REASON_REQUIRED")
            witness = json.loads(bound(row["witness_path"]).read_text())
            input_paths = [bound(p) for p in row["witness_images"]]
            if len(input_paths) != len(row["source_ids"]) + 1:
                raise ValueError("COVER_CPA_SOURCE_IMAGES_MISSING")
            import io

            for key, snapshot in zip(row["source_ids"], input_paths[1:]):
                with Image.open(bound(sources[key]["file"])) as source:
                    data = io.BytesIO()
                    source.convert("RGB").save(data, format="JPEG", quality=95, subsampling=0)
                    if not same_sha256(hashlib.sha256(data.getvalue()).hexdigest(), sha(snapshot)):
                        raise ValueError("COVER_CPA_SNAPSHOT_NOT_BOUND_SOURCE")
            provider_response = bound("candidates/" + row["id"] + "/score.provider-response.json")
            if (
                witness_verdict(witness, input_paths, provider_response=provider_response)
                != row["verdict"]
            ):
                raise ValueError("COVER_CHOICE_VERDICT_DRIFT")
            if not validate_rendered_text_pixel_evidence(meta):
                raise ValueError("COVER_CHOICE_PIXEL_METADATA_INVALID")
            final = bound(row["final_path"])
            pre = bound(row["pre_overlay_path"])
            mask = bound(row["mask_path"])
            background = bound(row["background_path"])
            import io

            with Image.open(final) as source:
                data = io.BytesIO()
                source.convert("RGB").save(data, format="JPEG", quality=95, subsampling=0)
                if not same_sha256(hashlib.sha256(data.getvalue()).hexdigest(), sha(input_paths[0])):
                    raise ValueError("COVER_CPA_SNAPSHOT_NOT_FINAL_PIXELS")
            if not verify_rendered_text_pixel_artifacts(
                pixels,
                final_cover_path=final,
                pre_overlay_path=pre,
                mask_path=mask,
                font_path=font_path,
                expected_pre_overlay_sha256=meta["pre_overlay_sha256"],
            ):
                raise ValueError("COVER_CHOICE_PIXEL_REPLAY_FAILED")
            if not verify_pre_overlay_route_background(
                route_background_path=background,
                pre_overlay_path=pre,
                expected_route_background_sha256=meta["ai_background_sha256"],
                text_backing=meta["text_backing"],
                scrim=meta["scrim"],
            ):
                raise ValueError("COVER_CHOICE_BACKGROUND_DRIFT")
            from .title_cover_composition import verify_candidate_composition

            verify_candidate_composition(
                row=row, sources=sources, lines=payload["cover_lines"], binding=binding,
                bundle=bundle, bound=bound,
            )
            if row["id"] == selected:
                if final_cover is not None and sha(final_cover) != sha(final):
                    raise ValueError("COVER_CHOICE_PACKAGE_FINAL_DRIFT")
                route = generation.get("route_decision") or {}
                if (
                    route.get("actual_treatment") != row["route"]
                    or route.get("selected_treatment") != row["route"]
                    or route.get("required_participant_ids") != payload["participants"]
                    or route.get("final_visible_participant_ids")
                    != row["verdict"]["people"]["present"]
                    or route.get("reason") != row["reason"]
                ):
                    raise ValueError("COVER_CHOICE_SELECTED_ROUTE_DRIFT")
                expected_model = (row.get("image_generation") or {}).get("selected_model", "none")
                if (
                    generation.get("model") != expected_model
                    or generation.get("image_generation_used")
                    != (row["route"] in {"cutout", "ai_sticker", "ai_scene"})
                    or generation.get("attempted_models")
                    != (row.get("image_generation") or {}).get("attempted_models", [])
                ):
                    raise ValueError("COVER_CHOICE_MODEL_LINEAGE_DRIFT")
                actual_pixels = dict(generation.get("rendered_text_pixels") or {})
                expected_pixels = dict(meta["rendered_text_pixels"])
                for field, relative in (
                    ("mask_path", row["mask_path"]),
                    ("pre_overlay_path", row["pre_overlay_path"]),
                ):
                    if actual_pixels.get(field) not in {
                        expected_pixels.get(field),
                        str(bundle / relative),
                        str(Path(binding["choose_path"]).parent / relative),
                    }:
                        raise ValueError("COVER_CHOICE_PIXEL_LOCATOR_ROLE_DRIFT")
                    actual_pixels.pop(field, None)
                    expected_pixels.pop(field, None)
                if actual_pixels != expected_pixels:
                    raise ValueError("COVER_CHOICE_SELECTED_RENDER_DRIFT:rendered_text_pixels")
                for key in (
                    "rendered_lines",
                    "final_cover_sha256",
                    "font_selection",
                    "pre_overlay_sha256",
                    "ai_background_sha256",
                ):
                    if generation.get(key) != meta.get(key):
                        raise ValueError("COVER_CHOICE_SELECTED_RENDER_DRIFT:" + key)
                if generation.get("final_host_identity_verification") != {
                    "schema_version": SCHEMA,
                    "verdict": row["verdict"],
                    "witness": witness,
                }:
                    raise ValueError("COVER_CHOICE_IDENTITY_RECEIPT_DRIFT")
        if (
            generation.get("method") != "skill.candidates"
            or generation.get("cover_origin") != "SKILL_CANDIDATE"
        ):
            raise ValueError("COVER_CHOICE_ROUTE_LABEL_INVALID")
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        return [str(exc)]
    return []


def validate_candidate_route(generation: Mapping) -> bool:
    try:
        route = generation["route_decision"]
        binding = generation["candidate_selection"]
        proof = generation["final_host_identity_verification"]
        return bool(
            route["schema_version"] == ROUTE_SCHEMA
            and route["selection_policy"] == SCHEMA
            and route["actual_treatment"] in ROUTES
            and route["selected_treatment"] == route["actual_treatment"]
            and route["execution_status"] == "READY"
            and route["reason"].strip()
            and binding["schema_version"] == SCHEMA
            and binding["selected_id"] == route["selected_candidate_id"]
            and proof["schema_version"] == SCHEMA
            and proof["witness"]["provider"] == "cpa"
            and proof["witness"]["status"] == "OBSERVED"
            and generation["method"] == "skill.candidates"
            and all(proof["verdict"][key]["pass"] is True for key in SAFE_FLAGS)
        )
    except (KeyError, TypeError, AttributeError):
        return False


def contact_sheet(root: Path, rows: list[dict], selected: str) -> Path:
    from PIL import ImageDraw

    sheet = Image.new("RGB", (960, 320 * ((len(rows) + 1) // 2)), "#20202a")
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(rows):
        x, y = (i % 2) * 480, (i // 2) * 320
        if row.get("status") == "RENDERED":
            with Image.open(strict_file(root, row["final_path"])) as src:
                sheet.paste(ImageOps.fit(src.convert("RGB"), (480, 270)), (x, y))
        label = f"{row['id']} {row['route']} {row['score'].get('total', '-')} {'SELECTED' if row['id'] == selected else row['score']['status']}"
        draw.text((x + 8, y + 280), label, fill="white")
    target = root / "candidates.jpg"
    sheet.save(target, quality=95)
    return target


def copy_choice_bundle(generation: Mapping, destination: Path) -> None:
    problems = validate_choice_bundle(generation)
    if problems:
        raise ValueError("; ".join(problems))
    source = Path(generation["candidate_selection"]["choose_path"]).parent
    if destination.exists():
        raise ValueError("COVER_CHOICE_DESTINATION_ALREADY_EXISTS")
    shutil.copytree(source, destination, symlinks=False)
    if validate_choice_bundle(generation, root=destination):
        raise ValueError("COVER_CHOICE_COPY_FAILED")
