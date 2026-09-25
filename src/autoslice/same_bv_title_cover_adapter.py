"""Production Bilibili adapter for title-and-cover-only revisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.autoslice.same_bv_cover_reconciliation import snapshots_equivalent
from src.autoslice.same_bv_repair import (
    BilibiliRepairAdapter,
    RemoteMutationError,
    normalise_creator_snapshot,
)


class BilibiliTitleCoverAdapter(BilibiliRepairAdapter):
    """Reuse observation/upload/section surfaces, add one narrow archive edit."""

    def edit_title_cover(
        self,
        bvid: str,
        *,
        expected_creator: Mapping[str, Any],
        target_title: str,
        cover_url: str,
    ) -> Mapping[str, Any]:
        """Re-read exact state, then change only title/P-title/cover once."""

        current = self.session.archive_view(bvid)
        current_creator = normalise_creator_snapshot(
            bvid=bvid, creator_data=current
        )
        if not snapshots_equivalent(
            {"creator": current_creator},
            {"creator": dict(expected_creator)},
        ):
            raise RemoteMutationError(
                "Creator archive drifted before title-cover edit"
            )
        payload = self.session.build_edit_payload(
            current,
            title=target_title,
            video_title=target_title,
            cover_url=cover_url,
        )
        return self.session.edit_archive(payload)
