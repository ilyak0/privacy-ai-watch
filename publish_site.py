#!/usr/bin/env python3
"""Build a static Russian news archive for GitHub Pages."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import editorial
from briefing import select_briefing
from post_ideas import select_ideas

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "latest.json"
DEFAULT_OUTPUT = ROOT / "site"
DEFAULT_TIMELINES = ROOT / "state" / "timelines.json"
CATEGORIES = {"privacy_world": "Конфиденциальность · мир", "privacy_ru": "Конфиденциальность · РФ / русский",
              "ai_world": "ИИ · мир", "ai_ru": "ИИ · РФ / русский"}
KINDS = {"primary": "Первоисточник ведомства", "commentary": "Комментарий / СМИ",
         "discovery": "Поисковый индекс · первоисточник не проверен", "unknown": "Источник не классифицирован"}


def safe_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"https", "http"} or not parts.netloc or parts.username or parts.password:
        return ""
    return value.strip()


def public_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[адрес скрыт]", text)
    text = re.sub(r"(?:/Users/|/home/|~/)[^\s<>\"']+", "[путь скрыт]", text)
    return text


def public_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        date = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return date.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _sources(provenance: object) -> list[dict]:
    result = []
    if not isinstance(provenance, dict):
        return result
    for source_id, source in provenance.items():
        if not isinstance(source, dict):
            continue
        kind = source.get("kind") if source.get("kind") in KINDS else "unknown"
        observations = []
        raw = source.get("observations", [])
        for obs in raw if isinstance(raw, list) else []:
            if not isinstance(obs, dict):
                continue
            topics = obs.get("topics", [])
            observations.append({"url": safe_url(obs.get("url")),
                "published_utc": obs.get("published_utc") if isinstance(obs.get("published_utc"), str) else None,
                "topics": sorted(t for t in topics if t in {"privacy", "ai"}) if isinstance(topics, list) else []})
        result.append({"id": str(source_id), "label": public_text(source.get("label") or source_id),
            "region": source.get("region") if source.get("region") in {"world", "ru"} else "unknown",
            "kind": kind, "observations": observations})
    return result


def _item(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = safe_url(raw.get("url"))
    key, title = str(raw.get("canonical_url") or url), raw.get("title")
    if not key or not isinstance(title, str) or not title.strip():
        return None
    sources = _sources(raw.get("provenance"))
    memberships = sorted({f"{topic}_{source['region']}" for source in sources
        if source["region"] in {"world", "ru"} for obs in source["observations"] for topic in obs["topics"]})
    published = public_date(raw.get("published_utc"))
    headline = title[:180] + ('…' if len(title) > 180 else '')
    kinds = sorted({s["kind"] for s in sources})
    result = {"_key": key, "url": url, "title": public_text(headline), "published_utc": published,
              "memberships": memberships, "sources": sources, "undated": published is None,
              "source_kind": kinds[0] if len(kinds) == 1 else "mixed" if kinds else "unknown"}
    result["editorial"] = editorial.classify(result)
    return result


def public_report(report: dict, timeline_state: dict | None = None) -> dict:
    """Deduplicate bucket copies; preserve only public title, date and source metadata."""
    by_url = {}
    for entries in report.get("buckets", {}).values():
        for raw in entries if isinstance(entries, list) else []:
            item = _item(raw)
            if not item:
                continue
            prior = by_url.get(item["_key"])
            if prior is None:
                by_url[item["_key"]] = item
                continue
            prior["memberships"] = sorted(set(prior["memberships"]) | set(item["memberships"]))
            sources = {s["id"]: s for s in prior["sources"]}
            for source in item["sources"]:
                if source["id"] not in sources:
                    prior["sources"].append(source)
                    continue
                existing = sources[source["id"]]
                seen = {(o["url"], o["published_utc"], tuple(o["topics"])) for o in existing["observations"]}
                for obs in source["observations"]:
                    marker = (obs["url"], obs["published_utc"], tuple(obs["topics"]))
                    if marker not in seen:
                        existing["observations"].append(obs)
                        seen.add(marker)
    undated = report.get("undated", [])
    for raw in undated if isinstance(undated, list) else []:
        item = _item(raw)
        if item and item["_key"] not in by_url:
            by_url[item["_key"]] = item
    items = sorted(by_url.values(), key=editorial.sort_key, reverse=True)
    counts = {key: sum(key in item["memberships"] for item in items) for key in CATEGORIES}
    health = []
    for entry in report.get("source_health", []):
        if isinstance(entry, dict):
            status = entry.get("status") if entry.get("status") in {"ok", "stale", "valid_empty", "error"} else "unknown"
            health.append({"label": public_text(entry.get("label")),
                "region": entry.get("region") if entry.get("region") in {"world", "ru"} else "unknown",
                "kind": entry.get("kind") if entry.get("kind") in KINDS else "unknown", "status": status,
                "window_coverage": entry.get("window_coverage") if entry.get("window_coverage") in {"capped", "boundary_reached", "repeated_page", "unknown_dates"} else None,
                "pages_fetched": _count(entry.get("pages_fetched")),
                "checked_at": public_date(entry.get("checked_at")),
                "parsed": _count(entry.get("parsed")), "recent": _count(entry.get("recent")),
                "undated": _count(entry.get("undated")), "latest_publication": public_date(entry.get("latest_publication"))})
    briefing = select_briefing(items, as_of=public_date(report.get("generated_at")))
    for item in items:
        item["id"] = hashlib.sha256(item.pop("_key").encode("utf-8")).hexdigest()
        for source in item["sources"]:
            source.pop("id", None)
    from timelines import public_timelines
    timeline_as_of = public_date(report.get("generated_at"))
    timeline_as_of = dt.datetime.fromisoformat(timeline_as_of.replace("Z", "+00:00")) if timeline_as_of else None
    stories = public_timelines(timeline_state, as_of=timeline_as_of) if timeline_state else []
    return {"generated_at": public_date(report.get("generated_at")), "publication_window_days": _count(report.get("publication_window_days")),
            "unique_items": len(items), "membership_counts": counts, "items": items,
            "undated_count": sum(i["undated"] for i in items), "source_health": health,
            "briefing": briefing,
            "timelines": stories,
            "post_ideas": select_ideas(items, as_of=public_date(report.get("generated_at")))}


PAGE = '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>Новости Privacy &amp; AI</title>
<style>
:root{--ink:#17251f;--muted:#64726b;--line:#dce5df;--paper:#f4f7f4;--green:#176b4a;--mint:#e1f2e9;--amber:#946314;--red:#a33737;--shadow:0 10px 30px #153c2610}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--green);text-underline-offset:3px}header,.wrap{width:min(1120px,calc(100% - 34px));margin:auto}header{padding:42px 0 22px}.eyebrow{color:var(--green);font-size:.75rem;font-weight:750;letter-spacing:.13em;text-transform:uppercase}h1{font-size:clamp(2.1rem,5vw,3.4rem);line-height:1.06;letter-spacing:-.045em;margin:.45rem 0}.lede,.meta{color:var(--muted);max-width:760px}.meta{font-size:.88rem}.notice{margin:14px 0;padding:10px 13px;background:#fff8e6;border-left:3px solid #d2a64f;border-radius:6px;color:#63502c;font-size:.87rem}.stats{display:flex;gap:9px;flex-wrap:wrap}.stat{background:white;border:1px solid var(--line);border-radius:12px;padding:9px 12px;box-shadow:var(--shadow);font-size:.83rem}.stat b{color:var(--green);font-size:1rem}.controls{position:sticky;top:0;z-index:2;background:#f4f7f4ed;backdrop-filter:blur(10px);padding:11px 0;border-block:1px solid var(--line)}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.filters{display:flex;gap:6px;flex-wrap:wrap}.filter{border:1px solid var(--line);border-radius:999px;background:white;color:var(--ink);padding:7px 11px;cursor:pointer;font:inherit;font-size:.82rem}.filter[aria-pressed=true]{background:var(--green);color:white;border-color:var(--green)}.search{flex:1 1 220px;min-width:190px;border:1px solid var(--line);border-radius:10px;padding:9px 11px;font:inherit}.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:24px 0 11px}.head h2{font-size:1.08rem;margin:0}.count{color:var(--muted);font-size:.83rem}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:12px;padding-bottom:26px}.card{background:white;border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:11px;min-width:0}.date{color:var(--muted);font-size:.82rem}.card h3{font-size:1rem;line-height:1.38;margin:0;overflow-wrap:anywhere}.card h3 a{color:var(--ink)}.tags,.sources{display:flex;gap:6px;flex-wrap:wrap}.tag,.kind{font-size:.72rem;padding:4px 8px;border-radius:999px;background:var(--mint);color:#21563d}.kind{background:#edf0ef;color:#45544d}.source{font-size:.8rem;overflow-wrap:anywhere}.empty{padding:32px 15px;text-align:center;color:var(--muted);background:white;border:1px dashed var(--line);border-radius:12px}.health{margin:8px 0 42px;background:white;border:1px solid var(--line);border-radius:13px;padding:15px}.health summary{cursor:pointer;font-weight:700}.healthlist{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));gap:8px;padding-top:12px}.healthitem{font-size:.8rem;color:var(--muted);border-top:1px solid var(--line);padding-top:7px}.status{font-weight:700}.status.error{color:var(--red)}.status.stale,.status.valid_empty{color:var(--amber)}footer{color:var(--muted);font-size:.78rem;padding-bottom:30px}@media(max-width:600px){header{padding-top:30px}.controls{position:static}.head{align-items:flex-start;flex-direction:column}}
.story-grid{display:grid;align-items:start;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:12px;margin-bottom:26px}.story{background:white;border:1px solid var(--line);border-radius:14px;padding:16px;scroll-margin-top:120px}.story summary{cursor:pointer;font-weight:700}.story summary .count{display:block;font-weight:400;margin-top:5px}.story p{font-size:.87rem;color:var(--muted)}.story ol{list-style:none;margin:12px 0 0;padding:0}.story li{border-left:2px solid var(--line);padding:0 0 20px 16px}.story li:last-child{padding-bottom:3px}.story li>a{display:block;margin:6px 0;font-size:.92rem}.story:target{border-color:var(--green)}
</style></head><body>
<header><div class="eyebrow">Мониторинг новостей</div><h1>Privacy <span aria-hidden="true">&amp;</span> AI</h1>
<p class="lede">Защита данных и регулирование искусственного интеллекта: международные и русскоязычные источники.</p>
<p class="meta">Обновлено: <time id="updated">—</time> · окно публикации: <span id="window">—</span> дней · <span id="total">—</span> уникальных материалов</p>
<div class="notice">Регион — поток источника, а не юрисдикция события. Результат поискового индекса ещё не проверен как первоисточник.</div><div class="stats" id="stats"></div></header>
<nav class="controls"><div class="row wrap"><div class="filters" role="group" aria-label="Фильтр новостей">
<button class="filter" data-filter="all" aria-pressed="true">Все</button><button class="filter" data-filter="privacy_world" aria-pressed="false">Privacy · мир</button>
<button class="filter" data-filter="privacy_ru" aria-pressed="false">Privacy · РФ / русский</button><button class="filter" data-filter="ai_world" aria-pressed="false">ИИ · мир</button>
<button class="filter" data-filter="ai_ru" aria-pressed="false">ИИ · РФ / русский</button><button class="filter" data-filter="undated" aria-pressed="false">Без даты</button></div>
<label><span hidden>Редакционный раздел</span><select class="search" id="section"><option value="all">Все разделы</option><option value="practice">Юридическая практика</option><option value="ai_context">Контекст ИИ и индустрии</option><option value="events">События и обучение</option></select></label><label><span hidden>Порядок публикаций</span><select class="search" id="order"><option value="priority">Сначала практика</option><option value="newest">Сначала новые</option></select></label><label><span hidden>Поиск по заголовку и источнику</span><input class="search" id="search" type="search" placeholder="Поиск по заголовку и источнику"></label></div></nav>
<main class="wrap"><section id="briefing"><div class="head"><h2>Сначала открыть</h2><span class="count">До 5 поводов за неделю · отбор по заголовкам</span></div><div class="grid" id="briefing-cards"></div></section><section id="timelines"><div class="head"><h2>История сюжетов</h2><span class="count">Даты публикаций · история сохраняется за пределами 30 дней</span></div><div class="story-grid" id="stories"></div></section><section id="post-ideas"><div class="head"><h2>Вопросы для постов</h2><span class="count">Поводы по заголовкам · перед постом проверить полный источник</span></div><div class="grid" id="ideas"></div></section><div class="head"><h2>Публикации</h2><span class="count" id="count"></span></div><section class="grid" id="news" aria-live="polite"></section>
<details class="health"><summary id="healthsummary">Состояние источников</summary><div class="healthlist" id="health"></div></details></main>
<footer class="wrap">Новостные сигналы не заменяют проверку первоисточника и юридический анализ.</footer>
<script>
const LABELS={privacy_world:"Конфиденциальность · мир",privacy_ru:"Конфиденциальность · РФ / русский",ai_world:"ИИ · мир",ai_ru:"ИИ · РФ / русский"};
const KIND={primary:"Первоисточник ведомства",commentary:"Комментарий / СМИ",discovery:"Поисковый индекс · не проверен",unknown:"Источник не классифицирован"};let DATA=null,active="all";
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n};
const dateText=v=>{if(!v)return"Дата неизвестна";const d=new Date(v);return Number.isNaN(d.getTime())?"Дата неизвестна":new Intl.DateTimeFormat("ru-RU",{dateStyle:"medium",timeZone:"Europe/Moscow"}).format(d)};
function stats(){const box=document.getElementById("stats");box.replaceChildren();for(const [key,label] of Object.entries(LABELS)){const s=el("div","stat");s.append(el("b","",DATA.membership_counts[key]||0),document.createTextNode(" "+label));box.append(s)}if(DATA.undated_count){const s=el("div","stat");s.append(el("b","",DATA.undated_count),document.createTextNode(" без даты"));box.append(s)}}
function health(){const box=document.getElementById("health");box.replaceChildren();const n={};for(const s of DATA.source_health)n[s.status]=(n[s.status]||0)+1;document.getElementById("healthsummary").textContent=`Источников: ${DATA.source_health.length} · ошибок ${n.error||0} · устаревших ${n.stale||0} · пустых ${n.valid_empty||0}`;for(const s of DATA.source_health){const row=el("div","healthitem"),name=el("strong","",s.label||s.source_id),status=el("span",`status ${s.status||"unknown"}`,` · ${s.status||"неизвестно"}`);row.append(name,status,document.createTextNode(` · ${s.region||"?"} · ${KIND[s.kind]||KIND.unknown} · разобрано ${s.parsed||0}, свежих ${s.recent||0}, без даты ${s.undated||0} · последняя публикация ${dateText(s.latest_publication)}${s.window_coverage ? " · архив: " + ({boundary_reached:"граница окна достигнута",capped:"достигнут лимит",repeated_page:"повтор страницы",unknown_dates:"даты не распознаны"}[s.window_coverage]||s.window_coverage) + " · страниц " + s.pages_fetched : ""}`));box.append(row)}}
function ideas(){const box=document.getElementById("ideas");box.replaceChildren();for(const [key,label] of [["linkedin","LinkedIn"],["telegram","Telegram"],["x","X / Twitter"]]){for(const i of (DATA.post_ideas||{})[key]||[]){const card=el("article","card");card.append(el("span","tag",label));const h=el("h3","",i.question);card.append(h,el("p","meta",i.angle));const a=el("a","",i.title);a.href=i.url;a.target="_blank";a.rel="noopener noreferrer";card.append(a,el("span","date",dateText(i.published_utc)+" · "+(KIND[i.source_kind]||"Несколько типов источников")));box.append(card)}}if(!box.children.length)box.append(el("div","empty","Свежих поводов для выбранных тем пока нет."))}
function briefing(){const box=document.getElementById("briefing-cards");box.replaceChildren();for(const i of DATA.briefing||[]){const card=el("article","card"),h=el("h3"),a=el("a","",i.title);a.href=i.url;a.target="_blank";a.rel="noopener noreferrer";h.append(a);card.append(el("span","tag",i.signal_label),h,el("p","",i.why_open),el("span","date",dateText(i.published_utc)+" · "+(KIND[i.source_kind]||"Несколько типов источников")),el("p","meta",i.verification_needed));box.append(card)}if(!box.children.length)box.append(el("div","empty","Свежих публикаций для короткого отбора пока нет."))}
function openStory(){const id=location.hash.slice(1);if(!/^story-[a-z0-9_-]+$/.test(id))return;const story=document.getElementById(id);if(story&&story.classList.contains("story")){story.open=true;story.scrollIntoView({block:"start"})}}
function stories(){const box=document.getElementById("stories");box.replaceChildren();for(const s of DATA.timelines||[]){const card=el("details","story");card.id="story-"+s.id;const summary=el("summary","",s.label);summary.append(el("span","count","Публикаций: "+s.publication_count+" · последняя: "+dateText(s.latest_publication)));card.append(summary,el("p","",s.scope));if(s.history_status==="watching")card.append(el("p","",s.publication_count?"Пока одна публикация. История будет дополняться.":"Наблюдаем: подходящих публикаций в архиве пока нет."));const list=el("ol");for(const e of s.events){const row=el("li"),time=el("time","date",dateText(e.published_utc));time.dateTime=e.published_utc;const a=el("a","",e.title);a.href=e.url;a.target="_blank";a.rel="noopener noreferrer";row.append(time,a);const sources=el("div","sources");for(const source of e.sources){const span=el("span","source"),link=el("a","",source.label);link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";span.append(link,document.createTextNode(" · "+(KIND[source.kind]||KIND.unknown)));sources.append(span)}if(!e.sources.length)sources.append(el("span","source",KIND.unknown));row.append(sources);list.append(row)}card.append(list);box.append(card)}if(!box.children.length)box.append(el("div","empty","История сюжетов пока не собрана."));openStory()}
function render(){if(!DATA)return;const q=document.getElementById("search").value.trim().toLocaleLowerCase("ru"),box=document.getElementById("news");box.replaceChildren();const section=document.getElementById("section").value;const found=DATA.items.filter(i=>{const f=active==="all"||active==="undated"||i.memberships.includes(active);return (section==="all"||i.editorial.section===section)&&f&&(active!=="undated"||i.undated)&&(!q||(i.title+" "+i.sources.map(s=>s.label).join(" ")).toLocaleLowerCase("ru").includes(q))});if(document.getElementById("order").value==="newest")found.sort((a,b)=>(b.published_utc||"").localeCompare(a.published_utc||""));document.getElementById("count").textContent=`Показано ${found.length} из ${DATA.unique_items}`;for(const i of found){const card=el("article","card"),time=el("time","date",dateText(i.published_utc));if(i.published_utc)time.dateTime=i.published_utc;card.append(time);const h=el("h3");if(i.url){const a=el("a","",i.title);a.href=i.url;a.target="_blank";a.rel="noopener noreferrer";h.append(a)}else h.textContent=i.title;card.append(h);const tags=el("div","tags");tags.append(el("span","kind",i.editorial.label));for(const key of i.memberships)if(LABELS[key])tags.append(el("span","tag",LABELS[key]));if(i.undated)tags.append(el("span","tag","Без даты публикации"));card.append(tags);const sources=el("div","sources");for(const s of i.sources){const span=el("span","source"),obs=s.observations.find(o=>o.url);if(obs){const a=el("a","",s.label);a.href=obs.url;a.target="_blank";a.rel="noopener noreferrer";span.append(a)}else span.textContent=s.label;span.append(document.createTextNode(" "),el("span","kind",KIND[s.kind]||KIND.unknown));sources.append(span)}card.append(sources);box.append(card)}if(!found.length)box.append(el("div","empty","По этому фильтру ничего не найдено."))}
async function start(){try{const r=await fetch("./news.json",{cache:"no-store"});if(!r.ok)throw new Error();DATA=await r.json();document.getElementById("updated").textContent=new Intl.DateTimeFormat("ru-RU",{dateStyle:"medium",timeStyle:"short",timeZone:"Europe/Moscow"}).format(new Date(DATA.generated_at))+" МСК";document.getElementById("updated").dateTime=DATA.generated_at||"";document.getElementById("window").textContent=DATA.publication_window_days||"—";document.getElementById("total").textContent=DATA.unique_items;stats();health();briefing();stories();ideas();render()}catch(_){document.getElementById("news").replaceChildren(el("div","empty","Не удалось загрузить архив. Попробуйте обновить страницу позже."))}}
window.addEventListener("hashchange",openStory);document.querySelectorAll(".filter").forEach(b=>b.addEventListener("click",()=>{active=b.dataset.filter;document.querySelectorAll(".filter").forEach(x=>x.setAttribute("aria-pressed",String(x===b)));render()}));document.getElementById("search").addEventListener("input",render);for(const id of ["section","order"])document.getElementById(id).addEventListener("change",render);start();
</script></body></html>'''


def build_site(report: dict, outdir: str | Path, timeline_state: dict | None = None) -> tuple[Path, Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    data_path, page_path = outdir / "news.json", outdir / "index.html"
    payload = json.dumps(public_report(report, timeline_state=timeline_state), ensure_ascii=False, indent=2)
    # Keep JSON safe if a static host or downstream template ever embeds it in HTML.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    data_path.write_text(payload + "\n", encoding="utf-8")
    page_path.write_text(PAGE, encoding="utf-8")
    return page_path, data_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timelines", type=Path, default=DEFAULT_TIMELINES)
    args = parser.parse_args()
    history = json.loads(args.timelines.read_text(encoding="utf-8")) if args.timelines.exists() else None
    page, data = build_site(json.loads(args.input.read_text(encoding="utf-8")), args.output_dir, timeline_state=history)
    print(f"Built {page} and {data}")


if __name__ == "__main__":
    main()
