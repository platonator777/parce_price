"""One-command synthesis (if necessary), validation and full normalization."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# Always run the source shipped beside this entry point, even with an older installed wheel.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

from price_monitor.adapter_runner import run_saved_adapter
from price_monitor.llm import OllamaClient
from price_monitor.quality_gate import check_adapter_quality
from price_monitor.synthesis import synthesize_adapter



def resolve(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def has_provider_column(path):
    if path.suffix.casefold() in {'.parquet', '.parqeut'}:
        import pyarrow.parquet as parquet
        return 'provider' in parquet.ParquetFile(path).schema.names
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return 'provider' in next(csv.reader(handle), [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--all', action='store_true', help='process all entries of providers.json')
    parser.add_argument('--config', type=Path, default=Path('providers.json'))
    parser.add_argument('--output', type=Path, default=Path('output-full'))
    parser.add_argument('--adapters', type=Path, default=Path('adapters'))
    parser.add_argument('--regenerate', action='store_true', help='regenerate even an existing adapter')
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--max-attempts', type=int, default=5)
    parser.add_argument('--adapter-timeout', type=float, default=8)
    parser.add_argument('--gold', type=Path, help='approved gold for a single provider')
    args = parser.parse_args()
    if args.all and (args.provider or args.input or args.gold):
        parser.error('--all cannot be combined with --provider, --input or --gold')
    if not args.all and not args.provider and not args.input:
        parser.error('use --input FILE, --provider NAME or --all')
    if args.input and not args.provider:
        if args.regenerate or args.gold:
            parser.error('mixed input mode cannot use --regenerate or --gold; select a provider')
        source = resolve(args.input)
        if not source.is_file():
            parser.error(f'input not found: {source}')
        adapter_root = resolve(args.adapters)
        print(f'Processing mixed input: {source} (no LLM calls)', flush=True)
        stats = run_saved_adapter(source, adapter_root / 'ones' / 'adapter.py',
                                  resolve(args.output) / 'combined', adapters_dir=adapter_root,
                                  timeout_seconds=args.adapter_timeout, progress_every=100)
        print(json.dumps({'records': stats.records_seen, 'offers': stats.offers, 'errors': stats.records_failed,
                          'providers': stats.providers, 'output': str(resolve(args.output) / 'combined' / 'offers.csv')}), flush=True)
        if stats.records_failed:
            raise RuntimeError('some records failed; inspect output combined/errors.jsonl and quality_report.json')
        return
    config = json.loads(resolve(args.config).read_text(encoding='utf-8')) if args.all or not args.input else {}
    jobs = config if args.all else {args.provider: args.input or config.get(args.provider)}
    if not isinstance(jobs, dict) or not jobs:
        parser.error('provider configuration must be a non-empty JSON object')
    for provider, source in jobs.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', provider):
            parser.error(f'invalid provider name: {provider!r}')
        if not source or not resolve(source).is_file():
            parser.error(f'{provider}: input not found: {source}; pass --input or update providers.json')
    for provider, source in jobs.items():
        source = resolve(source)
        adapter_root = resolve(args.adapters)
        adapter = adapter_root / provider / 'adapter.py'
        output = resolve(args.output) / provider
        if args.regenerate or not adapter.is_file():
            print(f'{provider}: generating adapter with {args.model}', flush=True)
            client = OllamaClient(args.ollama_url, args.model, 8192)
            client.healthcheck()
            result = synthesize_adapter(source, adapter_root, client, provider=provider,
                                        max_attempts=args.max_attempts, timeout_seconds=args.adapter_timeout)
            # The underlying synthesizer does not include smoke in accepted.
            # This entry point refuses to normalize when smoke reports failures.
            if not result.accepted or not result.smoke.ok:
                raise RuntimeError(f'{provider}: adapter rejected; inspect {adapter.parent / "validation_report.json"}')
        else:
            validation_path = adapter.parent / 'validation_report.json'
            if validation_path.is_file():
                validation = json.loads(validation_path.read_text(encoding='utf-8'))
                if not validation.get('accepted') or not validation.get('smoke', {}).get('ok'):
                    raise RuntimeError(f'{provider}: saved synthesis was rejected; use --regenerate')
            print(f'{provider}: using saved adapter (no LLM calls)', flush=True)
        if args.gold:
            gate = check_adapter_quality(source, adapter, provider=provider, gold_path=resolve(args.gold),
                                         report_path=output / 'gold_quality_report.json', require_gold=True,
                                         timeout_seconds=args.adapter_timeout)
            if not gate.passed:
                raise RuntimeError(f'{provider}: gold quality gate failed; normalization not started')
        # A combined source must be filtered, never relabelled as one operator.
        filter_source = provider != 'ones' and has_provider_column(source)
        stats = run_saved_adapter(source, adapter, output, provider=None if filter_source else provider,
                                  only_provider=provider if filter_source else None,
                                  timeout_seconds=args.adapter_timeout, progress_every=100)
        if not stats.records_seen:
            raise RuntimeError(f'{provider}: no matching source records; check the provider column')
        print(json.dumps({'provider': provider, 'records': stats.records_seen, 'offers': stats.offers,
                          'errors': stats.records_failed, 'output': str(output / 'offers.csv')}), flush=True)
        if stats.records_failed:
            raise RuntimeError(f'{provider}: records failed; inspect {output / "errors.jsonl"}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f'Pipeline failed: {exc}')
