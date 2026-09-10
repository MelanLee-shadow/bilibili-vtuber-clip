"""Replay explicit changes against the original reviewed SRT, never a new ASR.

The recipe and original input are repository-sealed.  The package auditor loads
that recipe independently of fields claimed by a candidate's own record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.operator_correction_policy import plan_operator_correction
from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.subtitle_validation import validate_srt_text

SCHEMA = "operator-fastlane-original-patch.v1"
SUFFIX = ".original-fastlane-patch.v1.json"
ROLE = "OPERATOR_REVIEW_INPUT"
MAX_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HEADER = re.compile(
    r"(?m)^(?P<index>[1-9][0-9]*)\r?\n"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}\r?\n"
)


class OriginalPatchError(ValueError):
    """Original, explicit changes, or release bytes do not match."""


def _need(ok: object, detail: str) -> None:
    if not ok:
        raise OriginalPatchError(detail)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def compile_original_patch(original: bytes, recipe: Mapping[str, Any]) -> tuple[bytes, dict]:
    """Pure replay: preserve every non-target byte, cue number and time header."""
    _need(isinstance(original, bytes) and 0 < len(original) <= MAX_BYTES, "original size/type")
    _need(
        isinstance(recipe, Mapping)
        and set(recipe)
        == {
            "schema_version",
            "candidate_id",
            "original_review_input",
            "operator_directive",
            "patches",
        },
        "recipe fields",
    )
    _need(recipe["schema_version"] == SCHEMA, "recipe schema")
    cid = recipe["candidate_id"]
    _need(isinstance(cid, str) and _ID.fullmatch(cid), "candidate identity")
    binding = recipe["original_review_input"]
    _need(
        isinstance(binding, Mapping)
        and set(binding) == {"path", "sha256", "role", "source_reference"},
        "original binding fields",
    )
    _need(binding["role"] == ROLE and _text(binding["source_reference"]), "original review role")
    _need(isinstance(binding["sha256"], str) and _SHA.fullmatch(binding["sha256"]), "original sha")
    _need(_sha(original) == binding["sha256"], "original review input hash drift")
    directive = recipe["operator_directive"]
    _need(
        isinstance(directive, Mapping)
        and set(directive) == {"kind", "evidence_ref"}
        and directive["kind"] == "REVIEWER_OPERATOR"
        and _text(directive["evidence_ref"]),
        "operator directive",
    )
    changes = recipe["patches"]
    _need(isinstance(changes, list) and len(changes) <= 512, "explicit patch list required")
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OriginalPatchError("original is not UTF-8") from exc
    _need(validate_srt_text(text)["status"] == "PASS", "original SRT structural policy")
    cues = parse_srt_cues(text)
    headers = list(_HEADER.finditer(text))
    _need(len(headers) == len(cues) and headers[0].start() == 0, "unaccounted original SRT bytes")
    by_cue: dict[int, Mapping[str, Any]] = {}
    for row in changes:
        _need(
            isinstance(row, Mapping)
            and set(row)
            == {"cue", "start_ms", "end_ms", "before", "after", "decision_kind", "evidence_ref"},
            "patch fields",
        )
        n = row["cue"]
        _need(type(n) is int and 1 <= n <= len(cues) and n not in by_cue, "duplicate/invalid cue")
        c = cues[n - 1]
        _need(
            type(row["start_ms"]) is int
            and type(row["end_ms"]) is int
            and (row["start_ms"], row["end_ms"]) == (c.start_ms, c.end_ms),
            "patch timing drift",
        )
        _need(
            _text(row["before"]) and _text(row["after"]) and row["before"] != row["after"],
            "patch needs distinct exact before/after text",
        )
        _need(
            row["decision_kind"] in {"REVIEWER_EXACT_CORRECTION", "DELEGATED_CONTEXT_CORRECTION"}
            and _text(row["evidence_ref"]),
            "patch authority must be explicit; not model self-approval",
        )
        by_cue[n] = row
    pieces: list[str] = []
    checked: list[dict] = []
    for pos, (header, cue) in enumerate(zip(headers, cues, strict=True)):
        n = pos + 1
        _need(int(header["index"]) == n, "original numbering drift")
        end = headers[pos + 1].start() if pos + 1 < len(headers) else len(text)
        body = text[header.end() : end]
        content = body.rstrip("\r\n")
        suffix = body[len(content) :]
        row = by_cue.get(n)
        if row is None:
            pieces.append(text[header.start() : end])
            continue
        _need(content == row["before"], f"original cue {n} preimage drift")
        newline = "\r\n" if "\r\n" in header[0] else "\n"
        after = row["after"].replace("\r\n", "\n").replace("\n", newline)
        pieces.append(header[0] + after + suffix)
        checked.append(
            {
                "cue": n,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "before": content,
                "after": after,
                "decision_kind": row["decision_kind"],
                "evidence_ref": row["evidence_ref"],
            }
        )
    result = "".join(pieces).encode("utf-8")
    _need(validate_srt_text(result.decode())["status"] == "PASS", "patched SRT structural policy")
    final_cues = parse_srt_cues(result.decode())
    _need(
        len(final_cues) == len(cues)
        and all(
            (a.start_ms, a.end_ms) == (b.start_ms, b.end_ms)
            for a, b in zip(cues, final_cues, strict=True)
        ),
        "patch changed cue grid",
    )
    return result, {
        "schema_version": "operator-fastlane-original-patch-receipt.v1",
        "candidate_id": cid,
        "original_review_input_sha256": _sha(original),
        "release_srt_sha256": _sha(result),
        "cue_count": len(cues),
        "changed_cues": checked,
        "unchanged_cue_count": len(cues) - len(checked),
        "timing_and_unlisted_bytes_preserved": True,
        "provider_calls": 0,
        "diagnostic_track_executed": False,
        "upload_authorized_by_receipt": False,
        "scope_plan": (
            plan_operator_correction(
                candidate_id=cid, issue_count=len(changes), explicitly_exhaustive=True
            )
            if changes
            else None
        ),
        "change_scope": "TARGETED_ORIGINAL_PATCH" if changes else "UNCHANGED_ORIGINAL_REPLAY",
    }


def _regular(path: Path) -> bytes:
    """Bound read with no symlink components and stable identity (atime excluded)."""
    path = Path(os.path.abspath(path))
    _need(
        not any(p.is_symlink() for p in (path, *path.parents)), "symlink in original authority path"
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        _need(
            stat.S_ISREG(before.st_mode) and before.st_size <= MAX_BYTES,
            "non-regular/oversized authority",
        )
        chunks = []
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            _need(total <= MAX_BYTES, "authority grew beyond size bound")
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)

    def keys(s):
        return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    _need(keys(before) == keys(after) == keys(path.lstat()), "authority changed during read")
    return data


def load_original_patch(
    repo_root: Path, directory: Path, candidate_id: str
) -> tuple[bytes, dict] | None:
    """A candidate's own record cannot switch off a registered original anchor."""
    _need(isinstance(candidate_id, str) and bool(_ID.fullmatch(candidate_id)), "candidate identity")
    path = directory / (candidate_id + SUFFIX)
    if not os.path.lexists(path):
        return None
    raw = _regular(path)
    require_repository_asset_authority(
        repo_root=repo_root, relative_path=path.relative_to(repo_root), observed_bytes=raw
    )
    recipe = json.loads(raw)
    _need(isinstance(recipe, dict), "recipe must be an object")
    _need(recipe.get("candidate_id") == candidate_id, "recipe candidate drift")
    binding = recipe.get("original_review_input", {})
    name = binding.get("path")
    _need(
        isinstance(name, str) and Path(name).name == name and name.endswith(".srt"),
        "original must be sibling SRT",
    )
    source = directory / name
    original = _regular(source)
    require_repository_asset_authority(
        repo_root=repo_root, relative_path=source.relative_to(repo_root), observed_bytes=original
    )
    result, receipt = compile_original_patch(original, recipe)
    receipt["recipe_sha256"] = _sha(raw)
    return result, receipt


def original_release_problems(
    *, repo_root: Path, directory: Path, candidate_id: str, subtitle_path: Path | None
) -> list[str]:
    if not candidate_id:
        return []  # Missing candidate identity is owned by the existing package gate.
    try:
        expected = load_original_patch(repo_root, directory, candidate_id)
        if expected is None:
            # C9 is a source-separated projection, not a generic full-text
            # human pin. Reuse its committed decisions at this same consumer.
            from src.autoslice.fastlane_c9_private_replay import preserved_c9_source_projection

            projected = preserved_c9_source_projection(root=repo_root, candidate_id=candidate_id)
            if projected is None:
                return []
            if subtitle_path is None:
                return ["source-separated release has no final subtitle"]
            if _regular(subtitle_path) != projected:
                return ["final SRT differs from the committed source-separation projection"]
            return []
        if subtitle_path is None:
            return ["original-anchored release has no final subtitle"]
        current = _regular(subtitle_path)
        if current != expected[0]:
            return ["final SRT differs from original reviewed bytes plus explicit approved changes"]
        return []
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"original-review authority invalid: {type(exc).__name__}: {exc}"]


def append_original_release_issues(*, item, is_song, subtitle_path, repo_root,
                                   directory, stem, issues, issue_adder):
    """Keep the canonical package entrypoint bounded without moving its check."""
    if is_song:
        return
    candidate = str(item.get("candidate_id") or item.get("id") or "")
    for detail in original_release_problems(repo_root=repo_root, directory=directory,
                                          candidate_id=candidate, subtitle_path=subtitle_path):
        issue_adder(issues, "OPERATOR_FASTLANE_ORIGINAL_BINDING_INVALID",
                    stem=stem, path=subtitle_path, detail=detail)
