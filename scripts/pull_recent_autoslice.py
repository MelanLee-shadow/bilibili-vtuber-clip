#!/usr/bin/env python3
"""Pull only recent, non-archived autoslice delivery dates from free."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_ROOT = PROJECT_ROOT / "lidousha"
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports/slice_monitor/autoslice_free"
DEFAULT_STATE_DIR = Path.home() / "Library/Application Support/lidousha-autoslice-pull"
DEFAULT_REMOTE_ROOT = "/opt/bilive/autoslice/repo/lidousha"
DEFAULT_REPORTS_REMOTE = "/opt/bilive/autoslice/reports"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_iso_date(value: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise ValueError(f"invalid ISO date: {value}")
    return date.fromisoformat(value)


def read_archived_dates(path: Path) -> set[date]:
    if not path.exists():
        return set()
    archived: set[date] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            archived.add(parse_iso_date(value))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return archived


def write_archived_dates(path: Path, archived: Iterable[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{value.isoformat()}\n" for value in sorted(set(archived)))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def select_recent_dates(
    remote_names: Iterable[str],
    *,
    today: date,
    days: int,
    archived: set[date],
) -> list[date]:
    if days < 1:
        raise ValueError("days must be at least 1")
    cutoff = today - timedelta(days=days - 1)
    # Remote date dirs are named by the Beijing recording date, which runs up
    # to a full day ahead of this Mac's local clock — a stream that starts
    # after Beijing midnight lands in a "tomorrow" dir that must still sync.
    horizon = today + timedelta(days=1)
    selected: set[date] = set()
    for name in remote_names:
        try:
            candidate = parse_iso_date(name.strip())
        except ValueError:
            continue
        if cutoff <= candidate <= horizon and candidate not in archived:
            selected.add(candidate)
    return sorted(selected)


def run_checked(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another autoslice pull is still running") from exc
        yield


def list_remote_dates(host: str, remote_root: str) -> list[str]:
    result = run_checked(
        [
            "ssh",
            host,
            "find",
            remote_root,
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-print",
        ]
    )
    return [line.rstrip("/").rsplit("/", 1)[-1] for line in result.stdout.splitlines()]


def _normalized(name: str) -> str:
    # APFS may hand back decomposed Unicode for names Finder touched; compare
    # both sides in NFC so a composed remote twin is not misread as local-only.
    try:
        return unicodedata.normalize("NFC", name)
    except ValueError:
        return name


def list_remote_subdirs(host: str, remote_dir: str) -> set[str]:
    """Relative paths of every directory below remote_dir."""
    result = subprocess.run(
        ["ssh", host, "find", remote_dir, "-mindepth", "1", "-type", "d", "-print0"],
        check=True,
        stdout=subprocess.PIPE,
    )
    prefix = remote_dir.rstrip("/") + "/"
    subdirs: set[str] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", "surrogateescape")
        if path.startswith(prefix):
            subdirs.add(path[len(prefix) :])
    return subdirs


def local_only_dirs(root: Path, remote_subdirs: set[str]) -> list[str]:
    """Topmost directories below root that have no remote counterpart."""
    remote = {_normalized(rel) for rel in remote_subdirs}
    found: list[str] = []
    for current, dirnames, _ in os.walk(root):
        base = os.path.relpath(current, root)
        shared: list[str] = []
        for name in sorted(dirnames):
            rel = name if base == "." else f"{base}/{name}"
            if _normalized(rel) in remote:
                shared.append(name)
            else:
                found.append(rel)
        dirnames[:] = shared
    return found


def delete_protection_excludes(rel_paths: Iterable[str]) -> list[str]:
    return [
        "--exclude=/" + re.sub(r"([*?\[])", r"\\\1", rel) + "/"
        for rel in rel_paths
    ]


def pull_date(host: str, remote_root: str, local_root: Path, value: date) -> None:
    date_name = value.isoformat()
    destination = local_root / date_name
    destination.mkdir(parents=True, exist_ok=True)
    remote_dir = f"{remote_root.rstrip('/')}/{date_name}"
    # The flat namespace of a date dir is a machine-owned mirror: files the
    # runner deleted or quarantined remotely must not survive locally as if
    # still deliverable (three stale pre-fix clip sets lingered
    # next to the fresh rerun).  Local-only subdirectories are human review
    # packages (e.g. 正式补切-*/) that must survive --delete.  macOS openrsync
    # forwards "--filter=protect */" to the sender as a plain exclude, which
    # silently drops every remote subdirectory from the transfer (
    # 舞台切片-* never synced), so the protection is spelled as anchored
    # --exclude rules for the dirs that are actually local-only — semantics
    # openrsync and GNU rsync agree on.
    protected = local_only_dirs(destination, list_remote_subdirs(host, remote_dir))
    if protected:
        print(f"  protecting local-only: {', '.join(protected)}", flush=True)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--delete",
            *delete_protection_excludes(protected),
            "--timeout=120",
            f"{host}:{remote_dir}/",
            f"{destination}/",
        ],
        check=True,
    )


def pull_reports(host: str, remote_root: str, local_root: Path) -> None:
    local_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--timeout=60",
            f"{host}:{remote_root.rstrip('/')}/",
            f"{local_root}/",
        ],
        check=True,
    )


def command_pull(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser()
    archive_path = state_dir / "archived-dates.txt"
    with exclusive_lock(state_dir / "pull.lock"):
        archived = read_archived_dates(archive_path)
        today = parse_iso_date(args.today) if args.today else date.today()
        remote_names = list_remote_dates(args.host, args.remote_root)
        selected = select_recent_dates(
            remote_names, today=today, days=args.days, archived=archived
        )
        print(
            "autoslice pull:"
            f" today={today.isoformat()} days={args.days}"
            f" selected={','.join(value.isoformat() for value in selected) or '(none)'}"
            f" archived={','.join(value.isoformat() for value in sorted(archived)) or '(none)'}",
            flush=True,
        )
        if args.dry_run:
            return 0
        for value in selected:
            print(f"pulling {value.isoformat()}", flush=True)
            pull_date(args.host, args.remote_root, Path(args.local_root), value)
        if not args.no_reports:
            print("pulling reports", flush=True)
            pull_reports(args.host, args.reports_remote, Path(args.reports_local))
    return 0


def command_archive(args: argparse.Namespace) -> int:
    value = parse_iso_date(args.date)
    state_dir = Path(args.state_dir).expanduser()
    archive_path = state_dir / "archived-dates.txt"
    with exclusive_lock(state_dir / "pull.lock"):
        archived = read_archived_dates(archive_path)
        archived.add(value)
        write_archived_dates(archive_path, archived)
        print(f"archived {value.isoformat()} in {archive_path}")
        if args.delete_local:
            local_date = Path(args.local_root).expanduser() / value.isoformat()
            if local_date.is_symlink():
                local_date.unlink()
            elif local_date.exists():
                shutil.rmtree(local_date)
            print(f"deleted local directory {local_date}")
    return 0


def command_unarchive(args: argparse.Namespace) -> int:
    value = parse_iso_date(args.date)
    state_dir = Path(args.state_dir).expanduser()
    archive_path = state_dir / "archived-dates.txt"
    with exclusive_lock(state_dir / "pull.lock"):
        archived = read_archived_dates(archive_path)
        archived.discard(value)
        write_archived_dates(archive_path, archived)
    print(f"unarchived {value.isoformat()}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    archive_path = Path(args.state_dir).expanduser() / "archived-dates.txt"
    for value in sorted(read_archived_dates(archive_path)):
        print(value.isoformat())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pull_parser = subparsers.add_parser("pull", help="pull recent non-archived dates")
    pull_parser.add_argument("--days", type=int, default=3)
    pull_parser.add_argument("--today", help=argparse.SUPPRESS)
    # 远端 runner 宿主是部署专属信息，用户自己决定——必填，不给任何人的主机名当默认。
    pull_parser.add_argument("--host", required=True, help="runner 宿主 ssh 别名/地址")
    pull_parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    pull_parser.add_argument("--local-root", default=str(DEFAULT_LOCAL_ROOT))
    pull_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    pull_parser.add_argument("--reports-remote", default=DEFAULT_REPORTS_REMOTE)
    pull_parser.add_argument("--reports-local", default=str(DEFAULT_REPORTS_ROOT))
    pull_parser.add_argument("--no-reports", action="store_true")
    pull_parser.add_argument("--dry-run", action="store_true")
    pull_parser.set_defaults(func=command_pull)

    archive_parser = subparsers.add_parser(
        "archive", help="persistently skip a date, optionally deleting its local copy"
    )
    archive_parser.add_argument("date")
    archive_parser.add_argument("--delete-local", action="store_true")
    archive_parser.add_argument("--local-root", default=str(DEFAULT_LOCAL_ROOT))
    archive_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    archive_parser.set_defaults(func=command_archive)

    unarchive_parser = subparsers.add_parser("unarchive", help="allow a date to sync again")
    unarchive_parser.add_argument("date")
    unarchive_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    unarchive_parser.set_defaults(func=command_unarchive)

    list_parser = subparsers.add_parser("list-archived", help="show skipped dates")
    list_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    list_parser.set_defaults(func=command_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"autoslice pull failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
