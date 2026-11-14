"""The script text of each command step 10 sends to the media host, one function per command.

These are the commands the nine-clip hand run used , in the same spelling: the upload tool is run as root with
AUTOSLICE_BASE set, from the deployed repo, through `adapters.root_script` (its wrapper hands what the command wrote back to
the lane's user).  Arguments, their order and the output files follow that run's drivers; only the paths are the clip's
own.  Nothing here decides anything: the tool's own gates (hashes, ledger, registry, quota, season, public readback) are
the only gates, and they are not reimplemented."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .adapters import REMOTE_PY, REMOTE_REPO, root_script
from .layout import layout
from .step10_route import COVER, FIRST, VIDEO

UPLOAD_TOOL = "authorized_upload.py"
AUDIT_TOOL = "audit_review_package.py"


@dataclass(frozen=True)
class Job:
    route: str
    bvid: str | None
    package: Path
    out: Path                       # the clip's upload/ directory: manifest, plan, completion receipt, logs
    title: str
    tags: str                       # the comma-joined final tags (first publication only)
    predecessor: str | None
    authorized_by: str
    quote: str

    @property
    def fm(self) -> str:
        return self.package.name

    def package_file(self, suffix: str) -> Path:
        return self.package / f"{self.fm}{suffix}"

    @property
    def audit(self) -> Path:        # the first publication keeps the audit beside its manifest, a revision in the package
        return (self.out if self.route == FIRST else self.package) / "package_audit.json"

    @property
    def manifest(self) -> Path:
        return self.out / {FIRST: "authorized-upload-manifest.json", VIDEO: "replace-manifest.json", COVER: "cover-manifest.json"}[self.route]

    @property
    def _prefix(self) -> str:
        return "same-bv-repair" if self.route == VIDEO else "same-bv-cover-repair"

    @property
    def plan(self) -> Path:
        return self.out / f"{self._prefix}-plan.json"

    @property
    def completed(self) -> Path:
        return self.out / f"{self._prefix}-completed.json"

    @property
    def journal(self) -> str:
        name = "same_bv_repair_ledger.jsonl" if self.route == VIDEO else "same_bv_cover_repair_ledger.jsonl"
        return f"{layout().autoslice_base}/reports/{name}"

    @property
    def uploaded(self) -> Path:     # the first publication's completion receipt, written by the upload command
        return self.manifest.with_name(self.manifest.name + ".uploaded.json")

    def sub(self, name: str) -> str:
        """The upload tool's subcommand of this route: repair-plan / cover-repair-plan, ..."""
        return name if self.route == VIDEO else f"cover-{name}"


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _tool(job: Job, script: str, args: Sequence[object], redirect: str) -> str:
    cmd = (f"cd {_q(REMOTE_REPO)} && env AUTOSLICE_BASE={_q(layout().autoslice_base)} "
           f"{shlex.join([REMOTE_PY, '-B', f'scripts/{script}', *map(str, args)])} {redirect}")
    return root_script(cmd.replace("'", "'\\''"), env=(), owned=str(job.out), recursive=True)


def _log(job: Job, name: str) -> str:
    return f"> {_q(job.out / name)} 2>&1"


def audit(job: Job) -> str:
    """Seal check, then the package audit as root (JSON to package_audit.json, its stderr to package_audit.err)."""
    seal = job.package / "review_manifest.json"
    return (f"[ -f {_q(seal)} ] || {{ echo NOT_SEALED {_q(job.package)}; exit 2; }}\n"
            f"rm -f {_q(job.package / 'package_audit.json')}\nmkdir -p {_q(job.out)}\n"
            + _tool(job, AUDIT_TOOL, [job.package, "--json"], f"> {_q(job.out / 'package_audit.json')} 2> {_q(job.out / 'package_audit.err')}"))


def make_manifest(job: Job) -> str:
    args: list[object] = ["make-manifest", "--video", job.package_file(".mp4"), "--cover", job.package_file(".cover.png"),
                          "--package-audit", job.audit, "--title", job.title]
    if job.route == FIRST:
        args += ["--tags", job.tags]
    else:
        args += ["--final-human-review", job.package_file(".finished-media-review.json")]
    return _tool(job, UPLOAD_TOOL, [*args, "--authorized-by", job.authorized_by, "--quote", job.quote, "--out", job.manifest],
                 _log(job, "make-manifest.log"))


def verify_manifest(job: Job) -> str:
    return _tool(job, UPLOAD_TOOL, ["verify", "--manifest", job.manifest], _log(job, "verify.log"))


def plan(job: Job, *, dry_run: bool) -> str:
    args: list[object] = [job.sub("repair-plan"), "--manifest", job.manifest, "--bvid", job.bvid or "", "--out", job.plan, "--journal", job.journal]
    if job.route == VIDEO:
        args.append("--preserve-existing-tags")
    if job.predecessor:
        args += ["--predecessor-completed", job.predecessor]
    if dry_run:
        args.append("--dry-run")
    return _tool(job, UPLOAD_TOOL, args, _log(job, "plan-dry.log" if dry_run else "plan.log"))


def status(job: Job) -> str:
    return _tool(job, UPLOAD_TOOL, [job.sub("repair-status"), "--plan", job.plan, "--journal", job.journal], _log(job, "status.log"))


def run(job: Job, *, log: str, dry_run: bool = False) -> str:
    args: list[object] = [job.sub("repair-run"), "--plan", job.plan, "--journal", job.journal]
    return _tool(job, UPLOAD_TOOL, [*args, *(["--dry-run"] if dry_run else [])], _log(job, log))


def verify_live(job: Job) -> str:
    return _tool(job, UPLOAD_TOOL, [job.sub("repair-verify-live"), "--plan", job.plan, "--journal", job.journal, "--out", job.completed],
                 _log(job, "verify-live.log"))


def upload(job: Job) -> str:
    return _tool(job, UPLOAD_TOOL, ["upload", "--manifest", job.manifest], _log(job, "upload.log"))


def duration(path: Path) -> str:
    """Seconds of the finished video, as the media host's ffprobe reads it (not root: the package is the lane user's)."""
    return f"ffprobe -v error -show_entries format=duration -of csv=p=0 {_q(path)}\n"
