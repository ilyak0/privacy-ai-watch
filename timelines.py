#!/usr/bin/env python3
"""Deterministic story timeline state built only from public news headlines."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import editorial

ROOT = Path(__file__).resolve().parent
ITEMS_PATH = ROOT / "state" / "items.jsonl"
CATALOG_PATH = ROOT / "stories.json"
STATE_PATH = ROOT / "state" / "timelines.json"
UTC = dt.timezone.utc
MOSCOW = ZoneInfo("Europe/Moscow")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PATH_RE = re.compile(r"(?<!\w)(?:/Users/[^\s<>\"']+|/home/[^\s<>\"']+|~/[^\s<>\"']+)")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _as_of(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(UTC).replace(microsecond=0)
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)


def _public_text(value: Any, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = CONTROL_RE.sub(" ", text)
    text = EMAIL_RE.sub("[email removed]", text)
    text = PATH_RE.sub("[path removed]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or CONTROL_RE.search(value):
        return None
    try:
        parts = urllib.parse.urlsplit(value.strip())
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        _ = parts.port
    except (ValueError, TypeError):
        return None
    if EMAIL_RE.search(value) or PATH_RE.search(value):
        return None
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/",
                                   parts.query, ""))


def _canonical(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    kept = [(key, val) for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in
            {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                   parts.path.rstrip("/") or "/",
                                   urllib.parse.urlencode(kept), ""))


def _validate_catalog(catalog: dict) -> list[dict]:
    if not isinstance(catalog, dict) or catalog.get("version") != 1 or not isinstance(catalog.get("stories"), list):
        raise ValueError("invalid story catalog schema")
    seen: set[str] = set()
    validated = []
    for entry in catalog["stories"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid story catalog entry")
        sid, label, scope = entry.get("id"), entry.get("label"), entry.get("scope")
        if (not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+|_[a-z0-9]+)*", sid)
                or sid in seen or not isinstance(label, str) or not label.strip()
                or not isinstance(scope, str) or not scope.strip()):
            raise ValueError("invalid story id, label, or scope")
        seen.add(sid)
        groups, excludes = entry.get("all_of"), entry.get("exclude", [])
        if not isinstance(groups, list) or not groups or not isinstance(excludes, list):
            raise ValueError(f"invalid matching rules for {sid}")
        try:
            compiled = []
            for group in groups:
                if not isinstance(group, list) or not group or any(not isinstance(p, str) for p in group):
                    raise ValueError
                compiled.append([re.compile(p, re.I) for p in group])
            excluded = [re.compile(p, re.I) for p in excludes if isinstance(p, str)]
            if len(excluded) != len(excludes):
                raise ValueError
        except (re.error, ValueError):
            raise ValueError(f"invalid regex rules for {sid}") from None
        validated.append({"id": sid, "label": _public_text(label, 120), "scope": _public_text(scope, 120),
                          "groups": compiled, "exclude": excluded})
    return validated


def _matches(title: str, entry: dict) -> bool:
    return (all(any(pattern.search(title) for pattern in group) for group in entry["groups"])
            and not any(pattern.search(title) for pattern in entry["exclude"]))


def _sources_from_item(item: dict) -> list[dict]:
    result = []
    prov = item.get("provenance", {})
    if isinstance(prov, dict):
        for meta in prov.values():
            if not isinstance(meta, dict):
                continue
            label = _public_text(meta.get("label", ""), 120)
            kind = meta.get("kind")
            if not label or kind not in {"primary", "commentary", "discovery"}:
                continue
            observations = meta.get("observations") or [{}]
            if not isinstance(observations, list):
                observations = [{}]
            for obs in observations:
                if not isinstance(obs, dict):
                    continue
                url = _safe_url(obs.get("url"))
                if url:
                    result.append({"label": label, "kind": kind, "url": url})
    if not result:
        for src in item.get("sources", []) if isinstance(item.get("sources"), list) else []:
            if not isinstance(src, dict) or src.get("kind") not in {"primary", "commentary", "discovery"}:
                continue
            url = _safe_url(src.get("url"))
            label = _public_text(src.get("label", ""), 120)
            if url and label:
                result.append({"label": label, "kind": src["kind"], "url": url})
    unique = {(x["label"], x["kind"], x["url"]): x for x in result}
    return sorted(unique.values(), key=lambda x: (x["label"].casefold(), x["kind"], x["url"]))


def _event_from_item(item: dict, as_of: dt.datetime) -> dict | None:
    published = _datetime(item.get("published_utc"))
    if not published or published > as_of:
        return None
    url = _safe_url(item.get("canonical_url")) or _safe_url(item.get("url"))
    if not url:
        return None
    canonical = _canonical(url)
    title = _public_text(item.get("title"), 181)
    if not title:
        return None
    # Avoid treating event marketing as a substantive publication.
    if editorial.classify({"title": title, "excerpt": ""})["section"] == "events":
        return None
    first_seen = _datetime(item.get("first_seen_utc")) or as_of
    return {"id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "canonical_url": canonical, "url": url, "title": title,
            "published_utc": _iso(published), "first_seen_utc": _iso(first_seen),
            "sources": _sources_from_item(item)}


def _validate_previous(previous: dict | None) -> dict:
    if previous is None:
        return {"version": 1, "generated_at": None, "stories": []}
    if (not isinstance(previous, dict) or previous.get("version") != 1
            or not isinstance(previous.get("stories"), list)):
        raise ValueError("invalid timeline state schema")
    for story in previous["stories"]:
        if (not isinstance(story, dict) or not isinstance(story.get("id"), str)
                or not isinstance(story.get("events"), list)
                or not isinstance(story.get("baseline_event_ids"), list)):
            raise ValueError("invalid timeline story state")
    return previous


def update_timelines(previous: dict | None, items: list[dict], catalog: dict,
                     as_of: dt.datetime) -> dict:
    """Merge matched dated publications into persistent, per-story history."""
    now = _as_of(as_of)
    rules = _validate_catalog(catalog)
    old = _validate_previous(previous)
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    prior_by_id = {s["id"]: s for s in old["stories"]}
    catalog_by_id = {r["id"]: r for r in rules}
    outputs = []

    # Existing catalog entries preserve their complete event history.
    for rule in rules:
        prior = prior_by_id.get(rule["id"])
        if prior:
            record = {"id": rule["id"], "label": rule["label"], "scope": rule["scope"],
                      "enabled": True, "initialized_at": prior.get("initialized_at") or _iso(now),
                      "baseline_event_ids": list(dict.fromkeys(prior["baseline_event_ids"])),
                      "events": copy_events(prior["events"])}
        else:
            record = {"id": rule["id"], "label": rule["label"], "scope": rule["scope"],
                      "enabled": True, "initialized_at": _iso(now),
                      "baseline_event_ids": [], "events": []}
        outputs.append(record)

    # Keep retired records in repository state but disabled in the active view.
    for prior in old["stories"]:
        if prior["id"] not in catalog_by_id:
            retired = copy_events(prior["events"])
            outputs.append({"id": prior["id"], "label": _public_text(prior.get("label"), 120),
                            "scope": _public_text(prior.get("scope"), 120), "enabled": False,
                            "initialized_at": prior.get("initialized_at"),
                            "baseline_event_ids": list(prior["baseline_event_ids"]), "events": retired})

    out_by_id = {s["id"]: s for s in outputs}
    known = {event["canonical_url"]: event for story in outputs for event in story["events"]}
    # Match each publication to the first matching catalog rule; no fuzzy grouping.
    for item in items:
        if not isinstance(item, dict):
            continue
        event = _event_from_item(item, now)
        if event is None:
            continue
        existing = known.get(event["canonical_url"])
        if existing is not None:
            existing["sources"] = _merge_sources(existing.get("sources", []), event["sources"])
            continue
        title = _public_text(item.get("title"))
        rule = next((r for r in rules if _matches(title, r)), None)
        if rule is None:
            continue
        record = out_by_id[rule["id"]]
        record["events"].append(event)
        known[event["canonical_url"]] = event

    for rule in rules:
        record = out_by_id[rule["id"]]
        if rule["id"] not in prior_by_id:
            record["baseline_event_ids"] = sorted({e["id"] for e in record["events"]})
        record["events"].sort(key=lambda e: (e.get("published_utc", ""), e.get("id", "")))
    return {"version": 1, "generated_at": _iso(now), "stories": outputs}


def copy_events(events: list[dict]) -> list[dict]:
    copied = []
    for event in events:
        if not isinstance(event, dict):
            continue
        url = _safe_url(event.get("url"))
        canonical = _safe_url(event.get("canonical_url"))
        published = _datetime(event.get("published_utc"))
        first_seen = _datetime(event.get("first_seen_utc"))
        event_id = hashlib.sha256(_canonical(canonical or url).encode()).hexdigest() if (canonical or url) else None
        if not event_id or not published:
            continue
        copied.append({"id": event_id, "canonical_url": _canonical(canonical or url), "url": url or canonical,
                       "title": _public_text(event.get("title"), 181), "published_utc": _iso(published),
                       "first_seen_utc": _iso(first_seen or published),
                       "sources": _merge_sources([], event.get("sources", []))})
    return copied


def _merge_sources(a: list, b: list) -> list[dict]:
    values = []
    for source in list(a or []) + list(b or []):
        if not isinstance(source, dict):
            continue
        url = _safe_url(source.get("url"))
        label = _public_text(source.get("label"), 120)
        kind = source.get("kind")
        if url and label and kind in {"primary", "commentary", "discovery"}:
            values.append({"label": label, "kind": kind, "url": url})
    unique = {(x["label"], x["kind"], x["url"]): x for x in values}
    return sorted(unique.values(), key=lambda x: (x["label"].casefold(), x["kind"], x["url"]))


def public_timelines(state: dict, as_of: dt.datetime | None = None) -> list[dict]:
    now = _as_of(as_of)
    valid = _validate_previous(state)
    public = []
    for story in valid["stories"]:
        if not story.get("enabled"):
            continue
        events = [event for event in copy_events(story["events"]) if _datetime(event["published_utc"]) <= now]
        events.sort(key=lambda e: (e["published_utc"], e["id"]))
        latest = max((_datetime(e["published_utc"]) for e in events), default=None)
        public.append({"id": _public_text(story["id"], 100), "label": _public_text(story.get("label"), 120),
                       "scope": _public_text(story.get("scope"), 120), "events": events,
                       "publication_count": len(events), "latest_publication": _iso(latest) if latest else None,
                       "history_status": "history" if len(events) >= 2 else "watching",
                       "stale": latest is None or (now - latest).days > 30})
    public.sort(key=lambda s: (s["latest_publication"] or "", s["id"]), reverse=True)
    return public


def pending_updates(state: dict, delivered: dict, day: str | dt.date, window_days: int = 30) -> list[dict]:
    valid = _validate_previous(state)
    if not isinstance(delivered, dict):
        raise ValueError("delivered must be the timeline_events map")
    try:
        target = day if isinstance(day, dt.date) else dt.date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError("day must be YYYY-MM-DD") from None
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError("window_days must be a positive integer")
    output = []
    for story in valid["stories"]:
        if not story.get("enabled"):
            continue
        baseline = set(story.get("baseline_event_ids", []))
        for event in story["events"]:
            clean = copy_events([event])
            if not clean:
                continue
            event = clean[0]
            if event.get("id") in baseline:
                continue
            published = _datetime(event.get("published_utc"))
            if published is None:
                continue
            local_day = published.astimezone(MOSCOW).date()
            age = (target - local_day).days
            if age < 0 or age >= window_days:
                continue
            delivery_key = hashlib.sha256(f"{story['id']}:{event['id']}".encode()).hexdigest()
            if delivered.get(delivery_key):
                continue
            output.append({**event, "story_id": story["id"],
                           "story_label": _public_text(story.get("label"), 120),
                           "delivery_key": delivery_key})
    output.sort(key=lambda e: (e["published_utc"], e["story_id"], e["id"]))
    return output


def _read_items(path: Path) -> list[dict]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("items archive row must be an object")
            result.append(parsed)
    return result


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, default=ITEMS_PATH)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--as-of", help="reproducible aware ISO datetime")
    args = parser.parse_args()
    as_of = dt.datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else dt.datetime.now(UTC)
    if as_of.tzinfo is None:
        raise SystemExit("--as-of must include a timezone")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    previous = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else None
    new_state = update_timelines(previous, _read_items(args.items), catalog, as_of)
    _atomic_json(args.state, new_state)


if __name__ == "__main__":
    main()
