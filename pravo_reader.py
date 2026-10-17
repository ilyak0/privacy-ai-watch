"""Bounded reader for the official Russian publication portal Documents API.

Results are publication metadata only. This module does not download or parse
the text or PDF of an act.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import time
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


API_URL = "http://publication.pravo.gov.ru/api/Documents"
DOCUMENT_URL = "http://publication.pravo.gov.ru/document/{eo_number}"
MOSCOW = ZoneInfo("Europe/Moscow")
MAX_PAGES_PER_QUERY = 2
MAX_TOTAL_REQUESTS = 10
MAX_SECONDS = 120
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TITLE_QUERIES = (
    "персональных данных",
    "биометрических",
    "искусственного интеллекта",
    "обезличивания",
    "трансграничной передачи",
)

Fetch = Callable[[str], tuple[bytes, str]]


class ReaderBudgetExceeded(TimeoutError):
    pass


def _as_aware_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _parse_publish_date(value: object) -> dt.datetime | None:
    """Parse only publishDateShort; never substitute documentDate or now."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = dt.datetime.strptime(value.strip(), "%d.%m.%Y")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return parsed.astimezone(dt.timezone.utc)


def _title(item: dict) -> str:
    for field in ("title", "complexName", "name"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return ""


def _int_field(payload: dict, key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"official publication API schema error: {key}")
    return value


def _page_url(query: str, page: int, start: str, end: str) -> str:
    params = {
        "Name": query,
        "PublishDateSearchType": "0",
        "PublishDateFrom": start,
        "PublishDateTo": end,
        "PageSize": "200",
        "Index": str(page),
    }
    return f"{API_URL}?{urlencode(params)}"


def _get_json(fetch: Fetch, url: str, deadline: float,
              executor: concurrent.futures.ThreadPoolExecutor) -> tuple[dict, int]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReaderBudgetExceeded("reader time limit reached")
    future = executor.submit(fetch, url)
    try:
        body, content_type = future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        if future.done():
            raise
        future.cancel()
        raise ReaderBudgetExceeded("reader time limit reached") from None
    if not isinstance(body, bytes):
        raise ValueError("official publication API response body is not bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("official publication API response exceeds 4 MiB")
    if content_type and "json" not in content_type.casefold():
        raise ValueError("official publication API returned non-JSON content")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("official publication API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("official publication API response is not an object")
    if any(payload.get(key) for key in ("error", "errors", "advisoryError", "isError")):
        raise ValueError("official publication API reported an error")
    return payload, len(body)


def _items_from_page(payload: dict, *, page: int, cutoff: dt.datetime,
                     now: dt.datetime, seen: set[str]) -> tuple[list[dict], int, int]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("official publication API schema error: items")
    total = _int_field(payload, "itemsTotalCount")
    _int_field(payload, "itemsPerPage", minimum=1)
    current_page = _int_field(payload, "currentPage", minimum=1)
    pages_total = _int_field(payload, "pagesTotalCount")
    if current_page != page:
        raise ValueError("official publication API schema error: currentPage mismatch")
    if total and not raw_items:
        raise ValueError("official publication API returned an empty page with positive total")

    items: list[dict] = []
    undated = 0
    out_of_window = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("official publication API schema error: item")
        eo_number = str(raw.get("eoNumber", ""))
        if not eo_number.isdigit() or not 12 <= len(eo_number) <= 20:
            raise ValueError("official publication API schema error: eoNumber")
        title = _title(raw)
        if not title:
            raise ValueError("official publication API schema error: title")
        if eo_number in seen:
            continue
        seen.add(eo_number)
        published = _parse_publish_date(raw.get("publishDateShort"))
        if published is None:
            undated += 1
        elif published < cutoff or published > now:
            out_of_window += 1
            continue
        items.append({
            "title": title,
            "url": DOCUMENT_URL.format(eo_number=eo_number),
            "excerpt": "",
            "published": published,
        })
    return items, undated, out_of_window


def fetch_documents(source: dict, days: int, now: dt.datetime,
                    fetch: Fetch) -> tuple[list[dict], dict]:
    """Fetch title-matched official acts within a bounded publication window.

    `fetch` is injected (normally the collector's capped urllib fetch) and must
    accept a URL, returning `(body_bytes, content_type)`. Search is over the
    API's `Name` field and publication-date metadata, not full text.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    now_utc = _as_aware_utc(now)
    cutoff = now_utc - dt.timedelta(days=days)
    start = cutoff.astimezone(MOSCOW).strftime("%d.%m.%Y")
    end = now_utc.astimezone(MOSCOW).strftime("%d.%m.%Y")

    configured = source.get("title_queries", DEFAULT_TITLE_QUERIES)
    if not isinstance(configured, (list, tuple)) or any(not isinstance(q, str) for q in configured):
        raise ValueError("title_queries must be a list of strings")
    queries = list(dict.fromkeys(q.strip() for q in configured if q.strip()))
    if not queries:
        raise ValueError("title_queries must not be empty")
    if any(len(q) > 100 for q in queries):
        raise ValueError("title query exceeds 100 characters")

    meta = {
        "window_coverage": "boundary_reached",
        "pages_fetched": 0,
        "bytes_received": 0,
        "queries_requested": queries,
        "queries_fetched": [],
        "capped_queries": [],
        "api_items_total_by_query": {},
        "undated": 0,
        "out_of_window": 0,
        "stop_reason": None,
        "window_from": start,
        "window_to": end,
    }
    results: list[dict] = []
    seen: set[str] = set()
    deadline = time.monotonic() + MAX_SECONDS
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        for query_index, query in enumerate(queries):
            if meta["pages_fetched"] >= MAX_TOTAL_REQUESTS:
                meta["window_coverage"] = "capped"
                meta["stop_reason"] = "request_limit"
                meta["capped_queries"].extend(queries[query_index:])
                break
            if time.monotonic() >= deadline:
                meta["window_coverage"] = "capped"
                meta["stop_reason"] = "time_limit"
                meta["capped_queries"].extend(queries[query_index:])
                break

            pages_total: int | None = None
            fetched_for_query = 0
            api_total: int | None = None
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                if meta["pages_fetched"] >= MAX_TOTAL_REQUESTS:
                    break
                if time.monotonic() >= deadline:
                    break
                url = _page_url(query, page, start, end)
                try:
                    payload, size = _get_json(fetch, url, deadline, executor)
                except ReaderBudgetExceeded:
                    meta["window_coverage"] = "capped"
                    meta["stop_reason"] = "time_limit"
                    break
                meta["pages_fetched"] += 1
                meta["bytes_received"] += size
                api_total = _int_field(payload, "itemsTotalCount")
                pages_total = _int_field(payload, "pagesTotalCount")
                page_items, undated, out_of_window = _items_from_page(
                    payload, page=page, cutoff=cutoff, now=now_utc, seen=seen)
                meta["undated"] += undated
                meta["out_of_window"] += out_of_window
                results.extend(page_items)
                fetched_for_query += 1
                if page >= pages_total or pages_total == 0:
                    break

            if fetched_for_query:
                meta["queries_fetched"].append(query)
                meta["api_items_total_by_query"][query] = api_total
            if pages_total is not None and fetched_for_query < pages_total:
                meta["window_coverage"] = "capped"
                meta["capped_queries"].append(query)
            if meta["stop_reason"] == "time_limit":
                remaining = queries[query_index + 1:]
                meta["capped_queries"].extend(q for q in remaining if q not in meta["capped_queries"])
                break
            if meta["pages_fetched"] >= MAX_TOTAL_REQUESTS and query_index + 1 < len(queries):
                meta["window_coverage"] = "capped"
                meta["stop_reason"] = "request_limit"
                meta["capped_queries"].extend(queries[query_index + 1:])
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda item: (item["published"] is not None,
                                   item["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                                   item["url"]), reverse=True)
    return results, meta
