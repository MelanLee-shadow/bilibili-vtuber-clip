# BV1Bau16nEyq r3 same-BV replacement handoff

## Outcome

PASS. The public video was replaced in place; no new BV was created.

- BVID: `BV1Bau16nEyq`
- predecessor CID: `40746484284`
- new CID: `40755790146`
- public state: `0` / open
- title, description, tags, cover, single-P shape, and `小李切片` section membership: unchanged and mutually consistent across creator/public/section views
- fresh-live receipt: `VERIFIED_FRESH_LIVE`
- public CDN 1080p AVC stream: full video decode rc0, duration `118.521000s`
- online frames at 7.5s, 20.5s, 80.5s, and 116.8s: no literal `[李豆沙]` speaker prefix; all three `ラブコード` review windows spell the song correctly

The exact frozen public title is:

`【李豆沙】小李解释偶像曲里那句“请感受穿越屏幕的热烈，再一次爱上我吧”`

## Authority and write boundary

All remote writes were confined to:

`/opt/bilive/autoslice/recovery/2026-08-09/auto_230125_960_1072-lovecode-r2/`

The same-BV commands ran with `AUTOSLICE_BASE` set to that R2 root, the predecessor journal explicitly reused, and `/opt/bilive/autoslice/upload.lock` explicitly supplied. Every real preflight observed the shared lock free; no lock bypass was used.

No Git commit, deploy, new-BV upload command, production `out/` write, or production `state/` reconciliation was performed. The read-only production state check found that `/opt/bilive/autoslice/state/2026-08-08.json` does not contain CID `40755790146`; production reconciliation remains the integrator's lane.

## Final package

Package root:

`R2/repo/lidousha/2026-08-08/`

Final artifact bindings:

| Artifact | SHA-256 | Notes |
|---|---|---|
| burned MP4 | `c4e549684a593ff140c1165d3a6e16a0d1540ccbc8cb132ce969c32dba775139` | 12,633,410 bytes; 118.521029s; no literal label burn |
| clean sidecar SRT | `d06d0a7158932a5a606f1a2ca856da9007660480d37df3ae00d62dca97d475dd` | 48 cues; zero `[李豆沙]` prefixes |
| speaker review SRT | `73fbf084dbad349ca90be073711f6da31738f9617ee04e702ed7ac2389149037` | 48 single prefixes; stripping one prefix is byte-identical to d06 |
| styled ASS | `b79c9c7868a0b724ca90e30250160e357d4b43e9b0eb4f0fbdfc36d0f03fb9cc` | zero literal `[李豆沙]`; speaker distinction is style-only |
| cover PNG | `f8f68a504dbcd0acbffd5d9af0ca16d3fae0df30f6ebcecdd6bf3339b02b0616` | bytes unchanged; frozen QC replay accepted |

The initial live R2 state contradicted the task brief: no d06 file existed and the alleged clean sidecar was still 73fb with 48 literal prefixes. r3 materialized d06 by one exact prefix strip, then rebuilt the story/source-fact/boundary/hash ladder. Both the replacement package and the human-title delivery bundle now audit PASS with zero issues.

## Audio lineage

The decoded PCM of the candidate is not byte-identical to the predecessor/r1-chain PCM. That fact is deliberately preserved in the evidence rather than hidden.

The decisive lineage check is stronger than PCM correlation:

`candidate AAC == expected intro AAC || exact recut-source AAC`

- candidate AAC: 1,983,824 bytes, SHA `84f3753567d2c0a45a599e6da0468d2cbf6abefec490b4ea2c756e5e246f48e0`
- expected intro AAC: 151,037 bytes, SHA `8798c81a535eb82869cada725692d601f6ce85b5929c17f9dd72f96c6cb00ba9`
- exact recut-source AAC: 1,832,787 bytes, SHA `2e423884e9c4cd72fcd2a5d0705d2cbb65469eb8f28df87b5af7daf690edbdff`
- prefix equality: true
- suffix equality through EOF: true
- full concatenation equality: true

The apparent PCM difference is a concat/decode priming-boundary effect, not a changed正文 audio source. Independent correlation evidence also covers the complete 112.32s recut source after the intro offset (`corr=0.982023`); it is retained as secondary evidence.

## Gates and receipts

Authoritative r3 receipt hashes:

| Receipt | SHA-256 / result |
|---|---|
| `review_manifest-r3.json` | `f0af4fffcd54871c751b3fa302682eb68d1c24b22a602e36370347d53a8aa4f9`; recovery/no-upload review status |
| `package-audit-r3.json` | `7ac2c8339e10db5faba0984117d853cea20d31b03d4dd48932a1dd48c9203175`; PASS, 0 issues |
| `final-human-review-aac-r3.json` | `dc9b6266afb5beb7b70e95f87b8de12a5284786b8894a587dfffd2ca9a0b73b1`; `ACCEPTED_FOR_SAME_BV` |
| `authorized-upload-manifest-r3.json` | `b4d9c9bb82943d98919d760e2bed7c9db4aaf0c2f49d64ee0560214e3e394dc8`; verify rc0 |
| `same-bv-repair-plan-r3.json` | `fe9be89c078588c67b70aa5c846599fa0d6ef797f1422fef984adea9bb4d66d6` |
| `same-bv-repair-completed-r3.json` | `4ddec1c38ee01772acd5d4d4f845c3c59aac9269ca470896bf2cad503e834d20`; CID `40755790146` |

Execution results:

- recovery manifest rc0
- bundle audit rc0; post-public rerun also PASS/0 issues
- final-human receipt rc0
- authorized manifest build and verify rc0
- repair-plan dry-run rc0
- repair-plan real rc0 (`PLANNED`, no remote mutation at plan time)
- repair-status-before rc6, expected `PLANNED`
- repair-run dry-run rc6, expected `PLANNED`
- repair-run real rc0, `VERIFIED`, changed to CID `40755790146`
- repair-status-after rc0, `VERIFIED`
- repair-verify-live rc0, `VERIFIED_FRESH_LIVE`
- R2 sandbox reconciliation: `VERIFIED_SAME_BV`, published CID `40755790146`

The earlier final-human receipt under `receipts/superseded-pre-aac/` is retained only as audit history. The authorized manifest binds the stronger `final-human-review-aac-r3.json` receipt.

## Public CDN acceptance

The fresh public view first fixed the expected CID to `40755790146`. An authenticated playurl request then selected the unique quality-80 AVC stream whose path itself binds that CID:

`/upgcxcode/46/01/40755790146/40755790146-1-30080.m4s`

The CDN request used only UA plus the BV referer; account cookies were not forwarded to the CDN.

- HTTP 200
- 1920x1080, 30fps AVC
- 6,255,091 bytes
- SHA `4da74818b568d0b5bfe088105efa80048c020b8d47650a1b18ed6fa17f0baf65`
- full video decode rc0

Visual observations and hashes are recorded in `LIVE_ACCEPTANCE-r3.json` and `logs/live-visual-review-r3.log`. The four online PNGs are in `live-frames/`.

## Historical integrity and recovery

The three protected r2 history receipts remain byte-identical:

- publication reconciliation: `4bf336fc825f8afcbc5eadfe7a9fbc61ceea41d073d698573443c36084cb43f9`
- uploaded receipt: `2a4bf025503ddfb3ba416a4954a89cea74a720f7ad441a4d8efadf8ddd7863a0`
- upload manifest: `b8e850ca61ad0bb009ea64fee9738874d20ba41cd8629dbe02221df465ec09f6`

The predecessor plan/completed receipts also remain `5acdd35c…` / `fe7b0b7d…`. The first 8 rows / 8,936 bytes of the predecessor journal compare byte-for-byte equal; r3 appended rows 9–16 only.

Every modified JSON and replaced media/subtitle surface has an R2-side `.r3bak`; the exact list is in `logs/backup-r3.log`. The old package MP4 is recoverable as the package `.mp4.r3bak` and remains SHA `8ae72e64c5d5a1a16c8f7ca8df510a07fb5ac05ecb2752bd85d9a35197534245`.

## Integrator import list

Use the authoritative files in `receipts/`:

1. `review_manifest-r3.json`
2. `package-audit-r3.json`
3. `final-human-review-evidence-aac-r3.v2.json`
4. `final-human-review-aac-r3.json`
5. `authorized-upload-manifest-r3.json`
6. `same-bv-repair-plan-r3.json`
7. `same-bv-repair-completed-r3.json`
8. `same_bv_repair_ledger-r3.jsonl`
9. predecessor plan/completed receipts
10. frozen title-cover QC receipt

R2 sandbox reconciliation snapshots are under `state-sandbox/`. Import/reconcile CID `40755790146` into production state only through the integrator's production-state workflow; no additional Bilibili replacement is needed.

`logs/` contains every r3 command log and rc, including the expected rc6 local states and the two failed media/final-evidence attempts. `frames/` contains package-side review evidence; `live-frames/` contains the new-CID CDN evidence.
