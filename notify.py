#!/usr/bin/env python3
"""Prepare or publish one idempotent daily privacy and AI news digest issue."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import editorial
from post_ideas import CHANNEL_LABELS, select_ideas

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "data/latest.json"
STATE = ROOT / "state/notifications.json"
TZ = ZoneInfo("Europe/Moscow")
REPO_DEFAULT = "ilyak0/privacy-ai-watch"
PAGES_DEFAULT = "https://ilyak0.github.io/privacy-ai-watch/"
CAP_ITEMS = 120
CAP_CHARS = 55_000
STREAMS = ("privacy_world", "privacy_ru", "ai_world", "ai_ru")


def today_moscow(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(TZ)).astimezone(TZ).date()


def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def item_marker(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def daily_marker(day: dt.date) -> str:
    return f"<!-- privacy-ai-watch:{day.isoformat()} -->"


def _date(value) -> dt.date | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if isinstance(parsed, dt.datetime):
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(TZ).date()
        return parsed
    except ValueError:
        return None


def _safe_title(value: str) -> str:
    # Escape Markdown punctuation and remove controls so untrusted titles stay text.
    value = "".join(c for c in str(value) if c >= " " and c != "\x7f")[:180]
    # Prevent source-supplied titles from pinging GitHub users or emitting HTML.
    value = re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", value)
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value) or "(untitled)"


def _safe_url(value: str) -> str:
    value = str(value).strip()
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    # Angle brackets and whitespace cannot escape a Markdown destination.
    return re.sub(r"[\s<>\\]", lambda m: urllib.parse.quote(m.group(0), safe=""), value)


def _stream_membership(report: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for stream in STREAMS:
        for item in (report.get("buckets") or {}).get(stream, []):
            url = item.get("canonical_url") or item.get("url")
            if url:
                result.setdefault(url, set()).add(stream)
    return result


def candidates(report: dict, state: dict, today: dt.date):
    """Return in-window unseen items and baseline changes without mutating state."""
    ledger = state.setdefault("items", {})
    window = max(1, int(report.get("publication_window_days") or 30))
    cutoff = today - dt.timedelta(days=window - 1)
    digest_cutoff = today - dt.timedelta(days=6)
    initialized = bool(state.get("initialized_at") or ledger or state.get("days"))
    memberships = _stream_membership(report)
    items = {}
    # Buckets are the canonical, report-window item set.
    for stream in STREAMS:
        for item in (report.get("buckets") or {}).get(stream, []):
            key = item.get("canonical_url") or item.get("url")
            if key:
                items.setdefault(key, dict(item, canonical_url=key))
    new, baselines = [], []
    for key, item in items.items():
        pub = _date(item.get("published_utc"))
        # Undated records remain available in the archive but are never news alerts.
        if pub is None:
            continue
        if key in ledger:
            continue
        if pub and pub < cutoff:
            # First-run archive baseline: preserve old URLs without a notification.
            baselines.append(key)
            continue
        if not initialized and pub < digest_cutoff:
            # In-window backlog older than the initial digest window is baseline too.
            baselines.append(key)
            continue
        item["_streams"] = sorted(memberships.get(key, set()))
        new.append(item)
    new.sort(key=lambda i: (*editorial.sort_key(i), i["canonical_url"]), reverse=True)
    return new, baselines


def health_summary(report: dict) -> tuple[list[str], int]:
    health = report.get("source_health") or {}
    rows, errors = [], 0
    if isinstance(health, list):
        health = {str(v.get("source_id", v.get("source", "source"))): v for v in health if isinstance(v, dict)}
    if isinstance(health, dict):
        for source, value in sorted(health.items()):
            if isinstance(value, dict):
                status = str(value.get("status", "unknown"))
                err = value.get("error") or value.get("errors")
                if err:
                    errors += 1
                    msg = _safe_title(re.sub(r"[\r\n]+", " ", str(err))[:240])
                    rows.append(f"- `{_safe_title(source)}`: ошибка ({_safe_title(status)}); {msg}")
                elif status.lower() not in ("ok", "success", "healthy"):
                    localized = {"stale": "устаревшие данные", "error": "ошибка", "empty": "нет данных"}.get(status.lower(), _safe_title(status))
                    rows.append(f"- `{_safe_title(source)}`: {localized}")
            else:
                status = str(value)
                if status.lower() not in ("ok", "success", "healthy"):
                    rows.append(f"- `{_safe_title(source)}`: {_safe_title(status[:240])}")
    return rows, errors


def build_body(report: dict, selected: list[dict], day: dt.date,
               overflow: bool = False, health_rows: list[str] | None = None,
               pages_url: str | None = None) -> str:
    pages_url = pages_url or os.environ.get("PAGES_URL", PAGES_DEFAULT)
    lines = [f"Privacy & AI news digest — {day.isoformat()}", "",
             f"[Открыть полный архив и фильтры]({pages_url})", "", "@ilyak0", ""]
    if selected:
        lines.append(f"Новых публикаций: **{len(selected)}**")
        lines.append("")
        # One row per URL; editorial groups keep practice ahead of broad AI news.
        labels = {"privacy_world": "Данные · мир", "privacy_ru": "Данные · Россия",
                  "ai_world": "ИИ · мир", "ai_ru": "ИИ · Россия"}
        for section, label in editorial.LABELS.items():
            group = [i for i in sorted(selected, key=editorial.sort_key, reverse=True)
                     if editorial.classify(i)["section"] == section]
            if not group:
                continue
            lines += [f"### {label}", ""]
            for item in group:
                url = _safe_url(item.get("url") or item.get("canonical_url", ""))
                if not url:
                    continue
                title = _safe_title(item.get("title", ""))
                date = _date(item.get("published_utc"))
                date = date.isoformat() if date else "дата неизвестна"
                tags = ", ".join(labels[s] for s in item.get("_streams", []) if s in labels)
                kinds = {"primary": "первоисточник", "commentary": "комментарий / СМИ",
                         "discovery": "поисковый индекс", "mixed": "несколько типов источников"}
                kind = kinds.get(item.get("source_kind"), "тип источника не указан")
                lines.append(f"- **{date}** — [{title}](<{url}>) — {kind}" + (f" · {tags}" if tags else ""))
            lines.append("")
        ideas = select_ideas(selected, as_of=report.get("generated_at"))
        if any(ideas.values()):
            lines += ["### Вопросы для постов", "", "Поводы по заголовкам; перед постом — проверка полного источника.", ""]
            for channel, label in CHANNEL_LABELS.items():
                if not ideas[channel]:
                    continue
                lines += [f"**{label}**", ""]
                for idea in ideas[channel]:
                    lines.append(f"- **{_safe_title(idea['question'])}** {_safe_title(idea['angle'])} "
                                 f"Повод: [{_safe_title(idea['title'])}](<{_safe_url(idea['url'])}>).")
                lines.append("")
    if health_rows:
        lines += ["", "### Состояние источников", "", *health_rows]
    if overflow:
        lines += ["", "Часть публикаций не вошла в рассылку из-за ограничения размера. "
                  f"[Посмотреть все публикации]({pages_url})."]
    lines += ["", daily_marker(day)]
    lines.extend(f"<!-- item:{item_marker(i['canonical_url'])} -->" for i in selected)
    return "\n".join(lines)


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token
        self.base = f"https://api.github.com/repos/{repo}"

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            # Never include request headers (or token) in the raised message.
            detail = e.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"GitHub API {method} failed ({e.code}): {detail[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub API connection failed: {e.reason}") from None

    def daily_issue(self, day: dt.date):
        marker = daily_marker(day)
        for page in range(1, 11):
            issues = self.request("GET", f"/issues?state=all&per_page=100&page={page}")
            for issue in issues:
                if issue.get("pull_request"):
                    continue
                body = issue.get("body") or ""
                if marker in body:
                    return issue
            if len(issues) < 100:
                break
        return None

    def create_issue(self, day: dt.date, body: str):
        return self.request("POST", "/issues", {"title": f"Privacy & AI — {day.isoformat()}",
            "body": body})


def persist_recovery(state: dict, issue: dict, day: dt.date):
    body = issue.get("body") or ""
    found = set(re.findall(r"<!-- item:([0-9a-f]{64}) -->", body))
    items = state.setdefault("items", {})
    # Reconstruct URL-keyed ledger from known first_seen records and markers in issue.
    for key, value in list(items.items()):
        if item_marker(key) in found:
            value["notified"] = True
    state.setdefault("days", {})[day.isoformat()] = {
        "issue_url": issue.get("html_url", ""), "markers": sorted(found)}


def run(report_path=REPORT, state_path=STATE, publish=False, today=None,
        github_factory=GitHub, repo=None, pages_url=None):
    day = today or today_moscow()
    report = load_json(Path(report_path), {})
    state = load_json(Path(state_path), {"items": {}, "days": {}})
    state.setdefault("items", {})
    state.setdefault("days", {})
    new, baselines = candidates(report, state, day)
    health_rows, errors = health_summary(report)
    # First-run archive older than seven days is ledgered only upon a real publication
    # or existing issue recovery; today's new items remain the digest candidates.
    selected, overflow = [], False
    for item in new:
        if len(selected) >= CAP_ITEMS:
            overflow = True
            continue
        trial = build_body(report, selected + [item], day, False, health_rows if errors else None, pages_url)
        if len(trial) > CAP_CHARS:
            overflow = True
            continue
        selected.append(item)
    overflow = overflow or len(selected) < len(new)
    body = build_body(report, selected, day, overflow, health_rows if errors else None, pages_url)
    should_publish = bool(selected or errors)
    if not publish:
        return {"prepared": True, "date": day.isoformat(), "items": len(selected),
                "overflow": max(0, len(new)-len(selected)), "source_errors": errors,
                "would_publish": should_publish}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required with --publish")
    repo = repo or os.environ.get("GITHUB_REPOSITORY", REPO_DEFAULT)
    gh = github_factory(repo, token)
    existing = gh.daily_issue(day)
    if existing:
        persist_recovery(state, existing, day)
        state.setdefault("initialized_at", report.get("generated_at"))
        # Existing markers are authoritative; do not mark unseen/capped URLs.
        marked = set(re.findall(r"<!-- item:([0-9a-f]{64}) -->", existing.get("body") or ""))
        for item in new:
            if item_marker(item["canonical_url"]) in marked:
                state["items"].setdefault(item["canonical_url"], {})
                state["items"][item["canonical_url"]].update(
                    first_seen=report.get("generated_at"), notified=True)
        for key in baselines:
            state["items"].setdefault(key, {"first_seen": report.get("generated_at"), "notified": False})
        atomic_json(Path(state_path), state)
        return {"published": True, "recovered": True, "url": existing.get("html_url", ""),
                "items": len(marked), "date": day.isoformat()}
    if not should_publish:
        return {"published": False, "reason": "no new items or source errors", "date": day.isoformat()}
    # State is changed only after confirmed creation. A lost response is safely retried
    # through the daily marker already embedded in the issue body.
    issue = gh.create_issue(day, body)
    for key in baselines:
        state["items"].setdefault(key, {"first_seen": report.get("generated_at"), "notified": False})
    for item in selected:
        state["items"][item["canonical_url"]] = {"first_seen": report.get("generated_at"), "notified": True}
    state["days"][day.isoformat()] = {"issue_url": issue.get("html_url", ""),
                                      "markers": [item_marker(i["canonical_url"]) for i in selected]}
    state.setdefault("initialized_at", report.get("generated_at"))
    atomic_json(Path(state_path), state)
    return {"published": True, "url": issue.get("html_url", ""), "items": len(selected), "date": day.isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="create the daily GitHub issue")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    try:
        result = run(args.report, args.state, args.publish)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
