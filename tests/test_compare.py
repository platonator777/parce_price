import json
import tempfile
from pathlib import Path
from unittest import TestCase

from price_monitor.compare import compare_snapshots


class CompareTests(TestCase):
    def test_reports_price_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old.jsonl"
            new = Path(directory) / "new.jsonl"
            old.write_text(json.dumps({"offerId": 1, "name": "A", "price": 400}) + "\n", encoding="utf-8")
            new.write_text(json.dumps({"offerId": 1, "name": "A", "price": 450}) + "\n", encoding="utf-8")
            changes = compare_snapshots(old, new)
            self.assertEqual(changes[0]["type"], "changed")
            self.assertEqual(changes[0]["fields"]["price"], {"before": 400, "after": 450})
