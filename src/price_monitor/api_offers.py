"""Mappings for saved MTS and Beeline catalogue API responses."""
from __future__ import annotations

from html import unescape
from typing import Any


def api_kind(item: dict) -> str | None:
    if isinstance(item.get('wifire_tariff'), dict) and 'speed_option' in item:
        return 'wifire'
    if isinstance(item.get('offer'), dict) and isinstance(item.get('internet_option'), dict):
        return api_kind(item['offer'])
    if isinstance(item.get('totalPrice'), dict) and 'productFeatureGroups' in item:
        return 'mts'
    if isinstance(item.get('price'), dict) and 'parameters' in item and 'tariffType' in item:
        return 'beeline'
    return None


def beeline_items(value: Any) -> list[dict] | None:
    if not isinstance(value, dict) or not isinstance(value.get('blocks'), list):
        return None
    result = []
    for block in value['blocks']:
        if block.get('alias') != 'catalog':
            continue
        tariffs = (block.get('data') or {}).get('tariffs', {})
        groups = tariffs.values() if isinstance(tariffs, dict) else [tariffs]
        for group in groups:
            if isinstance(group, list):
                result.extend(item for item in group if isinstance(item, dict))
    return result


def api_offer_facts(item: dict) -> dict:
    # Import here to avoid a circular dependency with json_items.
    from .adapter_helpers import number, text
    if api_kind(item) == 'wifire':
        from .embedded_tariffs import embedded_offer_facts
        return embedded_offer_facts(item)

    variant = item.get('internet_option') if isinstance(item.get('offer'), dict) else None
    if variant is not None:
        parent = item['offer']
        item = {**parent, 'totalPrice': variant['totalPrice'],
                'internetTariff': {**(parent.get('internetTariff') or {}), 'speed': variant['internetSpeed']},
                'subscriptionFee': variant.get('subscriptionFee'), 'discountFee': variant.get('discountFee'),
                'subscriptionFeeAnnotation': variant.get('subscriptionFeeAnnotation')}
    kind = api_kind(item)
    if kind is None:
        # Legacy flat JSON catalogues keep their existing field convention.
        aliases = {'price_period': 'pricePeriod', 'connection_cost': 'costConnection',
                   'technology': 'tech', 'internet_speed_mbps': 'speedVal'}
        keys = ('name', 'price', 'price_old', 'price_period', 'connection_cost', 'technology',
                'internet_speed_mbps', 'tv_channels', 'mobile_minutes', 'mobile_data_gb', 'services')
        return {key: item.get(key, item.get(aliases.get(key))) for key in keys if key in item or aliases.get(key) in item}

    facts = {'name': text(item.get('titleForCard') or item.get('title')) if kind == 'mts'
             else text(item.get('tariffName') or item.get('mobileTariffTitle') or item.get('title')),
             'services': [], 'conditions': [], 'optional_services': [], 'billing_variants': []}
    if kind == 'mts':
        if variant is not None:
            facts['variant_id'] = str(variant['id'])
        price = item['totalPrice']
        unit = price.get('unit') or {}
        facts.update(price=number(price.get('value')), price_old=number(price.get('oldValue')),
                     price_period='мес' if unit.get('quotaPeriod') == 'monthly' else None,
                     connection_cost=number((item.get('connectionFee') or {}).get('value')))
        speed = (item.get('internetTariff') or {}).get('speed') or {}
        if speed:
            value = number(speed.get('numValue'))
            facts['internet_speed_mbps'] = value * 1000 if value is not None and speed.get('quotaUnit') == 'gbit' else value
            facts['services'].append('internet')
        tv = item.get('tvPackage') or {}
        if tv:
            facts['tv_channels'] = number(tv.get('channelsCount'))
            facts['services'].append('tv')
        if item.get('phoneTariff'):
            facts['services'].append('phone')
        for group in item.get('productFeatureGroups') or []:
            if group.get('groupType') != 'Mobile':
                continue
            facts['services'].append('mobile')
            for feature in group.get('features') or []:
                code = feature.get('baseParameter')
                if feature.get('isUnlimited') or (feature.get('numValue') is None and feature.get('value')):
                    facts['conditions'].append(unescape(str(feature.get('value') or feature.get('title') or code)))
                elif code in {'MinutesPackage', 'InternetPackage'}:
                    key = 'mobile_minutes' if code == 'MinutesPackage' else 'mobile_data_gb'
                    facts[key] = number(feature.get('numValue'))
        annotation = item.get('subscriptionFeeAnnotation')
        if annotation:
            facts['conditions'].append(annotation)
        facts['conditions'].extend(b['title'] for b in price.get('badges') or [] if b.get('title'))
        if variant is not None:
            facts['conditions'].extend(b['title'] for b in (variant.get('offerTotalPrice') or {}).get('badges') or [] if b.get('title'))
            facts['conditions'] = list(dict.fromkeys(facts['conditions']))
        for key in ('subscriptionFee', 'discountFee'):
            fee = item.get(key) or {}
            if fee.get('numValue') is not None:
                facts['billing_variants'].append({'type': key, 'price': fee['numValue'],
                    'price_period': 'мес' if fee.get('quotaPeriod') == 'monthly' else None})
    else:
        price = item['price']
        value, old = number(price.get('fee')), number(price.get('oldFee'))
        facts.update(price=value, price_old=old if old is not None and old > 0 and (value is None or old >= value) else None,
                     price_period='мес' if 'мес' in str(price.get('feeUnit', '')) else None)
        if price.get('promo'):
            facts['conditions'].append(price['promo'])
        for param in item.get('parameters') or []:
            if param.get('name') == 'speed':
                facts['internet_speed_mbps'] = number(param.get('value'))
                facts['services'].append('internet')
            elif param.get('name') in {'channels', 'kinopoisk'}:
                facts['tv_channels'] = number(param.get('value'))
                facts['services'].append('tv')
        # Gift SIMs with a later separate fee are optional, not included mobile service.
        included_mobile = bool(item.get('mobileTariffTitle'))
        if included_mobile:
            facts['services'].append('mobile')
        for param in item.get('mobileParams') or []:
            code = param.get('name')
            if param.get('isUnlimited'):
                facts['conditions'].append(f"{code}: безлимит")
            elif included_mobile and code in {'MinutePackage', 'InternetPackage'}:
                facts['mobile_minutes' if code == 'MinutePackage' else 'mobile_data_gb'] = number(param.get('value'))
            elif code in {'AdditionalPackageGb', 'AdditionalPackageMin'}:
                facts['conditions'].append(f"{code}: +{param.get('value')} {param.get('unit') or ''}".strip())
        if not included_mobile and item.get('mobileParams'):
            facts['optional_services'].append('Подарочная SIM: условия и последующая плата в raw_offer.mobileParams')
        yandex = item.get('yandexPrice')
        if yandex:
            facts['optional_services'].append('Яндекс Плюс')
            facts['billing_variants'].append({'type': 'yandexPrice', 'price': yandex.get('fee'),
                'price_old': yandex.get('oldFee'), 'price_period': facts['price_period'], 'conditions': yandex.get('promo')})
    return facts


def api_offer_variants(item: dict) -> list[dict]:
    """Slider options replace the default card, rather than adding a duplicate base."""
    if api_kind(item) == 'mts' and item.get('internetOptions'):
        return [{'offer': item, 'internet_option': option} for option in item['internetOptions']]
    return [item]
