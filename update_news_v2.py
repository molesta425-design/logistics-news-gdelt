#!/usr/bin/env python3
"""Build a Russian logistics-news feed from the free GDELT DOC API.

The script deliberately uses no paid API. Translation is performed locally
with Argos Translate. Cause and effect are rule-based assessments and are
labelled as such in the output metadata.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
import trafilatura


API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
OUTPUT_PATH = Path(__file__).with_name("news.json")
MAX_NEWS = 12
TARGET_PER_LANGUAGE = MAX_NEWS // 2
MAX_CANDIDATES_PER_LANGUAGE = 60
MIN_LOGISTICS_SCORE = 72
GDELT_MAX_RECORDS = 250
GDELT_RETRIES = 4
GDELT_RETRY_DELAY = 15

RISK_TERMS_EN = (
    "closure OR closed OR strike OR tariff OR sanction OR congestion OR "
    "disruption OR attack OR accident OR storm OR restriction OR delay OR "
    "surcharge OR ban OR derailment OR collision OR piracy OR flood OR drought"
)

RISK_TERMS_RU = (
    "закрытие OR закрыт OR забастовка OR тариф OR санкции OR очередь OR "
    "перегрузка OR сбой OR атака OR авария OR шторм OR ограничение OR "
    "задержка OR надбавка OR запрет OR крушение OR столкновение OR пиратство OR "
    "наводнение OR засуха"
)

QUERIES = [
    (
        '(freight OR cargo OR shipping OR port OR maritime OR vessel OR canal OR '
        'railway OR railroad OR train OR trucking OR truck OR customs OR '
        '"border crossing" OR "air cargo" OR airport) '
        f'({RISK_TERMS_EN} OR Belarus OR Russia OR Turkey OR China) '
        'sourcelang:english'
    ),
    (
        '(груз OR грузоперевозки OR порт OR судно OR морские перевозки OR '
        'контейнер OR железная дорога OR поезд OR вагон OR грузовик OR фура OR '
        'таможня OR "пункт пропуска" OR авиагруз OR аэропорт) '
        f'({RISK_TERMS_RU} OR Беларусь OR Россия OR Турция OR Китай) '
        'sourcelang:russian'
    ),
]

USER_AGENT = "logistics-news-gdelt/1.0 (+public GitHub Actions feed)"


@dataclass(frozen=True)
class Rule:
    patterns: tuple[str, ...]
    cause: str
    effect: str
    importance: str
    score: int


RULES = [
    Rule(
        (
            "attack", "drone", "missile", "war ", "armed", "piracy", "pirate",
            "hijack", "strike on", "атак", "беспилот", "ракет", "пират",
        ),
        "Угроза безопасности, вооружённый инцидент или нападение на транспортную инфраструктуру.",
        "Возможны остановка движения, перенаправление грузов, рост страховых надбавок, стоимости и сроков доставки.",
        "Высокая",
        100,
    ),
    Rule(
        ("sanction", "sanctions", "export control", "import ban", "trade ban", "санкц", "запрет на импорт", "запрет на экспорт"),
        "Изменение санкционных или внешнеторговых ограничений.",
        "Нужно повторно проверить допустимость груза, перевозчика и расчётов; возможны отказ в перевозке и смена маршрута.",
        "Высокая",
        95,
    ),
    Rule(
        ("strike", "strikes", "walkout", "work stoppage", "labor action", "забастов", "стачк"),
        "Забастовка или иное ограничение работы персонала.",
        "Снижается пропускная способность; вероятны очереди, отмены операций и дополнительный простой транспорта.",
        "Высокая",
        90,
    ),
    Rule(
        ("closed", "closure", "closures", "suspend", "suspended", "shutdown", "blockade", "закрыт", "приостанов", "перекрыт", "блокад"),
        "Закрытие или временное ограничение работы маршрута, перехода либо терминала.",
        "Грузы потребуется перенаправлять; вероятны очереди, увеличение пробега, сроков и стоимости доставки.",
        "Высокая",
        88,
    ),
    Rule(
        ("derail", "derailed", "derailment", "collision", "collided", "crash", "sank", "sunk", "fire", "explosion", "accident", "авари", "столкнов", "крушен", "пожар", "взрыв", "затон"),
        "Авария или повреждение транспорта либо инфраструктуры.",
        "Возможны временная недоступность участка, задержки, дополнительная перегрузка и перенаправление грузов.",
        "Высокая",
        86,
    ),
    Rule(
        ("storm", "typhoon", "hurricane", "cyclone", "flood", "drought", "wildfire", "ice", "snow", "шторм", "тайфун", "ураган", "циклон", "наводнен", "засух", "лед", "снег"),
        "Неблагоприятные погодные условия.",
        "Возможны ограничения движения и обработки грузов, пропуск рейсов, очереди и увеличение транзитного времени.",
        "Высокая",
        84,
    ),
    Rule(
        ("border", "customs", "checkpoint", "inspection", "clearance", "границ", "тамож", "пункт пропуска", "досмотр", "оформлен"),
        "Изменение режима пограничного или таможенного контроля.",
        "Может вырасти время оформления; необходимо проверить документы, ограничения по грузу и доступность перехода.",
        "Средняя",
        72,
    ),
    Rule(
        ("congestion", "backlog", "backlogs", "queue", "queues", "overload", "перегруз", "очеред", "скоплен"),
        "Перегрузка инфраструктуры и накопление необработанных грузов.",
        "Вероятны ожидание свободного слота, простой транспорта и рост расходов на хранение и демередж.",
        "Средняя",
        70,
    ),
    Rule(
        ("tariff", "tariffs", "surcharge", "surcharges", "fee", "fees", "toll", "rate increase", "тариф", "надбавк", "сбор", "платон", "ставк"),
        "Изменение тарифа, сбора или коммерческой надбавки.",
        "Стоимость перевозки изменится; действующие расчёты и предложения клиентам требуется пересчитать.",
        "Средняя",
        68,
    ),
    Rule(
        ("delay", "delayed", "delays", "disruption", "disruptions", "restriction", "restrictions", "divert", "diverted", "reroute", "rerouted", "задерж", "сбой", "огранич", "перенаправ", "изменение маршрута"),
        "Операционные ограничения или изменение маршрута.",
        "Возможны увеличение срока доставки, дополнительный пробег, перегрузка и рост стоимости.",
        "Средняя",
        60,
    ),
]

TRANSPORT_TERMS = {
    "Авто": ("truck", "trucking", "lorry", "road freight", "highway", "border crossing", "грузовик", "грузовой автомобил", "грузовых автомобил", "автоперевоз", "фур", "автомобильн"),
    "Ж/д": ("rail", "railway", "railroad", "train", "wagon", "derail", "железнодорож", "поезд", "вагон", "ржд"),
    "Море": ("port", "ship", "shipping", "vessel", "maritime", "tanker", "container ship", "canal", "strait", "sea ", "порт", "судн", "морск", "танкер", "контейнеровоз", "канал", "пролив"),
    "Авиа": ("air cargo", "air freight", "airport", "airline", "flight", "авиагруз", "авиаперевоз", "аэропорт", "авиакомпан", "рейс"),
}

# At least one explicit commercial-freight term is required.  This prevents
# passenger tourism, baggage and general political stories from entering the
# feed merely because they mention a border, airport or port.
COMMERCIAL_FREIGHT_TERMS = (
    "freight", "cargo", "container", "merchant ship", "commercial vessel",
    "shipping", "tanker", "terminal", "truck", "trucking", "lorry",
    "rail freight", "freight train", "wagon", "goods", "consignment",
    "air cargo", "air freight", "warehouse", "port operations",
    "груз", "контейнер", "торговое судно", "судоход", "танкер", "терминал",
    "грузовик", "фур", "вагон", "товар", "отправк", "авиагруз", "склад",
    "портов", "перевозк",
)

PASSENGER_TERMS = (
    "tourist", "tourism", "passenger", "baggage", "luggage", "vacation",
    "holidaymaker", "турист", "пассажир", "багаж", "отпуск", "путешеств",
)

MILITARY_TERMS = (
    "military", "weapon", "ammunition", "troops", "battlefield", "frontline",
    "военн", "оруж", "боеприпас", "войск", "фронт", "всу",
)

COMMENTARY_TERMS = (
    "opinion", "analysis video", "daily review", "live updates", "explainer",
    "мнение", "обзор событий", "видео", "онлайн-трансляц", "что известно",
)

PRIORITY_REGION_TERMS = (
    "belarus", "russia", "turkey", "türkiye", "china",
    "беларус", "росси", "турц", "китай",
)

TRUSTED_DOMAINS = {
    "reuters.com": 14,
    "apnews.com": 12,
    "imo.org": 12,
    "iata.org": 12,
    "maersk.com": 10,
    "msc.com": 10,
    "rzd.ru": 10,
    "customs.gov.ru": 10,
    "gpk.gov.by": 10,
    "bamap.org": 8,
    "portnews.ru": 8,
}

LOW_QUALITY_DOMAINS = {
    "comandir.com",
    "news.mail.ru",
    "24tv.ua",
}

REGIONS = [
    (("black sea", "черное море", "чёрное море"), "Черноморский регион"),
    (("red sea", "красное море"), "Красное море — Суэцкий канал"),
    (("suez", "суэц"), "Красное море — Суэцкий канал"),
    (("panama canal", "панамск"), "Панамский канал"),
    (("hormuz", "ормуз"), "Ормузский пролив"),
    (("baltic", "балтик"), "Балтийский регион"),
    (("mediterranean", "средизем"), "Средиземноморский регион"),
    (("north sea", "северное море"), "Северное море"),
    (("arctic", "northern sea route", "аркти", "северный морской путь"), "Северный морской путь"),
    (("europe", "eu ", "европ", "ес "), "Европа"),
    (("middle east", "ближний восток"), "Ближний Восток"),
]


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def has_term(lowered_text: str, term: str) -> bool:
    """Match English words exactly and Russian stems by substring."""
    normalized = term.strip().lower()
    if re.fullmatch(r"[a-z0-9 -]+", normalized):
        pattern = r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])"
        return re.search(pattern, lowered_text) is not None
    return normalized in lowered_text


def fetch_gdelt(session: requests.Session, query: str) -> list[dict]:
    last_error: Exception | None = None

    for attempt in range(1, GDELT_RETRIES + 1):
        try:
            response = session.get(
                API_URL,
                params={
                    "query": query,
                    "mode": "artlist",
                    "maxrecords": GDELT_MAX_RECORDS,
                    "format": "json",
                    "sort": "datedesc",
                    "timespan": "24h",
                },
                timeout=75,
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            body_start = response.text[:250].lower()
            if "application/json" not in content_type and (
                "limit requests" in body_start
                or "temporarily unavailable" in body_start
                or "please try again" in body_start
            ):
                raise RuntimeError(clean(response.text[:250]))

            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("GDELT returned a non-object JSON response")
            return payload.get("articles", [])
        except Exception as error:
            last_error = error
            if attempt == GDELT_RETRIES:
                break
            delay = GDELT_RETRY_DELAY * attempt
            print(
                f"GDELT retry {attempt}/{GDELT_RETRIES - 1} in {delay}s: {error}",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(f"GDELT request failed after {GDELT_RETRIES} attempts: {last_error}")


def get_translator():
    import argostranslate.package
    import argostranslate.translate

    installed = argostranslate.translate.get_installed_languages()
    source = next((item for item in installed if item.code == "en"), None)
    target = next((item for item in installed if item.code == "ru"), None)
    if source and target:
        translator = source.get_translation(target)
        if translator:
            return translator

    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    package = next(
        item for item in available if item.from_code == "en" and item.to_code == "ru"
    )
    argostranslate.package.install_from_path(package.download())

    installed = argostranslate.translate.get_installed_languages()
    source = next(item for item in installed if item.code == "en")
    target = next(item for item in installed if item.code == "ru")
    return source.get_translation(target)


def translate(text: str, language: str, translator) -> str:
    text = clean(text)
    if not text:
        return ""
    if language.lower().startswith(("russian", "rus", "ru")):
        return text
    try:
        return clean(translator.translate(text))
    except Exception as error:  # one bad title must not stop the daily feed
        print(f"Translation warning: {error}", file=sys.stderr)
        return text


def article_excerpt(url: str) -> str:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        text = clean(text)
        if not text:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        selected: list[str] = []
        total = 0
        for sentence in sentences:
            sentence = clean(sentence)
            if len(sentence) < 35:
                continue
            selected.append(sentence)
            total += len(sentence)
            if total >= 420 or len(selected) == 2:
                break
        return " ".join(selected)[:650]
    except Exception as error:
        print(f"Article extraction warning for {url}: {error}", file=sys.stderr)
        return ""


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", title.lower()).strip()


def is_duplicate(title: str, accepted: Iterable[str]) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return True
    for other in accepted:
        other_normalized = normalize_title(other)
        if normalized == other_normalized:
            return True
        if SequenceMatcher(None, normalized, other_normalized).ratio() >= 0.84:
            return True
    return False


def rule_for(text: str) -> Rule | None:
    lowered = text.lower()
    matches = [rule for rule in RULES if any(has_term(lowered, term) for term in rule.patterns)]
    return max(matches, key=lambda item: item.score) if matches else None


def transports_for(text: str) -> list[str]:
    lowered = text.lower()
    result = [
        name
        for name, terms in TRANSPORT_TERMS.items()
        if any(has_term(lowered, term) for term in terms)
    ]
    return result


def directions_for(text: str) -> list[str]:
    lowered = text.lower()
    belarus = any(has_term(lowered, term) for term in ("belarus", "belarusian", "беларус", "минск"))
    russia = any(has_term(lowered, term) for term in ("russia", "russian", "росси", "москва"))
    turkey = any(has_term(lowered, term) for term in ("turkey", "turkish", "türkiye", "турц", "стамбул"))
    china = any(has_term(lowered, term) for term in ("china", "chinese", "китай", "пекин", "шанхай"))

    result: list[str] = []
    if belarus and russia:
        result.extend(["РБ–РФ", "РФ–РБ"])
    if belarus and turkey:
        result.append("РБ–Турция")
    if china:
        result.append("Китай")
    return result or ["Другие"]


def route_for(text: str, directions: list[str], source_country: str) -> str:
    if "РБ–РФ" in directions or "РФ–РБ" in directions:
        return "Беларусь — Россия"
    if "РБ–Турция" in directions:
        return "Беларусь — Турция"
    if "Китай" in directions:
        return "Китай — международные грузовые направления"
    lowered = text.lower()
    for terms, label in REGIONS:
        if any(has_term(lowered, term) for term in terms):
            return label
    source_country = clean(source_country)
    return f"Регион источника: {source_country}" if source_country else "Международные грузовые маршруты"


def date_for(value: str) -> str:
    digits = re.sub(r"\D", "", clean(value))
    if len(digits) >= 8:
        try:
            parsed = datetime.strptime(digits[:8], "%Y%m%d")
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%d.%m.%Y")


def source_name(url: str, domain: str) -> str:
    host = clean(domain) or urlparse(url).netloc
    return re.sub(r"^www\.", "", host, flags=re.I) or "Источник"


def language_group(language: str) -> str | None:
    """Return the balancing bucket used for the 50/50 source mix."""
    lowered = clean(language).lower()
    if lowered.startswith(("russian", "rus", "ru")):
        return "russian"
    if lowered.startswith(("english", "eng", "en")):
        return "foreign"
    return None


def contains_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(has_term(lowered, term) for term in terms)


def domain_bonus(domain: str) -> int:
    domain = domain.lower()
    if domain in LOW_QUALITY_DOMAINS:
        return -25
    if domain in TRUSTED_DOMAINS:
        return TRUSTED_DOMAINS[domain]
    if domain.endswith((".gov", ".gov.by", ".gov.ru", ".gov.cn", ".gov.tr")):
        return 10
    if domain.endswith(("europa.eu", "unece.org", "wto.org")):
        return 10
    return 0


def candidate_score(article: dict) -> int:
    title = clean(article.get("title"))
    rule = rule_for(title)
    score = rule.score if rule else 0
    if contains_any(title, PRIORITY_REGION_TERMS):
        score += 15
    domain = source_name(clean(article.get("url")), clean(article.get("domain")))
    score += domain_bonus(domain)
    if contains_any(title, COMMERCIAL_FREIGHT_TERMS):
        score += 10
    if contains_any(title, PASSENGER_TERMS):
        score -= 35
    if contains_any(title, COMMENTARY_TERMS):
        score -= 20
    return score


def logistics_score(
    text: str,
    rule: Rule,
    transports: list[str],
    directions: list[str],
    domain: str,
) -> int:
    """Estimate operational relevance specifically for commercial freight."""
    score = rule.score
    score += min(len(transports), 2) * 4
    score += domain_bonus(domain)

    if contains_any(text, COMMERCIAL_FREIGHT_TERMS):
        score += 10
    if directions != ["Другие"]:
        score += 12
    if contains_any(text, COMMENTARY_TERMS):
        score -= 22
    if contains_any(text, PASSENGER_TERMS):
        score -= 35

    return max(0, min(100, score))


def article_to_news(article: dict, translator) -> dict | None:
    original_title = clean(article.get("title"))
    language = clean(article.get("language"))
    group = language_group(language)
    if not group:
        return None

    url = clean(article.get("url"))
    domain = source_name(url, clean(article.get("domain")))
    excerpt = article_excerpt(url)
    combined = f"{original_title} {excerpt}"

    # Exclude passenger-only and military-only stories.  They may contain the
    # words border, airport, railway or port but do not describe freight impact.
    has_commercial_context = contains_any(combined, COMMERCIAL_FREIGHT_TERMS)
    if not has_commercial_context:
        return None
    if contains_any(combined, PASSENGER_TERMS) and not contains_any(
        combined,
        (
            "freight", "cargo", "container", "truck", "shipping", "goods",
            "груз", "контейнер", "грузовик", "фур", "товар", "перевозк",
        ),
    ):
        return None
    if contains_any(combined, MILITARY_TERMS) and not contains_any(
        combined,
        (
            "freight", "cargo", "container", "merchant ship", "commercial vessel",
            "truck", "rail freight", "груз", "контейнер", "торговое судно",
            "грузовик", "фур", "вагон", "перевозк",
        ),
    ):
        return None

    rule = rule_for(combined)
    transports = transports_for(combined)
    if not rule or not transports:
        return None

    directions = directions_for(combined)
    score = logistics_score(combined, rule, transports, directions, domain)
    if score < MIN_LOGISTICS_SCORE:
        return None

    title_ru = translate(original_title, language, translator)
    excerpt_ru = translate(excerpt, language, translator) if excerpt else ""
    fact = (excerpt_ru if len(excerpt_ru) >= 45 else title_ru)[:700]
    route = route_for(combined, directions, clean(article.get("sourcecountry")))

    return {
        "date": date_for(clean(article.get("seendate"))),
        "importance": "Высокая" if score >= 88 else "Средняя",
        "importanceScore": score,
        "sourceLanguage": "Русскоязычный" if group == "russian" else "Иностранный",
        "transports": transports,
        "directions": directions,
        "title": title_ru,
        "route": route,
        "fact": fact,
        "cause": rule.cause,
        "effect": rule.effect,
        "sources": [{"name": domain, "url": url}],
        "assessment": f"Алгоритмическая значимость для грузовой логистики: {score}/100",
    }


def build_language_pool(
    articles: list[dict],
    group: str,
    translator,
    accepted_titles: list[str],
) -> list[dict]:
    candidates = [
        article
        for article in articles
        if language_group(clean(article.get("language"))) == group
    ]
    candidates.sort(key=candidate_score, reverse=True)

    pool: list[dict] = []
    per_domain: dict[str, int] = {}
    for article in candidates[:MAX_CANDIDATES_PER_LANGUAGE]:
        if len(pool) >= MAX_NEWS:
            break

        original_title = clean(article.get("title"))
        if is_duplicate(original_title, accepted_titles):
            continue

        url = clean(article.get("url"))
        domain = source_name(url, clean(article.get("domain")))
        if per_domain.get(domain, 0) >= 2:
            continue

        item = article_to_news(article, translator)
        if not item:
            continue

        pool.append(item)
        accepted_titles.append(original_title)
        per_domain[domain] = per_domain.get(domain, 0) + 1

    pool.sort(key=lambda item: item["importanceScore"], reverse=True)
    return pool


def build_feed(articles: list[dict]) -> dict:
    translator = get_translator()
    unique_by_url: dict[str, dict] = {}
    for article in articles:
        url = clean(article.get("url"))
        title = clean(article.get("title"))
        if url.startswith("http") and title:
            unique_by_url[url] = article

    unique_articles = list(unique_by_url.values())
    accepted_titles: list[str] = []
    russian_pool = build_language_pool(
        unique_articles, "russian", translator, accepted_titles
    )
    foreign_pool = build_language_pool(
        unique_articles, "foreign", translator, accepted_titles
    )

    # Strict 50/50: if one group has fewer than six suitable articles, publish
    # the same smaller number from each group instead of distorting the ratio.
    pair_count = min(
        TARGET_PER_LANGUAGE,
        len(russian_pool),
        len(foreign_pool),
    )
    news = russian_pool[:pair_count] + foreign_pool[:pair_count]
    news.sort(
        key=lambda item: (
            0 if item["importance"] == "Высокая" else 1,
            -item["importanceScore"],
        )
    )

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "periodHours": 24,
        "language": "ru",
        "analysisMethod": "rule-based",
        "sourceMix": {
            "target": "50/50",
            "russian": pair_count,
            "foreign": pair_count,
        },
        "notice": (
            "Лента содержит равное количество русскоязычных и иностранных источников. "
            "Перевод выполнен локальной открытой моделью. Важность, причина и последствие — "
            "алгоритмическая оценка; ключевые решения проверяйте по ссылке на источник."
        ),
        "news": news,
    }


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    articles: list[dict] = []
    failures: list[str] = []
    for index, query in enumerate(QUERIES):
        try:
            batch = fetch_gdelt(session, query)
            articles.extend(batch)
            print(f"GDELT query {index + 1}: {len(batch)} articles")
        except Exception as error:
            failures.append(f"query {index + 1}: {error}")
        if index < len(QUERIES) - 1:
            time.sleep(20)

    if not articles:
        print("All GDELT requests failed; existing news.json was preserved.", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    feed = build_feed(articles)
    if not feed["news"]:
        for failure in failures:
            print(failure, file=sys.stderr)
        print(
            "No balanced set of relevant Russian and foreign logistics news was found; "
            "existing news.json was preserved.",
            file=sys.stderr,
        )
        return 1 if failures else 0

    temporary = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_PATH)
    print(f"Saved {len(feed['news'])} news items to {OUTPUT_PATH}")
    if failures:
        print("Partial GDELT failures: " + "; ".join(failures), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
