"""F5: 句内子窗混说/重叠检测——把恒空的 mixed/overlap 证据通道接上产出侧。

诊断（出处内部取证文档 §3.2
与 2026-08-08-forensics-auto_210739_1142_1436.md (b)4）：`mixed_overlap_evidence`
全程 null **不是阈值死区，也不是标志位被算成假——是产出侧根本不存在**：

1. `overlap_detected` / `mixed_speaker_within_unit_detected` 两个键在全仓库只出现
   在 *校验方*（`speaker_session_router.py`）、*消费方*（`producer_speaker.py`、
   `speaker_evidence.py`）和测试里，**没有任何 in-repo 生产者**；唯一的产出面是
   `AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON` 指向的外部密封 provider。
2. 该 env 在 deploy 脚本、cron 行和文档里都没有配置，`_speaker_routing_provider_command`
   因此返回 None，`prepare_speaker_routing` 提前返回 None，候选项上根本不会挂
   `speaker_routing_claim`；producer 侧只能落到 `ROUTING_CLAIM_MISSING`。
3. 即使有人配上了 env，`speaker_session_router.AUDITED_PROVIDER_BUNDLES` 是**空 dict**，
   `validate_provider_authority(require_audited=True)` 必然抛
   "provider bundle/algorithm is not repo-audited"，路由照样整条关闭。

三重独立断路 ⇒ `producer_speaker.run_producer_speaker_finalization` 里
`result["mixed_or_overlap_detected"] is True` 那条分支在生产上**结构性不可达**，
法证里"标志位算成假 vs 分支未执行"的二义在代码层解为后者。

本模块是缺失的那个产出者，落在**已经有音频和 CAM++ 打分的终定阶段**（provider
mixed gate 跑在声学分析之前，自产证据无法喂回同一轮的门——这是架构决定的，不是
本模块的取舍）。它按 cue 时长切确定性子窗、复用同一套 host/guest 打分与
`acoustic_hard_pass` 判据，只在同一 cue 的子窗落到**互相冲突的确信标签**时出证据。

保守面（维护者 口径「证据只披露不改标签」）：
* 本模块**不改任何标签**，不做句内切分，不动二分语义；决策数组原样返回。
* 产出物是一份 schema 合法、`validate_mixed_overlap_evidence_document` 可校验的
  sidecar + READY manifest 里的披露块。没有任何代码自动把它喂进
  `_evaluate_mixed_overlap_gate`——把它提升成阻断输入是 integrator/维护者 的开关，
  不是本次修复顺手打开的。
* v1 只出 `CUE_MIXED_SPEAKER`：子窗嵌入能证明"同一 cue 内出现了两个确信不同的
  说话人"，不能证明"两人同时说"；真正的同时重叠会把子窗嵌入拉成混合向量、落进
  模糊带，v1 不据此断言 `CUE_OVERLAPPING_SPEECH`。
* 只按时长决定哪些 cue 进子窗（**不按 margin 预筛**）：8/7 法证的假李豆沙
  cue33/37 margin 0.43/0.34 明显在模糊带之外，用 margin 预筛等于在这类案子上重建
  一个新的阈值死区。短于两个最小窗的 cue 无法分窗，属 v1 已知盲区。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.speaker_common import (
    MIXED_OVERLAP_EVIDENCE_SCHEMA,
    PROFILE_ID,
    SpeakerFinalizationError,
    milliseconds,
)
from src.autoslice.speaker_host_evidence import acoustic_hard_pass

DETECTION_SCHEMA = f"{PROFILE_ID}-speaker-subcue-overlap-detection.v1"
DETECTOR_NAME = f"{PROFILE_ID}-subcue-window-detector"
DETECTOR_VERSION = "v1"
# 最小子窗 700ms：低于这个长度 CAM++ 嵌入本身不稳定（同一说话人也会来回翻），
# 会把检测器变成噪声源。上限 4 窗把每 cue 的额外嵌入次数封死在常数。
MIN_SUBCUE_WINDOW_MS = 700
MAX_SUBCUE_WINDOWS = 4
EVIDENCE_FILENAME = "detected-mixed-overlap-evidence.json"
# 无人值守跑在 free 的 pinned diar runtime 上：本检测器给每条够长的 cue 增加最多
# 4 次窗口切分 + CAM++ 嵌入。默认开启（否则通道又是空的），但留一个 env 杀开关，
# 免得运行成本出问题时只能改代码。设成 "0"/"false"/"off" 关闭。
SUBCUE_OVERLAP_ENV = "AUTOSLICE_SPEAKER_SUBCUE_OVERLAP"


def subcue_detection_enabled() -> bool:
    return os.environ.get(SUBCUE_OVERLAP_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def plan_subcue_windows(
    start_ms: int,
    end_ms: int,
    *,
    min_window_ms: int = MIN_SUBCUE_WINDOW_MS,
    max_windows: int = MAX_SUBCUE_WINDOWS,
) -> list[tuple[int, int]]:
    """Deterministic equal split, or [] when the cue cannot hold two windows."""

    duration = int(end_ms) - int(start_ms)
    if min_window_ms <= 0 or max_windows < 2 or duration < 2 * min_window_ms:
        return []
    count = min(int(max_windows), duration // int(min_window_ms))
    edges = [int(start_ms) + (duration * step) // count for step in range(count + 1)]
    return [(edges[step], edges[step + 1]) for step in range(count)]


def mixed_window_reason(window_labels: Sequence[str | None]) -> tuple[list[str], int]:
    """Confident disagreement inside one cue -> (reason_codes, cluster_count).

    Ambiguous windows (``None``) are silent: a blended/overlapped window lands
    in the ambiguity band and must not by itself accuse a cue.  Only two
    windows that are each *confidently* on opposite sides are evidence.
    """

    confident = {label for label in window_labels if label is not None}
    if len(confident) < 2:
        return [], len(confident)
    return ["CUE_MIXED_SPEAKER"], len(confident)


def extract_window_wav(
    *, media_path: Path, start_ms: int, end_ms: int, output_path: Path
) -> None:
    """Cut one 16k mono sub-window; same ffmpeg contract as the cue extractor."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{int(start_ms) / 1000:.3f}",
            "-i", str(media_path),
            "-t", f"{max(1, int(end_ms) - int(start_ms)) / 1000:.3f}",
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0 or not output_path.is_file():
        raise SpeakerFinalizationError(
            "sub-cue window extraction failed: " + (completed.stderr or "")[-400:]
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def build_mixed_overlap_evidence_document(
    rows: Sequence[Mapping[str, object]],
    *,
    source_media_sha256: str,
    text_final_srt_sha256: str,
    config_sha256: str,
) -> dict[str, object]:
    """Assemble the exact document shape ``speaker_evidence`` already validates."""

    ordered = sorted(rows, key=lambda row: int(row["source_cue"]))
    return {
        "schema_version": MIXED_OVERLAP_EVIDENCE_SCHEMA,
        "status": "REVIEW_REQUIRED" if ordered else "CLEAR",
        "source_media_sha256": source_media_sha256,
        "text_final_srt_sha256": text_final_srt_sha256,
        "provider": {"name": DETECTOR_NAME, "config_sha256": config_sha256},
        "review_required_cues": [dict(row) for row in ordered],
    }


def _window_margin(
    window_path: Path,
    *,
    cue_index: int,
    cue_audio_paths: Sequence[Path],
    host_prints: Sequence[Path],
    guest_groups: Sequence[Sequence[int]],
    similarity: Callable[[Path, Path], float],
) -> float:
    """Same host/guest margin the whole-cue loop computes, on a sub-window.

    Self-exclusion mirrors the cue-level loop so the resulting margin stays
    comparable to the whole-clip ``threshold`` that gates it.
    """

    own = cue_audio_paths[cue_index]
    host_values = [similarity(path, window_path) for path in host_prints if path != own]
    host_score = sum(host_values) / len(host_values) if host_values else 1.0
    guest_score = 0.0
    for group in guest_groups:
        members = [index for index in group if index != cue_index]
        values = [similarity(cue_audio_paths[index], window_path) for index in members]
        guest_score = max(guest_score, sum(values) / len(values) if values else 0.0)
    return float(host_score) - float(guest_score)


def detect_subcue_mixed_overlap(
    *,
    cues: Sequence[TextCue],
    cue_audio_paths: Sequence[Path],
    media_path: Path,
    text_srt_path: Path,
    work_dir: Path,
    host_prints: Sequence[Path],
    guest_groups: Sequence[Sequence[int]],
    threshold: float,
    band: float,
    similarity: Callable[[Path, Path], float],
    extract_window: Callable[..., None] = extract_window_wav,
    min_window_ms: int = MIN_SUBCUE_WINDOW_MS,
    max_windows: int = MAX_SUBCUE_WINDOWS,
) -> dict[str, object]:
    """Produce disclosure-only sub-cue mixed evidence plus its bound sidecar.

    Returns the disclosure block for the READY manifest's ``analysis``.  The
    sidecar it writes is exactly what ``--mixed-overlap-evidence`` consumes, but
    nothing here feeds it back into this run's gate; labels are untouched.
    """

    window_dir = work_dir / "subcue-windows"
    policy = {
        "detector": DETECTOR_NAME,
        "detector_version": DETECTOR_VERSION,
        "min_window_ms": int(min_window_ms),
        "max_windows": int(max_windows),
        "threshold": round(float(threshold), 8),
        "ambiguity_band": round(float(band), 8),
    }
    rows: list[dict[str, object]] = []
    cue_windows: list[dict[str, object]] = []
    skipped_short = 0
    for index, cue in enumerate(cues):
        plan = plan_subcue_windows(
            milliseconds(cue.start),
            milliseconds(cue.end),
            min_window_ms=min_window_ms,
            max_windows=max_windows,
        )
        if not plan:
            skipped_short += 1
            continue
        windows: list[dict[str, object]] = []
        for step, (start_ms, end_ms) in enumerate(plan, start=1):
            window_path = window_dir / f"cue-{index + 1:04d}-w{step:02d}.wav"
            extract_window(
                media_path=media_path,
                start_ms=start_ms,
                end_ms=end_ms,
                output_path=window_path,
            )
            margin = _window_margin(
                window_path,
                cue_index=index,
                cue_audio_paths=cue_audio_paths,
                host_prints=host_prints,
                guest_groups=guest_groups,
                similarity=similarity,
            )
            windows.append(
                {
                    "window": step,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "margin": round(margin, 8),
                    "label": acoustic_hard_pass(margin, float(threshold), float(band)),
                }
            )
        reason_codes, cluster_count = mixed_window_reason(
            [str(row["label"]) if row["label"] is not None else None for row in windows]
        )
        cue_windows.append(
            {
                "source_cue": index + 1,
                "windows": windows,
                "reason_codes": reason_codes,
                "cluster_count": cluster_count,
            }
        )
        if not reason_codes:
            continue
        audio_path = cue_audio_paths[index].resolve()
        rows.append(
            {
                "source_cue": index + 1,
                "zero_based_index": index,
                "start": cue.start,
                "end": cue.end,
                "text": cue.text,
                "audio_sha256": _sha256_file(audio_path),
                "reason_codes": reason_codes,
                "provider_details": {
                    "cluster_count": cluster_count,
                    "audio_path": str(audio_path),
                    "detector": DETECTOR_NAME,
                    "detector_version": DETECTOR_VERSION,
                    "window_margins": [row["margin"] for row in windows],
                    "window_labels": [row["label"] for row in windows],
                },
            }
        )
    document = build_mixed_overlap_evidence_document(
        rows,
        source_media_sha256=_sha256_file(media_path),
        text_final_srt_sha256=_sha256_file(text_srt_path),
        config_sha256=_canonical_sha256(policy),
    )
    evidence_path = work_dir / EVIDENCE_FILENAME
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "schema_version": DETECTION_SCHEMA,
        "policy": policy,
        "windowed_cue_count": len(cue_windows),
        "skipped_short_cue_count": skipped_short,
        "mixed_cue_count": len(rows),
        "mixed_source_cues": [int(row["source_cue"]) for row in rows],
        "cue_windows": cue_windows,
        "evidence_status": document["status"],
        "evidence_document": str(evidence_path.resolve()),
        "evidence_document_sha256": _sha256_file(evidence_path),
        "consumed_by_this_run": False,
    }


def subcue_mixed_overlap_disclosure(**kwargs: object) -> dict[str, object]:
    """Disclosure lane wrapper: never turn a detector fault into a delivery block.

    The labels this run ships are decided entirely by the existing CAM++ /
    context path.  A missing ffmpeg, an unreadable window, or any other
    detector fault must surface as an ``UNAVAILABLE`` disclosure, not as a
    speaker-finalization failure — fail-closed belongs to the gates that own
    labels, not to a channel that only reports.
    """

    if not subcue_detection_enabled():
        return {
            "schema_version": DETECTION_SCHEMA,
            "status": "DISABLED",
            "reason": f"{SUBCUE_OVERLAP_ENV} disabled the sub-cue detector",
        }
    try:
        return {"status": "READY", **detect_subcue_mixed_overlap(**kwargs)}  # type: ignore[arg-type]
    except Exception as exc:
        return {
            "schema_version": DETECTION_SCHEMA,
            "status": "UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
        }
