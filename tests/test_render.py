from unittest import TestCase

from price_monitor.render import build_card
from price_monitor.schema import ExtractedOffer


class RenderTests(TestCase):
    def test_builds_stable_message_shape(self) -> None:
        offer = ExtractedOffer(
            name="Домашний 500", price=600.0, price_old=900.0,
            internet_speed_mbps=500.0, services=["internet"],
            promotion_name="Скидка", promotion_description="Первые 3 месяца",
        )
        first = build_card(offer, provider="test", city="Тест", url="https://example.test", timestamp="2026-09-01T10:00:00")
        second = build_card(offer, provider="test", city="Тест", url="https://example.test", timestamp="2026-09-01T10:00:00")
        self.assertEqual(first["offerId"], second["offerId"])
        self.assertEqual(first["price"], 600)
        self.assertEqual(first["products"][0]["speedVal"], 500)
        self.assertTrue(first["stockData"]["isStock"])

    def test_offer_id_does_not_change_with_price(self) -> None:
        common = dict(name="Домашний", internet_speed_mbps=100.0, source_block=1)
        cheap = build_card(ExtractedOffer(price=400.0, **common), provider="p", city="c", url="u", timestamp="2026-01-01")
        expensive = build_card(ExtractedOffer(price=450.0, **common), provider="p", city="c", url="u", timestamp="2026-01-02")
        self.assertEqual(cheap["offerId"], expensive["offerId"])
