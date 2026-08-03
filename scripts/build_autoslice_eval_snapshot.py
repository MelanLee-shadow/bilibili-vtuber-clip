#!/usr/bin/env python3
"""Build an immutable autoslice eval runtime from one committed Git tree.

The output contains exactly the tracked ``scripts/``, ``src/``, ``profiles/``,
and ``assets/`` surfaces plus a hash manifest.  It deliberately does not copy
a working tree: the July 11/12 blind eval omitted the selected profile's
voiceprint asset and therefore measured a broken deployment rather than the
pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile


ARCHIVE_ROOTS = ("scripts", "src", "profiles", "assets")
BASE_REQUIRED_PATHS = (
    "scripts/session_autoslice.py",
    "scripts/produce_slice_package.py",
    "src/autoslice/source_context_executor.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot(
    *,
    repo: Path,
    commit: str,
    output: Path,
    profile_id: str = "lidousha",
) -> dict[str, object]:
    repo = repo.resolve(strict=True)
    if output.exists():
        raise FileExistsError(f"snapshot output already exists: {output}")
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autoslice-eval-", dir=output.parent) as temp_raw:
        # macOS exposes the same temp tree through both /var and /private/var.
        # ChannelProfile resolves its repo root, so canonicalize this side too
        # before computing repository-relative manifest/asset paths.
        temp = Path(temp_raw).resolve()
        archive = temp / "snapshot.tar"
        with archive.open("wb") as sink:
            completed = subprocess.run(
                ["git", "archive", "--format=tar", resolved, *ARCHIVE_ROOTS],
                cwd=repo,
                check=False,
                stdout=sink,
                stderr=subprocess.PIPE,
            )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode("utf-8", "replace"))
        tree = temp / "tree"
        tree.mkdir()
        with tarfile.open(archive, "r") as bundle:
            bundle.extractall(tree, filter="data")
        missing = [
            relative for relative in BASE_REQUIRED_PATHS if not (tree / relative).is_file()
        ]
        if missing:
            raise RuntimeError("committed eval snapshot missing required paths: " + ", ".join(missing))
        try:
            profile = load_channel_profile(
                tree,
                profile_id=profile_id,
                environ={},
            )
        except ChannelProfileError as exc:
            raise RuntimeError(f"invalid committed channel profile: {exc}") from exc
        missing_runtime = profile.missing_runtime_paths()
        if missing_runtime:
            missing_relative = [
                path.relative_to(tree).as_posix() for path in missing_runtime
            ]
            raise RuntimeError(
                "committed eval snapshot missing required profile paths: "
                + ", ".join(missing_relative)
            )
        required_paths = [
            *BASE_REQUIRED_PATHS,
            profile.manifest_path.relative_to(tree).as_posix(),
            *(
                path.relative_to(tree).as_posix()
                for path in profile.asset_files.values()
            ),
            *(
                path.relative_to(tree).as_posix()
                for path in profile.asset_directories.values()
            ),
            *(path.relative_to(tree).as_posix() for path in profile.tools.values()),
        ]
        try:
            voiceprint_document = json.loads(
                profile.asset_file("voiceprint_profile").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"invalid committed speaker profile: {exc}") from exc
        if not isinstance(voiceprint_document, dict):
            raise RuntimeError("invalid committed speaker profile: root must be an object")
        files = {
            path.relative_to(tree).as_posix(): "sha256:" + sha256_file(path)
            for path in sorted(tree.rglob("*"))
            if path.is_file()
        }
        manifest: dict[str, object] = {
            "schema_version": "autoslice-eval-snapshot.v1",
            "commit": resolved,
            "profile_id": profile_id,
            "archive_roots": list(ARCHIVE_ROOTS),
            "required_paths": required_paths,
            "files": files,
        }
        (tree / "EVAL_SNAPSHOT_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tree, output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--profile", default="lidousha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_snapshot(
        repo=args.repo,
        commit=args.commit,
        output=args.output,
        profile_id=args.profile,
    )
    print(json.dumps({"output": str(args.output), "commit": manifest["commit"], "files": len(manifest["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
