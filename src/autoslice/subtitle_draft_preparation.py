"""Deterministic subtitle draft preparation and strict CPA response parsing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from src.autoslice.chat_evidence import normalize_code_switch_surfaces
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.surface_canon import normalize_expected_value_surfaces
from src.autoslice.term_boundary import unify_terms_across_cues


BOUNDARY_SEMANTICS_GUIDANCE = """
【字幕分段也是语义检查，不限词表专名】
ASR每行是时间块，不代表词/短语边界。将相邻2–3条一起读，检查完整人名、普通词、紧密搭配
是否被切开。不要为了完整句把两条合并成长行，也不要把整条后句提前显示；保留原条数和时段。
确实只有断词时，只能把原有的1–2个字（及紧随标点）移到相邻一条，拼接前后全文必须不增字、
不删字、不换字。优先保留原来已合理的句间分隔。时间相隔、说话人变化、明显停顿或需要大幅
重排时保留原分段并披露，不凭语义编造字级时间。词义确有误则走原有词面证据流程，不能把
分段调整伪装成新的听写或全文改写。
"""


def record_asr_boundary_origin(result: dict, media_path: Path) -> tuple[str, dict]:
    """Keep provider's actual utterance/word grid before conversion or correction.

    The normalized client result contains no transport credentials or upload
    URLs. Never manufacture word times when the provider omitted them.
    """
    from scripts.free_asr_client import to_srt

    raw = {key: result[key] for key in ("provider", "elapsed_s", "utterances") if key in result}
    if not isinstance(raw.get("utterances"), list):
        raise ValueError("ASR result has no utterance list")
    payload = (json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = media_path.with_suffix(f".asr-{digest[:16]}.raw.json")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise ValueError("ASR raw evidence path conflict")
    else:
        with path.open("xb") as out:
            os.chmod(path, 0o600)
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
    return to_srt(result), {
        "provider": raw.get("provider", "unknown"),
        "raw_result_path": str(path),
        "raw_result_sha256": digest,
        "raw_utterance_count": len(raw["utterances"]),
        "boundary_origin": "provider_utterance_grid_not_semantic_sentence",
        "word_timings_present": any(u.get("words") for u in raw["utterances"]),
    }


def _rebase_mmss(mmss: str, offset_ms: int) -> str:
    """Shift a probe-relative MM:SS to clip-relative; times before clip start
    come out negative ('-00:05') so the pairing rule still applies to titles
    the streamer starts reading right as the clip opens."""

    parts = mmss.strip().split(":")
    try:
        seconds = int(parts[-2]) * 60 + int(float(parts[-1])) if len(parts) >= 2 else int(float(parts[0]))
    except (ValueError, IndexError):
        return mmss
    rebased = seconds - offset_ms // 1000
    sign = "-" if rebased < 0 else ""
    rebased = abs(rebased)
    return f"{sign}{rebased // 60:02d}:{rebased % 60:02d}"


def _asr_ts(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _required_cpa_cues(prompt: str, cpa_llm_call, cue_count: int, *, resume=None, source_srt: str = "") -> dict[int, str]:
    """A failed or partial review is not a successful zero-edit review."""
    from src.autoslice.llm_client import LlmCallError, extract_json_object
    from src.autoslice.source_context_executor import AgyRunnerError

    def validate(completion):
        items = extract_json_object(completion)["cues"]
        if not isinstance(items, list) or len(items) != cue_count:
            raise ValueError("incomplete cue set")
        corrected = {}
        for item in items:
            if (
                not isinstance(item, dict)
                or type(item.get("n")) is not int
                or not isinstance(item.get("text"), str)
                or item["n"] in corrected
            ):
                raise ValueError("invalid or duplicate cue")
            corrected[item["n"]] = item["text"]
        if set(corrected) != set(range(1, cue_count + 1)):
            raise ValueError("out-of-range cue set")
        if not any(text.strip() for text in corrected.values()):
            raise ValueError("empty reviewed transcript")
        return corrected

    try:
        if resume is not None:
            return resume.review(prompt=prompt, source_srt=source_srt, cue_count=cue_count,
                                 llm_call=cpa_llm_call, validate=validate)
        return validate(cpa_llm_call(prompt))
    except (LlmCallError, ValueError, KeyError, TypeError) as exc:
        raise AgyRunnerError(
            "CPA_CORRECTION_UNAVAILABLE"
            if isinstance(exc, LlmCallError)
            else "CPA_CORRECTION_INVALID_OUTPUT",
            "CPA did not return a complete usable subtitle review",
        ) from exc



def _render_complete_cpa_review(cues, corrected: dict[int, str], draft_srt: str) -> str:
    """Apply the validated complete review to the original timestamp grid."""
    blocks = []
    out_index = 0
    for index, cue in enumerate(cues, start=1):
        raw = corrected.get(index)
        if raw is not None and raw.strip() == "":
            continue  # CPA flagged a hallucination cue → drop
        text = (raw or "").strip() or cue.text
        out_index += 1
        blocks.append(f"{out_index}\n{_asr_ts(cue.start_ms)} --> {_asr_ts(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n" if blocks else draft_srt


def _prepare_cpa_draft(raw_srt: str, term_boundary_surfaces=(), *, blocked_boundaries=()):
    """Keep lossless boundary moves separate from authorized spelling edits."""
    original = parse_srt_cues(raw_srt)
    cues, moves = unify_terms_across_cues(
        original, term_boundary_surfaces, blocked_boundaries=blocked_boundaries
    )
    if "".join(c.text for c in cues) != "".join(c.text for c in original):
        cues, moves = original, []
    boundary_srt = raw_srt
    if moves:
        boundary_srt = (
            "\n\n".join(
                f"{i}\n{_asr_ts(c.start_ms)} --> {_asr_ts(c.end_ms)}\n{c.text}"
                for i, c in enumerate(cues, 1)
            )
            + "\n"
        )
    prepared, surface_audit = normalize_code_switch_surfaces(boundary_srt)
    prepared, expected_audit = normalize_expected_value_surfaces(prepared)
    return (
        boundary_srt,
        prepared,
        {
            "raw_srt_sha256": hashlib.sha256(raw_srt.encode()).hexdigest(),
            "prepared_srt_sha256": hashlib.sha256(prepared.encode()).hexdigest(),
            "term_boundary_moves": moves,
            "blocked_term_boundaries": list(blocked_boundaries),
            "surface_canon": surface_audit,
            "expected_value_canon": expected_audit,
            "authority": "authorized_text_rules_not_independent_audio",
        },
    )


__all__ = ["_prepare_cpa_draft", "_required_cpa_cues"]
