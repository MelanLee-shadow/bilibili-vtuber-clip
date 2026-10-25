"""Runtime GC keeps every byte a published candidate's replay still reads.

Replays the OCI3 loss: the c20 candidate was published only in the
runtime publication registry (no daily state file), so GC classified its run
as idle and unlinked the recut, padded, piece and package media that the
published-recall builder replays.  Synthetic bytes; no media quality claim.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from src.autoslice import runtime_gc as gc
from src.autoslice.first_publication_boundary import published_recall_inputs

RUN = "private-runs/c20-oct12-201517-normal-20261013"
CID = "semantictalk_1438010_1548390"
DATE = "2026-10-12"
BVID = "BV1Zc20Oct12x"
TITLE = "童年挑大学看易中天和辩论队"


def write(path: Path, content: bytes | dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(content, ensure_ascii=False).encode() if isinstance(content, dict) else content)
    return path


def entry(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size}


def age_tree(root: Path, days: float = 8) -> None:
    stamp = time.time() - days * gc.DAY
    for directory, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            os.utime(Path(directory) / name, (stamp, stamp), follow_symlinks=False)
        os.utime(directory, (stamp, stamp))


def incident_tree(base: Path, *, registered: bool = True) -> dict:
    run = base / RUN
    out = run / "production-normal" / "output" / CID
    source = write(run / "source" / "22966160_20261012-20-15-17.flv", b"recording")
    piece = write(out / "piece_0_1428260_1597990.mp4", b"piece")
    padded = write(out / "padded_1428260_1597990.mp4", b"padded")
    recut = write(out / "replacement_recuts" / f"{CID}.recut.mp4", b"recut")
    clip_cache = write(run / "source" / ".context_clip_cache" / ("ab" * 32 + ".mp4"), b"shared context clip")
    piece_proof = {"source_path": str(source), "output_path": str(piece)}
    padded_proof = {"inputs": [{"path": str(piece)}], "output_path": str(padded)}
    write(piece.with_suffix(".provenance.json"), piece_proof)
    write(padded.with_suffix(".provenance.json"), padded_proof)
    write(recut.with_suffix(".provenance.json"), {
        "source_piece": piece_proof, "padded": padded_proof,
        "final_recut": {"source_path": str(padded), "output_path": str(recut)}})
    srt = write(out / "padded.fresh.srt", b"1\n00:00:00,000 --> 00:00:01,000\nx\n")
    live = write(out / f"{CID}.chat-authority.json", {"schema_version": "chat-authority-audit.v2"})
    blob = write(out / "evidence-store" / "blobs" / ("cd" * 32 + ".bin"), b"{}")
    checkpoint = write(out / f"{CID}.source-text-checkpoint.json", {
        "srt": {"path": str(srt), "sha256": "x"}, "padded": {"path": str(padded), "sha256": "x"},
        "authority": {"path": str(live), "sha256": "x"}})
    budget = write(out / "native-audio-budget.json", {"source_media": str(padded)})
    journal = write(out / f"{CID}.cpa-usage.jsonl", b'{"kind": "text"}\n')
    package = run / "production-native-first-clip-v03" / "REVIEW-PACKAGE-3a7f-V5"
    final = write(package / f"{TITLE}.mp4", b"final")
    cover = write(package / f"{TITLE}.cover.png", b"cover")
    context = write(package / f"{TITLE}.clip-context.json", {"candidate_id": CID})
    record = write(package / f"{TITLE}.record.json", {
        "candidate_id": CID, "media_path": str(recut), "clip_context_path": str(context)})
    manifest = write(package / f"{TITLE}.P-20261015.upload_manifest.json", {
        "video": entry(final), "cover": entry(cover), "package_attestation": {"record": entry(record)}})
    evidence = {"manifest": entry(manifest)}
    for key in ("public_verify", "season_verify", "uploaded"):
        evidence[key] = entry(write(package / f"{TITLE}.P-20261015.{key}.json", {"bvid": BVID}))
    authority = write(package / f"{TITLE}.P-20261015.publication_reconciliation.json", {
        "schema_version": "new-bv-publication-reconciliation-authority.v1", "status": "VERIFIED_PUBLIC",
        "candidate_id": CID, "recording_date": DATE, "bvid": BVID, "aid": 1, "cid": 2,
        "title": TITLE, "evidence": evidence})
    rows = [{"candidate_id": CID, "recording_date": DATE, "status": "published", "bvid": BVID,
             "publication_reconciliation": {
                 "schema_version": "publication-reconciliation.v1", "status": "VERIFIED_PUBLIC",
                 "candidate_id": CID, "recording_date": DATE, "bvid": BVID, "aid": 1, "cid": 2,
                 "authority": entry(authority), "reconciled_at": "2026-10-15T08:00:00+00:00"}}]
    write(base / gc.RUNTIME_REGISTRY, {"schema_version": "publication-reconciliation-registry.v1",
                                        "entries": rows if registered else []})
    registry = write(base.parent / "committed-registry.json",
                     {"schema_version": "publication-registry.v1", "entries": []})
    age_tree(base)
    return {"registry": registry, "recut": recut, "padded": padded, "piece": piece, "final": final,
            "cover": cover, "source": source, "clip_cache": clip_cache, "manifest": manifest,
            "authority": authority, "record": record, "checkpoint_files": {checkpoint, live, budget, journal, blob}}


def collect(base: Path, tree: dict, *, free: int = 30 * gc.GIB) -> dict:
    return gc.collect(base, home=base / "absent-home", live=gc.Activity(), free_bytes=free,
                      registry=tree["registry"])


def targets(value: dict) -> set[Path]:
    return {Path(row["path"]) for row in value["targets"]}


RECALL = ("recut", "padded", "piece", "final", "cover")


@pytest.mark.parametrize("free", [30 * gc.GIB, 10 * gc.GIB])
def test_registry_only_publication_keeps_recall_inputs_after_retention(tmp_path, free):
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    value = collect(base, tree, free=free)
    # Only the shared context-clip cache is a published intermediate.
    assert targets(value) == {tree["clip_cache"]}
    assert value["retained_logical_bytes"]["published-input"] == sum(
        tree[k].stat().st_size for k in RECALL)
    assert value["retained_logical_bytes"]["recording"] == tree["source"].stat().st_size


def test_control_same_tree_without_registry_row_collects_recall_inputs(tmp_path):
    base = tmp_path / "autoslice"
    tree = incident_tree(base, registered=False)
    assert {tree[k] for k in RECALL} | {tree["clip_cache"]} == targets(collect(base, tree))


def test_published_closure_names_checkpoint_parent_and_evidence_store(tmp_path):
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    files, complete = published_recall_inputs(tree["authority"], {CID}, within=base)
    assert complete
    assert {tree[k] for k in RECALL + ("manifest", "record", "authority", "source")} <= files
    assert tree["checkpoint_files"] <= files
    assert tree["clip_cache"] not in files


def test_unresolved_publication_chain_fails_closed_for_candidate_and_package(tmp_path):
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    tree["manifest"].unlink()  # authority's own hash binding is gone
    value = collect(base, tree)
    # Without the replayable chain, the whole candidate run stays (fail closed).
    assert not targets(value)
    assert value["retained_logical_bytes"]["published-unresolved"] == sum(
        tree[k].stat().st_size for k in RECALL + ("clip_cache",))


def test_unreadable_registry_refuses_instead_of_falling_back_to_idle(tmp_path):
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    write(base / gc.RUNTIME_REGISTRY, b"{not json")
    with pytest.raises(ValueError):
        collect(base, tree)
    output = []
    write(base / "runtime-gc.json", {"apply_enabled": True})  # the hook only collects when it may apply
    gc.request_terminal_gc(base, {"picks": [{"candidate_id": CID, "status": "candidate_rejected"}]},
                           log=output.append)
    assert output and output[0].startswith("RUNTIME_GC_TERMINAL_DEFERRED")
    proc = write(tmp_path / "proc" / "locks", b"").parent
    assert gc.main(["--base", str(base), "--home", str(base / "absent"), "--proc", str(proc)]) == 1


def test_registry_hold_keeps_candidate_media_until_released(tmp_path):
    base = tmp_path / "autoslice"
    media = write(base / "out" / "2026-10-01" / "auto_1_2_3" / "clip.mp4", b"media")
    age_tree(base)
    registry = write(tmp_path / "committed.json", {"schema_version": "publication-registry.v1", "entries": [
        {"candidate_id": "auto_1_2_3", "recording_date": "2026-10-01", "status": "hold_pending_review"}]})
    value = gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=30 * gc.GIB, registry=registry)
    assert not value["targets"] and value["retained_logical_bytes"]["registry-hold"] == 5
    write(registry, {"schema_version": "publication-registry.v1", "entries": [
        {"candidate_id": "auto_1_2_3", "recording_date": "2026-10-01", "status": "released_for_upload"}]})
    value = gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=30 * gc.GIB, registry=registry)
    assert targets(value) == {media}


class OpenedFiles:
    """Record every file the real readers open (audit hooks cannot be removed)."""
    active: set[Path] | None = None
    installed = False

    def __enter__(self):
        if not OpenedFiles.installed:
            sys.addaudithook(OpenedFiles._hook)
            OpenedFiles.installed = True
        OpenedFiles.active = set()
        return OpenedFiles.active

    def __exit__(self, *_):
        OpenedFiles.active = None

    @staticmethod
    def _hook(event, args):
        if event == "open" and OpenedFiles.active is not None and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.is_absolute() and path.is_file():
                OpenedFiles.active.add(path.resolve())


def test_round_trip_every_file_the_recall_replay_opens_is_protected(tmp_path):
    from tests.test_first_publication_boundary import CANDIDATE, published_fixture, recall_authority
    fixture = published_fixture(tmp_path)
    with OpenedFiles() as opened:
        recall_authority(fixture)
    files, complete = published_recall_inputs(fixture["publication"], {CANDIDATE}, within=tmp_path)
    assert complete and opened
    assert {path for path in opened if tmp_path in path.parents} <= {p.resolve() for p in files}


def test_round_trip_every_file_the_checkpoint_parent_opens_is_protected(tmp_path, monkeypatch):
    from src.autoslice.source_checkpoint_parent import build_source_checkpoint_parent
    from tests.test_source_checkpoint_parent import parent_fixture
    c, spec, _ = parent_fixture(tmp_path, monkeypatch)
    config = spec["retained_source_input"]
    parent_spec = Path(config["parent_spec"]["path"])
    with OpenedFiles() as opened:
        build_source_checkpoint_parent(parent_spec_path=parent_spec,
                                       checkpoint_path=Path(config["source_checkpoint"]["path"]))
    # A published record names the padded media; the closure reaches the rest.
    authority = write(tmp_path / "authority.json", {"record": {"media_path": str(c["padded"])}})
    files, _ = published_recall_inputs(authority, {c["cid"]}, within=tmp_path)
    # The parent SPEC is a recipe record GC never collects (it is not media).
    opened = {path for path in opened if tmp_path in path.parents} - {parent_spec.resolve()}
    assert opened and opened <= {p.resolve() for p in files}
