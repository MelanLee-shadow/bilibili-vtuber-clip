"""Create joint QC via CPA, or explicitly revalidate an existing unchanged PASS.

Resolve the deployment host with src/autoslice/deployed_code_root.py, cd to its
returned repository, then run:
  python3 scripts/run_title_cover_joint_qc.py <package_root> <title> <out_path>

收编自 recording-host:/tmp/run_title_cover_joint_qc.py(受骗片会话手作工具):
身份行按 assets/lidousha/persona.md 修正——墨镜是可选配饰不是身份特征
(v1 把"墨镜"写成必备,lidousha_primary 系统性假阴性,213135 案 1P2F)。
当前人物问卷以当场直播形象为准，不将某套造型的发色、耳朵或帽子写成跨场身份条件。
此问卷修正不补造同场参考，也不替代独立 SOURCE/FINAL 身份门或重标旧 FAIL。
The verdict is the exact parsed CPA answer (validator replays this bond);
any gate the model fails leaves status=FAIL and the upload chain stops.
"""
import argparse
import copy
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, ".")
from src.autoslice.cpa_frame_witness import image_vision_probe
from src.autoslice.cover_generation import ordinary_talk_redraw_style_binding
from src.autoslice.publication_identity import publication_identity


def resolve_candidate_id(record: dict, review: dict) -> str:
    """Preserve ordinary precedence; narrowly admit the exact C2 legacy shape."""
    identity = publication_identity(record)
    candidates = identity.authority_ids
    if candidates:
        if len(set(candidates)) != 1:
            raise ValueError("candidate authorities conflict")
        return next(iter(candidates))
    if record.get("schema_version") != "lidousha-c2-release-record.v1":
        raise ValueError("candidate id is unavailable")
    root_id, items = record.get("candidate_id"), review.get("items")
    if not isinstance(root_id, str) or not root_id or not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("candidate_id") != root_id:
        raise ValueError("C2 legacy record candidate is absent or conflicts with review item")
    return root_id


def _final_srt_context(record: Mapping[str, object]) -> dict[str, object] | None:
    """Read the already-bound final subtitle as semantic QC context.

    The subtitle remains source evidence owned by the package; this helper does
    not authorize or rewrite it.  A declared but unreadable path is retained as
    an explicit unavailable witness so the CPA cannot silently treat missing
    final text as proof.
    """

    subtitle_path = record.get("subtitle_path")
    if not isinstance(subtitle_path, str) or not subtitle_path.strip():
        return None
    path = Path(subtitle_path)
    try:
        raw = path.read_bytes()
    except OSError:
        return {
            "status": "UNAVAILABLE",
            "source": "record.subtitle_path",
            "reason_code": "FINAL_SRT_UNAVAILABLE",
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "status": "UNAVAILABLE",
            "source": "record.subtitle_path",
            "reason_code": "FINAL_SRT_NOT_UTF8",
        }
    if not text.strip():
        return {
            "status": "UNAVAILABLE",
            "source": "record.subtitle_path",
            "reason_code": "FINAL_SRT_EMPTY",
        }
    return {
        "status": "AVAILABLE",
        "source": "record.subtitle_path",
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "text": text,
    }


def package_story_context(*, record: dict, generation: dict,
                          candidate_id: str, frozen_published_replay: bool = False) -> dict | None:
    """Carry the current source-backed event into the pixel review."""
    from src.autoslice.story_contract import cover_story_contract_binding_matches

    story = record.get("story_contract")
    if not isinstance(story, dict) or not story.get("selection_hook"):
        return None  # Legacy records and song-only callers have no event contract.
    binding = generation.get("story_contract")
    if frozen_published_replay and isinstance(binding, dict) and isinstance(story, dict):
        old_review, current_review = binding.get("source_fact_review"), story.get("source_fact_review")
        if isinstance(old_review, dict) and isinstance(current_review, dict):
            title_review_fields = {"final_title", "original_selection_hook", "original_title",
                                   "passes", "receipt_sha256"}
            if all(old_review.get(key) == current_review.get(key)
                   and (key in old_review) == (key in current_review)
                   for key in (set(old_review) | set(current_review)) - title_review_fields):
                # Frozen title-review metadata is not a cover fact. Every other
                # field still goes through the ordinary projection matcher.
                binding = {key: value for key, value in binding.items() if key != "source_fact_review"}
    if (story.get("candidate_id") != candidate_id
            or not isinstance(binding, dict)
            or "selection_hook" not in binding
            or not cover_story_contract_binding_matches(story, binding)):
        raise ValueError("package story differs from current cover authority")
    hook = story["selection_hook"]
    if not isinstance(hook, str) or not hook.strip():
        raise ValueError("package selection hook is invalid")
    hook_sha = "sha256:" + hashlib.sha256(hook.encode("utf-8")).hexdigest()
    if story.get("selection_hook_sha256") != hook_sha:
        raise ValueError("package selection hook hash drifts")
    context = copy.deepcopy({
        "candidate_id": candidate_id,
        "selection_hook": hook,
        "selection_hook_sha256": hook_sha,
        "source_media_sha256s": story.get("source_media_sha256s"),
        "clip_context_binding": story.get("clip_context_binding"),
        "participants": story.get("participants"),
        "relation_state": story.get("relation_state"),
        "cover_fallback_mode": story.get("cover_fallback_mode"),
    })
    direction = generation.get("art_direction")
    style_binding = generation.get("art_direction_style")
    research_aware = (
        isinstance(direction, Mapping)
        and "research_application" in direction
    ) or (
        isinstance(style_binding, Mapping)
        and "research_projection" in style_binding
    )
    if research_aware:
        final_srt = _final_srt_context(record)
        if final_srt is not None:
            context["final_srt"] = final_srt
    context["context_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(context, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return context


def package_identity_context(*, root: Path, item: dict, generation: dict,
                             cover_path: Path, candidate_id: str,
                             frozen_published_replay: bool = False) -> dict | None:
    """Reuse the canonical bound source/final proof as context, not a verdict."""
    from src.autoslice.cover_host_identity_gate import (
        HOST_ONLY_SCHEMA_VERSION, validate_final_host_identity_verification,
    )
    from src.autoslice.host_only_v4_package_binding import (
        BINDING_ITEM_KEY, SONG_BINDING_ITEM_KEY, FROZEN_HOST_ONLY_V4_SCHEMA,
        package_binding_contract, validate_package_binding,
    )

    if generation.get("method") == "skill.candidates":
        from src.autoslice.title_cover_package import package_choice_problems
        problems = package_choice_problems(root, item, generation)
        if problems:
            raise ValueError("candidate cover source/identity evidence invalid: " + "; ".join(problems))
        if (item.get("candidate_id") or item.get("id")) != candidate_id:
            raise ValueError("candidate cover identity differs from current candidate")
        if (root / item["cover"]).resolve() != cover_path.resolve():
            raise ValueError("candidate identity context points at another final cover")
        proof = generation["final_host_identity_verification"]
        return copy.deepcopy({
            "candidate_id": candidate_id,
            "package_binding": {"final_cover_path": item["cover"], "final_cover_sha256": generation["final_cover_sha256"],
                                "candidate_choice_path": item["cover_choice"], "candidate_choice_sha256": item["cover_choice_sha256"]},
            "source_final_identity_verdict": proof["verdict"],
            "source_final_identity_witness": {key: proof["witness"].get(key) for key in ("provider", "model", "status", "answer", "response_sha256", "images")},
        })
    proof = generation.get("final_host_identity_verification")
    contract = package_binding_contract(generation)
    binding = item.get(contract[0]) if contract else item.get(BINDING_ITEM_KEY) or item.get(SONG_BINDING_ITEM_KEY)
    is_v4 = isinstance(proof, dict) and proof.get("schema_version") == HOST_ONLY_SCHEMA_VERSION
    if binding is None and not is_v4 and contract is None:
        return None
    legacy_v4 = bool(frozen_published_replay and isinstance(proof, dict)
                     and proof.get("schema_version") == FROZEN_HOST_ONLY_V4_SCHEMA)
    validate_package_binding(root=root, item=item, generation=generation,
                             frozen_published_v4=legacy_v4)
    if not legacy_v4 and not validate_final_host_identity_verification(generation):
        raise ValueError("package source/final identity verdict is invalid")
    if (item.get("candidate_id") or item.get("id")) != candidate_id:
        raise ValueError("package identity candidate differs from current candidate")
    if cover_path != (root / binding["final_cover_path"]).resolve(strict=True):
        raise ValueError("package identity final cover differs from current cover")
    # The package validator accepts both bare and prefixed SHA-256 digests.
    # Canonicalize the already-validated context before the later comparison.
    binding = copy.deepcopy(binding)
    binding["final_cover_sha256"] = "sha256:" + binding["final_cover_sha256"].removeprefix("sha256:")
    witness = proof["witness"]
    return copy.deepcopy({
        "candidate_id": candidate_id,
        "package_binding": binding,
        "source_final_identity_verdict": proof["verdict"],
        "source_final_identity_witness": {
            key: witness.get(key)
            for key in ("provider", "model", "status", "answer", "image_sha256", "response_sha256")
        },
    })


def preflight_create_only_output(path: Path) -> tuple[int, str]:
    absolute = path.absolute()
    if absolute.name != path.name or not absolute.name:
        raise ValueError("QC output filename is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(absolute.anchor, flags)
    try:
        # Descend under directory FDs rather than re-resolving an attacker-
        # controlled ancestor path after its safety check.
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("QC output parent has an unsafe component")
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise ValueError("QC output parent traverses unsafe directory") from exc
            os.close(parent_fd)
            parent_fd = child_fd
        if stat.S_IMODE(os.fstat(parent_fd).st_mode) != 0o700:
            raise ValueError("QC output parent must be 0700")
        try:
            os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent_fd, absolute.name
        raise FileExistsError("QC output already exists")
    except Exception:
        os.close(parent_fd)
        raise


def write_receipt_create_only(parent_fd: int, name: str, receipt: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    created = os.fstat(fd)
    try:
        data = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode()
        offset = 0
        while offset < len(data):
            wrote = os.write(fd, data[offset:])
            if wrote <= 0:
                raise OSError("QC receipt short write")
            offset += wrote
        os.fsync(fd)
    except Exception:
        try:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                # A concurrent unlink, replacement, or lstat failure cannot
                # make the original write/fsync failure less important.
                pass
            else:
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except OSError:
                        pass
        finally:
            raise
    finally:
        os.close(fd)
    os.fsync(parent_fd)


def _same_style_expectation(stored: object, current: object) -> bool:
    if stored == current:
        return True
    if not isinstance(stored, Mapping) or not isinstance(current, Mapping):
        return False
    old, new = copy.deepcopy(dict(stored)), copy.deepcopy(dict(current))
    old_asset, new_asset = old.get('expected_asset'), new.get('expected_asset')
    if not isinstance(old_asset, dict) or not isinstance(new_asset, dict):
        return False
    old_path, new_path = old_asset.pop('path', None), new_asset.pop('path', None)
    if old != new:
        return False
    expected = str(new_asset.get('sha256') or '').removeprefix('sha256:')
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        return False
    try:
        for raw_path in (old_path, new_path):
            if not isinstance(raw_path, str):
                return False
            path = Path(raw_path)
            if path.is_symlink() or not path.is_file():
                return False
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    return False
    except OSError:
        return False
    return True


def reuse_valid_qc(package_root: Path, title: str, receipt_path: Path) -> dict:
    """Read-only replay of an existing PASS through the uploader's real gate.

    Never relabel a historical model, overwrite a failed verdict, or invoke a
    provider. Every current QC input is rebound and checked again after replay.
    """
    from scripts.authorized_upload import (
        _sha_entry, _attach_title_cover_qc, _title_cover_qc_attestation_problems,
    )
    from src.autoslice.original_patch_package import json_file, sha_file

    root = package_root.absolute()
    receipt_path = receipt_path.absolute()
    review_path = root / "review_manifest.json"
    # The regular-file helpers reject links, nonfiles, excessive JSON and drift.
    review = json_file(review_path)
    record, publish, cover = resolve_package_inputs(root, title)
    candidate_id = resolve_candidate_id(record, review)
    item = review["items"][0]
    record_path = root / item["record"]
    publish_path = root / item["publish_json"]
    paths = (review_path, record_path, publish_path, cover, receipt_path)
    before = {path: sha_file(path) for path in paths}
    receipt = json_file(receipt_path)
    check = {
        "title": title,
        "cover": _sha_entry(cover),
        "package_attestation": {"package_root": str(root), "record": _sha_entry(record_path)},
    }
    errors = _attach_title_cover_qc(check, str(receipt_path))
    errors += _title_cover_qc_attestation_problems(check, required=True)
    if receipt.get("candidate_id") != candidate_id:
        errors.append("existing QC candidate differs from current package")
    style_expectation = _style_expectation_for_generation(publish.get("cover_generation"))
    if style_expectation is not None:
        stored_expectation = receipt.get("style_expectation")
        stored_asset = (
            stored_expectation.get("expected_asset")
            if isinstance(stored_expectation, Mapping)
            else None
        )
        expected_asset = style_expectation["expected_asset"]
        expected_reference = expected_asset.get("reference_image")
        witness = receipt.get("witness")
        observed_reference = witness.get("reference_image") if isinstance(witness, Mapping) else None
        reference_matches = expected_reference is None or (
            isinstance(expected_reference, Mapping)
            and isinstance(observed_reference, Mapping)
            and "sha256:" + str(observed_reference.get("image_sha256") or "")
                == expected_reference.get("sha256")
        )
        stored_research = receipt.get("research_application")
        stored_semantic = _research_semantic_verdict(
            {"research_application": stored_research}
        )
        research_valid = (
            stored_semantic is not None
            and stored_semantic["commitment_fidelity"] == "SUPPORTED"
            and stored_semantic["naturalness"] == "NATURAL"
        )
        if (
            not research_valid
            or not isinstance(stored_expectation, Mapping)
            or stored_expectation.get("binding_status") != "BOUND"
            or not isinstance(stored_asset, Mapping)
            or not isinstance(expected_asset, Mapping)
            or not _same_style_expectation(stored_expectation, style_expectation)
            or not reference_matches
        ):
            errors.append("existing QC lacks a current bound ordinary-talk style verdict")
    if errors:
        raise ValueError("existing QC is not reusable: " + "; ".join(errors))
    resolve_package_inputs(root, title)
    if any(sha_file(path) != digest for path, digest in before.items()):
        raise ValueError("QC input changed during reuse validation")
    return receipt


def run_qc(
    package_root: Path,
    title: str,
    out_path: Path,
    *,
    image_probe,
    reuse_existing: bool = False,
) -> dict:
    """Create a new receipt, or explicitly revalidate an unchanged existing PASS."""
    if reuse_existing and os.path.lexists(out_path):
        return reuse_valid_qc(package_root, title, out_path)
    record, publish, cover_path = resolve_package_inputs(package_root, title)
    review_path = package_root.absolute() / "review_manifest.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    parent_fd, output_name = preflight_create_only_output(out_path)
    try:
        candidate_id = resolve_candidate_id(record, review)
        review_candidate = review["items"][0].get("candidate_id") or review["items"][0].get("id")
        if review_candidate and review_candidate != candidate_id:
            raise ValueError("review candidate differs from current story authority")
        identity_context = package_identity_context(
            root=package_root.resolve(strict=True), item=review["items"][0],
            generation=publish["cover_generation"], cover_path=cover_path,
            candidate_id=candidate_id,
        )
        story_context = package_story_context(
            record=record, generation=publish["cover_generation"],
            candidate_id=candidate_id,
        )
        receipt = build_joint_qc_receipt(
            cover_path=cover_path,
            title=title,
            candidate_id=candidate_id,
            image_probe=image_probe,
            identity_context=identity_context,
            cover_generation=publish["cover_generation"],
            story_context=story_context,
        )
        if identity_context is not None:
            current = package_identity_context(
                root=package_root.resolve(strict=True), item=review["items"][0],
                generation=publish["cover_generation"], cover_path=cover_path,
                candidate_id=candidate_id,
            )
            if current != identity_context:
                raise ValueError("package identity context changed during probe")
        current_record, current_publish, _ = resolve_package_inputs(package_root, title)
        if package_story_context(
            record=current_record, generation=current_publish["cover_generation"],
            candidate_id=candidate_id,
        ) != story_context:
            raise ValueError("package story context changed during probe")
        write_receipt_create_only(parent_fd, output_name, receipt)
        return receipt
    finally:
        os.close(parent_fd)


def load_env(path: str) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_package_inputs(
    package_root: Path, title: str
) -> tuple[dict, dict, Path]:
    """Resolve the exact same-stem upload cover after daily assembly."""

    def strict_regular(
        path: Path, *, label: str, root: Path | None = None
    ) -> Path:
        absolute = path.absolute()
        cursor = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            cursor /= component
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{label} traverses a symlink")
        if not stat.S_ISREG(os.lstat(absolute).st_mode):
            raise ValueError(f"{label} is not a regular file")
        resolved = absolute.resolve(strict=True)
        if root is not None and not resolved.is_relative_to(root):
            raise ValueError(f"{label} escapes package root")
        return resolved

    package_absolute = package_root.absolute()
    package_info = os.lstat(package_absolute)
    if stat.S_ISLNK(package_info.st_mode) or not stat.S_ISDIR(package_info.st_mode):
        raise ValueError("package root is not a real directory")
    package_root = package_absolute.resolve(strict=True)
    review_path = package_root / "review_manifest.json"
    review_path = strict_regular(
        review_path, label="review manifest", root=package_root
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    items = review.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("review manifest must contain exactly one item")
    item = items[0]
    if item.get("title") != title:
        raise ValueError("review manifest title differs from joint-QC title")

    def package_file(key: str, *, direct: bool = True) -> Path:
        raw = item.get(key)
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise ValueError(f"review manifest {key} is not package-relative")
        relative = Path(raw)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"review manifest {key} has an unsafe component")
        path = strict_regular(
            package_root / relative,
            label=f"review manifest {key}",
            root=package_root,
        )
        if direct and path.parent != package_root:
            raise ValueError(f"review manifest {key} is not a package-root file")
        return path

    video_path = package_file("video")
    if video_path.suffix != ".mp4":
        raise ValueError("review manifest video is not MP4")
    upload_stem = video_path.name.removesuffix(".mp4")
    expected_cover_name = f"{upload_stem}.cover.png"
    expected_record_name = f"{upload_stem}.record.json"
    if item.get("cover") != expected_cover_name:
        raise ValueError("review manifest cover is not same-stem with video")
    if item.get("record") != expected_record_name:
        raise ValueError("review manifest record is not same-stem with video")
    record_path = package_file("record")
    publish_path = package_file("publish_json")
    cover_path = package_file("cover")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    if publish.get("title") != title:
        raise ValueError("publish title differs from joint-QC title")
    generation = publish.get("cover_generation")
    if not isinstance(generation, dict):
        raise ValueError("publish cover generation is missing")
    from src.autoslice.package_cover_network import validate_package_cover_network
    staging = record.get("publish_staging") or {}
    mirrored = staging.get("cover_generation") or {}
    generation_requires_binding = (
        any(surface.get(key) is not None for surface in (publish, staging)
            for key in ("current_binding", "title_selection"))
        or any(isinstance(value, Mapping) and any(value.get(key) is not None
            for key in ("input_binding", "request_sha256", "response_sha256"))
            for value in (generation, mirrored))
    )
    if generation_requires_binding:
        validate_package_cover_network(package_root, item, record, publish,
            subtitle_path=package_file("subtitle_srt"), cover_path=cover_path)
    generated_raw = generation.get("final_cover") or publish.get("cover_path")
    if not isinstance(generated_raw, str) or not generated_raw:
        raise ValueError("publish final cover path is missing")
    generated_path = Path(generated_raw)
    if not generated_path.is_absolute():
        generated_path = package_root / generated_path
    generated_path = generated_path.absolute()
    same_stem_sha = hashlib.sha256(cover_path.read_bytes()).hexdigest()
    declared_sha = str(generation.get("final_cover_sha256") or "").removeprefix(
        "sha256:"
    )
    # Song review packages are copied wholesale from the delivery location
    # into an independent package root (build_song_review_manifest.py);
    # publish.json's cover_generation.final_cover is copied byte-for-byte and
    # still names the pre-copy delivery path, which lives outside this
    # package root and may not even exist any more.  Detect that lexically
    # (no filesystem access outside the package) before ever touching the
    # path, and fall back to the package's own same-stem cover -- already
    # proven a real in-package regular file above -- only if its actual bytes
    # hash matches the frozen cover-generation hash recorded in publish.json.
    # Talk packages keep the video/publish/cover co-located, so their
    # final_cover always resolves inside the package root and this branch is
    # never taken for them.
    lexical_generated = Path(os.path.normpath(str(generated_path)))
    if not lexical_generated.is_relative_to(package_root):
        if not declared_sha or same_stem_sha != declared_sha:
            raise ValueError(
                "publish final cover escapes package root and the package's "
                "same-stem cover does not match the frozen cover generation "
                "hash"
            )
        return record, publish, cover_path
    generated_path = strict_regular(
        generated_path, label="publish final cover", root=package_root
    )
    generated_sha = hashlib.sha256(generated_path.read_bytes()).hexdigest()
    if not declared_sha or generated_sha != declared_sha or same_stem_sha != declared_sha:
        raise ValueError("same-stem cover differs from frozen cover generation")
    return record, publish, cover_path


def _rendered_style_palettes(
    generation: Mapping[str, object],
) -> list[Mapping[str, object]]:
    """Read every palette binding recorded by the local title render."""

    rendered = generation.get("rendered_text_pixels")
    candidates = [
        generation.get("text_palette"),
        rendered.get("text_palette") if isinstance(rendered, Mapping) else None,
        (
            rendered.get("render_spec", {}).get("text_palette")
            if isinstance(rendered, Mapping)
            and isinstance(rendered.get("render_spec"), Mapping)
            else None
        ),
    ]
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


def _style_expectation_for_generation(
    generation: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Resolve objective profile/research bindings for one actual redraw.

    The route and art-direction record decide whether this is an ordinary talk
    redraw.  The profile asset helper supplies the expected bytes and frozen
    research provenance; the declared generation binding must match those
    bindings before the semantic pixel verdict can be consumed.  The research
    projection itself is not a visual pass condition.
    Missing or drifted bindings remain a required-but-unbound expectation.
    """

    if not isinstance(generation, Mapping):
        return None
    route = generation.get("route_decision")
    direction = generation.get("art_direction")
    if not isinstance(route, Mapping):
        return None
    direction = direction if isinstance(direction, Mapping) else {}
    if direction.get("emote_mode") == "replace":
        return None
    method = generation.get("method")
    origin = generation.get("cover_origin")
    ordinary_cpa = (
        method == "images.edit"
        and origin == "AI_REDRAW"
        and route.get("actual_treatment") == "cpa_redraw"
    )
    ordinary_builtin = (
        method == "image_gen.imagegen"
        and origin == "BUILTIN_AI_REDRAW"
        and route.get("actual_treatment") == "builtin_imagegen_redraw"
    )
    if not ordinary_cpa and not ordinary_builtin:
        return None
    scene_kind = str(route.get("scene_kind") or "talk")
    relation_required = route.get("relationship_visual_required") is True
    render_palettes = _rendered_style_palettes(generation)
    render_palette = render_palettes[0] if render_palettes else None
    hook_color = direction.get("hook_color")
    if not isinstance(hook_color, str) and isinstance(render_palette, Mapping):
        hook_color = render_palette.get("primary")
    declared = generation.get("art_direction_style")
    declared_reference = declared.get("reference_image") if isinstance(declared, Mapping) else None
    reference_path = declared_reference.get("path") if isinstance(declared_reference, Mapping) else None
    expected_asset = ordinary_talk_redraw_style_binding(
        scene_kind=scene_kind,
        relationship_visual_required=relation_required,
        is_song=direction.get("is_song") is True,
        hook_color=str(hook_color) if hook_color is not None else None,
        style_reference_path=Path(reference_path) if isinstance(reference_path, str) and reference_path else None,
    )
    if expected_asset is None:
        return None
    # A sample is an optional aid, not a new prerequisite for the successful
    # single-identity-reference route.  Verify its provenance only when this
    # generation actually declares that it used one.
    if not isinstance(declared, Mapping) or not isinstance(declared.get("reference_image"), Mapping):
        expected_asset = dict(expected_asset)
        expected_asset.pop("reference_image", None)
    binding_status = "MISSING_DECLARATION"
    if isinstance(declared, Mapping):
        if (
            declared.get("schema_version") == expected_asset.get("schema_version")
            and declared.get("asset_key") == expected_asset.get("asset_key")
            and declared.get("status") == expected_asset.get("status") == "READY"
            and declared.get("sha256") == expected_asset.get("sha256")
            and declared.get("text") == expected_asset.get("text")
            and declared.get("reference_image") == expected_asset.get("reference_image")
            and declared.get("text_palette") == expected_asset.get("text_palette")
            and declared.get("research_projection") == expected_asset.get("research_projection")
            and str(declared.get("path") or "").strip()
        ):
            binding_status = "BOUND"
        else:
            binding_status = "DRIFTED_DECLARATION"
    expected_palette = expected_asset.get("text_palette")
    if binding_status == "BOUND":
        expected_reference = expected_asset.get("reference_image")
        sent_references = generation.get("style_reference_images")
        if ordinary_builtin and not isinstance(sent_references, list):
            from src.autoslice.builtin_imagegen_cover import validate_builtin_imagegen_provenance

            if validate_builtin_imagegen_provenance(generation):
                source = json.loads(Path(str(generation["builtin_imagegen_provenance_path"])).read_text())
                sent_references = [
                    {"sha256": ref.get("sha256"), "role": "STYLE_ONLY"}
                    for call in source["calls"] for ref in call["references"]
                ]
        if isinstance(expected_reference, Mapping) and not any(
            isinstance(sent, Mapping)
            and sent.get("sha256") == expected_reference.get("sha256")
            and sent.get("role") == "STYLE_ONLY"
            and sent.get("sent") is not False
            and sent.get("status") not in {"MISSING", "NOT_SENT_DEDUPLICATED"}
            for sent in (sent_references if isinstance(sent_references, list) else [])
        ):
            binding_status = "MISSING_SENT_STYLE_REFERENCE"
        elif not render_palettes:
            binding_status = "MISSING_PALETTE_BINDING"
        elif any(palette != expected_palette for palette in render_palettes):
            binding_status = "DRIFTED_PALETTE_BINDING"
    return {
        "required": True,
        "binding_status": binding_status,
        "scene_kind": scene_kind,
        "relationship_visual_required": relation_required,
        "expected_asset": dict(expected_asset),
        "selected_research_application": copy.deepcopy(
            direction.get("research_application")
            if isinstance(direction, Mapping)
            else None
        ),
    }


def _research_semantic_verdict(verdict: object) -> dict[str, object] | None:
    """Extract the CPA's one story-specific research applicability choice."""

    if not isinstance(verdict, Mapping):
        return None
    value = verdict.get("research_application")
    if not isinstance(value, Mapping):
        return None
    applicable = value.get("applicable")
    reason = value.get("reason")
    commitment_fidelity = value.get("commitment_fidelity")
    naturalness = value.get("naturalness")
    click_reason = value.get("click_reason")
    if (
        not isinstance(applicable, bool)
        or not isinstance(reason, str)
        or not reason.strip()
        or commitment_fidelity not in {"SUPPORTED", "PARTIAL", "OVERSTATED", "UNKNOWN"}
        or naturalness not in {"NATURAL", "AMBIGUOUS", "UNNATURAL", "UNKNOWN"}
        or not isinstance(click_reason, str)
        or not click_reason.strip()
    ):
        return None
    return {
        "applicable": applicable,
        "reason": reason.strip(),
        "commitment_fidelity": commitment_fidelity,
        "naturalness": naturalness,
        "click_reason": click_reason.strip(),
    }


def build_joint_qc_receipt(
    *,
    cover_path: Path,
    title: str,
    candidate_id: str,
    image_probe,
    logical_cover_path: str | None = None,
    identity_context: dict | None = None,
    cover_generation: Mapping[str, object] | None = None,
    story_context: dict | None = None,
) -> dict:
    """Probe staged cover bytes and return a receipt without writing it.

    ``logical_cover_path`` is deliberately separate from the bytes read by the
    probe.  A private-stage preflight may bind the intended public pathname
    only after proving the exact staged image SHA, without exposing its stage
    path as a final package locator.
    """
    cover_sha = hashlib.sha256(cover_path.read_bytes()).hexdigest()
    style_expectation = _style_expectation_for_generation(cover_generation)
    style_required = style_expectation is not None
    direction = cover_generation.get("art_direction") if isinstance(cover_generation, Mapping) else None
    is_song = isinstance(direction, Mapping) and direction.get("is_song") is True
    scene_context = (
        "本条是歌切 song-clean，封面规定只渲染《歌名》。single_clear_hook 由当场真实演唱主体"
        "与可读歌名共同判断，不要求杂谈事件、反转、悬念、额外口号或歌曲剧情。"
        "当场头套、面具或非人头部属于演出形象，不要求揭开原本遮住的脸，"
        "也不能根据历史发色、熊猫元素补造隐藏人脸；身份仍以本包源图—最终封面见证为依据。"
        "歌名大小写、标点须完整正确，标题与封面须指向同一演唱与歌曲。"
        "主体缺失、身份冲突、裁切/字层遮挡、歌名不可读、图文矛盾或虚构内容仍须拒绝。\n"
        if is_song else
        "杂谈的具体事件可以由主播神态与封面文字共同表达，不要求普通室内或虚拟直播背景逐字演示标题。"
        "不能仅凭背景没有被标题提及就认定图文无关，也不能仅因静帧没有演出台词中的动作就判定台词虚构。"
        "具体图文矛盾、虚构人物或事件、抢占主体的无关元素仍须列出，并说明可见依据；"
        "只有空泛口号、主体缺失、文案与标题不同题时仍须拒绝，身份正确本身不等于通过。\n"
    )
    if style_required:
        scene_context += (
            "本次是主动导演的 AI 重绘，single_clear_hook 也须判断画面与短字是否共同表达这条片的具体看点。"
            "观察与事件相连的动作、物件或前后状态关系；只重画通用头像，配惊讶、汗滴、星星或换色，"
            "即使身份、画风和文字事实正确，也不能据此认定叙事成立。无需逐字演出标题、强制分格或虚构道具；"
            "具体的自指、躲藏、困倦开会等反应可以成立。若像素仍只是可替换到任意切片的通用人形，"
            "single_clear_hook=false，并说明缺少哪种具体表达。截图和歌切继续按各自约定判断。\n"
        )
    question = (
        "你是李豆沙频道的标题+封面联合质检员。下图是最终封面,拟用标题是:\n"
        f"《{title}》\n"
        + scene_context
        +
        "请只输出一个 JSON 对象(不要 markdown 代码块,不要多余文字),字段与含义:\n"
        '{"lidousha_primary": bool 封面主体是否是李豆沙（以当场直播形象为准；不能把固定发色、熊猫耳、耳朵位置、帽子、墨镜或发饰当作所有场次必备或禁止的身份条件；证据不足时不得凭标题、白发或熊猫元素猜测身份，无法确认则为false）,'
        '"thumbnail_readable": bool 缩略图尺寸下封面大字是否清晰可读,'
        '"single_clear_hook": bool 封面文案是否构成一个清晰单一的钩子,'
        '"text_overcrowded": bool 文字是否过度拥挤,'
        '"title_cover_aligned": bool 标题与封面是否讲同一件事,'
        '"physical_text_line_count": int 封面主文案的物理行数,'
        '"unrelated_or_misleading_elements": [] 与内容无关或误导的元素列表(没有就空数组),'
        '"reason": str 一句话理由,'
        '"pass": bool 综合是否通过}\n如实判断,不要迎合。'
    )
    if story_context is not None:
        if story_context.get("candidate_id") != candidate_id:
            raise ValueError("source story context differs from current candidate")
        final_srt = story_context.get("final_srt")
        story_prompt_context = copy.deepcopy(story_context)
        if isinstance(final_srt, Mapping):
            story_prompt_context["final_srt"] = {
                key: value for key, value in final_srt.items() if key != "text"
            }
        question += (
            "\n以下是当前包故事合同中已绑定原素材和语境的事件摘要，补充标题省略的上下文。"
            "判断动作、物件或编辑符号是否有内容依据时须连同标题使用；它不是本次封面PASS，"
            "仍须拒绝像素里新增的事实、身份冲突、误导表达或空泛构图。"
            "无人形的编辑符号可以表达摘要中的事件，不应当作真实游戏截图或额外参与者。\n"
            + json.dumps(story_prompt_context, ensure_ascii=False, sort_keys=True)
        )
        if isinstance(final_srt, Mapping):
            if final_srt.get("status") == "AVAILABLE":
                question += (
                    "\n以下是当前包已声明且按 hash 读到的最终字幕原文。它只补充正文、回应和时间/模态语境，"
                    "不替代 StoryContract、声源或最终像素事实门；请按原文判断，不要把字幕中的假设、转述或游戏内容"
                    "改写成主播现实经历。final_srt_sha256="
                    + str(final_srt.get("sha256") or "(missing)")
                    + "\n"
                    + str(final_srt.get("text") or "")
                )
            else:
                question += (
                    "\n当前包声明了最终字幕路径，但最终字幕见证不可用（"
                    + str(final_srt.get("reason_code") or "UNKNOWN")
                    + "）。不要把缺失的最终字幕当作已证实的故事事实；"
                    "仍须按现有 StoryContract 和像素证据保守判断。"
                )
    if style_required:
        expected_asset = style_expectation["expected_asset"]
        if (
            isinstance(expected_asset, Mapping)
            and expected_asset.get("status") == "READY"
        ):
            palette = expected_asset.get("text_palette")
            palette = palette if isinstance(palette, Mapping) else {}
            question += (
                "\n本次是普通单人杂谈 AI 重绘。profile asset 和下方研究 projection 只提供"
                "有来源的编辑问题与可选视觉语言，不是永久画风、布局或点击收益门。请先根据标题、"
                "StoryContract/正文和当前最终封面判断本条选择是否适用，再判断最终像素是否清楚表达"
                "谁遇到什么、如何回应及其模态；不要因为提示文字、资产存在或某个 legacy 风格关键词"
                "出现在问题里就判通过。浅青纸感、粗深蓝线和可见笔触、大头比例和粉/黄字只是可能的"
                "历史参考，只有当本条选择和像素支持时才可采用；它们不是“制作采用以下当前 profile 画风要求”的永久门，也不替代实际 builtin prompt 或 source provenance。期望资产原文如下：\n"
                + str(expected_asset.get("text") or "")
                + "\n研究 projection（只作来源和限制记录）：\n"
                + json.dumps(expected_asset.get("research_projection"), ensure_ascii=False, sort_keys=True)
                + "\n本次导演选择（不是最终像素结论）：\n"
                + json.dumps(style_expectation.get("selected_research_application"), ensure_ascii=False, sort_keys=True)
                + "\n确定性文字层的实际绑定配色是主色 "
                + str(palette.get("primary") or "pink")
                + "、副色 "
                + str(palette.get("secondary") or "yellow")
                + "、深蓝描边；图像生成器不得自行绘制文字。"
                + "\n请在 JSON 中额外返回 research_application: {\"applicable\": bool, \"reason\": str, "
                "\"commitment_fidelity\": \"SUPPORTED|PARTIAL|OVERSTATED|UNKNOWN\", "
                "\"naturalness\": \"NATURAL|AMBIGUOUS|UNNATURAL|UNKNOWN\", "
                "\"click_reason\": str}。它是你对本条研究问题、题封承诺、画面自然度和"
                "点击理由的当前语义判断，不是评分；也可返回可选的"
                "style_matches_requested: bool 作为视觉语言观察，但它不能代替故事/像素判断。"
            )
            if isinstance(expected_asset.get("reference_image"), Mapping):
                question += (
                    "\n第一张图是待审最终封面，第二张图只是本次生成已采用的可选画风样张。"
                    "只比较本次导演选择的 visual language；样张的雷电、熊猫、文字、姿势和服装不是本片内容要求，"
                    "不应复制；身份服装仍以当前源图证据为准。样张发送 hash 已由客观 binding 校验，"
                    "不能单独证明研究适用或最终像素通过。"
                )
        else:
            question += (
                "\n本次普通杂谈重绘已注册风格资产，但生成回执没有可验证的完整资产绑定或资产不可读；"
                "style_matches_requested 必须为 false，不能凭画面关键词补救。"
                "请在 JSON 中额外返回 \"style_matches_requested\": false 和一个包含"
                "commitment_fidelity、naturalness、click_reason 的完整"
                "research_application 语义记录；这类绑定错误仍然会阻断。"
            )
    if identity_context is not None:
        binding = identity_context.get("package_binding", {})
        if (identity_context.get("candidate_id") != candidate_id
                or binding.get("final_cover_sha256") != "sha256:" + cover_sha):
            raise ValueError("source identity context differs from current candidate/cover")
        question += (
            "\n以下是当前包已重验哈希与原始裁决的独立源图—成品身份对比证据；"
            "它提供当场主播身份参考，避免只凭发色、熊猫元素或标题猜测。"
            "这份旧身份裁决不预设本次结果：仍须观察当前成品的身份、构图、文字和图文一致性；"
            "如当前可见内容与证据矛盾，指出具体冲突并返回false/FAIL。"
            "本次lidousha_primary与pass仍由你裁决。\n"
            + json.dumps(identity_context, ensure_ascii=False, sort_keys=True)
        )
    expected_reference = (
        style_expectation["expected_asset"].get("reference_image")
        if style_required else None
    )
    if isinstance(expected_reference, Mapping) and expected_reference.get("path"):
        witness = image_probe(
            cover_path, question,
            reference_image_path=Path(str(expected_reference["path"])),
        )
    else:
        witness = image_probe(cover_path, question)
    reference_observed = expected_reference is None or (
        isinstance(witness, Mapping)
        and isinstance(witness.get("reference_image"), Mapping)
        and isinstance(expected_reference, Mapping)
        and "sha256:" + str(witness["reference_image"].get("image_sha256") or "")
            == expected_reference.get("sha256")
    )
    if isinstance(witness, dict) and logical_cover_path is not None:
        # The probe observed private staged bytes, but the hash below binds
        # those exact bytes to the final logical cover.  Public consumers must
        # never retain the deleted stage locator as their witness image path.
        witness = dict(witness)
        witness["image_path"] = logical_cover_path
    verdict = None
    answer = witness.get("answer") if isinstance(witness, dict) else None
    if isinstance(answer, str):
        try:
            cleaned = answer.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`\n")
                cleaned = cleaned[cleaned.find("{"):]
            verdict = json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1])
            witness["answer"] = cleaned
        except ValueError:
            verdict = None
    research_verdict = _research_semantic_verdict(verdict)
    research_required = bool(
        style_required
        and isinstance(style_expectation.get("expected_asset"), Mapping)
        and style_expectation["expected_asset"].get("research_projection")
    )
    ok = (
        isinstance(verdict, dict) and isinstance(witness, dict)
        and witness.get("status") == "OBSERVED"
        and verdict.get("lidousha_primary") is True
        and verdict.get("thumbnail_readable") is True
        and verdict.get("single_clear_hook") is True
        and verdict.get("text_overcrowded") is False
        and verdict.get("title_cover_aligned") is True
        and verdict.get("physical_text_line_count") in (1, 2)
        and verdict.get("unrelated_or_misleading_elements") == []
        and isinstance(verdict.get("reason"), str)
        and bool(verdict.get("reason").strip())
        and verdict.get("pass") is True
        and (
            not research_required
            or (
                research_verdict is not None
                and research_verdict["commitment_fidelity"] == "SUPPORTED"
                and research_verdict["naturalness"] == "NATURAL"
            )
        )
        and (
            not style_required
            or (
                style_expectation.get("binding_status") == "BOUND"
                and reference_observed
            )
        )
    )
    style_match = (
        bool(
            isinstance(verdict, dict)
            and style_expectation.get("binding_status") == "BOUND"
            and reference_observed
            and verdict.get("style_matches_requested") is True
        )
        if style_required
        else None
    )
    receipt = {
        "schema_version": "lidousha-title-cover-joint-qc.v1",
        "candidate_id": candidate_id,
        "title": title,
        "title_sha256": "sha256:" + hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "cover_path": logical_cover_path or str(cover_path),
        "cover_sha256": "sha256:" + cover_sha,
        "preferred_provider": "cpa", "selected_provider": "cpa",
        "witness": witness, "verdict": verdict,
        "status": "PASS" if ok else "FAIL", "pass": bool(ok),
        "style_matches_requested": style_match,
        "research_application": research_verdict,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if style_expectation is not None:
        receipt["style_expectation"] = copy.deepcopy(style_expectation)
    if identity_context is not None:
        receipt["source_identity_context"] = copy.deepcopy(identity_context)
    if story_context is not None:
        receipt["source_story_context"] = copy.deepcopy(story_context)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("title")
    parser.add_argument("out_path", type=Path)
    parser.add_argument("--reuse-valid", action="store_true",
                        help="revalidate an existing PASS without a new model call or write")
    args = parser.parse_args()
    reused = args.reuse_valid and os.path.lexists(args.out_path)

    def probe(path: Path, prompt: str, *, reference_image_path: Path | None = None) -> dict:
        # Reusing a frozen result must not require credentials or provider health.
        env = load_env("/opt/bilive/autoslice/cpa.env")
        result = image_vision_probe(path, prompt, api_base=env.get("CPA_BASE_URL", ""), api_key=env.get("CPA_API_KEY", ""), reference_image_path=reference_image_path)
        result["image_path"] = str(path)
        return result

    from src.autoslice.production_attempts import observed_package_stage, record_production_result, package_stage_binding
    with observed_package_stage(args.package_root,'joint_qc'):
        input_binding = package_stage_binding(args.package_root)
        receipt = run_qc(args.package_root, args.title, args.out_path, image_probe=probe,
                         reuse_existing=args.reuse_valid)
        current_binding = package_stage_binding(args.package_root)
        if reused:
            from src.autoslice.cpa_runtime import record_cpa_event
            record_cpa_event('cache_hit',{'kind':'title_cover_joint_qc'})
        record_production_result({'runtime_stage_result':{
            'status':'JOINT_QC_PASS' if receipt['pass'] and input_binding==current_binding else 'BLOCKED',
            'reason_code':'PACKAGE_INPUT_BINDING_DRIFT' if input_binding!=current_binding else None if receipt['pass'] else 'TITLE_COVER_JOINT_QC_FAILED',
            **input_binding}})
    print("status:", receipt["status"], "| reused_existing:", reused,
          "| verdict:", json.dumps(receipt["verdict"], ensure_ascii=False)[:200])
    print("receipt:", args.out_path)
    return 0 if receipt["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
