import unittest
from unittest.mock import patch

from price_monitor.llm import OllamaClient
from price_monitor.prompt_budget import INPUT_MARKER, fit_prompt, prompt_byte_budget
from price_monitor.synthesis import Evaluation, JSON_CONTRACT, _repair_prompt, _json_omissions, _json_example
from types import SimpleNamespace
from price_monitor.atomic_files import replace_with_retry


class PromptBudgetTests(unittest.TestCase):
    def test_brief_windows_lock_is_retried(self):
        with patch('price_monitor.atomic_files.os.replace', side_effect=[PermissionError(), None]) as replace, patch('price_monitor.atomic_files.time.sleep') as sleep:
            replace_with_retry('source', 'destination')
            self.assertEqual(replace.call_count, 2)
            sleep.assert_called_once_with(0.05)

    def test_json_example_is_complete_and_contains_real_fields(self):
        record = SimpleNamespace(payload='[{"name":"Тариф","price":275,"price_old":550,"products":[{"productCode":"SHPD","speedVal":200}]}]')
        example = _json_example(record, 3000)
        self.assertIn('"price_old":550', example)
        self.assertIn('"speedVal":200', example)
        self.assertNotIn('JSON SCHEMA', example)

    def test_explicit_json_facts_cannot_silently_disappear(self):
        raw = {'price': 275, 'price_old': 550, 'products': [{'productCode': 'SHPD', 'speedVal': 200}, {'productCode': 'IPTV'}]}
        offer = {'price': 275, 'price_old': None, 'services': [], 'internet_speed_mbps': None, 'raw_offer': raw}
        errors = _json_omissions([offer])
        self.assertTrue(any('price_old' in error for error in errors))
        self.assertTrue(any('service tv' in error for error in errors))
        self.assertTrue(any('speedVal' in error for error in errors))
        offer.update(price_old=550, services=['internet', 'tv'], internet_speed_mbps=200)
        self.assertEqual(_json_omissions([offer]), [])

    def test_oversized_single_json_line_is_not_partially_sent(self):
        prompt = fit_prompt('Return code.', '{"name":"' + 'тариф' * 1000 + '"}', max_bytes=300)
        self.assertEqual(prompt.partition(INPUT_MARKER)[2], '')

    def test_repair_contains_runtime_cause(self):
        evaluation = Evaluation(False, 0, 1, 0, [{'error': 'adapter process exited 1', 'stderr': 'UnboundLocalError: speed'}])
        prompt = _repair_prompt('def parse(record): return []', evaluation, evaluation, SimpleNamespace(train=[], holdout=[]), 5500, 2, contract=JSON_CONTRACT)
        self.assertIn('UnboundLocalError: speed', prompt)

    def test_utf8_budget_keeps_instructions_and_complete_lines(self):
        instructions = 'Return Python only.'
        prompt = fit_prompt(instructions, 'тариф\n' * 1000, max_bytes=300)
        self.assertTrue(prompt.startswith(instructions + INPUT_MARKER))
        self.assertLessEqual(len(prompt.encode('utf-8')), 300)
        self.assertNotIn('\ufffd', prompt)

    def test_oversized_prompt_is_rejected_before_network(self):
        client = OllamaClient(context_size=8192)
        with patch.object(OllamaClient, '_post_json') as post:
            with self.assertRaises(ValueError):
                client.generate_code('x' * 8192, num_predict=2048)
            post.assert_not_called()

    def test_instructions_are_system_message(self):
        client = OllamaClient(context_size=8192)
        prompt = fit_prompt('Return Python only.', 'source data', max_bytes=prompt_byte_budget(8192))
        with patch.object(OllamaClient, '_post_json', return_value={'message': {'content': 'code'}}) as post:
            self.assertEqual(client.generate_code(prompt, num_predict=2048), 'code')
            payload = post.call_args.args[1]
            self.assertIn('Return Python only.', payload['messages'][0]['content'])
            self.assertEqual(payload['messages'][1]['content'], 'source data')
