# BV1Bau16nEyq ラブコード same-BV repair r2

## Result

- Same BV retained: `BV1Bau16nEyq`.
- Live identity verified on Creator, public view, and section surfaces:
  - AID: `117064333264829`
  - old CID: `40743275821`
  - new CID: `40746484284`
  - completed status: `VERIFIED_FRESH_LIVE`
- Public title stayed exactly:
  `【李豆沙】小李解释偶像曲里那句“请感受穿越屏幕的热烈，再一次爱上我吧”`
- Public cover URL stayed unchanged. The frozen source cover is SHA-256
  `f8f68a504dbcd0acbffd5d9af0ca16d3fae0df30f6ebcecdd6bf3339b02b0616`.
- No new BV was created. No git commit was made.

## Corrected r2 artifact

- Video SHA-256:
  `8ae72e64c5d5a1a16c8f7ca8df510a07fb5ac05ecb2752bd85d9a35197534245`
- Size: `13,108,987` bytes; duration: `118.553971` seconds.
- H.264 1920x1080 30 fps plus AAC 48 kHz stereo.
- Full video+audio decode to EOS: rc `0`.
- Decoded PCM SHA is unchanged from the published predecessor:
  `49e4d37e6771b72379fe615f473c0dad64bbbfe17fd89d18ac26c4831ce7cc11`.

## Subtitle acceptance

- Reviewed/final SRT SHA-256:
  `73fbf084dbad349ca90be073711f6da31738f9617ee04e702ed7ac2389149037`.
- Final SRT is byte-equal to the reviewed baseline: `true`.
- Speaker SRT after stripping exactly one producer-added prefix is byte-equal
  to the reviewed baseline: `true`.
- Baseline has 48 cues, exactly three `ラブコード`, zero `LoveLive!`, no
  forbidden parenthetical notes, and no isolated A/B residue.
- Against the actual published predecessor SRT, the three changed SRT cue
  indices are `1`, `7`, and `32`. (The request's `1/9/42` refers to a different
  numbering context.)
- Own r2 burned-frame review passed at 7.5 s, 20.5 s, and 80.5 s. The three PNGs
  are at the top level of this directory.

## Authority files for integrator import

1. `../assets/lidousha/reviewed_subtitle_baselines/auto_230125_960_1072.reviewed.srt`
2. `../assets/lidousha/reviewed_subtitle_baselines/auto_230125_960_1072.subtitle-baseline.v1.json`
3. `../reports/authorized_uploads/2026-08-09-960-lovecode-source/auto_230125_960_1072.public_verify.json`
4. `../assets/lidousha/recovery_publication_authority_2026-08-09_960_lovecode.v1.json`
5. The target's four-point entry added to
   `../assets/lidousha/final_media_review_contracts.v1.json`.

The reviewed SRT is also copied here as
`auto_230125_960_1072.reviewed.srt`; the exact frozen cover is
`auto_230125_960_1072.frozen-cover.png`.

## Repair receipts

- `receipts/same-bv-repair-plan.json` — SHA-256
  `5acdd35ccf41508190a7b0ec72053b8e7fc3a45d8b2b78a2ec4b12400c5b5a6a`.
- `receipts/same-bv-repair-completed.json` — the create-only output of
  `repair-verify-live`, SHA-256
  `fe7b0b7d130e7727597039387b8a39a27ee95ea2a78f1ac200fd65435825b5ce`.
- `receipts/authorized-upload-manifest.json` — SHA-256
  `a51ce41269560d6b99a1ac8a129e4011ec90036fa97a5ab7b7393dab363cd701`.
- `receipts/final-human-review.json` — SHA-256
  `58ccfbb63b1ddfec6e0552c88df4339b6a64048a6e906231f24aee8b47166707`.
- `receipts/title-cover-joint-qc.receipt.json` — status `PASS`, SHA-256
  `eabf3e142f37600d65ea95bcd920f5cf4f62d063514c973ef23225b361d06c7a`.
- `logs/repair-verify-live-r2.log` is the successful verify-live command output;
  `logs/repair-verify-live-r2-attempt1.log` preserves the earlier live-PASS /
  local-reconciliation-pending rc=6 event.

## Fail-closed fixes and regression

- `src/autoslice/final_human_review.py`: permits only the narrowly validated
  byte-identical published-cover carry case (`REUSED`, one candidate,
  `READY_DEGRADED`, screenshot-polish to CPA redraw, canonical v2 route and
  identity witness); ordinary cover routes retain the existing strict gate.
- `src/autoslice/publication_reconciliation.py`: derives recording date from
  canonical `out/<date>` or `lidousha/<date>` layout before the legacy unique
  date fallback. This fixes recovery wrappers whose operation date differs
  from the recording date while rejecting conflicting canonical dates.
- Tests added/updated in:
  - `tests/test_build_lidousha_final_human_review.py`
  - `tests/lidousha/test_final_human_review.py`
  - `tests/test_publication_reconciliation.py`
- Local focused regression: `286 passed, 2 warnings` (`regression-tests.log`).
- The remote host has no pytest module; that failed environment check is kept
  in `logs/publication-reconciliation-remote-tests.log`. A remote real-manifest
  smoke then resolved `('auto_230125_960_1072', '2026-08-08')` before the
  successful reconciliation.

## Disclosures and remaining risk

- The task text's AID `117064283068060` is stale/wrong. Creator, public view,
  member/section evidence, and the repair closure all agree on
  `117064333264829`.
- The task text's cover hash `4a90d109...` belongs to neighbor candidate
  `auto_213135_469_710 / BV18Gu16NEcX`. The target's manifest, published receipt,
  and frozen cover all agree on `f8f68a504...`; the neighbor cover was never
  copied into this package.
- Production `/opt/bilive/autoslice/state/2026-08-08.json` remains untouched and
  still carries the stale AID/missing publication closure. The sandbox-only
  reconciled projections are under `verification/sandbox-*`; integrator must
  decide and separately authorize any production state reconciliation.
- The producer did run one bounded sparse-cue self-heal upstream (missing draft
  cue 14). It occurred before the exact reviewed replay; all 48 final owned
  intervals and final output are SHA-bound to the baseline. Evidence status is
  `PASS_WITH_DISCLOSED_UPSTREAM_SELF_HEAL`.
- The same-BV manifest refreshed audited tags: old `唱跳/自信` were replaced by
  `可爱/帅气`; title, description, TID, source, BV, and cover stayed unchanged.
- `authorized_upload.py verify` is a local manifest/hash check, not a public
  network refresh. Fresh public closure is the Creator/public/section snapshot
  in `receipts/same-bv-repair-completed.json`.
- No post-edit CDN transcode frame was independently downloaded; public
  acceptance is the exact uploaded artifact plus the pipeline's fresh
  three-surface CID/metadata closure.
- The requested 1013 setup/review/plan source files were absent from this
  worktree and git history. The run used the current pipeline contracts plus
  the available 1013 authority shape and remote read-only precedent.

## Runtime location

The retained sandbox and full r2 package/logs are at:

`/opt/bilive/autoslice/recovery/2026-08-09/auto_230125_960_1072-lovecode-r2/`

Production `out/` and `state/` were read-only throughout; only the shared
`upload.lock` and Bilibili same-BV edit API were used for the authorized repair.
