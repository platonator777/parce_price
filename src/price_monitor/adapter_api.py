from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .source import SourceRecord


@dataclass(slots=True)
class RawOffer:
    name: str
    availability: str = "available"
    price: float | None = None
    price_old: float | None = None
    price_period: str | None = None
    connection_cost: float | None = None
    technology: str | None = None
    internet_speed_mbps: float | None = None
    tv_channels: int | None = None
    mobile_minutes: int | None = None
    mobile_data_gb: float | None = None
    services: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    optional_services: list[str] = field(default_factory=list)
    billing_variants: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    raw_offer: dict[str, Any] = field(default_factory=dict)
    variant_id: str | None = None

    def __post_init__(self) -> None:
        def numeric(value: Any, *, whole: bool = False) -> float | int | None:
            if value is None or isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return int(value) if whole else float(value)
            import re
            match = re.search(r"-?\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?", str(value))
            if not match:
                return None
            parsed = float(match.group(0).replace(" ", "").replace("\u00a0", "").replace(",", "."))
            return int(parsed) if whole else parsed
        for field_name in ("price", "price_old", "connection_cost", "internet_speed_mbps", "mobile_data_gb"):
            setattr(self, field_name, numeric(getattr(self, field_name)))
        for field_name in ("tv_channels", "mobile_minutes"):
            setattr(self, field_name, numeric(getattr(self, field_name), whole=True))
        self.name = str(self.name or "").strip()
        allowed = {"internet", "tv", "mobile", "phone", "cinema", "gaming", "other"}
        normalized_services: list[str] = []
        for item in self.services:
            value = str(item).strip().casefold()
            if not value:
                continue
            if value in allowed:
                normalized = value
            elif "интернет" in value:
                normalized = "internet"
            elif "тв" in value or "телевид" in value:
                normalized = "tv"
            elif "мобил" in value or "сим" in value:
                normalized = "mobile"
            elif "телефон" in value or "звон" in value:
                normalized = "phone"
            elif "кино" in value or "cinema" in value:
                normalized = "cinema"
            elif "игр" in value or "gaming" in value:
                normalized = "gaming"
            else:
                normalized = "other"
            if normalized not in normalized_services:
                normalized_services.append(normalized)
        self.services = normalized_services
        if self.technology and any(marker in self.technology.casefold() for marker in ("мбит", "гбит", "mbps", "gbps")):
            self.technology = None
        self.evidence = [str(item).strip() for item in self.evidence if str(item).strip()]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


OUTPUT_COLUMNS = (
    "provider", "city", "region", "source_url", "timestamp", "availability", "name", "variant_id",
    "price", "price_old", "price_period", "connection_cost", "technology",
    "internet_speed_mbps", "tv_channels", "mobile_minutes", "mobile_data_gb", "services",
    "source_record_id", "evidence", "raw_offer", "conditions", "optional_services", "billing_variants",
)


def offer_row(record: SourceRecord, offer: RawOffer) -> dict[str, Any]:
    result = offer.to_dict()
    result.update({
        "provider": record.provider, "city": record.city, "region": record.region,
        "source_url": record.url, "timestamp": record.timestamp,
        "source_record_id": record.record_id,
    })
    return {key: result.get(key) for key in OUTPUT_COLUMNS}
