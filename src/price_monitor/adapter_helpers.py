from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Iterator


def parse_json(payload: str) -> Any:
    return json.loads(payload)


def json_items(value: Any) -> list[dict[str, Any]]:
    from .api_offers import beeline_items
    catalogue = beeline_items(value)
    if catalogue is not None:
        return catalogue
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("list", "items", "offers", "tariffs", "data", "result"):
            child = value.get(key)
            if isinstance(child, list):
                return [item for item in child if isinstance(item, dict)]
            if isinstance(child, dict):
                nested = json_items(child)
                if nested:
                    return nested
    return []


def json_offer_facts(item: dict[str, Any]) -> dict[str, Any]:
    from .api_offers import api_offer_facts
    return api_offer_facts(item)


def json_offer_variants(item: dict[str, Any]) -> list[dict[str, Any]]:
    from .api_offers import api_offer_variants
    return api_offer_variants(item)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?", str(value))
    return float(match.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")) if match else None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return int(parsed) if parsed is not None else None


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def first(value: Any, *keys: Any) -> Any:
    """First populated mapping field, or first sequence item matching a predicate.

    The tolerant sequence form keeps generated adapters small; it does not execute
    data-originated code (the predicate is adapter code already covered by AST checks).
    """
    if isinstance(value, dict):
        default = None
        for key in keys:
            if not isinstance(key, str):
                default = key
                continue
            item = value.get(key)
            if item not in (None, "", [], {}):
                return item
        return default
    if isinstance(value, (list, tuple)):
        predicate = keys[0] if keys and callable(keys[0]) else None
        if predicate:
            return next((item for item in value if predicate(item)), None)
        terms = [key.casefold() for key in keys if isinstance(key, str)]
        if terms:
            return next((item for item in value if any(term in str(item).casefold() for term in terms)), None)
        return next((item for item in value if item not in (None, "")), None)
    return None


def evidence_fragment(payload: str, needle: Any, radius: int = 100) -> str:
    rendered = str(needle or "").strip()
    if not rendered:
        return ""
    index = payload.casefold().find(rendered.casefold())
    if index < 0:
        return ""
    # Keep it byte-for-byte (apart from trimming the edges) so grounding can use
    # a deterministic substring check against the original payload.
    return payload[max(0, index-radius):index+len(rendered)+radius].strip()[:280]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "template"}:
            self.ignored_depth += 1
    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "template"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def html_text(payload: str) -> str:
    from .html_reducer import _repair_mojibake

    parser = _TextParser()
    parser.feed(payload)
    return _repair_mojibake(re.sub(r"\s+", " ", " ".join(parser.parts)).strip())


def html_offer_blocks(payload: str, city: str = "", max_chars: int = 18000) -> list[str]:
    """Provider-neutral extraction of repeated commercial DOM regions."""
    from .html_reducer import reduce_html
    from .dom_offers import explicit_dom_offer_blocks
    dom_blocks = explicit_dom_offer_blocks(payload)
    if dom_blocks:
        unique = {}
        for block in dom_blocks:
            facts = html_block_facts(block)
            signature = tuple(facts.get(key) for key in ('name', 'price', 'price_old', 'internet_speed_mbps', 'tv_channels', 'mobile_minutes', 'mobile_data_gb'))
            unique.setdefault(signature, block)
        return list(unique.values())
    reduced = reduce_html(payload, city=city, max_chars=max_chars).text
    matches = list(re.finditer(r"^\[OFFER BLOCK \d+\]\s*$", reduced, re.M))
    blocks: list[str] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(reduced)
        block = reduced[match.end():end].strip()
        if block:
            unique_lines = list(dict.fromkeys(line.strip() for line in block.splitlines() if line.strip()))
            block = "\n".join(unique_lines)
            for atomic in _split_compound_block(block):
                # Navigation/service regions can contain a monthly price but are not a
                # home-internet card. A usable card needs both recurring price and speed.
                if not _monthly_prices(atomic) or not _offer_speeds(atomic):
                    continue
                key = atomic.casefold()
                if key not in seen:
                    blocks.append(atomic)
                    seen.add(key)
    if not blocks:
        detail = _detail_offer_block(reduced)
        if detail:
            blocks.extend(_split_compound_block(detail))
    blocks = [block for block in blocks if _monthly_prices(block) and _offer_speeds(block)]
    if not blocks:
        blocks.extend(_structured_script_blocks(reduced))
    # Do not run fallback grid reconstruction for already segmented card lists.
    if len(blocks) <= 1:
        sequential_blocks = (_sequential_offer_blocks(reduced) if re.search(r"хотите\s+смотреть\s+телевидение", reduced, re.I)
                             else _grid_offer_blocks(reduced) or _sequential_offer_blocks(reduced))
        # A broad DOM wrapper can look like one valid card while actually containing
        # a whole tariff grid. Prefer rows when they recover more cards.
        if len(sequential_blocks) > len(blocks):
            blocks = sequential_blocks
    named_blocks = _named_internet_blocks(reduced)
    if named_blocks and len(named_blocks) >= len(blocks):
        blocks = named_blocks
    filtered = [block for block in blocks if _monthly_prices(block) and _offer_speeds(block)]
    starred_terms = [line.strip() for line in reduced.splitlines() if re.match(r'^\*\s*Акционная\s+цена', line.strip(), re.I)]
    if starred_terms:
        filtered = [block + '\n' + '\n'.join(starred_terms) if re.search(r'\d+\s*₽\s*\*', block) else block for block in filtered]
    output: list[str] = []
    signatures: set[tuple[Any, ...]] = set()
    for block in filtered:
        facts = html_block_facts(block)
        signature = tuple(facts.get(key) for key in (
            "name", "price", "price_old", "internet_speed_mbps", "tv_channels",
            "mobile_minutes", "mobile_data_gb",
        ))
        if signature not in signatures:
            output.append(block)
            signatures.add(signature)
    return output


def html_block_facts(block: str) -> dict[str, Any]:
    """Extract conservative provider-neutral facts from one clean offer block."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    prices = _monthly_prices(block)
    discount = bool(re.search(r"(?:скидк|акци|промо|discount|[−–-]\s*\d+\s*%)", block, re.I))
    name = _commercial_name(lines)
    if len(lines) > 1 and lines[0] == '[DOM CARD TITLE]':
        name = lines[1]
    structured_speed = re.search(r"speed(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", block, re.I)
    if structured_speed:
        name = f"Интернет {structured_speed.group(1)} Мбит/с"
    name_position = block.find(name) if name else -1
    speed_block = '\n'.join(line for line in lines if not re.match(
        r'^(?:Можно\s+добавить|Оплата\s+за\s+цифровое|К\s+любому\s+тарифу\s+доступ)', line, re.I))
    speed_name_position = speed_block.find(name) if name else -1
    speeds_after_name = _speeds(speed_block[speed_name_position + len(name):]) if speed_name_position >= 0 else []
    speeds = speeds_after_name or _offer_speeds(speed_block)
    # MTS cards expose alternative access speeds for the same tariff. The existing
    # adapter contract reports the maximum advertised tariff speed, not the first
    # (default) selector item. Restrict this policy to the tariff's service section
    # so router specifications cannot become its speed.
    if re.match(r"^(?:МТС\s+Дома|РИИЛ\s+Плюс)", name, re.I):
        service_region = block[name_position + len(name):] if name_position >= 0 else block
        service_region = re.split(r"^(?:мобильная\s+связь\s*$|роутер|оборудован|аренд)", service_region, maxsplit=1, flags=re.I | re.M)[0]
        tariff_speeds = _speeds(service_region)
        if tariff_speeds:
            speeds = [max(tariff_speeds)]
    if speeds and (re.fullmatch(r"\d+(?:[.,]\d+)?", name) or _monthly_prices(name)
                   or re.match(r"^(?:возможно,?\s+вас\s+заинтересует|узнать\s+подробнее)$", name, re.I)):
        name = f"Интернет {speeds[0]:g} Мбит/с"
    included_block = '\n'.join(line for line in lines if not re.match(
        r'^(?:Можно\s+добавить|Оплата\s+за\s+цифровое|К\s+любому\s+тарифу\s+доступ)', line, re.I))
    tv_match = re.search(r"(?<!\d)(\d{1,4})\s*\+?\s*(?:тв[-\s]?канал|канал(?:ов|а)?\s+тв|tv\s*channels?)", included_block, re.I)
    if not tv_match and re.search(r"(?:телевид|\bтв\b|\btv\b)", included_block, re.I):
        tv_match = re.search(r"(?<!\d)(\d{1,4})\s*\+?\s*канал(?:ов|а)?\b", included_block, re.I)
    if not tv_match:
        tv_match = re.search(r"^(?:телевидение|тв|television)\s*\n\s*(\d{1,4})\s*\+?", included_block, re.I | re.M)
    minute_values = [int(item) for item in re.findall(r"(?<!\d)(\d{2,5})\s*(?:мин(?:ут)?|minutes?)\b", included_block, re.I)]
    data_values = [float(item.replace(",", ".")) for item in re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:гб|gb)\b", included_block, re.I)]
    services = ["internet"]
    folded = included_block.casefold()
    no_tv = bool(re.search(r'^Без\s+ТВ\s*$', included_block, re.I | re.M)) and tv_match is None
    if not no_tv and re.search(r"(?:тв[-\s]?канал|телевид|\bтв\b|\btv\b)", folded):
        services.append("tv")
    if re.search(r"(?:мобильн|сим[-\s]?карт|\bмин(?:ут)?\b)", folded):
        services.append("mobile")
    if re.search(r"(?:кинопоиск|кинотеатр|cinema|wink)", folded):
        services.append("cinema")
    if re.search(r"(?:игров|gaming|game)", folded):
        services.append("gaming")
    current_price = prices[0] if prices else None
    yearly_prices = _yearly_prices(block)
    if current_price is None and yearly_prices:
        current_price = yearly_prices[0]
    unknown_price = re.search(r"Цена тарифа:\s*(\d+(?:[.,]\d+)?)\s*₽", block) if '[UNSPECIFIED BILLING]' in block else None
    if current_price is None and unknown_price:
        current_price = float(unknown_price.group(1).replace(',', '.'))
    larger_prices = [value for value in prices[1:] if current_price is not None and value > current_price]
    standalone_old = _standalone_old_price(lines, current_price) if discount else None
    inline_old = None
    if current_price is not None:
        for line in lines:
            pair = re.fullmatch(r'(\d{2,8})\s+(\d{2,8})\s*(?:руб\.?|₽)\s*/\s*мес', line, re.I)
            if pair and float(pair.group(2)) == current_price and float(pair.group(1)) > current_price:
                inline_old = float(pair.group(1))
                break
    conditions = list(dict.fromkeys(line for line in lines if re.search(
        r'(?:при\s+активации|скидк.*(?:месяц|мес)|акци.*(?:месяц|адрес)|ограниченн.*адрес|безлимитный\s+мобильный)', line, re.I)))
    optional_services = [line for line in lines if re.match(r'^(?:Можно\s+добавить|Оплата\s+за\s+цифровое|К\s+любому\s+тарифу\s+доступ)', line, re.I)]
    billing_variants = []
    for line in lines:
        variant = re.search(r'(\d+)\s+месяц\w*.*?в\s+размере\s+(\d+)\s+руб.*?из\s+расчета\s*[-—]?\s*(\d+)\s+руб', line, re.I)
        if variant:
            item = {'prepaid_months': int(variant.group(1)), 'upfront_price': float(variant.group(2)), 'monthly_equivalent': float(variant.group(3))}
            if item not in billing_variants:
                billing_variants.append(item)
    return {
        "name": name,
        "price": current_price,
        "price_old": standalone_old if standalone_old is not None else inline_old if inline_old is not None else max(larger_prices) if discount and larger_prices else None,
        "price_period": "месяц" if prices else "год" if yearly_prices else None,
        "connection_cost": None,
        "technology": None,
        "internet_speed_mbps": speeds[0] if speeds else None,
        "tv_channels": int(tv_match.group(1)) if tv_match else None,
        "mobile_minutes": max(minute_values) if minute_values else None,
        "mobile_data_gb": max(data_values) if data_values else None,
        "services": services,
        "conditions": conditions,
        "optional_services": optional_services,
        "billing_variants": billing_variants,
    }


def _yearly_prices(block: str) -> list[float]:
    return [float(value.replace(' ', '').replace('\u00a0', '').replace(',', '.')) for value in re.findall(
        r'(?<!\d)(\d[\d \u00a0]{0,8}(?:[.,]\d+)?)\s*(?:₽|руб\.?)\s*/\s*год\b', block, re.I)]


def _monthly_prices(block: str) -> list[float]:
    values: list[float] = []
    pattern = re.compile(
        r"(?<!\d)((?:\d{1,3}(?:[ \t\u00a0]\d{3})+|\d{1,8})(?:[.,]\d+)?)"
        r"[ \t\u2060]*(?:₽|руб(?:\.|лей)?|р\.?)"
        r"[ \t\u2060]*(?:/|в[ \t\u2060]+)?[ \t\u2060]*(?:мес|месяц)",
        re.I,
    )
    ancillary = re.compile(
        r"(?:роутер|маршрутизатор|оборудован|приставк|аренд|подключ|установ|разов|"
        r"(?:белый|статическ\w*)\s+ip|ip[-\s]?адрес)", re.I,
    )
    for match in pattern.finditer(block):
        line_start = block.rfind("\n", 0, match.start()) + 1
        line_end = block.find("\n", match.end())
        line = block[line_start:line_end if line_end >= 0 else len(block)]
        if re.match(r'^Можно\s+добавить', line, re.I):
            continue
        if ancillary.search(line):
            continue
        previous_lines = block[:max(0, line_start - 1)].splitlines()[-2:]
        if any(ancillary.search(previous)
               and not re.search(r"(?:₽|руб|в\s+составе|включ[её]н|в\s+подарок|бесплатн|подключите)", previous, re.I)
               for previous in previous_lines):
            continue
        raw = match.group(1)
        rendered = raw.replace(" ", "").replace("\t", "").replace("\u00a0", "").replace(",", ".")
        try:
            parsed = float(rendered)
            # Promo markup sometimes renders old and current values as
            # "695 645 руб/мес". It is not a thousands-separated monthly price.
            if parsed > 100_000 and re.search(r"[ \t\u00a0]", raw):
                parsed = float(re.split(r"[ \t\u00a0]+", raw)[-1].replace(",", "."))
            values.append(parsed)
        except ValueError:
            continue
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    # DOM nodes often split "390 ₽" and "в месяц". Only join immediately
    # neighboring, line-shaped fragments to avoid crossing unrelated content.
    currency_only = re.compile(
        r"(?<!\d)((?:\d{1,3}(?:[ \t\u00a0]\d{3})+|\d{1,8})(?:[.,]\d+)?)"
        r"[ \t\u00a0]*(?:₽|руб(?:\.|лей)?|р\.?)[*\s]*$", re.I,
    )
    period_only = re.compile(r"^(?:/|в\s+)?(?:мес(?:яц)?|month)\.?$", re.I)
    currency_period = re.compile(r"^(?:₽|руб(?:\.|лей)?|р\.?)?\s*/?\s*(?:мес|месяц)\.?$", re.I)
    bare_number = re.compile(r"^(\d{1,8}(?:[.,]\d+)?)$")
    for index, line in enumerate(lines):
        if period_only.fullmatch(line):
            for previous in reversed(lines[max(0, index - 2):index]):
                match = currency_only.search(previous)
                if match:
                    parsed = float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
                    if parsed not in values:
                        values.append(parsed)
                    break
        if index and currency_period.fullmatch(line):
            match = bare_number.fullmatch(lines[index - 1])
            if match:
                parsed = float(match.group(1).replace(",", "."))
                if parsed not in values:
                    values.append(parsed)
    # Some legacy tariff tables omit the currency but retain a dedicated
    # "840 месяц" cell. Require a speed in the same candidate block.
    if _speeds(block):
        for line in lines:
            match = re.fullmatch(r"(\d{2,8}(?:[.,]\d+)?)\s+(?:в\s+)?месяц", line, re.I)
            if match:
                parsed = float(match.group(1).replace(",", "."))
                if parsed not in values:
                    values.append(parsed)
    reverse = re.compile(r"(?:мес(?:яц)?|month)\s+(?:всего\s+|за\s+|for\s+)?(\d+(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?|rub)", re.I)
    for match in reverse.finditer(block):
        value = float(match.group(1).replace(",", "."))
        if value not in values:
            values.append(value)
    if re.search(r"speed(?:OwnHouse)?[\\\"]*\s*:", block, re.I):
        for value in re.findall(r"price(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", block, re.I):
            parsed = float(value.replace(",", "."))
            if parsed not in values:
                values.append(parsed)
    return values


def _split_compound_block(block: str) -> list[str]:
    """Split a reducer region that accidentally contains adjacent tariff cards."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    price_indexes = [index for index, line in enumerate(lines) if _monthly_prices(line)]
    if len(price_indexes) <= 1:
        return [block]
    starts = [0]
    previous_price = price_indexes[0]
    for price_index in price_indexes[1:]:
        between = lines[previous_price + 1:price_index]
        if _speeds("\n".join(between)) or _has_card_identity(between):
            starts.append(previous_price + 1)
        previous_price = price_index
    if len(starts) == 1:
        return [block]
    output: list[str] = []
    parent_has_home = bool(re.search(r"(?:домашн\w*\s+интернет|интернет\s+для\s+дома|для\s+дома|\bдома\b|home\s+internet|broadband)", block, re.I))
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        segment = lines[start:end]
        while segment and re.fullmatch(r"(?:подробнее(?:\s+о\s+тарифе)?|подключить|выбрать)", segment[0], re.I):
            segment.pop(0)
        rendered = "\n".join(segment)
        if parent_has_home and _offer_speeds(rendered) and _commercial_name(segment):
            output.append(rendered)
    return output or [block]


def _has_card_identity(lines: list[str]) -> bool:
    return any(
        3 <= len(line) <= 100
        and not re.fullmatch(r"(?:скидк.*|акци.*|далее.*|подробнее.*|подключить|выбрать)", line, re.I)
        and not re.search(r"(?:₽|руб|мбит|гбит|mbps|gbps|гб|gb|минут|канал)", line, re.I)
        and (
            re.search(r"^(?:тариф|подписка|план|offer|plan)\b", line, re.I)
            or re.search(r"\b(?:дома|home)\b", line, re.I)
            or bool(re.search(r"[A-Za-zА-Яа-яЁё]{3}", line) and re.search(r"\d{2,4}", line))
        )
        for line in lines
    )


def _speeds(block: str) -> list[float]:
    result: list[float] = []
    for first_value, _, unit in re.findall(
        r"(?<!\d)(\d+(?:[.,]\d+)?)(\s*/\s*\d+(?:[.,]\d+)?)\s*(мбит|гбит|mbps|gbps)(?:/с)?",
        block, re.I,
    ):
        parsed = float(first_value.replace(",", "."))
        if unit.casefold() in {"гбит", "gbps"}:
            parsed *= 1000
        result.append(parsed)
    for value, unit in re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(мбит|гбит|mbps|gbps)(?:/с)?", block, re.I):
        parsed = float(value.replace(",", "."))
        if unit.casefold() in {"гбит", "gbps"}:
            parsed *= 1000
        result.append(parsed)
    for value in re.findall(r"speed(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", block, re.I):
        parsed = float(value.replace(",", "."))
        if parsed not in result:
            result.append(parsed)
    return result


def _offer_speeds(block: str) -> list[float]:
    speeds = _speeds(block)
    if speeds:
        return speeds
    name = _commercial_name([line.strip() for line in block.splitlines() if line.strip()])
    # Some provider cards encode speed only in a branded tariff name (e.g. "Orange 100").
    if name and not re.search(r"(?:антивирус|security|роутер|аренд|руб|₽)", name, re.I) and not re.fullmatch(r"(?:тариф|план|offer|plan|домашн\w*\s+интернет)", name, re.I):
        values = [float(value.replace(",", ".")) for value in re.findall(r"(?:^|\s)(\d{2,4})(?=$|\s|/|\+)", name)]
        return [value for value in values if 10 <= value <= 10_000]
    return []


def _commercial_name(lines: list[str]) -> str:
    explicit = next((
        line for line in lines
        if re.match(r"^(?:тариф|подписка|план|offer|plan)\b", line, re.I)
        and not re.fullmatch(r"(?:тариф|тарифы|подписка|план|offer|plan)[:\s]*", line, re.I)
        and not re.match(r"^тариф\s+для\s+(?:тех|тех,|того|всех)\b", line, re.I)
    ), None)
    if explicit:
        return explicit
    noise = re.compile(
        r"^(?:тарифы\b|акци|старт\s+на\b|изображение\b|домашн|скорост|над[её]ж|пакет|хит\s+продаж|до\s+\d|защит|вс[её]\s+включено|"
        r"для\s+дома|интернет\s*\+|мобильн|сим\b|(?:\d+\s+)?мегасил|безлимит$|полный\s+безлимит|родительск|блокировк|"
        r"группа\s+для|скидк|новинк|выберите\s+тариф|подключить$|хотите\s+смотреть|инсис\s+тв|"
        r"характеристики?$|скорость:?$|в\s+месяц$|абонентская\s+плата|[-−–]?\d+\s*%)", re.I
    )
    candidates = [
        line for line in lines if 3 <= len(line) <= 80 and not noise.search(line)
        and not line.startswith('[') and '<' not in line
        and not line.startswith('+')
        and not re.fullmatch(r"\d{4}", line)
        and not re.fullmatch(r"\+\d+\s+номер(?:а)?\b.*", line, re.I)
        and not re.search(r"\d.*(?:₽|руб|р\s*/|мбит|гбит|gb|гб|мин|канал)", line, re.I)
    ]
    preferred = next((
        line for line in candidates
        if re.search(r"(?:\bдома\b|\bhome\b)", line, re.I) and len(line.split()) <= 4
    ), None)
    if preferred:
        return preferred
    branded_numeric = next((line for line in candidates if re.search(r"[A-Za-zА-Яа-яЁё].*\d{2,4}", line)
                            and not re.search(r"(?:антивирус|security)", line, re.I)), None)
    if branded_numeric:
        return branded_numeric
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if re.search(r"(?:скидк|акци|промо)", line, re.I) and index:
            candidate = lines[index - 1]
            if candidate in candidates:
                return candidate
    return candidates[0] if candidates else lines[0] if lines else ""


def _standalone_old_price(lines: list[str], current: float | None) -> float | None:
    if current is None:
        return None
    for index, line in enumerate(lines):
        if current not in _monthly_prices('\n'.join(lines[index:index + 2])):
            continue
        for previous in reversed(lines[max(0, index - 3):index]):
            match = re.fullmatch(r"(\d[\d \t\u00a0]{0,8}(?:[.,]\d+)?)(?:\s*(?:₽|руб(?:\.|лей)?))?", previous, re.I)
            if match:
                value = float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
                if value > current:
                    return value
        for following in lines[index + 1:index + 4]:
            match = re.fullmatch(r"(\d[\d \t\u00a0]{0,8}(?:[.,]\d+)?)(?:\s*(?:₽|руб(?:\.|лей)?))?", following, re.I)
            if match:
                value = float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
                if value > current:
                    return value
    return None


def _sequential_offer_blocks(reduced: str) -> list[str]:
    """Recover simple repeated speed/price rows when the DOM has no card headings."""
    lines = [
        line.removeprefix("… ").strip() for line in reduced.splitlines()
        if line.strip() and not line.startswith("[") and not line.startswith("META ")
    ]
    ancillary = re.compile(r"(?:роутер|маршрутизатор|оборудован|приставк|wi-?fi\s*[56])", re.I)
    speed_indexes = [index for index, line in enumerate(lines) if _speeds(line) and not ancillary.search(line)]
    blocks: list[str] = []
    reverse_price_indexes = [
        index for index, line in enumerate(lines)
        if re.search(r"(?:мес(?:яц)?|month)\s+(?:всего\s+|за\s+|for\s+)?\d", line, re.I)
        and _monthly_prices(line)
    ]
    for price_index in reverse_price_indexes:
        next_speed = next((index for index in speed_indexes if price_index < index <= price_index + 8), None)
        if next_speed is None:
            continue
        region = lines[max(0, price_index - 3):next_speed + 1]
        rendered = "\n".join(dict.fromkeys(region))
        if _offer_speeds(rendered) and _monthly_prices(rendered):
            blocks.append(rendered)
    for position, speed_index in enumerate(speed_indexes):
        end = speed_indexes[position + 1] if position + 1 < len(speed_indexes) else min(len(lines), speed_index + 14)
        prior_prices = [
            index for index in range(max(0, speed_index - 8), speed_index)
            if _monthly_prices(lines[index])
            and re.search(r"(?:мес(?:яц)?|month)\s+(?:всего\s+|за\s+|for\s+)?\d", lines[index], re.I)
        ]
        if prior_prices:
            start = prior_prices[-1]
        else:
            start = _name_start_before_speed(lines, speed_index)
        region = lines[start:end]
        price_positions = [index for index, line in enumerate(region) if _monthly_prices(line)]
        if not price_positions:
            continue
        # In this legacy layout the total subscription follows optional add-ons.
        # Explicit sibling grids use _grid_offer_blocks instead.
        main_price = price_positions[-1]
        compact = [line for index, line in enumerate(region[:main_price + 1]) if index == main_price or not _monthly_prices(line)]
        rendered = "\n".join(dict.fromkeys(compact))
        if _offer_speeds(rendered) and _monthly_prices(rendered):
            blocks.append(rendered)
    return list(dict.fromkeys(blocks))


def _name_start_before_speed(lines: list[str], speed_index: int) -> int:
    label = re.compile(r"^(?:до|скорост|характеристик|абонентск|тарифы?$|выберите|подключ|стоимость)", re.I)
    for index in range(speed_index - 1, max(-1, speed_index - 6), -1):
        line = lines[index]
        if 2 <= len(line) <= 60 and re.search(r"[A-Za-zА-Яа-яЁё]", line) and not label.search(line):
            return index
    if speed_index and re.fullmatch(r"\d+(?:[.,]\d+)?", lines[speed_index - 1]):
        return speed_index - 1
    return speed_index


def _grid_offer_blocks(reduced: str) -> list[str]:
    """Recover visual tariff grids whose fields were split into sibling DOM nodes."""
    lines = [
        line.removeprefix("… ").strip() for line in reduced.splitlines()
        if line.strip() and not line.startswith("[") and not line.startswith("META ")
    ]
    ancillary = re.compile(
        r"(?:роутер|маршрутизатор|оборудован|приставк|аренд|покупк|выкуп|"
        r"исходящая\s+скорость|скорость\s+wi-?fi|порт(?:ы)?\s+lan)", re.I,
    )
    speed_indexes: list[int] = []
    for index, line in enumerate(lines):
        speeds = _speeds(line)
        if not speeds or len(line) > 120 or ancillary.search(line):
            continue
        if speed_indexes and index - speed_indexes[-1] <= 6:
            previous = _speeds(lines[speed_indexes[-1]])
            separated = any(re.match(r"^(?:узнать\s+подробнее|подробнее)$", lines[item], re.I)
                            for item in range(speed_indexes[-1] + 1, index))
            if previous and speeds[0] == previous[0] and not separated:
                continue
        speed_indexes.append(index)

    def price_at(index: int) -> tuple[list[float], int]:
        direct = _monthly_prices(lines[index])
        if direct:
            return direct, index + 1
        legacy = re.fullmatch(r"(\d{2,8}(?:[.,]\d+)?)\s+(?:в\s+)?месяц", lines[index], re.I)
        if legacy:
            return [float(legacy.group(1).replace(",", "."))], index + 1
        bare = bool(re.fullmatch(r"\d{1,8}(?:[.,]\d+)?", lines[index]))
        with_currency = bool(re.fullmatch(
            r"\d{1,8}(?:[.,]\d+)?\s*(?:₽|руб(?:\.|лей)?|р\.?)[*\s]*", lines[index], re.I))
        if bare and index + 1 < len(lines) and re.fullmatch(
            r"(?:₽|руб(?:\.|лей)?|р\.?)?\s*/?\s*(?:мес|месяц)", lines[index + 1], re.I):
            return _monthly_prices("\n".join(lines[index:index + 2])), index + 2
        if with_currency:
            for width in (2, 3):
                end = min(len(lines), index + width)
                if re.fullmatch(r"(?:/|в\s+)?(?:мес|месяц)", lines[end - 1], re.I):
                    found = _monthly_prices("\n".join(lines[index:end]))
                    if found:
                        return found, end
        return [], index + 1

    name_noise = re.compile(
        r"^(?:internet|интернет|акци|характеристик|скорост|до\b|сек$|мбит$|от$|"
        r"подключ|подробнее|узнать|выбрать|абонентск|цифровое\s+тв|кабельное\s+тв|"
        r"телефония|пакеты|список|регламент|роутер|скидк|выгодно|идеальн|стабильн|"
        r"⚠|при\s+наличии|в\s+месяц$|выкуп|покупка)", re.I,
    )
    blocks: list[str] = []
    previous_speed = -1
    for position, speed_index in enumerate(speed_indexes):
        next_speed = speed_indexes[position + 1] if position + 1 < len(speed_indexes) else len(lines)
        recent_action = max(
            (index for index in range(previous_speed + 1, speed_index) if re.match(r"^(?:узнать\s+подробнее|подробнее)$", lines[index], re.I)),
            default=previous_speed,
        )
        card_floor = recent_action + 1
        after: list[tuple[int, list[float], int]] = []
        for index in range(speed_index + 1, min(next_speed, speed_index + 9)):
            if ancillary.search(lines[index]) or (index and ancillary.search(lines[index - 1])):
                continue
            prices, end = price_at(index)
            if prices:
                after.append((index, prices, end))
                break
        before: list[tuple[int, list[float], int]] = []
        for index in range(max(card_floor, speed_index - 18), speed_index):
            if ancillary.search(lines[index]):
                continue
            prices, end = price_at(index)
            if prices:
                before.append((index, prices, end))
        immediate_before = False
        if speed_index:
            immediate_marker = bool(
                _monthly_prices(lines[speed_index - 1])
                or re.fullmatch(r"\d{2,8}(?:[.,]\d+)?(?:\s+(?:в\s+)?месяц)?", lines[speed_index - 1], re.I)
                or re.fullmatch(r"(?:руб\.?|р\.?)?\s*/?\s*(?:мес|месяц)", lines[speed_index - 1], re.I)
            )
            immediate_before = bool(before and immediate_marker)
        selected = (before[0] if before and immediate_before else None) or (after[0] if after else None)
        if selected is None:
            previous_speed = speed_index
            continue
        price_index, _, price_end = selected
        anchor = min(price_index, speed_index)
        start = anchor
        prose_name = None
        for index in range(max(card_floor, anchor - 18), anchor + 1):
            match = re.match(r"^(.{2,50}?)-\d+\s+месяц", lines[index], re.I)
            if match:
                prose_name = match.group(1).strip()
                start = index
                break
        if prose_name is None:
            candidates: list[int] = []
            for index in range(anchor - 1, max(card_floor - 1, anchor - 12), -1):
                line = lines[index]
                if (
                    2 <= len(line) <= 80
                    and re.search(r"[A-Za-zА-Яа-яЁё]", line)
                    and not name_noise.search(line)
                    and not _monthly_prices(line)
                    and not _speeds(line)
                ):
                    candidates.append(index)
            branded = next((index for index in candidates if re.search(r'(?:\bEVO\b|[«"])', lines[index], re.I)), None)
            if branded is not None:
                start = branded
            elif candidates:
                start = candidates[0]
        end = max(speed_index + 1, price_end)
        if end < len(lines) and re.match(r'^при\s+активации', lines[end], re.I):
            end += 1
        rendered_lines = lines[start:end]
        if start == anchor and re.search(r"\bинтернет\b.*(?:мбит|гбит|mbps|gbps)", lines[speed_index], re.I):
            rendered_lines.insert(0, lines[speed_index])
        if prose_name and (not rendered_lines or rendered_lines[0] != prose_name):
            rendered_lines.insert(0, prose_name)
        rendered = "\n".join(dict.fromkeys(rendered_lines))
        if _monthly_prices(rendered) and _speeds(rendered):
            blocks.append(rendered)
        previous_speed = speed_index
    return blocks


def _named_internet_blocks(reduced: str) -> list[str]:
    lines = [
        line.removeprefix("… ").strip() for line in reduced.splitlines()
        if line.strip() and not line.startswith("[") and not line.startswith("META ")
    ]
    starts = [
        index for index, line in enumerate(lines)
        if re.search(r"\bинтернет\b.*\d{2,4}", line, re.I)
        and any(_speeds(candidate) for candidate in lines[index:index + 4])
    ]
    blocks: list[str] = []
    seen_names: set[str] = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else min(len(lines), start + 24)
        name_key = re.sub(r"\s+", " ", lines[start]).casefold()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        rendered = "\n".join(dict.fromkeys(lines[start:end]))
        if _monthly_prices(rendered) and _offer_speeds(rendered):
            blocks.append(rendered)
    return blocks


def _structured_script_blocks(reduced: str) -> list[str]:
    lines = [line.strip() for line in reduced.splitlines() if line.strip()]
    speed_line = re.compile(r"speed(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", re.I)
    price_line = re.compile(r"price(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", re.I)
    blocks: list[str] = []
    for index, line in enumerate(lines):
        speed = speed_line.search(line)
        if not speed:
            continue
        price = next((candidate for candidate in lines[index + 1:index + 5] if price_line.search(candidate)), None)
        if price:
            blocks.append(f'{line}\n{price}')
    return blocks


def _detail_offer_block(reduced: str) -> str | None:
    """Recover one compact hero tariff from a non-repeated detail page."""
    lines = [
        line.removeprefix("… ").strip()
        for line in reduced.splitlines()
        if line.strip() and not line.startswith("[") and not line.startswith("META ")
    ]
    home = re.compile(
        r"(?:домашн\w*\s+интернет|интернет\s+для\s+дома|для\s+дома|home\s+internet|broadband)", re.I
    )
    name_line = re.compile(r"(?:^(?:тариф|подписка|план|offer|plan)\b|\bдома\b|\bhome\b)", re.I)
    for price_index, line in enumerate(lines):
        if not _monthly_prices(line):
            continue
        low = max(0, price_index - 30)
        high = min(len(lines), price_index + 7)
        region = lines[low:high]
        rendered = "\n".join(region)
        if re.search(r'(?:"(?:value|detailsDescription)"\s*:|<p\b|<h\d\b)', rendered, re.I):
            continue
        if not home.search(rendered) or not _speeds(rendered):
            continue
        candidates = [
            index for index in range(low, price_index)
            if 3 <= len(lines[index]) <= 80 and name_line.search(lines[index])
            and not re.search(r"\d.*(?:₽|руб|мбит|гбит|мин|гб|gb|канал)", lines[index], re.I)
        ]
        if candidates:
            # Prefer a branded "... Дома ..." line nearest the price over a generic
            # navigation label such as "Домашний интернет".
            branded = [
                index for index in candidates
                if re.search(r"\bдома\b|\bhome\b", lines[index], re.I)
                and not re.fullmatch(r"(?:для\s+дома|домашн\w*\s+интернет|home\s+internet)", lines[index], re.I)
            ]
            start = branded[-1] if branded else candidates[-1]
            if branded and len(lines[start].split()) >= 5:
                for previous in range(start - 1, max(low - 1, start - 3), -1):
                    if (
                        3 <= len(lines[previous]) <= 50
                        and not re.search(r"\d", lines[previous])
                        and not re.fullmatch(r"(?:акция|подробнее|хит\s+продаж)", lines[previous], re.I)
                    ):
                        start = previous
                        break
        else:
            start = low
        detail = "\n".join(dict.fromkeys(lines[start:high]))
        if _monthly_prices(detail) and _speeds(detail) and home.search(detail):
            return detail
    return None


def iter_json_dicts(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_dicts(child, depth + 1)
