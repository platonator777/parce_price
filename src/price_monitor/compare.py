from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TRACKED_FIELDS = ("price", "price_old", "costConnection", "tech", "packagesTypes", "products", "stockData", "lcs")


def load_cards(path: Path) -> dict[int, dict[str, Any]]:
    cards: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            offer_id = value.get("offerId")
            if isinstance(offer_id, int):
                cards[offer_id] = value
    return cards


def compare_snapshots(old_path: Path, new_path: Path) -> list[dict[str, Any]]:
    old = load_cards(old_path)
    new = load_cards(new_path)
    changes: list[dict[str, Any]] = []
    for offer_id in sorted(old.keys() | new.keys()):
        before = old.get(offer_id)
        after = new.get(offer_id)
        sample = after or before or {}
        identity = {
            "offerId": offer_id,
            "provider": sample.get("_provider") or sample.get("provider"),
            "city": sample.get("_city") or sample.get("city"),
            "name": sample.get("name"),
        }
        if before is None:
            changes.append({"type": "added", **identity, "after": _tracked(after or {})})
        elif after is None:
            changes.append({"type": "removed", **identity, "before": _tracked(before)})
        else:
            fields = {
                field: {"before": before.get(field), "after": after.get(field)}
                for field in TRACKED_FIELDS if before.get(field) != after.get(field)
            }
            if fields:
                changes.append({"type": "changed", **identity, "fields": fields})
    return changes


def _tracked(card: dict[str, Any]) -> dict[str, Any]:
    return {field: card.get(field) for field in TRACKED_FIELDS}
