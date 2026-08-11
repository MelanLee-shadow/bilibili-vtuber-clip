# Future speaker holdout source inventory (2026-08-11)

This is a read-only source discovery record.  It does **not** claim that a
holdout package, ASR grid, frozen prediction, or human speaker truth exists.
Nothing listed here is production or release authority.

## Authority and exclusion boundary

- Runtime source authority: `free:/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/`.
- `2026-08-10` and `2026-08-11` have no corresponding
  `/opt/bilive/autoslice/out/<date>` directory and no repository hit for their
  raw recording stems.  No candidate, SRT, speaker override, or reviewed
  speaker truth was found for either session.
- `2026-08-09` is excluded from locked evaluation: five candidates and final
  review records already exist.  It may be used only as disclosed development
  material.
- `2026-08-08` is the current cue challenge/development session and
  `2026-07-22` supplies reviewed development enrollment; both are excluded.

The inventory checks were read-only.  Large media files were not hashed, no
ASR or speaker model was run, and the 2026-08-11 recording session was not
declared closed.

## Proposed locked source H1: 2026-08-10

All rows have same-stem `.meta.json` and `.xml` companions.

| raw MP4 | bytes | ffprobe duration (s) |
|---|---:|---:|
| `22966160_20260810-20-07-05.mp4` | 225,526,064 | 1801.974 |
| `22966160_20260810-20-37-02.mp4` | 233,955,989 | 1804.221 |
| `22966160_20260810-21-07-06.mp4` | 259,109,139 | 1804.227 |
| `22966160_20260810-21-37-11.mp4` | 273,911,395 | 1804.213 |
| `22966160_20260810-22-07-15.mp4` | 277,724,188 | 1804.220 |
| `22966160_20260810-22-37-19.mp4` | 293,662,948 | 1804.226 |
| `22966160_20260810-23-07-23.mp4` | 156,237,484 | 798.733 |

Status: `SOURCE_DISCOVERED_NOT_LOCKED`.  This session is a suitable H1 source,
but the exact sampled intervals, source/sidecar hashes, cue grid, algorithm
receipt, and pre-label predictions have not been frozen.

## Proposed locked source H2: 2026-08-11

All rows have same-stem `.meta.json` and `.xml` companions.

| raw MP4 | bytes | ffprobe duration (s) |
|---|---:|---:|
| `22966160_20260811-10-27-49.mp4` | 536,673,431 | 1800.082 |
| `22966160_20260811-10-57-47.mp4` | 534,080,313 | 1804.189 |
| `22966160_20260811-11-27-51.mp4` | 532,419,036 | 1804.195 |
| `22966160_20260811-11-57-56.mp4` | 535,074,142 | 1804.181 |
| `22966160_20260811-12-28-00.mp4` | 466,289,383 | 1539.938 |

Status: `SOURCE_DISCOVERED_SESSION_CLOSURE_UNVERIFIED`.  H2 must not be frozen
until the recording/adapter authority proves the session is sealed and the
source bytes are stable.

## Required freeze order

The two dates are independent sessions; multiple candidates or files from one
date never count as multiple cross-session holdouts.

1. `SOURCE_FROZEN`: prove the session closed, choose non-overlapping intervals
   across early/middle/late portions, and bind regular media plus companion
   sidecars by path, size, duration, and SHA-256.
2. `PRELABEL_PACKAGE_FROZEN`: create and hash the exact decoded audio/cue grid,
   freeze algorithm/config/model/profile hashes, run the pre-registered
   prediction once, and seal its receipt.  The sampling process must not inspect
   speaker truth or select easy-looking scores.
3. `LABEL_REVEALED`: only after step 2, expose a blind review package and accept
   human HOST/OTHER/MIXED/UNJUDGEABLE labels.  Bind each label to the exact cue
   interval and bytes.
4. `EVALUATED_ONCE`: score the already-frozen predictions per candidate and
   pooled.  Any threshold, representative-selection, or aggregation change
   made after labels are seen creates a new algorithm version and requires new
   untouched holdouts.

Until both H1 and H2 reach `EVALUATED_ONCE`, auxiliary CAM++ experiments remain
development diagnostics and cannot authorize a production speaker gate.

## Source freeze completed after the inventory

The live recording authority was rechecked before writing any scratch result:

- `status.json` reported `streaming=false`, `recording=false`,
  `finalizing=false`, `service_reachable=true`, `running_status=idle`, and no
  error; the last webhook was `SessionEnded` and the adapter had been inactive
  for about 2.15 hours;
- the room root resolved to `CloudFS` on a `fuse` mount;
- all 15 unique MP4s had same-stem FLV/XML/JSONL/meta, one matching
  `FileClosed`, and one adapter `finalized` row whose source size matched the
  current FLV;
- `/opt/bilive/autoslice/DISABLED` remained present, and the timer/service
  remained inactive.

A create-only source freezer was run only under the isolated remote scratch
root:

`free:/opt/bilive/autoslice/holdout-runs/20260811-speaker-source-freeze-v0/`

The production runner enumerates the CloudFS recording root and explicit
`state`/`cache`/`reports` paths; this new `holdout-runs` directory is not one of
its input roots.  No production `state/`, `out/`, repo, service, or pointer was
changed.

The first implementation attempt rejected the live status before output
because the room ID was represented as a string rather than the integer used
by the unit fixture.  A second implementation created
`source-freeze.pass1.json`, but exposed a CloudFS directory-listing artifact:
five paths were returned twice, producing 20 rows for only 15 unique files.
That artifact is preserved as invalidated diagnostic evidence and is not an
accepted holdout manifest.

Version 3 canonicalizes identical absolute listing paths while retaining any
duplicate count in the non-authoritative observation.  Two complete passes
then independently re-read and SHA-256 hashed every current MP4 and all small
sidecars:

| artifact | file SHA-256 | deterministic payload SHA-256 |
|---|---|---|
| `source-freeze.v3.pass1.json` | `e80baec84ac8de4bca7de5a1edb265da732de2c4cf674aba29b8886c4c37de57` | `c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184` |
| `source-freeze.v3.pass2.json` | `18c71fe21a3f4ab89c0233b053a6e030308b57c8f4eb56745c7d4576c4ba7370` | `c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184` |

The v3 freezer bytes have SHA-256
`6295811bce14a7066f70495ced1de8ce6a365686659455e8100bd708ccc8a9dd`.
The two JSON file hashes differ only because the observation binds its own
read time/status snapshot.  Their complete deterministic payloads are equal
after removing `observation`, and both declare exactly `10 + 5 = 15` unique
segments.  All 15 recomputed MP4 hashes equal the adapter target hashes.

This advances H1/H2 only to `SOURCE_FROZEN`.  Both accepted manifests still
state:

```text
asr_frozen=false
cue_table_frozen=false
predictions_frozen=false
human_truth_opened=false
production_authority=false
deployment_authority=false
next_required_state=PRELABEL_PACKAGE_FROZEN
```

The next safe step needs a dedicated holdout-only extraction wrapper with
explicit scratch outputs.  Existing production runner entry points are not
safe substitutes because they may write production state.  Human labels must
remain unopened until exact ASR/cue bytes, the candidate algorithm/config, and
predictions are frozen.
