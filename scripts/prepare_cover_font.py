#!/usr/bin/env python3
"""Prepare a verified REEJI font in a private, non-repository asset directory.

The approved-local route copies only 维护者's pinned input, with no
network request. Official acquisition remains available as a separate route.
Neither route changes services, installs system fonts or adds font bytes to Git.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlparse
import zipfile

from PIL import ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.autoslice.cover_private_font import (
    FILE_NAME, SCHEMA, resolve_private_cover_font,
    APPROVED_LOCAL_SOURCE, APPROVED_LOCAL_SHA256, APPROVED_LOCAL_BYTES,
    APPROVED_LOCAL_PAGE, APPROVED_LOCAL_ACQUISITION, APPROVED_LOCAL_APPROVAL,
)


def official_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or (host != "reeji.com" and not host.endswith(".reeji.com"))
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Official HTTPS reeji.com URL required")
    return url


def get_official(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        official_url(url), headers={"User-Agent": "vtuber-slice font preparation"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        final_url = official_url(response.geturl())
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Official download exceeds font acquisition bound")
    return data, final_url


def acquire(
    *, source_page: str, download_url: str, output: Path, member: str | None = None
) -> dict:
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("Font must stay outside any Git repository")
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError("Private asset path must not contain a symlink")
    # Do not overwrite an installation or manufacture a manifest for old bytes.
    output.mkdir(parents=True, exist_ok=True)
    if any(
        (output / name).exists()
        for name in (FILE_NAME, "cover-font.json", "official-source.html", "official-download.bin")
    ):
        raise ValueError("Create-only output already contains a font acquisition")
    page, page_url = get_official(source_page)
    downloaded, final_url = get_official(download_url)
    font = downloaded
    chosen_member = None
    if zipfile.is_zipfile(io.BytesIO(downloaded)):
        with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if info.filename.lower().endswith((".ttf", ".otf"))
                and info.file_size <= 20 * 1024 * 1024
            ]
            if member is not None:
                candidates = [info for info in candidates if info.filename == member]
            if len(candidates) != 1:
                raise ValueError("Archive contains multiple fonts; specify the exact --member")
            chosen_member = candidates[0].filename
            font = archive.read(candidates[0])
    family, style = ImageFont.truetype(io.BytesIO(font), 64).getname()
    if "zhenyan" not in family.lower() and "真言" not in family:
        raise ValueError("Official download is not a Zhenyan family font")

    def digest(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    manifest = {
        "schema_version": SCHEMA,
        "acquisition": "OFFICIAL_DOWNLOAD",
        "font_file_name": FILE_NAME,
        "font_sha256": digest(font),
        "font_family": family,
        "font_style": style,
        "source_page": page_url,
        "source_page_sha256": digest(page),
        "download_url": final_url,
        "download_sha256": digest(downloaded),
        "archive_member": chosen_member,
        "distribution": "PRIVATE_LOCAL_ONLY_NO_REPOSITORY_NO_OSS_EXPORT",
    }
    for name, data in (
        ("official-source.html", page),
        ("official-download.bin", downloaded),
        (FILE_NAME, font),
        ("cover-font.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()),
    ):
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    _, authority = resolve_private_cover_font(binding_path=output / "cover-font.json")
    return authority


def acquire_approved_local(*, output: Path) -> dict:
    """Private local-file approval is unavailable in the public snapshot."""
    raise ValueError("PRIVATE_LOCAL_FONT_AUTHORITY_UNAVAILABLE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-page")
    route = parser.add_mutually_exclusive_group(required=True)
    route.add_argument("--download-url")
    route.add_argument("--approved-local-file", action="store_true",
                       help="Private local authorization is not distributed; use the official-download route")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--member")
    args = parser.parse_args()
    try:
        if args.approved_local_file:
            if args.source_page or args.member:
                raise ValueError("Approved local input cannot claim official download metadata")
            result = acquire_approved_local(output=args.out)
        else:
            if not args.source_page:
                raise ValueError("Official acquisition requires --source-page")
            result = acquire(
                source_page=args.source_page, download_url=args.download_url,
                output=args.out.absolute(), member=args.member,
            )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "PREPARED_PRIVATE_FONT", "authority": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
