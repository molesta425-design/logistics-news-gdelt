#!/usr/bin/env python3
"""Build a Russian logistics-news feed from free Google News RSS searches.

The script deliberately uses no paid API. Translation is performed locally
with Argos Translate. Summary and cause are extracted from the publication;
the logistics consequence and importance are rule-based assessments.
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
UNKNOWN_CAUSE = "Причина в публикации не указана."

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

CARRIER_UPDATE_TERMS_EN = (
    '"customer advisory" OR "customer advisories" OR "operational update" OR '
    '"operational updates" OR "operations update" OR "service update" OR '
    '"service updates" OR "service change" OR "booking suspension" OR '
    '"bookings suspended" OR "blank sailing" OR "port omission" OR rerouting OR '
    'diversion OR surcharge OR "war risk" OR "local information" OR schedule OR '
    'documentation OR "dangerous goods"'
)

CARRIER_UPDATE_TERMS_RU = (
    '"уведомление клиентам" OR "оперативная информация" OR "изменение сервиса" OR '
    '"изменение маршрута" OR "изменение расписания" OR "приостановка бронирований" OR '
    '"приостановка перевозок" OR "пропуск порта" OR перенаправление OR надбавка OR '
    '"военный риск" OR документы OR "опасные грузы"'
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
        "label": "foreign-indices-terminals",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "profile",
        "query": (
            '(site:drewry.co.uk OR site:balticexchange.com OR site:dpworld.com) '
            '(freight OR cargo OR shipping OR port OR container OR rates OR index) when:1d'
        ),
    },
    {
        "label": "official-carriers-global-a",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "carrier",
        "query": (
            '(site:msc.com OR site:maersk.com OR site:cma-cgm.com OR '
            'site:hapag-lloyd.com) '
            f'({CARRIER_UPDATE_TERMS_EN}) when:1d'
        ),
    },
    {
        "label": "official-carriers-global-b",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "carrier",
        "query": (
            '(site:lines.coscoshipping.com OR site:oocl.com OR site:one-line.com OR '
            'site:evergreen-marine.com) '
            f'({CARRIER_UPDATE_TERMS_EN}) when:1d'
        ),
    },
    {
        "label": "official-carriers-global-c",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "carrier",
        "query": (
            '(site:hmm21.com OR site:yangming.com OR site:zim.com OR '
            'site:wanhai.com OR site:pilship.com) '
            f'({CARRIER_UPDATE_TERMS_EN}) when:1d'
        ),
    },
    {
        "label": "official-carriers-russia-turkey-china",
        "language": "English",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
        "sourceType": "carrier",
        "query": (
            '(site:fesco.ru OR site:arkasline.com.tr OR site:turkon.com OR '
            'site:akkonlines.com OR site:sitc.com) '
            f'({CARRIER_UPDATE_TERMS_EN} OR {CARRIER_UPDATE_TERMS_RU}) when:1d'
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
            'site:logirus.ru OR site:infranews.ru OR site:rzd-partner.ru OR '
            'site:ati.su) '
            '(груз OR перевозки OR порт OR контейнер OR железная дорога OR '
            'таможня OR ставки OR индекс OR документы) when:1d'
        ),
    },
    {
        "label": "russian-customs-tks",
        "language": "Russian",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
        "sourceType": "documents",
        "query": (
            'site:tks.ru ("таможенное законодательство" OR "таможенное оформление" OR '
            'пошлина OR тариф OR декларация OR "ТН ВЭД" OR "электронные документы" OR '
            '"транспортные документы") -кокаин -наркотики -багаж -контрабанда when:1d'
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
            "customer advisory", "customer advisories", "operational update",
            "operational updates", "operations update", "service update",
            "service updates", "booking suspension", "bookings suspended",
            "blank sailing", "port omission", "service suspension", "service change",
            "schedule change", "route change", "new freight route", "new cargo route",
            "уведомление клиентам", "оперативная информация",
            "приостановка бронирований", "бронирования приостановлены",
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
    "Море": (
        "port", "ship", "shipping", "vessel", "maritime", "tanker",
        "container ship", "container carrier", "container line", "shipping line",
        "canal", "strait", "sea ", "bill of lading", "ebl",
        "msc", "maersk", "cma cgm", "cma-cgm", "hapag-lloyd", "hapag lloyd",
        "cosco", "oocl", "ocean network express", "one line", "evergreen marine",
        "hmm", "yang ming", "zim", "wan hai", "pil", "fesco", "феско",
        "arkas", "turkon", "akkon", "sitc",
        "порт", "судн", "морск", "танкер", "контейнеровоз",
        "контейнерн перевозчик", "контейнерн лини", "судоходн компани",
        "морск лини", "канал", "пролив", "коносамент",
    ),
    "Авиа": ("air cargo", "air freight", "airport", "airline", "flight", "air waybill", "e-awb", "авиагруз", "авиаперевоз", "аэропорт", "авиакомпан", "авиарейс", "авианакладн"),
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
    "war", "military", "weapon", "ammunition", "troops", "battlefield",
    "frontline", "drone attack", "missile attack", "naval blockade",
    "войн", "военн", "оруж", "боеприпас", "войск", "фронт", "всу",
    "ракет", "дрон", "беспилот", "морская блокада",
)

SPECULATIVE_WAR_COMMENTARY_TERMS = (
    "stalemate", "endgame", "war outlook", "war scenario", "could last",
    "may last", "predicts", "prediction", "opinion", "interview",
    "патовой", "тупиков", "сценари", "прогноз", "по мнению", "считает",
    "может продлиться", "будет длиться", "приближается к",
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
    "maersk.com": 18,
    "msc.com": 18,
    "cma-cgm.com": 18,
    "hapag-lloyd.com": 18,
    "coscoshipping.com": 18,
    "oocl.com": 18,
    "one-line.com": 18,
    "evergreen-marine.com": 18,
    "hmm21.com": 18,
    "yangming.com": 18,
    "zim.com": 18,
    "wanhai.com": 16,
    "pilship.com": 16,
    "fesco.ru": 18,
    "arkasline.com.tr": 18,
    "turkon.com": 18,
    "akkonlines.com": 18,
    "sitc.com": 16,
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
    "infranews.ru": 8,
    "tks.ru": 10,
    "rzd-partner.ru": 10,
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

# A concrete port, waterway or country is more useful in a card than a broad
# continent.  These labels are checked before the generic regional list.
SPECIFIC_ROUTES = [
    (("novorossiysk", "новороссийск"), "порт Новороссийск — Чёрное море"),
    (("rhine", "рейн"), "Рейн — Германия — порты ARA"),
    (("danube", "дунай"), "Дунай — Центральная и Юго-Восточная Европа"),
    (("jebel ali", "jebel-ali", "джебель-али", "джебель али"), "порт Джебель-Али — Персидский залив"),
    (("sri lanka", "шри-ланк", "шри ланк"), "Шри-Ланка — Индийский океан"),
    (("vanuatu", "вануату"), "порты Вануату — Тихий океан"),
    (("persian gulf", "персидск"), "Персидский залив"),
    (("iranian port", "ports of iran", "иранские порт", "порты ирана"), "Иран — Персидский залив"),
]


EVENT_GEOGRAPHY_TERMS = (
    ("Беларусь", ("belarus", "belarusian", "беларус", "минск", "брест")),
    ("Россия", ("russia", "russian", "росси", "москва", "ржд")),
    ("Турция", ("turkey", "turkish", "türkiye", "турц", "стамбул")),
    ("Китай", ("china", "chinese", "китай", "пекин", "шанхай")),
    (
        "Германия",
        (
            "germany", "german", "deutschland", "deutsche", "герман",
            "vda", "bundesbank", "bmv", "rhine", "рейн", "дуйсбург",
            "кёльн", "кауб",
        ),
    ),
    ("Австрия", ("austria", "austrian", "австри", "vienna", "вена")),
    ("Польша", ("poland", "polish", "польш", "варшав")),
    ("Казахстан", ("kazakhstan", "kazakh", "казахстан", "астана", "алматы")),
    ("Нидерланды", ("netherlands", "dutch", "нидерланд", "роттердам")),
    ("Бельгия", ("belgium", "belgian", "бельги", "антверпен")),
    ("Франция", ("france", "french", "франц", "марсель")),
    ("Италия", ("italy", "italian", "итали", "генуя", "триест")),
    ("Испания", ("spain", "spanish", "испани", "валенсия", "барселона")),
    ("Великобритания", ("united kingdom", "britain", "british", "великобрит", "лондон")),
    ("США", ("united states", "u.s.", "usa", "американ", "сша")),
    ("Канада", ("canada", "canadian", "канад")),
    ("ОАЭ", ("united arab emirates", "uae", "оаэ", "jebel ali", "джебель али")),
    ("Иран", ("iran", "iranian", "иран")),
    ("Индия", ("india", "indian", "инди")),
    ("Япония", ("japan", "japanese", "япони")),
    ("Южная Корея", ("south korea", "korean", "южная корея", "корей")),
    ("Шри-Ланка", ("sri lanka", "шри-ланк", "шри ланк")),
    ("Вануату", ("vanuatu", "вануату")),
    ("Панама", ("panama", "панам")),
    ("Египет", ("egypt", "egyptian", "егип", "suez", "суэц")),
)


GEOGRAPHY_ROUTE_FALLBACKS = {
    "Беларусь": "Беларусь — международные грузовые направления",
    "Россия": "Россия — международные грузовые направления",
    "Турция": "Турция — международные грузовые направления",
    "Китай": "Китай — международные грузовые направления",
    "Германия": "Германия — европейские грузовые направления",
    "Австрия": "Австрия — европейские грузовые направления",
    "Польша": "Польша — европейские грузовые направления",
    "Казахстан": "Казахстан — международные грузовые направления",
    "Нидерланды": "Нидерланды — европейские портовые направления",
    "Бельгия": "Бельгия — европейские портовые направления",
    "ОАЭ": "ОАЭ — Персидский залив",
    "Иран": "Иран — Персидский залив",
    "Шри-Ланка": "Шри-Ланка — Индийский океан",
    "Вануату": "Вануату — Тихий океан",
    "Панама": "Панамский канал — международный транзит",
    "Египет": "Египет — Суэцкий канал",
}


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
            if total >= 850 or len(selected) == 4:
                break
        return " ".join(selected)[:1200]
    except Exception as error:
        print(f"Article extraction warning for {url}: {error}", file=sys.stderr)
        return ""


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", title.lower()).strip()


DUPLICATE_EVENT_GROUPS = (
    (
        "event_suspend",
        (
            "suspend", "suspends", "suspended", "suspension",
            "halt", "halts", "halted", "pause", "pauses", "paused",
            "stop bookings", "stopped bookings", "pause bookings",
            "приостанов", "остановил бронирован", "прекратил перевоз",
        ),
    ),
    (
        "event_close",
        (
            "closed", "closure", "shutdown", "blocked", "blockade",
            "закрыт", "закрытие", "перекрыт", "блокад",
        ),
    ),
    (
        "event_attack",
        (
            "attack", "drone strike", "missile strike", "struck",
            "атак", "удар бпла", "удар дрон", "ракетный удар",
        ),
    ),
    (
        "event_delay",
        (
            "delay", "delayed", "disruption", "reroute", "diverted",
            "задерж", "сбой", "перенаправ", "обход маршрут",
        ),
    ),
    (
        "event_congestion",
        (
            "congestion", "backlog", "queue", "vessels gather", "ships gather",
            "tankers gather", "vessels gathered", "ships gathered", "tankers gathered",
            "перегрузк", "очеред", "скопил", "скопление",
        ),
    ),
    (
        "event_tariff",
        (
            "tariff", "freight rate", "surcharge", "rate increase", "rate cut",
            "тариф", "ставка фрахт", "ставки фрахт", "надбавк",
        ),
    ),
    (
        "event_documents",
        (
            "transport document", "consignment note", "bill of lading",
            "customs declaration", "maritime single window",
            "транспортн документ", "накладн", "коносамент", "деклараци",
            "единое морское окно",
        ),
    ),
    (
        "event_sanctions",
        (
            "sanction", "export ban", "import ban", "trade ban",
            "санкц", "запрет экспорт", "запрет импорт",
        ),
    ),
    (
        "event_accident",
        (
            "accident", "collision", "derailment", "fire", "explosion",
            "авари", "столкнов", "крушен", "сход вагон", "пожар", "взрыв",
        ),
    ),
    (
        "event_ranking",
        (
            "ranking", "top 30", "top-30", "fell out of the top",
            "рейтинг", "топ 30", "топ-30", "покинул топ", "выпал из топ",
        ),
    ),
)


DUPLICATE_SUBJECT_GROUPS = (
    ("company_msc", ("msc", "mediterranean shipping company")),
    ("company_maersk", ("maersk", "маерск")),
    ("company_cma_cgm", ("cma cgm", "cma-cgm")),
    ("company_hapag_lloyd", ("hapag lloyd", "hapag-lloyd")),
    ("company_cosco", ("cosco", "cosco shipping")),
    ("company_oocl", ("oocl", "orient overseas container line")),
    ("company_one", ("ocean network express", "one line")),
    ("company_evergreen", ("evergreen", "evergreen marine")),
    ("company_hmm", ("hmm", "hyundai merchant marine")),
    ("company_yang_ming", ("yang ming", "yangming")),
    ("company_zim", ("zim", "zim integrated shipping")),
    ("company_wan_hai", ("wan hai", "wanhai")),
    ("company_pil", ("pil", "pacific international lines")),
    ("company_fesco", ("fesco", "феско")),
    ("company_arkas", ("arkas", "arkas line")),
    ("company_turkon", ("turkon", "turkon line")),
    ("company_akkon", ("akkon", "akkon lines")),
    ("company_sitc", ("sitc", "sitc international")),
    ("company_rzd", ("rzd", "ржд", "russian railways")),
    ("place_novorossiysk", ("novorossiysk", "новороссийск")),
    ("place_jebel_ali", ("jebel ali", "jebel-ali", "джебель али", "джебель-али")),
    ("place_sri_lanka", ("sri lanka", "шри ланк", "шри-ланк")),
    ("place_vanuatu", ("vanuatu", "вануату")),
    ("place_persian_gulf", ("persian gulf", "персидский залив")),
    ("place_hormuz", ("hormuz", "ормуз")),
    ("place_suez", ("suez", "суэц")),
    ("place_black_sea", ("black sea", "черное море", "чёрное море")),
    ("country_iran", ("iran", "iranian", "иран")),
    ("subject_container", ("container", "containers", "containership", "containerships", "контейнер")),
    ("subject_tanker", ("tanker", "tankers", "танкер")),
    ("subject_rail", ("rail", "railway", "железнодорож", "поезд", "вагон")),
    ("subject_port", ("port", "ports", "порт")),
)


DUPLICATE_STOP_WORDS = {
    "about", "after", "again", "amid", "from", "into", "over", "through",
    "world", "worlds", "largest", "major", "new", "news", "says", "the",
    "with", "больше", "всего", "крупнейший", "мира", "новый", "новые",
    "после", "через", "сказал", "сообщил", "сообщает", "свои", "свою",
}


DUPLICATE_IGNORE_STEMS = (
    "приостанов", "перевоз", "брониров", "рейс", "перевозчик",
    "suspend", "halt", "booking", "shipment", "shipping", "transport",
)


def duplicate_signature(title: str) -> tuple[set[str], set[str]]:
    """Return canonical event labels and identity tokens for a headline."""
    lowered = normalize_title(title)
    events = {
        label
        for label, terms in DUPLICATE_EVENT_GROUPS
        if any(has_term(lowered, term) for term in terms)
    }
    identities = {
        label
        for label, terms in DUPLICATE_SUBJECT_GROUPS
        if any(has_term(lowered, term) for term in terms)
    }

    # Preserve uncommon words as a fallback for companies and locations that
    # are not yet in the explicit dictionaries.
    for token in lowered.split():
        if len(token) < 4 or token in DUPLICATE_STOP_WORDS:
            continue
        if any(stem in token for stem in DUPLICATE_IGNORE_STEMS):
            continue
        identities.add(token)

    return events, identities


def is_duplicate(title: str, accepted: Iterable[str]) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return True
    title_events, title_identities = duplicate_signature(title)
    for other in accepted:
        other_normalized = normalize_title(other)
        if normalized == other_normalized:
            return True
        if SequenceMatcher(None, normalized, other_normalized).ratio() >= 0.84:
            return True

        other_events, other_identities = duplicate_signature(other)
        shared_events = title_events & other_events
        canonical_prefixes = ("company_", "place_", "country_", "subject_")
        title_canonical = {
            token
            for token in title_identities
            if token.startswith(canonical_prefixes)
        }
        other_canonical = {
            token
            for token in other_identities
            if token.startswith(canonical_prefixes)
        }
        shared_canonical = title_canonical & other_canonical
        smaller_canonical_count = min(
            len(title_canonical),
            len(other_canonical),
        )
        canonical_overlap = (
            len(shared_canonical) / smaller_canonical_count
            if smaller_canonical_count
            else 0.0
        )

        # Different wording and even different languages still describe one
        # event when the action and at least two concrete subjects/locations
        # match.  The overlap guard prevents unrelated MSC or RZD stories from
        # collapsing merely because they mention the same operator.
        if (
            shared_events
            and len(shared_canonical) >= 2
            and canonical_overlap >= 0.65
        ):
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


def event_geography_for(text: str, source_country: str = "") -> str:
    """Return the location affected by the event, not the publisher country."""
    lowered = clean(text).lower()
    locations: list[str] = []
    for label, terms in EVENT_GEOGRAPHY_TERMS:
        if any(has_term(lowered, term) for term in terms):
            locations.append(label)

    if locations:
        # Two locations are enough to explain a cross-border event without
        # turning the card label into another headline.
        return " / ".join(locations[:2])

    source_country = clean(source_country)
    return source_country or "Международная"


def route_for(
    text: str,
    directions: list[str],
    source_country: str,
    event_geography: str = "",
) -> str:
    lowered = text.lower()
    specific_route = next(
        (
            label
            for terms, label in SPECIFIC_ROUTES
            if any(has_term(lowered, term) for term in terms)
        ),
        "",
    )
    if "РБ–РФ" in directions or "РФ–РБ" in directions:
        return "Беларусь — Россия"
    if "РБ–Турция" in directions:
        return "Беларусь — Турция"
    if "Китай" in directions:
        if specific_route:
            return f"{specific_route} — Китай"
        return "Китай — международные грузовые направления"
    if specific_route:
        return specific_route
    for terms, label in REGIONS:
        if any(has_term(lowered, term) for term in terms):
            return label
    event_geography = clean(event_geography)
    if event_geography and event_geography != "Международная":
        if event_geography in GEOGRAPHY_ROUTE_FALLBACKS:
            return GEOGRAPHY_ROUTE_FALLBACKS[event_geography]
        return f"{event_geography} — международные грузовые направления"
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
    is_carrier_source = article.get("sourceType") == "carrier"
    if contains_any(title, CRIME_AND_SEIZURE_TERMS):
        return -1000
    if contains_any(title, PASSENGER_TERMS):
        return -1000
    if (
        contains_any(title, MILITARY_TERMS)
        and contains_any(title, SPECULATIVE_WAR_COMMENTARY_TERMS)
    ):
        return -1000
    if contains_any(title, PERSONAL_INCIDENT_TERMS) and not (
        contains_any(title, DIRECT_LOGISTICS_ASSET_TERMS)
        and contains_any(title, DIRECT_OPERATIONAL_IMPACT_TERMS)
    ):
        return -1000
    if (
        contains_any(title, MILITARY_TERMS)
        and not is_carrier_source
        and not (
            contains_any(title, DIRECT_LOGISTICS_ASSET_TERMS)
            and contains_any(title, DIRECT_OPERATIONAL_IMPACT_TERMS)
        )
    ):
        return -1000

    rule = rule_for(title)
    score = rule.score if rule else 0
    if contains_any(title, PRIORITY_REGION_TERMS):
        score += 15
    domain = source_name(clean(article.get("url")), clean(article.get("domain")))
    score += domain_bonus(domain)
    if article.get("sourceType") == "carrier":
        # An operational notice from the carrier is the primary source and
        # should outrank media rewrites of the same event.
        score += 24
    elif article.get("sourceType") == "profile":
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


CAUSE_MARKERS = (
    "из-за", "в связи с", "на фоне", "поскольку", "так как",
    "причиной", "в результате", "после того как",
    "после атаки", "после аварии", "после закрытия", "после введения",
    "после повреждения",
    "due to", "because", "amid", "caused by", "driven by",
    "as a result of", "following", "after an attack", "after the attack",
    "after an accident", "after the closure", "after damage",
)


def sentences_for(text: str) -> list[str]:
    return [
        clean(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", clean(text))
        if len(clean(sentence)) >= 25
    ]


def sentence_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        normalize_title(left),
        normalize_title(right),
    ).ratio()


def finish_sentence(text: str, limit: int = 320) -> str:
    result = clean(text)[:limit].rstrip(" ,;:-")
    if result:
        result = result[0].upper() + result[1:]
    if result and result[-1] not in ".!?":
        result += "."
    return result


def is_causal_sentence(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(has_term(lowered, marker) for marker in CAUSE_MARKERS)


def summary_fallback(rule: Rule, transports: list[str], route: str) -> str:
    scope = transport_scope(transports)
    category = rule.cause.lower()
    if "документ" in category:
        action = "Меняется порядок оформления транспортных документов"
    elif "санкцион" in category:
        action = "Меняются ограничения для грузовых операций"
    elif "тариф" in category or "рыночных ставок" in category:
        action = "Меняется стоимость новых грузовых отправок"
    elif "закрытие" in category:
        action = "Ограничена доступность грузового маршрута или терминала"
    elif "забастов" in category:
        action = "Сокращается доступная пропускная способность"
    elif "авария" in category:
        action = "Нарушена работа транспортного участка или инфраструктуры"
    elif "погод" in category:
        action = "Погодные условия ограничивают движение или обработку грузов"
    elif "пограничного" in category or "таможенного" in category:
        action = "Меняется режим пограничного или таможенного оформления"
    elif "перегрузка инфраструктуры" in category:
        action = "На инфраструктуре накопилась очередь необработанных грузов"
    elif "расписание" in category or "маршрут" in category:
        action = "Перевозчик изменяет грузовой маршрут или расписание"
    elif "безопасност" in category:
        action = "Нарушена работа коммерческой грузовой инфраструктуры"
    else:
        action = "Возникло операционное ограничение грузового сообщения"
    return f"{action} для {scope} по направлению «{route}»."


def concrete_summary(
    title: str,
    article_text: str,
    rule: Rule,
    transports: list[str],
    route: str,
) -> str:
    """Return the main fact without repeating the title or the cause."""
    candidates = [
        sentence
        for sentence in sentences_for(article_text)
        if sentence_similarity(sentence, title) < 0.76
        and not is_causal_sentence(sentence)
    ]
    if candidates:
        # Two short factual sentences give the card enough incident detail
        # without turning it into a copy of the source article.
        return finish_sentence(" ".join(candidates[:2]), limit=520)
    return summary_fallback(rule, transports, route)


def concrete_cause(article_text: str, title: str, summary: str) -> str:
    """Extract a stated cause; never copy the title or invent a reason."""
    for sentence in sentences_for(article_text) + [clean(title)]:
        lowered = sentence.lower()
        marker_positions = [
            (lowered.find(marker), marker)
            for marker in CAUSE_MARKERS
            if lowered.find(marker) >= 0
        ]
        if not marker_positions:
            continue
        position, _ = min(marker_positions, key=lambda item: item[0])
        candidate = sentence[position:] if position > 0 else sentence
        if (
            sentence_similarity(candidate, title) >= 0.76
            or sentence_similarity(candidate, summary) >= 0.82
        ):
            continue
        return finish_sentence(candidate, limit=280)

    leading_cause_patterns = (
        r"^(.{12,180}?)\s+(?:привел[аио]?|вызвал[аио]?|стал[аио]? причиной)\b",
        r"^(.{12,180}?)\s+(?:led to|caused|forced)\b",
    )
    for pattern in leading_cause_patterns:
        match = re.search(pattern, clean(title), flags=re.I)
        if match:
            candidate = finish_sentence(match.group(1), limit=220)
            if sentence_similarity(candidate, summary) < 0.82:
                return candidate
    return UNKNOWN_CAUSE


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


def specific_event_details(
    text: str,
    title: str,
    article_text: str,
    route: str,
) -> dict[str, str]:
    """Build concrete fields for high-impact event shapes seen in the feed.

    These branches are intentionally based on the event's named operator,
    asset and location.  They override only wording, not source selection or
    importance scoring.
    """
    lowered = clean(f"{text} {title} {article_text}").lower()

    if (
        has_term(lowered, "msc")
        and contains_any(lowered, ("novorossiysk", "новороссийск"))
        and contains_any(
            lowered,
            (
                "suspend", "suspends", "suspended", "halt", "halts", "halted",
                "booking", "bookings", "stopped accepting bookings",
                "приостанов", "бронирован",
            ),
        )
    ):
        return {
            "summary": (
                "MSC приостановила все новые бронирования грузов "
                "в Новороссийск и из него. Решение принято после атаки БПЛА "
                "на контейнеровоз MSC ULSAN III и затрагивает один из немногих "
                "оставшихся международных контейнерных сервисов порта."
            ),
            "cause": (
                "Атака БПЛА на контейнеровоз MSC ULSAN III и возникший "
                "риск для коммерческого судоходства в Чёрном море."
            ),
            "effect": (
                "Новые контейнерные отправки MSC через Новороссийск временно "
                "не бронируются; грузовладельцам нужно согласовывать другой порт "
                "или линию, что увеличит срок и стоимость доставки."
            ),
        }

    if (
        contains_any(lowered, ("tanker", "танкер"))
        and contains_any(lowered, ("sri lanka", "шри-ланк", "шри ланк"))
        and contains_any(lowered, ("blockade", "блокад", "блокирован"))
    ):
        return {
            "summary": (
                "У берегов Шри-Ланки скопились более десятка иранских танкеров, "
                "которые не могут вернуться в иранские порты и продолжать нефтяные рейсы."
            ),
            "cause": (
                "Блокада США ограничила доступ танкеров к иранским портам "
                "и остановила часть экспортных нефтяных рейсов."
            ),
            "effect": (
                "Танкеры вынуждены ожидать у Шри-Ланки; доступный флот сокращается, "
                "а сроки и стоимость нефтяного фрахта на маршрутах Ирана растут."
            ),
        }

    if (
        contains_any(lowered, ("jebel ali", "jebel-ali", "джебель-али", "джебель али"))
        and contains_any(lowered, ("top 30", "top-30", "топ-30", "топ 30", "ranking", "рейтинг"))
    ):
        return {
            "summary": (
                "Джебель-Али выпал из тридцатки крупнейших контейнерных портов "
                "впервые за 20 лет."
            ),
            "cause": (
                "Снижение контейнерных потоков на фоне кризиса в Персидском заливе."
            ),
            "effect": (
                "Контейнерные потоки перераспределяются между портами региона; сроки "
                "и ставки через Джебель-Али нужно перепроверять, но это не означает закрытие порта."
            ),
        }

    if (
        contains_any(lowered, ("vanuatu", "вануату"))
        and contains_any(
            lowered,
            (
                "digital ship clearance", "maritime single window", "single window",
                "digital clearance", "цифров", "единое морское окно",
            ),
        )
    ):
        return {
            "summary": (
                "Вануату внедряет Maritime Single Window для цифрового обмена сведениями "
                "между судами и портовыми органами."
            ),
            "cause": (
                "Переход портового оформления на цифровой обмен данными через "
                "единое морское окно."
            ),
            "effect": (
                "При заходе в порты Вануату сведения будут подаваться через одно окно; "
                "повторный ввод документов и время портового оформления должны сократиться."
            ),
        }

    return {}


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

    # Reject obvious noise before downloading the article body.
    if contains_any(original_title, CRIME_AND_SEIZURE_TERMS):
        return None
    if contains_any(original_title, PASSENGER_TERMS):
        return None

    excerpt = clean(article.get("excerpt"))
    if (
        len(excerpt) < 45
        or sentence_similarity(excerpt, original_title) >= 0.82
    ):
        extracted = article_excerpt(url)
        excerpt = extracted or ""
    combined = f"{original_title} {excerpt}"

    rule = rule_for(combined)
    transports = transports_for(combined)
    is_carrier_source = article.get("sourceType") == "carrier"
    if is_carrier_source and not transports:
        transports = ["Море"]

    # Crime, drugs, baggage and tourism are outside the business-news feed even
    # when they mention a container, customs office, port or airport.
    if contains_any(combined, CRIME_AND_SEIZURE_TERMS):
        return None
    if contains_any(combined, PASSENGER_TERMS):
        return None
    if (
        contains_any(combined, MILITARY_TERMS)
        and contains_any(combined, SPECULATIVE_WAR_COMMENTARY_TERMS)
    ):
        return None

    has_direct_network_impact = (
        contains_any(combined, DIRECT_LOGISTICS_ASSET_TERMS)
        and contains_any(combined, DIRECT_OPERATIONAL_IMPACT_TERMS)
        and not contains_any(combined, NO_OPERATIONAL_IMPACT_TERMS)
    )
    if contains_any(combined, PERSONAL_INCIDENT_TERMS) and not has_direct_network_impact:
        return None
    if (
        contains_any(combined, MILITARY_TERMS)
        and not has_direct_network_impact
        and not is_carrier_source
    ):
        return None

    has_commercial_context = contains_any(combined, COMMERCIAL_FREIGHT_TERMS)
    is_profile_source = article.get("sourceType") in {"profile", "carrier"}
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
    article_text_ru = translate(excerpt, language, translator) if excerpt else ""
    source_country = clean(article.get("sourcecountry"))
    event_geography = event_geography_for(combined, source_country)
    route = route_for(
        combined,
        directions,
        source_country,
        event_geography,
    )
    event_details = specific_event_details(
        combined,
        title_ru,
        article_text_ru,
        route,
    )
    summary = event_details.get("summary") or concrete_summary(
        title_ru,
        article_text_ru,
        rule,
        transports,
        route,
    )
    cause = event_details.get("cause") or concrete_cause(
        article_text_ru,
        title_ru,
        summary,
    )
    # A generic "cause not stated" card is not actionable.  Keep only events
    # whose source text lets us identify what actually triggered the impact.
    if cause == UNKNOWN_CAUSE:
        return None
    effect = event_details.get("effect") or concrete_effect(
        rule,
        combined,
        transports,
        route,
    )

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
        "country": event_geography,
        "eventCountry": event_geography,
        "route": route,
        "summary": summary,
        "fact": summary,
        "cause": cause,
        "consequence": effect,
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
            "локальной открытой моделью. География события, суть и конкретная причина "
            "извлекаются из публикации; материалы без установленной причины исключаются. "
            "Последствие и важность — "
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
