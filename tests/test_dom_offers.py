from unittest import TestCase
from dataclasses import replace

from price_monitor.adapter_helpers import html_block_facts, html_offer_blocks
from price_monitor.dom_offers import explicit_dom_offer_blocks
from price_monitor.source import SourceRecord
from price_monitor.validation import validate_offers


class CommercialCardTests(TestCase):
    def test_mts_advertised_speed_alternatives_remain_available(self):
        payload = '<div class="card__wrapper"><div class="card-title">РИИЛ Плюс</div><label class="universal-regulator__label">500 Мбит/с<span class="mm-web-radio__marker mm-web-radio__marker_selected"></span></label><label class="universal-regulator__label">1 Гбит/с<span class="mm-web-radio__marker"></span></label><div class="price-main">425 ₽/мес</div></div>'
        facts = html_block_facts(html_offer_blocks(payload)[0])
        self.assertEqual(facts['name'], 'РИИЛ Плюс')
        self.assertEqual(facts['internet_speed_mbps'], 1000)

    def test_unselected_tv_is_not_an_included_service(self):
        payload = '<div class="card__wrapper"><div class="card-title">МТС Дома По-твоему</div><label class="universal-regulator__label">200 Мбит/с</label><p>Телевидение</p><label class="universal-regulator__label">230+<span class="mm-web-radio__marker"></span></label><label class="universal-regulator__label">Без ТВ<span class="mm-web-radio__marker mm-web-radio__marker_selected"></span></label><div class="price-main">1000 ₽/мес</div></div>'
        facts = html_block_facts(html_offer_blocks(payload)[0])
        self.assertIsNone(facts['tv_channels'])
        self.assertNotIn('tv', facts['services'])
        self.assertEqual(facts['internet_speed_mbps'], 200)

    def test_universal_cards_keep_all_tariffs_and_their_prices(self):
        def card(name, speed, price, old=''):
            return f'<div class="card card__wrapper"><div class="card-title">{name}</div><p>Для дома</p><p>{speed}</p><p>Мобильная связь 30 ГБ 900 минут</p><div class="price-main"><span>{price}</span><span>₽ / мес</span></div>{old}</div>'
        payload = '<nav>МТС Дома Отлично</nav>' + card('МТС Дома Супер', '500 Мбит/с 1 Гбит/с', 575, '<div class="price-sale">1150 ₽</div>') + card('МТС Дома Отлично', '200 Мбит/с', 425, '<div class="price-sale">850 ₽</div>') + card('РИИЛ Плюс', '500 Мбит/с 1 Гбит/с', 425, '<div class="price-sale">850 ₽</div>')
        facts = [html_block_facts(b) for b in html_offer_blocks(payload)]
        self.assertEqual([(f['name'], f['price'], f['price_old'], f['internet_speed_mbps']) for f in facts], [('МТС Дома Супер', 575, 1150, 1000), ('МТС Дома Отлично', 425, 850, 200), ('РИИЛ Плюс', 425, 850, 1000)])

    def test_universal_mobile_only_card_is_not_home_internet(self):
        payload = '<div class="card__wrapper"><div class="card-title">Мобильный тариф</div><p>30 ГБ 900 минут</p><div class="price-main">425 ₽/мес</div></div>'
        self.assertEqual(explicit_dom_offer_blocks(payload), [])

    def test_prepaid_variants_are_not_monthly_tariff_prices(self):
        facts = html_block_facts('Тариф 100\n100 Мбит/с\n590 рублей в месяц\nПри оплате за 6 месяцев в размере 3300 рублей из расчета - 550 рублей')
        self.assertEqual(facts['price'], 590)
        self.assertEqual(facts['billing_variants'], [{'prepaid_months': 6, 'upfront_price': 3300.0, 'monthly_equivalent': 550.0}])

    def test_gigabit_speed_is_grounded_in_source_units(self):
        html = '<div class="catalog-service-card"><div class="catalog-service-card__name">Интернет 1000</div><p>1 Гбит/с</p><div class="catalog-service-card__footer"><div class="catalog-price__value">500 ₽/мес</div></div></div>'
        block = html_offer_blocks(html)[0]
        facts = html_block_facts(block)
        record = SourceRecord('demo', 'A', None, 'u', 't', 'html', html)
        self.assertTrue(validate_offers(record, [{**facts, 'raw_offer': {'block': block}}]).ok)

    def test_tariff_footer_wins_over_optional_router(self):
        html = '<div class="catalog-service-card"><div class="catalog-service-card__name">Интернет на все 100</div><p>100 Мбит/с</p><div class="catalog-service-card__linked-products"><div class="catalog-service-card__linked-product">Роутер 1000 Мбит/с 1400 ₽/мес</div></div><div class="catalog-service-card__footer"><div class="catalog-price__value">300 ₽/мес</div><div class="catalog-price__original">500 ₽</div></div></div>'
        facts = html_block_facts(html_offer_blocks(html)[0])
        self.assertEqual((facts['name'], facts['price'], facts['price_old'], facts['internet_speed_mbps']), ('Интернет на все 100', 300, 500, 100))

    def test_explicit_annual_offer_keeps_annual_price(self):
        html = '<div class="general-cont" itemtype="https://schema.org/Offer"><a class="name-title">EVO Годовой 100/5100</a><p>100 Мбит/с</p><span>5100 руб./год</span></div>'
        facts = html_block_facts(html_offer_blocks(html)[0])
        self.assertEqual((facts['price'], facts['price_period']), (5100, 'год'))
        record = SourceRecord('demo', 'A', None, 'u', 't', 'html', html)
        report = validate_offers(record, [{**facts, 'raw_offer': {'block': html_offer_blocks(html)[0]}}])
        self.assertTrue(report.ok, report.errors)

    def test_starred_promotion_preserves_base_price_and_terms(self):
        html = '<h3>Ритм</h3><p>100 Мбит/с</p><p>550 ₽</p><p>390 ₽ *</p><p>в месяц</p><h3>Другой</h3><p>300 Мбит/с</p><p>750 ₽</p><p>в месяц</p><p>* Акционная цена действует первые 3 месяца</p>'
        facts = html_block_facts(html_offer_blocks(html)[0])
        self.assertEqual((facts['price'], facts['price_old']), (390, 550))
        self.assertTrue(facts['conditions'])

    def test_both_omkc_heading_styles_and_prices(self):
        html = '<div class="dopprice"><p class="nametp">Свобода 100</p><p class="prc">450 руб./мес.</p></div><div><p class="head-black szha">Свобода 300</p><p class="prc">850 руб./мес.</p></div>'
        facts = [html_block_facts(block) for block in html_offer_blocks(html)]
        self.assertEqual([(f['name'], f['price'], f['internet_speed_mbps']) for f in facts], [('Свобода 100', 450, 100), ('Свобода 300', 850, 300)])

    def test_ttk_cross_line_discount(self):
        html = '<div class="mkdf-tours-standard-item"><h5 class="mkdf-tour-title">Комфорт</h5><p>100 Мбит/с</p><span class="mkdf-tours-item-price" style="text-decoration: line-through">400</span><span class="mkdf-tours-item-price">250<span>руб./мес.</span></span></div>'
        facts = html_block_facts(explicit_dom_offer_blocks(html)[0])
        self.assertEqual((facts['name'], facts['price'], facts['price_old']), ('Комфорт', 250, 400))

    def test_unspecified_period_requires_original_price_span(self):
        html = '<div class="m-tariff"><span class="m-tariff__name">Винтаж</span><span class="m-tariff__speed">80 Мбит/с</span><span class="m-tariff__price-value" data-cost="600">600</span></div>'
        block = explicit_dom_offer_blocks(html)[0]
        facts = html_block_facts(block)
        self.assertEqual((facts['price'], facts['price_period']), (600, None))
        offer = {**facts, 'raw_offer': {'block': block}}
        record = SourceRecord('demo', 'A', None, 'https://example.test', 't', 'html', html)
        report = validate_offers(record, [offer])
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.warnings)
        record = replace(record, payload=html.replace('data-cost="600"', 'data-cost="700"'))
        self.assertFalse(validate_offers(record, [offer]).ok)

    def test_nested_product_is_not_a_combined_offer(self):
        card = '<div itemtype="https://schema.org/Product"><meta itemprop="name" content="Green"><meta itemprop="price" content="405"><div itemprop="additionalProperty"><meta itemprop="name" content="internetSpeed"><meta itemprop="value" content="100"></div><div class="rate-card_card">Green 100 Мбит/с 405 руб./мес.</div></div>'
        blocks = explicit_dom_offer_blocks('<div itemtype="https://schema.org/Product">' + card + '</div>')
        self.assertEqual(len(blocks), 1)

    def test_optional_services_do_not_become_included_services(self):
        facts = html_block_facts('Интернет 100\n100 Мбит/с\n500 руб./мес.\nМожно добавить: ТВ 200 каналов, кинотеатр')
        self.assertIsNone(facts['tv_channels'])
        self.assertTrue(facts['optional_services'])
