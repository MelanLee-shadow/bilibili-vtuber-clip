"""Bounded, blind source-frame observation before display-caption filtering."""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL
from src.autoslice.cpa_frame_witness import _extract_frame_jpeg
from src.autoslice.infra_retry_policy import _bounded_retry_delay_seconds
from src.autoslice.nested_media_caption_dedup import POLICY, SCHEMA, CaptionDedupError
from src.autoslice.package_import import atomic_write_bytes, atomic_write_json, exclusive_lock, json_bytes
from src.autoslice.producer_nested_external_audio import (
    EXTERNAL_AUDIO_SCENE_SOURCE_ROLE,
    normalize_source_controls,
)
from src.autoslice.subtitle_audio_correspondence import parse_timed_srt

MODEL = TOP_CPA_MODEL
BATCH_SIZE = 12
MAX_CALLS = 7  # Legacy name: soft resource target; candidate history never resets.
FOCUS_FRAME_VARIANT = "focus-controls-1920-v1"
PROMPT = """Observe only the supplied unburned livestream source pixels. Images are in the listed order.
Do not infer words from audio, a transcript, a title or prior knowledge. For each image return its frame_id,
scene (WATCHED_MEDIA, NOT_WATCHED_MEDIA, or UNKNOWN), and captions (a list of verbatim strings).
First locate the OUTERMOST independent video-player rectangle, using visible player controls,
timeline/progress, or other clear player evidence when available. If that player is visible, the
whole player contents are WATCHED_MEDIA, including a game window, menus, character nameplates,
dialogue boxes, and a streamer inside the watched video. Do not classify those contents as the
OUTER live stream's game UI merely because the inner video shows a game or anime-style art.
Game UI text (menus, nameplates, choices, dialogue/narration boxes, status text, and controls) is
content evidence only: do not put it in captions and do not authorize deleting a matching transcript
line. Return only distinct verbatim subtitle/caption lines visibly overlaid as subtitles inside the
player, including large yellow commentary subtitles and white subtitles in a deeper embedded video.
Exclude the OUTER live stream's captions, chat, gifts, banners, game UI, and our pipeline captions.
Preserve incomplete visible text exactly; do not finish
words or invent punctuation/words. If no such caption is visible, captions is []. A scene can be WATCHED_MEDIA
with no caption. NOT_WATCHED_MEDIA also covers clearly visible host applications, documents, browser
pages or audio-button soundboards without an independent video-player surface. It also covers a
clearly visible standalone host-talk composition: the host avatar/camera and microphone occupy the
livestream stage, with integrated live chat, gifts or a song-list panel, and no separate watched-video
surface. This ordinary talking scene does not require an application window or gameplay to qualify.
An avatar or anime artwork alone, a cropped/obscured scene, or the host shown inside another player
is insufficient; retain UNKNOWN when those alternatives cannot be distinguished. UI labels, chat and
gifts are content evidence, never subtitles; this visual classification does not identify acoustic speakers.
When an audio-button control is visibly present, optionally include source_controls with exact
visible_ui labels, button_under_cursor (only when the cursor visibly rests on one listed label), and
visible_site_or_url (only when the pixels show it). A cursor, color, selected styling, or label never
proves that audio is playing; omit source_controls when no such control is visible.
Without sufficient pixels distinguishing a watched video from the host's own application or gameplay,
use UNKNOWN; game or anime art alone is not evidence for NOT_WATCHED_MEDIA. This does
not identify any acoustic speaker.
A visible native gameplay composition can establish NOT_WATCHED_MEDIA without an OS window border:
look for the host avatar and live-chat panels integrated into the outer livestream stage, a playing-game
banner, and native interactive game controls or choices in that stage, with no enclosing independent
video-player timeline or playback surface. Their co-occurrence is evidence of the host's own gameplay;
a game drawing or a rectangle alone is not. Evaluate the supplied ordered frames jointly for this
composition, including visible choice/cursor or dialogue changes. A visual-novel skip/auto/history icon
belongs to game UI; do not confuse it with an enclosing video-player timeline. If a separate enclosing
player is visible, all of its contents remain WATCHED_MEDIA despite those game controls or a streamer
inside it. When the composition is cropped, obscured, or still ambiguous, preserve UNKNOWN. This scene
classification neither identifies a voice nor permits deleting game dialogue from the transcript.
Return JSON only: {"frames":[{"frame_id":"...","scene":"...","captions":["..."],"source_controls":{"visible_ui":["..."],"button_under_cursor":"...","visible_site_or_url":"..."}}]}.
"""

SCENE_VALIDATION_PROMPT = """Independently check scene ownership from these unburned livestream pixels.
Do not use audio, transcripts, titles or earlier model answers. First locate the OUTERMOST independent
video-player rectangle. Visible player controls, timeline/progress, or other clear player evidence
supports WATCHED_MEDIA. Everything inside that player remains watched-video content, including a game
window, visual-novel dialogue/narration boxes, character nameplates, menus, choices, skip/auto/history/
settings controls, and a playing-game banner. These game-UI pixels are evidence only and never
authorize deleting a matching transcript line; return captions: [] for them. A separate player can
contain a game even when the host is not visibly operating it. Use NOT_WATCHED_MEDIA only when the
pixels clearly show the host's own application, document, browser page, audio-button soundboard, or
direct gameplay with no separate video-player surface. A clearly visible standalone host-talk
composition also qualifies: host avatar/camera and microphone on the livestream stage, integrated
live chat, gifts or song-list panel, and no separate watched-video surface. An application window or
active gameplay is not required for this ordinary talking scene. An avatar or anime artwork alone,
a cropped/obscured scene, or a host shown inside another player does not establish this negative
scene; retain UNKNOWN if those alternatives cannot be distinguished. Text buttons and audio
playback are not video players or subtitles and do not establish acoustic speaker identity. Anime or
game art, a game window, or a rectangular panel alone is insufficient for that decision; without
enough player/host-operation evidence use UNKNOWN. This decides only scene kind, never acoustic
speaker identity. For a visible audio-button soundboard, optionally preserve exact source_controls
with visible_ui, button_under_cursor, and visible_site_or_url. These are pixel evidence only;
cursor or color never proves playback. Return captions: [] for every frame.
A visible native gameplay composition can establish NOT_WATCHED_MEDIA without an OS window border:
look for the host avatar and live-chat panels integrated into the outer livestream stage, a playing-game
banner, and native interactive game controls or choices in that stage, with no enclosing independent
video-player timeline or playback surface. Their co-occurrence is evidence of the host's own gameplay;
a game drawing or a rectangle alone is not. Evaluate the supplied ordered frames jointly for this
composition, including visible choice/cursor or dialogue changes. A visual-novel skip/auto/history icon
belongs to game UI; do not confuse it with an enclosing video-player timeline. If a separate enclosing
player is visible, all of its contents remain WATCHED_MEDIA despite those game controls or a streamer
inside it. When the composition is cropped, obscured, or still ambiguous, preserve UNKNOWN. This scene
classification neither identifies a voice nor permits deleting game dialogue from the transcript.
Return JSON only: {"frames":[{"frame_id":"...","scene":"WATCHED_MEDIA|NOT_WATCHED_MEDIA|UNKNOWN","captions":[],"source_controls":{"visible_ui":["..."],"button_under_cursor":"...","visible_site_or_url":"..."}}]}.
"""

FOCUS_CONTROLS_INSTRUCTION = """
Focused frame check: independently inspect BOTH scene ownership and visible audio-button controls.
Every returned frame MUST include source_controls, even when scene is NOT_WATCHED_MEDIA or UNKNOWN.
For a visible audio-button page, transcribe its legible button labels in visible_ui, and identify
button_under_cursor only if the actual pointer is visibly on that same legible button. Selected
color alone is insufficient. Do not infer a label from audio, transcript or an expected source.
Use null for an unreadable cursor label or absent visible URL. If no audio-button control is visible,
return source_controls: null explicitly. Do not omit this independent inspection when captions is [].
These controls are pixel evidence only; they never prove playback or acoustic speaker identity.
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _confirmed_http_failure(receipt) -> int | None:
    if (not isinstance(receipt, Mapping) or receipt.get("status") != "UNAVAILABLE"
            or receipt.get("provider") != "cpa" or receipt.get("reason_code") != "VISION_CALL_FAILED"):
        return None
    code = receipt.get("provider_status_code") if receipt.get("dispatch_outcome") == "HTTP_ERROR_RESPONSE" else None
    if code is None:
        # Older real receipts preserved urllib's complete HTTP error string.
        # A timeout, malformed completion or ambiguous dispatch is ineligible.
        match = re.fullmatch(r"HTTPError: HTTP Error ([0-9]{3}): [^\r\n]+", str(receipt.get("error", "")))
        code = int(match[1]) if match else None
    return code if type(code) is int and (code == 429 or 500 <= code <= 599) else None


def _file_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CaptionDedupError("CAPTION_OBSERVER_SOURCE_UNSAFE")
    before = path.stat()
    from .evidence_identity import EvidenceFileChangedError
    try:
        digest = _evidence_file_sha256(path)
    except EvidenceFileChangedError as exc:
        raise CaptionDedupError("CAPTION_OBSERVER_SOURCE_CHANGED") from exc
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise CaptionDedupError("CAPTION_OBSERVER_SOURCE_CHANGED")
    return digest


def _parse(receipt: Mapping, frame_ids: list[str], *, require_source_controls=False) -> list[dict]:
    if receipt.get("status") != "OBSERVED":
        if code := _confirmed_http_failure(receipt):
            error = CaptionDedupError(f"CAPTION_OBSERVER_PROVIDER_UNAVAILABLE: HTTP {code}")
            error.reason_code = "CAPTION_OBSERVER_PROVIDER_UNAVAILABLE"
            raise error
        raise CaptionDedupError("CAPTION_OBSERVER_PROVIDER_UNAVAILABLE")
    answer = str(receipt.get("answer", "")).strip()
    if answer.startswith("```json\n") and answer.endswith("\n```"):
        answer = answer[8:-4]
    try:
        rows = json.loads(answer)["frames"]
        if not isinstance(rows, list) or len(rows) != len(frame_ids):
            raise ValueError("incomplete rows")
        for row, identity in zip(rows, frame_ids, strict=True):
            if (not isinstance(row, dict) or row.get("frame_id") != identity
                    or row.get("scene") not in {"WATCHED_MEDIA", "NOT_WATCHED_MEDIA", "UNKNOWN"}
                    or not isinstance(row.get("captions"), list)
                    or len(row["captions"]) > 12
                    or any(not isinstance(text, str) or len(text) > 500 for text in row["captions"])
                    or (row["scene"] != "WATCHED_MEDIA" and row["captions"])):
                raise ValueError("invalid observation")
            if require_source_controls and "source_controls" not in row:
                raise ValueError("focused control inspection missing")
            if "source_controls" in row:
                row["source_controls"] = normalize_source_controls(row["source_controls"])
        return rows
    except (ValueError, TypeError, KeyError) as exc:
        raise CaptionDedupError("CAPTION_OBSERVER_RESPONSE_INVALID") from exc


def _validate_focus_cue_indexes(focus_cue_indexes, cues) -> tuple[int, ...]:
    """Validate explicit cue ids without changing the default survey contract."""
    if focus_cue_indexes is None:
        return ()
    if not isinstance(focus_cue_indexes, (list, tuple)):
        raise CaptionDedupError("CAPTION_OBSERVER_FOCUS_CUES_INVALID")
    if any(isinstance(index, bool) or not isinstance(index, int)
           for index in focus_cue_indexes):
        raise CaptionDedupError("CAPTION_OBSERVER_FOCUS_CUES_INVALID")
    if len(set(focus_cue_indexes)) != len(focus_cue_indexes):
        raise CaptionDedupError("CAPTION_OBSERVER_FOCUS_CUES_INVALID")
    cue_ids = {int(cue.index) for cue in cues}
    if any(index not in cue_ids for index in focus_cue_indexes):
        raise CaptionDedupError("CAPTION_OBSERVER_FOCUS_CUES_INVALID")
    return tuple(sorted(focus_cue_indexes))


def observe_nested_captions(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    source_start_ms: int, source_end_ms: int, subtitle_path: Path,
    evidence_root: Path, cache_root: Path, probe: Callable,
    extract_frame: Callable = _extract_frame_jpeg,
    extract_focus_frame: Callable | None = None,
    focus_cue_indexes: list[int] | tuple[int, ...] | None = None,
    chat_authority_audit: dict | None = None,
) -> dict:
    """Return an effective spec; never edit transcript or infer acoustic identity.

    Survey samples establish sampled scene evidence only. Whole-cue observation
    is bounded separately. The existing deterministic consumer owns any deletion.
    """
    effective = dict(spec)
    if spec.get("nested_media_caption_dedup") is not None:
        return effective  # Existing bound evidence is validated by the consumer.
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", candidate_id):
        raise CaptionDedupError("CAPTION_OBSERVER_CANDIDATE_INVALID")
    original = subtitle_path.read_bytes()
    cues = parse_timed_srt(original.decode("utf-8"), label="caption observer")
    if (not cues or source_start_ms < 0 or source_end_ms <= source_start_ms
            or any(cue.start_ms < 0 or cue.end_ms <= cue.start_ms
                   or source_start_ms + cue.end_ms > source_end_ms for cue in cues)):
        raise CaptionDedupError("CAPTION_OBSERVER_TIMELINE_INVALID")
    focus_cue_indexes = _validate_focus_cue_indexes(focus_cue_indexes, cues)
    cues_by_index = {int(cue.index): cue for cue in cues}
    source_sha = _file_sha(source_media)
    binding = {"candidate_id": candidate_id, "source_media_sha256": source_sha,
               "input_transcript_sha256": _sha(original),
               "source_interval_ms": [source_start_ms, source_end_ms]}
    folder = cache_root / candidate_id
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "observer.json"
    with exclusive_lock(folder / "observer.lock", label="caption observer"):
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "schema_version": "watched-media-caption-observer.v1", "candidate_id": candidate_id,
            "max_calls": MAX_CALLS, "calls_consumed": 0, "batches": {},
        }
        if (state.get("candidate_id") != candidate_id or state.get("max_calls") != MAX_CALLS
                or not isinstance(state.get("batches"), dict)):
            raise CaptionDedupError("CAPTION_OBSERVER_CACHE_INVALID")
        frames = {}
        # A begun candidate keeps its exact request contract across software
        # updates. Replaying older observations is not testing the new prompt.
        frozen_prompt = PROMPT
        for cached in state["batches"].values():
            request = cached.get("request", {})
            if (cached.get("status") == "COMPLETE" and request.get("model") == MODEL
                    and all(request.get(key) == value for key, value in binding.items())):
                question = request.get("question", "")
                try:
                    prefix, metadata = question.rsplit("\n", 1)
                    if json.loads(metadata).get("phase") == "scene_survey":
                        frozen_prompt = prefix
                        break
                except (ValueError, AttributeError):
                    raise CaptionDedupError("CAPTION_OBSERVER_CACHE_INVALID") from None

        focus_frames = {}

        def frame(cue):
            identity = f"cue-{cue.index}"
            ms = source_start_ms + (cue.start_ms + cue.end_ms) // 2
            path = folder / "frames" / f"{source_sha}-{ms}.jpg"
            if path.is_symlink():
                raise CaptionDedupError("CAPTION_OBSERVER_FRAME_UNSAFE")
            if not path.exists():
                atomic_write_bytes(path, extract_frame(source_media, ms))
            row = {"frame_id": identity, "cue_index": int(cue.index), "frame_ms": ms,
                   "frame_path": str(path.absolute()), "frame_sha256": _file_sha(path)}
            frames[identity] = row
            return row

        def focus_frame(cue):
            """Extract a control-reading frame without changing survey cache bytes."""
            identity = f"cue-{cue.index}"
            ms = source_start_ms + (cue.start_ms + cue.end_ms) // 2
            path = folder / "frames" / f"{source_sha}-{ms}-{FOCUS_FRAME_VARIANT}.jpg"
            if path.is_symlink():
                raise CaptionDedupError("CAPTION_OBSERVER_FRAME_UNSAFE")
            if not path.exists():
                if extract_focus_frame is not None:
                    payload = extract_focus_frame(source_media, ms)
                elif extract_frame is _extract_frame_jpeg:
                    payload = extract_frame(source_media, ms, max_width=1920)
                else:
                    # Preserve the historical two-argument injection contract
                    # used by offline callers and tests.
                    payload = extract_frame(source_media, ms)
                atomic_write_bytes(path, payload)
            row = {
                "frame_id": identity,
                "cue_index": int(cue.index),
                "frame_ms": ms,
                "frame_path": str(path.absolute()),
                "frame_sha256": _file_sha(path),
                "frame_variant": FOCUS_FRAME_VARIANT,
            }
            focus_frames[identity] = row
            return row

        def batch(rows, phase, *, prompt=None):
            question = (prompt or frozen_prompt) + "\n" + json.dumps({"phase": phase, "frame_ids": [r["frame_id"] for r in rows]})
            request = {**binding, "frames": rows, "question": question, "model": MODEL}
            key = _sha(json.dumps(request, sort_keys=True).encode())
            cached = state["batches"].get(key)
            prior_attempts = []
            if cached is not None and cached.get("status") != "COMPLETE":
                code = _confirmed_http_failure(cached.get("receipt"))
                if (cached.get("status") != "FAILED" or cached.get("request") != request
                        or code is None):
                    raise CaptionDedupError("CAPTION_OBSERVER_AMBIGUOUS_OR_FAILED_DISPATCH")
                prior_attempts = cached.get("prior_attempts", [])
                if not isinstance(prior_attempts, list) or any(not isinstance(row, dict) for row in prior_attempts):
                    raise CaptionDedupError("CAPTION_OBSERVER_CACHE_INVALID")
                if "infra_retry" not in cached:
                    # Legacy failures have no dispatch timestamp. The last
                    # locked cache write is a conservative failure-time bound.
                    failed_at = cached.get("failed_at_epoch", state_path.stat().st_mtime)
                    cached["infra_retry"] = {
                        "failure_time_source": "dispatch" if "failed_at_epoch" in cached else "legacy_cache_mtime",
                        "retry_not_before_epoch": failed_at + _bounded_retry_delay_seconds(len(prior_attempts) + int(code == 429)),
                        "provider_status_code": code,
                    }
                    atomic_write_json(state_path, state)
                if time.time() < cached["infra_retry"]["retry_not_before_epoch"]:
                    raise CaptionDedupError("CAPTION_OBSERVER_PROVIDER_RETRY_BACKOFF")
                # Keep each original failure intact; a new charged attempt
                # never refunds or converts it into a visual observation.
                prior_attempts = [*prior_attempts,
                                  {k: v for k, v in cached.items() if k != "prior_attempts"}]
                cached = None
            if cached is not None:
                if cached.get("status") != "COMPLETE" or cached.get("request") != request:
                    raise CaptionDedupError("CAPTION_OBSERVER_AMBIGUOUS_OR_FAILED_DISPATCH")
                receipt = cached["receipt"]
                parsed = _parse(receipt, [r["frame_id"] for r in rows],
                                require_source_controls=phase == "scene_focus")
            else:
                claim = {"status": "DISPATCHED", "request": request}
                if prior_attempts:
                    claim["prior_attempts"] = prior_attempts
                state["batches"][key] = claim
                state["calls_consumed"] += 1
                atomic_write_json(state_path, state)  # Charge before external dispatch.
                try:
                    receipt = probe([Path(r["frame_path"]) for r in rows], question)
                    if isinstance(receipt, Mapping):
                        claim["receipt"] = dict(receipt)
                        atomic_write_json(state_path, state)
                    expected = [{"image_path": r["frame_path"], "image_sha256": r["frame_sha256"]} for r in rows]
                    if receipt.get("images") != expected:
                        raise CaptionDedupError("CAPTION_OBSERVER_PROBE_PIXEL_BINDING_MISMATCH")
                    parsed = _parse(receipt, [r["frame_id"] for r in rows],
                                    require_source_controls=phase == "scene_focus")
                    claim.update(status="COMPLETE", receipt=receipt)
                except Exception as exc:
                    reason_code = getattr(exc, "reason_code", None)
                    if not isinstance(reason_code, str) or not reason_code:
                        reason_code = str(exc) if isinstance(exc, CaptionDedupError) else type(exc).__name__
                    if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", reason_code)
                            or (isinstance(exc, CaptionDedupError)
                                and not reason_code.startswith("CAPTION_OBSERVER_"))):
                        reason_code = type(exc).__name__
                    claim.update(
                        status="FAILED", error_type=type(exc).__name__, reason_code=reason_code,
                        failed_at_epoch=time.time(),
                    )
                    atomic_write_json(state_path, state)
                    raise
                atomic_write_json(state_path, state)
            return parsed

        indexes = sorted({round(i * (len(cues) - 1) / 5) for i in range(6)})
        survey = batch([frame(cues[i]) for i in indexes], "scene_survey")
        focus_observations = {}
        if focus_cue_indexes:
            # A scene-only survey did not inspect controls. Reuse a focus
            # observation only when it actually returned controls or null;
            # a missing field is not evidence that no control was visible.
            sampled_frame_ids = {row["frame_id"] for row in survey
                                 if "source_controls" in row}
            focus_rows = [focus_frame(cues_by_index[index]) for index in focus_cue_indexes
                          if f"cue-{index}" not in sampled_frame_ids]
            focus_observations.update(
                (row["frame_id"], row) for row in survey
                if row["frame_id"] in sampled_frame_ids
                and row["frame_id"] in {f"cue-{index}" for index in focus_cue_indexes}
            )
            if focus_rows:
                focus_observations.update(
                    (row["frame_id"], row)
                    for row in batch(focus_rows, "scene_focus",
                                     prompt=frozen_prompt + FOCUS_CONTROLS_INSTRUCTION)
                )
        focus_results = [focus_observations[f"cue-{index}"] for index in focus_cue_indexes]
        focus_results_with_frames = [
            {**(focus_frames.get(row["frame_id"]) or frames[row["frame_id"]]), **row}
            for row in focus_results
        ]
        watched = any(row["scene"] == "WATCHED_MEDIA"
                      for row in [*survey, *focus_results])
        fallback_observations = {}
        validation_frames = [frame(cues[i]) for i in indexes]
        if watched:
            validation_ids = {row["frame_id"] for row in validation_frames}
            validation_frames.extend(
                frame(cues_by_index[index])
                for index in focus_cue_indexes
                if f"cue-{index}" not in validation_ids
                and focus_observations[f"cue-{index}"]["scene"] == "WATCHED_MEDIA"
            )
        scene_validation = None
        rejected_discoveries = []
        if watched:
            scene_validation = batch(validation_frames, "scene_validation", prompt=SCENE_VALIDATION_PROMPT)
            watched = any(row["scene"] == "WATCHED_MEDIA" for row in scene_validation)
        if not watched and (any(row["scene"] == "UNKNOWN" for row in survey)
                            or any(row["scene"] == "UNKNOWN" for row in (scene_validation or []))):
            # Player controls may appear briefly between the survey samples.
            # Inspect previously unseen cue frames, preserving UNKNOWN and the
            # original receipts. These same caption reads are reused below.
            fallback_observations = {row["frame_id"]: row for row in survey}
            remaining = [cue for cue in cues if f"cue-{cue.index}" not in fallback_observations]
            for offset in range(0, len(remaining), BATCH_SIZE):
                rows = [frame(cue) for cue in remaining[offset:offset + BATCH_SIZE]]
                observed = batch(rows, "visible_caption_read")
                fallback_observations.update((row["frame_id"], row) for row in observed)
                positive = {row["frame_id"] for row in observed if row["scene"] == "WATCHED_MEDIA"}
                if positive:
                    validation_frames = [row for row in rows if row["frame_id"] in positive]
                    # A discovery can mistake game art for a player. Keep its
                    # independent rejection and inspect unseen frames instead
                    # of retrying those pixels or ending discovery early.
                    scene_validation = batch(validation_frames, "scene_validation", prompt=SCENE_VALIDATION_PROMPT)
                    watched = any(row["scene"] == "WATCHED_MEDIA" for row in scene_validation)
                    if watched:
                        break
                    rejected_discoveries.append(scene_validation)
        negative_scene_validation = None
        negative_scene_confirmed = False
        if not watched and any(row["scene"] == "UNKNOWN" for row in (scene_validation or survey)):
            # Historical discovery prompts can be narrower than scene ownership
            # (for example, only allowing direct gameplay as a negative scene).
            # Keep those answers and independently validate the observed frames
            # with the current ownership question. A remaining UNKNOWN never
            # becomes a negative scene by default or by vote.
            observed_frames = [frame(cue) for cue in cues] if fallback_observations else validation_frames
            negative_scene_validation = []
            for offset in range(0, len(observed_frames), BATCH_SIZE):
                negative_scene_validation.extend(batch(
                    observed_frames[offset:offset + BATCH_SIZE],
                    "scene_ownership_validation", prompt=SCENE_VALIDATION_PROMPT,
                ))
            watched = any(row["scene"] == "WATCHED_MEDIA" for row in negative_scene_validation)
            negative_scene_confirmed = bool(negative_scene_validation) and all(
                row["scene"] == "NOT_WATCHED_MEDIA" for row in negative_scene_validation
            )
        if not watched:
            unresolved_scene = not negative_scene_confirmed and (
                any(row["scene"] == "UNKNOWN" for row in (scene_validation or survey))
                or (fallback_observations and any(row["scene"] == "UNKNOWN" for row in survey))
            )
            if spec.get("watched_media") is True:
                if unresolved_scene:
                    raise CaptionDedupError("CAPTION_OBSERVER_SCENE_UNKNOWN")
                raise CaptionDedupError("CAPTION_OBSERVER_DECLARED_SCENE_CONFLICT")
            # Audio-button scenes are still useful current-frame evidence. Keep
            # their exact controls and sampled frame bindings so the acoustic
            # preparer can offer one cached reference without calling it
            # playback or a source-role decision. The empty caption list keeps
            # the existing display consumer byte-preserving.
            controls_by_frame = {}
            control_batches = [
                *((observed, frames) for observed in survey),
                *((
                    observed,
                    focus_frames if observed.get("frame_id") in focus_frames else frames,
                ) for observed in focus_results),
                *((observed, frames) for observed in (scene_validation or [])),
                *((observed, frames) for observed in (negative_scene_validation or [])),
            ]
            for observed, frame_map in control_batches:
                controls = observed.get("source_controls") if isinstance(observed, Mapping) else None
                frame_row = frame_map.get(observed.get("frame_id")) if isinstance(observed, Mapping) else None
                if controls is None or not isinstance(frame_row, Mapping):
                    continue
                if observed.get("scene") != "NOT_WATCHED_MEDIA":
                    continue
                controls_by_frame[observed["frame_id"]] = {
                    **frame_row, "scene": "NOT_WATCHED_MEDIA", "captions": [],
                    "source_controls": controls,
                }
            controls_rows = list(controls_by_frame.values())
            if controls_rows:
                document = {
                    "schema_version": SCHEMA, "source_role": EXTERNAL_AUDIO_SCENE_SOURCE_ROLE,
                    "policy": POLICY, **binding, "observations": [],
                    "scene_observations": controls_rows,
                    "scene_type": "NOT_WATCHED_MEDIA_EXTERNAL_AUDIO_BUTTON",
                    "display_policy": "NO_CAPTION_DEDUP",
                }
                evidence_bytes = json_bytes(document)
                evidence_sha = _sha(evidence_bytes)
                evidence_path = folder / f"evidence-{evidence_sha}.json"
                if evidence_path.exists():
                    if _file_sha(evidence_path) != evidence_sha:
                        raise CaptionDedupError("CAPTION_OBSERVER_EVIDENCE_DRIFT")
                else:
                    atomic_write_bytes(evidence_path, evidence_bytes)
                effective.update(
                    nested_media_external_audio_scene=True,
                    nested_media_caption_dedup={
                        "policy": POLICY, "evidence_path": str(evidence_path.absolute()),
                        "evidence_sha256": evidence_sha,
                    },
                )
                audit = {
                    **binding, "status": "BOUND_EXTERNAL_AUDIO_UI_OBSERVATIONS", "survey": survey,
                    "cue_frames": len(controls_rows), "observations": 0,
                    "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha,
                    "source_controls_present": True,
                    "whole_scene_identity_proven": False, "speaker_identity_proven": False,
                    "calls_consumed": state["calls_consumed"],
                }
            else:
                audit = {**binding, "status": "SAMPLED_NOT_WATCHED_MEDIA", "survey": survey,
                         "whole_scene_identity_proven": False, "speaker_identity_proven": False,
                         "calls_consumed": state["calls_consumed"]}
            if unresolved_scene:
                # Uncertain pixels do not authorize display deletion or a
                # speaker/scene claim. Keep every transcript cue and let the
                # independent final text/content gates judge the actual bytes.
                # Consume old exact-bound UNKNOWN receipts without retrying
                # the provider or relabeling them as a negative scene.
                unknown_rows = [
                    *survey, *focus_results, *(scene_validation or []),
                    *fallback_observations.values(), *(negative_scene_validation or []),
                ]
                audit.update(
                    status="SCENE_UNRESOLVED_KEEP_ALL", unknown_action="KEEP",
                    display_mutation_authorized=False, observations=0,
                    unknown_cue_indexes=sorted({
                        int(row["frame_id"].removeprefix("cue-"))
                        for row in unknown_rows if row["scene"] == "UNKNOWN"
                    }),
                )
        else:
            observations = []
            scene_observations = []
            unknown_cues = []
            for offset in range(0, len(cues), BATCH_SIZE):
                rows = [frame(cue) for cue in cues[offset:offset + BATCH_SIZE]]
                if fallback_observations:
                    missing = [row for row in rows if row["frame_id"] not in fallback_observations]
                    if missing:
                        fallback_observations.update((row["frame_id"], row) for row in batch(missing, "visible_caption_read"))
                    result = [fallback_observations[row["frame_id"]] for row in rows]
                else:
                    result = batch(rows, "visible_caption_read")
                for observed, row in zip(result, rows, strict=True):
                    # Keep the actual per-frame scene answer, including a
                    # watched player with no visible caption. Acoustic input
                    # preparation needs this evidence, not an inferred role.
                    scene_row = {**row, "scene": observed["scene"],
                                 "captions": list(observed["captions"])}
                    if observed.get("source_controls") is not None:
                        scene_row["source_controls"] = observed["source_controls"]
                    scene_observations.append(scene_row)
                    if observed["scene"] == "UNKNOWN":
                        unknown_cues.append(row["cue_index"])
                        continue  # No observation authorizes deletion of this cue.
                    for n, text in enumerate(observed["captions"]):
                        observations.append({**row, "observation_id": f"{row['frame_id']}-caption-{n}",
                            "visible_text": text, "role": "WATCHED_MEDIA_CAPTION",
                            "pipeline_burned_subtitle": False, "host_repeat_or_overlap": None})
            document = {"schema_version": SCHEMA, "source_role": "WATCHED_MEDIA",
                        "policy": POLICY, **binding, "observations": observations,
                        "scene_observations": scene_observations}
            # A final recut may retain the same SRT while changing media or
            # frames. Do not overwrite the earlier padded-stage receipt.
            evidence_bytes = json_bytes(document)
            evidence_sha = _sha(evidence_bytes)
            evidence_path = folder / f"evidence-{evidence_sha}.json"
            if evidence_path.exists():
                if _file_sha(evidence_path) != evidence_sha:
                    raise CaptionDedupError("CAPTION_OBSERVER_EVIDENCE_DRIFT")
            else:
                atomic_write_bytes(evidence_path, evidence_bytes)
            effective.update(watched_media=True, nested_media_caption_dedup={
                "policy": POLICY, "evidence_path": str(evidence_path.absolute()),
                "evidence_sha256": evidence_sha})
            audit = {**binding, "status": "BOUND_CAPTION_OBSERVATIONS", "survey": survey,
                     "cue_frames": len(cues), "observations": len(observations),
                     "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha,
                     "unknown_cue_indexes": unknown_cues, "unknown_action": "KEEP",
                     "speaker_identity_proven": False, "calls_consumed": state["calls_consumed"]}
        if focus_cue_indexes:
            audit["focus_cue_indexes"] = list(focus_cue_indexes)
            audit["focus_observations"] = focus_results_with_frames
            audit["focus_unknown_cue_indexes"] = [
                index for index, row in zip(focus_cue_indexes, focus_results, strict=True)
                if row["scene"] == "UNKNOWN"
            ]
        audit["replayed_previous_prompt"] = frozen_prompt != PROMPT
        if fallback_observations:
            audit["inconclusive_survey_fallback"] = {
                "frames_read": len(fallback_observations),
                "validation_frame_ids": [row["frame_id"] for row in validation_frames],
                "rejected_discoveries": rejected_discoveries,
                "unknown_action": "KEEP",
            }
        if scene_validation is not None:
            audit["scene_validation"] = scene_validation
            audit["survey_positive_rejected"] = not watched
        if negative_scene_validation is not None:
            audit["scene_ownership_validation"] = negative_scene_validation
            audit["negative_scene_confirmed"] = negative_scene_confirmed
        audit["cpa_resource_pressure"] = {
            "policy": "SOFT_DISTINCT_REQUESTS_CACHE_FIRST",
            "soft_call_target": MAX_CALLS,
            "charged_calls": state["calls_consumed"],
            "over_soft_target": state["calls_consumed"] > MAX_CALLS,
        }
        audit["whole_scene_identity_proven"] = False
        if subtitle_path.read_bytes() != original or _file_sha(source_media) != source_sha:
            raise CaptionDedupError("CAPTION_OBSERVER_INPUT_CHANGED")
        atomic_write_json(folder / "RESULT.json", audit)
        if chat_authority_audit is not None:
            chat_authority_audit["nested_media_caption_observer"] = audit
        return effective


def build_runtime_caption_observer(runtime_root: Path | None = None) -> Callable:
    """Bind only to canonical runtime credentials and candidate-persistent cache."""
    from src.autoslice.cpa_frame_witness import batch_jpeg_vision_probe
    from src.autoslice.llm_client import runtime_cpa_command_environment
    from src.autoslice.producer_final_review_transport import _resolved_runtime_root

    runtime = _resolved_runtime_root(runtime_root)

    def observe(**kwargs):
        if runtime is None:
            raise CaptionDedupError("CAPTION_OBSERVER_RUNTIME_BINDING_REQUIRED")
        env = runtime_cpa_command_environment(runtime)

        def probe(paths, question):
            return batch_jpeg_vision_probe(paths, question, api_base=env.get("CPA_BASE_URL", ""),
                                          api_key=env.get("CPA_API_KEY", ""), model=MODEL)

        return observe_nested_captions(**kwargs, cache_root=runtime / "state" / "caption-observer", probe=probe)

    return observe
