"""CLI-to-finalization option translation kept outside the producer entry point."""

from __future__ import annotations

from src.autoslice.producer_package_finalization import ProducerFinalizationOptions


def finalization_options_from_args(args: object) -> ProducerFinalizationOptions:
    """Translate the shared producer CLI namespace at one boundary."""

    return ProducerFinalizationOptions(
        spec=args.spec,
        substrate=args.substrate,
        correct=args.correct,
        speaker_mode=args.speaker_mode,
        speaker_overrides=args.speaker_overrides,
        speaker_source_session_anchors=args.speaker_source_session_anchors,
        speaker_mixed_overlap_evidence=args.speaker_mixed_overlap_evidence,
        speaker_python=args.speaker_python,
        reuse_cover=args.reuse_cover,
        prepare_only=args.prepare_only,
    )
