"""Bounded, blind source-frame observation before display-caption filtering."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.cpa_frame_witness import _extract_frame_jpeg
from src.autoslice.nested_media_caption_dedup import POLICY, SCHEMA, CaptionDedupError
from src.autoslice.package_import import atomic_write_bytes, atomic_write_json, exclusive_lock
from src.autoslice.subtitle_audio_correspondence import parse_timed_srt

MODEL = "gpt-6-sol"
BATCH_SIZE = 12
MAX_CALLS = 7  # Legacy name: soft resource target; candidate history never resets.
MAX_CUES = 72
PROMPT = """Observe only the supplied unburned livestream source pixels. Images are in the listed order.
Do not infer words from audio, a transcript, a title or prior knowledge. For each image return its frame_id,
scene (WATCHED_MEDIA, NOT_WATCHED_MEDIA, or UNKNOWN), and captions (a list of verbatim strings).
WATCHED_MEDIA means a separate video is being watched inside the stream. Use the OUTERMOST watched
player rectangle to decide ownership: ALL dialogue captions inside that player count, including large
yellow commentary subtitles and white subtitles in a deeper embedded video. A streamer in the watched
video is still watched-media content. Exclude only the OUTER live stream's captions, chat, gifts, banners,
game UI, and our pipeline captions. Return each distinct caption line inside the player separately.
Preserve incomplete visible text exactly; do not finish
words or invent punctuation/words. If no such caption is visible, captions is []. A scene can be WATCHED_MEDIA
with no caption. When scene identity is unclear use UNKNOWN. This does not identify any acoustic speaker.
Return JSON only: {"frames":[{"frame_id":"...","scene":"...","captions":["..."]}]}.
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CaptionDedupError("CAPTION_OBSERVER_SOURCE_UNSAFE")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise CaptionDedupError("CAPTION_OBSERVER_SOURCE_CHANGED")
    return digest.hexdigest()


def _parse(receipt: Mapping, frame_ids: list[str]) -> list[dict]:
    if receipt.get("status") != "OBSERVED":
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
        return rows
    except (ValueError, TypeError, KeyError) as exc:
        raise CaptionDedupError("CAPTION_OBSERVER_RESPONSE_INVALID") from exc


def observe_nested_captions(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    source_start_ms: int, source_end_ms: int, subtitle_path: Path,
    evidence_root: Path, cache_root: Path, probe: Callable,
    extract_frame: Callable = _extract_frame_jpeg,
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

        def batch(rows, phase):
            question = frozen_prompt + "\n" + json.dumps({"phase": phase, "frame_ids": [r["frame_id"] for r in rows]})
            request = {**binding, "frames": rows, "question": question, "model": MODEL}
            key = _sha(json.dumps(request, sort_keys=True).encode())
            cached = state["batches"].get(key)
            if cached is not None:
                if cached.get("status") != "COMPLETE" or cached.get("request") != request:
                    raise CaptionDedupError("CAPTION_OBSERVER_AMBIGUOUS_OR_FAILED_DISPATCH")
                receipt = cached["receipt"]
                parsed = _parse(receipt, [r["frame_id"] for r in rows])
            else:
                claim = {"status": "DISPATCHED", "request": request}
                state["batches"][key] = claim
                state["calls_consumed"] += 1
                atomic_write_json(state_path, state)  # Charge before external dispatch.
                try:
                    receipt = probe([Path(r["frame_path"]) for r in rows], question)
                    expected = [{"image_path": r["frame_path"], "image_sha256": r["frame_sha256"]} for r in rows]
                    if receipt.get("images") != expected:
                        raise CaptionDedupError("CAPTION_OBSERVER_PROBE_PIXEL_BINDING_MISMATCH")
                    parsed = _parse(receipt, [r["frame_id"] for r in rows])
                    claim.update(status="COMPLETE", receipt=receipt)
                except Exception as exc:
                    claim.update(status="FAILED", reason_code=type(exc).__name__)
                    atomic_write_json(state_path, state)
                    raise
                atomic_write_json(state_path, state)
            return parsed

        indexes = sorted({round(i * (len(cues) - 1) / 5) for i in range(6)})
        survey = batch([frame(cues[i]) for i in indexes], "scene_survey")
        watched = any(row["scene"] == "WATCHED_MEDIA" for row in survey)
        if not watched:
            if any(row["scene"] == "UNKNOWN" for row in survey):
                raise CaptionDedupError("CAPTION_OBSERVER_SCENE_UNKNOWN")
            if spec.get("watched_media") is True:
                raise CaptionDedupError("CAPTION_OBSERVER_DECLARED_SCENE_CONFLICT")
            audit = {**binding, "status": "SAMPLED_NOT_WATCHED_MEDIA", "survey": survey,
                     "whole_scene_identity_proven": False, "speaker_identity_proven": False,
                     "calls_consumed": state["calls_consumed"]}
        else:
            if len(cues) > MAX_CUES:
                raise CaptionDedupError("CAPTION_OBSERVER_CUE_CAP_EXCEEDED")
            observations = []
            unknown_cues = []
            for offset in range(0, len(cues), BATCH_SIZE):
                rows = [frame(cue) for cue in cues[offset:offset + BATCH_SIZE]]
                result = batch(rows, "visible_caption_read")
                for observed, row in zip(result, rows, strict=True):
                    if observed["scene"] == "UNKNOWN":
                        unknown_cues.append(row["cue_index"])
                        continue  # No observation authorizes deletion of this cue.
                    for n, text in enumerate(observed["captions"]):
                        observations.append({**row, "observation_id": f"{row['frame_id']}-caption-{n}",
                            "visible_text": text, "role": "WATCHED_MEDIA_CAPTION",
                            "pipeline_burned_subtitle": False, "host_repeat_or_overlap": None})
            document = {"schema_version": SCHEMA, "source_role": "WATCHED_MEDIA",
                        "policy": POLICY, **binding, "observations": observations}
            evidence_path = folder / f"evidence-{binding['input_transcript_sha256']}.json"
            evidence_sha = atomic_write_json(evidence_path, document)
            effective.update(watched_media=True, nested_media_caption_dedup={
                "policy": POLICY, "evidence_path": str(evidence_path.absolute()),
                "evidence_sha256": evidence_sha})
            audit = {**binding, "status": "BOUND_CAPTION_OBSERVATIONS", "survey": survey,
                     "cue_frames": len(cues), "observations": len(observations),
                     "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha,
                     "unknown_cue_indexes": unknown_cues, "unknown_action": "KEEP",
                     "speaker_identity_proven": False, "calls_consumed": state["calls_consumed"]}
        audit["replayed_previous_prompt"] = frozen_prompt != PROMPT
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
