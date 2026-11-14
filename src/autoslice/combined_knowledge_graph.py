"""The repository knowledge graph plus the news crawler's runtime candidates, validated together.

Split out of news_event_graph so the lane reads the combined graph without loading the news judge;
news_event_graph re-exports `load_combined`."""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.autoslice.knowledge_graph import EDGES_FILE, NODES_FILE, Graph, load_graph


def _combined_root(repo_root: Path, runtime_root: Path | None, scratch: Path) -> Path:
    """A scratch profile root whose knowledge files are the repository's followed by the runtime candidates'."""
    (scratch / NODES_FILE).parent.mkdir(parents=True, exist_ok=True)
    for rel in (NODES_FILE, EDGES_FILE):
        text = (repo_root / rel).read_text("utf-8")
        if runtime_root is not None and (runtime_root / rel).is_file():
            text += (runtime_root / rel).read_text("utf-8")
        (scratch / rel).write_text(text, "utf-8")
    return scratch


def load_combined(repo_root: Path, runtime_root: Path | None) -> Graph:
    """Repository graph plus the runtime candidates, validated together (a missing runtime graph adds nothing)."""
    with tempfile.TemporaryDirectory(prefix="news-event-graph-") as tmp:
        return load_graph(_combined_root(Path(repo_root), runtime_root and Path(runtime_root), Path(tmp)))
