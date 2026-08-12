# ChatGPT Pro decision: cue-to-speaker-subsegment alignment

- Thread: <https://chatgpt.com/c/6a7bb63a-c034-83ea-8397-848b3af2b778>
- Visible mode: `Pro`
- Prompt SHA-256: `88acc5601d56f06b916775da639ba59dc9fbe34cd43053a535176e123fa5be7b`
- Normalized response SHA-256: `fcf3f8065ff6dbd49602ed1540a4e82482f7ca0f3b829dd2aca44a919d3a6f3d`
- Durable run record: `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-11T23-53-09-906Z-88acc5601d56f06b.json`

## Accepted P0 contract

1. Replace the cardinality rule with a deterministic monotone mapping from every
   speaker segment to exactly one reviewed plain cue. A cue may own one or more
   ordered subsegments; `55 != 52` is not a failure condition.
2. A segment must have exactly one positive-overlap cue, must be contained by it,
   must not overlap another cue, and ownership must be monotone. Parse times as
   half-open integer-millisecond intervals. The Pro recommendation permits at
   most 1 ms containment overhang into an otherwise empty gap, never into another
   cue; project authority is allowed to be stricter when both reviewed files use
   the same millisecond timebase.
3. Speaker segments may have silent temporal gaps inside a cue, but never overlap.
   Every non-empty plain cue must own at least one segment. Empty/zero-duration,
   cross-cue, unowned, overlapping, or non-monotone inputs are rejected.
4. For every cue, concatenating its segment texts in file order and applying only
   the frozen whitespace-only `compact_ws/v1` operation must equal the reviewed
   cue text. No punctuation, Unicode, case, width, script, name, or character
   normalization is allowed.
5. Use a three-state result: `PresentValid`, `AbsentAuthorized`, or `Rejected`.
   `None`/unverifiable is legal only for authority-declared absence such as
   `uniform_host`. Present but malformed or drifted evidence is a hard production
   block before the source-fact judge runs.
6. Render one-segment cues byte-identically to the legacy transcript. Render mixed
   cues with hierarchical identifiers such as `18.1` and `18.2`; do not add a
   synthetic parent row, `[mixed]` label, or merge adjacent same-speaker segments.
7. Persist a canonical alignment document and bind all three layers: the exact
   speaker-final SRT bytes, the canonical alignment document, and the exact
   rendered speaker transcript seen by the judge. The plain transcript remains
   byte-identical.
8. Rebuild and verify those bindings in the package manifest/auditor. A package
   whose receipt claims a non-null speaker transcript must reproduce the same
   transcript hash from the package-contained speaker SRT and reviewed cue grid.

## Required migration tests

- legacy 1:1 byte compatibility;
- one and several mixed-cue splits with hierarchical identifiers;
- whitespace-only equivalence;
- punctuation/character drift rejection;
- missing cue, gap-owner, overlap, cross-boundary, non-monotone, empty and
  zero-duration rejection;
- speaker-final and manifest hash drift rejection;
- real `auto_223750_578_734` 52-cue/55-segment acceptance with three 1:2 groups;
- `uniform_host` authorized absence unchanged;
- present-but-invalid evidence never downgraded to absence.

## Root disposition

Implement the P0 contract before reproducing the story package. Use the existing
speaker-final manifest `source_index` as an additional independent reviewed
ownership witness, while still performing the Pro-required temporal and exact
text-partition checks. Keep the production boundary stricter than the optional
1 ms recommendation when the manifest and both SRTs already assert exact integer
millisecond boundaries; do not widen a reviewed exact-boundary contract merely to
accept historical drift.

The path-rich manifest is revalidated against the current record on every build
and audit, but its raw file hash is not part of the cross-host receipt identity:
the importer legitimately rewrites locator fields. The persisted relocation-safe
identity is the exact speaker-final SRT hash, canonical alignment hash, and exact
rendered speaker transcript hash required by the Pro decision. The canonical
alignment uses the manifest `source_index` and reviewed millisecond grid, not an
ephemeral in-process `cue_id`, so production and package replay derive the same
identity.
