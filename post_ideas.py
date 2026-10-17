"""Select grounded, channel-specific post questions from privacy/AI headlines."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any

from editorial import classify


THEMES = (
    ("workplace_monitoring", re.compile(r"\b(?:employee|workplace|worker|staff|employee monitoring|surveillance|monitoring at work|работник\w*|сотрудник\w*|рабоч\w+ мест\w*|слежк\w*|мониторинг\w*)\b", re.I),
     "Где проходит граница между рабочим контролем и приватностью сотрудников?",
     "Какие границы контроля стоит определить работодателю до внедрения мониторинга сотрудников?",
     "Мониторинг сотрудников поднимает вопрос о границах контроля и защите приватности на рабочем месте."),
    ("ai_copyright", re.compile(r"\b(?:training data|train(?:ing)? (?:an? )?(?:ai|model)|copyright|authors?['’]? rights|расширенн\w* обучен\w*|обучен\w* (?:модел\w*|ии|нейросет\w*)|авторск\w+ прав\w*)\b", re.I),
     "Как распределить права и ответственность за данные, использованные для обучения ИИ?",
     "Что проверить в договоре с AI-поставщиком: права на обучающие данные, гарантии и распределение ответственности?",
     "Обучающие данные и авторские права ставят вопрос о распределении рисков между поставщиком и заказчиком."),
    ("cross_border", re.compile(r"\b(?:cross[- ]border|international transfer|data localization|data localisation|locali[sz]ation|transfer mechanism|трансгранич\w*|локализац\w*|передач\w* данн\w* за рубеж|трансфер данн\w*)\b", re.I),
     "Какую операционную модель передачи данных за рубеж выбрать для этого процесса?",
     "Какие данные можно оставить в общей архитектуре, а какие требуют локальной обработки или отдельного маршрута?",
     "Трансграничная передача и локализация требуют увязать правовую оценку с устройством конкретного процесса."),
    ("consent_cookies", re.compile(r"\b(?:consent|cookie banner|cookies|tracking pixels?|user choice|соглас\w*|cookie\w*|куки|трекер\w*|выбор пользователя)\b", re.I),
     "Как сделать выбор пользователя понятным и реально работающим в интерфейсе?",
     "Какие настройки и доказательства выбора стоит заложить в дизайн согласия и cookie-механизма?",
     "Согласие и трекеры связывают юридическую оценку с конкретным пользовательским интерфейсом."),
    ("biometrics", re.compile(r"\b(?:biometric\w*|facial recognition|face recognition|face scan|fingerprint\w*|голосов\w+ биометр\w*|биометр\w*|распознавани\w* лиц|сканирован\w* лица|отпечат\w* пальц\w*)\b", re.I),
     "Когда для этой задачи действительно нужны биометрические данные?",
     "Как проверить необходимость, масштаб и альтернативы до внедрения биометрической системы?",
     "Биометрия делает особенно важными вопросы цели, необходимости и масштаба обработки."),
    ("automated_decisions", re.compile(r"\b(?:automated decision\w*|algorithmic decision\w*|credit scoring|customer profiling|personal profiling|profiling (?:people|users|customers)|human review|автоматизированн\w* решени\w*|скоринг\w*|профилирован\w* (?:пользовател|клиент|людей)|оценк\w* заемщик\w*|провер\w* человеком)\b", re.I),
     "Где в автоматизированной оценке нужен осмысленный пересмотр человеком?",
     "Как организовать пересмотр результата, чтобы человек мог проверить исходные данные и логику решения?",
     "Автоматизированная оценка поднимает вопрос о роли человеческого пересмотра и качестве входных данных."),
    ("breach_enforcement", re.compile(r"\b(?:data breach\w*|security breach\w*|breach notification\w*|fined|fines|fine(?![- ]tuning)\b|penalt\w*|штраф\w*|утечк\w*|нарушен\w* безопасност\w*|уведомлен\w* об инцидент\w*)\b", re.I),
     "Какие превентивные процессы помогают снизить риск такого инцидента или санкции?",
     "Какие процессы контроля, эскалации и реагирования стоит проверить до следующего инцидента?",
     "Инцидент или санкция дают повод обсудить превентивные процессы и готовность к реагированию."),
    ("ai_contracts_liability", re.compile(r"\b(?:ai liability|algorithmic liability|ai contract\w*|ai vendor\w*|ai supplier\w*|model provider\w*|generative ai (?:agreement|contract)|ответственност\w* за ии|договор\w* об? ии|ии[- ]?контракт\w*|поставщик\w* ии|поставщик\w* модел\w*)\b", re.I),
     "Как распределить риски между поставщиком ИИ и компанией, которая использует модель?",
     "Какие условия о проверке, инцидентах и распределении ответственности нужны в договоре с AI-поставщиком?",
     "Договоры и ответственность за ИИ требуют заранее согласовать границы контроля и распределение рисков."),
    ("ai_regulation", re.compile(r"\b(?:ai act|ai regulation|ai bill|regulat\w*.{0,30}(?:ai|artificial intelligence)|законопроект\w*.{0,60}(?:ии|искусственн\w* интеллект\w*)|регулирован\w*.{0,50}(?:ии|искусственн\w* интеллект\w*))\b", re.I),
     "Что предлагается в регулировании ИИ и кого это может затронуть?",
     "Когда бизнесу менять процессы под регулирование ИИ: при обсуждении, принятии или начале применения правил?",
     "Повод сопоставить стадию обсуждения правил с решениями, которые бизнесу предстоит принять."),
)


def _published(item: dict[str, Any]) -> dt.datetime | None:
    value = item.get("published_utc")
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _source_kinds(item: dict[str, Any]) -> list[str]:
    kinds = set()
    if item.get("source_kind"):
        kinds.add(str(item["source_kind"]))
    provenance = item.get("provenance") or {}
    entries = provenance.values() if isinstance(provenance, dict) else provenance if isinstance(provenance, list) else []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("kind"):
            kinds.add(str(entry["kind"]))
    return sorted(kinds)


def _safe_url(value: Any) -> str:
    try:
        parts = urllib.parse.urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        return str(value)
    except ValueError:
        return ""


def _theme(text: str):
    for name, pattern, linkedin_q, telegram_q, angle in THEMES:
        if pattern.search(text):
            return name, linkedin_q, telegram_q, angle
    return None


def _as_of(value: dt.datetime | str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def select_ideas(items: list[dict[str, Any]], limit_per_channel: int = 3,
                 as_of: dt.datetime | str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return bounded, deduplicated questions grounded in explicit headline terms."""
    if limit_per_channel <= 0:
        return {"linkedin": [], "telegram": []}
    anchor = _as_of(as_of)
    cutoff = anchor - dt.timedelta(days=7)
    dated = [(item, _published(item)) for item in items]
    by_theme = {}
    for item, published in dated:
        if published is None or published < cutoff or published > anchor:
            continue
        title = str(item.get("title") or "")
        if re.fullmatch(r"\s*NOTICE OF DATA BREACH(?:\s*-\s*.*)?", title, re.I):
            continue  # Generic registry headings do not identify a useful post case.
        url = _safe_url(item.get("url") or item.get("canonical_url"))
        if not title.strip() or not url or classify({**item, "excerpt": ""})["section"] != "practice":
            continue
        matched = _theme(title)
        if not matched:
            continue
        theme, linkedin_q, telegram_q, angle = matched
        if theme == "ai_copyright" and not re.search(r"\b(?:training|train|обучен\w*)\b", title, re.I):
            linkedin_q = "Кто автор результата работы ИИ и как распределить права между участниками?"
            telegram_q = "Какие права на созданный с ИИ контент можно обещать заказчику?"
            angle = "Спор об авторских правах даёт повод разобрать роли автора, пользователя и поставщика ИИ."
        kinds = _source_kinds(item)
        primary = any(kind.lower() == "primary" for kind in kinds)
        candidate = {"question": None, "angle": angle, "title": title, "url": url,
                     "published_utc": item["published_utc"], "basis": "headline",
                     "source_kind": kinds[0] if len(kinds) == 1 else "mixed" if kinds else "unknown",
                     "source_kinds": kinds, "theme": theme, "_date": published,
                     "_primary": primary, "_questions": (linkedin_q, telegram_q)}
        prev = by_theme.get(theme)
        # Prefer direct sources within the fresh window, then newer headlines.
        if prev is None or (primary, published, title) > (prev["_primary"], prev["_date"], prev["title"]):
            by_theme[theme] = candidate
    result = {"linkedin": [], "telegram": []}
    for channel, index in (("linkedin", 1), ("telegram", 0)):
        ordered = sorted(by_theme.values(), key=lambda c: (c["_primary"], c["_date"], c["theme"]), reverse=True)
        for candidate in ordered[:limit_per_channel]:
            clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
            clean["question"] = candidate["_questions"][index]
            result[channel].append(clean)
    return result
