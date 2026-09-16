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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


BASELINE_URL = (
    "https://raw.githubusercontent.com/"
    "molesta425-design/logistics-news-gdelt/"
    "46ace41/update_news.py"
)

COLLECTOR_VERSION = "2026-09-15-regulation-v1"

OUTPUT_PATH = Path(__file__).with_name("news.json")

RSS_RETRIES = 3
RSS_RETRY_DELAY = 10
RSS_INTER_FEED_DELAY = 2

MAX_FUEL_SIGNALS = 4
MAX_TOLL_SIGNALS = 4
MAX_COST_SIGNALS = MAX_FUEL_SIGNALS + MAX_TOLL_SIGNALS
MAX_REGULATION_SIGNALS = 4
MAX_REGULATION_BUILD_CANDIDATES = 18

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
        "label": "toll-roads-russia-official",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "cost-signal",
        "costCategory": "tolls",
        "query": (
            '(site:platon.ru OR site:avtodor-tr.ru OR site:rosavtodor.gov.ru OR site:mintrans.gov.ru) '
            '("платные дороги" OR "Платон" OR тариф OR проезд OR индексация OR '
            '"М-1" OR "М-3" OR "М-4" OR "М-11" OR "М-12" OR ЦКАД) when:1d'
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

OFFICIAL_TOLL_DOMAINS = (
    "platon.ru",
    "avtodor-tr.ru",
    "rosavtodor.gov.ru",
    "mintrans.gov.ru",
    "beltoll.by",
    "mintrans.gov.by",
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

CRUDE_FUEL_TERMS = (
    "brent",
    "urals",
    "crude oil",
    "oil price",
    "oil prices",
    "нефть brent",
    "нефть urals",
    "цена нефти",
    "цены на нефть",
)

FUEL_BUCKET_ORDER = ("crude", "road", "marine", "air")

FUEL_BUCKET_TITLES = {
    "crude": "Нефть Brent / Urals",
    "road": "Автотопливо / дизель",
    "marine": "Судовое топливо",
    "air": "Авиационное топливо",
}

FUEL_BUILD_LIMIT_PER_BUCKET = 8

REGULATION_TYPE_TITLES = {
    "documents": "Документы",
    "restrictions": "Ограничения",
    "borders": "Граница / таможня",
    "tariffs": "Тарифы и сборы",
    "tolls": "Платные дороги",
}

REGULATION_TYPE_ORDER = (
    "tolls",
    "borders",
    "documents",
    "restrictions",
    "tariffs",
)

REGULATION_DOCUMENT_TERMS = (
    "transport document",
    "transport documents",
    "electronic transport document",
    "electronic consignment note",
    "consignment note",
    "e-cmr",
    "cmr",
    "bill of lading",
    "electronic bill of lading",
    "ebl",
    "air waybill",
    "e-awb",
    "rail consignment note",
    "smgs",
    "cim",
    "transit declaration",
    "customs declaration",
    "cargo manifest",
    "transport permit",
    "transport documents",
    "documentation requirement",
    "транспортн документ",
    "перевозочн документ",
    "электронн перевозочн",
    "электронн транспортн накладн",
    "транспортн накладн",
    "эпд",
    "е-cmr",
    "e-cmr",
    "коносамент",
    "авианакладн",
    "железнодорожн накладн",
    "смгс",
    "цим",
    "транзитн деклараци",
    "таможенн деклараци",
    "грузов манифест",
    "разрешени на перевоз",
)

REGULATION_BORDER_TERMS = (
    "border crossing",
    "border checkpoint",
    "checkpoint",
    "customs checkpoint",
    "customs clearance",
    "border control",
    "customs control",
    "border closure",
    "border restriction",
    "пункт пропуска",
    "погранпереход",
    "пограничн переход",
    "границ",
    "таможенн оформлен",
    "таможенн контрол",
    "таможн",
    "досмотр",
)

REGULATION_RESTRICTION_TERMS = (
    "freight restriction",
    "truck restriction",
    "road restriction",
    "weight restriction",
    "axle load",
    "seasonal restriction",
    "movement restriction",
    "traffic restriction",
    "cargo ban",
    "import ban",
    "export ban",
    "booking suspension",
    "закрытие движения",
    "ограничение движения",
    "ограничения движения",
    "весогабарит",
    "осев нагруз",
    "временн огранич",
    "сезонн огранич",
    "запрет движения",
    "запрет перевоз",
    "запрет на ввоз",
    "запрет на вывоз",
    "приостановк перевоз",
)

REGULATION_TARIFF_TERMS = (
    "rail tariff",
    "rail freight tariff",
    "freight tariff",
    "port dues",
    "port fee",
    "terminal fee",
    "customs duty",
    "customs tariff",
    "tariff indexation",
    "tariff increase",
    "tariff decrease",
    "surcharge",
    "железнодорожн тариф",
    "тариф на груз",
    "тариф перевоз",
    "индексац тариф",
    "портов сбор",
    "терминальн сбор",
    "таможенн пошлин",
    "таможенн тариф",
    "дорожн сбор",
    "ставк сбор",
    "повышение тарифа",
    "снижение тарифа",
)

REGULATION_PRIORITY_REGION_TERMS = (
    "росси",
    "рф",
    "беларус",
    "рб",
    "китай",
    "china",
    "турц",
    "turkey",
    "еаэс",
    "eaeu",
)

REGULATION_OFFICIAL_MARKERS = (
    ".gov",
    ".gov.ru",
    ".gov.by",
    ".gov.cn",
    ".gov.tr",
    "government.ru",
    "publication.pravo.gov.ru",
    "pravo.by",
    "eec.eaeunion.org",
    "rzd.ru",
    "company.rzd.ru",
    "rw.by",
    "platon.ru",
    "avtodor-tr.ru",
    "beltoll.by",
    "iru.org",
    "iata.org",
    "imo.org",
    "wcoomd.org",
    "ec.europa.eu",
)


def clean(value) -> str:
    return " ".join(str(value or "").split())


def contains_any(text: str, terms) -> bool:
    lowered = clean(text).lower()
    return any(term.lower() in lowered for term in terms)


def fuel_bucket_for_text(text: str) -> str:
    """Map a fuel article to exactly one logistics cost bucket."""
    lowered = clean(text).lower()

    # Specific transport fuels win over broad oil mentions.
    if contains_any(lowered, AIR_FUEL_TERMS):
        return "air"

    if contains_any(lowered, SEA_FUEL_TERMS):
        return "marine"

    if contains_any(lowered, ROAD_FUEL_TERMS):
        return "road"

    if contains_any(lowered, CRUDE_FUEL_TERMS):
        return "crude"

    return ""


def geography_priority(text: str) -> int:
    """Priority follows the project's logistics directions."""
    lowered = clean(text).lower()

    if re.search(r"(?<!\w)(рф|россия|россии|российский|russia|russian)(?!\w)", lowered):
        return 100

    if re.search(r"(?<!\w)(рб|беларусь|белоруссия|belarus)(?!\w)", lowered):
        return 95

    if re.search(r"(?<!\w)(европа|евросоюз|ес|europe|european|eu)(?!\w)", lowered):
        return 85

    if re.search(r"(?<!\w)(китай|china|chinese)(?!\w)", lowered):
        return 80

    if re.search(r"(?<!\w)(турция|turkey|turkish)(?!\w)", lowered):
        return 75

    if re.search(r"(?<!\w)(сша|usa|u\.s\.|united states)(?!\w)", lowered):
        return 45

    if re.search(r"(?<!\w)(бразилия|brazil|petrobras)(?!\w)", lowered):
        return 35

    return 65


def is_official_toll_source(domain: str, url: str = "") -> bool:
    """Return True only for approved official toll-road domains/subdomains."""
    candidates: list[str] = []

    raw_domain = clean(domain).lower().strip().strip(".")
    if raw_domain:
        if raw_domain.startswith("www."):
            raw_domain = raw_domain[4:]
        candidates.append(raw_domain)

    raw_url = clean(url)
    if raw_url:
        try:
            host = (urlsplit(raw_url).hostname or "").lower().strip(".")
            if host.startswith("www."):
                host = host[4:]
            if host:
                candidates.append(host)
        except Exception:
            pass

    for candidate in candidates:
        for official_domain in OFFICIAL_TOLL_DOMAINS:
            if candidate == official_domain or candidate.endswith("." + official_domain):
                return True

    return False


def source_priority(domain: str) -> int:
    lowered = clean(domain).lower()

    official_markers = (
        "platon.ru",
        "minenergo.gov.ru",
        "rosstat.gov.ru",
        "mintrans.gov.ru",
        "rosavtodor.gov.ru",
        "avtodor-tr.ru",
        "beltoll.by",
        "mintrans.gov.by",
        "belneftekhim.by",
    )

    if any(marker in lowered for marker in official_markers):
        return 100

    if "reuters.com" in lowered:
        return 95

    if "bloomberg.com" in lowered:
        return 90

    if "interfax.ru" in lowered:
        return 85

    return 60


def raw_article_priority(article: dict) -> int:
    text = clean(
        f"{article.get('title', '')} {article.get('excerpt', '')} "
        f"{article.get('sourcecountry', '')}"
    )

    label = clean(article.get("feedLabel"))

    label_bonus = {
        "fuel-russia": 50,
        "fuel-belarus": 45,
        "fuel-china": 35,
        "fuel-reuters-bloomberg": 30,
        "fuel-global": 20,
        "toll-roads-russia-official": 55,
        "toll-roads-belarus-official": 55,
    }.get(label, 0)

    return geography_priority(text) * 10 + label_bonus


def final_fuel_priority(item: dict, article: dict) -> int:
    source = (item.get("sources") or [{}])[0] or {}
    domain = clean(source.get("name"))

    text = clean(
        f"{item.get('title', '')} {item.get('summary', '')} "
        f"{item.get('route', '')} {item.get('country', '')} "
        f"{article.get('title', '')} {article.get('excerpt', '')}"
    )

    movement_bonus = 8 if item.get("movement") in {"↑", "↓"} else 0

    return (
        geography_priority(text) * 1000
        + source_priority(domain) * 10
        + movement_bonus
        + int(item.get("importanceScore", 0))
    )



def regulation_type_for_text(text: str) -> str:
    lowered = clean(text).lower()

    # More specific classes first.
    if contains_any(lowered, REGULATION_DOCUMENT_TERMS):
        return "documents"

    if contains_any(lowered, REGULATION_BORDER_TERMS):
        return "borders"

    if contains_any(lowered, REGULATION_RESTRICTION_TERMS):
        return "restrictions"

    if contains_any(lowered, REGULATION_TARIFF_TERMS):
        return "tariffs"

    # Russian tariff wording is highly inflected, so use stems only when the
    # same text is clearly about freight/customs/transport infrastructure.
    if contains_any(
        lowered,
        ("тариф", "пошлин", "сбор", "индексац"),
    ) and contains_any(
        lowered,
        (
            "груз",
            "перевоз",
            "железнод",
            "ржд",
            "порт",
            "терминал",
            "тамож",
            "фрахт",
            "freight",
            "cargo",
            "rail",
            "port",
            "terminal",
            "customs",
            "truck",
        ),
    ):
        return "tariffs"

    return ""


def regulation_source_is_trusted(article: dict, base: dict) -> bool:
    url = clean(article.get("url"))
    domain = base["source_name"](url, clean(article.get("domain"))).lower()
    source_type = clean(article.get("sourceType")).lower()

    if any(marker in domain for marker in REGULATION_OFFICIAL_MARKERS):
        return True

    # Reuters/Bloomberg and baseline trusted sources are accepted as
    # secondary confirmation when an official page is not indexed yet.
    if domain in {"reuters.com", "bloomberg.com"}:
        return True

    try:
        if base["domain_bonus"](domain) >= 8:
            return True
    except Exception:
        pass

    if source_type in {"carrier", "documents"}:
        return True

    return False


def regulation_verification(article: dict, base: dict) -> str:
    url = clean(article.get("url"))
    domain = base["source_name"](url, clean(article.get("domain"))).lower()

    if any(marker in domain for marker in REGULATION_OFFICIAL_MARKERS):
        return "Официальный / первичный источник"

    if domain in {"reuters.com", "bloomberg.com"}:
        return "Подтверждено надёжным информационным источником"

    return "Проверенный отраслевой источник"


def regulation_raw_priority(article: dict, base: dict) -> int:
    text = clean(
        f"{article.get('title', '')} {article.get('excerpt', '')} "
        f"{article.get('sourcecountry', '')}"
    )
    category = regulation_type_for_text(text)
    if not category:
        return -10000

    score = geography_priority(text) * 10

    if contains_any(text, REGULATION_PRIORITY_REGION_TERMS):
        score += 120

    url = clean(article.get("url"))
    domain = base["source_name"](url, clean(article.get("domain"))).lower()

    if any(marker in domain for marker in REGULATION_OFFICIAL_MARKERS):
        score += 150
    elif domain == "reuters.com":
        score += 110
    elif domain == "bloomberg.com":
        score += 100
    else:
        try:
            score += max(0, base["domain_bonus"](domain)) * 5
        except Exception:
            pass

    # Border/document changes are more operationally urgent for the project.
    score += {
        "borders": 80,
        "documents": 70,
        "restrictions": 60,
        "tariffs": 50,
    }.get(category, 0)

    return score


def clone_regulation_item(item: dict, category: str, verification: str) -> dict:
    copied = dict(item)
    copied["regulationType"] = category
    copied["regulationTypeTitle"] = REGULATION_TYPE_TITLES.get(category, category)
    copied["verification"] = verification
    return copied


def is_duplicate_regulation_item(item: dict, selected: list[dict], base: dict) -> bool:
    title = clean(item.get("title"))
    for existing in selected:
        if base["sentence_similarity"](
            title,
            clean(existing.get("title")),
        ) >= 0.78:
            return True
    return False


def build_regulation_signals(
    core_articles: list[dict],
    toll_signals: list[dict],
    base: dict,
) -> list[dict]:
    """
    Build the compact third column independently from the 12-news selection.
    The source pool is the already fetched global core feed, so no extra RSS
    requests are added and the working collector remains stable.
    """
    translator = base["get_translator"]()
    now = datetime.now(timezone.utc)

    unique_by_url: dict[str, dict] = {}
    for article in core_articles:
        url = clean(article.get("url"))
        title = clean(article.get("title"))
        raw_text = clean(f"{title} {article.get('excerpt', '')}")

        if not (
            url.startswith("http")
            and title
            and regulation_type_for_text(raw_text)
            and regulation_source_is_trusted(article, base)
            and base["is_recent_article"](clean(article.get("seendate")), now)
        ):
            continue

        unique_by_url[url] = article

    candidates = list(unique_by_url.values())
    candidates.sort(
        key=lambda article: regulation_raw_priority(article, base),
        reverse=True,
    )

    built_by_type: dict[str, list[dict]] = {
        "borders": [],
        "documents": [],
        "restrictions": [],
        "tariffs": [],
    }

    build_count = 0
    for article in candidates:
        if build_count >= MAX_REGULATION_BUILD_CANDIDATES:
            break

        raw_text = clean(f"{article.get('title', '')} {article.get('excerpt', '')}")
        category = regulation_type_for_text(raw_text)
        if not category:
            continue

        build_count += 1
        try:
            item = base["article_to_news"](article, translator)
        except Exception as error:
            print(
                f"Regulation build warning for {clean(article.get('title'))}: {error}",
                file=sys.stderr,
            )
            continue

        if not item:
            continue

        verification = regulation_verification(article, base)
        built = clone_regulation_item(item, category, verification)

        if is_duplicate_regulation_item(
            built,
            [
                existing
                for values in built_by_type.values()
                for existing in values
            ],
            base,
        ):
            continue

        built_by_type[category].append(built)

    # Existing toll cards are already official-only. Add them to the same
    # compact regulation feed, without changing costSignals.tolls.
    toll_items: list[dict] = []
    for item in toll_signals:
        copied = clone_regulation_item(
            item,
            "tolls",
            clean(item.get("verification")) or "Подтверждено официальным источником",
        )
        toll_items.append(copied)

    for category, values in built_by_type.items():
        values.sort(
            key=lambda item: int(item.get("importanceScore", 0)),
            reverse=True,
        )

    toll_items.sort(
        key=lambda item: int(item.get("importanceScore", 0)),
        reverse=True,
    )

    pools = {
        **built_by_type,
        "tolls": toll_items,
    }

    selected: list[dict] = []

    # First pass: no more than one card from each category.
    for category in REGULATION_TYPE_ORDER:
        values = pools.get(category, [])
        if not values:
            continue

        candidate = values.pop(0)
        if not is_duplicate_regulation_item(candidate, selected, base):
            selected.append(candidate)

        if len(selected) >= MAX_REGULATION_SIGNALS:
            break

    # Second pass: fill remaining slots with the strongest leftovers.
    if len(selected) < MAX_REGULATION_SIGNALS:
        leftovers = [
            item
            for category in REGULATION_TYPE_ORDER
            for item in pools.get(category, [])
        ]
        leftovers.sort(
            key=lambda item: int(item.get("importanceScore", 0)),
            reverse=True,
        )

        for item in leftovers:
            if is_duplicate_regulation_item(item, selected, base):
                continue

            selected.append(item)
            if len(selected) >= MAX_REGULATION_SIGNALS:
                break

    print(
        "Regulation signals: "
        + ", ".join(
            f"{item.get('regulationType')}={item.get('title', '')[:45]}"
            for item in selected
        )
    )

    return selected


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

            import types

            module_name = "logistics_news_baseline"
            module = types.ModuleType(module_name)
            module.__file__ = str(Path(__file__))
            sys.modules[module_name] = module

            exec(compile(source, BASELINE_URL, "exec"), module.__dict__)
            return module.__dict__

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

    fuel_type = ""
    if category == "fuel":
        fuel_type = fuel_bucket_for_text(evidence + " " + full_ru)
        if not fuel_type:
            return None

    movement, rate_pressure = movement_for(evidence + " " + full_ru)
    transports = infer_transport(evidence + " " + full_ru, category)
    directions = direction_for_cost_signal(evidence + " " + full_ru, base)

    domain = base["source_name"](
        url,
        clean(article.get("domain")),
    )

    official_confirmed = False
    if category == "tolls":
        official_confirmed = is_official_toll_source(domain, url)
        if not official_confirmed:
            return None

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
        "platon.ru",
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
        "fuelType": fuel_type if category == "fuel" else "",
        "fuelTypeTitle": FUEL_BUCKET_TITLES.get(fuel_type, "") if category == "fuel" else "",
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
        "officialConfirmed": official_confirmed if category == "tolls" else None,
        "verification": (
            "Подтверждено официальным источником"
            if category == "tolls" and official_confirmed
            else ""
        ),
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

    # ------------------------------------------------------------
    # FUEL: exactly one best card per distinct logistics indicator.
    # This prevents several Reuters/Bloomberg articles about the
    # same diesel or Brent move from filling the whole column.
    # ------------------------------------------------------------
    fuel_groups: dict[str, list[dict]] = {
        bucket: [] for bucket in FUEL_BUCKET_ORDER
    }

    toll_candidates: list[dict] = []

    for article in candidates:
        category = clean(article.get("costCategory"))

        if category == "fuel":
            raw_text = clean(
                f"{article.get('title', '')} {article.get('excerpt', '')}"
            )
            bucket = fuel_bucket_for_text(raw_text)

            if bucket:
                fuel_groups[bucket].append(article)

        elif category == "tolls":
            toll_candidates.append(article)

    fuel_selected: list[dict] = []

    for bucket in FUEL_BUCKET_ORDER:
        group = fuel_groups[bucket]
        group.sort(key=raw_article_priority, reverse=True)

        built: list[tuple[dict, dict]] = []

        # Resolve only a limited number of the best raw candidates.
        for article in group[:FUEL_BUILD_LIMIT_PER_BUCKET]:
            item = build_cost_item(article, base, translator)

            if not item:
                continue

            if item.get("fuelType") != bucket:
                continue

            built.append((item, article))

        if not built:
            continue

        built.sort(
            key=lambda pair: final_fuel_priority(pair[0], pair[1]),
            reverse=True,
        )

        best_item = built[0][0]
        fuel_selected.append(best_item)

        if len(fuel_selected) >= MAX_FUEL_SIGNALS:
            break

    # ------------------------------------------------------------
    # TOLLS: output ONLY officially confirmed changes.
    # Media articles are not allowed to create a toll card.
    # ------------------------------------------------------------
    toll_candidates.sort(key=raw_article_priority, reverse=True)

    toll_selected: list[dict] = []
    toll_per_domain: dict[str, int] = {}

    for article in toll_candidates:
        if len(toll_selected) >= MAX_TOLL_SIGNALS:
            break

        item = build_cost_item(article, base, translator)
        if not item:
            continue

        domain = clean(item.get("sources", [{}])[0].get("name"))

        if toll_per_domain.get(domain, 0) >= 2:
            continue

        if is_duplicate_cost_item(item, toll_selected, base):
            continue

        toll_selected.append(item)
        toll_per_domain[domain] = toll_per_domain.get(domain, 0) + 1

    selected = toll_selected + fuel_selected

    print(
        "Cost signals: "
        f"fuel={len(fuel_selected)} "
        f"({', '.join(item.get('fuelType', '') for item in fuel_selected)}), "
        f"tolls={len(toll_selected)}, total={len(selected)}"
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

    # 3. Independent compact feed for the third DataLens column.
    # It is built from the full fetched core pool, not only from the selected
    # 12 main news cards, and includes official toll-road changes.
    regulation_signals = build_regulation_signals(
        core_articles,
        toll_signals,
        base,
    )

    regulation_groups = {
        category: [
            item for item in regulation_signals
            if item.get("regulationType") == category
        ]
        for category in REGULATION_TYPE_TITLES
    }

    feed["collectorVersion"] = COLLECTOR_VERSION
    feed["costSignals"] = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "fuel": fuel_signals,
        "tolls": toll_signals,
        "all": cost_signals,
    }
    feed["regulationSignals"] = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "documents": regulation_groups["documents"],
        "restrictions": regulation_groups["restrictions"],
        "borders": regulation_groups["borders"],
        "tariffs": regulation_groups["tariffs"],
        "tolls": regulation_groups["tolls"],
        "all": regulation_signals,
    }

    # Keep the original main "news" array untouched.
    write_feed(feed)

    print(f"Saved {len(feed['news'])} news items to {OUTPUT_PATH}")
    print(
        "Saved cost signals: "
        f"fuel={len(fuel_signals)}, tolls={len(toll_signals)}"
    )
    print(
        "Saved regulation signals: "
        f"{len(regulation_signals)}"
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
