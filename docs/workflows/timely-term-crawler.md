# Timely-term crawler

## Purpose and authority boundary

`scripts/crawl_timely_terms.py` builds the source-backed candidate snapshot used
by subtitle entity verification. Its default window is anchored to the runtime
date and covers nine months backward and six months forward.

The snapshot is **not subtitle authority**. It may raise a current anime,
project, character-family, or ACG-event name as a candidate, but it must never
override incompatible audio or exact structured SC/danmaku. Runtime prompt
rendering remains recording-date filtered and capped at 64 active terms.

Raw page bodies, RSS descriptions, and headlines never enter a correction
prompt. RSS can only:

1. attach a dated article source to an already known structured anime title; or
2. emit a locally configured event name such as `AnimeJapan` or `Anime Expo`.

This avoids turning untrusted news/chat text into instructions or an unbounded
per-cue web-search loop.

## Sources

- **AniList GraphQL** is the bounded 15-month recall lane. Three pages of at
  most 50 popular anime plus one page of 50 manga/light-novel projects are
  fetched by start-date range. Canonical/native/
  romaji titles, synonyms, relations, dates, popularity, record update time,
  and stable item URL are structured fields. `updatedAt` is used as the catalog
  publication/update date; records updated after the pinned `--now` date are
  excluded.
- **Bangumi `/calendar`** is a one-request Chinese-name enrichment lane for the
  current weekly schedule. A Chinese canonical candidate is emitted only when
  the Bangumi Japanese name exactly equals an AniList title surface. Its
  `published_at` records the date the structured calendar was observed, not an
  inferred franchise-announcement date.
- **Anime News Network RSS** supplies dated news evidence and configured ACG
  event observations. Arbitrary headline entity extraction is intentionally
  disabled.
- **TV Tokyo official anime RSS** is a first-party schedule/news enrichment
  lane. Legacy item links are upgraded to HTTPS only after their host matches
  the configured allowlist.
- `assets/lidousha/timely_term_seeds.json` preserves a small reviewed seed lane
  for Chinese names/readings/confusables that catalogs do not provide. Seeds do
  not replace automated discovery; they are merged with it and remain subject
  to source/active-date validation.

`assets/lidousha/timely_term_sources.json` is strict local configuration. Adding
a feed or event requires an explicit code/config review; remote pages cannot
add new endpoints or event-watch rules.

## Bounded operation and caching

Defaults:

- request budget: 8 network calls;
- AniList: 3 × 50 anime plus 1 × 50 manga/light-novel records maximum;
- per-response limit: 2 MiB;
- HTTP timeout: 15 seconds;
- fresh cache TTL: 18 hours;
- stale-on-error cache: at most 7 days;
- cache disk budget: 64 entries and 64 MiB;
- output cap: 240 snapshot terms;
- snapshot TTL: 2 days;
- prompt cap: 64 date-active terms.

Endpoints and redirects must stay on the credential-free HTTPS allowlist.
Cache entries are content-hashed and written atomically with mode `0600`.
Adapters are failure-isolated: one failed feed does not erase results from
healthy sources. A run exits `1` when it produced a valid partial snapshot and
reports adapter errors; it exits `2` when no valid snapshot can be produced.

## Commands

Network dry-run pinned to a reproducible processing time:

```bash
python3 scripts/crawl_timely_terms.py \
  --dry-run \
  --now 2026-07-12T12:00:00-04:00 \
  > /tmp/timely_terms.json
```

Validate the candidate through the existing strict consumer schema:

```bash
python3 scripts/gemini_slice_jingting.py \
  --validate-timely-terms /tmp/timely_terms.json
```

Atomically update a staged snapshot (this does not deploy it):

```bash
python3 scripts/crawl_timely_terms.py \
  --write assets/lidousha/timely_terms.json
```

Repeat from cache without network:

```bash
python3 scripts/crawl_timely_terms.py --offline --dry-run
```

`--write` validates the complete payload before replacement, fsyncs the new
file, and is idempotent: identical bytes report `"changed": false`.

## Known coverage limits

- AniList does not provide authoritative Chinese localization for every title;
  Bangumi enriches current weekly anime only. Reviewed seed terms remain useful
  for high-value Chinese canon and audio confusables.
- RSS is not a historical archive. The first run gets the catalog's full
  15-month title window, but news evidence accumulates through the cache only
  from future scheduled runs.
- Event discovery is conservative and starts with configured major-event names.
  It will miss a newly created event until its stable name is reviewed into the
  watch list or a future structured event source is added.
- No automatic homophone/confusable invention is attempted. Incorrect negative
  candidates are more dangerous than an empty list; audio verification and
  reviewed error truth should add those relationships.
