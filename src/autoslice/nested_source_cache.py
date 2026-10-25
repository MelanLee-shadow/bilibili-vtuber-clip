"""Read an existing Bili source cache without downloading or granting roles."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from src.autoslice.nested_media_acoustic_observer import _verify_file
from src.autoslice.producer_nested_source_locator import (
    _bare_sha256,
    _canonical_sha,
    _extract_search_rows,
    _normal_candidate,
    _normal_text,
)


def _sha(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _document(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SOURCE_CACHE_DOCUMENT_UNAVAILABLE")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("SOURCE_CACHE_DOCUMENT_INVALID")
    return result


def _file(row: object) -> tuple[Path, str]:
    if not isinstance(row, Mapping):
        raise ValueError("SOURCE_CACHE_FILE_BINDING_MISSING")
    return _verify_file(
        row.get("path"), row.get("sha256"),
        unavailable="SOURCE_CACHE_FILE_UNAVAILABLE", mismatch="SOURCE_CACHE_FILE_DRIFT",
    )


def _page_is(value: object, bvid: str) -> bool:
    parsed = urlsplit(str(value or ""))
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"bilibili.com", "www.bilibili.com"}
        and parsed.path.rstrip("/") == f"/video/{bvid}"
        and not parsed.username and not parsed.password
    )


def _caption_hash(value: object) -> str:
    return _canonical_sha(value)


def _caption_hash_matches(value: object, expected: object) -> bool:
    return isinstance(expected, str) and expected == _caption_hash(value)


def _metadata_identity_from_response(
    response: Mapping[str, object], bvid: str,
) -> dict[str, object] | None:
    data = response.get("data")
    data = data if isinstance(data, Mapping) else response
    actual_bvid = str(data.get("bvid") or data.get("BV") or "")
    title = _normal_text(data.get("title") or data.get("name"))
    cid_raw = data.get("cid")
    if cid_raw is None and isinstance(data.get("pages"), list) and data["pages"]:
        first = data["pages"][0]
        if isinstance(first, Mapping):
            cid_raw = first.get("cid")
    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        return None
    try:
        duration = float(data.get("duration") or data.get("length"))
    except (TypeError, ValueError):
        return None
    if (
        actual_bvid != bvid
        or not re.fullmatch(r"BV[0-9A-Za-z]{10}", actual_bvid)
        or not title
        or cid <= 0
        or duration <= 0
    ):
        return None
    result: dict[str, object] = {
        "bvid": bvid,
        "cid": cid,
        "title": title,
        "duration_seconds": int(duration) if duration.is_integer() else round(duration, 3),
    }
    aid = data.get("aid")
    try:
        aid_value = int(aid) if aid is not None else None
    except (TypeError, ValueError):
        aid_value = None
    if aid_value is not None and aid_value > 0:
        result["aid"] = aid_value
    return result


def _cached_metadata_identity(
    metadata: Mapping[str, object], *, bvid: str, cid: int,
) -> dict[str, object] | None:
    """Extract the bounded candidate identity from a verified yt-dlp record."""

    if metadata.get("id") != bvid or metadata.get("extractor_key") != "BiliBili":
        return None
    title = str(metadata.get("title") or "").strip()
    duration_raw = metadata.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        return None
    if not title or duration <= 0:
        return None
    result: dict[str, object] = {
        "bvid": bvid,
        "cid": cid,
        "title": title,
        "duration_seconds": int(duration) if duration.is_integer() else round(duration, 3),
    }
    aid = metadata.get("id_int") or metadata.get("aid")
    try:
        aid_value = int(aid) if aid is not None else None
    except (TypeError, ValueError):
        aid_value = None
    if aid_value is not None and aid_value > 0:
        result["aid"] = aid_value
    return result


def probe_cached_caption_source_candidates(
    *, cache_root: Path, candidates: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate existing source-cache entries in the original search order.

    This is a read-only probe.  A valid local entry is only a metadata and
    media-cache fact; it does not grant source-role authority or make a
    candidate unique.  The caller still obtains current public metadata and
    the normal mapper/acoustic consumers remain separate.
    """

    root = Path(cache_root).resolve()
    result: list[dict[str, object]] = []
    for search_index, candidate in enumerate(candidates):
        bvid = candidate.get("bvid") if isinstance(candidate, Mapping) else None
        probe: dict[str, object] = {
            "search_index": search_index,
            "bvid": bvid,
            "sort_basis": "SOURCE_SEARCH_RESPONSE_ORDER",
            "status": "CACHE_UNAVAILABLE",
            "source_role_authority": False,
            "uniqueness_claim": False,
            "reason_code": "SOURCE_CACHE_ENTRY_NOT_FOUND",
        }
        if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            probe["reason_code"] = "SOURCE_CACHE_IDENTITY_INVALID"
            result.append(probe)
            continue
        candidate_root = root / bvid
        try:
            candidate_root.relative_to(root)
        except ValueError:
            probe["reason_code"] = "SOURCE_CACHE_PATH_UNSAFE"
            result.append(probe)
            continue
        entries = sorted(candidate_root.glob("*/source.json")) if candidate_root.is_dir() else []
        valid_entries: list[tuple[dict[str, object], dict[str, object]]] = []
        for entry_path in entries:
            try:
                entry = _document(entry_path)
                cid = entry.get("cid")
                if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
                    raise ValueError("SOURCE_CACHE_IDENTITY_INVALID")
                cached = _read_cached_source_candidate(
                    cache_root=root,
                    candidate={"bvid": bvid, "cid": cid},
                    locator_candidate_id="caption-search",
                    consumer_candidate_id="caption-search",
                )
                metadata_binding = cached["source_cache_binding"]["metadata"]
                metadata_path = Path(str(metadata_binding["path"]))
                metadata = _document(metadata_path)
                cached_candidate = _cached_metadata_identity(
                    metadata, bvid=bvid, cid=cid,
                )
                if cached_candidate is None:
                    raise ValueError("SOURCE_CACHE_METADATA_IDENTITY_INVALID")
                identity = dict(cached["source_cache_binding"])
                identity["source_json_path"] = str(entry_path)
                identity["source_json_sha256"] = "sha256:" + _sha(entry_path)
                identity["media_sha256"] = cached["independent_source_media_sha256"]
                valid_entries.append((cached_candidate, identity))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                probe["reason_code"] = str(exc)
        if len(valid_entries) == 1:
            cached_candidate, identity = valid_entries[0]
            probe.update(
                {
                    "status": "CACHE_VALIDATED",
                    "reason_code": "SOURCE_CACHE_ENTRY_VERIFIED",
                    "cached_candidate": cached_candidate,
                    "cache_identity": identity,
                }
            )
        elif len(valid_entries) > 1:
            probe["reason_code"] = "SOURCE_CACHE_MULTIPLE_VALID_CIDS"
        result.append(probe)
    return result


def _validate_caption_candidate_discovery(
    *,
    locator: Mapping[str, object],
    source_media: Path,
    subtitle_path: Path,
    source_frame: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], Mapping[str, object], list[Mapping[str, object]]]:
    """Validate current discovery and return its ordered metadata candidates."""

    source_locator = locator
    if source_locator.get("status") != "CANDIDATE_DISCOVERY_ONLY":
        nested = source_locator.get("caption_candidate_discovery")
        if not isinstance(nested, Mapping):
            raise ValueError("CAPTION_DISCOVERY_CURRENT_LOCATOR_REQUIRED")
        source_locator = nested
    locator = source_locator
    if (
        locator.get("status") != "CANDIDATE_DISCOVERY_ONLY"
        or locator.get("action") != "KEEP"
        or locator.get("mutation_authorized") is not False
        or locator.get("source_role_authority") is not False
        or locator.get("candidate_discovery_only") is not True
        or not isinstance(locator.get("candidate_id"), str)
    ):
        raise ValueError("CAPTION_DISCOVERY_CURRENT_LOCATOR_REQUIRED")
    source_sha = _sha(source_media)
    subtitle_sha = _sha(subtitle_path)
    if (
        _bare_sha256(locator.get("source_media_sha256")) != source_sha
        or _bare_sha256(locator.get("final_srt_sha256")) != subtitle_sha
    ):
        raise ValueError("CAPTION_DISCOVERY_CURRENT_INPUT_DRIFT")
    interval = locator.get("source_interval_ms")
    if (
        not isinstance(interval, list)
        or len(interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
        or interval[0] < 0
        or interval[1] <= interval[0]
    ):
        raise ValueError("CAPTION_DISCOVERY_INTERVAL_INVALID")
    frame = locator.get("frame")
    if not isinstance(frame, Mapping):
        raise ValueError("CAPTION_DISCOVERY_FRAME_REQUIRED")
    frame_ms = frame.get("frame_ms")
    if (
        isinstance(frame_ms, bool)
        or not isinstance(frame_ms, int)
        or not interval[0] <= frame_ms <= interval[1]
    ):
        raise ValueError("CAPTION_DISCOVERY_FRAME_TIME_INVALID")
    if source_frame is not None:
        for key in ("frame_ms", "frame_sha256", "frame_path"):
            if key not in source_frame:
                continue
            if key == "frame_sha256":
                equal = _bare_sha256(source_frame.get(key)) == _bare_sha256(frame.get(key))
            else:
                equal = source_frame.get(key) == frame.get(key)
            if not equal:
                raise ValueError("CAPTION_DISCOVERY_FRAME_DRIFT")
    frame_path, frame_sha = _file(
        {"path": frame.get("frame_path"), "sha256": frame.get("frame_sha256")}
    )
    if _bare_sha256(frame_sha) != _bare_sha256(frame.get("frame_sha256")):
        raise ValueError("CAPTION_DISCOVERY_FRAME_DRIFT")
    evidence_path, evidence_sha = _file(
        {
            "path": locator.get("caption_evidence_path"),
            "sha256": locator.get("caption_evidence_sha256"),
        }
    )
    if _bare_sha256(evidence_sha) != _bare_sha256(locator.get("caption_evidence_sha256")):
        raise ValueError("CAPTION_DISCOVERY_EVIDENCE_DRIFT")
    evidence = _document(evidence_path)
    if (
        evidence.get("candidate_id") != locator["candidate_id"]
        or _bare_sha256(evidence.get("source_media_sha256")) != source_sha
        or _bare_sha256(evidence.get("input_transcript_sha256")) != subtitle_sha
        or evidence.get("source_interval_ms") != interval
        or evidence.get("source_role") != "WATCHED_MEDIA"
    ):
        raise ValueError("CAPTION_DISCOVERY_EVIDENCE_BINDING_MISMATCH")

    discovery = locator.get("caption_candidate_discovery")
    if not isinstance(discovery, Mapping):
        raise ValueError("CAPTION_DISCOVERY_PAYLOAD_REQUIRED")
    if (
        discovery.get("caption_frame_ms") != frame.get("frame_ms")
        or _bare_sha256(discovery.get("caption_frame_sha256"))
        != _bare_sha256(frame.get("frame_sha256"))
        or discovery.get("source_role_authority") is not False
        or discovery.get("uniqueness_claim") is not False
    ):
        raise ValueError("CAPTION_DISCOVERY_FRAME_BINDING_MISMATCH")
    rows = evidence.get("observations")
    observation_id = discovery.get("caption_observation_id")
    if not isinstance(rows, list) or not isinstance(observation_id, str):
        raise ValueError("CAPTION_DISCOVERY_OBSERVATION_REQUIRED")
    observations = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("observation_id") == observation_id
        and row.get("frame_ms") == frame.get("frame_ms")
        and _bare_sha256(row.get("frame_sha256"))
        == _bare_sha256(frame.get("frame_sha256"))
        and row.get("role") == "WATCHED_MEDIA_CAPTION"
        and (row.get("caption_present") is None or row.get("caption_present") is True)
        and row.get("pipeline_burned_subtitle") is False
    ]
    if len(observations) != 1:
        raise ValueError("CAPTION_DISCOVERY_OBSERVATION_BINDING_MISMATCH")
    if Path(str(observations[0].get("frame_path"))).resolve() != frame_path:
        raise ValueError("CAPTION_DISCOVERY_FRAME_BINDING_MISMATCH")
    visible_text = str(observations[0].get("visible_text") or "").strip()
    if not visible_text or _bare_sha256(discovery.get("caption_text_sha256")) != _bare_sha256(
        _caption_hash({"visible_text": visible_text})
    ):
        raise ValueError("CAPTION_DISCOVERY_TEXT_BINDING_MISMATCH")

    search_response = discovery.get("search_response")
    if not _caption_hash_matches(search_response, discovery.get("search_response_sha256")):
        raise ValueError("CAPTION_DISCOVERY_SEARCH_RESPONSE_DRIFT")
    normalized = []
    seen: set[str] = set()
    for row in _extract_search_rows(search_response):
        candidate = _normal_candidate(row)
        if candidate is None or str(candidate["bvid"]) in seen:
            continue
        seen.add(str(candidate["bvid"]))
        normalized.append(candidate)
    search_candidates = discovery.get("search_candidates")
    if not isinstance(search_candidates, list) or search_candidates != normalized[:20]:
        raise ValueError("CAPTION_DISCOVERY_SEARCH_BINDING_MISMATCH")

    search_order = discovery.get("search_candidate_order")
    if search_order is not None:
        if not isinstance(search_order, list) or len(search_order) != len(normalized[:20]):
            raise ValueError("CAPTION_DISCOVERY_SEARCH_ORDER_INVALID")
        for index, row in enumerate(search_order):
            if (
                not isinstance(row, Mapping)
                or row.get("search_index") != index
                or row.get("bvid") != normalized[index].get("bvid")
                or row.get("sort_basis") != "SOURCE_SEARCH_RESPONSE_ORDER"
            ):
                raise ValueError("CAPTION_DISCOVERY_SEARCH_ORDER_INVALID")
    cache_probes = discovery.get("cache_candidate_probes")
    if cache_probes is not None:
        if not isinstance(cache_probes, list) or len(cache_probes) != len(normalized[:20]):
            raise ValueError("CAPTION_DISCOVERY_CACHE_PROBE_ORDER_INVALID")
        for index, row in enumerate(cache_probes):
            if (
                not isinstance(row, Mapping)
                or row.get("search_index") != index
                or row.get("bvid") != normalized[index].get("bvid")
                or row.get("sort_basis") != "SOURCE_SEARCH_RESPONSE_ORDER"
                or row.get("source_role_authority") is not False
                or row.get("uniqueness_claim") is not False
            ):
                raise ValueError("CAPTION_DISCOVERY_CACHE_PROBE_ORDER_INVALID")
            if row.get("status") == "CACHE_VALIDATED":
                identity = row.get("cache_identity")
                cached_candidate = row.get("cached_candidate")
                if (
                    not isinstance(identity, Mapping)
                    or not isinstance(cached_candidate, Mapping)
                    or identity.get("bvid") != row.get("bvid")
                    or cached_candidate.get("bvid") != row.get("bvid")
                    or not isinstance(cached_candidate.get("cid"), int)
                ):
                    raise ValueError("CAPTION_DISCOVERY_CACHE_PROBE_BINDING_INVALID")

    metadata_rows = discovery.get("metadata_candidates")
    if not isinstance(metadata_rows, list) or len(metadata_rows) > 3:
        raise ValueError("CAPTION_DISCOVERY_METADATA_BINDING_INVALID")
    checked: list[Mapping[str, object]] = []
    selected_indexes: set[int] = set()
    for index, row in enumerate(metadata_rows):
        if not isinstance(row, Mapping):
            raise ValueError("CAPTION_DISCOVERY_METADATA_BINDING_INVALID")
        search_index = row.get("search_index", index)
        if (
            isinstance(search_index, bool)
            or not isinstance(search_index, int)
            or search_index < 0
            or search_index >= len(normalized)
            or search_index in selected_indexes
        ):
            raise ValueError("CAPTION_DISCOVERY_METADATA_ORDER_DRIFT")
        selected_indexes.add(search_index)
        bvid = row.get("bvid")
        if bvid != normalized[search_index].get("bvid"):
            raise ValueError("CAPTION_DISCOVERY_METADATA_ORDER_DRIFT")
        selection_reason = row.get("selection_reason")
        if selection_reason is not None and selection_reason not in {
            "VERIFIED_CACHE_FIRST", "SEARCH_RESPONSE_ORDER_FILL",
        }:
            raise ValueError("CAPTION_DISCOVERY_METADATA_SELECTION_INVALID")
        cache_validation = row.get("cache_validation")
        if cache_validation is not None:
            if not isinstance(cache_validation, Mapping) or (
                cache_validation.get("status") != "CACHE_VALIDATED"
                or cache_validation.get("bvid") != bvid
            ):
                raise ValueError("CAPTION_DISCOVERY_METADATA_CACHE_BINDING_INVALID")
        if row.get("status") != "METADATA_BOUND":
            continue
        response = row.get("response")
        response_sha = row.get("response_sha256")
        metadata = row.get("metadata")
        if (
            not isinstance(response, Mapping)
            or not _caption_hash_matches(response, response_sha)
            or not isinstance(metadata, Mapping)
            or _metadata_identity_from_response(response, str(bvid)) != dict(metadata)
        ):
            raise ValueError("CAPTION_DISCOVERY_METADATA_RESPONSE_DRIFT")
        checked.append(row)
    return locator, discovery, checked


def read_cached_caption_source_candidate(
    *,
    cache_root: Path,
    locator: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    subtitle_path: Path,
    source_frame: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the first cached public candidate from a bound caption search.

    The current caption discovery, frame, evidence, raw search response and
    metadata responses are validated before any cache entry is inspected.  A
    cache hit is still only a candidate fact: the normal mapper and acoustic
    consumers must establish content and timing independently.
    """

    source_locator, discovery, metadata_rows = _validate_caption_candidate_discovery(
        locator=locator,
        source_media=source_media,
        subtitle_path=subtitle_path,
        source_frame=source_frame,
    )
    attempts: list[dict[str, object]] = []
    for row in metadata_rows:
        candidate = row.get("metadata")
        if not isinstance(candidate, Mapping):
            continue
        bvid = str(candidate.get("bvid") or "")
        try:
            cached = _read_cached_source_candidate(
                cache_root=cache_root,
                candidate=candidate,
                locator_candidate_id=str(source_locator["candidate_id"]),
                consumer_candidate_id=candidate_id,
            )
            cache_validation = row.get("cache_validation")
            if isinstance(cache_validation, Mapping):
                identity = cache_validation.get("cache_identity")
                binding = cached["source_cache_binding"]
                if (
                    not isinstance(identity, Mapping)
                    or identity.get("bvid") != bvid
                    or identity.get("cid") != candidate.get("cid")
                    or identity.get("cache_entry_sha256") != binding.get("cache_entry_sha256")
                    or identity.get("media_sha256") != cached.get("independent_source_media_sha256")
                ):
                    raise ValueError("SOURCE_CACHE_PROBE_BINDING_MISMATCH")
        except ValueError as exc:
            attempts.append(
                {
                    "bvid": bvid,
                    "search_index": row.get("search_index"),
                    "selection_reason": row.get("selection_reason"),
                    "reason_code": str(exc),
                }
            )
            continue
        binding = dict(cached["source_cache_binding"])
        return {
            "schema_version": "nested-media-caption-source-cache-candidate.v1",
            "status": "CANDIDATE_CACHE_AVAILABLE",
            "action": "KEEP",
            "mutation_authorized": False,
            "source_role_authority": False,
            "selection_policy": "FIRST_AVAILABLE_PUBLIC_SEARCH_CANDIDATE",
            "uniqueness_claim": False,
            "locator_candidate_id": source_locator["candidate_id"],
            "consumer_candidate_id": candidate_id,
            "candidate": dict(candidate),
            "search_index": row.get("search_index"),
            "selection_reason": row.get("selection_reason"),
            "cache_validation": dict(row.get("cache_validation", {})) if isinstance(row.get("cache_validation"), Mapping) else None,
            "independent_source_media_path": cached["independent_source_media_path"],
            "independent_source_media_sha256": cached["independent_source_media_sha256"],
            "source_media_sha256": source_locator["source_media_sha256"],
            "final_srt_sha256": source_locator["final_srt_sha256"],
            "source_interval_ms": list(source_locator["source_interval_ms"]),
            "caption_candidate_discovery_request_sha256": source_locator.get("request_sha256"),
            "search_response_sha256": discovery["search_response_sha256"],
            "metadata_response_sha256": row["response_sha256"],
            "candidate_attempts": attempts,
            "source_cache_binding": binding,
        }
    return {
        "schema_version": "nested-media-caption-source-cache-candidate.v1",
        "status": "CANDIDATE_CACHE_UNAVAILABLE",
        "action": "KEEP",
        "mutation_authorized": False,
        "source_role_authority": False,
        "selection_policy": "FIRST_AVAILABLE_PUBLIC_SEARCH_CANDIDATE",
        "uniqueness_claim": False,
        "locator_candidate_id": source_locator["candidate_id"],
        "consumer_candidate_id": candidate_id,
        "source_media_sha256": source_locator["source_media_sha256"],
        "final_srt_sha256": source_locator["final_srt_sha256"],
        "source_interval_ms": list(source_locator["source_interval_ms"]),
        "caption_candidate_discovery_request_sha256": source_locator.get("request_sha256"),
        "candidate_attempts": attempts,
        "reason_code": "SOURCE_CACHE_NO_AVAILABLE_PUBLIC_CANDIDATE",
    }


def read_cached_nested_source(
    *, cache_root: Path, locator: Mapping[str, object], candidate_id: str,
    source_media: Path, subtitle_path: Path,
) -> dict[str, object]:
    """Resolve BVID/CID only when current locator and download bytes agree.

    The cache contains source.json under BVID/CID, plus unchanged download
    receipt, yt-dlp metadata and log. A relocated media file is allowed only
    when its actual bytes match the original download receipt. Mapping,
    current cue/frame and acoustic authority remain separate consumers.
    """
    if (
        locator.get("status") != "EVIDENCE_ONLY"
        or locator.get("reason_code") != "PUBLIC_SOURCE_CANDIDATE_BOUND"
        or locator.get("action") != "KEEP"
        or not isinstance(locator.get("candidate_id"), str)
        or str(locator.get("source_media_sha256")).removeprefix("sha256:") != _sha(source_media)
        or str(locator.get("final_srt_sha256")).removeprefix("sha256:") != _sha(subtitle_path)
    ):
        raise ValueError("SOURCE_CACHE_CURRENT_LOCATOR_REQUIRED")
    candidate = locator.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("SOURCE_CACHE_CANDIDATE_REQUIRED")
    return _read_cached_source_candidate(
        cache_root=cache_root,
        candidate=candidate,
        locator_candidate_id=locator["candidate_id"],
        consumer_candidate_id=candidate_id,
    )


def _read_cached_source_candidate(
    *,
    cache_root: Path,
    candidate: Mapping[str, object],
    locator_candidate_id: str,
    consumer_candidate_id: str,
) -> dict[str, object]:
    """Read one existing cache entry after the caller bound current inputs."""

    bvid, cid = candidate.get("bvid"), candidate.get("cid")
    if (
        not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
        or isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0
    ):
        raise ValueError("SOURCE_CACHE_IDENTITY_INVALID")
    path = Path(cache_root).resolve() / bvid / str(cid) / "source.json"
    if path.resolve().relative_to(Path(cache_root).resolve()) != Path(bvid) / str(cid) / "source.json":
        raise ValueError("SOURCE_CACHE_PATH_UNSAFE")
    entry = _document(path)
    if entry.get("bvid") != bvid or entry.get("cid") != cid or not _page_is(entry.get("webpage_url"), bvid):
        raise ValueError("SOURCE_CACHE_IDENTITY_MISMATCH")
    media, media_sha = _file(entry.get("media"))
    receipt_path, receipt_sha = _file(entry.get("download_receipt"))
    metadata_path, metadata_sha = _file(entry.get("metadata"))
    log_path, log_sha = _file(entry.get("download_log"))
    receipt, metadata = _document(receipt_path), _document(metadata_path)
    original_media, original_info = receipt.get("media"), receipt.get("info")
    if (
        receipt.get("status") != "PASS_EXACT_BVID_DOWNLOADED"
        or receipt.get("bvid") != bvid
        or not _page_is(receipt.get("webpage_url"), bvid)
        or not isinstance(original_media, Mapping) or not isinstance(original_info, Mapping)
        or str(original_media.get("sha256")).removeprefix("sha256:") != media_sha.removeprefix("sha256:")
        or original_media.get("bytes") != media.stat().st_size
        or entry["media"].get("bytes") != media.stat().st_size
        or str(original_info.get("sha256")).removeprefix("sha256:") != metadata_sha.removeprefix("sha256:")
        or original_info.get("bytes") != metadata_path.stat().st_size
        or metadata.get("id") != bvid or metadata.get("extractor_key") != "BiliBili"
        or not _page_is(metadata.get("webpage_url"), bvid)
    ):
        raise ValueError("SOURCE_CACHE_DOWNLOAD_BINDING_MISMATCH")
    # Use the downloader's unmodified page-part extraction, not a CID written
    # later into a mapping or an audio witness receipt.
    part_line = rf"(?m)^\[BiliBili\] {re.escape(bvid)}: Downloading video formats for cid {cid}\s*$"
    if not re.search(part_line, log_path.read_text(encoding="utf-8")):
        raise ValueError("SOURCE_CACHE_DOWNLOADED_CID_MISMATCH")
    return {
        "independent_source_media_path": str(media),
        "independent_source_media_sha256": media_sha,
        "source_cache_binding": {
            # Identity discovery is a source fact. A later cue consumer may
            # have its own ID while using exactly the same media/SRT bytes;
            # retain both IDs without relabeling the earlier locator receipt.
            "locator_candidate_id": locator_candidate_id,
            "consumer_candidate_id": consumer_candidate_id,
            "bvid": bvid, "cid": cid, "webpage_url": entry["webpage_url"],
            "cache_entry_path": str(path), "cache_entry_sha256": "sha256:" + _sha(path),
            "download_receipt": {"path": str(receipt_path), "sha256": receipt_sha},
            "metadata": {"path": str(metadata_path), "sha256": metadata_sha},
            "download_log": {"path": str(log_path), "sha256": log_sha},
            "source_role_authority": False,
        },
    }
