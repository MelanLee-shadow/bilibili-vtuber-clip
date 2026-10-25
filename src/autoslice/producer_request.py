"""CLI parsing, truth isolation, and fail-closed producer preflight."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from src.autoslice.channel_profile import (
    ChannelProfile, ChannelProfileError, binding_for_profile,
    load_channel_profile, normalize_profile_binding, require_profile_binding,
)

from src.autoslice.boundary_semantic_review import (
    SEMANTIC_TAIL_TRIM_MAX_MS,
)
from src.autoslice.branding_intro import BrandingIntroError, require_branding_intro
from src.autoslice.producer_boundary import (
    BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS,
    BOUNDARY_REPAIR_EXTEND_CAP_MS,
)
from src.autoslice.producer_media import _resolved_optional_path
from src.autoslice.reviewed_exact_source_interval import (
    RUNTIME_CONFIG_KEY as EXACT_INTERVAL_RUNTIME_CONFIG_KEY,
)
from src.autoslice.source_fact_rescore_provenance import (
    PROVENANCE_FIELD,
    SourceFactRescoreProvenanceError,
    validate_rebound_spec_provenance,
)
from src.autoslice.subtitle_regression import load_subtitle_regression_document
from src.autoslice.subtitle_text_override_schema import (
    validate_text_override_document_header,
)
from src.autoslice.supplement_audio_budget import (
    BUDGET_CONFIG_INVALID,
    BudgetConfigurationError,
    validate_budget_config,
)


_FOREIGN_SCRIPT_WITNESS_PROVIDERS = frozenset({"agy", "moss", "mai"})


class ProducerRuntimePrerequisiteError(ValueError):
    """A selected execution dependency is unavailable, rather than bad content."""

    def __init__(self, dependency: str, detail: str):
        self.dependency = dependency
        super().__init__(f"RUNTIME_PREREQUISITE: {dependency}: {detail}")


def resolve_runtime_base(value: str | Path) -> Path:
    """Pin the configured root without reinterpreting it in a child's cwd."""
    raw = str(value).strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        raise ProducerRuntimePrerequisiteError("AUTOSLICE_BASE", "must be an absolute path")
    # Do not resolve symlink ancestors: the delivery authority check must see them.
    return Path(os.path.abspath(path))


def validate_producer_runtime_prerequisites(
    *,
    host: str = "localhost",
    speaker_mode: str = "uniform_host",
    speaker_python: str | Path | None = None,
    lane: str = "talk",
    host_vocal_python: str | Path | None = None,
    check_media: bool = True,
) -> None:
    """Check only executables the selected branch will actually launch.

    AGY is deliberately absent here: its client has a supported provider fallback.
    Auto speaker routing checks its Python when it actually selects binary fallback.
    Remote Python paths cannot be tested against the controller's filesystem.
    """
    commands = ["ffmpeg", "ffprobe"] if check_media else []
    local = host in {"localhost", "127.0.0.1", "::1"}
    if not local:
        commands.extend(("ssh", "scp"))
    for command in commands:
        if shutil.which(command) is None:
            raise ProducerRuntimePrerequisiteError(command, "executable is unavailable")
    python = None
    dependency = "speaker_python"
    if local and lane == "song":
        python = host_vocal_python
        dependency = "host_vocal_python"
    elif local and speaker_mode == "required":
        python = speaker_python
    if local and (lane == "song" or speaker_mode == "required"):
        if python is None:
            root = resolve_runtime_base(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
            env_name = "AUTOSLICE_HOST_VOCAL_PYTHON" if lane == "song" else "AUTOSLICE_SPEAKER_PYTHON"
            python = os.environ.get(env_name, str(root / "venv-diar/bin/python"))
        raw = str(python).strip()
        path = Path(raw).expanduser()
        if not raw or (len(path.parts) > 1 and not path.is_absolute()):
            raise ProducerRuntimePrerequisiteError(dependency, "must be an absolute path or PATH command")
        if shutil.which(str(path)) is None:
            raise ProducerRuntimePrerequisiteError(dependency, "executable is unavailable")


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
    profile_binding: dict[str, str]


def parse_producer_args(
    argv: list[str] | None,
    *,
    description: str,
    speaker_display_name: str,
    default_speaker_mode: str,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    configured_correction = os.environ.get("AUTOSLICE_CORRECTION_MODE", "cpa")
    if configured_correction not in ("bcut_agy_cpa", "moss_cpa", "cpa"):
        parser.error("AUTOSLICE_CORRECTION_MODE must retain CPA: bcut_agy_cpa, moss_cpa or cpa")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--resume-prepared", type=Path,
                        help="resume a sealed private Talk prepare at its completed finalization stages")
    parser.add_argument("--resume-spec-sha256", help="exact original spec SHA256 for prepared recovery")
    parser.add_argument("--resume-output-root", type=Path,
                        help="separate private successor output root; original prepare remains read-only")
    parser.add_argument("--resume-cover-failure", type=Path,
                        help="cover-only successor from a hash-bound native failure; requires --resume-prepared")
    parser.add_argument("--resume-cover-failure-sha256")
    parser.add_argument("--resume-cover-prepare-qc", action="store_true",
                        help="render retained cover and prepare the real identity comparison without calling its provider")
    # 媒体宿主由用户自己决定；默认本机（媒体在别的机器时显式传 --ssh-host）。
    parser.add_argument("--ssh-host", default="localhost")
    parser.add_argument(
        "--substrate",
        choices=("aggregate_asr", "agy_fresh", "existing_srt"),
        default="aggregate_asr",
        help="subtitle substrate: aggregate_asr = free ASR (bcut/jianying, accurate ms timeline) + text-only correction (default); agy_fresh = legacy agy whole-window transcription; existing_srt = hash-bound padded-timeline subtitle input with no ASR.",
    )
    parser.add_argument(
        "--speaker-mode",
        choices=("uniform_host", "required", "auto"),
        default=default_speaker_mode,
        help=(
            "uniform_host = no speaker separation: every cue keeps the single host "
 f"({speaker_display_name}) style and speaker uncertainty can never reject a delivery "
 "(公开规则data-accumulation policy; evidence capture stays passive); "
 "required = always run binary finalizer; auto = verified FAST_SOLO else binary fallback"
        ),
    )
    parser.add_argument(
        "--subtitle-text-overrides",
        type=Path,
        help="hash-bound human text decisions applied before speaker inference",
    )
    parser.add_argument(
        "--subtitle-regression",
        type=Path,
        help="candidate-scoped final subtitle truth gate evaluated before burn/delivery",
    )
    parser.add_argument(
        "--speaker-overrides",
        type=Path,
        help="hash-bound reviewed turn/split/overlap decisions applied after automatic speaker inference",
    )
    parser.add_argument(
        "--speaker-source-session-anchors",
        type=Path,
        help=("hash-bound high-gate selected-host anchors from the same source recording"),
    )
    parser.add_argument(
        "--speaker-mixed-overlap-evidence",
        type=Path,
        help="hash-bound provider evidence that mixed/overlap cues require review",
    )
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(
            os.environ.get(
                "AUTOSLICE_SPEAKER_PYTHON",
                str(Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")) / "venv-diar/bin/python"),
            )
        ),
    )
    parser.add_argument(
        "--correct",
        choices=("bcut_agy_cpa", "moss_cpa", "cpa", "agy", "none"),
        default=configured_correction,
        help="correction: cpa = BCUT + mandatory CPA (default; local audio checks remain); moss_cpa = MOSS Pro + mandatory CPA (candidate); bcut_agy_cpa = legacy BCUT + whole-clip AGY + mandatory CPA; agy/none = diagnostic only.",
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
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "build and hash-seal a candidate-private delivery handle without "
            "writing public delivery targets; the runner commits it later"
        ),
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
        validate_text_override_document_header(
            document,
            expected_candidate_id=candidate_id,
        )

    if subtitle_regression_path is not None:
        # The existing regression loader is read-only and already owns this
        # asset's schema, candidate binding, and structural validation.
        load_subtitle_regression_document(
            subtitle_regression_path,
            candidate_id=candidate_id,
        )


def _validate_foreign_script_witness_provider(spec: dict) -> None:
    for field in ("foreign_script_witness_provider", "local_audio_witness_provider"):
        provider = spec.get(field)
        if provider is None:
            continue
        if (
            isinstance(provider, bool)
            or not isinstance(provider, str)
            or provider not in _FOREIGN_SCRIPT_WITNESS_PROVIDERS
        ):
            raise ValueError(f"{field} must be unset, agy, moss or mai")
    native_budget = validate_budget_config(spec.get("local_audio_witness_budget"))
    if native_budget is not None and spec.get("local_audio_witness_provider") not in {
        "moss",
        "mai",
    }:
        raise BudgetConfigurationError(
            BUDGET_CONFIG_INVALID,
            "local_audio_witness_budget requires local_audio_witness_provider moss or mai",
        )


def load_producer_request(
    args: argparse.Namespace,
    *,
    repo_root: Path,
    profile_asset_file: Callable[[str], Path],
    selected_channel_profile: ChannelProfile | None = None,
    trusted_parent_profile_binding: dict[str, str] | None = None,
) -> ProducerRequest:
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    try:
        # Legacy specs have a positive compatibility authority only when their
        # parent explicitly passes its frozen selection. Never guess default.
        binding = normalize_profile_binding(spec.get("profile_binding", trusted_parent_profile_binding))
        if trusted_parent_profile_binding is not None:
            require_profile_binding(binding, trusted_parent_profile_binding)
        selected = selected_channel_profile or load_channel_profile(repo_root, profile_binding=binding)
        require_profile_binding(binding_for_profile(selected), binding)
        spec["profile_binding"] = binding
    except ChannelProfileError as exc:
        raise ProducerRuntimePrerequisiteError("channel_profile", str(exc)) from exc
    from src.autoslice.argon_escalation_defaults import (
        apply_production_argon_default, is_production_runtime_spec,
    )
    # Derived rerun SPECs (copied from an older SPEC) bound to the live runtime
    # get the same production Argon default as runner-built SPECs.
    if is_production_runtime_spec(spec):
        apply_production_argon_default(spec)
    from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
    # Replay source provenance before creating output or invoking any provider.
    bind_published_recall_to_spec(spec)
    from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
    validate_source_checkpoint_parent(spec)
    _validate_foreign_script_witness_provider(spec)
    if spec.get(EXACT_INTERVAL_RUNTIME_CONFIG_KEY) is not None:
        raise ValueError(
            "reviewed exact source interval runtime authority is internal; "
            "the producer must compile it from the repository-sealed baseline reference"
        )
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
            f"semantic_tail_trim_cap_ms must stay within 0..{SEMANTIC_TAIL_TRIM_MAX_MS}"
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
            raise ValueError(f"SOURCE_FACT_SCORECARD_RESCORE_PROVENANCE_INVALID: {exc}") from exc
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

    validate_producer_runtime_prerequisites(
        host=args.ssh_host,
        speaker_mode=getattr(args, "speaker_mode", "uniform_host"),
        speaker_python=getattr(args, "speaker_python", None),
    )
    resume = getattr(args, "resume_prepared", None)
    if resume is not None:
        from src.autoslice.producer_prepared_recovery import validate_resume_options
        validate_resume_options(args)
        # Runtime identity comes from the hash-bound original spec, never the
        # depth of the writable successor directory. Keep it explicit in this
        # in-memory request so every later prepare uses the same authority.
        from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
        from src.autoslice.producer_delivery_transaction import ProducerDeliveryTransactionError
        try:
            runtime_root = resolve_talk_delivery_runtime_root(spec)
            if not args.resume_output_root.is_relative_to(runtime_root):
                raise ProducerDeliveryTransactionError("resume output escapes original runtime root")
        except (ProducerDeliveryTransactionError, OSError, ValueError) as exc:
            raise ProducerRuntimePrerequisiteError("delivery_runtime", str(exc)) from exc
        spec["runtime_root"] = str(runtime_root)
        spec["output_root"] = str(args.resume_output_root)
    elif any(getattr(args, name, None) for name in (
        "resume_output_root", "resume_spec_sha256", "resume_cover_failure",
        "resume_cover_failure_sha256", "resume_cover_prepare_qc",
    )):
        raise ValueError("PREPARED_RESUME_OPTIONS_REQUIRE_HANDLE")
    out_root = Path(spec["output_root"]) / cid
    out_root.mkdir(parents=True, exist_ok=True)
    if getattr(args, "prepare_only", False):
        from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
        from src.autoslice.producer_delivery_transaction import (
            ProducerDeliveryTransactionError,
            deployment_authority_binding,
        )
        try:
            deployment_authority_binding(resolve_talk_delivery_runtime_root(spec))
        except (ProducerDeliveryTransactionError, OSError, ValueError) as exc:
            raise ProducerRuntimePrerequisiteError("delivery_runtime", str(exc)) from exc
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
        profile_binding=binding,
    )
