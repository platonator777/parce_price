from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .html_reducer import reduce_html
from .compare import compare_snapshots
from .llm import OllamaClient
from .pipeline import load_locality_map, run_pipeline
from .adapter_runner import run_saved_adapter
from .quality_gate import check_adapter_quality, write_gold_template
from .synthesis import synthesize_adapter


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="price-monitor", description="Convert provider HTML snapshots to tariff cards with a local Ollama LLM")
    sub = root.add_subparsers(dest="command", required=True)

    process = sub.add_parser("process", help="process CSV rows")
    process.add_argument("input", type=Path)
    process.add_argument("--output", type=Path, default=Path("output"))
    process.add_argument("--model", default="qwen3:8b")
    process.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    process.add_argument("--context", type=int, default=8_192)
    process.add_argument("--max-chars", type=int, default=10_000)
    process.add_argument("--limit", type=int)
    process.add_argument("--city", help="case-insensitive city substring")
    process.add_argument("--force", action="store_true", help="overwrite result indexes and reprocess rows (LLM cache remains)")
    process.add_argument("--locality-map", type=Path, help="optional JSON object mapping city names to localityId")

    inspect = sub.add_parser("inspect", help="show the exact reduced text sent to the LLM")
    inspect.add_argument("html_file", type=Path)
    inspect.add_argument("--city", default="")
    inspect.add_argument("--max-chars", type=int, default=10_000)

    diff = sub.add_parser("diff", help="compare two cards.jsonl snapshots")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    diff.add_argument("--output", type=Path, help="write JSON report instead of stdout")

    synthesize = sub.add_parser("synthesize", help="generate and validate a provider adapter with local Ollama")
    synthesize.add_argument("input", type=Path)
    synthesize.add_argument("--provider")
    synthesize.add_argument("--output", type=Path, default=Path("generated_adapters"))
    synthesize.add_argument("--model", default="qwen3:8b")
    synthesize.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    synthesize.add_argument("--context", type=int, default=8192)
    synthesize.add_argument("--seed", type=int, default=42)
    synthesize.add_argument("--max-attempts", type=int, default=5)
    synthesize.add_argument("--train-examples", type=int, default=4)
    synthesize.add_argument("--holdout-examples", type=int, default=3)
    synthesize.add_argument("--smoke-examples", type=int, default=2)
    synthesize.add_argument("--context-chars", type=int, default=24000)
    synthesize.add_argument("--max-records", type=int)
    synthesize.add_argument("--adapter-timeout", type=float, default=8.0)
    synthesize.add_argument("--resume", action="store_true")

    run = sub.add_parser("run-adapter", help="stream a CSV through a saved adapter without LLM calls")
    run.add_argument("input", type=Path)
    run.add_argument("--provider")
    run.add_argument("--adapter", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--max-records", type=int)
    run.add_argument("--adapter-timeout", type=float, default=8.0)
    run.add_argument("--resume", action="store_true")

    quality = sub.add_parser("quality-check", help="full adapter audit with coverage and optional approved gold cases")
    quality.add_argument("input", type=Path)
    quality.add_argument("--provider")
    quality.add_argument("--adapter", type=Path, required=True)
    quality.add_argument("--gold", type=Path)
    quality.add_argument("--report", type=Path, required=True)
    quality.add_argument("--max-records", type=int)
    quality.add_argument("--adapter-timeout", type=float, default=8.0)
    quality.add_argument("--min-positive-records", type=int, default=0)
    quality.add_argument("--min-offers", type=int, default=0)
    quality.add_argument("--require-gold", action="store_true", help="fail unless at least one manually approved gold case is checked")

    gold = sub.add_parser("make-gold-template", help="create diverse unapproved cases for manual source review")
    gold.add_argument("input", type=Path)
    gold.add_argument("--provider")
    gold.add_argument("--adapter", type=Path, required=True)
    gold.add_argument("--output", type=Path, required=True)
    gold.add_argument("--cases", type=int, default=20)
    gold.add_argument("--adapter-timeout", type=float, default=8.0)
    return root


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    if args.command == "inspect":
        raw = args.html_file.read_text(encoding="utf-8")
        reduced = reduce_html(raw, city=args.city, max_chars=args.max_chars)
        print(reduced.text)
        print(json.dumps({"originalChars": reduced.original_chars, "reducedChars": reduced.reduced_chars, "candidateBlocks": reduced.candidate_blocks}, ensure_ascii=False))
        return
    if args.command == "diff":
        rendered = json.dumps(compare_snapshots(args.old, args.new), ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(args.output)
        else:
            print(rendered)
        return
    if args.command == "run-adapter":
        stats = run_saved_adapter(
            args.input, args.adapter, args.output, provider=args.provider,
            max_records=args.max_records, resume=args.resume, timeout_seconds=args.adapter_timeout,
        )
        from dataclasses import asdict
        print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
        return
    if args.command == "make-gold-template":
        count = write_gold_template(
            args.input, args.adapter, args.output, provider=args.provider,
            limit=args.cases, timeout_seconds=args.adapter_timeout,
        )
        print(json.dumps({"path": str(args.output), "cases": count, "approved": 0}, ensure_ascii=False, indent=2))
        return
    if args.command == "quality-check":
        from dataclasses import asdict
        result = check_adapter_quality(
            args.input, args.adapter, provider=args.provider, gold_path=args.gold,
            report_path=args.report, max_records=args.max_records,
            timeout_seconds=args.adapter_timeout,
            min_positive_records=args.min_positive_records, min_offers=args.min_offers,
            require_gold=args.require_gold,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        if not result.passed:
            raise SystemExit(2)
        return
    if args.command == "synthesize":
        client = OllamaClient(args.ollama_url, args.model, args.context)
        client.healthcheck()
        result = synthesize_adapter(
            args.input, args.output, client, provider=args.provider, max_records=args.max_records,
            max_attempts=args.max_attempts, train_count=args.train_examples,
            holdout_count=args.holdout_examples, smoke_count=args.smoke_examples,
            context_chars=args.context_chars, seed=args.seed, resume=args.resume,
            timeout_seconds=args.adapter_timeout,
        )
        print(json.dumps({
            "adapter": str(result.adapter_path), "accepted": result.accepted,
            "attempts": result.attempts, "train": result.train.__dict__ if hasattr(result.train, "__dict__") else {
                "ok": result.train.ok, "passed": result.train.passed, "failed": result.train.failed, "offers": result.train.offers,
            }, "holdout": {"ok": result.holdout.ok, "passed": result.holdout.passed, "failed": result.holdout.failed, "offers": result.holdout.offers},
            "smoke": {"ok": result.smoke.ok, "passed": result.smoke.passed, "failed": result.smoke.failed, "offers": result.smoke.offers},
        }, ensure_ascii=False, indent=2))
        if not result.accepted:
            raise SystemExit(2)
        return

    client = OllamaClient(args.ollama_url, args.model, args.context)
    client.healthcheck()
    stats = run_pipeline(
        args.input,
        args.output,
        client,
        max_chars=args.max_chars,
        limit=args.limit,
        city_filter=args.city,
        force=args.force,
        locality_map=load_locality_map(args.locality_map),
    )
    print(json.dumps({
        "rows": stats.rows, "llmCalls": stats.llm_calls, "cacheHits": stats.cache_hits,
        "offers": stats.offers, "unavailable": stats.unavailable, "errors": stats.errors,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
