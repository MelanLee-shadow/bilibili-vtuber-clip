"""Profile-selected branding intro prepended to delivered autoslice videos.

Ivan (2026-07-12) selected the 活字乱刷 candidate-2 render
（李豆沙一直是零，不对，李豆沙一直是为爱做一）as the fixed opening for all future
auto-slice deliverables.  The selected profile's committed
``branding_intro_manifest`` asset is the single switch and binds the exact
intro bytes by SHA-256; the media itself lives outside the repo tree
(``free:/opt/bilive/autoslice/assets/intro/``) like the CAM++ model and
enrollment WAVs.

Fail-closed contract: when the committed manifest is enabled, a missing or
hash-drifted intro, a failed concat, or a failed post-concat verification
must abort the delivery instead of shipping a clip without the intro.  A
manifest that is absent or ``enabled: false`` turns the feature off; media
problems never do.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
BRANDING_INTRO_SCHEMA = f"{CHANNEL_PROFILE.profile_id}-branding-intro.v1"
# v2 (Ivan 2026-07-20): the manifest may carry SEVERAL verified intros and the
# delivery rotates between them, keyed on the main clip's content hash so a
# re-burn of the same material never flips its intro (edit-replace and refix
# reruns must stay byte-stable relative to their recorded bindings).
BRANDING_INTRO_SCHEMA_V2 = f"{CHANNEL_PROFILE.profile_id}-branding-intro.v2"
BRANDING_INTRO_MANIFEST_RELPATH = CHANNEL_PROFILE.asset_file(
    "branding_intro_manifest"
).relative_to(REPO_ROOT)
BRANDING_INTRO_ENV_SWITCH = "AUTOSLICE_BRANDING_INTRO"
# Operator hand-pick escape hatch for the v2 rotation: must name an intro_id
# from the manifest; any other value fails closed (a typo must not silently
# change which intro ships).
BRANDING_INTRO_PICK_ENV = "AUTOSLICE_BRANDING_INTRO_PICK"
_COPY_CONCAT_DURATION_TOLERANCE_MS = 150
_REENCODE_DURATION_TOLERANCE_MS = 250
_X264_PROFILE_FLAGS = {"high": "high", "main": "main", "baseline": "baseline"}


class BrandingIntroError(RuntimeError):
    """Any condition that must block delivery instead of dropping the intro."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(part) for part in command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise BrandingIntroError(
            f"{command[0]} failed rc={completed.returncode}: {completed.stderr[-1200:]}"
        )
    return completed


def load_branding_intro_policy(manifest_path: Path) -> Mapping[str, object] | None:
    """Read the committed intro policy; absent or disabled means off."""

    if not manifest_path.is_file():
        return None
    try:
        policy = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrandingIntroError(f"unreadable branding intro manifest {manifest_path}: {exc}") from exc
    if not isinstance(policy, Mapping):
        raise BrandingIntroError(f"branding intro manifest is not an object: {manifest_path}")
    schema = policy.get("schema_version")
    if schema not in (BRANDING_INTRO_SCHEMA, BRANDING_INTRO_SCHEMA_V2):
        raise BrandingIntroError(
            f"unsupported branding intro schema {policy.get('schema_version')!r} in {manifest_path}"
        )
    if policy.get("enabled") is False:
        return None
    if policy.get("enabled") is not True:
        raise BrandingIntroError(f"branding intro manifest needs an explicit enabled flag: {manifest_path}")
    for entry in policy_intros(policy):
        _validate_intro_entry(entry, manifest_path)
    return policy


def policy_intros(policy: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Normalize v1 (single top-level intro) and v2 (``intros`` list) manifests."""

    if policy.get("schema_version") == BRANDING_INTRO_SCHEMA_V2:
        intros = policy.get("intros")
        if not isinstance(intros, list) or not intros:
            raise BrandingIntroError("branding intro v2 manifest lists no intros")
        if not all(isinstance(entry, Mapping) for entry in intros):
            raise BrandingIntroError("branding intro v2 manifest has a non-object intro entry")
        return list(intros)
    return [policy]


def _validate_intro_entry(entry: Mapping[str, object], manifest_path: Path) -> None:
    video = entry.get("video")
    if not isinstance(video, Mapping):
        raise BrandingIntroError(f"branding intro manifest has no video binding: {manifest_path}")
    sha = str(video.get("sha256") or "").removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise BrandingIntroError(f"branding intro manifest has no valid video sha256: {manifest_path}")
    if int(video.get("duration_ms", 0)) <= 0:
        raise BrandingIntroError(f"branding intro manifest has no positive duration: {manifest_path}")
    paths = entry.get("runtime_media_paths")
    if not isinstance(paths, list) or not paths:
        raise BrandingIntroError(f"branding intro manifest lists no runtime media paths: {manifest_path}")
    if not str(entry.get("intro_id") or ""):
        raise BrandingIntroError(f"branding intro manifest has no intro_id: {manifest_path}")


def resolve_intro_media(policy: Mapping[str, object], repo_root: Path) -> Path:
    """Find the first intro's bytes at a declared runtime path, hash-verified."""

    return _resolve_intro_entry(policy_intros(policy)[0], repo_root)


def _resolve_intro_entry(entry: Mapping[str, object], repo_root: Path) -> Path:
    expected_sha = str(entry["video"]["sha256"]).removeprefix("sha256:")  # type: ignore[index]
    attempts: list[str] = []
    for raw in entry.get("runtime_media_paths", []):  # type: ignore[union-attr]
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if not candidate.is_file():
            attempts.append(f"{candidate}: missing")
            continue
        actual_sha = _sha256_file(candidate)
        if actual_sha != expected_sha:
            attempts.append(f"{candidate}: sha256 drift {actual_sha[:12]}")
            continue
        return candidate
    raise BrandingIntroError(
        "branding intro media unavailable (policy enabled, delivery must not proceed): "
        + "; ".join(attempts)
    )


def require_branding_intro(
    repo_root: Path,
    *,
    manifest_path: Path | None = None,
) -> dict[str, object] | None:
    """Resolve the delivery-time intro context, or None when the feature is off.

    ``AUTOSLICE_BRANDING_INTRO=off`` is an explicit operator/test escape hatch;
    any other value of that variable is rejected so a typo cannot silently
    disable the intro.
    """

    env_value = os.environ.get(BRANDING_INTRO_ENV_SWITCH, "").strip().lower()
    if env_value == "off":
        return None
    if env_value not in {"", "on"}:
        raise BrandingIntroError(
            f"unsupported {BRANDING_INTRO_ENV_SWITCH} value {env_value!r} (use 'off' or unset)"
        )
    resolved_manifest = manifest_path or (repo_root / BRANDING_INTRO_MANIFEST_RELPATH)
    policy = load_branding_intro_policy(resolved_manifest)
    if policy is None:
        return None
    # Resolve EVERY declared intro up front: a drifted/missing member of the
    # rotation roster blocks delivery instead of silently narrowing the pool.
    candidates = [
        {
            "intro_id": str(entry["intro_id"]),
            "media_path": _resolve_intro_entry(entry, repo_root),
            "media_sha256": str(entry["video"]["sha256"]),  # type: ignore[index]
        }
        for entry in policy_intros(policy)
    ]
    rotation = policy.get("rotation") if isinstance(policy.get("rotation"), Mapping) else {}
    return {
        "policy": policy,
        "intro_id": candidates[0]["intro_id"],
        "media_path": candidates[0]["media_path"],
        "media_sha256": candidates[0]["media_sha256"],
        "candidates": candidates,
        "rotation_mode": str(rotation.get("mode") or ("main-sha256" if len(candidates) > 1 else "single")),
        "manifest_path": resolved_manifest,
        "manifest_sha256": _sha256_file(resolved_manifest),
    }


def _probe_media(path: Path) -> dict[str, object]:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,"
            "pix_fmt,sample_rate,channels,profile,level,time_base:format=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BrandingIntroError(f"ffprobe returned invalid JSON for {path}") from exc
    video = None
    audio = None
    counts = {"video": 0, "audio": 0, "other": 0}
    for stream in payload.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video":
            counts["video"] += 1
            video = video or stream
        elif kind == "audio":
            counts["audio"] += 1
            audio = audio or stream
        else:
            counts["other"] += 1
    duration_raw = (payload.get("format") or {}).get("duration")
    try:
        duration_ms = round(float(duration_raw) * 1000)
    except (TypeError, ValueError):
        duration_ms = 0
    if video is None or audio is None or duration_ms <= 0:
        raise BrandingIntroError(f"media has no probeable video+audio+duration: {path}")
    return {"video": video, "audio": audio, "counts": counts, "duration_ms": duration_ms}


def _fps_fraction(stream: Mapping[str, object]) -> str:
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = str(stream.get(key) or "")
        match = re.fullmatch(r"(\d+)/(\d+)", raw)
        if match and int(match.group(1)) > 0 and int(match.group(2)) > 0:
            return raw
    raise BrandingIntroError(f"cannot determine target frame rate from {dict(stream)!r}")


def _stream_contract(probe: Mapping[str, object]) -> dict[str, object]:
    video = probe["video"]
    audio = probe["audio"]
    return {
        "video_codec": video.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _fps_fraction(video),
        "pix_fmt": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name"),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
    }


def _video_timescale(stream: Mapping[str, object]) -> int | None:
    match = re.fullmatch(r"1/(\d+)", str(stream.get("time_base") or ""))
    return int(match.group(1)) if match else None


def _verify_prepended(
    output: Path,
    *,
    main_contract: Mapping[str, object],
    expected_duration_ms: int,
    tolerance_ms: int,
) -> dict[str, object]:
    probe = _probe_media(output)
    counts = probe["counts"]
    if counts["video"] != 1 or counts["audio"] != 1 or counts["other"] != 0:
        raise BrandingIntroError(f"prepended output stream contract drift: {counts}")
    contract = _stream_contract(probe)
    if contract != dict(main_contract):
        raise BrandingIntroError(
            f"prepended output stream parameters drifted: {contract} != {dict(main_contract)}"
        )
    duration_delta = abs(int(probe["duration_ms"]) - expected_duration_ms)
    if duration_delta > tolerance_ms:
        raise BrandingIntroError(
            f"prepended output duration off by {duration_delta}ms "
            f"(expected ~{expected_duration_ms}ms, got {probe['duration_ms']}ms)"
        )
    decode = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(output), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if decode.returncode != 0 or decode.stderr.strip():
        raise BrandingIntroError(
            f"prepended output failed full decode: rc={decode.returncode} {decode.stderr[-800:]}"
        )
    return {
        "full_decode": "clean",
        "duration_ms": int(probe["duration_ms"]),
        "duration_delta_ms": duration_delta,
        "stream_contract": contract,
    }


def _encode_matched_intro(
    intro_path: Path,
    destination: Path,
    *,
    main_probe: Mapping[str, object],
    contract: Mapping[str, object],
) -> None:
    width = int(contract["width"])
    height = int(contract["height"])
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={contract['fps']},format={contract['pix_fmt']},setsar=1"
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(intro_path),
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", video_filter,
        "-af", f"aresample={contract['sample_rate']}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-ar", str(contract["sample_rate"]), "-ac", str(contract["channels"]),
        "-movflags", "+faststart",
    ]
    profile_flag = _X264_PROFILE_FLAGS.get(str(main_probe["video"].get("profile") or "").lower())
    level_raw = main_probe["video"].get("level")
    if profile_flag and isinstance(level_raw, int) and level_raw > 0:
        command += ["-profile:v", profile_flag, "-level:v", f"{level_raw / 10:.1f}"]
    timescale = _video_timescale(main_probe["video"])
    if timescale:
        command += ["-video_track_timescale", str(timescale)]
    command.append(str(destination))
    _run(command)
    if not destination.is_file() or destination.stat().st_size < 2_048:
        raise BrandingIntroError(f"matched intro encode produced no usable file: {destination}")


def _concat_list_entry(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _concat_copy(intro_matched: Path, main_path: Path, output: Path, work_dir: Path) -> None:
    concat_list = work_dir / "concat.txt"
    concat_list.write_text(
        _concat_list_entry(intro_matched) + _concat_list_entry(main_path),
        encoding="utf-8",
    )
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy", "-movflags", "+faststart",
            str(output),
        ]
    )


def _concat_reencode(
    intro_path: Path,
    main_path: Path,
    output: Path,
    *,
    contract: Mapping[str, object],
) -> None:
    width = int(contract["width"])
    height = int(contract["height"])
    channels = int(contract["channels"])
    layout = "mono" if channels == 1 else "stereo"
    audio_norm = (
        f"aresample={contract['sample_rate']},"
        f"aformat=sample_fmts=fltp:sample_rates={contract['sample_rate']}:channel_layouts={layout}"
    )
    video_norm = f"fps={contract['fps']},format={contract['pix_fmt']},setsar=1"
    filter_complex = (
        f"[0:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,{video_norm}[iv];"
        f"[0:a:0]{audio_norm}[ia];"
        f"[1:v:0]{video_norm}[mv];"
        f"[1:a:0]{audio_norm}[ma];"
        f"[iv][ia][mv][ma]concat=n=2:v=1:a=1[v][a]"
    )
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(intro_path), "-i", str(main_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", str(contract["sample_rate"]), "-ac", str(channels),
            "-movflags", "+faststart",
            str(output),
        ]
    )


def _pick_intro(
    context: Mapping[str, object], main_sha256: str
) -> tuple[Mapping[str, object], dict[str, object] | None]:
    """Select the intro for this delivery from the verified candidate roster.

    Single-intro contexts (v1 manifests, hand-built contexts) keep the legacy
    top-level fields and no rotation binding.  With several candidates the
    choice is keyed on the main clip's content hash — deterministic, stateless,
    and stable across re-burns of the same material — unless the operator
    pins one explicitly via ``AUTOSLICE_BRANDING_INTRO_PICK`` (which must name
    an intro_id from the roster; anything else fails closed).
    """

    raw = context.get("candidates")
    candidates = [c for c in raw if isinstance(c, Mapping)] if isinstance(raw, list) else []
    if len(candidates) <= 1:
        # Single-intro contexts (v1 manifests, hand-built contexts) stay driven
        # by the legacy top-level fields so callers that pin or adjust them
        # keep their meaning.
        if "media_path" in context and "media_sha256" in context:
            return {
                "intro_id": context.get("intro_id"),
                "media_path": context["media_path"],
                "media_sha256": context["media_sha256"],
            }, None
        return candidates[0], None
    ids = [str(c["intro_id"]) for c in candidates]
    override = os.environ.get(BRANDING_INTRO_PICK_ENV, "").strip()
    if override:
        if override not in ids:
            raise BrandingIntroError(
                f"{BRANDING_INTRO_PICK_ENV}={override!r} names no manifest intro (have: {', '.join(ids)})"
            )
        index = ids.index(override)
        reason = "env_override"
    else:
        index = int(main_sha256[:16], 16) % len(candidates)
        reason = "main_sha256"
    return candidates[index], {
        "mode": str(context.get("rotation_mode") or "main-sha256"),
        "selector": reason,
        "candidate_intro_ids": ids,
        "picked_intro_id": ids[index],
    }


def prepend_branding_intro(
    *,
    context: Mapping[str, object],
    main_path: Path,
    work_dir: Path,
) -> dict[str, object]:
    """Prepend the verified intro to ``main_path`` in place, fail closed.

    The intro is re-encoded on the same host to the exact stream parameters of
    the main clip, then losslessly concatenated (``-c copy``).  If the copy
    concat cannot be verified (stream contract, duration, full decode), a full
    re-encode concat is attempted; if that cannot be verified either, the
    delivery must fail.
    """

    if not main_path.is_file():
        raise BrandingIntroError(f"main clip missing before intro prepend: {main_path}")
    main_sha_before = _sha256_file(main_path)
    picked, rotation_binding = _pick_intro(context, main_sha_before)
    intro_path = Path(str(picked["media_path"]))
    expected_sha = str(picked["media_sha256"]).removeprefix("sha256:")
    actual_sha = _sha256_file(intro_path)
    if actual_sha != expected_sha:
        raise BrandingIntroError(
            f"branding intro media drifted at render time: {intro_path} {actual_sha[:12]}"
        )
    main_probe = _probe_media(main_path)
    contract = _stream_contract(main_probe)
    main_counts = main_probe["counts"]

    work_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[str] = []
    result: dict[str, object] | None = None
    output = work_dir / "with-intro.mp4"

    copy_eligible = (
        contract["video_codec"] == "h264"
        and contract["audio_codec"] == "aac"
        and main_counts["video"] == 1
        and main_counts["audio"] == 1
        and main_counts["other"] == 0
    )
    if copy_eligible:
        try:
            intro_matched = work_dir / "intro-matched.mp4"
            _encode_matched_intro(
                intro_path, intro_matched, main_probe=main_probe, contract=contract
            )
            matched_probe = _probe_media(intro_matched)
            _concat_copy(intro_matched, main_path, output, work_dir)
            verification = _verify_prepended(
                output,
                main_contract=contract,
                expected_duration_ms=int(matched_probe["duration_ms"]) + int(main_probe["duration_ms"]),
                tolerance_ms=_COPY_CONCAT_DURATION_TOLERANCE_MS,
            )
            result = {
                "method": "matched-intro-concat-copy",
                "intro_offset_ms": int(matched_probe["duration_ms"]),
                "verification": verification,
            }
        except BrandingIntroError as exc:
            attempts.append(f"matched-intro-concat-copy: {exc}")
            result = None
    else:
        attempts.append(f"matched-intro-concat-copy: ineligible main contract {contract} {main_counts}")

    if result is None:
        _concat_reencode(intro_path, main_path, output, contract=contract)
        intro_probe = _probe_media(intro_path)
        reencode_contract = dict(contract)
        # The whole timeline is re-encoded by libx264/aac on this host; codec
        # identity is preserved but profile-dependent metadata may differ from
        # the source clip, so only the concat-copy path pins profile/level.
        verification = _verify_prepended(
            output,
            main_contract=reencode_contract,
            expected_duration_ms=int(intro_probe["duration_ms"]) + int(main_probe["duration_ms"]),
            tolerance_ms=_REENCODE_DURATION_TOLERANCE_MS,
        )
        result = {
            "method": "concat-filter-reencode",
            "intro_offset_ms": int(intro_probe["duration_ms"]),
            "verification": verification,
        }

    os.replace(output, main_path)
    shutil.rmtree(work_dir, ignore_errors=True)
    return {
        "status": "PREPENDED",
        "intro_id": str(picked["intro_id"]),
        "rotation": rotation_binding,
        "policy_manifest_path": str(context["manifest_path"]),
        "policy_manifest_sha256": str(context["manifest_sha256"]),
        "intro_media_path": str(intro_path),
        "intro_media_sha256": expected_sha,
        "main_sha256_before": main_sha_before,
        "main_duration_ms_before": int(main_probe["duration_ms"]),
        "fallback_attempts": attempts,
        **result,
    }
