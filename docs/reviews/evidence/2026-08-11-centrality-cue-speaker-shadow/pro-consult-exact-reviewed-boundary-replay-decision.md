# ChatGPT Pro decision: exclusive reviewed exact-source interval replay

- Thread: <https://chatgpt.com/c/6a7bcdca-dd10-83ea-80a7-8a769a596c22>
- Visible mode: `Pro`
- Prompt SHA-256: `675780747a6888e4bbddfb90f96dfeaff4f6097190eaac91dcc248da10d46073`
- Normalized response SHA-256: `8e6f0bcd9e096324730a03907fdeada89d865cf3327a5bf23814402b13d4fb68`
- Durable run record: `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-12T01-33-19-959Z-675780747a6888e4.json`

## Accepted decision

For `auto_223750_578_734`, the only permissible production interval is the
human-reviewed half-open source interval `[577780, 735090)` ms once a valid
candidate-scoped exact-interval authority is present. Fresh ASR is a diagnostic
witness only. It must not move either boundary, and any exact-authority validation
failure must block instead of falling back to live semantic boundary selection.

The canonical reviewed semantic surface is the exact 52-cue reviewed SRT plus
the independently represented full speaker truth. It contains a media-end anchor
at local `157310` ms. The last reviewed cue ends at `157250` ms, yielding a
`60` ms unsubtitled terminal tail. Do not synthesize an empty subtitle cue for
that tail.

The verified recut is an optional derivative cache, not root authority. A prior
successful/public package, a previously favorable ASR grid, a frozen closure
cue, or whole-SRT ownership alone cannot be promoted into endpoint authority.

## Required P0 contract

Introduce an exclusive dispatch mode such as
`reviewed_exact_source_interval_v1`, backed by a typed
`operator_reviewed_exact_source_interval_v1` grant issued from authenticated
evidence that Ivan reviewed the exact audiovisual interval.

The authority must be candidate-scoped, environment-independent, and bind:

- candidate and semantic-spec identity;
- immutable source bytes, logical recording/timeline identity, and exact source
  interval;
- reviewed SRT bytes and canonical cue manifest;
- independently represented speaker-truth bytes and canonical segments, without
  assuming one speaker row per subtitle cue;
- terminal media anchor, recomputed terminal tail, and an inclusive `0..400` ms
  tail policy;
- selection hook/scorecard authority and the frozen semantic verdict evaluated
  against the reviewed timeline;
- exact supported boundary-policy identity and an authenticated authority
  receipt/self hash;
- optional verified recut bytes and provenance only as a cache.

Filesystem paths, hostnames, mount prefixes, inodes, mtimes, temporary names,
remote-ASR job IDs, fresh cue indices, and current ASR hashes must not participate
in authority identity, so WSL to `free` relocation preserves the same authority.

For this exact branch:

1. verify exactly one active authority and all current source/truth/selection/
   policy bindings;
2. bypass the ordinary fresh-grid boundary resolver entirely;
3. replay the frozen semantic verdict on the reviewed SRT and speaker truth;
4. materialize exactly `[577780, 735090)` from the source or a verified recut;
5. apply exact reviewed subtitles and speaker truth, then run all deterministic
   final-delivery gates;
6. optionally record fresh ASR drift as a non-authoritative witness.

The ordinary 250 ms projection rule and historical candidate behavior remain
unchanged. Under exact authority, matching closure text, a nearby fresh cue,
matching fresh effective-end milliseconds, or a unique crossing cue are not
requirements.

## Failure semantics

Block on any source/candidate/spec/interval/SRT/speaker/selection/verdict/policy
drift, malformed or ambiguous authority, interval outside the verified source,
cue beyond the interval, recomputed tail mismatch, tail above `400` ms, terminal
anchor mismatch, output-span overrun, or unsupported bound policy. There is no
nearest-match search and no legacy/live-review fallback.

A fresh ASR cue crossing the endpoint is witness-only. It becomes a hard
contradiction only when independent hash-bound human authority establishes
next-topic content before the approved endpoint. Half-open semantics permit an
authoritative next-topic start exactly at `735090` ms and reject one at
`735089` ms when the policy requires exclusion.

## Required tests

- three materially different fresh grids produce the same `[577780, 735090)`
  interval and never call the ordinary resolver;
- `60` ms and exactly `400` ms terminal tails pass; `401` ms fails;
- cue/end/anchor/tail/source/spec/SRT/speaker/selection/scorecard/policy/authority
  tampering fails closed;
- exact mode with a missing or multiple active grants blocks without fallback;
- WSL to `free` path/host/inode changes preserve authority identity;
- output source-span overrun and a truly authoritative next-topic contradiction
  fail;
- valid/corrupt recut cache behavior never re-enters live boundary selection;
- ordinary and historical candidates retain their existing projection behavior.

## Root disposition

Implement the narrow exact-authority branch and issue the candidate grant only
from authenticated evidence that the exact interval was reviewed. The observed
`734550` endpoint must become structurally unreachable for this candidate. Do
not solve the incident by widening the ordinary projection tolerance, caching a
random ASR result, or relying on another favorable stochastic retry.
