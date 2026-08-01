#!/usr/bin/env python3
"""Export a clean open-source snapshot of the pipeline (Ivan 2026-08-02 rules).

原则：产品只有产品。代码/产品文档/词表等劳动成果保留；真值台账、出版登记、
恢复权威、评审基线等**运营状态**剔除但保模板；开发过程记录（reports、
HANDOFF、reviews、spark、.agent）整目录剔除。未分类的新路径直接报错——
再导出时新增文件必须显式归类，防止未来无意泄漏。

导出后跑禁词扫描（私有域名/主机名/密钥形状），命中即失败。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO.parent / "bilibili-vtuber-clip"

STRIP_PREFIXES = (
    "reports/",
    "docs/reviews/",
    "docs/spark/",
    "docs/history/",
    "docs/workflows/",
    "docs/HANDOFF.md",
    "docs/handoff-",
    "AGENTS.md",   # 私库操作者规则；OSS 版由 docs/oss/AGENTS.md 提供
    "CLAUDE.md",
    "scripts/export_oss_snapshot.py",          # 私库导出工具，不随 OSS 分发
    "ops/blrec-patches/",                      # blrec 已退役
    "ops/recording/blrec_live_watchdog.py",
    "ops/recording/patch_autoslice_runner_recorder_status.py",  # blrec 迁移遗物
    "cleanup_manifests/",          # 运营清理台账
    "docs/audit-uploads-",          # 上传审计=运营记录
    "docs/pending-provisional-",    # 待办清单=运营记录
    "docs/plan-",                   # 开发计划=过程记录
    "docs/autoslice-capability-status",  # 状态快照=过程记录
    "docs/remote-first-autoslice-route.md",  # 私有主机拓扑
    "lidousha/",                    # 旧布局运营产物+遗留词表(已被assets取代)
)

# 运营状态 → 模板化（内容剔除，形状保留）。值=模板生成器名。
TEMPLATE_FILES = {
    "assets/lidousha/publication_registry.v1.json": "registry",
    "assets/lidousha/subtitle_truth_ledger.v1.json": "truth_ledger",
    "assets/lidousha/final_media_review_contracts.v1.json": "review_contracts",
    "assets/lidousha/recovery_publication_authority.v1.json": "rpa_index",
    "assets/lidousha/session_relation_ledger.v1.json": "empty_entries",
    "assets/lidousha/speech_memory_ledger.v1.json": "empty_entries",
    "assets/lidousha/published_songs.v1.json": "empty_entries",
    "assets/lidousha/manual_title_overrides.v1.json": "empty_entries",
    "assets/lidousha/manual_archive_metadata.v1.json": "empty_entries",
    "assets/lidousha/cover_reference_overrides.v1.json": "empty_entries",
    "assets/lidousha/selection_score_calibration.v1.json": "keep",  # 标定=劳动成果
    "assets/lidousha/timely_terms.json": "empty_entries",  # 运行时缓存
}

# 整目录运营状态：剔除全部内容，保目录+模板说明。
TEMPLATE_DIRS = (
    "assets/lidousha/recovery_publication_authority_",  # 前缀匹配单文件们
    "assets/lidousha/reviewed_subtitle_baselines/",
    "assets/lidousha/subtitle_regressions/",
    "assets/lidousha/subtitle_text_overrides/",
    "assets/lidousha/subtitle_overrides/",
    "assets/lidousha/speaker_overrides/",
    "assets/lidousha/speaker_session_anchors/",
    "assets/lidousha/speaker_batch_plans/",
    "assets/lidousha/manual_title_repair_authorities/",
    "assets/lidousha/cover_repair_plans/",
    "assets/lidousha/talk_recoveries/",
    "assets/lidousha/voiceprint_profile.v1.json",  # 生物特征，绝不发布
)

FORBIDDEN_PATTERNS = (
    r"aierlma",
    r"cpa\.[a-z0-9.-]+\.top",
    r"sk-[A-Za-z0-9]{16,}",
    r"\boracle\b.*ssh|ssh.*\boracle\b",
)

FORBIDDEN_ALLOWLIST_SUFFIXES = (".ttf",)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def classify(path: str) -> str:
    if any(path.startswith(prefix) for prefix in STRIP_PREFIXES):
        return "strip"
    if path in TEMPLATE_FILES:
        return "template" if TEMPLATE_FILES[path] != "keep" else "keep"
    if any(path.startswith(prefix) for prefix in TEMPLATE_DIRS):
        return "strip_templated_dir"
    if path.startswith("docs/oss/"):
        return "rootmap"
    known_roots = (
        "src/", "scripts/", "tests/", "docs/pipeline/", "ops/",
        "assets/", "profiles/", "prompts/", ".agent/skills/",
        "docs/bilibili-ai-subtitle-via-bcut.md",
        "docs/lidousha-auto-review-architecture.md",
        ".gitignore", ".gitattributes", "pyproject.toml",
        "requirements", "README", "Makefile", "conftest.py",
    )
    if any(path.startswith(root) for root in known_roots):
        return "keep"
    return "unknown"


def _template_payload(kind: str, original: Path) -> str:
    if kind == "registry":
        data = {
            "schema_version": "publication-registry.v1",
            "notes": (
                "候选↔BV 出版登记：published 候选禁止再走新投稿（修复只允许 "
                "authorized_upload.py repair-* 的原 BV 修复链）；"
                "hold_pending_review 候选在放行前禁止任何上传。"
            ),
            "entries": [
                {
                    "_example": True,
                    "candidate_id": "auto_000000_0_0",
                    "recording_date": "2026-01-01",
                    "status": "published",
                    "bvid": "BV1xxxxxxxxx",
                    "note": "example entry — replace with your own uploads",
                }
            ],
        }
    elif kind == "truth_ledger":
        data = {
            "schema_version": "subtitle-truth-ledger.v1",
            "notes": (
                "SOURCE_INTERVAL_TRUTH：已发布切片文本修复的唯一合法所有者。"
                "每条绑定源录像 sha + 毫秒区间 + replace_cue/replace_substring。"
                "本模板为空——真值属于你自己的录播。"
            ),
            "entries": [
                {
                    "_example": True,
                    "truth_id": "example-0001",
                    "source_recording_basename": "your-recording.mp4",
                    "source_sha256": "0" * 64,
                    "interval_ms": [0, 1000],
                    "action": "replace_cue",
                    "text": "示例真值文本",
                    "authority": "operator adjudication note",
                }
            ],
        }
    elif kind == "review_contracts":
        data = {
            "schema_version": "lidousha-final-media-review-contracts.v1",
            "authority": (
                "per-candidate exact final-review points; timestamps are "
                "deliberately broad windows around the reported issue"
            ),
            "contracts": [
                {
                    "_example": True,
                    "candidate_id": "auto_000000_0_0",
                    "subtitle_review_points": [
                        {
                            "point_id": "example-point",
                            "final_video_start_ms": 0,
                            "final_video_end_ms": 5000,
                            "expectation": "描述评审者必须逐帧/逐句核对的点",
                        }
                    ],
                }
            ],
        }
    elif kind == "rpa_index":
        data = {
            "schema_version": "recovery-publication-authority-index.v1",
            "entries": [],
            "notes": "same-BV 修复的出版权威索引——运营状态，模板为空。",
        }
    else:  # empty_entries
        try:
            original_data = json.loads(original.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            original_data = {}
        data = {
            key: (
                []
                if isinstance(value, list)
                else {} if isinstance(value, dict) else value
            )
            for key, value in original_data.items()
            if key in ("schema_version", "notes", "authority")
            or not isinstance(original_data.get(key), (list, dict))
        } or {"schema_version": "unknown", "entries": []}
        for key, value in original_data.items():
            if isinstance(value, list):
                data[key] = []
            elif isinstance(value, dict):
                data[key] = {}
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    kept, templated, stripped, unknown = [], [], [], []
    templated_dirs_seen = set()
    for rel in tracked_files():
        kind = classify(rel)
        source = REPO / rel
        if kind == "rootmap":
            target = out_root / rel.removeprefix("docs/oss/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            kept.append(rel)
        elif kind == "keep":
            target = out_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if rel.startswith(".agent/skills/") and target.suffix in (
                ".md", ".yaml", ".yml", ".py",
            ):
                # 技能脱敏：维护者个人名讳不进 OSS（Ivan 2026-08-02 指示）。
                text = target.read_text(encoding="utf-8")
                cleaned = re.sub(r"\bIvan\b", "维护者", text)
                if cleaned != text:
                    target.write_text(cleaned, encoding="utf-8")
            kept.append(rel)
        elif kind == "template":
            target = out_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                _template_payload(TEMPLATE_FILES[rel], source),
                encoding="utf-8",
            )
            templated.append(rel)
        elif kind == "strip_templated_dir":
            for prefix in TEMPLATE_DIRS:
                if rel.startswith(prefix):
                    templated_dirs_seen.add(prefix)
            stripped.append(rel)
        elif kind == "strip":
            stripped.append(rel)
        else:
            unknown.append(rel)

    for prefix in sorted(templated_dirs_seen):
        if prefix.endswith("/"):
            marker_dir = out_root / prefix
            marker_dir.mkdir(parents=True, exist_ok=True)
            (marker_dir / "README.md").write_text(
                "本目录存放运营状态（逐候选评审/修复文件），"
                "属于每个部署自己的数据，不随开源仓分发。\n",
                encoding="utf-8",
            )

    if unknown:
        print("UNCLASSIFIED paths — refusing to export:")
        for rel in unknown:
            print("  ", rel)
        return 2

    violations = []
    pattern = re.compile("|".join(FORBIDDEN_PATTERNS), re.IGNORECASE)
    for path in out_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in FORBIDDEN_ALLOWLIST_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_str = str(path.relative_to(out_root))
        for line_number, line in enumerate(text.splitlines(), start=1):
            hit = pattern.search(line)
            if (
                hit
                and hit.group(0).lower() == "aierlma"
                and "aierlma521" not in line
                and rel_str in ("LICENSE", "README.md", "NOTICE")
            ):
                continue  # 作者署名是有意公开的内容；邮箱仍禁
            if hit:
                violations.append(
                    f"{path.relative_to(out_root)}:{line_number}: "
                    f"{line.strip()[:110]}"
                )
    report = {
        "kept": len(kept),
        "templated": len(templated),
        "stripped": len(stripped),
        "forbidden_hits": len(violations),
    }
    print(json.dumps(report, ensure_ascii=False))
    if violations:
        print("FORBIDDEN CONTENT — snapshot NOT publishable yet:")
        for row in violations[:60]:
            print("  ", row)
        return 3
    print(f"clean snapshot at {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
