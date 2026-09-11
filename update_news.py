#!/usr/bin/env python3
"""
Logistics news collector wrapper.

Keeps the proven 2026-08-28 collector logic unchanged for the main 12-news feed,
and adds independent cost signals for:
- global / RU / BY fuel,
- marine bunker fuel,
- jet fuel,
- Russia toll roads / Platon,
- Belarus BelToll.

The cost signals are stored in news.json under "costSignals" and DO NOT consume
slots from the main "news" list.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


BASELINE_URL = (
    "https://raw.githubusercontent.com/"
    "molesta425-design/logistics-news-gdelt/"
    "46ace41/update_news.py"
)

COLLECTOR_VERSION = "2026-09-11-fuel-tolls-v1"

OUTPUT_PATH = Path(__file__).with_name("news.json")

RSS_RETRIES = 3
RSS_RETRY_DELAY = 10
RSS_INTER_FEED_DELAY = 2

MAX_FUEL_SIGNALS = 6
MAX_TOLL_SIGNALS = 6
MAX_COST_SIGNALS = MAX_FUEL_SIGNALS + MAX_TOLL_SIGNALS

USER_AGENT = "logistics-news-rss/2.1 (+public GitHub Actions feed)"


EXTRA_FEEDS = [
    {
        "label": "fuel-global",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "cost-signal",
        "costCategory": "fuel",
        "query": (
            '("Brent" OR "Urals" OR diesel OR gasoil OR "jet fuel" OR '
            '"marine fuel" OR "bunker fuel" OR VLSFO OR MGO) '
            '(price OR prices OR rise OR rises OR increase OR increases OR '
            'fall OR falls OR decline OR declines OR index) when:1d'
        ),
    },
    {
        "label": "fuel-reuters-bloomberg",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "cost-signal",
        "costCategory": "fuel",
        "query": (
            '(site:reuters.com OR site:bloomberg.com) '
            '("Brent crude" OR "Urals" OR diesel OR gasoil OR "jet fuel" '
            'OR "bunker fuel" OR VLSFO OR MGO) '
            '(price OR prices OR rise OR fall OR increase OR decline) when:1d'
        ),
    },
    {
        "label": "fuel-russia",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "cost-signal",
        "costCategory": "fuel",
        "query": (
            '(дизель OR дизтопливо OR "дизельное топливо" OR '
            '"авиационный керосин" OR "реактивное топливо" OR '
            '"судовое топливо" OR "бункерное топливо" OR Urals OR Brent) '
            '(цена OR цены OR подорожание OR рост OR снижение OR подешевел OR индекс) '
            '(Россия OR РФ) when:1d'
        ),
    },
    {
        "label": "fuel-belarus",
        "language": "Russian",
        "hl": "ru",
        "gl": "BY",
        "ceid": "BY:ru",
        "sourceType": "cost-signal",
        "costCategory": "fuel",
        "query": (
            '(дизель OR дизтопливо OR "дизельное топливо" OR топливо) '
            '(цена OR цены OR подорожание OR рост OR снижение OR тариф) '
            '(Беларусь OR РБ) when:1d'
        ),
    },
    {
        "label": "fuel-china",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "cost-signal",
        "costCategory": "fuel",
        "query": (
            '(China OR Chinese) '
            '(diesel OR fuel OR "jet fuel" OR bunker OR VLSFO OR MGO) '
            '(price OR prices OR increase OR rise OR fall OR cut OR index) when:1d'
        ),
    },
    {
        "label": "toll-roads-russia",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "cost-signal",
        "costCategory": "tolls",
        "query": (
            '("Платон" OR "платные дороги" OR "платный участок" OR Автодор OR '
            '"М-1" OR "М-3" OR "М-4" OR "М-11" OR "М-12" OR ЦКАД) '
            '(тариф OR стоимость OR индексация OR повышение OR снижение OR '
            'плата OR проезд OR грузовик OR большегруз) when:1d'
        ),
    },
    {
        "label": "toll-roads-russia-official",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "cost-signal",
        "costCategory": "tolls",
        "query": (
            '(site:avtodor-tr.ru OR site:rosavtodor.gov.ru OR site:mintrans.gov.ru) '
            '("платные дороги" OR "Платон" OR тариф OR проезд OR индексация OR '
            '"М-1" OR "М-3" OR "М-4" OR "М-11" OR "М-12" OR ЦКАД) when:1d'
        ),
    },
    {
        "label": "toll-roads-belarus",
        "language": "Russian",
        "hl": "ru",
        "gl": "BY",
        "ceid": "BY:ru",
        "sourceType": "cost-signal",
        "costCategory": "tolls",
        "query": (
            '(BelToll OR "платные дороги" OR "плата за проезд" OR '
            '"дорожный сбор" OR "электронная система сбора платы") '
            '(Беларусь OR РБ) '
            '(тариф OR стоимость OR повышение OR снижение OR изменение OR ставка) when:1d'
        ),
    },
    {
        "label": "toll-roads-belarus-official",
        "language": "Russian",
        "hl": "ru",
        "gl": "BY",
        "ceid": "BY:ru",
        "sourceType": "cost-signal",
        "costCategory": "tolls",
        "query": (
            '(site:beltoll.by OR site:mintrans.gov.by) '
            '(BelToll OR "плата за проезд" OR тариф OR ставка OR дорога) when:1d'
        ),
    },
]


FUEL_TERMS = (
    "brent",
    "urals",
    "diesel",
    "gasoil",
    "jet fuel",
    "aviation fuel",
    "marine fuel",
    "bunker fuel",
    "vlsfo",
    "mgo",
    "дизель",
    "дизтопливо",
    "дизельное топливо",
    "авиационный керосин",
    "реактивное топливо",
    "судовое топливо",
    "бункерное топливо",
)

TOLL_TERMS = (
    "platon",
    "платон",
    "toll road",
    "toll roads",
    "road toll",
    "платная дорога",
    "платные дороги",
    "платный участок",
    "плата за проезд",
    "автодор",
    "beltoll",
    "цкад",
    "м-1",
    "м-3",
    "м-4",
    "м-11",
    "м-12",
)

UP_TERMS = (
    "rise",
    "rises",
    "rose",
    "increase",
    "increases",
    "increased",
    "higher",
    "surge",
    "surges",
    "up ",
    "подорож",
    "повыш",
    "вырос",
    "выросли",
    "рост",
    "увелич",
    "индексац",
)

DOWN_TERMS = (
    "fall",
    "falls",
    "fell",
    "decline",
    "declines",
    "declined",
    "decrease",
    "decreases",
    "decreased",
    "lower",
    "cut",
    "cuts",
    "down ",
    "сниж",
    "подешев",
    "сократ",
    "уменьш",
)

ROAD_FUEL_TERMS = (
    "diesel",
    "gasoil",
    "дизель",
    "дизтопливо",
    "дизельное топливо",
)

AIR_FUEL_TERMS = (
    "jet fuel",
    "aviation fuel",
    "авиационный керосин",
    "реактивное топливо",
)

SEA_FUEL_TERMS = (
    "marine fuel",
    "bunker",
    "vlsfo",
    "mgo",
    "судовое топливо",
    "бункерное топливо",
)


def clean(value) -> str:
    return " ".join(str(value or "").split())


def contains_any(text: str, terms) -> bool:
    lowered = clean(text).lower()
    return any(term.lower() in lowered for term in terms)


def load_baseline_namespace() -> dict:
    last_error = None

    for attempt in range(1, RSS_RETRIES + 1):
        try:
            response = requests.get(
                BASELINE_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=45,
            )
            response.raise_for_status()
            source = response.text

            # Safety check: make sure we received the expected working collector.
            required_markers = (
                'COLLECTOR_VERSION = "2026-08-28-url-resolver-v2-safeguard"',
                "RSS_FEEDS = [",
                "def build_feed(",
                "def fetch_google_news_rss(",
                "def article_to_news(",
            )
            if not all(marker in source for marker in required_markers):
                raise RuntimeError(
                    "Pinned baseline update_news.py has unexpected contents"
                )

            namespace = {
                "__name__": "logistics_news_baseline",
                "__file__": str(Path(__file__)),
            }
            exec(compile(source, BASELINE_URL, "exec"), namespace)
            return namespace

        except Exception as error:
            last_error = error
            if attempt < RSS_RETRIES:
                time.sleep(RSS_RETRY_DELAY * attempt)

    raise RuntimeError(f"Cannot load pinned baseline collector: {last_error}")


def fetch_extra_articles(base: dict, session: requests.Session) -> tuple[list[dict], list[str]]:
    articles: list[dict] = []
    failures: list[str] = []

    for index, feed in enumerate(EXTRA_FEEDS):
        try:
            batch = base["fetch_google_news_rss"](session, feed)

            for article in batch:
                article["sourceType"] = "cost-signal"
                article["costCategory"] = feed["costCategory"]
                article["feedLabel"] = feed["label"]

            articles.extend(batch)
            print(f"RSS {feed['label']}: {len(batch)} articles")

        except Exception as error:
            failures.append(f"RSS {feed['label']}: {error}")

        if index < len(EXTRA_FEEDS) - 1:
            time.sleep(RSS_INTER_FEED_DELAY)

    return articles, failures


def infer_transport(text: str, category: str) -> list[str]:
    if category == "tolls":
        return ["Авто"]

    transports: list[str] = []

    if contains_any(text, ROAD_FUEL_TERMS):
        transports.append("Авто")

    if contains_any(text, AIR_FUEL_TERMS):
        transports.append("Авиа")

    if contains_any(text, SEA_FUEL_TERMS):
        transports.append("Море")

    # Crude oil prices affect several transport fuel markets indirectly.
    if not transports and contains_any(text, ("brent", "urals")):
        transports = ["Авто", "Море", "Авиа"]

    if not transports:
        transports = ["Авто"]

    return transports


def direction_for_cost_signal(text: str, base: dict) -> list[str]:
    directions = base["directions_for"](text)

    if directions and directions != ["Другие"]:
        return directions

    lowered = text.lower()

    if "belarus" in lowered or "беларус" in lowered or "beltoll" in lowered:
        return ["РБ"]

    if (
        "russia" in lowered
        or "russian" in lowered
        or "росси" in lowered
        or "платон" in lowered
        or "автодор" in lowered
    ):
        return ["РФ"]

    if "china" in lowered or "chinese" in lowered or "китай" in lowered:
        return ["Китай"]

    return ["Мировые"]


def movement_for(text: str) -> tuple[str, str]:
    has_up = contains_any(text, UP_TERMS)
    has_down = contains_any(text, DOWN_TERMS)

    if has_up and not has_down:
        return "↑", "Рост затрат"

    if has_down and not has_up:
        return "↓", "Снижение затрат"

    return "→", "Нейтрально / требуется проверка"


def fuel_effect(text: str, transports: list[str], movement: str) -> str:
    scope = ", ".join(transports)

    if movement == "↑":
        return (
            f"Рост стоимости топлива создаёт повышательное давление на ставки "
            f"по видам транспорта: {scope}. Новые расчёты перевозки нужно перепроверить."
        )

    if movement == "↓":
        return (
            f"Снижение стоимости топлива ослабляет давление на себестоимость "
            f"перевозок по видам транспорта: {scope}. Реальное снижение ставки зависит "
            f"от топливной надбавки перевозчика и условий договора."
        )

    return (
        f"Сигнал по стоимости топлива для видов транспорта: {scope}. "
        f"Для изменения ставки требуется подтвердить фактическое движение цены "
        f"и механизм топливной надбавки перевозчика."
    )


def toll_effect(movement: str) -> str:
    if movement == "↑":
        return (
            "Рост дорожного тарифа напрямую увеличивает себестоимость автоперевозки "
            "на затронутых платных участках; маршрутные ставки нужно пересчитать."
        )

    if movement == "↓":
        return (
            "Снижение дорожного тарифа уменьшает прямые маршрутные расходы "
            "автоперевозчика на затронутых участках."
        )

    return (
        "Изменение правил или тарифа платной дороги может изменить себестоимость "
        "автоперевозки; требуется проверить дату вступления и маршрут."
    )


def build_cost_item(article: dict, base: dict, translator) -> dict | None:
    title = clean(article.get("title"))
    excerpt = clean(article.get("excerpt"))
    language = clean(article.get("language"))
    url = clean(article.get("url"))
    category = clean(article.get("costCategory"))

    if not title or not url:
        return None

    combined = f"{title} {excerpt}"

    if category == "fuel" and not contains_any(combined, FUEL_TERMS):
        return None

    if category == "tolls" and not contains_any(combined, TOLL_TERMS):
        return None

    # Resolve Google News redirect and try to extract article text.
    resolved_url = base["resolve_google_news_url"](url)
    if resolved_url:
        url = resolved_url
        article["url"] = resolved_url

    article_text = ""
    try:
        if url and "news.google.com" not in url:
            article_text = base["article_excerpt"](url, title) or ""
    except Exception:
        article_text = ""

    evidence = clean(f"{title} {excerpt} {article_text}")

    if category == "fuel" and not contains_any(evidence, FUEL_TERMS):
        return None

    if category == "tolls" and not contains_any(evidence, TOLL_TERMS):
        return None

    title_ru = base["translate"](title, language, translator)
    text_ru = (
        base["translate"](article_text or excerpt, language, translator)
        if (article_text or excerpt)
        else ""
    )

    full_ru = clean(f"{title_ru} {text_ru}")
    movement, rate_pressure = movement_for(evidence + " " + full_ru)
    transports = infer_transport(evidence + " " + full_ru, category)
    directions = direction_for_cost_signal(evidence + " " + full_ru, base)

    domain = base["source_name"](
        url,
        clean(article.get("domain")),
    )

    event_country = base["event_geography_for"](
        evidence,
        clean(article.get("sourcecountry")),
    )

    route = base["route_for"](
        evidence,
        directions,
        clean(article.get("sourcecountry")),
        event_country,
    )

    # Keep the summary and cause distinct.
    summary = base["concrete_summary"](
        title_ru,
        text_ru,
        base["rule_for"](evidence) or base["RULES"][0],
        transports,
        route,
    )

    cause = base["concrete_cause"](
        text_ru,
        title_ru,
        summary,
    )

    if not cause or cause == base["UNKNOWN_CAUSE"]:
        if category == "fuel":
            if movement == "↑":
                cause = "Опубликовано повышение рыночной цены или ценового индикатора топлива."
            elif movement == "↓":
                cause = "Опубликовано снижение рыночной цены или ценового индикатора топлива."
            else:
                cause = "Опубликовано изменение ценового ориентира топлива."
        else:
            if movement == "↑":
                cause = "Оператор или власти повысили дорожный тариф либо сбор."
            elif movement == "↓":
                cause = "Оператор или власти снизили дорожный тариф либо сбор."
            else:
                cause = "Опубликовано изменение тарифа или правил оплаты дороги."

    effect = (
        fuel_effect(evidence + " " + full_ru, transports, movement)
        if category == "fuel"
        else toll_effect(movement)
    )

    score = 80

    if category == "tolls":
        score += 5

    if directions != ["Мировые"] and directions != ["Другие"]:
        score += 5

    if domain in {
        "reuters.com",
        "bloomberg.com",
        "mintrans.gov.ru",
        "rosavtodor.gov.ru",
        "avtodor-tr.ru",
        "beltoll.by",
        "mintrans.gov.by",
    }:
        score += 5

    score = min(score, 100)

    return {
        "date": base["date_for"](clean(article.get("seendate"))),
        "category": category,
        "categoryTitle": "Топливо и ставки" if category == "fuel" else "Платные дороги",
        "importance": "Высокая" if score >= 90 else "Средняя",
        "importanceScore": score,
        "sourceLanguage": (
            "Русскоязычный"
            if base["language_group"](language) == "russian"
            else "Иностранный"
        ),
        "transports": transports,
        "directions": directions,
        "title": title_ru,
        "country": event_country,
        "eventCountry": event_country,
        "route": route,
        "summary": summary,
        "fact": summary,
        "cause": cause,
        "consequence": effect,
        "effect": effect,
        "movement": movement,
        "ratePressure": rate_pressure,
        "sources": [{"name": domain, "url": url}],
        "assessment": (
            f"Сигнал влияния на логистические ставки: "
            f"{movement} {rate_pressure.lower()}; значимость {score}/100"
        ),
    }


def cost_item_key(item: dict) -> tuple:
    return (
        item.get("category"),
        clean(item.get("title")).lower(),
    )


def is_duplicate_cost_item(item: dict, selected: list[dict], base: dict) -> bool:
    title = clean(item.get("title"))

    for existing in selected:
        if item.get("category") != existing.get("category"):
            continue

        if base["sentence_similarity"](
            title,
            clean(existing.get("title")),
        ) >= 0.82:
            return True

    return False


def build_cost_signals(articles: list[dict], base: dict) -> list[dict]:
    translator = base["get_translator"]()
    now = datetime.now(timezone.utc)

    # Fresh unique URLs only.
    unique_by_url: dict[str, dict] = {}

    for article in articles:
        url = clean(article.get("url"))
        title = clean(article.get("title"))

        if (
            url.startswith("http")
            and title
            and base["is_recent_article"](
                clean(article.get("seendate")),
                now,
            )
        ):
            unique_by_url[url] = article

    candidates = list(unique_by_url.values())

    # Prefer official / high-value labels first.
    label_bonus = {
        "toll-roads-russia-official": 40,
        "toll-roads-belarus-official": 40,
        "fuel-reuters-bloomberg": 35,
        "toll-roads-russia": 25,
        "toll-roads-belarus": 25,
        "fuel-russia": 20,
        "fuel-belarus": 20,
        "fuel-global": 15,
        "fuel-china": 15,
    }

    candidates.sort(
        key=lambda article: label_bonus.get(
            clean(article.get("feedLabel")),
            0,
        ),
        reverse=True,
    )

    selected: list[dict] = []
    fuel_count = 0
    toll_count = 0
    per_domain: dict[str, int] = {}

    for article in candidates:
        category = clean(article.get("costCategory"))

        if category == "fuel" and fuel_count >= MAX_FUEL_SIGNALS:
            continue

        if category == "tolls" and toll_count >= MAX_TOLL_SIGNALS:
            continue

        item = build_cost_item(article, base, translator)
        if not item:
            continue

        domain = clean(item.get("sources", [{}])[0].get("name"))
        if per_domain.get(domain, 0) >= 2:
            continue

        if is_duplicate_cost_item(item, selected, base):
            continue

        selected.append(item)
        per_domain[domain] = per_domain.get(domain, 0) + 1

        if category == "fuel":
            fuel_count += 1
        elif category == "tolls":
            toll_count += 1

        if len(selected) >= MAX_COST_SIGNALS:
            break

    selected.sort(
        key=lambda item: (
            0 if item.get("category") == "tolls" else 1,
            -int(item.get("importanceScore", 0)),
        )
    )

    print(
        "Cost signals: "
        f"fuel={fuel_count}, tolls={toll_count}, total={len(selected)}"
    )

    return selected


def write_feed(feed: dict) -> None:
    temporary = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_PATH)


def main() -> int:
    print(f"Collector version: {COLLECTOR_VERSION}")

    try:
        base = load_baseline_namespace()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
    )

    # 1. Original feeds -> original main 12-news logic.
    core_articles: list[dict] = []
    core_failures: list[str] = []

    core_feeds = list(base["RSS_FEEDS"])

    for index, feed in enumerate(core_feeds):
        try:
            batch = base["fetch_google_news_rss"](session, feed)
            core_articles.extend(batch)
            print(f"RSS {feed['label']}: {len(batch)} articles")
        except Exception as error:
            core_failures.append(f"RSS {feed['label']}: {error}")

        if index < len(core_feeds) - 1:
            time.sleep(base["RSS_INTER_FEED_DELAY"])

    if not core_articles:
        for failure in core_failures:
            print(failure, file=sys.stderr)

        print(
            "ERROR: core RSS returned no articles; existing news.json was preserved.",
            file=sys.stderr,
        )
        return 1

    feed = base["build_feed"](core_articles)

    if not feed.get("news"):
        for failure in core_failures:
            print(failure, file=sys.stderr)

        print(
            "ERROR: all core articles were rejected; existing news.json was preserved.",
            file=sys.stderr,
        )
        return 1

    # 2. Independent cost-signal feeds.
    cost_articles, cost_failures = fetch_extra_articles(base, session)
    cost_signals = build_cost_signals(cost_articles, base)

    fuel_signals = [
        item for item in cost_signals
        if item.get("category") == "fuel"
    ]

    toll_signals = [
        item for item in cost_signals
        if item.get("category") == "tolls"
    ]

    feed["collectorVersion"] = COLLECTOR_VERSION
    feed["costSignals"] = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fuel": fuel_signals,
        "tolls": toll_signals,
        "all": cost_signals,
    }

    # Keep the original main "news" array untouched.
    write_feed(feed)

    print(f"Saved {len(feed['news'])} news items to {OUTPUT_PATH}")
    print(
        "Saved cost signals: "
        f"fuel={len(fuel_signals)}, tolls={len(toll_signals)}"
    )

    all_failures = core_failures + cost_failures
    if all_failures:
        print(
            "Partial RSS failures: " + "; ".join(all_failures),
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
