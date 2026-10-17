"""Select a short, source-diverse briefing of strong privacy/AI legal-practice news."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any

from editorial import classify

MAX_AGE = dt.timedelta(days=7)
MAX_PER_FAMILY = 2
MAX_PER_THEME = 2

SIGNALS = (
    ("breach", re.compile(r"\b(?:data breach|security breach|security incident|breach notification|утечк\w*|инцидент информационной безопасност\w*|уведомлен\w* об инцидент\w*)\b", re.I), "Инцидент и раскрытие данных"),
    ("enforcement", re.compile(r"\b(?:fined|fines|fine\b|penalt\w*|sanction\w*|enforcement action|lawsuit|sued|complaint filed|court (?:rules|orders|decision)|regulator(?:y)? decision|DPA decision|decision on|штраф\w*|оштраф\w*|иск(?:а|у|ом|е|и|ов|ами|ах|ам)?|жалоб\w*|суд (?:решил|постановил|оштрафовал)|решени\w* надзорн\w* орган\w*|предписан\w*|расследован\w*)\b", re.I), "Правоприменение и решения"),
    ("cross_border", re.compile(r"\b(?:cross[- ]border|international transfer|data localization|data localisation|transfer mechanism|binding corporate rules|\bBCR\b|трансгранич\w*|локализац\w*|передач\w* данн\w* за рубеж|трансфер данн\w*)\b", re.I), "Передача данных и BCR"),
    ("ai_rights", re.compile(r"\b(?:AI liability|algorithmic liability|copyright\w*|training data|AI training|model training|generative AI.*(?:rights|copyright)|авторск\w+ прав\w*|обучен\w* (?:модел\w*|ии|нейросет\w*)|ответственност\w* за ии)\b", re.I), "Права и ответственность в ИИ"),
    ("biometrics", re.compile(r"\b(?:biometric\w*|facial recognition|face recognition|fingerprint\w*|биометр\w*|распознавани\w* лиц|отпечат\w* пальц\w*)\b", re.I), "Биометрические данные"),
    ("automated_decisions", re.compile(r"\b(?:automated decision\w*|algorithmic decision\w*|automated scoring|credit scoring|profiling|human review|автоматизированн\w* решени\w*|скоринг\w*|профилирован\w*|провер\w* человеком)\b", re.I), "Автоматизированные решения и оценка"),
    ("workplace_monitoring", re.compile(r"\b(?:employee monitoring|workplace monitoring|worker surveillance|employee surveillance|мониторинг\w* сотрудник\w*|слежк\w* за сотрудник\w*|контрол\w* работников)\b", re.I), "Мониторинг на рабочем месте"),
    ("consent_cookies", re.compile(r"\b(?:cookie\w*|consent|tracking pixels?|user choice|соглас\w* на обработк\w*|cookie\w*|куки|трекер\w*)\b", re.I), "Согласие и трекеры"),
    ("regulatory_initiative", re.compile(r"\b(?:bill|draft law|proposed law|proposal to regulate|regulation|regulatory framework|AI Act|legislation|законопроект\w*|проект закон\w*|инициатив\w* по регулирован\w*|регулирован\w*|акт об искусственн\w* интеллект\w*)\b", re.I), "Инициатива или регулирование"),
    ("guidance", re.compile(r"\b(?:guidance|guidelines|recommendations|best practices|published (?:a )?guide|privacy guidance|data protection guidance|руководств\w*|рекомендац\w*|разъяснен\w*|методическ\w* рекомендац\w*)\b", re.I), "Официальные рекомендации"),
    ("privacy_operations", re.compile(r"\b(?:data processing agreement|DPA|privacy program|contractual terms|data retention|data minimization|договор\w* об обработк\w* данн\w*|соглашен\w* об обработк\w*|срок\w* хранен\w* данн\w*|минимизац\w* данн\w*)\b", re.I), "Практика обработки данных"),
)

EVENT_OR_SOFT = re.compile(r"\b(?:webinar|conference|workshop|course|masterclass|registration|join us|conversation with|interview with|podcast|chat with|приглаша\w*|вебинар\w*|конференц\w*|курс\w*|разговор с|беседа с|подкаст\w*)\b", re.I)
GENERIC_NOTICE = re.compile(r"^\s*NOTICE OF DATA BREACH(?:\s*[-:—].*)?\s*$", re.I)
WEAK_CONVERSATION = re.compile(r"\b(?:conversation with|interview with|podcast with|chat with|a conversation|new essay|разговор с|беседа с|эксперт (?:назвал|о)|эксперт по.{0,40}:)\b", re.I)
STOP = set("the a an and or of for in on to with from by over after under as at is are was were says said new its this that their about into according report reports news privacy personal data processing authority commission regulator dpa edpb court opinion decision fine fines fined euros euro eur dollars usd million m billion ai act law laws guide guidance on the eu uk us eu e p d b publication update statement".split())
AUTHORITY_WORDS = {"edpb", "dpa", "cnil", "ico", "ftc", "oaic", "iapp", "pipc", "commission", "authority", "court", "regulator", "norwegian", "european", "federal", "privacy", "data", "protection", "minister", "ministry", "office", "board", "committee", "act", "ai", "eu", "us", "uk", "против", "суд", "новый", "новые"}

WHY_OPEN = {
    "breach": "Повод сверить порядок реакции на инциденты и уведомления о них с описанным случаем.",
    "enforcement": "Кейс для проверки собственных процессов: стоит открыть факты, претензии и стадию разбирательства.",
    "cross_border": "Материал для работы с международными потоками данных и механизмами их передачи.",
    "ai_rights": "Повод проверить права на данные для обучения ИИ, условия использования и распределение ответственности.",
    "biometrics": "Повод проверить цель сбора биометрии, условия её использования и доступные альтернативы.",
    "automated_decisions": "Материал для проверки скоринга, профилирования и пересмотра автоматизированных решений.",
    "workplace_monitoring": "Повод сверить цели, объём и прозрачность мониторинга работников.",
    "consent_cookies": "Материал для проверки согласий, трекеров и интерфейсов пользовательского выбора.",
    "regulatory_initiative": "Сигнал для планирования изменений: сначала выяснить стадию, адресатов и сроки инициативы.",
    "guidance": "Рекомендации могут дать практические ориентиры; стоит проверить адресатов и область применения.",
    "privacy_operations": "Материал для сверки договоров об обработке данных, ролей сторон и сроков хранения.",
}


def _parse_dt(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _as_of(value: dt.datetime | str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _valid_url(value: Any) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        return str(value)
    except ValueError:
        return ""


def _sources(item: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    provenance = item.get("provenance") or {}
    if isinstance(provenance, dict):
        for key, value in provenance.items():
            if isinstance(value, dict):
                result.append({"id": str(key), **value})
    entries = item.get("sources") or []
    if isinstance(entries, list):
        result.extend(x for x in entries if isinstance(x, dict))
    ids = item.get("source_ids") or []
    known = {str(s.get("id") or s.get("source_id") or "") for s in result}
    for source_id in ids:
        if str(source_id) not in known:
            result.append({"id": str(source_id)})
    if item.get("source_kind") and not result:
        result.append({"id": "", "kind": item["source_kind"]})
    return result


def _source_kinds(item: dict[str, Any]) -> list[str]:
    kinds = {str(item["source_kind"])} if item.get("source_kind") else set()
    kinds.update(str(s["kind"]) for s in _sources(item) if s.get("kind"))
    return sorted(kinds)


def _source_families(item: dict[str, Any], kinds: list[str]) -> list[str]:
    families = set()
    parsed = urllib.parse.urlsplit(str(item.get("url") or item.get("canonical_url") or ""))
    host = (parsed.hostname or "").lower()
    for source in _sources(item):
        sid = str(source.get("family") or source.get("family_id") or source.get("id") or source.get("source_id") or "").lower()
        kind = str(source.get("kind") or "").lower()
        label = str(source.get("label") or "").lower()
        if host in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"} or sid.startswith(("x-", "twitter-")):
            family = "x"
        elif host.endswith("news.google.com") or "google news" in label or (kind == "discovery" and "поиск публикаций" in label):
            family = "google_news"
        elif sid:
            family = re.sub(r"-(?:news|feed|rss|publications|decisions|updates|search)$", "", sid)
            if family.startswith(("x-", "twitter-")):
                family = "x"
        elif host:
            family = host.removeprefix("www.")
        else:
            family = "unknown"
        families.add(family or "unknown")
    if not families:
        families.add("unknown")
    # Prefer a primary publisher family for diversity accounting on multi-source records.
    primary_ids = {str(s.get("family") or s.get("family_id") or s.get("id") or s.get("source_id") or "").lower()
                   for s in _sources(item) if str(s.get("kind", "")).lower() == "primary"}
    if primary_ids:
        preferred = {re.sub(r"-(?:news|feed|rss|publications|decisions|updates|search)$", "", sid)
                     for sid in primary_ids if sid}
        if preferred:
            return sorted(preferred)
    return sorted(families)


def _theme(title: str) -> tuple[str, str, list[str]] | None:
    if GENERIC_NOTICE.fullmatch(title):
        return None
    if WEAK_CONVERSATION.search(title):
        return None
    if EVENT_OR_SOFT.search(title) and classify({"title": title})["section"] == "events":
        return None
    # A proposal's stage is more useful here than the subject area it would regulate.
    initiative = re.compile(
        r"\b(?:draft (?:law|bill|regulation)|proposed (?:law|bill|rules)|bill (?:proposes|would)|законопроект\w*|проект закон\w*)\b",
        re.I,
    )
    matches = [m.group(0) for m in initiative.finditer(title)]
    if matches:
        return "regulatory_initiative", "Инициатива или регулирование", matches[:4]
    for theme, pattern, label in SIGNALS:
        matches = [m.group(0) for m in pattern.finditer(title)]
        if matches:
            return theme, label, matches[:4]
    return None


def _tokens(title: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[\w]+", title, re.UNICODE)
            if len(x) > 2 and x.lower() not in STOP and not x.isdigit()}


def _actors(title: str) -> set[str]:
    return {x.lower().strip("'.") for x in re.findall(r"(?<!\w)[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9&'.-]{2,}", title)
            if x.lower() not in AUTHORITY_WORDS and x.lower() not in STOP}


def _story_topics(title: str) -> set[str]:
    checks = {
        "breach": r"\b(?:breach|incident|утечк|инцидент)\w*",
        "cookies": r"\b(?:cookie|cookies|куки|трекер)\w*",
        "advertising": r"\b(?:advertis\w*|target\w*|adtech|real.time bidding|реклам\w*|таргет\w*)\b",
        "copyright": r"\b(?:copyright\w*|author\w* rights|музык\w* датасет|авторск\w* прав\w*)\b",
        "training": r"\b(?:training data|training|обучен\w*)\b",
        "transfer": r"\b(?:transfer|cross.border|locali[sz]ation|BCR|трансгранич\w*|локализац\w*)\b",
        "biometric": r"\b(?:biometric\w*|facial recognition|биометр\w*|распознавани\w* лиц)\b",
        "processing": r"\b(?:personal data|data processing|unlawful processing|персональн\w* данн\w*|обработк\w* данн\w*)\b",
        "automated": r"\b(?:automated decision\w*|scoring|profiling|автоматизированн\w* решени\w*|скоринг\w*)\b",
        "employment": r"\b(?:employee monitoring|workplace monitoring|сотрудник\w*|работник\w*)\b",
        "enforcement": r"\b(?:fines?|fined|penalt\w*|штраф\w*|оштраф\w*)\b",
    }
    return {name for name, pattern in checks.items() if re.search(pattern, title, re.I)}


def _amounts(title: str) -> set[float]:
    result = set()
    pattern = r"(?:€|eur|usd|\$|£)\s*([0-9][0-9., ]*)\s*(million|billion|mln|bn|m\b)?"
    for number, scale in re.findall(pattern, title, re.I):
        number = number.replace(" ", "").strip(".,")
        # A lone separator before 1–2 digits is decimal; otherwise a grouping mark.
        if re.fullmatch(r"\d+[.,]\d{1,2}", number):
            number = number.replace(",", ".")
        else:
            number = number.replace(",", "").replace(".", "")
        try:
            multiplier = 1e9 if scale.lower() in {"billion", "bn"} else 1e6 if scale else 1
            result.add(float(number) * multiplier)
        except ValueError:
            continue
    return result


def _duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["url"] == b["url"] or a.get("canonical_url") == b.get("canonical_url"):
        return True
    if a["theme"] != b["theme"]:
        return False
    ta, tb = a["_tokens"], b["_tokens"]
    if ta and tb and ta == tb:
        return True
    actors = a["_actors"] & b["_actors"]
    topics = a["_topics"] & b["_topics"]
    if not actors or not topics:
        return False
    aa, ab = a["_amounts"], b["_amounts"]
    comparable_amount = bool(aa and ab and any(abs(x - y) <= max(x, y) * .02 for x in aa for y in ab))
    if aa and ab and not comparable_amount:
        return False
    if comparable_amount and "enforcement" in topics and abs(a["_date"] - b["_date"]) <= dt.timedelta(days=2):
        return True
    overlap = len(ta & tb) / max(1, len(ta | tb))
    return overlap >= 0.25 or (len(actors) >= 2 and len(topics) >= 1)


def _quality(item: dict[str, Any]) -> tuple[int, int, dt.datetime, str]:
    kinds = item["_kinds"]
    primary = int("primary" in {k.lower() for k in kinds})
    score = int(classify({**item, "excerpt": ""})["score"])
    return primary, score, item["_date"], item["title"]


def _prepare(item: dict[str, Any], date: dt.datetime, theme: str, label: str,
             headline_matches: list[str]) -> dict[str, Any] | None:
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    url = _valid_url(item.get("url") or item.get("canonical_url"))
    if not url:
        return None
    kinds = _source_kinds(item)
    families = _source_families(item, kinds)
    primary = "primary" in {kind.lower() for kind in kinds}
    source_kind = kinds[0] if len(kinds) == 1 else "mixed" if kinds else "unknown"
    verification = ("Сверить первичную публикацию и точную формулировку до цитирования."
                    if any(k.lower() in {"discovery", "commentary"} for k in kinds)
                    else "Проверить полный текст и стадию документа перед выводами.")
    return {
        "url": url, "title": title, "published_utc": item.get("published_utc"),
        "source_kind": source_kind, "source_kinds": kinds, "basis": "headline",
        "signal_label": label, "why_open": WHY_OPEN[theme], "verification_needed": verification,
        "theme": theme, "source_family": families[0],
        "evidence": {"headline_matches": headline_matches, "source_kinds": kinds},
        "_date": date, "_kinds": kinds, "_actors": _actors(title),
        "_tokens": _tokens(title), "_topics": _story_topics(title), "_amounts": _amounts(title),
        "_primary": primary, "canonical_url": item.get("canonical_url") or url,
    }


def select_briefing(items: list[dict[str, Any]], limit: int = 5,
                    as_of: dt.datetime | str | None = None) -> list[dict[str, Any]]:
    """Select up to five recent, diverse practice headlines without legal conclusions."""
    if limit <= 0:
        return []
    try:
        anchor = _as_of(as_of)
    except (TypeError, ValueError):
        return []
    candidates = []
    seen_urls = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        published = _parse_dt(item.get("published_utc"))
        if published is None or published > anchor or anchor - published > MAX_AGE:
            continue
        title = item.get("title")
        if not isinstance(title, str):
            continue
        if classify({**item, "excerpt": ""})["section"] != "practice":
            continue
        matched = _theme(title)
        if matched is None:
            continue
        theme, label, headline_matches = matched
        candidate = _prepare(item, published, theme, label, headline_matches)
        if candidate is None:
            continue
        dedupe_url = candidate["canonical_url"]
        if dedupe_url in seen_urls:
            # Multiple report buckets can share a canonical item; keep the richer record.
            for index, old in enumerate(candidates):
                if old["canonical_url"] == dedupe_url and _quality(candidate) > _quality(old):
                    candidates[index] = candidate
                    break
            continue
        seen_urls.add(dedupe_url)
        candidates.append(candidate)

    # Collapse same-story cross-posts only when both a named actor and a specific
    # topic overlap; amount conflicts keep distinct enforcement matters separate.
    deduped = []
    for candidate in sorted(candidates, key=_quality, reverse=True):
        duplicate_index = next((i for i, kept in enumerate(deduped) if _duplicate(candidate, kept)), None)
        if duplicate_index is None:
            deduped.append(candidate)
        elif _quality(candidate) > _quality(deduped[duplicate_index]):
            deduped[duplicate_index] = candidate

    selected, family_counts, theme_counts = [], {}, {}
    for candidate in sorted(deduped, key=_quality, reverse=True):
        family, theme = candidate["source_family"], candidate["theme"]
        if family_counts.get(family, 0) >= MAX_PER_FAMILY or theme_counts.get(theme, 0) >= MAX_PER_THEME:
            continue
        selected.append(candidate)
        family_counts[family] = family_counts.get(family, 0) + 1
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
        if len(selected) >= limit:
            break

    public_fields = {"url", "title", "published_utc", "source_kind", "source_kinds", "basis",
                     "signal_label", "why_open", "verification_needed", "theme", "source_family", "evidence"}
    return [{key: value for key, value in candidate.items() if key in public_fields} for candidate in selected]
