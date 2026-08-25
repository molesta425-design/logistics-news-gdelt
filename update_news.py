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

RISK_TERMS = (
    "closure OR closed OR strike OR tariff OR sanction OR congestion OR "
    "disruption OR attack OR accident OR storm OR restriction OR delay OR "
    "surcharge OR ban OR derailment OR collision OR piracy OR flood OR drought"
)

QUERIES = [
    (
        '(freight OR cargo OR shipping OR port OR maritime OR vessel OR canal) '
        f'({RISK_TERMS}) sourcelang:english'
    ),
    (
        '(railway OR railroad OR train OR trucking OR truck OR customs OR '
        '"border crossing" OR "air cargo" OR airport) '
        f'({RISK_TERMS}) sourcelang:english'
    ),
    (
        '(Belarus OR Russia OR Turkey OR China) '
        '(freight OR cargo OR shipping OR port OR railway OR trucking OR customs OR border) '
        'sourcelang:english'
    ),
    (
        '(Belarus OR Russia OR Turkey OR China) '
        '(freight OR cargo OR shipping OR port OR railway OR trucking OR customs OR border) '
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
        ("sanction", "export control", "import ban", "trade ban", "санкц", "запрет на импорт", "запрет на экспорт"),
        "Изменение санкционных или внешнеторговых ограничений.",
        "Нужно повторно проверить допустимость груза, перевозчика и расчётов; возможны отказ в перевозке и смена маршрута.",
        "Высокая",
        95,
    ),
    Rule(
        ("strike", "walkout", "work stoppage", "labor action", "забастов", "стачк"),
        "Забастовка или иное ограничение работы персонала.",
        "Снижается пропускная способность; вероятны очереди, отмены операций и дополнительный простой транспорта.",
        "Высокая",
        90,
    ),
    Rule(
        ("closed", "closure", "suspend", "shutdown", "blockade", "закрыт", "приостанов", "перекрыт", "блокад"),
        "Закрытие или временное ограничение работы маршрута, перехода либо терминала.",
        "Грузы потребуется перенаправлять; вероятны очереди, увеличение пробега, сроков и стоимости доставки.",
        "Высокая",
        88,
    ),
    Rule(
        ("derail", "collision", "crash", "sank", "sunk", "fire", "explosion", "accident", "авари", "столкнов", "крушен", "пожар", "взрыв", "затон"),
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
        ("congestion", "backlog", "queue", "overload", "перегруз", "очеред", "скоплен"),
        "Перегрузка инфраструктуры и накопление необработанных грузов.",
        "Вероятны ожидание свободного слота, простой транспорта и рост расходов на хранение и демередж.",
        "Средняя",
        70,
    ),
    Rule(
        ("tariff", "surcharge", "fee", "toll", "rate increase", "тариф", "надбавк", "сбор", "платон", "ставк"),
        "Изменение тарифа, сбора или коммерческой надбавки.",
        "Стоимость перевозки изменится; действующие расчёты и предложения клиентам требуется пересчитать.",
        "Средняя",
        68,
    ),
    Rule(
        ("delay", "disruption", "restriction", "divert", "reroute", "задерж", "сбой", "огранич", "перенаправ", "изменение маршрута"),
        "Операционные ограничения или изменение маршрута.",
        "Возможны увеличение срока доставки, дополнительный пробег, перегрузка и рост стоимости.",
        "Средняя",
        60,
    ),
]

TRANSPORT_TERMS = {
    "Авто": ("truck", "trucking", "lorry", "road freight", "highway", "border crossing", "грузовик", "автоперевоз", "фур", "автомобильн"),
    "Ж/д": ("rail", "railway", "railroad", "train", "wagon", "derail", "железнодорож", "поезд", "вагон", "ржд"),
    "Море": ("port", "ship", "shipping", "vessel", "maritime", "tanker", "container ship", "canal", "strait", "sea ", "порт", "судн", "морск", "танкер", "контейнеровоз", "канал", "пролив"),
    "Авиа": ("air cargo", "air freight", "airport", "airline", "flight", "авиагруз", "авиаперевоз", "аэропорт", "авиакомпан", "рейс"),
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
    response = session.get(
        API_URL,
        params={
            "query": query,
            "mode": "artlist",
            "maxrecords": 75,
            "format": "json",
            "sort": "datedesc",
            "timespan": "24h",
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("articles", []) if isinstance(payload, dict) else []


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


def candidate_score(article: dict) -> int:
    title = clean(article.get("title"))
    rule = rule_for(title)
    score = rule.score if rule else 0
    lowered = title.lower()
    if any(has_term(lowered, term) for term in ("belarus", "russia", "turkey", "china", "беларус", "росси", "турц", "китай")):
        score += 15
    return score


def build_feed(articles: list[dict]) -> dict:
    translator = get_translator()
    unique_by_url: dict[str, dict] = {}
    for article in articles:
        url = clean(article.get("url"))
        title = clean(article.get("title"))
        if url.startswith("http") and title:
            unique_by_url[url] = article

    ranked = sorted(unique_by_url.values(), key=candidate_score, reverse=True)
    news: list[dict] = []
    accepted_titles: list[str] = []
    per_domain: dict[str, int] = {}

    for article in ranked:
        if len(news) >= MAX_NEWS:
            break

        original_title = clean(article.get("title"))
        language = clean(article.get("language")) or "English"
        if not language.lower().startswith(("english", "eng", "en", "russian", "rus", "ru")):
            continue
        if is_duplicate(original_title, accepted_titles):
            continue

        url = clean(article.get("url"))
        domain = source_name(url, clean(article.get("domain")))
        if per_domain.get(domain, 0) >= 2:
            continue

        excerpt = article_excerpt(url)
        combined = f"{original_title} {excerpt}"
        rule = rule_for(combined)
        if not rule:
            continue

        title_ru = translate(original_title, language, translator)
        excerpt_ru = translate(excerpt, language, translator) if excerpt else ""
        fact = excerpt_ru if len(excerpt_ru) >= 45 else title_ru
        fact = fact[:700]

        directions = directions_for(combined)
        transports = transports_for(combined)
        if not transports:
            continue
        route = route_for(combined, directions, clean(article.get("sourcecountry")))

        news.append(
            {
                "date": date_for(clean(article.get("seendate"))),
                "importance": rule.importance,
                "transports": transports,
                "directions": directions,
                "title": title_ru,
                "route": route,
                "fact": fact,
                "cause": rule.cause,
                "effect": rule.effect,
                "sources": [{"name": domain, "url": url}],
                "assessment": "Правило: ключевые слова и тип события",
            }
        )
        accepted_titles.append(original_title)
        per_domain[domain] = per_domain.get(domain, 0) + 1

    # Python sort is stable, so the relevance order inside each importance
    # group remains the same as in the ranked candidate list.
    news.sort(key=lambda item: 0 if item["importance"] == "Высокая" else 1)

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "periodHours": 24,
        "language": "ru",
        "analysisMethod": "rule-based",
        "notice": (
            "Перевод выполнен локальной открытой моделью. Причина и последствие — "
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
            time.sleep(6)

    if not articles:
        print("All GDELT requests failed; existing news.json was preserved.", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    feed = build_feed(articles)
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
