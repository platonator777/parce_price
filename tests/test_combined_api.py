import csv
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from price_monitor.source import SourceRecord, iter_source_records
from price_monitor.adapter_helpers import json_items, json_offer_facts
from price_monitor.adapter_helpers import html_offer_blocks, html_block_facts
from price_monitor.adapter_api import RawOffer
from price_monitor.adapter_runtime import run_adapter
from price_monitor.adapter_runner import run_saved_adapter
from price_monitor.validation import validate_offers
from price_monitor.embedded_tariffs import embedded_tariff_items, embedded_offer_facts


MTS = {'title': 'Домашний 1000', 'totalPrice': {'value': 700, 'oldValue': 1400,
       'unit': {'quotaPeriod': 'monthly'}}, 'connectionFee': {'value': 0},
       'internetTariff': {'speed': {'numValue': 1, 'quotaUnit': 'gbit'}},
       'tvPackage': {'channelsCount': 216}, 'productFeatureGroups': [
           {'groupType': 'Mobile', 'features': [
               {'baseParameter': 'InternetPackage', 'numValue': 30},
               {'baseParameter': 'MinutesPackage', 'numValue': 850}]}]}
BEELINE = {'title': 'интернет и тв', 'tariffName': 'bee HIT', 'tariffType': 'Preset',
           'price': {'fee': 470, 'oldFee': 950, 'feeUnit': '₽/мес', 'promo': 'скидка на 2 месяца'},
           'parameters': [{'name': 'speed', 'value': 100}, {'name': 'kinopoisk', 'value': 200}],
           'mobileTariffTitle': 'bee HIT', 'mobileParams': [
               {'name': 'InternetPackage', 'value': 100, 'isUnlimited': True},
               {'name': 'MinutePackage', 'value': 700}]}


def beeline_page(item):
    return {'blocks': [{'alias': 'news', 'data': {'items': [{'title': 'news'}]}},
                       {'alias': 'catalog', 'data': {'tariffs': {'home': [item]}}}]}


class CombinedApiTests(unittest.TestCase):
    def test_wifire_flight_tariffs_speed_prices_and_house_formula(self):
        tariffs = {
            '201': {'name': '#ДляДома Интернет', 'price': '520', 'speed': '200', 'priceOwnHouse': '450',
                    'speedOwnHouse': '100', 'isOwnHouse': 'true', 'di_options': {'100': ''},
                    'sale': {'sale': '50', 'text': '50%', 'duration': '2 месяца'}, 'tvchan': '0'},
            '301': {'name': 'Минимум', 'price': '400', 'speed': '', 'isOwnHouse': 'true',
                    'di_priceOwnHouse': '100', 'di_speedOwnHouse': '500', 'di_options': {'100': '400', '500': '500'},
                    'sale': {'sale': 100, 'text': '100%', 'scope': 'full'}, 'minutes': '200', 'inet': '10'},
        }
        flight = '20:' + json.dumps({'initialTariffs': tariffs}, ensure_ascii=False)
        # Flight strings can be split across chunks; no JS execution is needed.
        html = ''.join('<script>self.__next_f.push(' + json.dumps([1, part]) + ')</script>'
                       for part in (flight[:45], flight[45:]))
        variants = embedded_tariff_items(html)
        facts = [embedded_offer_facts(v) for v in variants]
        self.assertEqual([(v['variant_id'], v['internet_speed_mbps'], v['price'], v['price_old']) for v in facts],
                         [('201:flat:200', 200, 260, 520), ('201:house:100', 100, 450, None),
                          ('301:flat:100', 100, 0, 800), ('301:flat:500', 500, 0, 900), ('301:house:500', 500, 500, None)])
        record = SourceRecord('wifire_ru', 'C', None, 'u', 't', 'json', json.dumps(variants))
        result = run_adapter(ROOT / 'adapters/wifireru/adapter.py', record)
        self.assertTrue(result.ok, result.stderr)
        self.assertTrue(validate_offers(record, result.offers).ok)
        self.assertFalse(validate_offers(record, result.offers[:-1]).ok)

    def test_letai_card_boundaries_keep_adsl_and_do_not_borrow_modal_names(self):
        html = '<main>'
        for name, speed, price in [('Летай. Оранжевый 100', 100, 675), ('Летай. Оранжевый 50', 50, 525), ('Летай. Оранжевый. 6', 6, 600)]:
            html += (f'<div class="rates-list-internet__item"><div class="rates-list-internet__item-title">{name}</div>'
                     f'<div>Интернет до {speed} Мбит/сек.</div><div>Аренда роутера - 150 ₽/мес</div>'
                     f'<div class="rates-list-internet__item-price"><span>{price}</span>₽/мес</div></div>')
        html += '</main><div>Тариф «Много интернета» 240 ₽/мес</div>'
        facts = [html_block_facts(b) for b in html_offer_blocks(html)]
        self.assertEqual([(f['name'], f['internet_speed_mbps'], f['price']) for f in facts],
                         [('Летай. Оранжевый 100', 100, 675), ('Летай. Оранжевый 50', 50, 525), ('Летай. Оранжевый. 6', 6, 600)])

    def test_mts_text_unlimited_is_preserved_without_boolean_flag(self):
        item = json.loads(json.dumps(MTS))
        item['productFeatureGroups'][0]['features'][0] = {'baseParameter': 'InternetPackage', 'numValue': None,
                                                        'isUnlimited': None, 'value': 'Полный безлимит ГБ'}
        facts = json_offer_facts(item)
        self.assertIsNone(facts.get('mobile_data_gb'))
        self.assertIn('Полный безлимит ГБ', facts['conditions'])

    def test_mts_slider_expands_every_option_without_default_duplicate(self):
        item = json.loads(json.dumps(MTS))
        item['internetOptions'] = []
        for identifier, speed, current, old in [(11, 200, 700, 1400), (12, 500, 850, 1550), (13, 1, 950, 1650)]:
            item['internetOptions'].append({'id': identifier,
                'internetSpeed': {'numValue': speed, 'quotaUnit': 'gbit' if speed == 1 else 'mbit'},
                'totalPrice': {'value': current, 'oldValue': old, 'unit': {'quotaPeriod': 'monthly'}},
                'subscriptionFee': {'numValue': old, 'quotaPeriod': 'monthly'},
                'discountFee': {'numValue': current, 'quotaPeriod': 'monthly'}})
        record = SourceRecord('mts', 'C', None, 'u', 't', 'json', json.dumps([item]))
        result = run_adapter(ROOT / 'adapters/mts/adapter.py', record)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual([(o['variant_id'], o['internet_speed_mbps'], o['price'], o['price_old']) for o in result.offers],
                         [('11', 200, 700, 1400), ('12', 500, 850, 1550), ('13', 1000, 950, 1650)])
        self.assertTrue(validate_offers(record, result.offers).ok)
        self.assertFalse(validate_offers(record, result.offers[:2]).ok)
        self.assertFalse(validate_offers(record, result.offers + result.offers[:1]).ok)
        self.assertEqual(result.offers[2]['billing_variants'][0]['price'], 1650)
        self.assertEqual(result.offers[2]['tv_channels'], 216)
        self.assertEqual(result.offers[2]['mobile_minutes'], 850)
        same = dict(item['internetOptions'][-1], id=14)
        item['internetOptions'].append(same)
        record = SourceRecord('mts', 'C', None, 'u', 't', 'json', json.dumps([item]))
        result = run_adapter(ROOT / 'adapters/mts/adapter.py', record)
        self.assertEqual(len(result.offers), 4)
        self.assertTrue(validate_offers(record, result.offers).ok)

    def test_native_fields_and_grounding(self):
        for provider, data in [('mts', [MTS]), ('beeline', beeline_page(BEELINE))]:
            record = SourceRecord(provider, 'Москва', None, 'https://test', 't', 'json', json.dumps(data))
            result = run_adapter(ROOT / 'adapters' / provider / 'adapter.py', record)
            self.assertTrue(result.ok, result.stderr)
            self.assertEqual(len(result.offers), 1)
            self.assertTrue(validate_offers(record, result.offers).ok)
            offer = result.offers[0]
            self.assertEqual(offer['price'], 700 if provider == 'mts' else 470)
            self.assertEqual(offer['price_old'], 1400 if provider == 'mts' else 950)
            self.assertEqual(offer['internet_speed_mbps'], 1000 if provider == 'mts' else 100)
            self.assertEqual(offer['mobile_minutes'], 850 if provider == 'mts' else 700)
            self.assertEqual(offer['mobile_data_gb'], 30 if provider == 'mts' else None)
            offer['price'] += 1
            self.assertFalse(validate_offers(record, result.offers).ok)

    def test_optional_sim_old_fee_sentinel_and_unknown_period(self):
        item = dict(BEELINE, mobileTariffTitle=None, price={'fee': 500, 'oldFee': 0, 'feeUnit': '₽'})
        facts = RawOffer(**json_offer_facts(item)).to_dict()
        self.assertIsNone(facts['price_old'])
        self.assertIsNone(facts['price_period'])
        self.assertNotIn('mobile', facts['services'])
        self.assertIsNone(facts['mobile_minutes'])
        self.assertTrue(facts['optional_services'])
        self.assertEqual(json_items(beeline_page(item)), [item])

    def test_mixed_parquet_routing_and_collection_errors(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'combined.parquet'
            pq.write_table(pa.Table.from_pylist([
                {'provider': 'mts', 'url': 'https://mts/1', 'content': json.dumps([MTS]), 'status': 'success'},
                {'provider': 'beeline', 'url': 'https://beeline/1', 'content': json.dumps(beeline_page(BEELINE)), 'status': 'success'},
                {'provider': 'wifire_ru', 'url': 'https://wifire/1', 'content': '<html></html>', 'status': 'success'},
                {'provider': 'beeline', 'url': 'https://beeline/2', 'content': '', 'status': 'error'},
            ]), source)
            records = list(iter_source_records(source))
            self.assertEqual([r.payload_type for r in records], ['json', 'json', 'html', 'html'])
            stats = run_saved_adapter(source, ROOT / 'adapters/ones/adapter.py', root / 'out', adapters_dir=ROOT / 'adapters')
            self.assertEqual((stats.records_seen, stats.records_succeeded, stats.records_failed, stats.offers), (4, 3, 1, 2))
            with (root / 'out/offers.csv').open(encoding='utf-8-sig') as handle:
                self.assertEqual([r['provider'] for r in csv.DictReader(handle)], ['mts', 'beeline'])
            self.assertIn('source collection failed', (root / 'out/errors.jsonl').read_text())
            selected = run_saved_adapter(source, ROOT / 'adapters/mts/adapter.py', root / 'mts', only_provider='mts')
            self.assertEqual((selected.records_seen, selected.offers, selected.records_failed), (1, 1, 0))
            cli = subprocess.run([sys.executable, str(ROOT / 'run_pipeline.py'), '--provider', 'mts',
                                  '--input', str(source), '--output', str(root / 'cli')],
                                 cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            report = json.loads((root / 'cli/mts/quality_report.json').read_text())
            self.assertEqual((report['records_seen'], report['offers']), (1, 1))

    def test_legacy_and_per_row_payload_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'rows.csv'
            with source.open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['html', 'response_json', 'content'])
                writer.writeheader()
                writer.writerows([{'html': '<html>x</html>'}, {'response_json': '[]'}, {'content': '[]'}])
            self.assertEqual([r.payload_type for r in iter_source_records(source)], ['html', 'json', 'json'])


if __name__ == '__main__':
    unittest.main()
