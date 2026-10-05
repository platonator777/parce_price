from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .adapter_helpers import _monthly_prices, _yearly_prices, _speeds, html_block_facts, html_offer_blocks, json_items
from .html_reducer import reduce_html
from .api_offers import api_kind, api_offer_facts, api_offer_variants
from collections import Counter
from .adapter_api import RawOffer


NUMBER = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)")
POSITIVE_HTML = re.compile(r"(?:домашн\w*\s+интернет|тариф)", re.I)
PRICE = re.compile(r"(?:\d[\d\s]{1,7}\s*(?:₽|руб)|price)", re.I)
SPEED = re.compile(r"(?:\d+\s*(?:мбит|гбит|mbps|gbps)|speed)", re.I)
UNAVAILABLE = re.compile(r"(?:недоступ|нет технической возможности|доступен\s+только\s+мобильн)", re.I)


@dataclass(slots=True)
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    offer_count: int = 0


def validate_offers(record, offers: list[dict[str, Any]]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[Any, ...]] = set()
    payload_fold = record.payload.casefold()
    source_numbers = None
    source_speeds = None
    for index, offer in enumerate(offers):
        prefix = f"offer {index}"
        name = str(offer.get("name") or "").strip()
        if not name:
            errors.append(f"{prefix}: empty name")
        price, old = offer.get("price"), offer.get("price_old")
        for field_name in ("price", "price_old", "connection_cost", "internet_speed_mbps", "tv_channels", "mobile_minutes", "mobile_data_gb"):
            value = offer.get(field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                errors.append(f"{prefix}: invalid {field_name}")
        if price is not None and old is not None and old < price:
            errors.append(f"{prefix}: price_old is lower than price")
        if record.payload_type == "html" and price is not None:
            source_fragment = offer.get("raw_offer", {}).get("block", record.payload) if isinstance(offer.get("raw_offer"), dict) else record.payload
            annual = offer.get('price_period') == 'год' and float(price) in _yearly_prices(source_fragment) and float(price) in _yearly_prices(reduce_html(record.payload).text)
            if not annual and not _recurring_price_grounded(float(price), source_fragment):
                source_price = re.search(r'\[SOURCE PRICE\]\s*(<span\b[^>]*data-cost=["\'](\d+(?:\.\d+)?)["\'][^>]*>.*?</span>)', source_fragment, re.S)
                unknown_period = (offer.get('price_period') is None and '[UNSPECIFIED BILLING]' in source_fragment
                                  and source_price is not None and source_price.group(1) in record.payload
                                  and float(source_price.group(2)) == float(price))
                if unknown_period:
                    warnings.append(f"{prefix}: billing period is unspecified in the source")
                else:
                    errors.append(f"{prefix}: price={price} is not a recurring monthly price")
        evidence = offer.get("evidence") or []
        if not isinstance(evidence, list) or any(str(item).casefold() not in payload_fold for item in evidence if item):
            errors.append(f"{prefix}: evidence is not grounded in payload")
        signature = (name.casefold(), price, offer.get("internet_speed_mbps"), offer.get("tv_channels"), offer.get("mobile_minutes"), offer.get("mobile_data_gb"), offer.get('variant_id'))
        if signature in seen:
            errors.append(f"{prefix}: duplicate offer")
        seen.add(signature)
        for field_name in ("price", "price_old", "internet_speed_mbps", "tv_channels", "mobile_minutes", "mobile_data_gb"):
            value = offer.get(field_name)
            native_api = record.payload_type == 'json' and isinstance(offer.get('raw_offer'), dict) and api_kind(offer['raw_offer']) is not None
            if value is None or native_api:
                continue
            if field_name == 'internet_speed_mbps':
                if source_speeds is None:
                    source_speeds = _speeds(record.payload)
                if float(value) in source_speeds:
                    continue
            if source_numbers is None:
                source_numbers = _numbers_in_text(record.payload)
            if float(value) not in source_numbers:
                raw = offer.get('raw_offer')
                if not isinstance(raw, dict) or float(value) not in _numbers_in_text(json.dumps(raw, ensure_ascii=False)):
                    errors.append(f"{prefix}: {field_name}={value} is not grounded")
    if record.payload_type == "json":
        try:
            source_items = json_items(json.loads(record.payload))
        except json.JSONDecodeError:
            source_items = []
        if source_items and not offers:
            errors.append("non-empty structured offer list produced no offers")
        if source_items and len(offers) < min(len(source_items), 3):
            warnings.append(f"only {len(offers)} of {len(source_items)} structured items parsed")
        for index, offer in enumerate(offers):
            raw = offer.get("raw_offer")
            parent = raw.get('offer') if isinstance(raw, dict) and 'internet_option' in raw else raw
            option = raw.get('internet_option') if isinstance(raw, dict) else None
            if not isinstance(parent, dict) or parent not in source_items or (option is not None and option not in parent.get('internetOptions', [])):
                errors.append(f"offer {index}: raw_offer does not preserve a source JSON item")
                continue
            if api_kind(raw):
                expected = RawOffer(**api_offer_facts(raw)).to_dict()
                for key in ('name', 'variant_id', 'price', 'price_old', 'price_period', 'connection_cost',
                            'internet_speed_mbps', 'tv_channels', 'mobile_minutes', 'mobile_data_gb',
                            'services', 'conditions', 'optional_services', 'billing_variants'):
                    if offer.get(key) != expected[key]:
                        errors.append(f"offer {index}: {key} differs from source API fields")
                continue
            for output_key, source_key in (("name", "name"), ("price", "price"), ("price_old", "price_old"), ("price_period", "pricePeriod"), ("connection_cost", "costConnection"), ("technology", "tech")):
                source_value = raw.get(source_key)
                output_value = offer.get(output_key)
                if source_value not in (None, "") and output_value is not None:
                    if output_key in {"price", "price_old", "connection_cost"}:
                        if float(output_value) != float(source_value):
                            errors.append(f"offer {index}: {output_key} differs from source {source_key}")
                    elif _normalized_text(output_value) != _normalized_text(source_value):
                        errors.append(f"offer {index}: {output_key} differs from source {source_key}")
            semantic_keys = {
                "internet_speed_mbps": {"speedVal", "internet_speed_mbps", "speed"},
                "tv_channels": {"tv_channels", "channelsCount", "channelCount"},
                "mobile_minutes": {"mobile_minutes", "minutes", "minutesCount"},
                "mobile_data_gb": {"mobile_data_gb", "dataGb", "internetTraffic"},
            }
            for output_key, keys in semantic_keys.items():
                output_value = offer.get(output_key)
                if output_value is not None and float(output_value) not in _numbers_for_keys(raw, keys):
                    errors.append(f"offer {index}: {output_key} is not grounded in a matching JSON field")
        if source_items and all(api_kind(item) for item in source_items):
            expected_raw = [raw for item in source_items for raw in api_offer_variants(item)]
            def identities(values):
                return Counter(json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values)
            if identities(offer.get('raw_offer') for offer in offers) != identities(expected_raw):
                errors.append('API catalogue coverage mismatch: missing, repeated, or unexpected card/slider variant')
    else:
        blocks = html_offer_blocks(record.payload, record.city)
        if len(offers) != len(blocks):
            errors.append(f"expected exactly {len(blocks)} offers for {len(blocks)} offer blocks, got {len(offers)}")
        seen_blocks: set[str] = set()
        for index, offer in enumerate(offers):
            raw = offer.get("raw_offer")
            block = raw.get("block") if isinstance(raw, dict) else None
            if not isinstance(block, str) or block not in blocks:
                errors.append(f"offer {index}: raw_offer.block does not preserve an exact selected block")
                continue
            if block in seen_blocks:
                errors.append(f"offer {index}: source block reused")
            seen_blocks.add(block)
            expected = html_block_facts(block)
            for key in ("name", "price", "price_old", "internet_speed_mbps", "tv_channels", "mobile_minutes", "mobile_data_gb"):
                actual = offer.get(key)
                wanted = expected.get(key)
                if key == "name":
                    equal = _normalized_text(actual) == _normalized_text(wanted)
                else:
                    equal = (actual is None and wanted is None) or (actual is not None and wanted is not None and float(actual) == float(wanted))
                if not equal:
                    errors.append(f"offer {index}: {key}={actual!r}, expected {wanted!r} from its block")
    return ValidationReport(not errors, errors, warnings, len(offers))


def _number_grounded(value: float, payload: str, raw_offer: Any) -> bool:
    if value in _numbers_in_text(payload):
        return True
    if isinstance(raw_offer, dict):
        rendered = json.dumps(raw_offer, ensure_ascii=False)
        return value in _numbers_in_text(rendered)
    return False


def _recurring_price_grounded(value: float, text: str) -> bool:
    # Recognize strictly neighboring DOM fragments and legacy month cells too.
    if value in _monthly_prices(text):
        return True
    for match in re.finditer(
        r"(?<!\d)(\d[\d \u00a0]{0,8}(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?|р\.?)"
        r"\s*(?:/|в\s+)?\s*(?:мес|месяц)",
        text, re.I,
    ):
        parsed = float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        if parsed == value:
            return True
    for match in re.finditer(
        r"(?:мес(?:яц)?|month)\s+(?:всего\s+|за\s+|for\s+)?(\d+(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?|rub)",
        text, re.I,
    ):
        if float(match.group(1).replace(",", ".")) == value:
            return True
    for match in re.finditer(
        r"(?<!\d)(\d{2,8}(?:[.,]\d+)?)\s*\n\s*(?:₽|руб(?:\.|лей)?|р\.?)?\s*/?\s*(?:мес|месяц)",
        text, re.I,
    ):
        if float(match.group(1).replace(",", ".")) == value:
            return True
    if re.search(r"speed(?:OwnHouse)?[\\\"]*\s*:", text, re.I):
        for match in re.finditer(r"price(?:OwnHouse)?[\\\"]*\s*:\s*[\\\"]*(\d+(?:[.,]\d+)?)", text, re.I):
            if float(match.group(1).replace(",", ".")) == value:
                return True
    return False


def _numbers_in_text(text: str) -> set[float]:
    result: set[float] = set()
    for match in re.finditer(r"(?<!\d)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)(?!\d)", text):
        try:
            result.add(float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")))
        except ValueError:
            pass
    return result


def _numbers_for_keys(value: Any, keys: set[str], depth: int = 0) -> set[float]:
    if depth > 10:
        return set()
    result: set[float] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (int, float)) and not isinstance(child, bool):
                result.add(float(child))
            result.update(_numbers_for_keys(child, keys, depth + 1))
    elif isinstance(value, list):
        for child in value:
            result.update(_numbers_for_keys(child, keys, depth + 1))
    return result


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()
