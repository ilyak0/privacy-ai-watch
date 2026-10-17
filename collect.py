#!/usr/bin/env python3
"""Bounded, local-only collector for configured privacy and AI news sources."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import fcntl
import gzip
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sources.json"
DEFAULT_OUT = ROOT / "data"
MAX_BYTES = 4 * 1024 * 1024
UA = "CD-News-Monitor/1.0 (public feed reader)"
UTC = dt.timezone.utc


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def canonical_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                    parts.path.rstrip("/") or "/",
                                    urllib.parse.urlencode(query), ""))


def clean_text(value: str, limit: int = 700) -> str:
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit]


def title_key(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.casefold(), flags=re.UNICODE).strip()


def title_alias_key(title: str) -> str:
    return hashlib.sha256(title_key(title).encode('utf-8')).hexdigest()


def fetch(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, text/html, application/xml;q=0.9, */*;q=0.5"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"response exceeds {MAX_BYTES} bytes")
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                data = compressed.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError(f"decompressed response exceeds {MAX_BYTES} bytes")
        return data, response.headers.get_content_type()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node: ET.Element, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for child in node.iter():
        if child is not node and local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def rss_items(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    if local_name(root.tag) not in {"rss", "feed", "rdf"}:
        raise ValueError("response is not an RSS, Atom, or RSS/RDF document")
    nodes = [n for n in root.iter() if local_name(n.tag) in {"item", "entry"}]
    items = []
    for node in nodes:
        title = child_text(node, "title")
        link = ""
        for child in node:
            if local_name(child.tag) == "link":
                link = child.attrib.get("href", "").strip() or (child.text or "").strip()
                if link:
                    break
        excerpt = child_text(node, "description", "summary", "content", "encoded")
        published = child_text(node, "pubdate", "published", "updated", "date", "issued")
        if title and link:
            items.append({"title": clean_text(title), "url": link,
                          "excerpt": clean_text(excerpt), "published": parse_date(published)})
    if nodes and not items:
        raise ValueError("RSS/Atom entries present but none had a parseable title and link")
    return items


def selector_text(node, selector: str | None) -> str:
    if not selector:
        return ""
    found = node.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def html_items(data: bytes, source: dict) -> list[dict]:
    soup = BeautifulSoup(data, "html.parser")
    selector = source.get("item_selector")
    if not selector:
        raise ValueError("HTML source missing item_selector")
    nodes = soup.select(selector)
    if not nodes:
        raise ValueError(f"HTML item_selector matched no listing containers: {selector}")
    items = []
    for node in nodes:
        title_sel = source.get("title_selector")
        title_node = node.select_one(title_sel) if title_sel else node
        title = title_node.get_text(" ", strip=True) if title_node else ""
        link_sel = source.get("link_selector")
        link_node = node.select_one(link_sel) if link_sel else (title_node if title_sel else node)
        href = link_node.get("href", "") if link_node else ""
        url = urllib.parse.urljoin(source["url"], href)
        date_sel = source.get("date_selector")
        date_node = node.select_one(date_sel) if date_sel else None
        raw_date = (date_node.get("datetime") or date_node.get_text(" ", strip=True)) if date_node else ""
        if raw_date and source.get("date_format"):
            try:
                raw_date = dt.datetime.strptime(raw_date, source["date_format"]).isoformat()
            except ValueError:
                pass
        if raw_date and source.get("date_timezone"):
            try:
                local = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if local.tzinfo is None:
                    raw_date = local.replace(tzinfo=ZoneInfo(source["date_timezone"])).isoformat()
            except ValueError:
                pass
        excerpt = selector_text(node, source.get("excerpt_selector"))
        if title and href:
            items.append({"title": clean_text(title), "url": url,
                          "excerpt": clean_text(excerpt), "published": parse_date(raw_date)})
    if nodes and not items:
        raise ValueError("HTML listing containers present but none had a parseable title and link")
    return items


def paged_html_items(source: dict, days: int, checked: dt.datetime) -> tuple[list[dict], dict]:
    """Read a verified newest-first listing until the date boundary or a hard cap."""
    template = source["page_url_template"]
    max_pages = min(16, max(1, int(source.get("max_pages", 16))))
    deadline = time.monotonic() + 120
    cutoff = checked - dt.timedelta(days=days)
    items, seen = [], set()
    meta = {"pages_fetched": 0, "bytes_received": 0, "window_coverage": "capped"}
    for page in range(1, max_pages + 1):
        if time.monotonic() >= deadline:
            break
        url = source["url"] if page == 1 else template.format(page=page)
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(source["url"]).netloc:
            raise ValueError("pagination must stay on the configured publisher host")
        data, _ = fetch(url)
        meta["pages_fetched"] += 1
        meta["bytes_received"] += len(data)
        rows = html_items(data, {**source, "url": url})
        fresh = [row for row in rows if row["url"] not in seen]
        if not fresh:
            meta["window_coverage"] = "repeated_page"
            break
        items.extend(fresh)
        seen.update(row["url"] for row in fresh)
        dates = [row["published"] for row in rows if row["published"]]
        if not dates:
            meta["window_coverage"] = "unknown_dates"
            break
        if min(dates) < cutoff:
            meta["window_coverage"] = "boundary_reached"
            break
    return items, meta


def telegram_items(data: bytes) -> list[dict]:
    soup = BeautifulSoup(data, "html.parser")
    nodes = soup.select(".tgme_widget_message")
    if not nodes:
        raise ValueError("public Telegram page has no .tgme_widget_message nodes (redirect or anti-bot likely)")
    items = []
    for node in nodes:
        body = node.select_one(".tgme_widget_message_text")
        date_link = node.select_one("a.tgme_widget_message_date")
        time_node = node.select_one("time[datetime]")
        if body and date_link and date_link.get("href"):
            date_raw = time_node.get("datetime", "") if time_node else ""
            items.append({"title": clean_text(body.get_text(" ", strip=True)),
                          "url": date_link["href"], "excerpt": clean_text(str(body)),
                          "published": parse_date(date_raw)})
    if not items:
        raise ValueError("Telegram message nodes present but none had parseable text, date, and link")
    return items


def topic_matches(item: dict, source: dict, config: dict) -> list[str]:
    corpus = f"{item['title']} {item.get('excerpt', '')}".casefold()
    if any(re.search(pattern, corpus, flags=re.I) for pattern in config.get("exclude_patterns", [])):
        return []
    forced = set(source.get("force_topics", []))
    matched = []
    for topic in source.get("topics", []):
        patterns = config.get("topics", {}).get(topic, [])
        if topic in forced or any(re.search(pattern, corpus, flags=re.I) for pattern in patterns):
            matched.append(topic)
    return matched


def process_source(source: dict, config: dict, days: int) -> tuple[dict, list[dict]]:
    checked = utcnow()
    health = {"source_id": source.get("id"), "label": source.get("label", source.get("id")),
              "region": source.get("region"), "kind": source.get("kind"),
        "url": source.get("url"), "transport": source.get("transport"),
        "checked_at": iso(checked), "status": "ok", "fetched": 0, "bytes_received": 0,
              "parsed": 0, "recent": 0, "undated": 0, "latest_publication": None,
              "error": None}
    try:
        transport = source["transport"]
        if transport == "x_cookies":
            from x_reader import fetch_posts
            parsed = fetch_posts(source)
        elif transport == "html" and source.get("page_url_template"):
            parsed, pagination = paged_html_items(source, days, checked)
            health.update(pagination)
        elif transport == "pravo_api":
            from pravo_reader import fetch_documents
            parsed, pagination = fetch_documents(source, days, checked, fetch)
            health.update({key: value for key, value in pagination.items() if key != "undated"})
        else:
            data, _ = fetch(source["url"])
            health["bytes_received"] = len(data)
            parsed = rss_items(data) if transport == "rss" else html_items(data, source) if transport == "html" else telegram_items(data)
        health["fetched"] = len(parsed)
        health["parsed"] = len(parsed)
        results = []
        cutoff = checked - dt.timedelta(days=days)
        for item in parsed:
            if source.get("title_suffix_pattern"):
                item["title"] = re.sub(source["title_suffix_pattern"], "", item["title"], flags=re.I).strip()
            item['url'] = urllib.parse.urljoin(source['url'], item['url'])
            item["published"] = item.get("published")
            published = item["published"]
            if published and published > checked + dt.timedelta(minutes=5):
                continue
            if published:
                latest = health["latest_publication"]
                health["latest_publication"] = max(latest, iso(published)) if latest else iso(published)
                if published >= cutoff:
                    health["recent"] += 1
            else:
                health["undated"] += 1
            topics = topic_matches(item, source, config)
            if not topics:
                continue
            item.update({"source_id": source["id"], "source_label": source.get("label", source["id"]),
                         "region": source["region"], "source_kind": source["kind"],
                         "jurisdictions": source.get("jurisdictions", []), "source_note": source.get("note"),
                         "topics": topics, "is_recent": bool(published and published >= cutoff),
                         "first_seen": iso(checked)})
            results.append(item)
        if health["fetched"] == 0:
            health["status"] = "valid_empty"
        elif health["recent"] == 0 and health["undated"] == 0:
            health["status"] = "stale"
        return health, results
    except Exception as exc:
        health.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        return health, []


def init_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS items (
        canonical_url TEXT PRIMARY KEY, original_url TEXT NOT NULL, title TEXT NOT NULL,
        excerpt TEXT NOT NULL, published_utc TEXT, first_seen_utc TEXT NOT NULL,
        topics_json TEXT NOT NULL, regions_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
        source_kind TEXT NOT NULL)""")
    db.execute("CREATE TABLE IF NOT EXISTS title_aliases_v2 (title_key TEXT NOT NULL, published_day TEXT NOT NULL, canonical_url TEXT NOT NULL, PRIMARY KEY(title_key,published_day))")
    db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at TEXT, days INTEGER, selected_sources_json TEXT, health_json TEXT)")
    return db


def merge_items(db: sqlite3.Connection, incoming: list[dict]) -> dict[str, list[str]]:
    current_sources: dict[str, set[str]] = {}
    for item in incoming:
        key = canonical_url(item["url"])
        normalized_title = title_alias_key(item["title"])
        published_day = iso(item.get("published"))[:10] if item.get("published") else ""
        row = db.execute("SELECT original_url,title,excerpt,published_utc,first_seen_utc,topics_json,regions_json,provenance_json,source_kind FROM items WHERE canonical_url=?", (key,)).fetchone()
        if not row and normalized_title and published_day:
            alias = db.execute("SELECT canonical_url FROM title_aliases_v2 WHERE title_key=? AND published_day=?", (normalized_title, published_day)).fetchone()
            if alias:
                key = alias[0]
                row = db.execute("SELECT original_url,title,excerpt,published_utc,first_seen_utc,topics_json,regions_json,provenance_json,source_kind FROM items WHERE canonical_url=?", (key,)).fetchone()
        observation = {"url": item["url"], "published_utc": iso(item.get("published")), "topics": item["topics"]}
        source_meta = {"label": item["source_label"], "region": item["region"], "kind": item["source_kind"],
                       "observations": [observation]}
        if item.get("jurisdictions"):
            source_meta["jurisdictions"] = item["jurisdictions"]
        if item.get("source_note"):
            source_meta["note"] = item["source_note"]
        prov = {item["source_id"]: source_meta}
        if row:
            topics = set(json.loads(row[5])) | set(item["topics"])
            regions = set(json.loads(row[6])) | {item["region"]}
            provenance = json.loads(row[7])
            prior = provenance.get(item["source_id"])
            if prior:
                prior.update({k: v for k, v in source_meta.items() if k != "observations"})
                if observation not in prior.setdefault("observations", []):
                    prior["observations"].append(observation)
            else:
                provenance.update(prov)
            pub = row[3] or iso(item.get("published"))
            kinds = {p["kind"] for p in provenance.values()}
            merged_kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
            db.execute("UPDATE items SET topics_json=?,regions_json=?,provenance_json=?,published_utc=?,source_kind=? WHERE canonical_url=?",
                       (json.dumps(sorted(topics)), json.dumps(sorted(regions)), json.dumps(provenance, ensure_ascii=False), pub, merged_kind, key))
        else:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", (
                key, item["url"], item["title"], item["excerpt"], iso(item.get("published")),
                item["first_seen"], json.dumps(item["topics"]), json.dumps([item["region"]]),
                json.dumps(prov, ensure_ascii=False), item["source_kind"]))
        if normalized_title and published_day:
            db.execute("INSERT OR IGNORE INTO title_aliases_v2 VALUES (?,?,?)", (normalized_title, published_day, key))
        current_sources.setdefault(key, set()).add(item["source_id"])
    db.commit()
    return {key: sorted(ids) for key, ids in current_sources.items()}


def read_recent(db: sqlite3.Connection, days: int) -> list[dict]:
    cutoff = iso(utcnow() - dt.timedelta(days=days))
    rows = db.execute("SELECT canonical_url,original_url,title,excerpt,published_utc,first_seen_utc,topics_json,regions_json,provenance_json,source_kind FROM items WHERE published_utc>=? ORDER BY published_utc DESC", (cutoff,)).fetchall()
    result = []
    for r in rows:
        provenance = json.loads(r[8]); regions = json.loads(r[7])
        result.append(dict(canonical_url=r[0], url=r[1], title=r[2], excerpt=r[3], published_utc=r[4],
                           first_seen_utc=r[5], topics=json.loads(r[6]), regions=regions,
                           region=regions[0] if len(regions) == 1 else "mixed",
                           source_ids=sorted(provenance), provenance=provenance, source_kind=r[9]))
    return result


def read_undated(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT canonical_url,original_url,title,excerpt,first_seen_utc,topics_json,regions_json,provenance_json,source_kind FROM items WHERE published_utc IS NULL ORDER BY first_seen_utc DESC").fetchall()
    result = []
    for r in rows:
        provenance = json.loads(r[7]); regions = json.loads(r[6])
        result.append(dict(canonical_url=r[0], url=r[1], title=r[2], excerpt=r[3], published_utc=None,
                           first_seen_utc=r[4], topics=json.loads(r[5]), regions=regions,
                           region=regions[0] if len(regions) == 1 else "mixed",
                           source_ids=sorted(provenance), provenance=provenance, source_kind=r[8]))
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def display_priority(item: dict) -> tuple:
    """Prioritize practice signals in the view; keep every match in the archive."""
    title = item["title"]
    score = 3 if any(p["kind"] == "primary" for p in item["provenance"].values()) else 0
    if re.search(r"enforce|guideline|regulat|ruling|court|fine[ds]?\b|AI Act|\blaw\b|decision|consultation|штраф|суд|закон|регулир|разъяснен|обезлич|ответственност|трансгранич", title, re.I):
        score += 3
    if re.search(r"\bwebinar\b|\bconference\b|\bregister\b|вебинар|конференци|регистрац|приглаша|промокод|скидк", title, re.I):
        score -= 6
    return score, item["published_utc"]


def md_text(value: str) -> str:
    return re.sub(r"([\\`*_\[\]])", r"\\\1", value)


def write_reports(out_dir: Path, items: list[dict], undated: list[dict], health: list[dict], days: int, run_id: str, current_sources: dict[str, list[str]]) -> None:
    now = utcnow()
    buckets = {name: [] for name in ("privacy_world", "privacy_ru", "ai_world", "ai_ru")}
    for item in items:
        item["current_source_ids"] = current_sources.get(item["canonical_url"], [])
        item["is_current_fetch"] = bool(item["current_source_ids"])
        observed_buckets = {f"{topic}_{p['region']}"
                            for p in item["provenance"].values()
                            for observation in p.get("observations", [])
                            for topic in observation["topics"]}
        for key in observed_buckets:
            if key in buckets:
                buckets[key].append(item)
    for item in undated:
        item["current_source_ids"] = current_sources.get(item["canonical_url"], [])
        item["is_current_fetch"] = bool(item["current_source_ids"])
    report = {"run_id": run_id, "generated_at": iso(now), "publication_window_days": days,
              "recent_unique_items": len(items),
              "buckets": buckets, "undated": undated, "source_health": health}
    atomic_write(out_dir / "latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [f"# Новости о данных и ИИ — {now.date().isoformat()}", "",
             f"Окно публикации: последние {days} дней. Записи без даты публикации сохранены отдельно и не считаются свежими новостями.",
             f"Уникальных публикаций в окне: **{len(items)}**. Ошибок источников: **{sum(h['status'] == 'error' for h in health)}/{len(health)}**.",
             "", "В подборке выше идут новости регулирования и прямые публикации ведомств; анонсы мероприятий ниже. Полный список — в latest.json и архиве.",
             "", "Метки: `primary` — публикация ведомства; `commentary` — автор/СМИ; `discovery` — поисковый индекс, оригинал не проверен.", ""]
    health_lines = ["", "## Состояние источников", "", "<details><summary>Показать проверку каждого источника</summary>", ""]
    for h in health:
        health_lines.append(f"- **{h['label']}** ({h['region']}, {h['kind']}) — `{h['status']}`; разобрано: {h['parsed']}, свежих до тематического отбора: {h['recent']}, без даты: {h['undated']}; последняя публикация: {h['latest_publication'] or 'неизвестна'}" + (f"; ошибка: {h['error']}" if h['error'] else ""))
    for key, group in buckets.items():
        topic, region = key.rsplit("_", 1)
        topic_label = "Конфиденциальность и защита данных" if topic == "privacy" else "Искусственный интеллект"
        region_label = "Международные источники" if region == "world" else "Источники из России / на русском языке (юрисдикция события может отличаться)"
        lines += ["", f"## {topic_label} — {region_label}", "", f"В архиве за {days} дней: {len(group)}. Подборка:", ""]
        for item in sorted(group, key=display_priority, reverse=True)[:12]:
            source_labels = ", ".join(f"{p['label']} [{p['kind']}]" for p in item["provenance"].values())
            current_label = "получено при этом запуске" if item["is_current_fetch"] else f"из архива; впервые замечено {item['first_seen_utc'][:10]}"
            title = item['title'][:180] + ('…' if len(item['title']) > 180 else '')
            lines.append(f"- [{md_text(title)}](<{item['url']}>) — {item['published_utc'][:10]} | {source_labels} | {current_label}")
            if item["excerpt"]:
                lines.append(f"  {md_text(item['excerpt'][:240])}{'…' if len(item['excerpt']) > 240 else ''}")
        if not group:
            lines.append("Публикаций с датой в указанном окне нет.")
    lines += ["", "## Архив записей без даты публикации", ""]
    for item in undated:
        sources = ", ".join(p["label"] for p in item["provenance"].values())
        lines.append(f"- [{item['title']}]({item['url']}) — дата публикации неизвестна; впервые замечено {item['first_seen_utc']}; {sources}")
    if not undated:
        lines.append("Записей без даты нет.")
    health_lines += ["", "</details>"]
    atomic_write(out_dir / "latest.md", "\n".join(lines + health_lines) + "\n")
    archive = out_dir / "history" / f"{now.date().isoformat()}-{run_id}.json"
    atomic_write(archive, json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("version") != 1 or not isinstance(config.get("sources"), list):
        raise ValueError("config must have version=1 and a sources array")
    for source in config["sources"]:
        if source.get("transport") not in {"rss", "telegram", "html", "x_cookies", "pravo_api"}:
            raise ValueError(f"invalid transport for source {source.get('id')}")
        if source.get("region") not in {"world", "ru"} or source.get("kind") not in {"primary", "commentary", "discovery"}:
            raise ValueError(f"invalid region/kind for source {source.get('id')}")
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--source", action="append", help="source id; repeatable")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    try:
        config = load_config(args.config)
    except Exception as exc:
        parser.error(str(exc))
    sources = [s for s in config["sources"] if s.get("enabled", True) and (not args.source or s["id"] in args.source)]
    if args.source and {s["id"] for s in sources} != set(args.source):
        parser.error("one or more selected source IDs are missing or disabled")
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / ".collector.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another collection run holds the output lock")
        started = utcnow(); run_id = hashlib.sha256(f"{iso(started)}:{os.getpid()}".encode()).hexdigest()[:12]
        health, incoming = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as pool:
            futures = [pool.submit(process_source, s, config, args.days) for s in sources]
            for future in concurrent.futures.as_completed(futures):
                h, items = future.result(); health.append(h); incoming.extend(items)
        db = init_db(out / "archive.sqlite3")
        current_sources = merge_items(db, incoming)
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, iso(started), args.days,
                   json.dumps([s["id"] for s in sources]), json.dumps(health, ensure_ascii=False)))
        db.commit()
        items = read_recent(db, args.days)
        undated = read_undated(db)
        db.close()
        write_reports(out, items, undated, health, args.days, run_id, current_sources)
        errors = sum(h["status"] == "error" for h in health)
        print(f"run={run_id} sources={len(sources)} errors={errors} new_or_seen={len(incoming)} dated_recent_archive={len(items)}")
        return 1 if errors and not any(h["status"] != "error" for h in health) else 0


if __name__ == "__main__":
    raise SystemExit(main())
