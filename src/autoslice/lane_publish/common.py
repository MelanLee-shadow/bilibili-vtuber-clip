"""What every subcommand shares: refusals with step 10's return codes, sha256 bindings, JSON files, where things are."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OK, REFUSED, GATE, LOCK, FAILED, NOT_YET, NOT_PUBLIC, QUOTA = 0, 2, 3, 4, 5, 6, 7, 8
LOCK_BUSY = "shared upload/repair lock is busy"      # step10_run.LOCK_BUSY: these words make a 4 a wait, not a failure


class Refused(Exception):
    """Stop with this return code and these words (printed on stderr by the CLI)."""

    def __init__(self, rc: int, words: str):
        super().__init__(words)
        self.rc, self.words = rc, words


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def bind(path: Path) -> dict[str, Any]:
    """{path, sha256, bytes} of a regular file, by its absolute path."""
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise Refused(REFUSED, f"不是普通文件：{path}")
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}


def drift(binding: object, what: str) -> str | None:
    """Why the file `binding` names is no longer those bytes, or None."""
    if not isinstance(binding, Mapping) or not binding.get("path"):
        return f"{what}：没有绑定"
    path = Path(str(binding["path"]))
    if path.is_symlink() or not path.is_file():
        return f"{what}：文件不在了：{path}"
    if sha256(path) != str(binding.get("sha256") or "").removeprefix("sha256:"):
        return f"{what}：文件变了（sha256 不符）：{path}"
    if "bytes" in binding and binding["bytes"] != path.stat().st_size:
        return f"{what}：文件大小变了：{path}"
    return None


def read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(REFUSED, f"读不到{what} {path}：{type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise Refused(REFUSED, f"{what} {path} 不是 JSON 对象")
    return value


def write_json(path: Path, value: object) -> None:
    """Write whole or not at all (a reader never sees half a file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def base() -> Path:
    """The runtime base on this host: AUTOSLICE_BASE (step 10 sets it), else the profile layout's."""
    if os.environ.get("AUTOSLICE_BASE"):
        return Path(os.environ["AUTOSLICE_BASE"])
    from src.autoslice.manual_lane.layout import layout

    return Path(layout().autoslice_base)


@dataclass(frozen=True)
class Config:
    """This host's uploader: `<base>/lane_publish.json` {"biliup_bin": ..., "biliup_cookies": ...}.  The cookie file is the
    one biliup writes at `biliup login` (it is read here only to sign the creator-centre calls; it is never copied or printed)."""

    biliup_bin: Path
    cookies: Path

    @classmethod
    def load(cls) -> Config:
        path = base() / "lane_publish.json"
        doc = read_json(path, "本机上传配置")
        try:
            config = cls(biliup_bin=Path(doc["biliup_bin"]), cookies=Path(doc["biliup_cookies"]))
        except (KeyError, TypeError) as exc:
            raise Refused(REFUSED, f"{path} 要写 biliup_bin 和 biliup_cookies：缺 {exc}") from exc
        if not (config.biliup_bin.is_file() and os.access(config.biliup_bin, os.X_OK)):
            raise Refused(REFUSED, f"biliup 不可执行：{config.biliup_bin}（{path} 的 biliup_bin）")
        if not config.cookies.is_file():
            raise Refused(REFUSED, f"上传账号的 cookie 文件不在：{config.cookies}（先 biliup login；{path} 的 biliup_cookies）")
        return config


@dataclass(frozen=True)
class Publish:
    """lane_rules.json upload.publish: the archive policy of a new BV and how long to wait for Bilibili."""

    tid: int
    copyright: int
    source: str
    public_wait_s: float
    public_poll_s: float
    new_part_wait_s: float

    @classmethod
    def load(cls) -> Publish:
        from src.autoslice.manual_lane.lane_rules import rules

        try:
            doc = rules("upload")["publish"]
            return cls(tid=int(doc["tid"]), copyright=int(doc["copyright"]), source=str(doc["source"]),
                       public_wait_s=float(doc["public_wait_s"]), public_poll_s=float(doc["public_poll_s"]),
                       new_part_wait_s=float(doc["new_part_wait_s"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Refused(REFUSED, f"lane_rules.json upload.publish 不完整：{type(exc).__name__}: {exc}") from exc
