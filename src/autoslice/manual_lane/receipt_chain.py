"""The receipt chain of a published manuscript: from what the registry row binds back to the first-publication receipt.

The registry row of a published manuscript binds one receipt (`publication_reconciliation.authority`, a {path, sha256, bytes}):
the first-publication receipt as long as the manuscript was never repaired, afterwards the completion receipt of the latest
repair - `same-bv-repair-completed.v1` (the video and cover were replaced on the same BV) or
`same-bv-cover-repair-completed.v1` (only the cover was).  Step 8 of a successor builds its same-BV authority from the FIRST
receipt (new_bv_repair_authority), so it walks the chain back: a completion receipt binds the manifest of its repair
({path, sha256}), whose top-level `recovery_publication_authority` binds the receipt that repair was built from
(`source_publication`), and so on until a receipt of the first-publication schema.  Any link that is missing, differs from the
sha256 its referrer recorded, or has a schema not known here stops with a ReceiptChainError that names the link.

What the walk also says, for the upload tool and for `revision.json`:
- `predecessor_completed`: the latest completion that changed the video (`same-bv-repair-completed.v1`), the file that
  `repair-plan` / `cover-repair-plan` take as `--predecessor-completed`, with its `new_cid`: the row's own receipt when that is
  one, else (a cover completion) the one its plan names as its predecessor_completion, else none;
- `predecessor_plan_authority`: the `recovery_publication_authority` of that completion's plan.  `repair-plan
  --predecessor-completed` accepts a new repair only when its authority equals that one byte for byte (a migration is
  allowed only for the registry-built authorities its policy lists, not for these), so the caller says whether the authority it
  built equals it; the authority carries the final title, so a second repair with another title is not accepted;
- the video and cover the manuscript has online now (`online_video_sha256`, `online_cover_sha256`): from the latest video
  completion's `replacement`, the cover from the row's own receipt (a cover completion's `new_cover`); None where no repair
  has changed it, which is the first publication's (the caller reads that from the first manifest);
- the tags and description online now (`online_tags`, `online_desc`): the final ones the row's own completion recorded in its
  plan (`target_metadata` of a video repair, `unchanged_metadata` of a cover repair), None where the row binds the first
  receipt (`first_manifest_meta`) or where the plan records none (then they are not known from the chain, and the caller
  stops rather than guesses).

- the packages that went online (`used_packages`): the sealed package of the first publication and of every completed repair the
  chain passed (the packages their manifests attest), which is what makes a package "used online" and so not to be redone.

Only this module and `title_history.serve` (which runs it where the receipts are readable) read these files."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FIRST_SCHEMA = "new-bv-publication-reconciliation-authority.v1"
VIDEO_COMPLETED = "same-bv-repair-completed.v1"
COVER_COMPLETED = "same-bv-cover-repair-completed.v1"
MAX_LINKS = 16


class ReceiptChainError(ValueError):
    """A link of the chain cannot be followed; the message says which and why."""


@dataclass(frozen=True)
class Chain:
    first_receipt: Path
    bound: Path
    predecessor_completed: Path | None = None
    predecessor_new_cid: int | None = None
    online_video_sha256: str | None = None
    online_cover_sha256: str | None = None
    predecessor_plan_authority: dict | None = None      # the recovery_publication_authority of the predecessor's plan
    online_tags: tuple[str, ...] | None = None          # the final tags of the completion the row binds (None: see the docstring)
    online_desc: str | None = None                      # its final description
    completions: tuple[Path, ...] = ()                  # every completion receipt the walk passed, and the video completion a cover repair followed


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _load(binding: object, what: str) -> tuple[Path, dict]:
    """The file a {path, sha256[, bytes]} binding names, checked against that sha256, as a JSON object."""
    if not isinstance(binding, Mapping) or not binding.get("path"):
        raise ReceiptChainError(f"{what}: the binding is missing (没有绑定)")
    path = Path(str(binding["path"]))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ReceiptChainError(f"{what}: is missing or not a regular file (文件缺失): {path}")
    if _plain(binding.get("sha256")) != _sha(path) or (
            "bytes" in binding and binding["bytes"] != path.stat().st_size):
        raise ReceiptChainError(f"{what}: differs from the sha256 recorded for it (sha 不符): {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReceiptChainError(f"{what}: is not readable JSON ({exc}): {path}") from exc
    if not isinstance(value, dict):
        raise ReceiptChainError(f"{what}: is not a JSON object: {path}")
    return path, value


def _completed(value: dict, path: Path, schema: str) -> None:
    if value.get("status") != "VERIFIED_FRESH_LIVE" or value.get("rc") != 0:
        raise ReceiptChainError(f"{path} ({schema}) is not a verified, successful completion (status {value.get('status')!r})")


def _video_completed(path: Path, value: dict) -> tuple[int, str]:
    """(new_cid, the replacement video's sha256) of a `same-bv-repair-completed.v1`."""
    _completed(value, path, VIDEO_COMPLETED)
    new_cid, replacement = value.get("new_cid"), value.get("replacement")
    video = replacement.get("video") if isinstance(replacement, Mapping) else None
    if type(new_cid) is not int or not isinstance(video, Mapping) or not _plain(video.get("sha256")):
        raise ReceiptChainError(f"{path}: the completion carries no new_cid or replacement video (缺 new_cid / replacement.video)")
    return new_cid, _plain(video["sha256"])


def _plan_authority(path: Path, value: dict) -> dict:
    """The publication authority of the plan the video completion `value` completed."""
    _plan_path, plan = _load(value.get("plan"), f"the plan of {path.name}")
    authority = plan.get("recovery_publication_authority")
    if not isinstance(authority, dict):
        raise ReceiptChainError(f"the plan of {path.name} carries no recovery_publication_authority (plan 没有修订授权)")
    return authority


def _tags(value: object) -> tuple[str, ...] | None:
    return tuple(value) if isinstance(value, list) and value and all(isinstance(t, str) and t for t in value) else None


def _plan_meta(path: Path, value: dict, field: str) -> tuple[tuple[str, ...] | None, str | None]:
    """(final tags, final description) the plan of the completion `value` recorded in its `field`; None for what it records not."""
    _plan_path, plan = _load(value.get("plan"), f"the plan of {path.name}")
    meta = plan.get(field) if isinstance(plan.get(field), Mapping) else {}
    desc = meta.get("desc")
    return _tags(meta.get("tags")), desc if isinstance(desc, str) else None


def first_manifest_meta(first_receipt: Path) -> tuple[tuple[str, ...], str | None]:
    """(tags, description) of the upload manifest a first-publication receipt binds as its evidence."""
    _path, receipt = _load({"path": str(first_receipt), "sha256": _sha(first_receipt)}, "the first-publication receipt")
    evidence = receipt.get("evidence")
    _manifest_path, manifest = _load(evidence.get("manifest") if isinstance(evidence, Mapping) else None,
                                     "the first upload manifest")
    tags = _tags(manifest.get("tags"))
    if tags is None:
        raise ReceiptChainError(f"{_manifest_path}: the first upload manifest records no tags (没有标签)")
    desc = manifest.get("description")
    return tags, desc if isinstance(desc, str) else None


def _replacement_cover(value: dict, path: Path) -> str:
    cover = (value.get("replacement") or {}).get("cover") if isinstance(value.get("replacement"), Mapping) else None
    if not isinstance(cover, Mapping) or not _plain(cover.get("sha256")):
        raise ReceiptChainError(f"{path}: the completion carries no replacement cover (缺 replacement.cover)")
    return _plain(cover["sha256"])


def _cover_completed(path: Path, value: dict) -> str:
    """The new cover's sha256 of a `same-bv-cover-repair-completed.v1`."""
    _completed(value, path, COVER_COMPLETED)
    cover = value.get("new_cover")
    if not isinstance(cover, Mapping) or not _plain(cover.get("sha256")):
        raise ReceiptChainError(f"{path}: the cover completion carries no new_cover (缺 new_cover)")
    return _plain(cover["sha256"])


def _predecessor_of_cover(path: Path, value: dict) -> tuple[Path, dict] | None:
    """The video completion the cover repair was planned after (its plan's predecessor_completion), or None when it was
    planned on the first publication."""
    _plan_path, plan = _load(value.get("plan"), f"the plan of {path.name}")
    entry = plan.get("predecessor_completion")
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise ReceiptChainError(f"the plan of {path.name}: predecessor_completion is not an object")
    return _load(entry.get("completed"), f"the predecessor completion of {path.name}")


def resolve(binding: object) -> Chain:
    """Walk the chain from the registry row's binding to the first-publication receipt (see the module docstring)."""
    bound, value = _load(binding, "the receipt the registry row binds")
    path, link = bound, value
    predecessor: Path | None = None
    plan_authority: dict | None = None
    new_cid: int | None = None
    video_sha: str | None = None
    cover_sha: str | None = None
    tags: tuple[str, ...] | None = None
    desc: str | None = None
    completions: list[Path] = []
    seen: set[Path] = set()
    for _ in range(MAX_LINKS):
        if path in seen:
            raise ReceiptChainError(f"the receipt chain returns to {path} (回执链成环)")
        seen.add(path)
        schema = link.get("schema_version")
        if schema == FIRST_SCHEMA:
            return Chain(first_receipt=path, bound=bound, predecessor_completed=predecessor, predecessor_new_cid=new_cid,
                         online_video_sha256=video_sha, online_cover_sha256=cover_sha, predecessor_plan_authority=plan_authority,
                         online_tags=tags, online_desc=desc, completions=tuple(completions))
        if schema == VIDEO_COMPLETED:
            completions.append(path)
            cid, sha = _video_completed(path, link)
            if path == bound:
                predecessor, new_cid, video_sha, cover_sha = path, cid, sha, _replacement_cover(link, path)
                plan_authority = _plan_authority(path, link)
                tags, desc = _plan_meta(path, link, "target_metadata")
        elif schema == COVER_COMPLETED:
            completions.append(path)
            if path == bound:
                cover_sha = _cover_completed(path, link)
                tags, desc = _plan_meta(path, link, "unchanged_metadata")
                before = _predecessor_of_cover(path, link)
                if before is not None:
                    before_path, before_value = before
                    if before_value.get("schema_version") != VIDEO_COMPLETED:
                        raise ReceiptChainError(f"{before_path}: the predecessor of a cover completion must be a "
                                                f"{VIDEO_COMPLETED}, not {before_value.get('schema_version')!r} (前序回执 schema 不认识)")
                    new_cid, video_sha = _video_completed(before_path, before_value)
                    predecessor, plan_authority = before_path, _plan_authority(before_path, before_value)
                    completions.append(before_path)
            else:
                _cover_completed(path, link)
        else:
            raise ReceiptChainError(f"{path}: schema {schema!r} is not a receipt this lane knows (回执 schema 不认识)")
        manifest_path, manifest = _load(link.get("manifest"), f"the manifest of {path.name}")
        authority = manifest.get("recovery_publication_authority")
        if not isinstance(authority, Mapping):
            raise ReceiptChainError(f"{manifest_path}: the manifest carries no recovery_publication_authority (manifest 没有修订授权)")
        path, link = _load(authority.get("source_publication"), f"the receipt that the repair of {path.name} was built from")
    raise ReceiptChainError(f"the receipt chain is longer than {MAX_LINKS} links without reaching the first publication")


def _package_of(binding: object, what: str) -> dict[str, object]:
    """{package_root, review_manifest: {path, sha256} | None} of the package the manifest bound by `binding` attests."""
    path, manifest = _load(binding, what)
    attestation = manifest.get("package_attestation")
    root = attestation.get("package_root") if isinstance(attestation, Mapping) else None
    if not isinstance(root, str) or not root:
        raise ReceiptChainError(f"{path}: the manifest names no package (package_attestation.package_root) (没有成片包绑定)")
    sealed = attestation.get("review_manifest")
    review = {"path": str(sealed["path"]), "sha256": _plain(sealed["sha256"])} if (
        isinstance(sealed, Mapping) and sealed.get("path") and _plain(sealed.get("sha256"))) else None
    return {"package_root": root, "review_manifest": review}


def used_packages(chain: Chain) -> list[dict[str, object]]:
    """The packages that went online for this chain's manuscript, each as {package_root, review_manifest: {path, sha256} | None}
    (what the manifest of the publication attests): the first publication's, and that of every completed repair in the chain.
    ReceiptChainError naming the link when one cannot be read or attests no package: the caller must not take that for 'none'."""
    _path, first = _load({"path": str(chain.first_receipt), "sha256": _sha(chain.first_receipt)}, "the first-publication receipt")
    evidence = first.get("evidence")
    packages = [_package_of(evidence.get("manifest") if isinstance(evidence, Mapping) else None, "the first upload manifest")]
    for done in chain.completions:
        _path, value = _load({"path": str(done), "sha256": _sha(done)}, f"the completion receipt {done.name}")
        packages.append(_package_of(value.get("manifest"), f"the manifest of {done.name}"))
    return packages
