"""Joint CPA text gate for source-bound facts in hooks and titles."""

from __future__ import annotations


# Existing read-only editorial research is projected into normal staging/tag
# receipts as method provenance. These references identify the already
# consumed source material; production must not read the report tree as a
# runtime input or treat its prose as factual authority.
EDITORIAL_RESEARCH_METHOD_PROVENANCE: tuple[dict[str, str], ...] = (
    {
        "id": "research-source-manifest",
        "schema": "research-source-consumption.v2",
        "version": "research-source-consumption.v2",
        "path": (
            "reports/lidousha-performance-20261007/title-packaging-followup/"
            "active-editorial-consumption/source-manifest.json"
        ),
        "sha256": "sha256:64c138eef0ff239aec284e0a8101fc0efc05312e95a09d1e4a41543c8c7c943d",
        "role": "bounded_method_and_source_consumption_manifest",
    },
    {
        "id": "candidate-editorial-applications",
        "schema": "research-editorial-applications.v1",
        "version": "research-editorial-applications.v1",
        "path": (
            "reports/lidousha-performance-20261007/title-packaging-followup/"
            "active-editorial-consumption/candidate-applications.json"
        ),
        "sha256": "sha256:ce7c96aed115e7787d53b5b99e1d627a6f5ffad7fbc60e82f3f6070bc62c8561",
        "role": "source_bound_candidate_comparison_method",
    },
    {
        "id": "e04-official-work-authority",
        "schema": "official-source-reference.v1",
        "version": "official-source-reference.v1",
        "url": "https://www.shinchosha.co.jp/book/105049/",
        "role": "E04_春の雪_and_三島由纪夫_source_check",
    },
)


