#!/usr/bin/env python3
"""Install private 李豆沙 enrollment WAVs against a versioned hash profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(path: Path) -> str:
    root = path.resolve(strict=True)
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    if not files:
        raise SystemExit(f"model directory contains no files: {path}")
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_digest = bytes.fromhex(sha256(item))
        digest.update(len(file_digest).to_bytes(8, "big"))
        digest.update(file_digest)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--model-source-dir", type=Path)
    parser.add_argument("--model-target-dir", type=Path)
    args = parser.parse_args()

    import sys

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from src.autoslice.channel_profile import load_channel_profile

    channel_profile = load_channel_profile(repo_root)
    expected_schema = f"{channel_profile.profile_id}-voiceprint-profile.v1"
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    if profile.get("schema_version") != expected_schema or profile.get("configuration_status") != "READY":
        raise SystemExit(
            f"voiceprint profile is not production READY (need schema {expected_schema})"
        )
    references = profile.get("references")
    if not isinstance(references, list) or len(references) != 3:
        raise SystemExit("voiceprint profile must bind exactly three references")
    args.target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.target_dir, 0o700)
    for reference in references:
        filename = str(reference.get("filename") or "")
        expected = str(reference.get("sha256") or "").removeprefix("sha256:").lower()
        if not filename or Path(filename).name != filename or len(expected) != 64:
            raise SystemExit("unsafe or incomplete voiceprint reference entry")
        target = args.target_dir / filename
        if target.is_file() and sha256(target) == expected:
            os.chmod(target, 0o600)
            continue
        source = args.source_dir / filename
        if not source.is_file() or sha256(source) != expected:
            raise SystemExit(f"missing or hash-mismatched enrollment source: {source}")
        with tempfile.NamedTemporaryFile(dir=args.target_dir, prefix=f".{filename}.", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            if sha256(temporary) != expected:
                raise SystemExit(f"copied enrollment hash mismatch: {filename}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    for reference in references:
        target = args.target_dir / str(reference["filename"])
        print(f"{reference['id']} {sha256(target)} {target}")
    if (args.model_source_dir is None) != (args.model_target_dir is None):
        raise SystemExit("--model-source-dir and --model-target-dir must be supplied together")
    if args.model_source_dir is not None and args.model_target_dir is not None:
        expected_model_sha = str((profile.get("model") or {}).get("tree_sha256") or "").lower()
        if len(expected_model_sha) != 64:
            raise SystemExit("voiceprint profile model tree hash is missing")
        source_model_sha = sha256_directory(args.model_source_dir)
        if source_model_sha != expected_model_sha:
            raise SystemExit(f"hash-mismatched model source: {args.model_source_dir}")
        args.model_target_dir.parent.mkdir(parents=True, exist_ok=True)
        if args.model_target_dir.exists():
            if sha256_directory(args.model_target_dir) != expected_model_sha:
                raise SystemExit(f"existing model target is hash-mismatched: {args.model_target_dir}")
        else:
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{args.model_target_dir.name}.", dir=args.model_target_dir.parent)
            )
            try:
                shutil.copytree(args.model_source_dir, temporary, dirs_exist_ok=True)
                if sha256_directory(temporary) != expected_model_sha:
                    raise SystemExit("copied model tree hash mismatch")
                os.chmod(temporary, 0o700)
                for item in temporary.rglob("*"):
                    os.chmod(item, 0o700 if item.is_dir() else 0o600)
                os.replace(temporary, args.model_target_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        print(f"model {expected_model_sha} {args.model_target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
