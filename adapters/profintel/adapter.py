from price_monitor.adapter_api import RawOffer
from price_monitor.adapter_helpers import parse_json, json_items, number, integer, text, first, evidence_fragment, html_text, html_offer_blocks, html_block_facts, iter_json_dicts

def parse(record):
    if record.payload_type == "json":
        parsed = parse_json(record.payload)
        if not isinstance(parsed, list):
            return []
        return [RawOffer(**first(item, "name", "price", "price_period", "services"), raw_offer=item) for item in json_items(parsed)]
    elif record.payload_type == "html":
        blocks = html_offer_blocks(record.payload, record.city)
        if not blocks:
            return []
        return [RawOffer(**html_block_facts(block), evidence=[], raw_offer={"block": block}) for block in blocks]
    return []