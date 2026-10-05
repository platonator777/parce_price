from unittest import TestCase

from price_monitor.html_reducer import reduce_html


class HtmlReducerTests(TestCase):
    def test_keeps_offer_and_removes_noise(self) -> None:
        raw = """
        <html><head><style>.price{color:red}</style><script>analytics.track('price')</script></head>
        <body><nav>Личный кабинет</nav><main><article>
        <h2>Тариф Быстрый</h2><div>Домашний интернет</div><div>500 Мбит/с</div>
        <div><s>900 руб/мес</s><strong>600 руб/мес</strong></div>
        </article></main></body></html>
        """
        reduced = reduce_html(raw, max_chars=4000)
        self.assertIn("Тариф Быстрый", reduced.text)
        self.assertIn("500 Мбит/с", reduced.text)
        self.assertIn("600 руб/мес", reduced.text)
        self.assertNotIn("color:red", reduced.text)
        self.assertNotIn("analytics.track", reduced.text)

    def test_normalizes_city_for_cross_city_cache(self) -> None:
        raw = "<title>Интернет в Екатеринбург</title><div>Тариф 500 руб/мес</div>"
        reduced = reduce_html(raw, city="Екатеринбург")
        self.assertIn("<CITY>", reduced.text)
        self.assertNotIn("Екатеринбург", reduced.text)

    def test_drops_faq_after_offer_grid(self) -> None:
        raw = """
        <div>Тариф Дом</div><div>100 Мбит/с</div><div>500 ₽/мес</div>
        <h2>Частые вопросы</h2><p>Статический IP стоит 180 ₽/месяц</p>
        """
        reduced = reduce_html(raw, max_chars=4000)
        self.assertIn("500 ₽/мес", reduced.text)
        self.assertNotIn("180 ₽/месяц", reduced.text)

    def test_marks_adjacent_cards(self) -> None:
        raw = """
        <div>Домашний интернет</div><div>100 Мбит/с</div><div>Тариф Первый</div>
        <div>400 ₽/мес</div><div>700 ₽/мес</div>
        <div>Домашний интернет</div><div>300 Мбит/с</div><div>Тариф Второй</div>
        <div>600 ₽/мес</div><div>900 ₽/мес</div>
        """
        reduced = reduce_html(raw, max_chars=4000)
        self.assertIn("[OFFER BLOCK 1]", reduced.text)
        self.assertIn("[OFFER BLOCK 2]", reduced.text)
        self.assertLess(reduced.text.find("400 ₽/мес"), reduced.text.find("[OFFER BLOCK 2]"))

    def test_keeps_single_home_tariff_detail_card(self) -> None:
        raw = """
        <main><h1>\u0422\u0430\u0440\u0438\u0444 \u041c\u0422\u0421 \u0414\u043e\u043c\u0430 \u041e\u0442\u043b\u0438\u0447\u043d\u043e</h1>
        <p>\u0414\u043e\u043c\u0430\u0448\u043d\u0438\u0439 \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442</p><div>500 \u041c\u0431\u0438\u0442/\u0441</div>
        <div>230+ \u0422\u0412-\u043a\u0430\u043d\u0430\u043b\u043e\u0432</div><div>30 \u0413\u0411</div><div>900 \u043c\u0438\u043d\u0443\u0442</div>
        <div>425 \u20bd/\u043c\u0435\u0441</div><div>\u0434\u0430\u043b\u0435\u0435 \u2014 850 \u20bd/\u043c\u0435\u0441</div></main>
        """
        reduced = reduce_html(raw, max_chars=4000)
        self.assertIn("[OFFER BLOCK 1]", reduced.text)
        self.assertIn("\u041c\u0422\u0421 \u0414\u043e\u043c\u0430 \u041e\u0442\u043b\u0438\u0447\u043d\u043e", reduced.text)
