"""CLI parsing, truth isolation, and fail-closed producer preflight."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice.boundary_semantic_review import (
    SEMANTIC_TAIL_TRIM_MAX_MS,
)
from src.autoslice.branding_intro import BrandingIntroError, require_branding_intro
from src.autoslice.producer_boundary import (
    BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS,
    BOUNDARY_REPAIR_EXTEND_CAP_MS,
)
from src.autoslice.producer_media import _resolved_optional_path
from src.autoslice.source_fact_rescore_provenance import (
    PROVENANCE_FIELD,
    SourceFactRescoreProvenanceError,
    validate_rebound_spec_provenance,
)
from src.autoslice.subtitle_regression import load_subtitle_regression_document


@dataclass(frozen=True)
class ProducerRequest:
    spec: dict
    boundary_repair_extend_cap_ms: int
    branding_intro: dict[str, object] | None
    cid: str
    out_root: Path
    host: str
    text_override_path: Path | None
    subtitle_regression_path: Path | None


def parse_producer_args(
    argv: list[str] | None,
    *,
    description: str,
    speaker_display_name: str,
    default_speaker_mode: str,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--spec", type=Path, required=True)
    # 媒体宿主由用户自己决定；默认本机（媒体在别的机器时显式传 --ssh-host）。
    parser.add_argument("--ssh-host", default="localhost")
    parser.add_argument(
        "--substrate",
        choices=("aggregate_asr", "agy_fresh"),
        default="aggregate_asr",
        help="subtitle substrate: aggregate_asr = free ASR (bcut/jianying, accurate ms timeline) + text-only correction (default); agy_fresh = legacy agy whole-window transcription.",
    )
    parser.add_argument(
        "--speaker-mode",
        choices=("uniform_host", "required", "auto"),
        default=default_speaker_mode,
        help=(
            "uniform_host = no speaker separation: every cue keeps the single host "
            f"({speaker_display_name}) style and speaker uncertainty can never reject a delivery "
            "(Ivan 2026-07-13 data-accumulation policy; evidence capture stays passive); "
            "required = always run binary finalizer; auto = verified FAST_SOLO else binary fallback"
        ),
    )
    parser.add_argument("--subtitle-text-overrides", type=Path, help="hash-bound human text decisions applied before speaker inference")
    parser.add_argument(
        "--subtitle-regression",
        type=Path,
        help="candidate-scoped final subtitle truth gate evaluated before burn/delivery",
    )
    parser.add_argument("--speaker-overrides", type=Path, help="hash-bound reviewed turn/split/overlap decisions applied after automatic speaker inference")
    parser.add_argument(
        "--speaker-source-session-anchors",
        type=Path,
        help=(
            "hash-bound high-gate selected-host anchors from the same source recording"
        ),
    )
    parser.add_argument(
        "--speaker-mixed-overlap-evidence",
        type=Path,
        help="hash-bound provider evidence that mixed/overlap cues require review",
    )
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    parser.add_argument(
        "--correct",
        choices=("bcut_agy_cpa", "cpa", "agy", "none"),
        default="bcut_agy_cpa",
        help="correction: bcut_agy_cpa = BCUT draft + AGY refine (hears audio) + CPA reconcile (default, best quality); cpa = CPA text-only (fast, blind to audio); agy = AGY refine only; none = raw BCUT.",
    )
    parser.add_argument(
        "--screen-text",
        action="store_true",
        help="cpa correct only: also feed agy-extracted on-screen text (superchat cards/titles) to CPA. Off by default — the glossary is the reliable authority for known names; agy vision on stylized cards is unreliable and can override the glossary. Use only when a clip's meaning hinges on on-screen text NOT yet in the glossary.",
    )
    parser.add_argument(
        "--reuse-cover",
        action="store_true",
        help="subtitle-only re-run: keep the EXISTING delivered cover, skip the AI cover (art-direction LLM + gpt-image-2 ~90s/clip). Title still regenerates. Use when re-correcting subtitles on an already-covered clip.",
    )
    return parser.parse_args(argv)


def _load_truth_schema_document(path: Path, *, label: str) -> dict:
    """Read one producer truth input without accepting path indirection."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} document must be a JSON object: {path}")
    return document


def _truth_input_paths(
    cli_path: Path | None,
    spec_value: object,
    *,
    relative_to: Path,
) -> tuple[Path | None, Path | None]:
    """Keep the declared path for symlink checks and the existing runtime path."""

    if cli_path is not None:
        return cli_path, cli_path
    resolved = _resolved_optional_path(spec_value, relative_to=relative_to)
    if resolved is None:
        return None, None
    declared = Path(spec_value)  # _resolved_optional_path proved this path-like.
    if not declared.is_absolute():
        declared = relative_to / declared
    return declared, resolved


def _validate_truth_input_schemas(
    *,
    candidate_id: str,
    text_override_path: Path | None,
    subtitle_regression_path: Path | None,
) -> None:
    if text_override_path is not None:
        document = _load_truth_schema_document(
            text_override_path,
            label="text override",
        )
        schema_version = document.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2, 3}:
            raise ValueError(
                "text override schema_version must be 1, 2, or 3; "
                f"got {schema_version!r}"
            )

    if subtitle_regression_path is not None:
        # The existing regression loader is read-only and already owns this
        # asset's schema, candidate binding, and structural validation.
        load_subtitle_regression_document(
            subtitle_regression_path,
            candidate_id=candidate_id,
        )


def load_producer_request(
    args: argparse.Namespace,
    *,
    repo_root: Path,
    profile_asset_file: Callable[[str], Path],
) -> ProducerRequest:
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    repair_cap_raw = spec.get("boundary_repair_extend_cap_ms", BOUNDARY_REPAIR_EXTEND_CAP_MS)
    if isinstance(repair_cap_raw, bool) or not isinstance(repair_cap_raw, int):
        raise ValueError("boundary_repair_extend_cap_ms must be an integer")
    if not BOUNDARY_REPAIR_EXTEND_CAP_MS <= repair_cap_raw <= BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS:
        raise ValueError(
            "boundary_repair_extend_cap_ms must stay within "
            f"{BOUNDARY_REPAIR_EXTEND_CAP_MS}..{BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS}"
        )
    boundary_repair_extend_cap_ms = repair_cap_raw
    tail_trim_cap_raw = spec.get("semantic_tail_trim_cap_ms", 0)
    if (
        isinstance(tail_trim_cap_raw, bool)
        or not isinstance(tail_trim_cap_raw, int)
        or not 0 <= tail_trim_cap_raw <= SEMANTIC_TAIL_TRIM_MAX_MS
    ):
        raise ValueError(
            "semantic_tail_trim_cap_ms must stay within "
            f"0..{SEMANTIC_TAIL_TRIM_MAX_MS}"
        )
    spec["semantic_tail_trim_cap_ms"] = tail_trim_cap_raw
    truth_mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if truth_mode not in {"delivery", "withheld"}:
        raise ValueError("AUTOSLICE_HUMAN_TRUTH_MODE must be delivery or withheld")
    spec_truth_mode = str(spec.get("human_truth_mode") or truth_mode).strip().lower()
    if spec_truth_mode != truth_mode:
        raise ValueError("spec human_truth_mode does not match the process truth-isolation mode")
    if truth_mode == "withheld":
        leaked_inputs = [
            name
            for name, value in (
                ("--subtitle-text-overrides", args.subtitle_text_overrides),
                ("--subtitle-regression", args.subtitle_regression),
                ("--speaker-overrides", args.speaker_overrides),
                ("spec.subtitle_text_overrides", spec.get("subtitle_text_overrides")),
                ("spec.subtitle_regression", spec.get("subtitle_regression")),
                (
                    "spec.subtitle_redelivery_baseline",
                    spec.get("subtitle_redelivery_baseline"),
                ),
                ("spec.speaker_overrides", spec.get("speaker_overrides")),
                (f"spec.{PROVENANCE_FIELD}", spec.get(PROVENANCE_FIELD)),
            )
            if value is not None
        ]
        if leaked_inputs:
            raise ValueError(
                "blind subtitle generation refuses human-truth inputs: " + ", ".join(leaked_inputs)
            )
    if truth_mode == "delivery":
        try:
            rescore_provenance = validate_rebound_spec_provenance(
                spec,
                repo_root=repo_root,
            )
        except SourceFactRescoreProvenanceError as exc:
            raise ValueError(
                f"SOURCE_FACT_SCORECARD_RESCORE_PROVENANCE_INVALID: {exc}"
            ) from exc
        if rescore_provenance is not None:
            # Persist the normalized plain dict instead of an arbitrary Mapping
            # implementation supplied by a caller.
            spec[PROVENANCE_FIELD] = rescore_provenance
    cid = spec["candidate_id"]
    text_override_preflight_path, text_override_path = _truth_input_paths(
        args.subtitle_text_overrides,
        spec.get("subtitle_text_overrides"),
        relative_to=args.spec.parent,
    )
    subtitle_regression_preflight_path, subtitle_regression_path = _truth_input_paths(
        args.subtitle_regression,
        spec.get("subtitle_regression"),
        relative_to=args.spec.parent,
    )
    _validate_truth_input_schemas(
        candidate_id=cid,
        text_override_path=text_override_preflight_path,
        subtitle_regression_path=subtitle_regression_preflight_path,
    )
    try:
        branding_intro = require_branding_intro(
            repo_root,
            manifest_path=profile_asset_file("branding_intro_manifest"),
        )
    except BrandingIntroError as exc:
        raise SystemExit(f"BRANDING_INTRO_UNAVAILABLE: {exc}")
    # Time-sensitive terminology must be evaluated as of the recording date,
    # never the processing date.  This prevents future-news leakage when an old
    # stream is repaired later.
    if isinstance(spec.get("date"), str):
        os.environ["AUTOSLICE_TERM_AS_OF"] = spec["date"]
        os.environ["LIDOUSHA_TERM_AS_OF"] = spec["date"]

    out_root = Path(spec["output_root"]) / cid
    out_root.mkdir(parents=True, exist_ok=True)
    host = args.ssh_host
    return ProducerRequest(
        spec=spec,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
        branding_intro=branding_intro,
        cid=cid,
        out_root=out_root,
        host=host,
        text_override_path=text_override_path,
        subtitle_regression_path=subtitle_regression_path,
    )
