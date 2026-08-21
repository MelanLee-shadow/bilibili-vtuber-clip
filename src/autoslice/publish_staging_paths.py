"""Small path/text helpers shared by publish staging entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from src.autoslice.review_evidence import SourceCue


def materialized_artifact_root(materialized_recut: Mapping[str, object], media_path: Path) -> Path:
    manifest_value = materialized_recut.get("manifest_path")
    if isinstance(manifest_value, str) and manifest_value:
        manifest_path = Path(manifest_value)
        return manifest_path.parent.parent if manifest_path.parent.name in {"recuts", "media"} else manifest_path.parent
    return media_path.parent.parent if media_path.parent.name in {"recuts", "media"} else media_path.parent


def staged_transcript_sample(record: Mapping[str, object], cues: Sequence[SourceCue]) -> str:
    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues

            sample = " ".join(" ".join(cue.text.split()) for cue in parse_srt_cues(Path(subtitle_path).read_text(encoding="utf-8")) if cue.text.strip())[:600]
            if sample:
                return sample
        except OSError:
            pass
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())[:600]


def private_publish_path(
    *, media_path: Path, private_artifact_root: Path | None, private_publish_json_path: Path | None, stage_cover: object
) -> Path:
    if private_publish_json_path is not None and private_artifact_root is None:
        raise ValueError("private publish output requires a private artifact root")
    if private_artifact_root is not None and stage_cover is not None:
        raise ValueError("private artifact staging requires the canonical cover stage")
    if private_artifact_root is None:
        return media_path.with_suffix(".publish.json")
    output = private_publish_json_path or private_artifact_root / media_path.with_suffix(".publish.json").name
    try:
        output.relative_to(private_artifact_root)
    except ValueError as exc:
        raise ValueError("private publish output escapes the private artifact root") from exc
    return output


def stage_cover_with_private_root(
    stage_cover: Callable[..., dict[str, object]], record: Mapping[str, object], *, private_artifact_root: Path | None, kwargs: dict[str, object]
) -> dict[str, object]:
    if private_artifact_root is not None:
        kwargs["private_artifact_root"] = private_artifact_root
    return stage_cover(record, **kwargs)
