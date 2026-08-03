#!/usr/bin/env python3
"""Evaluate binary speaker predictions against 维护者's blind-listening labels."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from src.autoslice.surface_canon import CHANNEL_PROFILE

try:
    from scripts.build_speaker_blind_review import Cue, parse_srt
except ModuleNotFoundError:  # Direct invocation: python3 scripts/evaluate_speaker_phase1.py
    from build_speaker_blind_review import Cue, parse_srt


TRUTH_TO_AUTO = {
    CHANNEL_PROFILE.profile_id: CHANNEL_PROFILE.host_speaker_label,
    f"non_{CHANNEL_PROFILE.profile_id}": CHANNEL_PROFILE.guest_speaker_label,
}


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _round(value: float) -> float:
    return round(value, 6)


def _candidate_id(manifest: dict) -> str:
    source = Path(str(manifest["source_media"]))
    if len(source.parts) < 3:
        raise ValueError(f"cannot derive candidate id from {source}")
    return source.parts[-3]


def _load_auto_dir(path: Path) -> dict[str, tuple[dict, list[Cue]]]:
    result: dict[str, tuple[dict, list[Cue]]] = {}
    for manifest_path in sorted(path.glob("*.speaker.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidate = _candidate_id(manifest)
        text_path = path / manifest_path.name.replace(".speaker.json", ".text-final.srt")
        if not text_path.is_file():
            raise FileNotFoundError(f"missing local text-final SRT for {candidate}: {text_path}")
        cues = parse_srt(text_path)
        decisions = manifest.get("analysis", {}).get("decisions", [])
        if len(cues) != len(decisions):
            raise ValueError(f"cue/decision count mismatch for {candidate}: {len(cues)} != {len(decisions)}")
        result[candidate] = (manifest, cues)
    return result


def _overlap_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def align_prediction(item: dict, manifest: dict, cues: list[Cue]) -> dict:
    decisions = {int(row["source_index"]): row for row in manifest["analysis"]["decisions"]}
    label_ms: Counter[str] = Counter()
    source_ms: Counter[str] = Counter()
    rows: list[tuple[int, dict, int]] = []
    for cue in cues:
        overlap = _overlap_ms(int(item["start_ms"]), int(item["end_ms"]), cue.start_ms, cue.end_ms)
        if not overlap:
            continue
        decision = decisions[cue.number]
        label_ms[str(decision["speaker"])] += overlap
        source_ms[str(decision["decision_source"])] += overlap
        rows.append((overlap, decision, cue.number))
    if not rows:
        raise ValueError(f"no automatic cue overlaps {item['review_id']}")
    rows.sort(key=lambda entry: entry[0], reverse=True)
    dominant = rows[0][1]
    total_overlap = sum(label_ms.values())
    duration = int(item["duration_ms"])
    return {
        "predicted": label_ms.most_common(1)[0][0],
        "decision_source": source_ms.most_common(1)[0][0],
        "label_overlap_ms": dict(label_ms),
        "total_overlap_ms": total_overlap,
        "coverage": _round(_safe_div(total_overlap, duration)),
        "automatic_cue_numbers": [number for _overlap, _decision, number in rows],
        "seed_score": dominant.get("seed_score"),
        "host_score": dominant.get("host_score"),
        "guest_score": dominant.get("guest_score"),
        "margin": dominant.get("margin"),
    }


def _class_metrics(confusion: Counter[tuple[str, str]], label: str) -> dict:
    other = "连线" if label == "李豆沙" else "李豆沙"
    tp = confusion[(label, label)]
    fp = confusion[(other, label)]
    fn = confusion[(label, other)]
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return {"precision": _round(precision), "recall": _round(recall), "f1": _round(f1), "support": tp + fn}


def evaluate(*, labels_path: Path, review_manifest_path: Path, auto_dir: Path) -> dict:
    labels_doc = json.loads(labels_path.read_text(encoding="utf-8"))
    review_doc = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    if labels_doc.get("package_id") != review_doc.get("package_id"):
        raise ValueError("labels and review manifest package_id mismatch")
    items = {row["review_id"]: row for row in review_doc["items"]}
    answers = {row["review_id"]: row for row in labels_doc["answers"]}
    if set(items) != set(answers):
        raise ValueError("labels do not cover the exact review item set")
    auto = _load_auto_dir(auto_dir)
    batch_path = auto_dir / "batch-manifest.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8")) if batch_path.is_file() else {}

    rows: list[dict] = []
    confusion: Counter[tuple[str, str]] = Counter()
    clear_correct = 0
    clear_total = 0
    duration_correct = 0
    duration_total = 0
    per_source: dict[str, Counter[str]] = defaultdict(Counter)
    per_decision_source: dict[str, Counter[str]] = defaultdict(Counter)
    label_counts: Counter[str] = Counter()

    for review_id, item in items.items():
        answer = answers[review_id]
        truth = answer.get("label")
        label_counts[str(truth)] += 1
        candidate = str(item["source_id"])
        if candidate not in auto:
            raise ValueError(f"missing automatic result for {candidate}")
        manifest, cues = auto[candidate]
        aligned = align_prediction(item, manifest, cues)
        row = {
            "review_id": review_id,
            "source_id": candidate,
            "cue_number": item["cue_number"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "duration_ms": item["duration_ms"],
            "text": item["text"],
            "truth": truth,
            "note": answer.get("note") or "",
            **aligned,
        }
        expected = TRUTH_TO_AUTO.get(str(truth))
        if expected is not None:
            clear_total += 1
            per_source[candidate]["clear_total"] += 1
            per_decision_source[aligned["decision_source"]]["clear_total"] += 1
            predicted = str(aligned["predicted"])
            confusion[(expected, predicted)] += 1
            correct = predicted == expected
            row["cue_correct"] = correct
            if correct:
                clear_correct += 1
                per_source[candidate]["clear_correct"] += 1
                per_decision_source[aligned["decision_source"]]["clear_correct"] += 1
            correct_ms = int(aligned["label_overlap_ms"].get(expected, 0))
            duration_correct += correct_ms
            duration_total += int(aligned["total_overlap_ms"])
        else:
            row["cue_correct"] = None
            per_source[candidate][str(truth)] += 1
        rows.append(row)

    by_class = {label: _class_metrics(confusion, label) for label in ("李豆沙", "连线")}
    macro_f1 = sum(row["f1"] for row in by_class.values()) / 2
    mixed = label_counts["mixed_or_overlap"]
    unjudgeable = label_counts["unjudgeable"]
    strict_denominator = len(rows) - unjudgeable
    errors = [row for row in rows if row["cue_correct"] is False]
    return {
        "schema_version": "lidousha-speaker-phase1-evaluation.v1",
        "package_id": review_doc["package_id"],
        "automatic_generator_sha256": batch.get("generator_sha256"),
        "sample": {
            "total": len(rows),
            "clear_binary": clear_total,
            "mixed_or_overlap": mixed,
            "unjudgeable": unjudgeable,
            "truth_counts": dict(label_counts),
        },
        "clear_binary_metrics": {
            "cue_accuracy": _round(_safe_div(clear_correct, clear_total)),
            "duration_weighted_accuracy": _round(_safe_div(duration_correct, duration_total)),
            "macro_f1": _round(macro_f1),
            "by_class": by_class,
            "confusion": {
                "李豆沙_as_李豆沙": confusion[("李豆沙", "李豆沙")],
                "李豆沙_as_连线": confusion[("李豆沙", "连线")],
                "连线_as_李豆沙": confusion[("连线", "李豆沙")],
                "连线_as_连线": confusion[("连线", "连线")],
            },
        },
        "strict_all_judgeable_cue_accuracy_mixed_counted_wrong": _round(
            _safe_div(clear_correct, strict_denominator)
        ),
        "per_source": {
            source: {**counts, "clear_accuracy": _round(_safe_div(counts["clear_correct"], counts["clear_total"]))}
            for source, counts in sorted(per_source.items())
        },
        "per_decision_source": {
            source: {**counts, "clear_accuracy": _round(_safe_div(counts["clear_correct"], counts["clear_total"]))}
            for source, counts in sorted(per_decision_source.items())
        },
        "errors": errors,
        "rows": rows,
    }


def render_markdown(result: dict) -> str:
    metric = result["clear_binary_metrics"]
    lines = [
        "# 人声二分离第一阶段自动准确率",
        "",
        f"- 总样本：{result['sample']['total']}；明确二分类：{result['sample']['clear_binary']}；换人/重叠：{result['sample']['mixed_or_overlap']}；无法判断：{result['sample']['unjudgeable']}。",
        f"- 明确 cue accuracy：{metric['cue_accuracy']:.1%}",
        f"- 时长加权 accuracy：{metric['duration_weighted_accuracy']:.1%}",
        f"- Macro-F1：{metric['macro_f1']:.3f}",
        f"- 严格全可判断 cue accuracy（换人/重叠计错）：{result['strict_all_judgeable_cue_accuracy_mixed_counted_wrong']:.1%}",
        "",
        "## 两类指标",
        "",
        "| 类别 | Precision | Recall | F1 | Support |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, row in metric["by_class"].items():
        lines.append(f"| {label} | {row['precision']:.1%} | {row['recall']:.1%} | {row['f1']:.1%} | {row['support']} |")
    lines.extend(["", "## 每条素材", "", "| Candidate | Clear | Correct | Accuracy | Mixed | Unjudgeable |", "|---|---:|---:|---:|---:|---:|"])
    for source, row in result["per_source"].items():
        lines.append(
            f"| {source} | {row.get('clear_total', 0)} | {row.get('clear_correct', 0)} | {row.get('clear_accuracy', 0):.1%} | {row.get('mixed_or_overlap', 0)} | {row.get('unjudgeable', 0)} |"
        )
    lines.extend(["", "## 明确二分类错误", ""])
    for row in result["errors"]:
        note = f"；备注：{row['note']}" if row["note"] else ""
        lines.append(
            f"- `{row['review_id']}`：真值 `{row['truth']}`，自动 `{row['predicted']}`，来源 `{row['decision_source']}`{note}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path, required=True)
    parser.add_argument("--auto-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(labels_path=args.labels, review_manifest_path=args.review_manifest, auto_dir=args.auto_dir)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"output_json": str(args.output_json), "output_md": str(args.output_md), "metrics": result["clear_binary_metrics"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
