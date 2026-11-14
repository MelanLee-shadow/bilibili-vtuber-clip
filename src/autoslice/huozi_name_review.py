"""Hash-bound human text review (proper names, 维护者 export cues) and timed transcript observations."""

from __future__ import annotations
from typing import TYPE_CHECKING

import json
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Mapping, Sequence, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.huozi_luanshua import CorpusUtterance, MatchPiece


class TimingDelta(TypedDict):
    start: int
    end: int
    maximum_absolute: int


class TimedObservation(TypedDict):
    observed_text: str
    observed_interval_ms: list[int]
    timing_delta_ms: TimingDelta
    utterance_index: int


class NameObservation(TypedDict):
    authority: str
    path: str
    sha256: str
    raw_text: str
    utterance_index: int
    observed_name: str
    name_char_span: list[int]
    name_interval_ms: list[int]
    timeline_offset_ms: int


class _ReviewBinding(TypedDict):
    review_path: str
    review_sha256: str
    source_media_sha256: str
    reviewed_target: str
    written_name: str
    primary_asr_path: str
    primary_utterance_index: int
    source_name_span: list[int]
    name_interval_ms: list[int]
    observations: list[NameObservation]


class NameReviewBinding(_ReviewBinding, total=False):
    reviewer_export_path: str
    reviewer_export_sha256: str
    cue_id: str
    cue_range_ms: list[int]
    excerpt_offset_ms: int


def _find_timed_observation(
    payload: object,
    *,
    expected_text: str,
    core_start_ms: int,
    core_end_ms: int,
    timeline_offset_ms: int,
    timing_tolerance_ms: int,
) -> TimedObservation | None:
    from src.autoslice.huozi_luanshua import normalize_spoken_evidence

    expected = normalize_spoken_evidence(expected_text)
    rows = payload.get("utterances") if isinstance(payload, Mapping) else None
    if not expected or not isinstance(rows, list):
        return None
    candidates: list[tuple[int, int, TimedObservation]] = []
    for utterance_index, raw_utterance in enumerate(rows):
        if not isinstance(raw_utterance, Mapping):
            continue
        raw_words = raw_utterance.get("words")
        if not isinstance(raw_words, list) or not raw_words:
            continue
        tokens: list[tuple[str, int, int, int, int]] = []
        cursor = 0
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            token = normalize_spoken_evidence(
                str(raw_word.get("label") or raw_word.get("text") or "")
            )
            start_ms = int(raw_word.get("start_time", raw_word.get("start_ms", -1)))
            end_ms = int(raw_word.get("end_time", raw_word.get("end_ms", -1)))
            if not token or start_ms < 0 or end_ms < start_ms:
                continue
            tokens.append((token, cursor, cursor + len(token), start_ms, end_ms))
            cursor += len(token)
        joined = "".join(token[0] for token in tokens)
        search_from = 0
        while tokens:
            found = joined.find(expected, search_from)
            if found < 0:
                break
            stop = found + len(expected)
            search_from = found + 1
            first = next((token for token in tokens if token[1] <= found < token[2]), None)
            last = next((token for token in tokens if token[1] < stop <= token[2]), None)
            if first is None or last is None:
                continue
            observed_start = first[3] + timeline_offset_ms
            observed_end = last[4] + timeline_offset_ms
            endpoint_delta = max(
                abs(observed_start - core_start_ms), abs(observed_end - core_end_ms)
            )
            if endpoint_delta > timing_tolerance_ms:
                continue
            total_delta = abs(observed_start - core_start_ms) + abs(observed_end - core_end_ms)
            candidates.append(
                (
                    total_delta,
                    utterance_index,
                    {
                        "observed_text": str(raw_utterance.get("transcript") or joined),
                        "observed_interval_ms": [observed_start, observed_end],
                        "timing_delta_ms": {
                            "start": observed_start - core_start_ms,
                            "end": observed_end - core_end_ms,
                            "maximum_absolute": endpoint_delta,
                        },
                        "utterance_index": utterance_index,
                    },
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row[0], row[1]))[2]


def _review_parts(review: Mapping[str, object], target: str) -> tuple[str, str, str]:
    """Split the reviewed text into unchanged left context, written span and right context."""
    from src.autoslice.huozi_luanshua import CorpusValidationError, normalize_text

    span = review.get("written_char_span")
    if span is None:
        written = normalize_text(str(review.get("proper_name_spelling") or ""))
        if not target or not written or target.count(written) != 1:
            raise CorpusValidationError("proper-name review target does not identify one name")
        left, right = target.split(written)
        return left, written, right
    if (not isinstance(span, list) or len(span) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in span)
            or not 0 <= span[0] < span[1] <= len(target)):
        raise CorpusValidationError("proper-name reviewed written range is malformed")
    written = target[span[0]:span[1]]
    spelling = review.get("proper_name_spelling")
    if spelling is not None and normalize_text(str(spelling)) != written:
        raise CorpusValidationError("proper-name reviewed written range does not match the spelling")
    return target[:span[0]], written, target[span[1]:]


def _check_export_cue(
    review: Mapping[str, object], target: str, source: Mapping[str, object],
    interval: Sequence[int],
) -> dict[str, object]:
    """Bind the review text to 维护者's export cue: file hash, cue id, recording-clock range."""
    from src.autoslice.huozi_luanshua import CorpusValidationError, normalize_text, sha256_file

    keys = ("reviewer_export_path", "reviewer_export_sha256", "cue_id", "cue_range_ms")
    if any(review.get(key) is None for key in keys):
        raise CorpusValidationError("human review export binding is incomplete")
    export = Path(str(review["reviewer_export_path"]))
    if not export.is_absolute():
        raise CorpusValidationError("human review export path must be absolute")
    export = export.resolve()
    export_sha = str(review["reviewer_export_sha256"]).removeprefix("sha256:")
    if not export.is_file() or sha256_file(export) != export_sha:
        raise CorpusValidationError("human review export hash drift")
    labels = json.loads(export.read_text(encoding="utf-8")).get("labels")
    cue_id = str(review["cue_id"])
    cue = labels.get(cue_id) if isinstance(labels, Mapping) else None
    if not isinstance(cue, Mapping) or normalize_text(str(cue.get("text") or "")) != target:
        raise CorpusValidationError("human review cue text mismatch")
    declared = review["cue_range_ms"]
    if (not isinstance(declared, list) or len(declared) != 2
            or declared != [cue.get("abs_start_ms"), cue.get("abs_end_ms")]):
        raise CorpusValidationError("human review cue range mismatch")
    offset = source.get("excerpt_offset_ms")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise CorpusValidationError("human review needs the excerpt recording-clock offset")
    if not (declared[0] <= offset + int(interval[0]) and offset + int(interval[1]) <= declared[1]):
        raise CorpusValidationError("human review spoken interval outside cue range")
    return {"reviewer_export_path": str(export), "reviewer_export_sha256": export_sha,
            "cue_id": cue_id, "cue_range_ms": list(declared), "excerpt_offset_ms": offset}


def _name_span(context: str, left: str, right: str) -> tuple[int, int]:
    """Find one reviewed span between its unchanged surrounding words."""
    from src.autoslice.huozi_luanshua import CorpusValidationError

    starts = [0] if not left else [i + len(left) for i in range(len(context)) if context.startswith(left, i)]
    spans = [(start, end) for start in starts for end in range(start + 1, len(context) + 1)
             if (context.startswith(right, end) if right else end == len(context))]
    if len(spans) != 1:
        raise CorpusValidationError("proper-name review context does not identify one spoken span")
    return spans[0]


def bind_name_review(
    source: object, transcript_evidence: Sequence[object], *, base: Path
) -> NameReviewBinding | None:
    """Bind an existing human review to its original media and two timed ASRs."""
    from src.autoslice.huozi_luanshua import CorpusValidationError, normalize_text, sha256_file

    if not isinstance(source, Mapping):
        raise CorpusValidationError("proper-name source must be an object")
    review_value = source.get("proper_name_review_path")
    if not review_value:
        return None
    path = Path(str(review_value))
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    expected_review_sha = str(source.get("proper_name_review_sha256") or "").removeprefix("sha256:")
    if not path.is_file() or not expected_review_sha or sha256_file(path) != expected_review_sha:
        raise CorpusValidationError("proper-name review hash drift")
    review = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(review, Mapping) or not review.get("authority"):
        raise CorpusValidationError("proper-name review has no human authority")
    media_path = Path(str(source.get("media_path") or ""))
    media_path = (base / media_path).resolve() if not media_path.is_absolute() else media_path.resolve()
    media_sha = str(source.get("media_sha256") or "").removeprefix("sha256:")
    if (not media_path.is_file() or not media_sha or sha256_file(media_path) != media_sha
            or review.get("source_excerpt_sha256") != media_sha):
        raise CorpusValidationError("proper-name review media hash drift")
    target = normalize_text(str(review.get("reviewer_text") or ""))
    left, written, right = _review_parts(review, target)
    primary_path = Path(str(source.get("asr_json_path") or ""))
    primary_path = (base / primary_path).resolve() if not primary_path.is_absolute() else primary_path.resolve()
    reviewed_observations = review.get("observations")
    if reviewed_observations is not None and not isinstance(reviewed_observations, list):
        raise CorpusValidationError("proper-name observations must be a list")
    observations: list[NameObservation] = []
    utterance_spans: dict[str, list[int]] = {}
    for spec in transcript_evidence:
        if not isinstance(spec, Mapping):
            raise CorpusValidationError("proper-name transcript evidence must be an object")
        authority = str(spec.get("authority") or "")
        reviewed = [row for row in reviewed_observations or []
                    if isinstance(row, Mapping) and row.get("authority") == authority]
        if len(reviewed) > 1:
            raise CorpusValidationError("proper-name review repeats an authority")
        observed_sha = str(reviewed[0].get("sha256") or "") if reviewed else str(review.get(f"{authority}_raw_sha256") or "")
        observed_text = str(reviewed[0].get("raw_text") or "") if reviewed else str(review.get(f"{authority}_raw_text") or "")
        if not observed_sha or not observed_text:
            continue
        transcript_path = Path(str(spec.get("path") or "")).resolve()
        if (not transcript_path.is_file() or sha256_file(transcript_path) != observed_sha
                or str(spec.get("sha256") or "").removeprefix("sha256:") != observed_sha):
            raise CorpusValidationError("proper-name review transcript hash drift")
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        rows = payload.get("utterances") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise CorpusValidationError("proper-name review transcript has no timed utterances")
        context = normalize_text(observed_text)
        matching = [(i, row) for i, row in enumerate(rows) if isinstance(row, Mapping)
                    and normalize_text(str(row.get("transcript") or "")) == context]
        if reviewed and "utterance_index" in reviewed[0]:
            matching = [(i, row) for i, row in matching if i == reviewed[0]["utterance_index"]]
        if len(matching) != 1:
            raise CorpusValidationError("proper-name review does not identify one transcript utterance")
        row_index, row = matching[0]
        if reviewed and "name_char_span" in reviewed[0]:
            span = reviewed[0]["name_char_span"]
            if (not isinstance(span, list) or len(span) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in span)):
                raise CorpusValidationError("proper-name reviewed character range is malformed")
            name_start, name_end = span
            if (not 0 <= name_start < name_end <= len(context)
                    or not context[:name_start].endswith(left)
                    or not context[name_end:].startswith(right)):
                raise CorpusValidationError("proper-name reviewed character range does not match context")
        else:
            name_start, name_end = _name_span(context, left, right)
        words = row.get("words")
        if not isinstance(words, list):
            raise CorpusValidationError("proper-name review needs word timing")
        cursor = 0
        joined = ""
        boundaries: dict[int, int] = {}
        ends: dict[int, int] = {}
        for word in words:
            if not isinstance(word, Mapping):
                raise CorpusValidationError("invalid proper-name transcript token")
            token = normalize_text(str(word.get("label") or word.get("text") or ""))
            if not token:
                continue
            start_ms = int(word.get("start_time", word.get("start_ms", -1)))
            end_ms = int(word.get("end_time", word.get("end_ms", -1)))
            if start_ms < 0 or end_ms <= start_ms:
                raise CorpusValidationError("invalid proper-name transcript token timing")
            boundaries[cursor] = start_ms
            joined += token
            cursor += len(token)
            ends[cursor] = end_ms
        if joined != context or name_start not in boundaries or name_end not in ends:
            raise CorpusValidationError("proper-name review does not match timed token boundaries")
        offset = int(spec.get("timeline_offset_ms", 0))
        interval = [boundaries[name_start] + offset, ends[name_end] + offset]
        utterance_spans[str(transcript_path)] = [min(boundaries.values()) + offset, max(ends.values()) + offset]
        if reviewed and "name_interval_ms" in reviewed[0] and reviewed[0]["name_interval_ms"] != interval:
            raise CorpusValidationError("proper-name reviewed range does not match the transcript")
        observations.append({
            "authority": authority, "path": str(transcript_path), "sha256": observed_sha,
            "raw_text": observed_text, "utterance_index": row_index,
            "observed_name": context[name_start:name_end], "name_char_span": [name_start, name_end],
            "name_interval_ms": interval,
            "timeline_offset_ms": offset,
        })
    primary = [row for row in observations if row["path"] == str(primary_path)]
    if len(primary) != 1 or len({row["authority"] for row in observations}) < 2:
        raise CorpusValidationError("proper-name review needs the primary and an independent transcript")
    primary_row = primary[0]
    if int(primary_row["timeline_offset_ms"]) != int(source.get("timeline_offset_ms", 0)):
        raise CorpusValidationError("proper-name primary transcript timeline drift")
    first_range = primary_row["name_interval_ms"]
    for row in observations:
        other_range = row["name_interval_ms"]
        if (max(int(first_range[0]), int(other_range[0])) >= min(int(first_range[1]), int(other_range[1]))
                or max(abs(int(first_range[i]) - int(other_range[i])) for i in (0, 1)) > 1200):
            raise CorpusValidationError("proper-name review spoken ranges do not match")
    binding: NameReviewBinding = {
        "review_path": str(path), "review_sha256": expected_review_sha,
        "source_media_sha256": media_sha, "reviewed_target": target, "written_name": written,
        "primary_asr_path": str(primary_path), "primary_utterance_index": primary_row["utterance_index"],
        "source_name_span": primary_row["name_char_span"],
        "name_interval_ms": primary_row["name_interval_ms"], "observations": observations,
    }
    if any(review.get(key) is not None for key in
           ("reviewer_export_path", "reviewer_export_sha256", "cue_id", "cue_range_ms")):
        binding.update(cast(NameReviewBinding, _check_export_cue(
            review, target, source, utterance_spans[str(primary_path)])))
    return binding


def _rebind(binding: object, *, media_path: str, media_sha: str,
            asr_path: str, evidence: Sequence[object]) -> None:
    from src.autoslice.huozi_luanshua import CorpusValidationError

    if not isinstance(binding, Mapping):
        raise CorpusValidationError("malformed proper-name review binding")
    primary = next((row for row in binding.get("observations", [])
                    if isinstance(row, Mapping) and row.get("path") == asr_path), None)
    if primary is None:
        raise CorpusValidationError("proper-name primary observation is missing")
    current = bind_name_review({
        "proper_name_review_path": binding.get("review_path"),
        "proper_name_review_sha256": binding.get("review_sha256"),
        "media_path": media_path, "media_sha256": media_sha, "asr_json_path": asr_path,
        "timeline_offset_ms": primary.get("timeline_offset_ms"),
        "excerpt_offset_ms": binding.get("excerpt_offset_ms"),
    }, evidence, base=Path.cwd())
    if current != binding:
        raise CorpusValidationError("proper-name review binding or spoken range drift")


def _primary_tokens(binding: NameReviewBinding) -> list[tuple[str, int, int]]:
    from src.autoslice.huozi_luanshua import normalize_text

    primary = next(row for row in binding["observations"] if row["path"] == binding["primary_asr_path"])
    rows = json.loads(Path(str(primary["path"])).read_text(encoding="utf-8"))["utterances"]
    words = rows[int(primary["utterance_index"])]["words"]
    offset = int(primary["timeline_offset_ms"])
    return [(normalize_text(str(word.get("label") or word.get("text") or "")),
             int(word.get("start_time", word.get("start_ms", -1))) + offset,
             int(word.get("end_time", word.get("end_ms", -1))) + offset)
            for word in words if normalize_text(str(word.get("label") or word.get("text") or ""))]


def validate_reviewed_corpus(corpus: list[CorpusUtterance]) -> list[CorpusUtterance]:
    from src.autoslice.huozi_luanshua import CorpusValidationError, normalize_text

    for utterance in corpus:
        for binding in utterance.proper_name_reviews:
            _rebind(binding, media_path=utterance.media_path, media_sha=utterance.media_sha256 or "",
                    asr_path=utterance.source_asr_path, evidence=utterance.transcript_evidence)
            checked = binding
            primary = next(row for row in checked["observations"] if row["path"] == checked["primary_asr_path"])
            if (utterance.utterance_id != f"{utterance.source_id}:u{checked['primary_utterance_index'] + 1:05d}"
                    or utterance.normalized_text != normalize_text(str(primary["raw_text"]))
                    or [(token.normalized, token.start_ms, token.end_ms) for token in utterance.tokens] != _primary_tokens(checked)):
                raise CorpusValidationError("proper-name corpus source or token range drift")
    return corpus


def reviewed_occurrences(utterance: CorpusUtterance, fragment: str) -> Iterator[MatchPiece]:
    from src.autoslice.huozi_luanshua import _occurrences

    yield from _occurrences(utterance, fragment)
    for binding in utterance.proper_name_reviews:
        written = str(binding["written_name"])
        start, end = binding["source_name_span"]
        spoken = utterance.normalized_text[int(start):int(end)]
        for position in range(len(fragment)):
            if not fragment.startswith(written, position):
                continue
            source_fragment = fragment[:position] + spoken + fragment[position + len(written):]
            for piece in _occurrences(utterance, source_fragment):
                if piece.source_char_start + position == int(start):
                    yield replace(piece, display_text=fragment, proper_name_review=binding)


def validate_piece_review(piece: object) -> None:
    from src.autoslice.huozi_luanshua import CorpusValidationError, VerificationError, normalize_text

    if not isinstance(piece, Mapping):
        raise VerificationError("malformed plan piece")
    binding = piece.get("proper_name_review")
    spoken = normalize_text(str(piece.get("text") or ""))
    display = normalize_text(str(piece.get("display_text") or piece.get("text") or ""))
    if binding is None:
        if spoken != display:
            raise VerificationError("display text has no proper-name review")
        return
    if not isinstance(binding, Mapping):
        raise VerificationError("malformed proper-name review")
    specs = piece.get("transcript_evidence")
    if not isinstance(specs, list) or any(not isinstance(row, Mapping) for row in specs):
        raise VerificationError("proper-name transcript evidence is missing")
    try:
        _rebind(binding, media_path=str(piece.get("media_path") or ""),
                media_sha=str(piece.get("media_sha256") or ""),
                asr_path=str(piece.get("source_asr_path") or ""), evidence=specs)
    except (CorpusValidationError, OSError, ValueError) as exc:
        raise VerificationError(str(exc)) from exc
    binding = cast(NameReviewBinding, binding)
    start, end = (int(value) for value in binding["source_name_span"])
    relative = start - int(piece["source_char_start"])
    primary = next(row for row in binding["observations"] if row["path"] == binding["primary_asr_path"])
    name = str(primary["observed_name"])
    interval = binding["name_interval_ms"]
    cursor = 0
    starts, ends = {}, {}
    for token, start_ms, end_ms in _primary_tokens(binding):
        starts[cursor] = start_ms
        cursor += len(token)
        ends[cursor] = end_ms
    piece_start, piece_end = int(piece["source_char_start"]), int(piece["source_char_end"])
    if (relative < 0 or spoken[relative:relative + len(name)] != name
            or end - start != len(name)
            or piece_end - piece_start != len(spoken)
            or starts.get(piece_start) != int(piece["core_start_ms"])
            or ends.get(piece_end) != int(piece["core_end_ms"])
            or display != spoken[:relative] + str(binding["written_name"]) + spoken[relative + len(name):]
            or int(piece["core_start_ms"]) > int(interval[0])
            or int(piece["core_end_ms"]) < int(interval[1])
            or normalize_text(str(piece.get("source_utterance_text") or "")) != normalize_text(str(primary["raw_text"]))
            or piece.get("utterance_id") != f"{piece.get('source_id')}:u{int(binding['primary_utterance_index']) + 1:05d}"):
        raise VerificationError("proper-name reviewed text or spoken range drift")


def expected_for_authority(piece: object, authority: str) -> str:
    if not isinstance(piece, Mapping):
        raise ValueError("malformed plan piece")
    spoken = str(piece.get("text") or "")
    binding = piece.get("proper_name_review")
    if not isinstance(binding, Mapping):
        return spoken
    binding = cast(NameReviewBinding, binding)
    start = int(binding["source_name_span"][0]) - int(piece["source_char_start"])
    rows = binding["observations"]
    primary = next(row for row in rows if row["path"] == binding["primary_asr_path"])
    observed = next((row for row in rows if row["authority"] == authority), None)
    if observed is None:
        return spoken
    length = len(str(primary["observed_name"]))
    return spoken[:start] + str(observed["observed_name"]) + spoken[start + length:]
