from price_monitor.adapter_api import RawOffer
from price_monitor.adapter_helpers import parse_json, json_items, number, integer, text, first, evidence_fragment, html_text, html_offer_blocks, html_block_facts, iter_json_dicts

def parse(record):
    if record.payload_type == "json":
        parsed = parse_json(record.payload)
        if not isinstance(parsed, list):
            return []
        return [RawOffer(**{
            "name": text(first(item, "name", "product")),
            "price": number(first(item, "price", "cost")),
            "price_period": text(first(item, "period", "duration")),
            "price_old": number(first(item, "old_price", "discounted_price")),
            "internet_speed_mbps": number(first(item, "speed", "internet_speed")),
            "services": first(item, "services", "products", default=[]),
            "raw_offer": item,
            "evidence": [evidence_fragment(record.payload, text(line)) for line in item.get("lines", []) if "₽" in line]
        }) for item in parsed if all(k in item for k in ["name", "price", "period", "speed", "services"])]
    else:
        blocks = html_offer_blocks(record.payload, record.city)
        return [RawOffer(**html_block_facts(block), evidence=[], raw_offer={"block": block}) for block in blocks if all(k in html_block_facts(block) for k in ["name", "price", "price_period", "internet_speed_mbps", "services"])]