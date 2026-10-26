"""Final-subtitle quote and candidate checks; no media, ASR or title-as-source."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from .channel_envelope import (
    accepted_talk_title_prefix,
    render_talk_title,
    talk_title_body,
)

PUNCT = re.compile(r"""[\s，。！？、；：,.!?;:“”"'‘’「」『』（）()《》〈〉…—\-~～·]""")
QUOTES = re.compile(r'“([^”]+)”|"([^"]+)"|「([^」]+)」|『([^』]+)』|‘([^’]+)’')


def canonical(text: str) -> str:
    return PUNCT.sub("", text)


def final_cues(srt: str) -> list[dict]:
    rows = []
    for block in re.split(r"\n\s*\n", srt.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        text = " ".join(lines[2:])
        speaker = ""
        match = re.match(r"\[([^\]]+)\]\s*(.*)", text)
        if match:
            speaker, text = match.groups()
        rows.append(
            {
                "cue": lines[0],
                "time": lines[1].split(" --> ")[0],
                "timing": lines[1],
                "speaker": speaker,
                "text": text,
            }
        )
    return rows


def aligned_quote_subtitles(record: Mapping, final_srt: str) -> str:
    """Accept an existing speaker SRT only when every final cue still matches."""
    if not final_cues(final_srt):
        raise ValueError("TITLE_FINAL_SUBTITLES_REQUIRED")
    raw_path = record.get("speaker_review_srt_path")
    if not raw_path:
        return final_srt
    path = Path(str(raw_path))
    if path.is_symlink() or not path.is_file():
        raise ValueError("TITLE_SPEAKER_SUBTITLES_MISSING")
    speaker_text = path.read_text(encoding="utf-8")

    def identity(text):
        return [(r["cue"], r["timing"], r["text"]) for r in final_cues(text)]

    if identity(speaker_text) != identity(final_srt):
        raise ValueError("TITLE_SPEAKER_SUBTITLES_NOT_ALIGNED")
    return speaker_text


def _destutter(text: str) -> str:
    # Only explicitly separated repetitions; do not collapse 人人 or 好好学习.
    return re.sub(r"([^\s，,、。]{1,4})(?:[\s，,、]+\1)+", r"\1", text)


def find_quote(quote: str, srt: str, *, speaker: str = "") -> dict | None:
    needle = canonical(quote)
    if not needle:
        return None
    cues = final_cues(srt)
    for size in range(1, 5):
        for start in range(len(cues) - size + 1):
            window = cues[start : start + size]
            speakers = {row["speaker"] for row in window}
            if len(speakers - {""}) > 1:
                continue
            if speaker and speakers != {speaker}:
                continue
            original = "".join(canonical(row["text"]) for row in window)
            destuttered = canonical(_destutter(" ".join(row["text"] for row in window)))
            if needle in original or needle in destuttered:
                known = next(iter(speakers)) if len(speakers) == 1 else ""
                return {
                    "cues": [r["cue"] for r in window],
                    "time": window[0]["time"],
                    "speaker": known,
                    "speaker_status": "VERIFIED_SUBTITLE" if known else "UNKNOWN",
                    "quote": quote,
                    "match": "VERBATIM" if needle in original else "STUTTER_REMOVED",
                }
    return None


def check_title_quotes(title: str, final_srt: str, *, declared_quote: object = None) -> dict:
    phrases = [next(v for v in match if v) for match in QUOTES.findall(title)]
    speaker = ""
    if isinstance(declared_quote, Mapping):
        speaker = str(declared_quote.get("speaker") or "").strip()
        declared_quote = declared_quote.get("text")
    if isinstance(declared_quote, str) and declared_quote.strip():
        if canonical(declared_quote) not in canonical(title):
            phrases.append("")
        elif declared_quote not in phrases:
            phrases.append(declared_quote)
    errors = []
    if not final_cues(final_srt):
        errors.append("TITLE_FINAL_SUBTITLES_REQUIRED")
    if not phrases:
        errors.append("TITLE_VERBATIM_QUOTE_REQUIRED")
    for left, right in (("“", "”"), ("「", "」"), ("『", "』"), ("‘", "’")):
        if title.count(left) != title.count(right):
            errors.append("TITLE_QUOTE_UNBALANCED")
    if title.count('"') % 2:
        errors.append("TITLE_QUOTE_UNBALANCED")
    matches = []
    body = talk_title_body(title)
    for phrase in phrases:
        # A declared speaker is a claim, never authority. Explicit attribution
        # beside a quote also requires the final subtitle to identify that speaker.
        attributed = speaker
        for label in ("李豆沙", "kmx", "KMX"):
            if re.search(
                re.escape(label)
                + r"(?:说|回|喊|来|一句|当场说|表示)*[：:\s]*[“\"「『‘]?"
                + re.escape(phrase),
                body,
            ):
                attributed = label.lower() if label.upper() == "KMX" else label
        match = find_quote(phrase, final_srt, speaker=attributed)
        matches.append(match)
        if attributed and match is None and find_quote(phrase, final_srt) is not None:
            errors.append("TITLE_QUOTE_SPEAKER_UNVERIFIED")
    if any(match is None for match in matches):
        errors.append("TITLE_QUOTE_NOT_IN_FINAL_SUBTITLES")
    return {
        "schema_version": "lidousha-title-quote-check.v1",
        "status": "PASS" if not errors else "FAIL",
        "reason_codes": sorted(set(errors)),
        "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
        "final_srt_sha256": hashlib.sha256(final_srt.encode()).hexdigest(),
        "matches": matches,
        "audio_calls": 0,
    }


def check_title_candidates(payload: Mapping, final_srt: str) -> dict:
    """Validate every compared candidate and its actual quoted text."""
    from . import title_policy

    rows = payload.get("candidates")
    errors = []
    receipts = []
    if not isinstance(rows, list) or not (
        title_policy._TITLE_MIN_CANDIDATES <= len(rows) <= title_policy._TITLE_MAX_CANDIDATES
    ):
        errors.append(
            f"TITLE_CANDIDATES_REQUIRE_{title_policy._TITLE_MIN_CANDIDATES}_TO_{title_policy._TITLE_MAX_CANDIDATES}"
        )
        rows = []
    titles = []
    for row in rows:
        if not isinstance(row, Mapping):
            errors.append("TITLE_CANDIDATE_INVALID")
            continue
        title = str(row.get("title") or "").strip()
        titles.append(title)
        full_title = title if accepted_talk_title_prefix(title) is not None else render_talk_title(title)
        quote = check_title_quotes(full_title, final_srt, declared_quote=row.get("quote"))
        if not title_policy._TITLE_MIN_LEN <= len(full_title) <= title_policy._TITLE_MAX_LEN:
            errors.append("TITLE_CANDIDATE_LENGTH_OR_LANE_INVALID")
        if (
            row.get("status") not in {"SELECTED", "REJECTED"}
            or not str(row.get("rationale") or "").strip()
        ):
            errors.append("TITLE_CANDIDATE_COMPARISON_REASON_REQUIRED")
        errors.extend(quote["reason_codes"])
        receipts.append(
            {
                "title": title,
                "quote": row.get("quote"),
                "quote_check": quote,
                "status": row.get("status"),
                "rationale": row.get("rationale"),
            }
        )
    if len(set(titles)) != len(titles):
        errors.append("TITLE_CANDIDATES_DUPLICATE")
    selected = [row for row in receipts if row["status"] == "SELECTED"]
    choice = payload.get("editorial_choice")
    if (
        len(selected) != 1
        or not isinstance(choice, Mapping)
        or selected[0]["title"] != payload.get("title")
        or choice.get("selected") != payload.get("title")
        or not str(choice.get("reason") or "").strip()
    ):
        errors.append("TITLE_CANDIDATE_SELECTION_MISMATCH")
    return {
        "schema_version": "lidousha-title-candidates.v1",
        "status": "PASS" if not errors else "FAIL",
        "reason_codes": sorted(set(errors)),
        "candidates": receipts,
        "editorial_choice": choice,
        "selected_quote": selected[0]["quote"] if len(selected) == 1 else None,
        "final_srt_sha256": hashlib.sha256(final_srt.encode()).hexdigest(),
        "audio_calls": 0,
    }


def final_title_skill_receipt(
    title: str, final_srt: str, quote_srt: str, attempts: list[dict]
) -> dict:
    from .title_policy import canonicalize_automatic_title_fillers

    def same_title(row: Mapping) -> bool:
        # The publish choke may only strip profile-declared filler words.
        attempted = canonicalize_automatic_title_fillers(
            talk_title_body(str(row.get("title") or ""))
        )
        return canonical(attempted) == canonical(talk_title_body(title))

    accepted = [row for row in attempts if row.get("status") == "ACCEPTED"]
    if not accepted and attempts:
        # Publish-choke deterministic filler removal repairs the last rejected
        # attempt; it keeps that attempt's own candidate receipt only when the
        # staged title is exactly its filler-stripped title.
        last_attempt = attempts[-1]
        if last_attempt.get("status") == "REJECTED_POLICY" and same_title(last_attempt):
            accepted = [last_attempt]
    last = accepted[-1] if accepted else {}
    skill = last.get("title_cover_skill") or {}
    # 60-title: a fact-repaired final title re-verifies its quote.  The selected
    # candidate's declaration is carried forward as a claim only;
    # check_title_quotes still requires it verbatim in this title and in the
    # final subtitles, so a repair that dropped or changed it fails closed.
    declaration = skill.get("selected_quote") if last else None
    checked = check_title_quotes(title, quote_srt, declared_quote=declaration)
    if skill.get("status") != "PASS":
        checked["status"] = "FAIL"
        checked["reason_codes"].append("TITLE_CANDIDATE_SELECTION_NOT_ACCEPTED")
    return {
        "schema_version": "lidousha-final-title-skill.v1",
        "fact_source": "FINAL_SUBTITLES_ONLY",
        "final_srt_sha256": hashlib.sha256(final_srt.encode()).hexdigest(),
        "quote_srt_sha256": hashlib.sha256(quote_srt.encode()).hexdigest(),
        "quote_check": checked,
        "declared_quote": declaration,
        "audio_calls": 0,
    }


def check_cover_quotes(lines: list[str], final_srt: str) -> list[dict]:
    result = []
    for line in lines:
        body = re.split(r"[：:]", line, maxsplit=1)[-1]
        match = find_quote(body, final_srt)
        quoted = any(QUOTES.findall(line))
        result.append(
            {
                "line": line,
                "status": "VERBATIM" if match else "MISSING_QUOTE" if quoted else "PARAPHRASE",
                "match": match,
                "semantic_review_required": not bool(match),
            }
        )
    return result


def validate_title_skill_selection(record: Mapping, staging: Mapping) -> None:
    """Replay persisted automatic titles from actual final subtitle bytes."""
    from .llm_client import extract_json_object

    selection = staging.get("title_selection") or {}
    declared = selection.get("title_cover_skill")
    generation = staging.get("cover_generation") or {}
    required = generation.get("method") == "skill.candidates" and str(
        staging.get("title_source") or ""
    ).startswith("llm")
    if declared is None and not required:
        return
    if not isinstance(declared, Mapping):
        raise ValueError("TITLE_FINAL_SKILL_RECEIPT_MISSING")
    path = Path(str(record.get("subtitle_path") or ""))
    if path.is_symlink() or not path.is_file():
        raise ValueError("TITLE_FINAL_SUBTITLES_REQUIRED")
    final_srt = path.read_text(encoding="utf-8")
    quote_srt = aligned_quote_subtitles(record, final_srt)
    attempts = staging.get("title_generation_attempts") or []
    for attempt in attempts:
        accepted = attempt.get("status") == "ACCEPTED"
        # Every stored candidate receipt is recomputed: a rejected attempt's
        # receipt can carry a publish-choke filler repair, so it must not be
        # trusted from stored bytes either.
        if not accepted and attempt.get("title_cover_skill") is None:
            continue
        expected = check_title_candidates(extract_json_object(attempt["response_text"]), quote_srt)
        if expected != attempt.get("title_cover_skill") or (accepted and expected["status"] != "PASS"):
            raise ValueError("TITLE_CANDIDATE_QUOTE_RECEIPT_DRIFT")
    actual = final_title_skill_receipt(
        str(staging.get("title") or ""), final_srt, quote_srt, attempts
    )
    if actual != declared:
        raise ValueError("TITLE_FINAL_SKILL_RECEIPT_DRIFT")
    if actual["quote_check"]["status"] != "PASS" and not staging.get("title_authority_error"):
        raise ValueError("TITLE_FINAL_QUOTE_CHECK_FAILED")
