# ChatGPT Pro overnight speaker decision (2026-08-11)

## Consultation binding

- Mode visibly confirmed: `Pro`.
- Thread: `https://chatgpt.com/c/6a7ac583-52e4-83ea-b05d-65d3fbed2572`.
- Durable run record:
  `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-11T06-46-41-146Z-d252528edd5e287b.json`.
- Submitted prompt SHA-256:
  `d252528edd5e287b17fead50988ed42ea01b74dc2c740b2391668abf4185f24f`.
- Exact submitted bytes:
  `pro-consult-overnight-next-decision-oneline.txt`; the adjacent Markdown file
  is the human-readable equivalent.
- Complete stable answer SHA-256:
  `9998da160a24a3234a63b64bef9eedfd19284cff823a342c0d4b14b83c36dc4d`.
- Read status: `read_complete`; the same complete assistant text was read twice
  after generation stopped and was byte-identical after newline normalization
  and `trimEnd()`.

This file is an engineering decision summary, not a verbatim transcript and
not production, deployment, upload, or release authority.

## Decisive recommendation

Build one shadow-only **session-stratified, duration-matched
centroid--medoid consensus verifier with an explicit OTHER veto**.  Keep CAM++
as the fixed embedding extractor.  Do not add AS-norm, learned calibration, or
same-session promotion in this cycle.

Normalize every embedding.  Use two duration strata:

- `SHORT`: 300--1499 ms;
- `LONG`: at least 1500 ms.

For every duration stratum, a usable HOST bank requires at least three
genuinely independent recording sessions and at least three exact, audited,
solo-HOST clips per session.  Same date/stream/media or a re-encoded copy is one
session.  The three existing long references count as separate sessions only
after provenance proves independence.  The 7/22 bank is one session; 8/8 may
be one development-bank session for future sessions but cannot vote while 8/8
itself is evaluated.

For session `s`, compute its normalized HOST centroid `c_s` and choose the
actual reviewed clip nearest that centroid as medoid `m_s`, breaking ties by
canonical PCM SHA-256 and then cue start sample.  For a view embedding `e_v`,
session support is:

```text
h_s(v) = min(cos(e_v, c_s), cos(e_v, m_s))
```

With `|S|` valid sessions, require strict-majority cross-session support and
take the `floor(|S|/2)+1`-th largest session support as `H(v)`.  With three
sessions this is continuous 2-of-3 consensus.  Pro explicitly rejected the
minimum across all sessions because one mismatched source can reproduce the
current recall collapse.

Build a separate reviewed OTHER veto bank with at least 12 clear OTHER clips
per duration stratum.  Select exactly eight representatives per stratum by a
deterministic greedy facility-location rule with PCM-hash tie breaks.  Holdout
material is categorically excluded.  When diagnosing 8/8, same-session OTHER
material is allowed only through fixed contiguous-block cross-fitting that
excludes the evaluated block and adjacent source intervals.

Let `N(v)` be the maximum cosine to any selected OTHER medoid and
`M(v) = H(v) - N(v)`.  A future frozen threshold tuple per duration stratum is
`(tau_other, tau_host, delta_host)`.  OTHER prototypes are veto-only: they may
turn prospective HOST into UNKNOWN, but never promote HOST and never normalize
scores.  **Until a threshold manifest is frozen, thresholds are `NULL` and all
real-data hard decisions from this candidate are UNKNOWN.**

Cue policy remains fail closed:

- under 300 ms: UNKNOWN;
- 300--1499 ms: whole-cue view, still requiring centroid/medoid and
  cross-session agreement plus the OTHER margin;
- at least 1500 ms: whole cue plus existing disjoint speech cells; a hard label
  would require whole-view agreement, strict-majority valid cells, and zero
  cells voting the opposite label;
- mixed cues: always MIXED/UNKNOWN, never hard HOST/OTHER;
- NON_SPEECH, gaps, conflicts, or any unmet bank minimum: UNKNOWN, with no weak
  fallback.

## What may be built now

Development-only work may implement the evidence/bank registry,
centroid/medoid and OTHER representative selection, raw cosine scoring,
abstention, provenance, synthetic decision-branch tests, blocked 8/8
cross-fitting diagnostics, and a threshold **frontier** at observed score
breakpoints.  It may not choose or serialize a winning threshold after looking
at 8/8 errors.

Production logic, v1 behavior, centrality, human-review routing, deployment,
receipt authority, and all acceptance gates remain unchanged.  Before holdout
labels are opened, one complete candidate must freeze code/environment/model,
bank membership, centroid/medoid identities, duration/window rules, OTHER veto
members, exclusion rules, metric implementation, and both threshold tuples.
Any change after seeing holdout A converts A to development and requires two
fresh untouched holdouts.

## Ranked unattended tasks and stop conditions

1. **Evidence registry and new-session lock.** Resolve provenance for the three
   long references, 7/22, and 8/8; mechanically extract/hash-bind the complete
   cue population from two newer independent sealed sessions.  The registry
   must bind session group, split role, source/canonical PCM/ASR/cue hashes,
   sample bounds, duration, mechanical exclusions, extraction version, and
   initial `UNLABELED_LOCKED` state.  Stop only after two identical extractions,
   or report `INSUFFICIENT_NEW_SESSIONS`; never substitute leaked, approximate,
   or machine-only material.
2. **Score-only architecture.** Produce a spec, bank manifest, per-view score
   table, and reproducibility receipt containing all centroid/medoid cosines,
   session support, `H/N/M`, votes, bank hashes, bank-minimum status, and
   `threshold_state=NULL`.  Stop after two clean hash-identical runs and all
   fail-closed tests, or at the first unresolved provenance/intersection/
   nondeterminism issue.
3. **One preregistered 8/8 diagnostic.** Run cross-session-only scoring,
   deterministic blocked cross-fit, the fixed threshold frontier, duration and
   pooled gate tables, and leakage canaries once.  Finish with exactly one of
   `NO_DEV_FEASIBLE_REGION`, `DEV_FEASIBLE_REGION_NO_THRESHOLD_SELECTED`,
   `INVALID_LEAKAGE_OR_PROVENANCE`, or `INSUFFICIENT_BANK_SUPPORT`.  Stop after
   this report; do not tune again overnight.  AUC improvement is not a
   continuation criterion.

## Required negative canaries

- Renamed/re-encoded duplicates must collapse to one canonical session and
  make the three-session minimum fail.
- One-sample overlap, adjacent evaluated audio, or re-encoded target material
  entering either bank must invalidate the receipt before scoring.
- Duration/block-preserving truth permutation and replacement of same-session
  HOST anchors with duration-matched OTHER anchors must destroy any apparent
  same-session gain; otherwise the diagnostic is channel leakage.
- Any holdout cue in the OTHER/cohort manifest must fail closed; it cannot be
  repaired after truth by leave-one-out filtering.
- Mutating a threshold or bank hash after a holdout truth hash is attached must
  demote that holdout to development and reset the accepted holdout count.
- All-UNKNOWN and all-OTHER stub scorers must fail the full gate denominators;
  UNKNOWN may never disappear from coverage, recall, or UNKNOWN-share metrics.

## Production and ETA decision

**No defensible production path exists without two new human-reviewed
cross-session holdouts.**  The absence of those holdouts is itself a hard
failure regardless of 8/8 AUC or oracle recall.  The existing bounded human
review route remains valid but does not validate this architecture.

A development artifact is conditionally achievable during the unattended run
only if source provenance, cue tables, deterministic CAM++ inference, and the
needed repository/runtime interfaces are available.  Otherwise the correct
overnight output is a named blocker plus completed deterministic artifacts.
Production-readiness has no honest fixed date: the earliest decision is after
one candidate is frozen before truth access, both independent sessions are
human-reviewed, and the unchanged candidate passes every gate on each session
and pooled.  Failure of either holdout returns ETA to unknown and starts a new
evidence cycle; it never licenses retuning on the failed holdout.
