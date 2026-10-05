from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .adapter_api import OUTPUT_COLUMNS, RawOffer, offer_row
from .adapter_runtime import run_adapter
from .adapter_runtime import ExecutionResult
from .source import iter_source_records
from .validation import validate_offers
from .atomic_files import replace_with_retry


@dataclass(slots=True)
class AdapterRunStats:
    records_seen: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    offers: int = 0
    providers: dict = field(default_factory=dict)
    records_empty: int = 0


def run_saved_adapter(input_csv: Path, adapter_path: Path, output_dir: Path, *, provider: str | None = None, max_records: int | None = None, resume: bool = False, timeout_seconds: float = 8.0, adapters_dir: Path | None = None, only_provider: str | None = None, progress_every: int = 0) -> AdapterRunStats:
    """Apply a saved adapter streamingly. This function intentionally has no LLM dependency."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path, errors_path, state_path = output_dir / "offers.csv", output_dir / "errors.jsonl", output_dir / "run_state.json"
    completed: set[str] = set()
    if resume and state_path.exists():
        completed = set(json.loads(state_path.read_text(encoding="utf-8")).get("completed", []))
    append = resume and output_path.exists()
    stats = AdapterRunStats()
    with output_path.open("a" if append else "w", encoding="utf-8-sig", newline="") as output, errors_path.open("a" if resume else "w", encoding="utf-8", newline="\n") as errors:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_COLUMNS)
        if not append:
            writer.writeheader()
        for record in iter_source_records(input_csv, provider=provider, max_records=max_records):
            adapter_provider = {'wifire_ru': 'wifireru', 'ooofirmaintersvyaz': 'intersvyaz'}.get(record.provider, record.provider)
            if only_provider and adapter_provider != only_provider:
                continue
            stats.records_seen += 1
            counts = stats.providers.setdefault(record.provider, {'records': 0, 'offers': 0, 'errors': 0, 'empty': 0})
            counts['records'] += 1
            if record.record_id in completed:
                stats.records_skipped += 1
                continue
            selected_adapter = adapter_path
            if adapters_dir is not None:
                candidate = adapters_dir / adapter_provider / 'adapter.py'
                # Provider strings are input data, never filesystem paths.
                selected_adapter = candidate if re.fullmatch(r'[A-Za-z0-9_-]+', adapter_provider) and candidate.is_file() else adapters_dir / 'ones' / 'adapter.py'
            if record.status and record.status.casefold() not in {'success', 'ok', '200'}:
                result = ExecutionResult(False, [], f'source collection failed: status={record.status}')
            elif not record.payload.strip():
                result = ExecutionResult(False, [], 'source payload is empty')
            else:
                result = run_adapter(selected_adapter, record, timeout_seconds=timeout_seconds)
            if result.ok:
                report = validate_offers(record, result.offers)
                if report.ok:
                    for item in result.offers:
                        row = offer_row(record, RawOffer(**item))
                        for key in ("services", "evidence", "raw_offer", "conditions", "optional_services", "billing_variants"):
                            row[key] = json.dumps(row[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        writer.writerow(row)
                    stats.records_succeeded += 1
                    stats.offers += len(result.offers)
                    counts['offers'] += len(result.offers)
                    if not result.offers:
                        stats.records_empty += 1
                        counts['empty'] += 1
                else:
                    result.ok = False; result.error = "; ".join(report.errors)
            if not result.ok:
                stats.records_failed += 1
                counts['errors'] += 1
                errors.write(json.dumps({"source_record_id": record.record_id, **record.metadata(), "error": result.error, "stderr": result.stderr}, ensure_ascii=False) + "\n")
            completed.add(record.record_id)
            output.flush(); errors.flush()
            temp = state_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"completed": sorted(completed)}, ensure_ascii=False), encoding="utf-8")
            replace_with_retry(temp, state_path)
            if progress_every and stats.records_seen % progress_every == 0:
                print(f'Processed {stats.records_seen} records: {stats.offers} offers, {stats.records_failed} errors', flush=True)
    report = {**asdict(stats), "success_rate": stats.records_succeeded / max(1, stats.records_seen - stats.records_skipped), "adapter": str(adapters_dir or adapter_path), "input": str(input_csv)}
    (output_dir / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats
