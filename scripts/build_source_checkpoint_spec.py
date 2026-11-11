#!/usr/bin/env python3
# brief-contract: writes <output-spec> <output-spec.parent>/ ; reads: <parent-spec> <native-source-checkpoint-closure> <source-media> ; effects: create-only-derived-spec ; expects_change: none
"""Create an isolated ordinary producer spec from its parent's frozen SOURCE.

No provider calls, release verdict, publication or original-input mutation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice.source_checkpoint_parent import build_source_checkpoint_successor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output-spec", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = build_source_checkpoint_successor(
        parent_spec_path=args.parent_spec, output_root=args.output_root)
    args.output_spec.parent.mkdir(parents=True, exist_ok=True)
    with args.output_spec.open("x", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"candidate_id": spec["candidate_id"], "spec": str(args.output_spec),
                      "release_authorized": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
