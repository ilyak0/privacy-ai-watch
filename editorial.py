"""Small deterministic editorial classifier for the privacy and AI watch."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any


PRACTICE_PATTERNS = {
    "regulation": re.compile(
        r"\b(?:law|bill|legislation|regulat(?:ion|ory)|directive|act|statute|закон(?:опроект)?|регулирован\w*|законодательств\w*)\b",
        re.I),
    "enforcement_decision": re.compile(
        r"\b(?:fined|fine(?![- ]tuning)[sd]?|penalt(?:y|ies)|sanction\w*|enforcement|court|lawsuit|sued|complaint filed|ruling|judgment|decision|штраф\w*|оштраф\w*|суд\w*|иск(?:а|у|ом|е|и|ов|ами|ах|ам)?|жалоб\w*|решени\w*|постановлен\w*|предписан\w*)\b",
        re.I),
    "privacy_guidance": re.compile(
        r"\b(?:guidance|guideline\w*|recommendation\w*|privacy|data protection|personal data|cross[- ]border|consent|cookie\w*|biometric\w*|facial recognition|employee monitoring|workplace monitoring|automated decision\w*|profiling|scoring|data breach\w*|security breach\w*|конфиденциальност\w*|персональн\w+ данн\w*|трансгранич\w*|соглас\w*|разъяснен\w*|рекомендац\w*|биометр\w*|распознавани\w* лиц|мониторинг\w* сотрудник\w*|слежк\w* за сотрудник\w*|автоматизированн\w* решени\w*|скоринг\w*|профилирован\w*|утечк\w*)\b",
        re.I),
    "ai_legal": re.compile(
        r"\b(?:ai act|artificial intelligence act|ai liability|algorithmic liability|copyright|liabilit\w*|авторск\w+ прав\w*|ответственност\w*|исключительн\w+ прав\w*)\b",
        re.I),
    "legal_operations": re.compile(
        r"\b(?:contract\w*|privacy operations|data processing agreement|dpa|privacy program|договор\w*|контракт\w*|операцион\w* по защите данн\w*)\b",
        re.I),
}

EVENT_PATTERN = re.compile(
    r"\b(?:register|registration|save (?:your )?seat|book (?:your )?place|join us|webinar|conference|congress|summit|workshop|masterclass|master-class|course|training (?:course|session|workshop|program|programme)|event|приходите|приглаша\w*|регистрац\w*|вебинар\w*|конференц\w*|конгресс\w*|форум\w*|семинар\w*|мастер[- ]класс\w*|курс\w*|обучени\w*|запись вебинар\w*)\b",
    re.I)
SUBSTANTIVE_ACTION = re.compile(
    r"\b(?:fined|penali[sz]ed|imposed (?:a )?fine|ordered to pay (?:a )?fine|adopted|passed|enacted|issued (?:new )?(?:guidance|guidelines|rules)|published (?:new )?(?:guidance|guidelines)|filed (?:a )?(?:lawsuit|complaint)|court (?:rules|holds|orders)|внесен\w* законопроект|принял\w* закон|закон\w*.{0,24}принят\w*|оштраф\w*|наложил\w* штраф|наложен\w* штраф|опубликовал\w* (?:рекомендац|разъяснен|руководств)|подан\w* иск|суд вынес\w*|вынес\w* решени\w*)\b",
    re.I)
AI_CONTEXT = re.compile(
    r"\b(?:ai|artificial intelligence|llm\w*|large language model\w*|generative|model\w*|agent\w*|coding|developer tool\w*|funding|raises? \$|startup\w*|нейросет\w*|искусственн\w+ интеллект\w*|модел\w*|кодинг|стартап\w*|инвестиц\w*)\b",
    re.I)

LABELS = {
    "practice": "Юридическая практика",
    "ai_context": "Контекст ИИ и индустрии",
    "events": "События и обучение",
}
PRIORITY = {"practice": 3, "ai_context": 2, "events": 1}


def _text(item: dict[str, Any]) -> str:
    # Keep only the supplied headline and short excerpt; do not infer from linked pages.
    text = " ".join(str(item.get(k) or "") for k in ("title", "excerpt"))
    # Runtime profiling is a software performance term, not personal profiling.
    if re.search(r"\b(?:python|flame graphs?|heatmaps?|profiler|performance profiling)\b", text, re.I):
        text = re.sub(r"\bprofiling\b", "", text, flags=re.I)
    return text


def _source_kinds(item: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    direct = item.get("source_kind")
    if direct:
        found.add(str(direct))
    provenance = item.get("provenance") or {}
    values = provenance.values() if isinstance(provenance, dict) else provenance if isinstance(provenance, list) else []
    values = list(values) + (item.get("sources") or [])
    for entry in values:
        if isinstance(entry, dict) and entry.get("kind"):
            found.add(str(entry["kind"]))
    return sorted(found)


def classify(item: dict[str, Any]) -> dict[str, Any]:
    """Classify the supplied title/excerpt without asserting legal status or validity."""
    text = _text(item)
    matches = [name for name, pattern in PRACTICE_PATTERNS.items() if pattern.search(text)]
    event = bool(EVENT_PATTERN.search(text))
    substantive = bool(SUBSTANTIVE_ACTION.search(text))
    ai = bool(AI_CONTEXT.search(text))
    kinds = _source_kinds(item)
    privacy = "privacy" in item.get("topics", []) or any(
        str(m).startswith("privacy_") for m in item.get("memberships", []))
    if privacy and "primary" in kinds:
        matches.append("privacy_source")

    # Event copy is kept out of practice unless the text reports an actual action.
    # A primary source alone cannot promote unrelated scientific or product news.
    if event and not substantive:
        section, score = "events", 20
        evidence = ["lexical:event"]
    elif matches:
        weights = {"regulation": 82, "enforcement_decision": 85, "privacy_guidance": 60, "privacy_source": 60,
                   "ai_legal": 78, "legal_operations": 74}
        section, score = "practice", max(weights[name] for name in matches)
        if re.search(r"guidance|guidelines|разъяснен|рекомендац|руководств", text, re.I):
            score = max(score, 82)
        if substantive:
            score += 10
        if "primary" in kinds:
            score += 15
        evidence = [f"lexical:{signal}" for signal in matches]
        if substantive:
            evidence.append("lexical:substantive_action")
    else:
        section, score = "ai_context", 40 if ai else 30
        evidence = ["lexical:ai_industry"] if ai else ["lexical:general_context"]
    evidence.extend(f"source_kind:{kind}" for kind in kinds)
    return {"section": section, "label": LABELS[section], "score": score,
            "evidence": evidence}


def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    """Sort higher-priority editorial sections first, then newest publication date."""
    result = classify(item)
    published = str(item.get("published_utc") or "")
    try:
        parsed = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            published = parsed.astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        published = ""
    # Reverse sorting gives section priority descending and publication descending.
    return PRIORITY[result["section"]], result["score"], published
