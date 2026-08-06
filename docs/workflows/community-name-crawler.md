# Official streamer registry and daily community names

This workflow deliberately separates two clocks that answer different
questions:

- `crawl_streamer_registry.py` asks **who is an official streamer and which
  official surfaces identify that person?** Membership changes slowly, so the
  production schedule refreshes it weekly.
- `crawl_community_names.py` asks **which new nickname, fan name, or event meme
  is the Bilibili clipping community using for an already anchored person?**
  News and community language can change after one incident, so this stage runs
  daily and incrementally.

Neither output proves that a name occurred in a subtitle cue. Neither crawler
edits subtitles.

## Official registry

The profile-owned `streamer_registry_sources` config currently combines:

1. the existing PSPLive official roster video and reviewed compatibility
   overrides;
2. VirtuaReal's official member API; and
3. official VirtuaReal Bilibili new-member announcements, verified by numeric
   owner MID, owner name, title anchor, publication time, and member space MID.

The announcement source fills recent members that may not yet be reflected by
the slower website catalog. Members merge by stable Bilibili MID, retain
organization provenance, and expire after 35 days so four missed weekly runs
fail closed instead of silently presenting an old roster as current.

Run a read-only live snapshot:

```bash
python3 scripts/crawl_streamer_registry.py \
  --now 2026-08-06T01:50:00+00:00 \
  > /tmp/streamer_registry.json
```

Production writes `/opt/bilive/autoslice/state/streamer_registry.json` every
Sunday at 06:07 UTC. The legacy PSP snapshot refreshes Sunday at 06:12 UTC for
backward compatibility; it is no longer a daily job.
The weekly job performs at most three attempts per official endpoint (nine
requests total) so an intermittent Bilibili 412 or truncated website response
does not discard an otherwise valid refresh; all attempts still use the same
strict identity and minimum-count gates.

## Daily community relation lane

The daily stage reads the last-good official registry and gives **every member**
one fresh `pubdate/page 1` Bilibili search. The configured registry ceiling is
128; the current 112-member registry is therefore checked in one daily run,
not sampled over ten days. This freshness lane answers whether a new incident
or nickname appeared today.

Freshness is separate from historical coverage. A durable round-robin cursor
gives 16 members an additional backfill position each day, rotating
`pubdate` → `totalrank` → `click`, pages 1 → 3, and the shortest official CJK
surface (for example, `犬绒`) through the canonical and `canonical + 切片`
queries. Up to eight stored candidates also receive a surface-specific query
such as `花礼 鼠鼠`; existing candidates are deterministically matched against
every newly returned row even when the model does not propose them again.
Consequently a broad result page cannot strand an already-known name at its
first two witnesses.

The judge runs in batches of at most 16 members and only revisits BVIDs whose
metadata or sampled comment page has not yet been judged. Each member contributes
at most 50 rows per batch. Repeated target co-occurrence across titles whose
other participants change is treated as stronger identity evidence than one
ambiguous multi-person title.
The judge may propose exact metadata substrings and one of four relation types:

| Relation | Meaning |
| --- | --- |
| `alias_of` | the community uses the surface to call the person |
| `fan_name_of` | the surface names the person's fans, not the person |
| `meme_of` | an incident/persona/appearance/object meme associated with the person |
| `associated_with` | a useful association whose type is still unclear |

The judge has no mutation tools. Its output is rejected unless the surface is a
safe 2–24-character atom and appears byte-for-byte in every cited BVID's title,
description, tags, or fetched comments. Multiple registry members without an
explicit one-to-one link must not be guessed.

Comments are a first-class discovery surface because fans, rather than official
accounts, often originate nicknames and incident memes. Before the judge runs,
the crawler fairly rotates one 20-comment page across up to 24 videos, alternating
chronological and popular order so a new incident name and an established nickname
both have a discovery path. Inline child replies already returned with a root are
included within the same 20-comment cap; the crawler does not recursively fetch
unbounded reply threads.
It prefers a target's official upload and otherwise requires a single-entity
metadata anchor; a multi-person video's comment must name the target in the same
comment before it can count. Placement under an official upload strengthens
entity attribution but is explicitly **not** treated as official adoption of
the nickname.

Clear comment text and account identity are transient. The mode-0600 durable
state stores BVID, dates, uploader MID, raw-content SHA-256, and salted hashes of
the comment and commenter. It stores neither message text, username, clear
commenter MID, nor RPID. The public accepted snapshot contains only aggregate
counts. The per-runtime random salt is created during the v1 → v2 state
migration and is never committed or exported.
The current search and reply endpoints are `/x/web-interface/wbi/search/type`
and `/x/v2/reply/wbi/main`. Once per run, the crawler reads Bilibili's public
`nav` signing image names, derives the protocol mixin key, and signs both bounded
query lanes. Search requests also declare the PC platform and Bilibili search
page location. This uses no login cookie or account token; the bootstrap response
is cached like every other source response.

### Acceptance and persistence

A community relation is automatically accepted only with all of:

- score at least 8;
- at least four videos;
- at least three stable uploader MIDs;
- evidence on at least two dates;
- at least two medium/strong metadata links; and
- no uploader contributing more than half of the videos.

One uploader can publish any number of clips and still only create a
`candidate`. An official self-post has a narrower path, but still needs a
second uploader and a second video. A surface owned by another official entity
becomes `conflict`. Accepted mappings persist when search results age out; a
registry identity conflict is the only automatic downgrade path.

Comment-originated relations have a separate independence gate instead of
pretending that a comment is metadata score:

- normally, at least four distinct salted commenter keys across two videos and
  two comment dates;
- with at least one target official-upload context, at least three commenters
  plus either two videos or two dates;
- for a same-day `meme_of`, at least three commenters across two videos, with an
  official-upload context or two distinct video uploader MIDs.

The same commenter counts once across all videos. `associated_with` cannot be
accepted by comments alone. The official-comment reason code is
`OFFICIAL_UPLOAD_COMMENT_CONTEXT_PLUS_COMMUNITY_QUORUM`, not
`OFFICIAL_SELF_EVIDENCE_PLUS_INDEPENDENT_SUPPORT`.

An event/persona `meme_of` has a deliberately faster, same-day path because the
event itself may last only one news cycle: score at least 5, two strong-title
videos from two independent uploaders, and balanced contributions. Its weaker
acceptance does not grant stronger authority: it remains occurrence-neutral
context and can never become a mechanical subtitle replacement.

This is why a person nickname such as `鼠鼠`, a food-shaped community moniker
such as `蒜蓉蘑菇`, and an event/persona name such as `白色奶龙` do not share one
flat `aliases` array. The crawler may discover all three, but it must preserve
their relation type and evidence status.

Only `accepted` rows enter `community_names.json`, with the explicit policy
`COMMUNITY_RELATION_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY`. Candidate,
rejected/conflict rows and raw source text stay out of the subtitle prompt.

## Resource and failure contract

The production job runs daily at 06:27 UTC with `flock -n` and these caps:

- 177 real HTTP requests total;
- all registry members, up to 128, receive one fresh `pubdate/page 1` search;
- up to 16 historical backfill searches and 8 candidate-specific searches;
- up to 24 one-page comment discovery checks;
- at most one public WBI signing bootstrap when comment targets exist;
- one request at a time, at least 10 seconds apart, with a 40-minute HTTP-phase cap;
- 15 seconds and 512 KiB per response;
- 18-hour fresh cache, 7-day stale-on-error cache, and 1,024 cache entries.

HTTP 412/429, Bilibili risk-control code `-352`, or a request/runtime budget exhaustion opens an endpoint circuit
for that run; the crawler does not immediately repeat the same blocked URL.
Other member failures remain isolated. Stale cache can preserve already fetched
source facts, but it does not advance freshness or historical cursors. If every
search fails or is stale, the job updates only failure state and refuses to
replace the last-good prompt snapshot. State is mode 0600; the prompt-safe
snapshot is mode 0444. No cookie, account credential, raw response, title,
description, clear comment identity, or comment text is committed or exported.
Accepted/conflict decisions persist; noisy candidate/rejected state is capped
at 2,048 mappings so an unattended daily job cannot grow without bound.

Production command shape:

```bash
set -a
source /opt/bilive/autoslice/cpa.env
set +a
python3 scripts/crawl_community_names.py \
  --registry /opt/bilive/autoslice/state/streamer_registry.json \
  --cache-dir /opt/bilive/autoslice/cache/community-name-crawler \
  --state /opt/bilive/autoslice/state/community_name_state.json \
  --write /opt/bilive/autoslice/state/community_names.json
```

For a bounded canary, add repeated `--entity` values. `--no-llm` verifies
network/cursor behavior without proposing new relations.
