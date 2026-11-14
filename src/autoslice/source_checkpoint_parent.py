"""Explicit immutable SOURCE parents for a new ordinary producer attempt.

A failed producer has no completed package record. Its native SOURCE checkpoint
and later accounting are independent inputs; neither grants a release verdict.
"""
from __future__ import annotations

from pathlib import Path


def checkpoint_parent_paths(parent: Path, cid: str) -> dict[str, Path]:
    """Native files a producer run leaves in ``output_root/<cid>`` for its SOURCE parent.

    The builder below and runtime GC's published-input closure share this
    one naming; neither keeps its own list.
    """
    return {"source_checkpoint": parent / f"{cid}.source-text-checkpoint.json",
            "live_authority": parent / f"{cid}.chat-authority.json",
            "native_budget": parent / "native-audio-budget.json",
            "cpa_journal": parent / f"{cid}.cpa-usage.jsonl"}


