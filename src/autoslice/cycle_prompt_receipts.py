"""Lossless rendering of repeated convergence receipts without closure retention."""
from __future__ import annotations

from collections.abc import Callable
import json


def cycle_prompt_receipts(
    history: object, evidence: object, *,
    text_sha256: Callable[[str], str], json_sha256: Callable[[object], str],
) -> tuple[str, str, str]:
    """Represent repeated receipts once, without dropping any historical fact."""
    counts: dict[str, int] = {}
    values: dict[str, object] = {}
    sizes: dict[str, int] = {}
    keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif not isinstance(value, str):
            return
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = text_sha256(encoded)
        counts[digest] = counts.get(digest, 0) + 1
        values[digest] = value
        sizes[digest] = len(encoded.encode("utf-8"))

    try:
        collect(history)
        collect(evidence)
    finally:
        del collect
    shared = {digest for digest, count in counts.items() if count > 1 and sizes[digest] >= 256}
    reference_key = "$receipt_ref"
    while reference_key in keys:
        reference_key += "_"
    table: dict[str, object] = {}
    ids = {digest: "R" + str(index) for index, digest in enumerate(sorted(shared), 1)}

    def render(value: object, *, expand: bool = False) -> object:
        if isinstance(value, (dict, list, str)):
            digest = json_sha256(value)
            if not expand and digest in shared:
                reference = ids[digest]
                if reference not in table:
                    table[reference] = render(values[digest], expand=True)
                return {reference_key: reference}
            if isinstance(value, dict):
                return {key: render(child) for key, child in value.items()}
            if isinstance(value, list):
                return [render(child) for child in value]
            return value
        return value

    try:
        def dump(value: object) -> str:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        original_history, original_evidence = dump(history), dump(evidence)
        if not shared:
            return original_history, original_evidence, ""
        rendered_history, rendered_evidence = dump(render(history)), dump(render(evidence))
        section = (
            "\n## 共享收据（无损去重）\n"
            f"仅含 {reference_key} 的对象引用下表同名完整收据；按引用递归展开。"
            "引用没有裁决权，不代表新证据；候选、旧拒绝理由、原观察和哈希均保留。"
            "HISTORY_SHA256/CURRENT_EVIDENCE_SHA256 绑定展开后的原始对象，未改动。\n"
            + dump(table)
        )
        if len(rendered_history) + len(rendered_evidence) + len(section) >= len(original_history) + len(original_evidence):
            return original_history, original_evidence, ""
        return rendered_history, rendered_evidence, section
    finally:
        del render
