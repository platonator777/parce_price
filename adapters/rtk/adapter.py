import json
import re
import math
import html
from price_monitor.adapter_api import RawOffer
from price_monitor.adapter_helpers import parse_json, json_items, number, integer, text, first, evidence_fragment, html_text, html_offer_blocks, iter_json_dicts

def parse(record):
    if record.payload_type == "json":
        parsed = parse_json(record.payload)
        if not parsed or not isinstance(parsed, list):
            return []
        offers = []
        for item in json_items(parsed):
            if not item or not isinstance(item, dict):
                continue
            name = text(item.get("name"))
            if not name:
                continue
            price = number(item.get("price"))
            price_old = number(item.get("price_old"))
            price_period = text(item.get("pricePeriod"))
            connection_cost = number(item.get("costConnection"))
            technology = text(item.get("tech"))
            products = item.get("products", [])
            internet_speed = None
            for product in products:
                if product.get("code") == "Main_Internet_service":
                    internet_speed = number(product.get("speedVal"))
                    break
            tv_channels = None
            mobile_minutes = None
            mobile_data_gb = None
            services = []
            for product in products:
                code = text(product.get("code"))
                if code == "Main_Internet_service":
                    services.append("internet")
                elif code == "SIM-card_main":
                    services.append("mobile")
                elif code == "Unlimited_access":
                    services.append("tv")
            evidence = []
            for key in ["price", "price_old", "pricePeriod", "costConnection", "tech", "products"]:
                val = text(item.get(key))
                if val:
                    evidence.append(evidence_fragment(record.payload, val))
            raw_offer = item
            offers.append(RawOffer(
                name=name,
                availability="available",
                price=price,
                price_old=price_old,
                price_period=price_period,
                connection_cost=connection_cost,
                technology=technology,
                internet_speed_mbps=internet_speed,
                tv_channels=tv_channels,
                mobile_minutes=mobile_minutes,
                mobile_data_gb=mobile_data_gb,
                services=services,
                evidence=evidence,
                raw_offer=raw_offer
            ))
        return offers
    elif record.payload_type == "html":
        blocks = html_offer_blocks(record.payload, record.city)
        if not blocks:
            return []
        offers = []
        for block in blocks:
            lines = block.splitlines()
            name = None
            price = None
            price_old = None
            price_period = None
            connection_cost = None
            technology = None
            internet_speed = None
            tv_channels = None
            mobile_minutes = None
            mobile_data_gb = None
            services = []
            evidence = []
            for line in lines:
                if "Хит сезона" in line:
                    name = text(line)
                elif "200 Мбит/с" in line:
                    internet_speed = 200
                elif "Мобильная связь" in line:
                    services.append("mobile")
                elif "Телевидение" in line:
                    services.append("tv")
                elif "Скидка" in line:
                    evidence.append(evidence_fragment(block, line))
                elif "месяц" in line and "руб" in line:
                    price = number(line)
                    price_period = "мес"
                elif "Старая цена" in line:
                    price_old = number(line)
                elif "Пакет услуг" in line:
                    connection_cost = number(line)
                elif "Технология" in line:
                    technology = text(line)
            if name and price and price_period and connection_cost and technology and internet_speed:
                offers.append(RawOffer(
                    name=name,
                    availability="available",
                    price=price,
                    price_old=price_old,
                    price_period=price_period,
                    connection_cost=connection_cost,
                    technology=technology,
                    internet_speed_mbps=internet_speed,
                    tv_channels=tv_channels,
                    mobile_minutes=mobile_minutes,
                    mobile_data_gb=mobile_data_gb,
                    services=services,
                    evidence=evidence,
                    raw_offer={}
                ))
        return offers
    return []