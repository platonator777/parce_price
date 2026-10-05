from unittest import TestCase

from price_monitor.llm import _ground_to_source_blocks, _normalize_extraction
from price_monitor.schema import ExtractedOffer, Extraction


class NormalizeExtractionTests(TestCase):
    def test_removes_reversed_duplicate_but_keeps_distinct_same_name(self) -> None:
        base = dict(name="Домашний интернет", internet_speed_mbps=100, evidence=["цитата"])
        extraction = Extraction("available", [
            ExtractedOffer(price=400, price_old=700, promotion_name="Скидка", **base),
            ExtractedOffer(price=700, price_old=400, promotion_name="Скидка", **base),
            ExtractedOffer(price=900, price_old=None, mobile_minutes=500, **base),
        ], [])
        result = _normalize_extraction(extraction)
        self.assertEqual(len(result.offers), 2)
        discounted = next(item for item in result.offers if item.price == 400)
        self.assertEqual(discounted.price_old, 700)
        self.assertIn("100 Мбит/с", discounted.name)

    def test_removes_values_copied_from_another_block(self) -> None:
        extraction = Extraction("available", [
            ExtractedOffer(name="Общий заголовок", price=400, source_block=1,
                           internet_speed_mbps=999, mobile_minutes=700, evidence=[]),
        ], [])
        source = "[OFFER BLOCK 1]\nТариф Честный\n100 Мбит/с\n400 ₽/мес\n[OFFER BLOCK 2]\n700 минут\n"
        result = _ground_to_source_blocks(extraction, source)
        self.assertEqual(result.offers[0].name, "Тариф Честный")
        self.assertIsNone(result.offers[0].internet_speed_mbps)
        self.assertIsNone(result.offers[0].mobile_minutes)
