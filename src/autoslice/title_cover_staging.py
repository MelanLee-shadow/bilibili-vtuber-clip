"""Normal non-song title/cover candidate producer; no audio/ASR calls."""

from __future__ import annotations

from src.autoslice.structured_output import call_json

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .title_cover_candidates import (
    SCHEMA,
    ROUTE_SCHEMA,
    ROUTES,
    SKILL_COMMIT,
    sha,
    text_sha,
    write_json,
    parse_answer,
    jpeg_snapshot,
    witness_verdict,
    choose_rows,
    contact_sheet,
    validate_choice_bundle,
)


from .title_cover_history import recent_choices, source_session_key


def pick_final_srt_frames(media_path: Path, final_srt: str, record: dict, out: Path) -> list[dict]:
    """Port pick_frames' final-SRT time + recorded intro offset, video only."""
    import subprocess
    from .title_cover_quotes import final_cues

    cues = final_cues(final_srt)
    if not cues:
        raise ValueError("COVER_COMPLETE_FINAL_SRT_REQUIRED")
    intro = ((record.get("burned_preview") or {}).get("branding_intro") or {}).get(
        "intro_offset_ms", 0
    )
    if type(intro) is not int or intro < 0:
        raise ValueError("COVER_INTRO_OFFSET_INVALID")
    # Subtitles choose a bounded, temporally spread shortlist. Actual CPA images
    # own facial/story suitability; no audio envelope or ASR is read here.
    order = sorted(
        range(len(cues)),
        key=lambda i: (bool(re.search(r"[！？!?]|哈哈|呜|笑", cues[i]["text"])), -i),
        reverse=True,
    )
    picked = []
    for index in order:
        cue = cues[index]
        values = re.fullmatch(r"(\d+):(\d+):(\d+)[,.](\d+)", cue["time"])
        if values is None:
            raise ValueError("COVER_SUBTITLE_TIME_INVALID")
        h, m, sec, ms = map(int, values.groups())
        at = h * 3600000 + m * 60000 + sec * 1000 + ms + intro + 120
        if any(abs(at - row["frame_ms"]) < 1000 for row in picked):
            continue
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"subtitle-frame-{len(picked)}.png"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-ss",
                    f"{at / 1000:.3f}",
                    "-i",
                    str(media_path),
                    "-an",
                    "-frames:v",
                    "1",
                    "-y",
                    str(target),
                ],
                capture_output=True,
                check=True,
                timeout=90,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                "COVER_SUBTITLE_FRAME_EXTRACTION_FAILED:" + type(exc).__name__
            ) from exc
        if not target.is_file():
            raise ValueError("COVER_SUBTITLE_FRAME_UNAVAILABLE")
        picked.append(
            {
                "path": target,
                "frame_ms": at,
                "subtitle_cue": cue["cue"],
                "intro_offset_ms": intro,
                "sha256": sha(target),
                "audio_used": False,
            }
        )
        if len(picked) == 3:
            break
    return picked


def _assets(root: Path, reference: Path, library, repo_root: Path, extra_frames=()) -> dict:
    from .cover_emote import resolve_emote_reference, emote_media_roots

    source_dir = root / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / ("frame" + reference.suffix.lower())
    shutil.copyfile(reference, target)
    assets = {
        "frame": {
            "path": target,
            "kind": "stream_frame",
            "sha256": sha(target),
            "participant": "李豆沙",
        }
    }
    for index, frame in enumerate(extra_frames):
        destination = source_dir / f"frame-{index + 1}.png"
        shutil.copyfile(frame["path"], destination)
        assets[f"frame:{index + 1}"] = {
            **{key: value for key, value in frame.items() if key != "path"},
            "path": destination,
            "kind": "stream_frame",
            "participant": "UNKNOWN",
        }
    if library:
        for entry in library.entries:
            hd, _ = resolve_emote_reference(
                entry, repo_root=repo_root, runtime_roots=library.runtime_roots
            )
            if hd is None:
                continue
            small = next(
                (
                    p / entry.file
                    for p in emote_media_roots(repo_root, runtime_roots=library.runtime_roots)
                    if (p / entry.file).is_file()
                ),
                None,
            )
            if small is None or small.is_symlink() or hd.is_symlink():
                continue
            with Image.open(small) as image:
                if image.mode != "RGBA" or image.getchannel("A").getextrema()[0] == 255:
                    continue
            target = source_dir / f"official-{entry.id}.png"
            mask = source_dir / f"official-{entry.id}.mask.png"
            shutil.copyfile(hd, target)
            shutil.copyfile(small, mask)
            assets["emote:" + entry.id] = {
                "path": target,
                "mask": mask,
                "sha256": sha(target),
                "mask_sha256": sha(mask),
                "kind": "official_emote",
                "official_id": entry.id,
                "label": entry.label,
                "participant": "kmx" if entry.id == "25" else "李豆沙",
                "description": entry.description,
                "baked_text": entry.baked_text,
            }
    from .title_cover_shabutton import stage_assets

    assets.update(stage_assets(root))
    return assets


def _catalog(assets: dict) -> dict:
    return {
        key: {
            **{k: v for k, v in row.items() if k not in {"path", "mask"}},
            "file": "sources/" + row["path"].name,
            **({"mask_file": "sources/" + row["mask"].name} if row.get("mask") else {}),
        }
        for key, row in assets.items()
    }


def _plan_prompt(*, title: str, final_srt: str, catalog: dict, recent: dict) -> str:
    return (
        "执行已批准的 lidousha-title-cover skill。只以完整最终SRT作事实来源，不听音频，"
        "不得用旧标题、选片摘要或人物设定补事实。投稿标题是待核对的编辑结果。\n"
        "先定1–2行封面字，每行1–9个字符，第二行梗字更大；允许名字：原话，"
        "不写逗号长句。与标题重复或补充均可。封面字在全部候选间固定。\n"
        "列2–4个实际可做的候选，先考虑情绪贴合的官方表情包与截图；"
        "可选 screenshot/screenshot_collage/emote/cutout/ai_sticker/ai_scene。"
        "ai_scene仅故事确实需要场景时才选，说明来源；不要先默认重绘。"
        "截图拼贴必须有两份不同真实帧；素材目录没有的来源不能虚构。"
        "所有故事参与者列齐并给最终字幕引语；只允许已有素材的身份外观，"
        "kmx是粉丝，官方25号可代表kmx。未出现人物不可补画。"
        "主表情包同源场次及近5张不重复；缺素材路线列入 rejected_routes 写原因。"
        "不要为了评分改文字、制造双人或增大超过200px继续刷分。\n"
        + json.dumps(
            {"title_to_check": title, "final_srt": final_srt, "assets": catalog, "recent": recent},
            ensure_ascii=False,
        )
        + '\n输出JSON：{"participants":[{"name":"李豆沙","quote":"最终字幕原话"}],'
        '"cover_punch":{"main":"情境","sub":"梗字"},'
        '"candidates":[{"id":"ascii-id","route":"emote","source_ids":["emote:08"],'
        '"main_emote":"emote:08","reason":"贴合哪句话","scene_reason":""}],'
        '"rejected_routes":[{"route":"ai_scene","reason":"不需要场景"}]}'
    )


def _validate_plan(
    plan: dict, final_srt: str, assets: dict, recent: dict
) -> tuple[list[str], list[str]]:
    from .title_cover_quotes import find_quote
    from .cover_punch_semantics import _validated_punch_lines

    people = plan.get("participants")
    if not isinstance(people, list) or not people:
        raise ValueError("COVER_STORY_PARTICIPANTS_REQUIRED")
    names = []
    for person in people:
        if not isinstance(person, dict) or not str(person.get("name") or "").strip():
            raise ValueError("COVER_STORY_PARTICIPANT_INVALID")
        name = person["name"]
        quote = str(person.get("quote") or "")
        if name != "李豆沙" and (not find_quote(quote, final_srt) or name not in quote):
            raise ValueError("COVER_PARTICIPANT_NOT_IN_FINAL_SUBTITLES")
        names.append(name)
    if "李豆沙" not in names or len(set(names)) != len(names):
        raise ValueError("COVER_STORY_PARTICIPANT_INVALID")
    lines = _validated_punch_lines(plan.get("cover_punch"), "")
    if not lines:
        raise ValueError("COVER_SKILL_LINES_INVALID")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 4:
        raise ValueError("COVER_REQUIRES_TWO_TO_FOUR_CANDIDATES")
    ids = set()
    for row in candidates:
        if (
            not isinstance(row, dict)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", str(row.get("id") or ""))
            or row["id"] in ids
        ):
            raise ValueError("COVER_CANDIDATE_ID_INVALID")
        ids.add(row["id"])
        route = row.get("route")
        source_ids = row.get("source_ids")
        if route not in ROUTES or not str(row.get("reason") or "").strip():
            raise ValueError("COVER_CANDIDATE_PLAN_INVALID")
        if (
            not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= 4
            or len(set(source_ids)) != len(source_ids)
            or any(key not in assets for key in source_ids)
        ):
            raise ValueError("COVER_CANDIDATE_SOURCE_UNAVAILABLE")
        if route == "screenshot_collage" and (
            len(source_ids) < 2 or len({assets[key]["sha256"] for key in source_ids}) < 2
        ):
            raise ValueError("COVER_COLLAGE_NEEDS_DISTINCT_SOURCE_FRAMES")
        if route in {"screenshot", "screenshot_collage"} and any(
            assets[key]["kind"] != "stream_frame" for key in source_ids
        ):
            raise ValueError("COVER_SCREENSHOT_SOURCE_INVALID")
        if route == "emote" and not any(
            assets[key]["kind"] in {"official_emote", "shabutton_emote"} for key in source_ids
        ):
            raise ValueError("COVER_EMOTE_SOURCE_REQUIRED")
        if route == "ai_scene" and not str(row.get("scene_reason") or "").strip():
            raise ValueError("COVER_AI_SCENE_STORY_REASON_REQUIRED")
        emote = row.get("main_emote")
        if route == "emote" and (
            emote not in source_ids
            or assets[emote]["kind"] not in {"official_emote", "shabutton_emote"}
        ):
            raise ValueError("COVER_MAIN_EMOTE_REQUIRED")
        if emote and (
            emote in recent["last_five_main_emotes"]
            or emote in recent["source_session_main_emotes"]
        ):
            raise ValueError("MAIN_EMOTE_REPEATED")
    return names, list(lines)


def _probe(
    root: Path,
    directory: Path,
    name: str,
    paths: list[Path],
    question: str,
    *,
    probe,
    base_url: str,
    api_key: str,
) -> tuple[dict, dict, list[str]]:
    snapshots = []
    for index, path in enumerate(paths):
        target = directory / f"{name}.input-{index}.jpg"
        jpeg_snapshot(path, target)
        snapshots.append(target)
    write_json(
        directory / f"{name}.request.json",
        {
            "question": question,
            "images": [{"path": str(p.relative_to(root)), "sha256": sha(p)} for p in snapshots],
        },
    )
    raw_response = directory / f"{name}.provider-response.json"
    witness = probe(
        snapshots, question, api_base=base_url, api_key=api_key, raw_response_path=raw_response
    )
    write_json(directory / f"{name}.response.json", witness)
    verdict = witness_verdict(witness, snapshots, provider_response=raw_response)
    return verdict, witness, [str(path.relative_to(root)) for path in snapshots]


def _render_spec(
    row: dict, geometry: dict, assets: dict, lines: list[str], generated: Path | None
) -> dict:
    """Translate whitelisted source IDs; never execute model supplied file paths."""
    spec = {
        "face": geometry["face"],
        "keep": geometry.get("keep", []),
        "layout": geometry.get("layout", "auto"),
        "lines": [
            {"text": text, "color": "blue" if i == 0 and len(lines) == 2 else "pink"}
            for i, text in enumerate(lines)
        ],
    }
    route = row["route"]
    if generated is not None:
        spec["background_image"] = str(generated)
        return spec
    source_ids = row["source_ids"]
    if route == "screenshot":
        source = assets[source_ids[0]]
        spec.update(frame=str(source["path"]), crop=geometry["crop"])
        if geometry.get("inset"):
            spec["inset"] = geometry["inset"]
        return spec
    stickers = geometry.get("stickers")
    if not isinstance(stickers, list) or not stickers:
        raise ValueError("COVER_COMPOSITION_STICKERS_REQUIRED")
    selected = []
    for sticker in stickers:
        if not isinstance(sticker, dict) or set(sticker) - {"source_id", "x", "y", "h", "crop", "cutout"}:
            raise ValueError("COVER_COMPOSITION_STICKER_FIELDS_INVALID")
        key = sticker.get("source_id")
        if key not in source_ids or key not in assets:
            raise ValueError("COVER_COMPOSITION_UNKNOWN_SOURCE")
        source = assets[key]
        from .title_cover_composition import sticker_cutout

        record = {
            "src": str(source["path"]),
            "x": sticker["x"],
            "y": sticker["y"],
            "h": sticker["h"],
            "cutout": sticker_cutout(source, sticker.get("cutout")),
        }
        if sticker.get("crop"):
            crop = sticker["crop"]
            with Image.open(source["path"]) as im:
                if not isinstance(crop, list) or len(crop) != 4 or any(type(v) is not int for v in crop) or not (
                    0 <= crop[0] < crop[2] <= im.width and 0 <= crop[1] < crop[3] <= im.height
                ):
                    raise ValueError("COVER_SOURCE_CROP_INVALID")
            record["crop"] = crop
        if (
            any(type(record[k]) is not int for k in ("x", "y", "h"))
            or not 0 <= record["x"] < 1920
            or not 0 <= record["y"] < 1080
            or not 100 <= record["h"] <= 1080
        ):
            raise ValueError("COVER_STICKER_GEOMETRY_INVALID")
        selected.append(record)
    if {sticker["source_id"] for sticker in stickers} != set(source_ids):
        raise ValueError("COVER_COMPOSITION_DROPPED_SOURCE")
    spec["stickers"] = selected
    return spec


def stage_title_cover_candidates(
    *,
    record: dict,
    candidate_id: str,
    title: str,
    cover_text: str,
    final_srt: str,
    reference_path: Path,
    artifact_root: Path,
    generation: dict,
    llm_call,
    image_edit,
    emote_library,
    repo_root: Path,
    history_root: Path,
    base_url: str,
    api_key: str,
    image_probe=None,
    font_resolver=None,
    extra_frames=(),
) -> dict:
    from .cover_private_font import resolve_private_cover_font
    from .cpa_frame_witness import batch_jpeg_vision_probe
    from .cover_style import materialize_cover, background
    from .cover_punch_semantics import review_cover_punch_semantics
    from .title_cover_quotes import final_cues, check_cover_quotes

    if not final_cues(final_srt):
        raise ValueError("COVER_COMPLETE_FINAL_SRT_REQUIRED")
    if llm_call is None:
        raise ValueError("COVER_CANDIDATE_PLANNER_REQUIRED")
    font_path, font_authority = (font_resolver or resolve_private_cover_font)()
    root = artifact_root / "title_cover" / candidate_id
    root.mkdir(parents=True, exist_ok=False)
    (root / "final.srt").write_text(final_srt)
    session = source_session_key(record)
    recent = recent_choices(history_root, session, candidate_id)
    write_json(root / "recent.json", recent)
    assets = _assets(root, reference_path, emote_library, repo_root, extra_frames)
    catalog = _catalog(assets)
    write_json(root / "sources.json", catalog)
    prompt = _plan_prompt(title=title, final_srt=final_srt, catalog=catalog, recent=recent)
    write_json(root / "plan-request.json", {"prompt": prompt, "sha256": text_sha(prompt)})
    raw = call_json(llm_call, prompt, reuse=False)
    write_json(root / "plan-response.json", {"answer": raw, "sha256": text_sha(raw)})
    plan = parse_answer(raw)
    names, lines = _validate_plan(plan, final_srt, assets, recent)
    write_json(root / "plan.json", plan)
    text_calls = []

    def reviewed_call(prompt):
        answer = llm_call(prompt)
        text_calls.append(
            {
                "prompt": prompt,
                "answer": answer,
                "request_sha256": text_sha(prompt),
                "response_sha256": text_sha(answer),
            }
        )
        write_json(root / "punch-calls.json", text_calls)
        return answer

    from .completed_review_checkpoint import inherit_ruling_context
    inherit_ruling_context(reviewed_call, llm_call)
    lines, review = review_cover_punch_semantics(
        title=title,
        cover_text=cover_text,
        story_hook=str((record.get("story_contract") or {}).get("selection_hook") or ""),
        punch=tuple(lines),
        llm_call=reviewed_call,
        final_srt_text=final_srt,
    )
    if not lines:
        raise ValueError("CPA_PUNCH_SEMANTIC_REVIEW_REQUIRED")
    lines = list(lines)
    quotes = check_cover_quotes(lines, final_srt)
    if any(row["status"] == "MISSING_QUOTE" for row in quotes):
        raise ValueError("COVER_QUOTE_NOT_IN_FINAL_SUBTITLES")
    write_json(root / "semantic-review.json", review)
    probe = image_probe or batch_jpeg_vision_probe
    rows = []
    for candidate in plan["candidates"]:
        row = {**candidate, "status": "FAILED"}
        directory = root / "candidates" / row["id"]
        directory.mkdir(parents=True)
        paths = [assets[key]["path"] for key in row["source_ids"]]
        try:
            generated = None
            if row["route"] in {"cutout", "ai_sticker", "ai_scene"}:
                # Multiple people retain independent real references in one labelled montage.
                reference = paths[0]
                if len(paths) > 1:
                    montage = Image.new("RGB", (960 * len(paths), 1080), "white")
                    for i, path in enumerate(paths):
                        with Image.open(path) as im:
                            montage.paste(
                                ImageOps.contain(im.convert("RGB"), (960, 1080)), (i * 960, 0)
                            )
                    reference = directory / "identity-sources.png"
                    montage.save(reference)
                generated = directory / "generated-background.png"
                image_prompt = (
                    "Create only a text-free 1920x1080 cover background. Preserve every referenced person's identity, visible outfit, accessories and face. "
                    "Never invent people, body details, actions or facts. No typography, captions or logo. "
                    + {
                        "cutout": "Faithfully isolate the real source subject and place on a clean background. No redraw of identity or new scene.",
                        "ai_sticker": "Express the sourced reaction as a clean sticker-style composition with the same identities.",
                        "ai_scene": "Build only the scene justified by the final subtitle story.",
                    }[row["route"]]
                    + "\n"
                    + json.dumps(
                        {
                            "candidate": row,
                            "final_srt": final_srt,
                            "locked_cover_lines": lines,
                            "participants": names,
                        },
                        ensure_ascii=False,
                    )
                )
                row["image_call_started"] = True
                result = image_edit(
                    base_url=base_url,
                    api_key=api_key,
                    reference_path=reference,
                    output_path=generated,
                    prompt=image_prompt,
                    request_path=directory / "image-request.json",
                    response_path=directory / "image-response.json",
                )
                write_json(directory / "image-result.json", result)
                if result.get("status") != "AI_BACKGROUND_READY" or not generated.is_file():
                    raise ValueError("COVER_IMAGE_GENERATION_FAILED")
                row["image_generation"] = result
            question = (
                "你是封面排版见证。按实际输入图规划1920x1080成品，不编造人物和道具，不改变服装。"
                "横向文字安全区260–1660，主脸至少高270像素，整脸及所有参与者脸/身份细节必须保留且不被字遮挡。"
                "若截图是游戏，保留关键游戏场景，可用真实同源脸小窗放大。"
                "首行情境、第二行梗字更大，每行字完全固定。只给实际构图坐标。"
                "输出JSON含face=[x0,y0,x1,y1]（成品主脸）、keep=[其他脸及必要身份细节bbox]、layout=auto|left|right|top|diagonal、reason。"
                "截图另给crop=[源图x,y,宽]，裁切高=宽*1080/1920，可选inset={src:[x,y,w,h],dst:[x,y,w]}。"
                "拼贴/表情包给stickers=[{source_id,x,y,h,crop:可选源图bbox,cutout:auto|none|white}]，禁止文件路径。"
                "完整放入所列素材；官方小图alpha优先作HD蒙版，真实透明素材保留alpha。"
                "仅对已核沙按钮的实际不透明白底显式选white，用既有边界泛洪去白，勿破坏主体白色细节；其他不透明素材用none。自带字有冲突时裁掉。"
                "已有生成背景则只给成品face/keep，不再改图。\n"
                + json.dumps(
                    {
                        "candidate": row,
                        "locked_lines": lines,
                        "sources": catalog,
                        "generated_background": generated is not None,
                    },
                    ensure_ascii=False,
                )
            )
            geometry, _, _ = _probe(
                root,
                directory,
                "geometry",
                [generated] if generated else paths,
                question,
                probe=probe,
                base_url=base_url,
                api_key=api_key,
            )
            if not str(geometry.get("reason") or "").strip():
                raise ValueError("COVER_GEOMETRY_REASON_REQUIRED")
            spec = _render_spec(row, geometry, assets, lines, generated)
            write_json(directory / "render-spec.json", spec)
            # Keep exact deterministic source composition for source/pixel replay.
            background(spec).convert("RGB").save(directory / "composition.png")
            final_path = directory / (candidate_id + "." + row["id"] + ".cover.png")
            meta = materialize_cover(
                spec, final_path, font_path=font_path, font_authority=font_authority
            )
            meta.update(final_cover=str(final_path), final_cover_sha256=sha(final_path))
            question = (
                "你是CPA最终封面图像评分者。第一张是实际成品，后续是素材来源，仅以最终SRT作事实来源。"
                "不要采信排版坐标或候选自述；实际核对人物到齐、梗贴合、拼贴边缘干净、完整脸、身份服装细节和画面事件真实性。"
                "官方表情包是合法身份来源，25号代表粉丝kmx，不能冒充人形李豆沙。不能补造另一人的长相。"
                "主脸必须至少高270/1080；文字含描边阴影须在260–1660并且不遮脸/必要细节。"
                "列全故事参与者expected和实际可见present，逐一说明来源；无来源的人出现即no_fabrication=false。"
                "每项必须写具体图像理由，不能只写true。输出JSON："
                '{"people":{"expected":["李豆沙"],"present":["李豆沙"],"reason":"..."},'
                '"hook_fit":{"score":0或1或2,"reason":"..."},'
                '"clean_layers":{"pass":true或false,"reason":"..."},'
                '"identity":{"pass":true或false,"reason":"..."},'
                '"source_faithful":{"pass":true或false,"reason":"..."},'
                '"no_fabrication":{"pass":true或false,"reason":"..."},'
                '"face":[x0,y0,x1,y1],"keep":[其他脸和必要身份特征bbox]}'
                + "\n"
                + json.dumps(
                    {
                        "final_srt": final_srt,
                        "participants_to_verify": names,
                        "cover_lines": lines,
                        "route": row["route"],
                        "source_ids": row["source_ids"],
                        "sources": catalog,
                    },
                    ensure_ascii=False,
                )
            )
            verdict, witness, snapshots = _probe(
                root,
                directory,
                "score",
                [final_path, *paths],
                question,
                probe=probe,
                base_url=base_url,
                api_key=api_key,
            )
            if verdict.get("people", {}).get("expected") != names:
                raise ValueError("COVER_STORY_PARTICIPANTS_DISAGREE")
            row.update(
                status="RENDERED",
                verdict=verdict,
                render=meta,
                punch_font_size=meta["punch_font_size"],
                final_path=str(final_path.relative_to(root)),
                pre_overlay_path=str(Path(meta["pre_overlay_path"]).relative_to(root)),
                background_path=str(Path(meta["ai_background"]).relative_to(root)),
                mask_path=str(Path(meta["rendered_text_pixels"]["mask_path"]).relative_to(root)),
                witness_path=str((directory / "score.response.json").relative_to(root)),
                witness_images=snapshots,
            )
        except (ValueError, KeyError, TypeError, OSError, SystemExit) as exc:
            row.update(status="FAILED", failure=str(exc))
            write_json(directory / "failure.json", {"candidate": row["id"], "reason": str(exc)})
        rows.append(row)
    try:
        rows, selected = choose_rows(rows, participants=names, recent=recent)
    except ValueError:
        write_json(
            root / "choose.json",
            {
                "schema_version": SCHEMA,
                "status": "BLOCKED",
                "candidate_id": candidate_id,
                "candidates": rows,
            },
        )
        raise
    if recent != recent_choices(history_root, session, candidate_id):
        raise ValueError("COVER_HISTORY_CHANGED_DURING_GENERATION")
    contact_sheet(root, rows, selected)
    winner = next(row for row in rows if row["id"] == selected)
    payload = {
        "schema_version": SCHEMA,
        "skill_commit": SKILL_COMMIT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": candidate_id,
        "source_session": session,
        "final_srt_sha256": text_sha(final_srt),
        "source_frame_selection": generation.get("reference_selection"),
        "execution_mode": "OFFLINE_TEST"
        if font_resolver is not None or image_probe is not None
        else "LIVE_CPA",
        "participants": names,
        "cover_lines": lines,
        "cover_quotes": quotes,
        "recent": recent,
        "font_authority": font_authority,
        "candidates": rows,
        "selected": selected,
        "score_formula": "3P+2T+H+C+V; T saturates at 200px; ties C then stable order",
        "artifacts": {
            str(path.relative_to(root)): sha(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "choose.json"
        },
    }
    write_json(root / "choose.json", payload)
    witness = json.loads((root / winner["witness_path"]).read_text())
    result = {
        **generation,
        **winner["render"],
        "method": "skill.candidates",
        "cover_origin": "SKILL_CANDIDATE",
        "reference_image": str(root / "sources" / ("frame" + reference_path.suffix.lower())),
        "reference_sha256": sha(reference_path),
        "request_path": str(root / "candidates" / selected / "score.request.json"),
        "response_path": str(root / winner["witness_path"]),
        "art_direction": {
            "is_song": False,
            "cover_punch": lines,
            "cover_punch_semantic_review": review,
        },
        "candidate_selection": {
            "schema_version": SCHEMA,
            "bundle_root": str(root),
            "candidate_id": candidate_id,
            "choose_path": str(root / "choose.json"),
            "choose_sha256": sha(root / "choose.json"),
            "selected_id": selected,
            "final_srt_sha256": text_sha(final_srt),
        },
        "final_host_identity_verification": {
            "schema_version": SCHEMA,
            "verdict": winner["verdict"],
            "witness": witness,
        },
        "route_decision": {
            "schema_version": ROUTE_SCHEMA,
            "selection_policy": SCHEMA,
            "execution_status": "READY",
            "selected_treatment": winner["route"],
            "actual_treatment": winner["route"],
            "selected_candidate_id": selected,
            "required_participant_ids": names,
            "final_visible_participant_ids": winner["verdict"]["people"]["present"],
            "reason": winner["reason"],
            "host_identity_required": True,
        },
        "image_generation_planned": any(
            r["route"] in {"cutout", "ai_sticker", "ai_scene"} for r in rows
        ),
        "image_generation_attempted": any(r.get("image_call_started") is True for r in rows),
        "image_generation_used": winner["route"] in {"cutout", "ai_sticker", "ai_scene"},
        "model": (winner.get("image_generation") or {}).get("selected_model", "none"),
        "attempted_models": (winner.get("image_generation") or {}).get("attempted_models", []),
        "model_fallback_used": (winner.get("image_generation") or {}).get(
            "model_fallback_used", False
        ),
    }
    problems = validate_choice_bundle(result, font_path=font_path)
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "status": "AI_COVER_READY",
        "cover_path": result["final_cover"],
        "cover_generation": result,
        "blocked_reason_codes": [],
    }
