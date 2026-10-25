"""Derive Python, shell and compose recording paths from one topology JSON.

The JSON sits beside this module in both repository and installed locations.
Loading the definition performs no media or network I/O.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
from string import Template

CONFIG_NAME = "oci3-recording-topology.json"
CONFIG_RELATIVE = Path("ops/recording") / CONFIG_NAME


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {label}")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or value == "/":
        raise ValueError(f"{label} must be a normalized absolute path")
    if ":" in value:  # Docker short volume syntax delimiter
        raise ValueError(f"{label} contains a volume delimiter")
    return Path(value)


class RecordingTopology:
    def __init__(self, document: dict):
        required = {"schema_version", "spool_root", "cloud_mount", "archive_root",
                    "n100_archive_root", "backup_root", "rooms", "consumer_mounts",
                    "state_dir", "legacy_state_dirs"}
        if not isinstance(document, dict) or set(document) != required:
            raise ValueError("recording topology keys differ from the contract")
        if document["schema_version"] != "oci3-recording-topology.v1":
            raise ValueError("unknown recording topology schema")
        for name in ("spool_root", "cloud_mount", "archive_root", "n100_archive_root",
                     "backup_root", "state_dir"):
            setattr(self, name, _absolute(document[name], name))
        rooms = document["rooms"]
        if (not isinstance(rooms, list) or not rooms
                or any(not isinstance(r, str) or re.fullmatch(r"[1-9][0-9]*", r) is None for r in rooms)
                or len(set(rooms)) != len(rooms)):
            raise ValueError("rooms must be a nonempty list of distinct numeric room IDs")
        self.rooms = tuple(rooms)
        self.room = self.rooms[0]  # current single-room adapter/runner entry
        mounts = document["consumer_mounts"]
        if not isinstance(mounts, dict) or set(mounts) != {"bilive", "bililive-adapter", "bililive-recorder"}:
            raise ValueError("exactly the three recording consumers must have mounts")
        self.consumer_mounts = {name: _absolute(value, name) for name, value in mounts.items()}
        if len(set(self.consumer_mounts.values())) != 3:
            raise ValueError("consumer mount points must be distinct")
        legacy = document["legacy_state_dirs"]
        if not isinstance(legacy, list) or not legacy:
            raise ValueError("legacy state directories must be listed")
        self.legacy_state_dirs = tuple(_absolute(p, "legacy state directory") for p in legacy)
        if (self.state_dir in self.legacy_state_dirs
                or not self.state_dir.is_relative_to(self.legacy_state_dirs[0])):
            raise ValueError("local recording must use a fresh ledger")
        roots = (self.archive_root, self.n100_archive_root, self.backup_root)
        if any(not p.is_relative_to(self.cloud_mount) or p == self.cloud_mount for p in roots):
            raise ValueError("cloud roots must be below the configured mount")
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for i, a in enumerate(roots) for b in roots[i + 1:]):
            raise ValueError("OCI3 archive, N100 archive and backup must be separate")
        if (self.spool_root.is_relative_to(self.cloud_mount)
                or self.cloud_mount.is_relative_to(self.spool_root)
                or self.spool_root.is_relative_to(self.state_dir)
                or self.state_dir.is_relative_to(self.spool_root)
                or "云盘" in self.spool_root.parts):
            raise ValueError("spool must be local and separate from cloud/ledger roots")
        if "115open" not in self.archive_root.relative_to(self.cloud_mount).parts:
            raise ValueError("final archive must remain on 115")
        if "云盘" not in self.backup_root.relative_to(self.cloud_mount).parts:
            raise ValueError("backup must remain on 123")

    @property
    def spool_room(self) -> Path:
        return self.spool_root / self.room

    @property
    def archive_room(self) -> Path:
        return self.archive_root / self.room

    @property
    def archive_state(self) -> Path:
        return self.state_dir / "archive-state.json"

    @property
    def coverage_states(self) -> tuple[Path, ...]:
        return tuple(p / "adapter-state.json" for p in (*self.legacy_state_dirs, self.state_dir))

    def environment(self) -> dict[str, str]:
        return {
            "AUTOSLICE_RECORDING_BIND_ROOT": str(self.spool_root),
            "AUTOSLICE_RECORDING_SPOOL_ROOM": str(self.spool_room),
            "AUTOSLICE_RECORDING_ARCHIVE_ROOT": str(self.archive_root),
            "AUTOSLICE_RECORDING_ARCHIVE_ROOM": str(self.archive_room),
            "AUTOSLICE_RECORDING_N100_ROOT": str(self.n100_archive_root),
            "AUTOSLICE_RECORDING_BACKUP_ROOT": str(self.backup_root),
            "AUTOSLICE_RECORDING_CLOUD_MOUNT": str(self.cloud_mount),
            "AUTOSLICE_RECORDING_ROOM": self.room,
            "AUTOSLICE_RECORDING_ROOMS": " ".join(self.rooms),
            "AUTOSLICE_RECORDING_STATE_DIR": str(self.state_dir),
            "AUTOSLICE_RECORDING_ARCHIVE_STATE": str(self.archive_state),
            "AUTOSLICE_RECORD_HEALTH_STATUS_PATH": str(self.state_dir / "status.json"),
            "AUTOSLICE_RECORD_HEALTH_STATE_PATH": str(self.state_dir / "adapter-state.json"),
            "AUTOSLICE_RECORDING_BILIVE_MOUNT": str(self.consumer_mounts["bilive"]),
            "AUTOSLICE_RECORDING_ADAPTER_MOUNT": str(self.consumer_mounts["bililive-adapter"]),
            "AUTOSLICE_RECORDING_RECORDER_MOUNT": str(self.consumer_mounts["bililive-recorder"]),
            "AUTOSLICE_RECORDING_ADAPTER_ROOM": str(self.consumer_mounts["bililive-adapter"] / self.room),
            "AUTOSLICE_RECORDING_ADAPTER_STATE_DIR": str(
                Path("/state") / self.state_dir.relative_to(self.legacy_state_dirs[0])),
        }

    def shell(self) -> str:
        return "# Derived from ops/recording/oci3-recording-topology.json; do not edit.\n" + "".join(
            f"{key}={shlex.quote(value)}\n" for key, value in self.environment().items())

    def render_compose(self, template: str) -> str:
        # All placeholders occur inside JSON/YAML double quoted scalars.
        values = {k: json.dumps(v, ensure_ascii=False)[1:-1] for k, v in self.environment().items()}
        return Template(template).substitute(values)

    def validate_compose(self, compose: str, requested: str = "") -> list[str]:
        if requested and requested != str(self.spool_root):
            raise ValueError("--recording-root differs from the topology bind root")
        scalars = [line.strip()[2:] for line in compose.splitlines() if line.strip().startswith("- ")]
        scalars = [json.loads(s) if s.startswith('"') else s for s in scalars]
        videos = [s for s in scalars if s.endswith(":rshared")]
        expected = [f"{self.spool_root}:{mount}:rshared" for mount in self.consumer_mounts.values()]
        if sorted(videos) != sorted(expected):
            raise ValueError("compose must bind exactly the three local topology Videos mounts")
        if "/opt/bilive/recording:/state:rw" not in scalars:
            raise ValueError("compose must keep the adapter ledger bind")
        container_state = Path("/state") / self.state_dir.relative_to(self.legacy_state_dirs[0])
        for flag, value in (("--record-root", self.consumer_mounts["bililive-adapter"] / self.room),
                            ("--room", self.room),
                            ("--status-path", container_state / "status.json"),
                            ("--state-path", container_state / "adapter-state.json"),
                            ("--webhook-journal", container_state / "webhook-events.jsonl"),
                            ("--archive-state-path", container_state / "archive-state.json")):
            if (scalars.count(flag) != 1 or scalars.index(flag) + 1 >= len(scalars)
                    or scalars[scalars.index(flag) + 1] != str(value)):
                raise ValueError(f"compose adapter {flag} differs from topology")
        images = re.findall(r"^\s*image:\s*(\S+)\s*$", compose, re.MULTILINE)
        if len(images) != 3 or any(re.fullmatch(
            r"ghcr\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}", image
        ) is None for image in images):
            raise ValueError("compose images must be ghcr references pinned to a full sha256 digest")
        return sorted(set(images))


def load_topology(path: Path | None = None) -> RecordingTopology:
    if path is None:
        module = Path(__file__).resolve()
        path = module.with_name(CONFIG_NAME)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"recording topology is not a regular file: {path}")
    return RecordingTopology(json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shell", action="store_true")
    mode.add_argument("--compose", type=Path)
    mode.add_argument("--layout", type=Path)
    parser.add_argument("--recording-root", default="")
    args = parser.parse_args()
    topology = load_topology(args.config)
    if args.shell:
        print(topology.shell(), end="")
    else:
        path = args.compose or args.layout
        compose = topology.render_compose(path.read_text(encoding="utf-8"))
        images = topology.validate_compose(compose, args.recording_root)
        if args.compose:
            print(compose, end="")
        else:
            print(topology.spool_root)
            print(topology.state_dir)
            print(topology.room)
            print("\n".join(images))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
