# ChatGPT Pro decision: deterministic text-only solo title resolution

- Thread: <https://chatgpt.com/c/6a7bd35c-3980-83ea-99b5-542a40d50cfa>
- Visible mode: `Pro`
- Prompt SHA-256: `99afa279a73f1e692468d7107e43e308afcbc9f0f3f0756dfc431d5a5d7c86a6`
- Normalized response SHA-256: `5f86abef13add311336f4583e4d635f735d4d3919df77ff9eb80ad4aca984956`
- Durable run record: `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-12T01-57-24-115Z-99afa279a73f1e69.json`

## Accepted decision

Use option 4: replace both publication surfaces with a closed, deterministic
rendering of Ivan's reviewed subtitle/entity truth. No new Ivan title decision
and no further source-fact provider call are required.

The former title and hook must not remain. `公主抱` is an unbound visual-action
claim; the detached `被奶P赖上了` has unresolved grammatical role attribution;
and `钓到` / `成功截图赖上` add unsupported causal or result claims. The failed
provider response remains diagnostic only and must not be relabelled `PASS` or
`KEEP`.

Freeze these exact UTF-8 NFC surfaces without BOM, surrounding whitespace, or
trailing newline:

- Title: `【李豆沙】被问有没有跳《夜蝶》的对象，xxsk说“你给了我你的第一次”，李豆沙接着问“那你要负责吗”`
- Title byte length: `142`
- Title SHA-256: `97bf7997892126ce9dfeed7d5e98a047a91b570513d6ad47c05fb7285afa9951`
- Hook: `李豆沙被问有没有跳《夜蝶》的对象后，讲到自己对xxsk说“我好容易就能抱起你”，又说“我确实这是我第一次这样抱别人”；xxsk说“你给了我你的第一次”，李豆沙接着问“那你要负责吗”，随后说“那我已经截图了”。`
- Hook byte length: `296`
- Hook SHA-256: `c6b71718c9651c63ff97efbb1cfb341b7f3ddaec4222f1bad736b2ac58f844c6`
- Combined `SHA256("title\\0" || title || "\\0hook\\0" || hook)`:
  `1204e6b99d3c12f1cacbd111c3984bc84f146ff4ad24f764e9a84e4e321f1348`

The wording reports reviewed utterances. It does not assert that a princess
carry occurred or independently prove that a screenshot exists.

## Required P0 contract

Implement one candidate-scoped `DETERMINISTIC_TEXT_NARROWING_V1` receipt that
keeps these authorities nominally distinct:

- Ivan's clip-level release grant;
- Ivan's reviewed subtitle/entity truth;
- exact human surface adjudication, which is absent here;
- visual-claim authority, which is also absent here;
- deterministic editorial/source-fact resolution.

Bind the candidate, source SHA, exact half-open interval
`[1157280, 1229510)`, reviewed SRT SHA, candidate entity projection, current
uniform-host authority, clip-release chain, exact failed source-fact attempt,
old surface hashes, exact new surface bytes/hashes, policy bundle, and a closed
surface plan. Absolute paths, hostnames, mount points, inodes, mtimes, and WSL
identifiers must remain resolver hints outside authority identity.

The closed plan may use only question/addressee/cue-order/entity surfaces and
exact reported utterances. It must have no node capable of introducing a visual
action, causal result, unresolved role attribution, unsupported actor/addressee,
or free-form factual literal. Replay is valid only when deterministic rendering
reproduces the exact frozen title and hook bytes.

Preserve the original `CPA_TEXT_REVIEW_INVALID` attempt byte-for-byte and record
the resulting states separately:

- `original_source_fact_attempt = FAILED`
- `exact_surface_resolution = VALID`
- `publication_fact_authority = RESOLVED_EXACT_SURFACE`

A later same-key provider `KEEP` is non-authoritative. Retry is permissible only
for a transport failure with no provider response bytes, never to sample a more
convenient verdict.

## Required negative tests

- source/interval/SRT/entity/uniform-host drift blocks;
- `xxsk` changed to another equivalent surface blocks;
- any byte, punctuation, quote glyph, space, case, or newline drift blocks;
- inserting `公主抱`, `钓到`, `成功`, or participant-bound `被奶P赖上了` blocks;
- swapping the speakers of `你给了我你的第一次` and `那你要负责吗` blocks;
- deleting, rewriting, or marking the failed source-fact attempt as pass blocks;
- using the clip-level release quote as an exact-title wildcard is a schema
  failure;
- WSL-to-`free` relocation with identical content hashes passes without a
  provider call.

## Root disposition

Implement the narrow closed-plan renderer and receipt, update the solo package's
title/hook through that deterministic route, and continue the already-authorized
fast lane. Do not claim Ivan approved the exact new title bytes; describe them as
a deterministic text-only resolution of Ivan-approved clip and subtitle/entity
truth.
