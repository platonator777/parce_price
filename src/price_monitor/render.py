from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .schema import ExtractedOffer


def build_card(
    offer: ExtractedOffer,
    *,
    provider: str,
    city: str,
    url: str,
    timestamp: str,
    locality_id: int | None = None,
) -> dict[str, Any]:
    identity = (
        f"{provider}\0{city}\0{offer.name}\0{offer.internet_speed_mbps}"
        f"\0{offer.tv_channels}\0{offer.mobile_minutes}"
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
    offer_id = int(digest[:14], 16)
    services = _services(offer)
    product_items = _products(offer, digest)
    stock_data = None
    tabs: list[dict[str, Any]] = []
    if offer.promotion_name or offer.promotion_description:
        stock_data = {
            "isStock": True,
            "stockName": offer.promotion_name or "Акция",
            "stockTheme": "gradient-border",
            "stockDesc": offer.promotion_description,
            "stockBtnText": offer.promotion_duration,
            "discountDesc": offer.promotion_description,
            "discountValue": _discount_value(offer),
            "discountName": offer.promotion_name,
            "discountTextValue": offer.promotion_duration,
        }
        tabs.append({
            "code": "STOCK",
            "name": offer.promotion_name or "Акция",
            "theme": "gradient-border",
            "desc": offer.promotion_description,
        })

    legal_items = [*_unique(offer.legal_notes), *[f"Источник: {url}"]]
    return {
        "name": offer.name,
        "offerId": offer_id,
        "id": digest[:8],
        "price": _compact_number(offer.price),
        "price_old": _compact_number(offer.price_old),
        "atoPrice": 0,
        "pricePeriod": _normalize_period(offer.price_period),
        "orderBuyPrice": 0,
        "costConnection": _compact_number(offer.connection_cost),
        "tech": offer.technology,
        "landscape": "ALLINONE" if len(services) > 1 else "INTERNET",
        "packagesTypes": ",".join(item.title() for item in services),
        "pts": "internet",
        "created": _normalize_timestamp(timestamp),
        "orderHash": hashlib.sha1((identity + "\0order").encode("utf-8")).hexdigest(),
        "localityId": locality_id,
        "stockData": stock_data,
        "products": product_items,
        "tabs": tabs,
        "pocV2": 1,
        "lcs": "active",
        "legals": [{"offerId": offer_id, "title": offer.name, "items": legal_items}],
        "pcs": len(product_items),
    }


def _products(offer: ExtractedOffer, digest: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    if offer.internet_speed_mbps is not None or "internet" in [s.lower() for s in offer.services]:
        speed = _compact_number(offer.internet_speed_mbps)
        products.append({
            "code": "Main_Internet_service",
            "productCode": "SHPD",
            "priority": 1,
            "title": "Безлимитный интернет",
            "v": 1,
            "speedVal": speed,
            "codeTechnology": offer.technology.upper() if offer.technology else None,
            "id": digest[8:16],
            "name": f"{speed} Мбит/с" if speed is not None else "Домашний интернет",
            "desc": offer.technology,
            "iconText": speed,
        })
    if offer.tv_channels is not None:
        products.append({"code": "tv_channels", "productCode": "IPTV", "priority": 2, "v": offer.tv_channels})
    if offer.mobile_data_gb is not None:
        products.append({"code": "mobile_data", "productCode": "MOBILE", "priority": 2, "v": _compact_number(offer.mobile_data_gb), "unit": "GB"})
    if offer.mobile_minutes is not None:
        products.append({"code": "mobile_minutes", "productCode": "MOBILE", "priority": 3, "v": offer.mobile_minutes})
    for index, equipment in enumerate(_unique(offer.equipment), start=10):
        products.append({"code": "equipment", "productCode": "EQUIPMENT", "priority": index, "name": equipment})
    return products


def _services(offer: ExtractedOffer) -> list[str]:
    result = [service.lower() for service in offer.services if service]
    if offer.internet_speed_mbps is not None and "internet" not in result:
        result.insert(0, "internet")
    if offer.tv_channels is not None and "tv" not in result:
        result.append("tv")
    if (offer.mobile_minutes is not None or offer.mobile_data_gb is not None) and "mobile" not in result:
        result.append("mobile")
    return _unique(result) or ["internet"]


def _normalize_period(value: str | None) -> str:
    if not value:
        return "мес"
    lower = value.casefold()
    if "месяц" in lower or "мес" in lower:
        return "мес"
    if "день" in lower or "сут" in lower:
        return "день"
    return value


def _discount_value(offer: ExtractedOffer) -> float | int | None:
    if offer.price is None or offer.price_old is None or offer.price_old <= 0:
        return None
    return round((offer.price_old - offer.price) / offer.price_old * 100, 1)


def _compact_number(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def _normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed.isoformat(timespec="seconds")


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def card_json(card: dict[str, Any]) -> str:
    return json.dumps(card, ensure_ascii=False, indent=2)
