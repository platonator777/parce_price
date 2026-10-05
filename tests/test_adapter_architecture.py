from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from price_monitor.adapter_runner import run_saved_adapter
from price_monitor.adapter_runtime import check_adapter_source, extract_python_code, run_adapter
from price_monitor.context_pack import build_context_pack
from price_monitor.sampling import fingerprint, select_samples
from price_monitor.source import SourceRecord, iter_source_records
from price_monitor.validation import validate_offers
from price_monitor.adapter_helpers import html_block_facts, html_offer_blocks, html_text, json_items
from price_monitor.adapter_api import RawOffer
from price_monitor.quality_gate import check_adapter_quality, write_gold_template


def record(payload: str, payload_type: str = "json", *, url: str = "https://example.test/catalog", city: str = "A") -> SourceRecord:
    return SourceRecord("demo", city, None, url, "2026-01-01", payload_type, payload)  # type: ignore[arg-type]


class SourceLoaderTests(TestCase):
    def test_mts_without_provider_and_html_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mts_results.csv"
            path.write_text('url,city,timestamp,html,status\nhttps://mts.ru/home,Москва,t,"<html>x</html>",success\n', encoding="utf-8")
            item = next(iter_source_records(path))
            self.assertEqual(item.provider, "mts")
            self.assertEqual(item.payload_type, "html")

    def test_rtk_extra_index_and_json_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rtk.csv"
            path.write_text(',provider,url,timestamp,region,city,response_json\n0,rtk,u,t,R,C,"[{""name"":""X""}]"\n', encoding="utf-8")
            item = next(iter_source_records(path))
            self.assertEqual((item.provider, item.region, item.payload_type), ("rtk", "R", "json"))
            self.assertEqual(json.loads(item.payload)[0]["name"], "X")

    def test_site_export_aliases_and_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo_results.csv"
            path.write_text('city_name,region_name,html_content\nМосква,ЦФО,"<html>x</html>"\n', encoding="utf-8")
            item = next(iter_source_records(path))
            self.assertEqual((item.city, item.region, item.payload), ("Москва", "ЦФО", "<html>x</html>"))
            self.assertTrue(item.url.startswith("file:"))
            self.assertTrue(item.timestamp)


class SamplingAndContextTests(TestCase):
    def test_json_array_empty_and_list_wrapper_fingerprints(self) -> None:
        values = [record('[{"name":"A"}]'), record("[]"), record('{"list":[{"name":"A"}]}')]
        keys = {fingerprint(item).key for item in values}
        self.assertEqual(len(keys), 3)
        self.assertEqual(json_items([]), [])
        self.assertEqual(json_items({"list": [{"id": 1}]}), [{"id": 1}])

    def test_distinct_groups_and_disjoint_splits(self) -> None:
        values = [record("[]", url=f"https://e.test/{kind}/{index}", city=str(index)) for index, kind in enumerate(("catalog", "mobile", "service", "catalog", "mobile", "service"))]
        plan = select_samples(values, train_count=2, holdout_count=2, smoke_count=2)
        ids = [item.record_id for item in plan.train + plan.holdout + plan.smoke]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(plan.fingerprints), 3)

    def test_context_budget(self) -> None:
        pack = build_context_pack([record(json.dumps([{"name": "A", "blob": "x" * 10000}]))], max_chars=1200)
        self.assertLessEqual(pack.chars, 1200)
        self.assertIn("JSON SCHEMA", pack.text)


class AdapterRuntimeTests(TestCase):
    def test_extracts_only_code(self) -> None:
        value = extract_python_code("Here\n```python\nfrom price_monitor.adapter_api import RawOffer\ndef parse(record): return []\n```\n")
        self.assertTrue(value.startswith("from price_monitor"))
        self.assertNotIn("```", value)

    def test_ast_allowlist(self) -> None:
        good = "from price_monitor.adapter_api import RawOffer\ndef parse(record):\n return []\n"
        bad = "import subprocess\ndef parse(record):\n return subprocess.run(['x'])\n"
        self.assertTrue(check_adapter_source(good).ok)
        self.assertFalse(check_adapter_source(bad).ok)

    def test_timeout_and_adapter_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.py"
            path.write_text("def parse(record):\n while True: pass\n", encoding="utf-8")
            self.assertTrue(run_adapter(path, record("[]"), timeout_seconds=.2).timed_out)
            path.write_text("def parse(record):\n raise ValueError('boom')\n", encoding="utf-8")
            result = run_adapter(path, record("[]"))
            self.assertFalse(result.ok)
            self.assertIn("ValueError", result.stderr)

    def test_subprocess_preserves_utf8_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.py"
            path.write_text(
                "from price_monitor.adapter_api import RawOffer\n"
                "def parse(record):\n return [RawOffer(name=record.payload)]\n", encoding="utf-8")
            result = run_adapter(path, record("тариф домашнего интернета"))
            self.assertTrue(result.ok)
            self.assertEqual(result.offers[0]["name"], "тариф домашнего интернета")

    def test_validation_json_preserves_raw_and_price_rules(self) -> None:
        source = record('[{"name":"A","price":500}]')
        valid = {"name": "A", "price": 500.0, "price_old": None, "evidence": ['"price":500'], "raw_offer": {"name": "A", "price": 500}}
        self.assertTrue(validate_offers(source, [valid]).ok)
        invalid = {**valid, "price": 600.0, "price_old": 500.0}
        self.assertFalse(validate_offers(source, [invalid]).ok)

    def test_raw_offer_normalizes_numeric_strings(self) -> None:
        offer = RawOffer(name=" A ", price="500 ₽", tv_channels="237 каналов")  # type: ignore[arg-type]
        self.assertEqual((offer.name, offer.price, offer.tv_channels), ("A", 500.0, 237))

    def test_html_block_facts_extracts_discounted_bundle(self) -> None:
        block = (
            "домашний интернет и тв\n500 мбит/с\nподписка bee TEST\n"
            "237 тв-каналов\n50 гб\n1200 минут\n400 ₽/мес\n800 ₽/мес\nскидка на 2 месяца"
        )
        facts = html_block_facts(block)
        self.assertEqual(facts["name"], "подписка bee TEST")
        self.assertEqual((facts["price"], facts["price_old"]), (400.0, 800.0))
        self.assertEqual((facts["internet_speed_mbps"], facts["tv_channels"]), (500.0, 237))
        self.assertEqual((facts["mobile_minutes"], facts["mobile_data_gb"]), (1200, 50.0))

    def test_html_block_facts_handles_spaced_mts_price(self) -> None:
        block = "Домашний интернет\nМТС Дома Супер\n500 Мбит/с\n230+ ТВ-каналов\n2000 минут\n575₽/ мес"
        facts = html_block_facts(block)
        self.assertEqual((facts["name"], facts["price"]), ("МТС Дома Супер", 575.0))
        self.assertEqual((facts["tv_channels"], facts["mobile_minutes"]), (230, 2000))

    def test_price_parser_does_not_cross_lines_or_use_router_rent(self) -> None:
        block = (
            "Летай Оранжевый 100\n100 Мбит/с\n"
            "Аренда роутера - 150 ₽/мес\n450\n350 ₽/мес"
        )
        facts = html_block_facts(block)
        self.assertEqual(facts["price"], 350.0)

    def test_compound_region_is_split_without_cross_card_facts(self) -> None:
        payload = """
        <main><h1>Домашний интернет</h1>
        <div>МТС Дома Отлично</div><div>100 Мбит/с</div><div>30 ГБ 750 минут</div><div>810 ₽/мес</div>
        <div>МТС Дома Супер</div><div>1000 Мбит/с</div><div>50 ГБ 2000 минут</div><div>1000 ₽/мес</div>
        </main>
        """
        blocks = html_offer_blocks(payload)
        facts = [html_block_facts(block) for block in blocks]
        self.assertEqual(len(facts), 2)
        self.assertEqual(
            [(item["name"], item["price"], item["internet_speed_mbps"], item["mobile_minutes"]) for item in facts],
            [("МТС Дома Отлично", 810.0, 100.0, 750), ("МТС Дома Супер", 1000.0, 1000.0, 2000)],
        )

    def test_split_price_nodes_preserve_promotional_old_price(self) -> None:
        facts = html_block_facts("Минимум\n500 Мбит/с\nСкидка\n899\n0 руб./месяц")
        self.assertEqual((facts["price"], facts["price_old"]), (0.0, 899.0))

    def test_reverse_promotional_month_price(self) -> None:
        facts = html_block_facts("-99%\n750 ₽\nМесяц за 1 рубль\nИнтернет на все 100\n100 Мбит/с")
        self.assertEqual((facts["price"], facts["price_old"]), (1.0, 750.0))

    def test_tv_service_heading_does_not_turn_speed_into_channel_count(self) -> None:
        block = "домашний интернет и тв\n100 мбит/с\nсим в подарок\n350 ₽/мес"
        facts = html_block_facts(block)
        self.assertIsNone(facts["tv_channels"])
        self.assertEqual(facts["name"], "домашний интернет и тв")

    def test_abbreviated_minutes_mark_mobile_service(self) -> None:
        facts = html_block_facts("домашний интернет\n100 мбит/с\n500 мин\n400 ₽/мес")
        self.assertIn("mobile", facts["services"])

    def test_single_tariff_detail_is_extracted_with_old_price(self) -> None:
        payload = """
        <main><div>\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435</div><h1>\u041c\u0422\u0421 \u0414\u043e\u043c\u0430 \u041e\u0442\u043b\u0438\u0447\u043d\u043e</h1>
        <p>\u0423\u044e\u0442\u043d\u043e\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0441 \u0434\u043e\u043c\u0430\u0448\u043d\u0438\u043c \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442\u043e\u043c \u0438 \u0422\u0412</p><div>\u0414\u043b\u044f \u0434\u043e\u043c\u0430</div>
        <div>500 \u041c\u0431\u0438\u0442/\u0441</div><div>230+ \u0422\u0412-\u043a\u0430\u043d\u0430\u043b\u043e\u0432</div><div>30 \u0413\u0411</div>
        <div>900 \u043c\u0438\u043d\u0443\u0442</div><div>\u0421\u043a\u0438\u0434\u043a\u0430 50%</div><div>425 \u20bd/\u043c\u0435\u0441</div>
        <div>\u0434\u0430\u043b\u0435\u0435 \u2014 850 \u20bd\u2060/\u2060\u043c\u0435\u0441</div></main>
        """
        blocks = html_offer_blocks(payload)
        self.assertEqual(len(blocks), 1)
        facts = html_block_facts(blocks[0])
        self.assertEqual(
            (facts["name"], facts["price"], facts["price_old"]),
            ("\u041c\u0422\u0421 \u0414\u043e\u043c\u0430 \u041e\u0442\u043b\u0438\u0447\u043d\u043e", 425.0, 850.0),
        )

    def test_split_price_nodes_and_partner_cards_are_extracted(self) -> None:
        def card(name: str, current: int, old: int) -> str:
            return (
                f"<section><h2>{name}</h2><div>\u041c\u043e\u0431\u0438\u043b\u044c\u043d\u0430\u044f \u0441\u0432\u044f\u0437\u044c</div><div>20 \u0413\u0411</div>"
                "<div>500 \u043c\u0438\u043d\u0443\u0442</div><div>\u0414\u043e\u043c\u0430\u0448\u043d\u0438\u0439 \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442</div>"
                f"<div>100 \u041c\u0431\u0438\u0442/\u0441</div><div>-50%</div><div>{current}</div><div>{old}</div><div>\u20bd/\u043c\u0435\u0441</div></section>"
            )
        blocks = html_offer_blocks(card("alpha", 400, 800) + card("beta", 500, 1000))
        self.assertEqual(len(blocks), 2)
        facts = [html_block_facts(block) for block in blocks]
        self.assertEqual([(item["name"], item["price"], item["price_old"]) for item in facts], [
            ("alpha", 400.0, 800.0), ("beta", 500.0, 1000.0),
        ])

    def test_grid_price_before_speed_does_not_take_next_card_price(self) -> None:
        from price_monitor.adapter_helpers import _grid_offer_blocks
        blocks = _grid_offer_blocks("LifeLine\n840 месяц\nСкорость 100 Мбит/с\nРоутер - Нет\nПодробнее\nBeta\n1080 месяц\nСкорость 300 Мбит/с")
        facts = [html_block_facts(block) for block in blocks]
        self.assertEqual([(x['name'], x['price'], x['internet_speed_mbps']) for x in facts],
                         [('LifeLine', 840.0, 100.0), ('Beta', 1080.0, 300.0)])

    def test_grid_split_price_does_not_turn_router_into_tariff(self) -> None:
        from price_monitor.adapter_helpers import _grid_offer_blocks
        blocks = _grid_offer_blocks("Ритм\n100 Мбит/с\n550 ₽\n390 ₽ *\nв месяц\nВыберите оборудование\nПокупка\nСкорость Wi-Fi\n574 Мбит/с")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(html_block_facts(blocks[0])['price'], 390.0)

    def test_adjacent_promotional_prices_are_not_concatenated(self) -> None:
        from price_monitor.adapter_helpers import _monthly_prices
        self.assertEqual(_monthly_prices('695 645 руб/мес'), [645.0])
        self.assertEqual(_monthly_prices('1300 1230 руб/мес'), [1230.0])
        self.assertEqual(_monthly_prices('590 рублей в месяц'), [590.0])
        self.assertEqual(_monthly_prices('375 р/мес'), [375.0])

    def test_antivirus_brand_number_is_not_internet_speed(self) -> None:
        from price_monitor.adapter_helpers import _offer_speeds
        self.assertEqual(_offer_speeds('PRO32 Ultimate Security\n350 руб/мес'), [])

    def test_cross_sell_layout_keeps_subscription_total(self) -> None:
        from price_monitor.adapter_helpers import _sequential_offer_blocks
        blocks = _sequential_offer_blocks('100\n100 Мбит/с\nХотите смотреть телевидение с 5 устройств?\nИнсис ТВ\n250 ₽/мес\n890 ₽/мес')
        self.assertEqual(html_block_facts(blocks[0])['price'], 890.0)

    def test_month_label_is_not_commercial_name(self) -> None:
        self.assertEqual(html_block_facts('Домашний\n50 Мбит/с\n450 ₽\nв месяц')['name'], 'Домашний')

    def test_mts_speed_variants_keep_maximum_without_router_pollution(self) -> None:
        facts = html_block_facts('МТС Дома Отлично\n200 Мбит/с\n500 Мбит/с\n1 Гбит/с\nМобильная связь\nРоутер 2 Гбит/с\n425 ₽/мес')
        self.assertEqual(facts['internet_speed_mbps'], 1000.0)

    def test_included_router_and_connection_cta_do_not_hide_tariff_price(self) -> None:
        from price_monitor.adapter_helpers import _monthly_prices
        self.assertEqual(_monthly_prices('100 Мбит/с\nроутер в составе подписки\nbeeGPT\n950 ₽/мес'), [950.0])
        self.assertEqual(_monthly_prices('Подключите ещё 5 номеров\nФильмы\n410 ₽/мес\n820 ₽/мес'), [410.0, 820.0])

    def test_standalone_old_price_wins_over_optional_subscription_price(self) -> None:
        facts = html_block_facts('Комфорт Плюс\nАкция\n100 Мбит/с\n650\n325 руб/мес\n+ кинотеатр\n450 руб/мес')
        self.assertEqual(facts['price_old'], 650.0)

    def test_grid_annual_offer_does_not_borrow_monthly_price(self) -> None:
        from price_monitor.adapter_helpers import _grid_offer_blocks
        blocks = _grid_offer_blocks("EVO 100/250\n100 Мбит/с\n250 руб/мес\nПодробнее\nEVO Годовой\n100 Мбит/с\n5100 руб/год")
        self.assertEqual(len(blocks), 1)

    def test_html_text_ignores_script_and_style_noise(self) -> None:
        rendered = html_text("<style>500 \u20bd/\u043c\u0435\u0441</style><script>1000 Mbps</script><p>Visible</p>")
        self.assertEqual(rendered, "Visible")


class RunnerTests(TestCase):
    def test_run_adapter_is_llm_free_serializes_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rtk.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["provider", "url", "timestamp", "region", "city", "response_json"])
                writer.writeheader(); writer.writerow({"provider": "rtk", "url": "u", "timestamp": "t", "region": "r", "city": "c", "response_json": '[{"name":"A","price":500}]'})
            adapter = root / "adapter.py"
            adapter.write_text(
                "from price_monitor.adapter_api import RawOffer\n"
                "from price_monitor.adapter_helpers import parse_json, json_items\n"
                "def parse(record):\n"
                " out=[]\n"
                " for item in json_items(parse_json(record.payload)):\n"
                "  out.append(RawOffer(name=item['name'], price=float(item['price']), evidence=['500'], raw_offer=item))\n"
                " return out\n", encoding="utf-8")
            output = root / "out"
            first = run_saved_adapter(source, adapter, output)
            second = run_saved_adapter(source, adapter, output, resume=True)
            self.assertEqual((first.offers, first.records_failed), (1, 0))
            self.assertEqual(second.records_skipped, 1)
            with (output / "offers.csv").open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(json.loads(rows[0]["services"]), [])
            self.assertEqual(json.loads(rows[0]["raw_offer"])["name"], "A")

    def test_one_bad_record_does_not_stop_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rtk.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["provider", "url", "timestamp", "city", "response_json"])
                writer.writeheader()
                writer.writerow({"provider": "rtk", "url": "u1", "timestamp": "t", "city": "bad", "response_json": "[]"})
                writer.writerow({"provider": "rtk", "url": "u2", "timestamp": "t", "city": "good", "response_json": "[]"})
            adapter = root / "adapter.py"
            adapter.write_text("def parse(record):\n if record.city == 'bad': raise ValueError('bad row')\n return []\n", encoding="utf-8")
            stats = run_saved_adapter(source, adapter, root / "out")
            self.assertEqual((stats.records_failed, stats.records_succeeded), (1, 1))


class QualityGateTests(TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, SourceRecord]:
        source = root / "demo.csv"
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["provider", "url", "timestamp", "city", "response_json"])
            writer.writeheader()
            writer.writerow({
                "provider": "demo", "url": "https://example.test/catalog", "timestamp": "t",
                "city": "A", "response_json": '[{"name":"A","price":500}]',
            })
        adapter = root / "adapter.py"
        adapter.write_text(
            "from price_monitor.adapter_api import RawOffer\n"
            "from price_monitor.adapter_helpers import parse_json, json_items\n"
            "def parse(record):\n"
            " return [RawOffer(name=x['name'], price=x['price'], raw_offer=x) for x in json_items(parse_json(record.payload))]\n",
            encoding="utf-8",
        )
        return source, adapter, next(iter_source_records(source))

    def test_quality_gate_is_provisional_without_approved_gold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, adapter, _ = self._fixture(Path(directory))
            result = check_adapter_quality(source, adapter)
            self.assertTrue(result.passed)
            self.assertEqual(result.status, "provisional")
            strict = check_adapter_quality(source, adapter, require_gold=True)
            self.assertFalse(strict.passed)
            self.assertEqual(strict.status, "failed")

    def test_approved_gold_certifies_and_detects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, adapter, item = self._fixture(root)
            gold = root / "gold.json"
            gold.write_text(json.dumps({"version": 1, "cases": [{
                "approved": True, "source_record_id": item.record_id,
                "expected_offer_count": 1, "offers": [{"name": "A", "price": 500}],
            }]}), encoding="utf-8")
            good = check_adapter_quality(source, adapter, gold_path=gold, require_gold=True)
            self.assertTrue(good.passed)
            self.assertEqual(good.status, "passed")
            document = json.loads(gold.read_text(encoding="utf-8"))
            document["cases"][0]["offers"][0]["price"] = 600
            gold.write_text(json.dumps(document), encoding="utf-8")
            bad = check_adapter_quality(source, adapter, gold_path=gold, require_gold=True)
            self.assertFalse(bad.passed)
            self.assertEqual(bad.gold["failed"], 1)

    def test_gold_template_is_never_self_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, adapter, _ = self._fixture(root)
            path = root / "template.json"
            self.assertEqual(write_gold_template(source, adapter, path, limit=1), 1)
            case = json.loads(path.read_text(encoding="utf-8"))["cases"][0]
            self.assertIs(case["approved"], False)
            self.assertEqual(case["expected_offer_count"], 1)
