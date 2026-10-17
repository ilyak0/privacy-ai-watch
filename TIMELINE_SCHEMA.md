# Story timelines

Version 1 tracks dated public publications, not independently verified dates of
legal events or inferred legal status. Matching uses only the supplied headline.
No LLM, private notes, excerpts or full legal texts enter the state.

- `stories.json`: `version:1`, `stories:[{id,label,scope,all_of:[[regex,...],...],exclude:[regex,...]}]`.
  All groups must match; one regex per group suffices. Registry order resolves
  overlaps, specific cases before broader initiatives. IDs are permanent slugs.
- `state/timelines.json`: `version:1`, `generated_at`, `stories:[{id,label,scope,enabled,
  initialized_at,baseline_event_ids:[sha256,...],events:[...]}]`.
- Event: `id` is SHA256 of its canonical HTTP(S) URL; `canonical_url`, `url`,
  `title` (max 180 characters plus ellipsis), `published_utc`, `first_seen_utc`,
  `sources:[{label,kind,url}]`. Source kinds remain separate; multiple observations
  of one canonical record add source links. Different URLs with different
  headlines remain separate publications; they are not guessed to be one event.
- Missing/naive/invalid/future publication dates and unsafe URLs do not create
  entries. The date is always publication time, never first-seen or document date.
- Pure API: `update_timelines(previous, items, catalog, as_of) -> state`;
  `public_timelines(state, as_of=None) -> list[story]`; each public story also has
  `publication_count`, `latest_publication`, `history_status` (`watching` for 0–1
  publications, `history` for 2+), and `stale` (>30 days).
  `pending_updates(state, delivered, day, window_days=30) -> list[dict]` adds
  `story_id`, `story_label`, and `delivery_key` (SHA256 of story ID + ':' + event ID)
  to an event. `delivered` is the notification ledger's `timeline_events` map.
- Events survive refresh, missing feeds and the rolling report window. Known
  canonical URLs update metadata without changing event IDs. Source links are
  merged. Removed stories remain in the public repository state with `enabled:false`
  and are omitted from the active public view; their history is retained.
- On a story's first creation, its existing entries become `baseline_event_ids`.
  These are history, never fresh timeline email updates. Later matching entries
  remain pending until actually displayed in a confirmed daily issue.
- Notification attaches pending story updates to the same URL's existing news
  candidate, or adds an absent eligible candidate. A URL gets one full row in the
  story section instead of another row in the bulk list. Item and timeline hash
  markers are both written; only shown markers enter the ledger. Same-day retry
  recovers both sets of markers without modifying or recreating the issue.
- CLI `python3 timelines.py` reads `state/items.jsonl`, `stories.json`, and prior
  `state/timelines.json`, then atomically replaces the timeline only after success.
  It runs after archive export and before Pages/notification. Pages reads this
  independent state through `--timelines`; tests pass explicit states and never
  read production state implicitly.
