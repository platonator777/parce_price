from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

from .source import SourceRecord


PRICE = re.compile(r"(?:\d[\d\s]{1,8}\s*(?:₽|руб)|(?:price|cost)\W{0,20}\d)", re.I)
SPEED = re.compile(r"(?:\d+\s*(?:мбит|гбит|mbps|gbps)|speed)", re.I)
TV = re.compile(r"(?:телевид|\bтв\b|канал|television)", re.I)
MOBILE = re.compile(r"(?:мобильн|минут|гигабайт|mobile)", re.I)
UNAVAILABLE = re.compile(r"(?:недоступ|нет технической возможности|unavailable)", re.I)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    key: str
    features: dict[str, object]


@dataclass(slots=True)
class SamplePlan:
    train: list[SourceRecord]
    holdout: list[SourceRecord]
    smoke: list[SourceRecord]
    fingerprints: dict[str, dict[str, object]]
    records_seen: int


def normalized_path(url: str) -> str:
    path = urlsplit(url).path.casefold()
    path = re.sub(r"/-[^/]+-(?=/|$)", "/:slug", path)
    path = re.sub(r"/[0-9a-f-]{8,}", "/:id", path)
    path = re.sub(r"/\d+", "/:n", path)
    return path.rstrip("/") or "/"


def fingerprint(record: SourceRecord) -> Fingerprint:
    host = (urlsplit(record.url).hostname or "").casefold()
    host_parts = host.split(".")
    base: dict[str, object] = {
        "payload_type": record.payload_type,
        "domain": ".".join(host_parts[-2:]) if len(host_parts) >= 2 else host,
        "path": normalized_path(record.url),
        "size_bucket": min(len(record.payload) // 100_000, 15),
    }
    if record.payload_type == "json":
        try:
            value = json.loads(record.payload)
            base["root"] = type(value).__name__
            if isinstance(value, dict):
                base["top_keys"] = sorted(map(str, value.keys()))[:20]
                items = next((v for v in value.values() if isinstance(v, list)), [])
            else:
                items = value if isinstance(value, list) else []
            base["item_count_bucket"] = min(len(items), 10)
            keys: set[str] = set()
            for item in items[:3]:
                if isinstance(item, dict):
                    keys.update(map(str, item.keys()))
            base["item_keys"] = sorted(keys)[:30]
        except (json.JSONDecodeError, TypeError):
            base["root"] = "invalid"
    else:
        sample = record.payload[:200_000]
        title = re.search(r"<title[^>]*>(.*?)</title", sample, re.I | re.S)
        headings = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", sample, re.I | re.S)[:5]
        base.update({
            "title_shape": _shape(title.group(1) if title else "", record.city),
            "headings": [_shape(re.sub(r"<[^>]+>", " ", item), record.city) for item in headings],
            "forms": len(re.findall(r"<form\b", sample, re.I)),
            "json_scripts": len(re.findall(r"<script[^>]+(?:json|__next_data__|initial)", sample, re.I)),
            "signals": [name for name, regex in (("price", PRICE), ("speed", SPEED), ("tv", TV), ("mobile", MOBILE), ("unavailable", UNAVAILABLE)) if regex.search(sample)],
        })
    encoded = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib
    return Fingerprint(hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16], base)


def select_samples(records, *, train_count: int = 4, holdout_count: int = 3, smoke_count: int = 2, max_groups: int = 24) -> SamplePlan:
    """Round-robin across structural groups; each record belongs to exactly one split."""
    groups: OrderedDict[str, list[SourceRecord]] = OrderedDict()
    descriptions: dict[str, dict[str, object]] = {}
    seen = 0
    # A homogeneous catalog still needs enough records for all disjoint splits.
    per_group = max(train_count + holdout_count + smoke_count, 2)
    for record in records:
        seen += 1
        fp = fingerprint(record)
        if fp.key not in groups and len(groups) >= max_groups:
            continue
        descriptions[fp.key] = fp.features
        bucket = groups.setdefault(fp.key, [])
        if len(bucket) < per_group:
            bucket.append(record)
    ordered: list[SourceRecord] = []
    offset = 0
    while True:
        added = False
        for bucket in groups.values():
            if offset < len(bucket):
                ordered.append(bucket[offset]); added = True
        if not added:
            break
        offset += 1
    train = ordered[:train_count]
    holdout = ordered[train_count:train_count + holdout_count]
    smoke = ordered[train_count + holdout_count:train_count + holdout_count + smoke_count]
    return SamplePlan(train, holdout, smoke, descriptions, seen)


def _shape(value: str, city: str = "") -> str:
    if city:
        value = re.sub(re.escape(city), "<city>", value, flags=re.I)
    value = re.sub(r"\d+", "#", re.sub(r"\s+", " ", value)).strip().casefold()
    return value[:120]
