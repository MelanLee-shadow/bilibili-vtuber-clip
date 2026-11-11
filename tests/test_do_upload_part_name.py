"""The upload wrapper names the single part after the title via a hard link.

biliup 0.2.4 has no part-name option, so the wrapper links the reviewed video
as ``<title>.mp4`` in a hidden temp directory beside it.  A fake uploader
records what it received; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts/do_upload.sh"
DESCRIPTION = "假简介"
TAGS = "标签甲,标签乙"

FAKE_UPLOADER = """#!/bin/bash
printf 'x' >> "$ISOLATED_CALLS"
printf '%s\\0' "$@" > "$ISOLATED_ARGS"
link="$4"
if [ -f "$link" ] && [ ! -L "$link" ] && [ "$link" -ef "$ISOLATED_VIDEO" ]; then
  printf same > "$ISOLATED_SAME"
fi
printf '%s' "$(dirname "$link")" > "$ISOLATED_LINKDIR"
if [ -n "${ISOLATED_SLEEP:-}" ]; then
  : > "$ISOLATED_STARTED"
  sleep "$ISOLATED_SLEEP"
fi
echo 'ResponseData { code: 0, data: Some(Object {"bvid": String("BV1Mug46EEQz")}) }'
exit "${ISOLATED_RC:-0}"
"""


class Rig:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.package = tmp_path / "package dir"
        self.package.mkdir()
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        (self.workdir / "biliup_cookies.json").write_text("{}")
        self.video = self.package / "内部编号文件名.mp4"
        self.video.write_bytes(b"reviewed video bytes")
        self.cover = self.package / "cover.png"
        self.cover.write_bytes(b"cover")
        self.binary = tmp_path / "fake-biliup"
        self.binary.write_text(FAKE_UPLOADER)
        self.binary.chmod(0o700)
        self.calls = tmp_path / "calls"
        self.args_file = tmp_path / "args"
        self.same = tmp_path / "same"
        self.linkdir = tmp_path / "linkdir"
        self.started = tmp_path / "started"
        self.env = {
            **os.environ,
            "AUTHORIZED_UPLOAD": "1",
            "AUTOSLICE_UPLOAD_WORKDIR": str(self.workdir),
            "AUTOSLICE_BILIUP_BIN": str(self.binary),
            "AUTOSLICE_UPLOAD_LOG": str(tmp_path / "log"),
            "ISOLATED_CALLS": str(self.calls),
            "ISOLATED_ARGS": str(self.args_file),
            "ISOLATED_VIDEO": str(self.video),
            "ISOLATED_SAME": str(self.same),
            "ISOLATED_LINKDIR": str(self.linkdir),
            "ISOLATED_STARTED": str(self.started),
        }

    def command(self, title: str) -> list[str]:
        return ["bash", str(WRAPPER), str(self.video), str(self.cover), title, TAGS, DESCRIPTION]

    def run(self, title: str, **env) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(title), env={**self.env, **env}, capture_output=True, text=True, check=False)

    def received(self) -> list[str]:
        return self.args_file.read_bytes().decode("utf-8").split("\0")[:-1]

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.package.iterdir() if path.name.startswith(".upload-"))


def _protocol(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout.splitlines()[-1].split("=", 1)[1])


AWKWARD_TITLES = [
    "【李豆沙】全角［方括号］！还有？问号",
    "带 空格 的 标题",
    "含$HOME与`反引号`和*星号*",
    "含'单引号'与\"双引号\"",
    "a;b&c|d(e)<f>g\\h",
    "x" * 251,
    "字" * 83,  # 249 bytes; with .mp4 exactly under the limit
]


@pytest.mark.parametrize("title", AWKWARD_TITLES)
@pytest.mark.parametrize("rc", ["0", "7"])
def test_part_file_name_is_the_title_hard_linked_and_cleaned_up(tmp_path, title, rc):
    rig = Rig(tmp_path)
    original = rig.video.read_bytes()
    result = rig.run(title, ISOLATED_RC=rc)
    assert result.returncode == int(rc)
    assert _protocol(result)["dispatch_state"] == "DISPATCHED"
    assert rig.calls.read_text() == "x"
    received = rig.received()
    link = received[received.index("upload") + 1]
    assert Path(link).name == title + ".mp4"  # literally the title, one argument
    assert Path(link).stem == title
    assert rig.same.read_text() == "same"  # same inode as the reviewed video at call time
    assert received[received.index("--title") + 1] == title
    assert Path(link).parent.name.startswith(".upload-")
    assert Path(link).parent.parent == rig.package.resolve()
    assert not Path(rig.linkdir.read_text()).exists()
    assert rig.leftovers() == []
    assert rig.video.read_bytes() == original and rig.video.name == "内部编号文件名.mp4"
    assert sorted(path.name for path in rig.package.iterdir()) == ["cover.png", "内部编号文件名.mp4"]


@pytest.mark.parametrize(
    "title",
    ["", "含/斜杠", "-开头的减号", ".开头的点", "字" * 84, "x" * 252, "含\n换行", "含\t制表", "含\x7f删除符", "含\r回车"],
)
def test_unusable_part_file_name_is_refused_before_the_uploader_runs(tmp_path, title):
    rig = Rig(tmp_path)
    result = rig.run(title)
    assert result.returncode == 65
    assert _protocol(result) == {
        "schema_version": "authorized-upload-dispatch.v1", "dispatch_state": "NOT_DISPATCHED", "rc": 65}
    assert "part file name" in result.stderr
    assert not rig.calls.exists()
    assert rig.leftovers() == []


def test_hard_link_failure_refuses_without_symlink_or_copy_fallback(tmp_path):
    rig = Rig(tmp_path)
    rig.package.chmod(0o500)  # the link directory cannot be created beside the video
    try:
        result = rig.run("普通标题")
    finally:
        rig.package.chmod(0o700)
    assert result.returncode == 74
    assert _protocol(result)["dispatch_state"] == "NOT_DISPATCHED"
    assert not rig.calls.exists()
    assert rig.leftovers() == []


def test_link_that_is_not_a_hard_link_is_refused(tmp_path):
    rig = Rig(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    # A shim "ln" that makes a copy: the wrapper must notice and refuse.
    (shim / "ln").write_text('#!/bin/bash\nshift\ncp "$1" "$2"\n')
    (shim / "ln").chmod(0o700)
    result = rig.run("普通标题", PATH=f"{shim}:{os.environ['PATH']}")
    assert result.returncode == 74
    assert not rig.calls.exists()
    assert rig.leftovers() == []


def test_temp_directory_is_removed_when_the_wrapper_is_signalled(tmp_path):
    rig = Rig(tmp_path)
    process = subprocess.Popen(
        rig.command("被打断的标题"), env={**rig.env, "ISOLATED_SLEEP": "30"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not rig.started.exists():
            assert time.monotonic() < deadline, "fake uploader never started"
            time.sleep(0.05)
        assert len(rig.leftovers()) == 1
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    assert rig.leftovers() == []
