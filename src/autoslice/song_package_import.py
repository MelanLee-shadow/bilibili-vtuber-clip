"""Import one externally produced **song review package** into free.

谈话切的 :mod:`src.autoslice.package_import` 吃的是生产树
（``out/{date}/{cid}/replacement_recuts``）：它按 ``{cid}.*`` 的文件名约定读三份
可变文档，再把运行期定位符**根重基**到本机。歌切的成品**形状根本不同**——

* 交付物是 ``build_lidousha_song_review_manifest.v1`` 压平改名后的**评审包**：
  11 个按投稿 stem 命名的交付件 + ``delivery.manifest.json`` +
  ``review_manifest.json`` + ``package_audit.json``；
* 身份是分裂的：``record.candidate_id == publish.candidate_id`` 是**选择器**候选
  （``seededsong_*``），投稿身份是 ``record.delivery_candidate_id``；
* 投影权威是**改名映射**（delivery manifest 逐 role 记
  ``{path, sha256, source_path, source_sha256}``），不是根重基；
* 落地面是 ``repo/lidousha/{date}/`` 扁平交付 + state 的 ``songs`` 行，不是
  ``out/.../replacement_recuts`` + ``picks``。

**为什么不需要扩重定位契约（2026-08-10 实测）**：上传面
（``authorized_upload._strict_verified_song_package`` /
``_validate_v3_package_attestation``）对这个包**只按包内 basename + sha 消费**，
delivery manifest 里的绝对路径一个都不解析；全包唯一钉死目的地的绝对路径是
``package_audit.json.root``，而 ``authorized_upload`` 本来就会在目的地现跑
``audit_package(root)`` 重算。所以包内每一个字节都可以**原样冻结**搬运，只有
``package_audit.json`` 必须在落地根**重新生成**（本模块因此不搬它）。
冻结件里留着的生产机路径是**出处**，不是可用定位符：谁真去解析都会 ENOENT，
方向是 fail-closed。

fail-closed：歌切自己的证据链（歌词对齐 / host-vocal 声纹自证 / recut manifest
/ 交付 manifest / 三份封面复现件）在这里逐条重验，任何一条不成立就 typed 拒绝，
不因为"谈话的门不适用"整段跳过。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from src.autoslice.package_import import (
    CopyItem,
    PackageImportError,
    _iter_source_files,
    _regular_file,
    declared_digest,
    load_json_document,
    require_directory,
    require_regular_file,
    sha256_file,
)
from src.autoslice.publication_registry import cover_maintenance_block_reason


SONG_PACKAGE_IMPORT_SCHEMA_VERSION = "external-song-package-import.v1"
SONG_REVIEW_MANIFEST_SCHEMA = "lidousha-song-review-manifest.v1"
SONG_REVIEW_MANIFEST_GENERATOR = "build_lidousha_song_review_manifest.v1"
SONG_REVIEW_PACKAGE_STATUS = (
    "finished_review_package_no_upload_pending_human_review"
)
VERIFIED_SONG_DELIVERY_SCHEMA = "verified-song-delivery.v1"
PACKAGE_AUDIT_NAME = "package_audit.json"
REVIEW_MANIFEST_NAME = "review_manifest.json"

# 必须与 ``build_lidousha_song_review_manifest.REQUIRED_ROLES`` 逐字一致：那是
# 产这个包的人；两边分家就会出现"包里有、导入不认"的静默缺件。
# tests/test_song_package_import.py 有一条平价断言钉死这一点。
SONG_ROLE_SUFFIXES: dict[str, str] = {
    "video": ".mp4",
    "subtitle": ".srt",
    "lyrics_alignment_report": ".lyrics-alignment-report.json",
    "host_vocal_proof": ".host-vocal-proof.json",
    "recut_manifest": ".recut.manifest.json",
    "active_record": ".record.json",
    "publish": ".publish.json",
    "cover": ".cover.png",
    "cover_title_mask": ".cover.title-mask.png",
    "cover_pre_overlay": ".cover.pre-overlay.png",
    "cover_route_background": ".cover.route-background.png",
}

# 冻结证人的判据全集，与 ``authorized_upload._strict_verified_song_package``
# 同一组常量。任何一条不等就是 typed 拒绝。
_COMPLETION_REQUIRED = {
    "ready": True,
    "reason_codes": [],
    "song_boundary_status": "FULL_SONG_READY",
    "lyrics_alignment_status": "READY",
    "host_vocal_status": "READY",
    "live_performance_status": "READY",
    "live_performance_mode": "LIVE_STREAMER_SINGING",
    "joint_singing_decision": "VERIFIED_LIDOUSHA_SINGING",
    "subtitle_source": "external_lrc_global_shift",
}
_COMPLETION_ARTIFACT_HASHES = {
    "video": "burned_preview_sha256",
    "lyrics_alignment_report": "alignment_report_sha256",
    "host_vocal_proof": "host_vocal_proof_sha256",
    "recut_manifest": "recut_manifest_sha256",
}

# 批级状态：**故意与谈话的白名单不同**。谈话那份镜像的是 free 现跑的
# ``build_lidousha_daily_review_manifest``；歌切的评审 manifest 在 free 上永远
# 重建不了（``selector_summary_path`` 只存在于产它那台机），而
# ``authorized_upload`` 根本不读 state。于是真实约束只剩两条：runner 不在写
# picks（``processing``），以及这一批已经走到人可审阅的终态。本导入存在的理由
# 恰恰是"那天已经收官了，成品还在别的机器上"，所以 ``published*`` 必须准入。
REVIEWABLE_SONG_BATCH_STATUSES = frozenset(
    {
        "review_ready",
        "review_ready_with_failures",
        "review_ready_retry_wait",
        "publication_in_progress",
        "ready_unpublished",
        "ready_unpublished_with_failures",
        "published",
        "published_with_failures",
    }
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


def song_lane_import_hint() -> str:
    return (
        "song review packages are imported by "
        "src.autoslice.song_package_import.plan_song_import "
        "(scripts/import_external_song_package.py) — they are flattened "
        "delivery packages, not a replacement_recuts production tree"
    )


def refuse_if_song_review_package(source_package_dir: Path) -> None:
    """Refuse early, and by name, when a song review package is handed to talk.

    ``package_import.read_package_documents`` would otherwise die on the talk
    file-name convention (``{cid}.record.json``) and report a missing document
    instead of "wrong lane, wrong entry point".
    """

    manifest_path = Path(source_package_dir) / REVIEW_MANIFEST_NAME
    if not _regular_file(manifest_path):
        return
    manifest, _ = load_json_document(manifest_path, label="review manifest")
    if manifest.get("schema_version") != SONG_REVIEW_MANIFEST_SCHEMA:
        return
    raise PackageImportError(
        "LANE_NOT_SUPPORTED",
        f"{manifest_path} declares {SONG_REVIEW_MANIFEST_SCHEMA}; this entry "
        "point only covers the talk lane",
        hint=song_lane_import_hint(),
    )


# --------------------------------------------------------------------------
# package documents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SongPackageDocuments:
    """One verified song review package, read straight off the source bytes."""

    candidate_id: str
    source_candidate_id: str
    stem: str
    date: str
    title: str
    package_root: Path
    review: dict[str, Any]
    review_item: dict[str, Any]
    review_sha256: str
    delivery: dict[str, Any]
    delivery_sha256: str
    delivery_authority: dict[str, Any]
    completion: dict[str, Any]
    record: dict[str, Any]
    publish: dict[str, Any]
    role_paths: dict[str, Path]
    role_sha256: dict[str, str]

    @property
    def delivery_manifest_name(self) -> str:
        return f"{self.stem}.delivery.manifest.json"


def _require(condition: bool, code: str, detail: str, *, hint: str = "") -> None:
    if not condition:
        raise PackageImportError(code, detail, hint=hint)


def _mapping(value: object, *, code: str, label: str) -> dict[str, Any]:
    _require(isinstance(value, Mapping), code, f"{label} is not an object")
    return dict(value)  # type: ignore[arg-type]


def _read_review_manifest(
    package_root: Path, candidate_id: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    path = package_root / REVIEW_MANIFEST_NAME
    review, payload = load_json_document(path, label="song review manifest")
    _require(
        review.get("schema_version") == SONG_REVIEW_MANIFEST_SCHEMA
        and review.get("generated_by") == SONG_REVIEW_MANIFEST_GENERATOR
        and review.get("status") == SONG_REVIEW_PACKAGE_STATUS
        and review.get("classification") == "Song"
        and review.get("run_mode") == "PRODUCTION_REVIEW"
        and review.get("upload_allowed") is False
        and review.get("story_contract_required") is False,
        "SONG_REVIEW_MANIFEST_INVALID",
        f"{path} is not a canonical no-upload song review manifest",
        hint="only the song review builder's own envelope is importable; a "
        "hand-assembled package is refused",
    )
    _require(
        str(review.get("candidate_id") or "") == candidate_id,
        "CANDIDATE_MISMATCH",
        f"review manifest declares candidate_id={review.get('candidate_id')!r}, "
        f"import was asked for {candidate_id!r}",
    )
    items = review.get("items")
    _require(
        isinstance(items, list) and len(items) == 1 and isinstance(items[0], Mapping),
        "SONG_REVIEW_MANIFEST_INVALID",
        "song review manifest must carry exactly one item",
    )
    item = dict(items[0])  # type: ignore[index]
    _require(
        item.get("classification") == "Song"
        and item.get("kind") == "song"
        and str(item.get("candidate_id") or "") == candidate_id
        and str(item.get("id") or candidate_id) == candidate_id,
        "SONG_REVIEW_MANIFEST_INVALID",
        "song review item is not this candidate's song entry",
    )
    return review, item, sha256_file(path) if payload else sha256_file(path)


def _verified_stem(item: Mapping[str, Any]) -> str:
    """The投稿 stem is read, never recomputed.

    ``_song_delivery_basename`` ran ``safe_name`` on the producing host and the
    result is not a pure function of the title (the 《心型病毒 (Live)》 package's
    stem lost its ``(Live)》`` tail).  Recomputing it here would name files the
    package does not contain.
    """

    stem = str(item.get("stem") or "")
    _require(
        bool(stem)
        and not stem.startswith(".")
        and "/" not in stem
        and "\\" not in stem
        and PurePosixPath(stem).name == stem,
        "SONG_PACKAGE_STEM_UNSAFE",
        f"review item declares an unsafe delivery stem: {stem!r}",
    )
    return stem


def _verify_role_files(
    package_root: Path,
    *,
    stem: str,
    item: Mapping[str, Any],
    delivery_artifacts: Mapping[str, Any],
) -> tuple[dict[str, Path], dict[str, str]]:
    """Every delivery role must be one regular file whose bytes三方一致。

    三方 = review item ``sha256`` 表、delivery manifest 的 ``sha256`` 与
    ``source_sha256``、以及磁盘上的真实字节。改名映射就是靠这三方闭合来证明
    "投稿 stem 下的这份，正是生产树里那份"。
    """

    item_hashes = _mapping(
        item.get("sha256"), code="SONG_PACKAGE_INCOMPLETE", label="review item sha256"
    )
    paths: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for role, suffix in sorted(SONG_ROLE_SUFFIXES.items()):
        path = require_regular_file(
            package_root / f"{stem}{suffix}", label=f"song {role}"
        )
        actual = sha256_file(path)
        entry = _mapping(
            delivery_artifacts.get(role),
            code="SONG_DELIVERY_MANIFEST_INVALID",
            label=f"delivery artifact {role}",
        )
        declared_item = declared_digest(
            item_hashes.get(role), label=f"review item {role}"
        )
        declared_delivered = declared_digest(
            entry.get("sha256"), label=f"delivery {role}"
        )
        declared_source = declared_digest(
            entry.get("source_sha256"), label=f"delivery {role} source"
        )
        _require(
            actual == declared_item == declared_delivered == declared_source,
            "SONG_ARTIFACT_SHA_DRIFT",
            f"{role}: bytes={actual} review_item={declared_item} "
            f"delivery={declared_delivered} source={declared_source} path={path}",
            hint="the package is internally inconsistent; do not import it",
        )
        # 名字与 sha 必须一起动：review item 的角色文件名也要指回同一份。
        declared_name = item.get(role)
        if isinstance(declared_name, str) and declared_name:
            _require(
                PurePosixPath(declared_name).name == path.name,
                "SONG_ARTIFACT_NAME_DRIFT",
                f"review item names {declared_name!r} for {role}, package holds "
                f"{path.name!r}",
            )
        paths[role] = path
        digests[role] = actual
    return paths, digests


def _verify_delivery_manifest(
    package_root: Path,
    *,
    stem: str,
    candidate_id: str,
    item: Mapping[str, Any],
    review: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    name = f"{stem}.delivery.manifest.json"
    _require(
        str(item.get("delivery_manifest") or "") == name,
        "SONG_DELIVERY_MANIFEST_INVALID",
        f"review item names {item.get('delivery_manifest')!r} as the delivery "
        f"manifest, expected {name!r}",
    )
    path = package_root / name
    delivery, _ = load_json_document(path, label="song delivery manifest")
    digest = sha256_file(path)
    _require(
        delivery.get("schema_version") == VERIFIED_SONG_DELIVERY_SCHEMA
        and delivery.get("status") == "DELIVERED_NO_UPLOAD"
        and delivery.get("upload_enabled") is False
        and str(delivery.get("candidate_id") or "") == candidate_id,
        "SONG_DELIVERY_MANIFEST_INVALID",
        f"{path} is not this candidate's no-upload verified delivery marker",
    )
    authority = _mapping(
        review.get("delivery_authority"),
        code="SONG_DELIVERY_AUTHORITY_INVALID",
        label="review delivery_authority",
    )
    _require(
        authority.get("schema_version") == VERIFIED_SONG_DELIVERY_SCHEMA
        and str(authority.get("manifest") or "") == name
        and declared_digest(
            authority.get("manifest_sha256"), label="delivery authority manifest"
        )
        == digest,
        "SONG_DELIVERY_AUTHORITY_INVALID",
        "review manifest's delivery_authority does not bind the package's own "
        "delivery manifest bytes",
    )
    artifacts = _mapping(
        delivery.get("artifacts"),
        code="SONG_DELIVERY_MANIFEST_INVALID",
        label="delivery artifacts",
    )
    absent = _mapping(
        delivery.get("absent_artifacts"),
        code="SONG_DELIVERY_MANIFEST_INVALID",
        label="delivery absent_artifacts",
    )
    _require(
        not (set(absent) & set(SONG_ROLE_SUFFIXES)),
        "SONG_PACKAGE_INCOMPLETE",
        "the delivery manifest declares a required song artifact absent: "
        + ",".join(sorted(set(absent) & set(SONG_ROLE_SUFFIXES))),
    )
    return delivery, digest, artifacts


def _verify_song_proof_chain(
    *,
    authority: Mapping[str, Any],
    role_paths: Mapping[str, Path],
    role_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Re-verify the frozen song proofs — the歌切 equivalent of talk's gates."""

    completion = _mapping(
        authority.get("song_completion_evidence"),
        code="SONG_COMPLETION_EVIDENCE_INVALID",
        label="delivery authority song_completion_evidence",
    )
    mismatched = sorted(
        key for key, expected in _COMPLETION_REQUIRED.items()
        if completion.get(key) != expected
    )
    _require(
        not mismatched,
        "SONG_COMPLETION_EVIDENCE_INVALID",
        "frozen song completion proof is not delivery-ready at "
        + ",".join(mismatched),
        hint="a song without a complete boundary/lyrics/host-vocal proof is "
        "never importable; re-produce it, do not import it",
    )
    for role, key in sorted(_COMPLETION_ARTIFACT_HASHES.items()):
        _require(
            declared_digest(completion.get(key), label=f"completion {key}")
            == role_sha256[role],
            "SONG_COMPLETION_EVIDENCE_INVALID",
            f"completion proof {key} does not bind the packaged {role}",
        )

    alignment, _ = load_json_document(
        role_paths["lyrics_alignment_report"], label="lyrics alignment report"
    )
    _require(
        alignment.get("schema_version") == "lyrics-alignment-report.v1",
        "SONG_LYRICS_ALIGNMENT_INVALID",
        "packaged lyrics alignment report is not lyrics-alignment-report.v1",
    )
    host_proof, _ = load_json_document(
        role_paths["host_vocal_proof"], label="host vocal proof"
    )
    _require(
        host_proof.get("schema_version") == "host-vocal-proof.v3"
        and host_proof.get("status") == "READY"
        and host_proof.get("decision")
        == "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS",
        "SONG_HOST_VOCAL_PROOF_INVALID",
        "packaged host-vocal proof does not prove Lidousha sang the lyric "
        "checkpoints",
    )
    recut, _ = load_json_document(
        role_paths["recut_manifest"], label="recut manifest"
    )
    binding = recut.get("verified_output_binding")
    artifacts = binding.get("artifacts") if isinstance(binding, Mapping) else None
    proofs = binding.get("proofs") if isinstance(binding, Mapping) else None
    _require(
        recut.get("schema_version") == "materialized-recut.v2"
        and recut.get("status") == "MATERIALIZED"
        and recut.get("reason_codes") == []
        and recut.get("subtitle_source") == "external_lrc_global_shift"
        and isinstance(binding, Mapping)
        and binding.get("schema_version") == "verified-song-output-binding.v1"
        and isinstance(artifacts, Mapping)
        and isinstance(proofs, Mapping),
        "SONG_RECUT_MANIFEST_INVALID",
        "packaged recut manifest lacks the verified song output binding",
    )
    bound = {
        "video": artifacts.get("burned_media_sha256"),  # type: ignore[union-attr]
        "subtitle": artifacts.get("subtitle_sha256"),  # type: ignore[union-attr]
        "lyrics_alignment_report": proofs.get(  # type: ignore[union-attr]
            "lyrics_alignment_report_sha256"
        ),
        "host_vocal_proof": proofs.get("host_vocal_proof_sha256"),  # type: ignore[union-attr]
    }
    for role, declared in sorted(bound.items()):
        _require(
            declared_digest(declared, label=f"recut binding {role}")
            == role_sha256[role],
            "SONG_RECUT_MANIFEST_INVALID",
            f"recut output binding does not bind the packaged {role}",
        )
    return completion


def _verify_documents(
    *,
    role_paths: Mapping[str, Path],
    role_sha256: Mapping[str, str],
    candidate_id: str,
    title: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    record, _ = load_json_document(role_paths["active_record"], label="song record")
    publish, _ = load_json_document(role_paths["publish"], label="song publish")
    _require(
        str(record.get("delivery_candidate_id") or "") == candidate_id,
        "CANDIDATE_MISMATCH",
        "packaged record declares delivery_candidate_id="
        f"{record.get('delivery_candidate_id')!r}, import was asked for "
        f"{candidate_id!r}",
    )
    source_candidate_id = str(record.get("source_candidate_id") or "")
    _require(
        bool(_SAFE_ID_RE.fullmatch(source_candidate_id))
        and str(publish.get("candidate_id") or "") == source_candidate_id,
        "CANDIDATE_MISMATCH",
        "record/publish disagree on the selector candidate id "
        f"(record={source_candidate_id!r} publish={publish.get('candidate_id')!r})",
        hint="song identity is split: the投稿 id lives in "
        "record.delivery_candidate_id, the selector id in candidate_id",
    )
    _require(
        publish.get("schema_version") == "shadow-publish-draft.v1"
        and publish.get("upload_enabled") is False
        and publish.get("title") == title,
        "SONG_PUBLISH_DRAFT_INVALID",
        "packaged publish draft is not the frozen no-upload draft for this title",
    )
    staging = _mapping(
        record.get("publish_staging"),
        code="SONG_RECORD_INVALID",
        label="record.publish_staging",
    )
    _require(
        staging.get("title") == title and staging.get("upload_enabled") is False,
        "TITLE_SURFACE_DRIFT",
        "record.publish_staging and the review manifest disagree on the frozen "
        "title / no-upload binding",
    )
    hashes = _mapping(
        record.get("artifact_hashes"),
        code="SONG_RECORD_INVALID",
        label="record.artifact_hashes",
    )
    for role, key in (
        ("video", "burned_video_sha256"),
        ("subtitle", "subtitle_sha256"),
        ("cover", "cover_sha256"),
    ):
        _require(
            declared_digest(hashes.get(key), label=f"record {key}")
            == role_sha256[role],
            "SONG_ARTIFACT_SHA_DRIFT",
            f"record.artifact_hashes.{key} does not bind the packaged {role}",
        )
    return record, publish, source_candidate_id


def read_song_package(
    package_root: Path, candidate_id: str
) -> SongPackageDocuments:
    """Load and fully re-verify one song review package.  Fail-closed."""

    package_root = require_directory(package_root, label="song package")
    review, item, review_sha256 = _read_review_manifest(package_root, candidate_id)
    date = str(review.get("date") or "")
    _require(
        bool(_DATE_RE.fullmatch(date)),
        "SONG_REVIEW_MANIFEST_INVALID",
        f"review manifest declares an unusable recording date: {date!r}",
    )
    stem = _verified_stem(item)
    delivery, delivery_sha256, artifacts = _verify_delivery_manifest(
        package_root,
        stem=stem,
        candidate_id=candidate_id,
        item=item,
        review=review,
    )
    role_paths, role_sha256 = _verify_role_files(
        package_root, stem=stem, item=item, delivery_artifacts=artifacts
    )
    authority = dict(review.get("delivery_authority") or {})
    completion = _verify_song_proof_chain(
        authority=authority, role_paths=role_paths, role_sha256=role_sha256
    )
    title = str(item.get("title") or "")
    _require(bool(title.strip()), "PUBLISH_TITLE_MISSING", "review item has no title")
    record, publish, source_candidate_id = _verify_documents(
        role_paths=role_paths,
        role_sha256=role_sha256,
        candidate_id=candidate_id,
        title=title,
    )
    return SongPackageDocuments(
        candidate_id=candidate_id,
        source_candidate_id=source_candidate_id,
        stem=stem,
        date=date,
        title=title,
        package_root=package_root,
        review=review,
        review_item=item,
        review_sha256=review_sha256,
        delivery=delivery,
        delivery_sha256=delivery_sha256,
        delivery_authority=authority,
        completion=completion,
        record=record,
        publish=publish,
        role_paths=role_paths,
        role_sha256=role_sha256,
    )


# --------------------------------------------------------------------------
# copy planning
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SongImportPlan:
    """Exactly which bytes move where.  ``copies`` feeds ``execute_copy``."""

    candidate_id: str
    date: str
    documents: SongPackageDocuments
    destination_package_root: Path
    destination_delivery_root: Path
    copies: tuple[CopyItem, ...]
    skipped: tuple[str, ...]

    def delivery_path(self, role: str) -> Path:
        return self.destination_delivery_root / (
            self.documents.stem + SONG_ROLE_SUFFIXES[role]
        )

    @property
    def destination_delivery_manifest(self) -> Path:
        return (
            self.destination_delivery_root / self.documents.delivery_manifest_name
        )

    @property
    def destination_review_manifest(self) -> Path:
        return self.destination_package_root / REVIEW_MANIFEST_NAME

    @property
    def destination_package_audit(self) -> Path:
        return self.destination_package_root / PACKAGE_AUDIT_NAME


def _safe_destination_root(value: Path, *, label: str, source: Path) -> Path:
    root = Path(value).absolute()
    _require(
        root.is_absolute() and ".." not in root.parts and str(root) != "/",
        "UNSAFE_RELOCATION_ROOT",
        f"{label} is not a safe absolute root: {value}",
    )
    _require(
        root != source,
        "SOURCE_IS_DESTINATION",
        f"{label} equals the source package directory ({root})",
        hint="stage the external package somewhere else; the import must be "
        "able to tell the two sides apart",
    )
    return root


def plan_song_import(
    *,
    source_package_dir: Path,
    destination_package_root: Path,
    destination_delivery_root: Path,
    candidate_id: str,
) -> SongImportPlan:
    """Verify the package, then enumerate the byte-frozen two-place landing.

    Landing is two-place because free's own songs are: the flat delivery under
    ``repo/lidousha/{date}/`` is what the ``songs`` row binds, and the review
    package root is what ``authorized_upload`` consumes.  ``package_audit.json``
    is **not** copied — its ``root`` names the producing host and the canonical
    auditor must re-run at the destination.
    """

    source = require_directory(source_package_dir, label="source package")
    documents = read_song_package(source, candidate_id)
    package_root = _safe_destination_root(
        destination_package_root, label="destination package root", source=source
    )
    delivery_root = _safe_destination_root(
        destination_delivery_root, label="destination delivery root", source=source
    )
    _require(
        package_root != delivery_root,
        "UNSAFE_RELOCATION_ROOT",
        "the review package root and the flat delivery root must differ",
    )
    if _regular_file(package_root / PACKAGE_AUDIT_NAME):
        landed_audit, _ = load_json_document(
            package_root / PACKAGE_AUDIT_NAME, label="destination package audit"
        )
        _require(
            Path(str(landed_audit.get("root") or "")).absolute() == package_root,
            "PACKAGE_AUDIT_ROOT_MISMATCH",
            "the destination already holds a package audit for another root: "
            f"{landed_audit.get('root')!r}",
            hint="archive the destination package and import again from scratch",
        )

    copies: list[CopyItem] = []
    skipped: list[str] = []
    for path in _iter_source_files(source):
        relative = str(path.relative_to(source))
        if relative.startswith(".") or "/." in relative:
            skipped.append(relative)
            continue
        if relative == PACKAGE_AUDIT_NAME:
            # 生产机的 root 断言在这里就是错的；目的地必须自己重跑审计器。
            skipped.append(relative)
            continue
        copies.append(
            CopyItem(
                source=path,
                destination=package_root / relative,
                relative=relative,
                role="review_package",
            )
        )

    # 扁平交付：先所有交付件，delivery manifest **最后**——它是这条链的持久提交
    # 标记，评审 manifest builder 还有一条 "manifest mtime ≥ 最新产物" 的检查。
    for role, suffix in sorted(SONG_ROLE_SUFFIXES.items()):
        destination = delivery_root / f"{documents.stem}{suffix}"
        copies.append(
            CopyItem(
                source=documents.role_paths[role],
                destination=destination,
                relative=destination.name,
                role=f"delivery:{role}",
            )
        )
    manifest_source = source / documents.delivery_manifest_name
    copies.append(
        CopyItem(
            source=manifest_source,
            destination=delivery_root / documents.delivery_manifest_name,
            relative=documents.delivery_manifest_name,
            role="delivery:manifest",
        )
    )
    return SongImportPlan(
        candidate_id=candidate_id,
        date=documents.date,
        documents=documents,
        destination_package_root=package_root,
        destination_delivery_root=delivery_root,
        copies=tuple(copies),
        skipped=tuple(sorted(skipped)),
    )


def verify_landed_song_delivery(plan: SongImportPlan) -> dict[str, str]:
    """Re-hash the landed delivery bytes before anything binds to them."""

    verified: dict[str, str] = {}
    for role in sorted(SONG_ROLE_SUFFIXES):
        path = require_regular_file(
            plan.delivery_path(role), label=f"landed song {role}"
        )
        actual = sha256_file(path)
        _require(
            actual == plan.documents.role_sha256[role],
            "LANDED_ARTIFACT_SHA_DRIFT",
            f"landed {role} is {actual}, package declares "
            f"{plan.documents.role_sha256[role]}",
            hint="the transport corrupted these bytes; re-stage and re-run",
        )
        verified[role] = actual
    manifest = require_regular_file(
        plan.destination_delivery_manifest, label="landed delivery manifest"
    )
    actual = sha256_file(manifest)
    _require(
        actual == plan.documents.delivery_sha256,
        "LANDED_ARTIFACT_SHA_DRIFT",
        "landed delivery manifest drifted from the package's frozen bytes",
    )
    verified["delivery_manifest"] = actual
    return verified


# --------------------------------------------------------------------------
# state binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SongStatePreconditions:
    batch_status: str
    song_index: int | None
    will_supersede: bool
    will_create_row: bool


def check_song_state_preconditions(
    before_state: Mapping[str, Any],
    *,
    candidate_id: str,
    date: str,
    project_closure,
    allow_supersede_blocked: bool,
    registry_path: Path | None = None,
) -> SongStatePreconditions:
    """Refuse every state shape this bind could not legally land on."""

    batch_status = str(before_state.get("status") or "")
    _require(
        batch_status in REVIEWABLE_SONG_BATCH_STATUSES,
        "BATCH_STATUS_NOT_REVIEWABLE",
        f"state.status={batch_status!r} is outside the song import whitelist "
        + ",".join(sorted(REVIEWABLE_SONG_BATCH_STATUSES)),
        hint="'processing' means the runner tick is currently writing rows — "
        "wait for the tick to finish",
    )
    state_date = str(before_state.get("date") or "")
    _require(
        not state_date or state_date == date,
        "STATE_DATE_MISMATCH",
        f"state declares date={state_date!r}, import was asked for {date!r}",
    )
    songs = before_state.get("songs")
    _require(
        isinstance(songs, list), "STATE_SHAPE_INVALID", "state.songs is not a list"
    )
    matches = [
        index
        for index, row in enumerate(songs)  # type: ignore[arg-type]
        if isinstance(row, Mapping) and row.get("candidate_id") == candidate_id
    ]
    _require(
        len(matches) <= 1,
        "DUPLICATE_SONG_ROW",
        f"{candidate_id} already appears {len(matches)} times in songs",
    )
    for queue in ("pending_talk", "pending_song"):
        rows = before_state.get(queue)
        _require(
            not (
                isinstance(rows, list)
                and any(
                    isinstance(row, Mapping)
                    and str(row.get("candidate_id") or row.get("cid") or "")
                    == candidate_id
                    for row in rows
                )
            ),
            "CANDIDATE_STILL_QUEUED",
            f"{candidate_id} is still queued in {queue}; the runner would "
            "produce over the import",
        )
    closure = project_closure(before_state)
    _require(
        candidate_id not in list(closure.get("published_candidate_ids") or []),
        "CANDIDATE_ALREADY_PUBLISHED",
        f"{candidate_id} is already published; import refuses to rebind a "
        "published delivery",
        hint="published candidates only accept the same-BV edit/replace lane",
    )

    # 封面维护闸口：这个包的封面 generation 血统留在产它那台机上，free 永远
    # 复算不出 ``_initial_cover_proof_valid``——于是只要这行进了 state，
    # ``repair_covers`` 就会判"需要修封面"，花图片额度重画一张已经定稿的封面，
    # 并把交付根那份覆盖掉。唯一不靠伪造就能关掉它的闸口是出版登记（也正是
    # 上传的唯一授权面），所以这里 fail-closed 要求它先被登记阻断。
    _require(
        cover_maintenance_block_reason(
            candidate_id, recording_date=date, registry_path=registry_path
        )
        is not None,
        "SONG_COVER_MAINTENANCE_UNBLOCKED",
        f"generic cover maintenance is not blocked for {candidate_id}; an "
        "imported song's cover lineage cannot be re-verified on this host, so "
        "the maintenance loop would spend image quota and overwrite the "
        "finished cover",
        hint="add the candidate to assets/lidousha/publication_registry.v1.json "
        "with status=hold_pending_review first (that is also the only upload "
        "authority); flipping it to released_for_upload later restores normal "
        "cover maintenance",
    )

    if not matches:
        return SongStatePreconditions(
            batch_status=batch_status,
            song_index=None,
            will_supersede=False,
            will_create_row=True,
        )
    row = songs[matches[0]]  # type: ignore[index]
    if isinstance(row.get("external_song_package_import"), Mapping):
        _require(
            row.get("status") == "review_ready" and row.get("rc") == 0,
            "SONG_ROW_NOT_REBINDABLE",
            f"{candidate_id} carries an import binding but is "
            f"status={row.get('status')!r} rc={row.get('rc')!r}",
        )
        return SongStatePreconditions(
            batch_status=batch_status,
            song_index=matches[0],
            will_supersede=False,
            will_create_row=False,
        )
    _require(
        allow_supersede_blocked,
        "SONG_ROW_NOT_REBINDABLE",
        f"{candidate_id} already has this host's own song row "
        f"(status={row.get('status')!r}); import never overwrites a native "
        "verdict in place",
        hint="pass --supersede-existing-row to move the existing row verbatim "
        "into song_superseded_attempts (its history is kept) and bind the "
        "imported package as a new row",
    )
    return SongStatePreconditions(
        batch_status=batch_status,
        song_index=matches[0],
        will_supersede=True,
        will_create_row=False,
    )


def _import_block(plan: SongImportPlan, *, bound_at: str) -> dict[str, Any]:
    documents = plan.documents
    producing_root = ""
    video = (documents.delivery.get("artifacts") or {}).get("video")
    if isinstance(video, Mapping) and isinstance(video.get("path"), str):
        producing_root = str(PurePosixPath(str(video["path"])).parent)
    return {
        "schema_version": SONG_PACKAGE_IMPORT_SCHEMA_VERSION,
        "status": "VERIFIED_SONG_PACKAGE_BOUND",
        "bound_at": bound_at,
        "review_package_root": str(plan.destination_package_root),
        "review_manifest_path": str(plan.destination_review_manifest),
        "review_manifest_sha256": "sha256:" + documents.review_sha256,
        "delivery_manifest_path": str(plan.destination_delivery_manifest),
        "delivery_manifest_sha256": "sha256:" + documents.delivery_sha256,
        "selector_record_candidate_id": documents.source_candidate_id,
        # 声明而不是让人发现：冻结件里的定位符是产它那台机的出处，本机不解析。
        "frozen_locator_authority": "producing_host",
        "producing_host_delivery_root": producing_root,
        "package_audit_regeneration_required": True,
        "upload_authority": "assets/lidousha/publication_registry.v1.json",
    }


def build_song_row(plan: SongImportPlan, *, bound_at: str) -> dict[str, Any]:
    """The minimal ``songs`` binding — every value read off the package.

    Deliberately **not** bound: ``cover_generation`` and ``selector_summary_path``.
    Both are locator-bearing producing-host documents; copying them into free's
    runtime state would plant paths that do not resolve here.  Their
    authoritative copies stay in the landed ``publish.json`` and in the review
    package, and the review-manifest builder's refusal on a missing selector
    summary is the correct fail-closed direction.
    """

    documents = plan.documents
    sidecars = {
        role: str(plan.delivery_path(role))
        for role in sorted(SONG_ROLE_SUFFIXES)
        if role != "video"
    }
    return {
        "candidate_id": documents.candidate_id,
        "status": "review_ready",
        "rc": 0,
        "lane": "song",
        "title": documents.title,
        "delivered": str(plan.delivery_path("video")),
        "delivered_sha256": "sha256:" + documents.role_sha256["video"],
        "video_sha256": "sha256:" + documents.role_sha256["video"],
        "delivered_sidecars": sidecars,
        "delivered_sidecar_hashes": {
            role: "sha256:" + documents.role_sha256[role] for role in sidecars
        },
        "delivery_manifest_path": str(plan.destination_delivery_manifest),
        "delivery_manifest_sha256": "sha256:" + documents.delivery_sha256,
        # 导入后默认不可上传。放行只走仓内出版登记，不在运行期改这一位。
        "delivery_upload_enabled": False,
        "cover_status": str(documents.publish.get("cover_status") or ""),
        "cover_path": str(plan.delivery_path("cover")),
        "cover_sha256": "sha256:" + documents.role_sha256["cover"],
        "song_complete": True,
        "song_completion_evidence": copy.deepcopy(documents.completion),
        "selector_summary_sha256": str(
            documents.delivery_authority.get("selector_summary_sha256") or ""
        ),
        "selector_record_candidate_id": documents.source_candidate_id,
        "external_song_package_import": _import_block(plan, bound_at=bound_at),
    }


def _superseded_entry(row: Mapping[str, Any], *, bound_at: str) -> dict[str, Any]:
    """Keep this host's own verdict verbatim; never overwrite it in place."""

    return {
        "candidate_id": str(row.get("candidate_id") or ""),
        "status": row.get("status"),
        "reason_codes": list(row.get("reason_codes") or []),
        "pipeline_fingerprint": row.get("pipeline_fingerprint"),
        "song_pipeline_fingerprint": row.get("song_pipeline_fingerprint"),
        "session_id": row.get("session_id"),
        "retry_reason": "superseded_by_external_song_package_import",
        "superseded_at": bound_at,
        "superseded_row": copy.deepcopy(dict(row)),
    }


def build_bound_song_state(
    before_state: Mapping[str, Any],
    *,
    plan: SongImportPlan,
    date: str,
    bound_at: str,
    project_closure,
    allow_supersede_blocked: bool = False,
    registry_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute the post-image of one song bind.  Pure — the caller writes it."""

    candidate_id = plan.documents.candidate_id
    preconditions = check_song_state_preconditions(
        before_state,
        candidate_id=candidate_id,
        date=date,
        project_closure=project_closure,
        allow_supersede_blocked=allow_supersede_blocked,
        registry_path=registry_path,
    )
    after_state = copy.deepcopy(dict(before_state))
    songs = after_state["songs"]
    row = build_song_row(plan, bound_at=bound_at)
    carried: dict[str, Any] = {}
    if preconditions.song_index is not None:
        previous = songs[preconditions.song_index]
        # 选择窗口是 free 自己的选题证据（包里的 start/end 是重剪内部偏移，
        # 不是场次毫秒），只从被取代的本机行逐字带走，不重算。
        for key in ("start_ms", "end_ms", "hook", "session_id", "segment"):
            if previous.get(key) is not None:
                carried[key] = copy.deepcopy(previous[key])
        if preconditions.will_supersede:
            after_state.setdefault("song_superseded_attempts", []).append(
                _superseded_entry(previous, bound_at=bound_at)
            )
            songs.pop(preconditions.song_index)
        else:
            previous_import = previous.get("external_song_package_import")
            if isinstance(previous_import, Mapping):
                replayed = dict(row["external_song_package_import"])
                replayed["bound_at"] = previous_import.get("bound_at")
                if replayed == dict(previous_import):
                    row["external_song_package_import"] = replayed
            songs.pop(preconditions.song_index)
    songs.append({**carried, **row})

    before_closure = project_closure(before_state)
    after_closure = project_closure(after_state)
    before_published = list(before_closure.get("published_candidate_ids") or [])
    after_published = list(after_closure.get("published_candidate_ids") or [])
    _require(
        before_published == after_published,
        "PUBLICATION_MEMBERSHIP_MOVED",
        "the bind would change the day's published candidate set: "
        f"{before_published} -> {after_published}",
    )
    for key in ("picks", "pending_talk", "pending_song"):
        _require(
            after_state.get(key) == before_state.get(key),
            "STATE_COLLECTION_MUTATED",
            f"the bind changed {key}",
        )
    _require(
        len(
            [
                candidate
                for candidate in after_state["songs"]
                if isinstance(candidate, Mapping)
                and candidate.get("candidate_id") == candidate_id
            ]
        )
        == 1,
        "DUPLICATE_SONG_ROW",
        f"the bind left {candidate_id} in songs more than once",
    )
    # 后置断言要真咬得住：只要当日闭包是可用的（那天已经发过东西，正是本导入
    # 存在的场景），这一行必须被认成"已就绪、未发布"。日后若行形状改到闭包把
    # 它归进 unresolved，这里立刻红，而不是等审阅时才发现成品在台账上消失。
    # 当天一条都没发布时闭包整体 NOT_APPLICABLE（它只投影**发布**闭包），
    # 此时无可断言。
    if str(after_closure.get("status") or "") != "NOT_APPLICABLE":
        _require(
            candidate_id
            in list(after_closure.get("ready_unpublished_candidate_ids") or [])
            and candidate_id
            not in list(after_closure.get("unresolved_candidate_ids") or []),
            "SONG_ROW_NOT_REVIEW_READY_IN_CLOSURE",
            f"the bind would not leave {candidate_id} in the day's "
            f"ready-unpublished set (closure={after_closure.get('status')!r})",
        )
    if before_state.get("publication_closure") is not None:
        after_state["publication_closure"] = after_closure
    changed = any(
        after_state.get(key) != before_state.get(key)
        for key in ("songs", "song_superseded_attempts", "publication_closure")
    )
    if changed:
        after_state["updated_at"] = bound_at
    return after_state, {
        "created_song_row": preconditions.will_create_row,
        "superseded_existing_row": preconditions.will_supersede,
        "carried_selection_keys": sorted(carried),
        "state_changed": changed,
        "closure_before": before_closure,
        "closure_after": after_closure,
    }
