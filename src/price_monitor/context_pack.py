from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .html_reducer import reduce_html
from .adapter_helpers import html_block_facts, html_offer_blocks
from .source import SourceRecord


@dataclass(slots=True)
class ContextPack:
    text: str
    chars: int
    records: int


def build_context_pack(records: list[SourceRecord], *, max_chars: int = 24_000) -> ContextPack:
    header = (
        "INPUT: SourceRecord(provider, city, region, url, timestamp, payload_type, payload).\n"
        "Implement parse(record) -> list[RawOffer]. Return [] for non-catalog, unavailable, or empty pages.\n"
        "Never invent facts. raw_offer preserves the source dict/compact source fragment.\n"
    )
    if not records:
        return ContextPack(header[:max_chars], min(len(header), max_chars), 0)
    remaining = max(0, max_chars - len(header))
    per_record = max(700, remaining // len(records))
    sections: list[str] = [header]
    for index, record in enumerate(records, 1):
        meta = json.dumps(record.metadata(), ensure_ascii=False, sort_keys=True)
        if record.payload_type == "json":
            evidence = _json_context(record.payload, per_record - len(meta) - 80)
        else:
            blocks = html_offer_blocks(record.payload, record.city, per_record - len(meta) - 80)
            if blocks:
                rendered = []
                for block_index, block in enumerate(blocks, 1):
                    facts = json.dumps(html_block_facts(block), ensure_ascii=False, sort_keys=True)
                    rendered.append(f"[OFFER BLOCK {block_index}]\n{block}\nDETERMINISTIC FACTS: {facts}")
                evidence = "\n".join(rendered)[:per_record - len(meta) - 80]
            else:
                evidence = reduce_html(record.payload, city=record.city, max_chars=per_record - len(meta) - 80).text
        sections.append(f"\n[TRAIN RECORD {index}]\n{meta}\n{evidence}")
    text = "".join(sections)[:max_chars]
    return ContextPack(text, len(text), len(records))


def relevant_fragment(record: SourceRecord, *, max_chars: int = 3000) -> str:
    if record.payload_type == "json":
        return _json_context(record.payload, max_chars)
    blocks = html_offer_blocks(record.payload, record.city, max_chars)
    if blocks:
        return "\n".join(f"[OFFER BLOCK {index}]\n{block}" for index, block in enumerate(blocks, 1))[:max_chars]
    return reduce_html(record.payload, city=record.city, max_chars=max_chars).text


def _json_context(payload: str, budget: int) -> str:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        return f"INVALID JSON: {exc}; prefix={payload[:max(0, budget-40)]}"
    lines = ["JSON SCHEMA: " + json.dumps(_schema(value), ensure_ascii=False, sort_keys=True)]
    items = value if isinstance(value, list) else next((child for child in value.values() if isinstance(child, list)), []) if isinstance(value, dict) else []
    for index, item in enumerate(items[:3]):
        # Large provider objects can contain legal texts and media blobs. Preserve all
        # scalar business fields and representative nested values deterministically.
        rendered = json.dumps(_compact_item(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if sum(len(line) + 1 for line in lines) + len(rendered) > budget:
            break
        lines.append(f"ITEM {index}: {rendered}")
    return "\n".join(lines)[:budget]


def _compact_item(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if isinstance(child, str) and len(child) > 500:
                result[str(key)] = child[:500] + "…"
            else:
                result[str(key)] = _compact_item(child, depth + 1)
        return result
    if isinstance(value, list):
        return [_compact_item(child, depth + 1) for child in value[:3]]
    return value


def _schema(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): _schema(child, depth + 1) for key, child in list(value.items())[:40]}
    if isinstance(value, list):
        return {"type": "array", "count": len(value), "item": _schema(value[0], depth + 1) if value else None}
    return type(value).__name__
