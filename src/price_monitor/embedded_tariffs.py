"""Decode Wifire's saved Next Flight data; never execute source JavaScript."""
import json
import math
import re


def embedded_tariff_items(payload):
    if 'initialTariffs' not in payload:
        return None
    chunks = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'self\.__next_f\.push\(', payload):
        try:
            value, _ = decoder.raw_decode(payload[match.end():])
        except ValueError:
            continue
        if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            chunks.append(value[1])
    match = re.search(r'"initialTariffs"\s*:\s*', ''.join(chunks))
    if not match:
        return None
    tariffs, _ = decoder.raw_decode(''.join(chunks)[match.end():])
    if not isinstance(tariffs, dict):
        raise ValueError('initialTariffs must be a mapping')
    result = []
    for identifier, tariff in tariffs.items():
        options = {key: value for key, value in (tariff.get('di_options') or {}).items() if value not in ('', None)}
        speeds = list(options) if options else [str(tariff.get('speed') or '')]
        for speed in speeds:
            if re.fullmatch(r'\d+(?:_\d+)?', speed):
                result.append({'wifire_tariff': tariff, 'tariff_id': identifier, 'house_type': 'flat', 'speed_option': speed})
        if str(tariff.get('isOwnHouse')).casefold() == 'true':
            speed = tariff.get('di_speedOwnHouse') if options else tariff.get('speedOwnHouse')
            if speed not in (None, ''):
                result.append({'wifire_tariff': tariff, 'tariff_id': identifier, 'house_type': 'house', 'speed_option': str(speed)})
    return result


def embedded_offer_facts(source):
    from .html_reducer import _repair_mojibake
    from .adapter_helpers import number
    item = source['wifire_tariff']
    house = source['house_type'] == 'house'
    option = source['speed_option']
    base = number(item.get('price')) or 0
    options = {key: value for key, value in (item.get('di_options') or {}).items() if value not in ('', None)}
    bundle = bool(options)
    sale = item.get('sale') or {}
    percent = number(sale.get('sale')) or 0
    if not bundle:
        old = number(item.get('priceOwnHouse')) if house else base
        current = old if house or not sale.get('text') else old * (1 - percent / 100)
    else:
        extra = number(item.get('di_priceOwnHouse')) or 0
        increment = number(options.get(option)) or 0
        old = base + (extra if house else increment)
        if house:
            current = old
        elif sale.get('text'):
            if sale.get('scope') == 'dominet':
                current = base
            elif sale.get('scope') == '':
                current = base + increment + extra
            else:
                total = base + increment + extra
                current = math.ceil(total - math.floor(total * percent / 100 + 0.5))
        else:
            current = base + increment
    conditions = [f"Тип подключения: {'частный дом' if house else 'квартира'}"]
    if sale.get('text') and not house:
        conditions.append(_repair_mojibake(f"Скидка {sale['text']}: {sale.get('duration') or ''}; {sale.get('date') or ''}"))
    services = ['internet']
    optional = []
    channels = number(item.get('tvchan')) or 0
    if channels:
        if bundle:
            optional.append(f'Телевидение: доступно {channels:g} каналов; подключение за отдельную плату')
        else:
            services.append('tv')
    data = number(item.get('inet'))
    minutes = number(item.get('minutes'))
    if bundle:
        services.append('mobile')
        if data == 999:
            data = None
            conditions.append('Безлимитный мобильный интернет')
    else:
        bonus = number(item.get('mobile_inet'))
        if bonus:
            conditions.append(f'Бонусный мобильный интернет: {bonus:g} ГБ к основному тарифу')
    return {'name': _repair_mojibake(str(item.get('name') or source['tariff_id'])),
            'variant_id': f"{source['tariff_id']}:{source['house_type']}:{option}",
            'price': current, 'price_old': old if old > current else None, 'price_period': 'мес',
            'internet_speed_mbps': number(option.split('_')[0]), 'tv_channels': int(channels) if channels and not bundle else None,
            'mobile_minutes': minutes if bundle else None, 'mobile_data_gb': data if bundle else None,
            'connection_cost': number(item.get('connection_fee')), 'services': services,
            'conditions': conditions, 'optional_services': optional}
