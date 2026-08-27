#!/usr/bin/env python3
"""Build a Russian logistics-news feed from free Google News RSS searches.

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
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree

import requests
import trafilatura


OUTPUT_PATH = Path(__file__).with_name("news.json")
MAX_NEWS = 12
TARGET_PER_LANGUAGE = MAX_NEWS // 2
MAX_CANDIDATES_PER_LANGUAGE = 100
MIN_LOGISTICS_SCORE = 72
RSS_RETRIES = 3
RSS_RETRY_DELAY = 10
RSS_INTER_FEED_DELAY = 2
NEWS_WINDOW_HOURS = 24
MAX_FUTURE_SKEW_MINUTES = 10

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

DOCUMENT_TERMS_EN = (
    '"transport document" OR "transport documents" OR "electronic transport document" OR '
    '"electronic consignment note" OR "consignment note" OR "e-CMR" OR CMR OR '
    '"bill of lading" OR "electronic bill of lading" OR eBL OR '
    '"air waybill" OR e-AWB OR "rail consignment note" OR CIM OR SMGS OR '
    '"transit declaration" OR "customs declaration" OR "cargo manifest" OR '
    '"transport permit" OR "import certificate" OR "export certificate"'
)

DOCUMENT_TERMS_RU = (
    '"транспортный документ" OR "транспортные документы" OR '
    '"электронные перевозочные документы" OR ЭПД OR '
    '"электронная транспортная накладная" OR "транспортная накладная" OR '
    'е-CMR OR CMR OR коносамент OR "электронный коносамент" OR '
    'авианакладная OR e-AWB OR "железнодорожная накладная" OR СМГС OR ЦИМ OR '
    '"транзитная декларация" OR "таможенная декларация" OR '
    '"грузовой манифест" OR "разрешение на перевозку" OR сертификат'
)

GENERAL_FREIGHT_EN = (
    'freight OR cargo OR shipping OR port OR maritime OR vessel OR container OR canal OR '
    'railway OR railroad OR train OR trucking OR truck OR customs OR "border crossing" OR '
    '"air cargo" OR "air freight"'
)

GENERAL_FREIGHT_RU = (
    'груз OR грузоперевозки OR порт OR судно OR морские перевозки OR контейнер OR '
    'железная дорога OR поезд OR вагон OR грузовик OR фура OR таможня OR '
    '"пункт пропуска" OR авиагруз OR авиаперевозки'
)

RSS_FEEDS = [
    {
        "label": "foreign",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "general",
        "query": (
            f'({GENERAL_FREIGHT_EN}) '
            f'({RISK_TERMS_EN} OR Belarus OR Russia OR Turkey OR China OR '
            'rates OR index OR regulation OR mandatory OR required OR '
            '"service change" OR "blank sailing") when:1d'
        ),
    },
    {
        "label": "russian",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "general",
        "query": (
            f'({GENERAL_FREIGHT_RU}) '
            f'({RISK_TERMS_RU} OR Беларусь OR Россия OR Турция OR Китай OR '
            'ставки OR индекс OR правила OR обязательный OR требования OR '
            '"изменение сервиса" OR "отмена рейса") when:1d'
        ),
    },
    {
        "label": "foreign-carriers-indices",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "profile",
        "query": (
            '(site:drewry.co.uk OR site:balticexchange.com OR site:maersk.com OR '
            'site:msc.com OR site:cma-cgm.com OR site:hapag-lloyd.com OR '
            'site:dpworld.com) '
            '(freight OR cargo OR shipping OR port OR container OR rates OR index) when:1d'
        ),
    },
    {
        "label": "foreign-organizations",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "profile",
        "query": (
            '(site:imo.org OR site:iata.org OR site:iru.org OR site:fiata.org OR '
            'site:wcoomd.org OR site:ec.europa.eu) '
            '(freight OR cargo OR shipping OR customs OR border OR transport) when:1d'
        ),
    },
    {
        "label": "foreign-industry-media",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "profile",
        "query": (
            '(site:theloadstar.com OR site:freightwaves.com OR site:aircargonews.net OR '
            'site:railwaygazette.com OR site:seatrade-maritime.com OR site:splash247.com) '
            '(freight OR cargo OR shipping OR port OR railway OR trucking OR airfreight) when:1d'
        ),
    },
    {
        "label": "russian-official",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "profile",
        "query": (
            '(site:rzd.ru OR site:company.rzd.ru OR site:rw.by OR '
            'site:customs.gov.ru OR site:customs.gov.by OR site:gpk.gov.by OR '
            'site:bamap.org OR site:mintrans.gov.ru OR site:mintrans.gov.by) '
            '(груз OR перевозки OR железная дорога OR таможня OR граница OR документы) when:1d'
        ),
    },
    {
        "label": "russian-industry-media",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "profile",
        "query": (
            '(site:seanews.ru OR site:portnews.ru OR site:morvesti.ru OR '
            'site:logirus.ru OR site:ati.su) '
            '(груз OR перевозки OR порт OR контейнер OR железная дорога OR '
            'таможня OR ставки OR индекс OR документы) when:1d'
        ),
    },
    {
        "label": "foreign-documents",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "documents",
        "query": f'({DOCUMENT_TERMS_EN}) (freight OR cargo OR customs OR logistics) when:1d',
    },
    {
        "label": "russian-documents",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "documents",
        "query": f'({DOCUMENT_TERMS_RU}) (груз OR перевозки OR таможня OR логистика) when:1d',
    },
]

USER_AGENT = "logistics-news-rss/2.0 (+public GitHub Actions feed)"


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
            "hijack", "strike on", "атак", "беспилотник", "бпла", "дрон",
            "ракет", "пират",
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
        (
            "mandatory transport document", "mandatory electronic", "must use e-cmr",
            "required transport document", "document requirement", "documentation requirement",
            "обязательн перевозочн документ", "обязательн транспортн документ",
            "обязательн электронн накладн", "обязательн электронн транспортн накладн",
            "обязательн эпд",
            "новые требования к документ", "изменения в оформлении документ",
        ),
        "Вводятся обязательные требования к перевозочным или таможенным документам.",
        "Нужно обновить оформление и информационные системы; неподготовленные отправки могут задержать или не принять к перевозке.",
        "Высокая",
        84,
    ),
    Rule(
        (
            "transport document", "transport documents", "electronic consignment note",
            "consignment note", "e-cmr", "bill of lading", "electronic bill of lading",
            "air waybill", "e-awb", "rail consignment note", "transit declaration",
            "customs declaration", "cargo manifest", "transport permit",
            "транспортн документ", "перевозочн документ", "электронн накладн", "накладн", "эпд",
            "транспортн накладн", "электронн транспортн накладн",
            "е-cmr", "коносамент", "авианакладн", "железнодорожн накладн",
            "смгс", "цим", "транзитн деклараци", "таможенн деклараци",
            "грузов манифест", "разрешени на перевоз",
        ),
        "Изменяется порядок оформления, обмена или проверки транспортных документов.",
        "Потребуется проверить форму и канал подачи документов; возможны задержки оформления и дополнительные требования к участникам перевозки.",
        "Средняя",
        76,
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
        (
            "freight index", "freight rate", "freight rates", "container index",
            "world container index", "baltic dry index", "bdi ", "air freight index",
            "фрахтов индекс", "индекс фрахт", "контейнерн индекс",
            "мировой контейнерный индекс", "балтийский индекс", "ставки фрахта",
        ),
        "Изменение рыночных ставок или отраслевого индекса перевозок.",
        "Изменяется ориентир стоимости новых бронирований; конкретную ставку нужно перепроверить по маршруту и типу груза.",
        "Средняя",
        66,
    ),
    Rule(
        (
            "blank sailing", "port omission", "service suspension", "service change",
            "schedule change", "route change", "new freight route", "new cargo route",
            "отмена рейса", "пропуск порта", "приостановка сервиса",
            "изменение сервиса", "изменение расписания", "изменение маршрута",
            "новый грузовой маршрут", "запуск грузового маршрута",
        ),
        "Перевозчик или оператор изменяет расписание, сервис либо маршрут.",
        "Нужно проверить доступную ёмкость и новое расписание; возможны перенос отправки, изменение транзитного времени и стоимости.",
        "Средняя",
        72,
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
    "Авто": ("truck", "trucking", "lorry", "road freight", "highway", "border crossing", "e-cmr", "road consignment note", "грузовик", "грузовой автомобил", "грузовые автомобил", "грузовых автомобил", "грузовой транспорт", "автоперевоз", "фур", "автомобильн", "транспортная накладная", "транспортн накладн", "е-cmr"),
    "Ж/д": ("rail", "railway", "railroad", "train", "wagon", "derail", "rail consignment note", "smgs", "cim", "железнодорож", "поезд", "вагон", "ржд", "железнодорожная накладная", "смгс", "цим"),
    "Море": ("port", "ship", "shipping", "vessel", "maritime", "tanker", "container ship", "canal", "strait", "sea ", "bill of lading", "ebl", "порт", "судн", "морск", "танкер", "контейнеровоз", "канал", "пролив", "коносамент"),
    "Авиа": ("air cargo", "air freight", "airport", "airline", "flight", "air waybill", "e-awb", "авиагруз", "авиаперевоз", "аэропорт", "авиакомпан", "рейс", "авианакладн"),
}

GENERIC_DOCUMENT_TERMS = (
    "transport document", "electronic transport document", "transport permit",
    "customs declaration", "transit declaration", "cargo manifest",
    "транспортн документ", "перевозочн документ", "транспортн накладн", "эпд",
    "таможенн деклараци", "транзитн деклараци", "грузов манифест",
    "разрешени на перевоз",
)

# At least one explicit commercial-freight term is required.  This prevents
# passenger tourism, baggage and general political stories from entering the
# feed merely because they mention a border, airport or port.
COMMERCIAL_FREIGHT_TERMS = (
    "freight", "cargo", "container", "merchant ship", "commercial vessel",
    "shipping", "tanker", "terminal", "trucking", "road freight",
    "rail freight", "freight train", "wagon", "goods", "consignment",
    "air cargo", "air freight", "warehouse", "port operations",
    "груз", "контейнер", "торговое судно", "судоход", "танкер", "терминал",
    "грузоперевоз", "вагон", "товар", "отправк", "авиагруз", "склад",
    "портов", "перевозк", "transport document", "consignment note",
    "bill of lading", "air waybill", "e-cmr", "e-awb", "smgs",
    "транспортн документ", "перевозочн документ", "транспортн накладн", "накладн", "эпд",
    "коносамент", "авианакладн", "смгс", "транзитн деклараци",
)

PASSENGER_TERMS = (
    "tourist", "tourism", "passenger", "baggage", "luggage", "vacation",
    "holidaymaker", "турист", "пассажир", "багаж", "отпуск", "путешеств",
)

MILITARY_TERMS = (
    "military", "weapon", "ammunition", "troops", "battlefield", "frontline",
    "военн", "оруж", "боеприпас", "войск", "фронт", "всу", "ракет",
)

CRIME_AND_SEIZURE_TERMS = (
    "cocaine", "heroin", "methamphetamine", "marijuana", "cannabis",
    "narcotic", "narcotics", "drug bust", "drug seizure", "drug trafficking",
    "smuggling", "smuggler", "contraband", "cartel", "seized drugs",
    "кокаин", "героин", "метамфетамин", "марихуан", "каннабис",
    "наркотик", "наркоторгов", "контрабанд", "тайник", "изъяли наркот",
    "изъят наркот", "партия наркот", "перевозил наркот",
)

PERSONAL_INCIDENT_TERMS = (
    "driver killed", "driver injured", "motorist", "fatal crash",
    "people killed", "people injured", "car crash", "bus crash",
    "road accident", "traffic accident", "truck crash",
    "водитель погиб", "погиб водитель", "водитель пострадал", "пострадал водитель",
    "погиб человек", "погибли люди", "погиб", "пострадал человек",
    "пострадали люди", "пострадал", "дтп", "авария с грузовиком",
    "легковой автомобил", "автобус", "частное лицо", "уголовное дело",
)

# Military and private-incident stories are allowed only when the text states
# a direct operational effect on commercial freight infrastructure or routes.
DIRECT_LOGISTICS_ASSET_TERMS = (
    "freight route", "shipping route", "trade route", "logistics corridor",
    "cargo terminal", "freight terminal", "port operations", "port traffic",
    "rail infrastructure", "rail freight", "freight train", "commercial vessel",
    "merchant ship", "container ship", "border crossing", "cargo airport",
    "motorway", "highway", "major road",
    "грузовой маршрут", "судоходный маршрут", "торговый маршрут",
    "транспортный коридор", "грузовой терминал", "работа порта",
    "движение судов", "железнодорожная инфраструктура", "грузовой поезд",
    "торговое судно", "контейнеровоз", "пункт пропуска", "грузовой аэропорт",
    "автомагистраль", "федеральная дорога", "трасса", "грузоперевозки",
)

DIRECT_OPERATIONAL_IMPACT_TERMS = (
    "closed", "closure", "suspended", "halted", "stopped", "blocked", "shutdown",
    "disrupted", "damaged", "destroyed", "reroute", "rerouted", "diverted",
    "delay", "delays", "restriction", "restrictions", "outage", "attack on",
    "strike on", "traffic stopped", "operations stopped",
    "закрыт", "закрытие", "приостанов", "останов", "перекрыт",
    "заблокирован", "нарушена работа", "поврежд", "разруш", "перенаправ",
    "задерж", "огранич", "атакован", "удар по", "обстрел", "движение прекращено",
)

NO_OPERATIONAL_IMPACT_TERMS = (
    "no disruption", "no operational impact", "not affected", "remained open",
    "without restrictions", "traffic is open", "operations continue normally",
    "нет влияния", "не повлиял", "не повлияло", "не нарушен", "не нарушена",
    "не огранич", "без ограничений", "движение открыто", "работает штатно",
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
    "iru.org": 12,
    "fiata.org": 10,
    "wcoomd.org": 12,
    "maersk.com": 10,
    "msc.com": 10,
    "cma-cgm.com": 10,
    "hapag-lloyd.com": 10,
    "drewry.co.uk": 12,
    "balticexchange.com": 12,
    "theloadstar.com": 8,
    "freightwaves.com": 8,
    "aircargonews.net": 8,
    "railwaygazette.com": 8,
    "seatrade-maritime.com": 8,
    "splash247.com": 8,
    "rzd.ru": 10,
    "company.rzd.ru": 12,
    "rw.by": 12,
    "customs.gov.ru": 10,
    "customs.gov.by": 12,
    "gpk.gov.by": 10,
    "bamap.org": 8,
    "portnews.ru": 8,
    "seanews.ru": 8,
    "morvesti.ru": 8,
    "logirus.ru": 8,
    "ati.su": 6,
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
    if normalized == "порт":
        # Do not treat the ending of «экспорт» as the word «порт».
        return re.search(
            r"(?<![а-яё])порт(?:а|у|е|ы|ов|ом|ами|ах|овый|овая|овые|овую|ового)?(?![а-яё])",
            lowered_text,
        ) is not None
    if normalized == "груз":
        # «Груз» and its cargo-related forms, but not «грузовик»: a truck can
        # appear in an ordinary road accident unrelated to freight logistics.
        return re.search(
            r"(?<![а-яё])груз(?:а|ы|ов|ом|ами|ах|овой|овая|овое|овые|ового|овую|овым|овыми)?(?![а-яё])",
            lowered_text,
        ) is not None
    return normalized in lowered_text


def parse_article_datetime(value: str) -> datetime | None:
    """Parse an RSS or ISO timestamp and normalize it to UTC."""
    cleaned = clean(value)
    if not cleaned:
        return None

    try:
        parsed = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_recent_article(value: str, now: datetime | None = None) -> bool:
    """Return True only for timestamps inside the strict rolling 24h window."""
    published_at = parse_article_datetime(value)
    if published_at is None:
        return False

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)

    oldest_allowed = reference - timedelta(hours=NEWS_WINDOW_HOURS)
    newest_allowed = reference + timedelta(minutes=MAX_FUTURE_SKEW_MINUTES)
    return oldest_allowed <= published_at <= newest_allowed


def fetch_google_news_rss(session: requests.Session, feed: dict) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urlencode(
        {
            "q": feed["query"],
            "hl": feed["hl"],
            "gl": feed["gl"],
            "ceid": feed["ceid"],
        }
    )
    last_error: Exception | None = None

    for attempt in range(1, RSS_RETRIES + 1):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            articles: list[dict] = []
            fetched_at = datetime.now(timezone.utc)

            for item in root.findall("./channel/item"):
                title = clean(item.findtext("title"))
                link = clean(item.findtext("link"))
                published_raw = clean(item.findtext("pubDate") or "")
                published_at = parse_article_datetime(published_raw)
                source_element = item.find("source")
                source = clean(source_element.text if source_element is not None else "")
                source_url = clean(
                    source_element.get("url") if source_element is not None else ""
                )

                if source and title.endswith(" - " + source):
                    title = title[: -(len(source) + 3)].strip()
                if not title or not link.startswith("http"):
                    continue
                if published_at is None or not is_recent_article(
                    published_at.isoformat(), fetched_at
                ):
                    continue

                articles.append(
                    {
                        "title": title,
                        "url": link,
                        "domain": urlparse(source_url).netloc or source,
                        "language": feed["language"],
                        "sourcecountry": "",
                        "seendate": published_at.isoformat(),
                        "excerpt": title,
                        "sourceType": feed.get("sourceType", "general"),
                        "feedLabel": feed.get("label", "rss"),
                    }
                )

            return articles
        except Exception as error:
            last_error = error
            if attempt == RSS_RETRIES:
                break
            delay = RSS_RETRY_DELAY * attempt
            print(
                f"RSS retry {attempt}/{RSS_RETRIES - 1} in {delay}s: {error}",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(f"RSS request failed after {RSS_RETRIES} attempts: {last_error}")


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
    if not result and contains_any(lowered, GENERIC_DOCUMENT_TERMS):
        # A generic multimodal document change can affect every mode.  More
        # specific CMR/SMGS/eBL/e-AWB terms are assigned above to one mode.
        return ["Авто", "Ж/д", "Море", "Авиа"]
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
    for trusted_domain, bonus in TRUSTED_DOMAINS.items():
        if domain == trusted_domain or domain.endswith("." + trusted_domain):
            return bonus
    if domain.endswith((".gov", ".gov.by", ".gov.ru", ".gov.cn", ".gov.tr")):
        return 10
    if domain.endswith(("europa.eu", "unece.org", "wto.org")):
        return 10
    return 0


def candidate_score(article: dict) -> int:
    title = clean(article.get("title"))
    if contains_any(title, CRIME_AND_SEIZURE_TERMS):
        return -1000
    if contains_any(title, PASSENGER_TERMS):
        return -1000
    if contains_any(title, PERSONAL_INCIDENT_TERMS) and not (
        contains_any(title, DIRECT_LOGISTICS_ASSET_TERMS)
        and contains_any(title, DIRECT_OPERATIONAL_IMPACT_TERMS)
    ):
        return -1000
    if contains_any(title, MILITARY_TERMS) and not (
        contains_any(title, DIRECT_LOGISTICS_ASSET_TERMS)
        and contains_any(title, DIRECT_OPERATIONAL_IMPACT_TERMS)
    ):
        return -1000

    rule = rule_for(title)
    score = rule.score if rule else 0
    if contains_any(title, PRIORITY_REGION_TERMS):
        score += 15
    domain = source_name(clean(article.get("url")), clean(article.get("domain")))
    score += domain_bonus(domain)
    if article.get("sourceType") == "profile":
        score += 8
    elif article.get("sourceType") == "documents":
        score += 10
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


def concrete_cause(title: str, fact: str) -> str:
    """Use the article's specific event instead of a generic rule label."""
    candidate = clean(title)
    if len(candidate) < 20:
        candidate = clean(fact)
    candidate = re.split(r"(?<=[.!?])\s+", candidate, maxsplit=1)[0]
    candidate = candidate[:280].rstrip(" ,;:-")
    if candidate and candidate[-1] not in ".!?":
        candidate += "."
    return candidate


def transport_scope(transports: list[str]) -> str:
    labels = {
        "Авто": "автоперевозок",
        "Ж/д": "железнодорожных перевозок",
        "Море": "морских перевозок",
        "Авиа": "авиаперевозок",
    }
    if len(transports) == 1:
        return labels.get(transports[0], "грузовых перевозок")
    return "грузовых перевозок (" + ", ".join(transports).lower() + ")"


def concrete_effect(
    rule: Rule,
    text: str,
    transports: list[str],
    route: str,
) -> str:
    """Describe the operational effect for this transport and route."""
    scope = f"Для {transport_scope(transports)} по направлению «{route}»"
    category = rule.cause.lower()

    if "документ" in category:
        return (
            f"{scope} нужно проверить новые формы и порядок подачи документов; "
            "ошибки могут задержать оформление или приём груза."
        )
    if "санкцион" in category:
        return (
            f"{scope} нужно повторно проверить груз, перевозчика, получателя и расчёты; "
            "возможны отказ в перевозке или смена маршрута."
        )
    if "тариф" in category or "рыночных ставок" in category:
        return (
            f"{scope} необходимо пересчитать стоимость новых отправок и проверить "
            "актуальную ставку у перевозчика."
        )
    if "закрытие" in category:
        return (
            f"{scope} возможны перенаправление груза, очередь и увеличение срока "
            "и стоимости доставки."
        )
    if "забастов" in category:
        return (
            f"{scope} снизится пропускная способность; возможны простой, перенос "
            "обработки и дополнительные расходы."
        )
    if "авария" in category:
        return (
            f"{scope} возможны временное ограничение участка, задержка и перенос "
            "груза на альтернативный маршрут."
        )
    if "погод" in category:
        return (
            f"{scope} возможны ограничения движения или обработки, пропуск рейсов "
            "и увеличение транзитного времени."
        )
    if "пограничного" in category or "таможенного" in category:
        return (
            f"{scope} может увеличиться время оформления; нужно проверить документы, "
            "ограничения по грузу и доступность перехода."
        )
    if "перегрузка инфраструктуры" in category:
        return (
            f"{scope} вероятны ожидание свободного слота, простой и дополнительные "
            "расходы на хранение."
        )
    if "расписание" in category or "маршрут" in category:
        return (
            f"{scope} нужно проверить новое расписание и доступную ёмкость; возможны "
            "перенос отправки и изменение срока доставки."
        )
    if "безопасност" in category:
        return (
            f"{scope} возможны приостановка операций, обход участка, рост страховых "
            "надбавок и срока доставки."
        )
    return (
        f"{scope} возможны задержка, изменение маршрута и дополнительные расходы."
    )


def article_to_news(article: dict, translator) -> dict | None:
    original_title = clean(article.get("title"))
    language = clean(article.get("language"))
    group = language_group(language)
    if not group:
        return None

    url = clean(article.get("url"))
    domain = source_name(url, clean(article.get("domain")))
    excerpt = clean(article.get("excerpt"))
    if len(excerpt) < 45:
        excerpt = article_excerpt(url)
    combined = f"{original_title} {excerpt}"

    rule = rule_for(combined)
    transports = transports_for(combined)

    # Crime, drugs, baggage and tourism are outside the business-news feed even
    # when they mention a container, customs office, port or airport.
    if contains_any(combined, CRIME_AND_SEIZURE_TERMS):
        return None
    if contains_any(combined, PASSENGER_TERMS):
        return None

    has_direct_network_impact = (
        contains_any(combined, DIRECT_LOGISTICS_ASSET_TERMS)
        and contains_any(combined, DIRECT_OPERATIONAL_IMPACT_TERMS)
        and not contains_any(combined, NO_OPERATIONAL_IMPACT_TERMS)
    )
    if contains_any(combined, PERSONAL_INCIDENT_TERMS) and not has_direct_network_impact:
        return None
    if contains_any(combined, MILITARY_TERMS) and not has_direct_network_impact:
        return None

    has_commercial_context = contains_any(combined, COMMERCIAL_FREIGHT_TERMS)
    is_profile_source = article.get("sourceType") == "profile"
    if not has_commercial_context and not (
        is_profile_source and rule is not None and transports
    ):
        return None
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
    cause = concrete_cause(title_ru, fact)
    effect = concrete_effect(rule, combined, transports, route)

    return {
        "date": date_for(clean(article.get("seendate"))),
        "importance": (
            "Высокая"
            if rule.importance == "Высокая"
            or (rule.score >= 72 and directions != ["Другие"] and score >= 92)
            else "Средняя"
        ),
        "importanceScore": score,
        "sourceLanguage": "Русскоязычный" if group == "russian" else "Иностранный",
        "transports": transports,
        "directions": directions,
        "title": title_ru,
        "route": route,
        "fact": fact,
        "cause": cause,
        "effect": effect,
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
    now = datetime.now(timezone.utc)
    unique_by_url: dict[str, dict] = {}
    for article in articles:
        url = clean(article.get("url"))
        title = clean(article.get("title"))
        if (
            url.startswith("http")
            and title
            and is_recent_article(clean(article.get("seendate")), now)
        ):
            unique_by_url[url] = article

    unique_articles = list(unique_by_url.values())
    raw_russian = sum(
        1 for article in unique_articles
        if language_group(clean(article.get("language"))) == "russian"
    )
    raw_foreign = sum(
        1 for article in unique_articles
        if language_group(clean(article.get("language"))) == "foreign"
    )
    accepted_titles: list[str] = []
    russian_pool = build_language_pool(
        unique_articles, "russian", translator, accepted_titles
    )
    foreign_pool = build_language_pool(
        unique_articles, "foreign", translator, accepted_titles
    )

    # Aim for 50/50 first. If one language group has too few suitable articles,
    # fill the remaining slots with the strongest unused articles from either
    # group instead of shrinking the whole feed to the smaller pool.
    pair_count = min(
        TARGET_PER_LANGUAGE,
        len(russian_pool),
        len(foreign_pool),
    )
    news = russian_pool[:pair_count] + foreign_pool[:pair_count]

    remaining_candidates = (
        russian_pool[pair_count:]
        + foreign_pool[pair_count:]
    )
    remaining_candidates.sort(
        key=lambda item: item["importanceScore"],
        reverse=True,
    )
    news.extend(
        remaining_candidates[
            : max(0, MAX_NEWS - len(news))
        ]
    )

    selected_russian = sum(
        1
        for item in news
        if item["sourceLanguage"] == "Русскоязычный"
    )
    selected_foreign = sum(
        1
        for item in news
        if item["sourceLanguage"] == "Иностранный"
    )
    print(
        "Filter summary: "
        f"fresh unique russian={raw_russian}, foreign={raw_foreign}; "
        f"relevant russian={len(russian_pool)}, foreign={len(foreign_pool)}; "
        f"selected russian={selected_russian}, foreign={selected_foreign}"
    )
    news.sort(
        key=lambda item: (
            0 if item["importance"] == "Высокая" else 1,
            -item["importanceScore"],
        )
    )

    return {
        "updatedAt": now.isoformat(),
        "periodHours": NEWS_WINDOW_HOURS,
        "language": "ru",
        "analysisMethod": "rule-based",
        "sourceMix": {
            "target": "50/50 when available",
            "russian": selected_russian,
            "foreign": selected_foreign,
        },
        "notice": (
            "Целевой баланс ленты — 50/50 русскоязычных и иностранных источников. "
            "Если в одной группе недостаточно значимых свежих публикаций, свободные "
            "места заполняются лучшими новостями из другой группы. "
            "Новости получены из общего RSS-поиска Google News и дополнительных "
            "поисков по официальным и отраслевым логистическим сайтам. Перевод выполнен "
            "локальной открытой моделью. Важность, причина и последствие — "
            "алгоритмическая оценка; ключевые решения проверяйте по ссылке на источник."
        ),
        "news": news,
    }


def empty_feed(notice: str) -> dict:
    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "periodHours": NEWS_WINDOW_HOURS,
        "language": "ru",
        "analysisMethod": "rule-based",
        "sourceMix": {
            "target": "50/50 when available",
            "russian": 0,
            "foreign": 0,
        },
        "notice": notice,
        "news": [],
    }


def write_feed(feed: dict) -> None:
    temporary = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_PATH)


def main() -> int:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
    )

    articles: list[dict] = []
    failures: list[str] = []
    for index, feed in enumerate(RSS_FEEDS):
        try:
            batch = fetch_google_news_rss(session, feed)
            articles.extend(batch)
            print(f"RSS {feed['label']}: {len(batch)} articles")
        except Exception as error:
            failures.append(f"RSS {feed['label']}: {error}")
        if index < len(RSS_FEEDS) - 1:
            time.sleep(RSS_INTER_FEED_DELAY)

    if not articles:
        for failure in failures:
            print(failure, file=sys.stderr)
        write_feed(
            empty_feed(
                "За последние 24 часа свежие публикации не получены. "
                "Старые новости не показываются."
            )
        )
        print("Saved an empty fresh feed; stale news was removed.")
        return 0

    feed = build_feed(articles)
    if not feed["news"]:
        for failure in failures:
            print(failure, file=sys.stderr)
        feed = empty_feed(
            "За последние 24 часа не найдено значимых свежих новостей, "
            "прошедших проверку на влияние на грузовую логистику. "
            "Старые новости не показываются."
        )
        write_feed(feed)
        print("Saved an empty fresh feed; stale news was removed.")
        return 0

    write_feed(feed)
    print(f"Saved {len(feed['news'])} news items to {OUTPUT_PATH}")
    if failures:
        print("Partial RSS failures: " + "; ".join(failures), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
