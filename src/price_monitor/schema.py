from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "availability": {"type": "string", "enum": ["available", "unavailable", "unknown"]},
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "source_block": {"type": ["integer", "null"]},
                    "price": {"type": ["number", "null"]},
                    "price_old": {"type": ["number", "null"]},
                    "price_period": {"type": ["string", "null"]},
                    "connection_cost": {"type": ["number", "null"]},
                    "technology": {"type": ["string", "null"]},
                    "internet_speed_mbps": {"type": ["number", "null"]},
                    "tv_channels": {"type": ["integer", "null"]},
                    "mobile_minutes": {"type": ["integer", "null"]},
                    "mobile_data_gb": {"type": ["number", "null"]},
                    "services": {"type": "array", "items": {"type": "string"}},
                    "promotion_name": {"type": ["string", "null"]},
                    "promotion_description": {"type": ["string", "null"]},
                    "promotion_duration": {"type": ["string", "null"]},
                    "equipment": {"type": "array", "items": {"type": "string"}},
                    "legal_notes": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name", "source_block", "price", "price_old", "price_period", "connection_cost",
                    "technology", "internet_speed_mbps", "tv_channels", "mobile_minutes",
                    "mobile_data_gb", "services", "promotion_name", "promotion_description",
                    "promotion_duration", "equipment", "legal_notes", "evidence"
                ],
                "additionalProperties": False,
            },
        },
        "page_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["availability", "offers", "page_notes"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class ExtractedOffer:
    name: str
    price: float | None
    source_block: int | None = None
    price_old: float | None = None
    price_period: str | None = None
    connection_cost: float | None = None
    technology: str | None = None
    internet_speed_mbps: float | None = None
    tv_channels: int | None = None
    mobile_minutes: int | None = None
    mobile_data_gb: float | None = None
    services: list[str] = field(default_factory=list)
    promotion_name: str | None = None
    promotion_description: str | None = None
    promotion_duration: str | None = None
    equipment: list[str] = field(default_factory=list)
    legal_notes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExtractedOffer":
        def number(name: str) -> float | None:
            item = value.get(name)
            if item is None:
                return None
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"{name} must be a number or null")
            return float(item)

        name = str(value.get("name", "")).strip()
        if not name:
            raise ValueError("offer name is empty")
        price = number("price")
        price_old = number("price_old")
        if price is not None and price < 0:
            raise ValueError("price cannot be negative")
        if price_old is not None and price_old < 0:
            raise ValueError("price_old cannot be negative")

        def strings(key: str) -> list[str]:
            raw = value.get(key, [])
            if not isinstance(raw, list):
                raise ValueError(f"{key} must be an array")
            return [str(item).strip() for item in raw if str(item).strip()]

        def integer(key: str) -> int | None:
            item = value.get(key)
            if item is None:
                return None
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"{key} must be an integer or null")
            return int(item)

        return cls(
            name=name,
            price=price,
            source_block=integer("source_block"),
            price_old=price_old,
            price_period=_optional_string(value.get("price_period")),
            connection_cost=number("connection_cost"),
            technology=_optional_string(value.get("technology")),
            internet_speed_mbps=number("internet_speed_mbps"),
            tv_channels=integer("tv_channels"),
            mobile_minutes=integer("mobile_minutes"),
            mobile_data_gb=number("mobile_data_gb"),
            services=strings("services"),
            promotion_name=_optional_string(value.get("promotion_name")),
            promotion_description=_optional_string(value.get("promotion_description")),
            promotion_duration=_optional_string(value.get("promotion_duration")),
            equipment=strings("equipment"),
            legal_notes=strings("legal_notes"),
            evidence=strings("evidence"),
        )


@dataclass(slots=True)
class Extraction:
    availability: str
    offers: list[ExtractedOffer]
    page_notes: list[str]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Extraction":
        availability = value.get("availability")
        if availability not in {"available", "unavailable", "unknown"}:
            raise ValueError("invalid availability")
        raw_offers = value.get("offers")
        if not isinstance(raw_offers, list):
            raise ValueError("offers must be an array")
        raw_notes = value.get("page_notes", [])
        if not isinstance(raw_notes, list):
            raise ValueError("page_notes must be an array")
        offers = [ExtractedOffer.from_dict(item) for item in raw_offers if isinstance(item, dict)]
        if availability == "unavailable" and offers:
            raise ValueError("unavailable page cannot contain offers")
        return cls(availability, offers, [str(item).strip() for item in raw_notes if str(item).strip()])

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
