from __future__ import annotations

import csv
import hashlib
import json
import os
import pprint
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .html_reducer import ReducedHtml, reduce_html
from .llm import OllamaClient
from .render import build_card
from .schema import Extraction


REQUIRED_COLUMNS = {"provider", "url", "timestamp", "city", "html"}


@dataclass(slots=True)
class RunStats:
    rows: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    offers: int = 0
    unavailable: int = 0
    errors: int = 0


def iter_csv(path: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(2_147_483_647)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        yield from reader


def run_pipeline(
    input_csv: Path,
    output_dir: Path,
    client: OllamaClient,
    *,
    max_chars: int = 16_000,
    limit: int | None = None,
    city_filter: str | None = None,
    force: bool = False,
    locality_map: dict[str, int] | None = None,
) -> RunStats:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)
    errors_path = output_dir / "errors.jsonl"
    cards_path = output_dir / "cards.jsonl"
    stats = RunStats()
    locality_map = locality_map or {}

    write_mode = "w" if force else "a"
    completed = set() if force else _completed_keys(cards_path)
    with cards_path.open(write_mode, encoding="utf-8", newline="\n") as cards_file, \
            errors_path.open(write_mode, encoding="utf-8", newline="\n") as errors_file:
        for row_index, row in enumerate(iter_csv(input_csv), start=1):
            if city_filter and city_filter.casefold() not in row["city"].casefold():
                continue
            row_key = _row_key(row)
            if row_key in completed:
                continue
            if limit is not None and stats.rows >= limit:
                break
            stats.rows += 1
            try:
                reduced = reduce_html(row["html"], city=row["city"], max_chars=max_chars)
                extraction, cached = _extract_cached(client, reduced, row, cache_dir)
                stats.cache_hits += int(cached)
                stats.llm_calls += int(not cached)
                if extraction.availability == "unavailable":
                    stats.unavailable += 1
                cards = [
                    build_card(
                        offer,
                        provider=row["provider"],
                        city=row["city"],
                        url=row["url"],
                        timestamp=row["timestamp"],
                        locality_id=locality_map.get(row["city"].casefold()),
                    )
                    for offer in extraction.offers
                ]
                stats.offers += len(cards)
                for card in cards:
                    envelope = {
                        "_rowKey": row_key,
                        "_provider": row["provider"],
                        "_city": row["city"],
                        "_sourceUrl": row["url"],
                        **card,
                    }
                    cards_file.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                _write_city_result(output_dir, row, extraction, cards, reduced)
                if not cards:
                    # A status record makes resume work for unavailable/unknown pages too.
                    cards_file.write(json.dumps({
                        "_rowKey": row_key,
                        "_status": extraction.availability,
                        "provider": row["provider"],
                        "city": row["city"],
                        "sourceUrl": row["url"],
                        "pageNotes": extraction.page_notes,
                    }, ensure_ascii=False) + "\n")
                cards_file.flush()
                print(
                    f"[{stats.rows}] {row['provider']} / {row['city']}: "
                    f"{len(cards)} offer(s){' [cache]' if cached else ''}",
                    flush=True,
                )
            except Exception as exc:
                stats.errors += 1
                error = {"row": row_index, "provider": row["provider"], "city": row["city"], "url": row["url"], "error": str(exc)}
                errors_file.write(json.dumps(error, ensure_ascii=False) + "\n")
                errors_file.flush()
                print(f"[{stats.rows}] ERROR {row['provider']} / {row['city']}: {exc}", file=sys.stderr, flush=True)
    return stats


def _extract_cached(client: OllamaClient, reduced: ReducedHtml, row: dict[str, str], cache_dir: Path) -> tuple[Extraction, bool]:
    fingerprint = hashlib.sha256(
        (client.model + "\0extractor-v7\0" + reduced.text).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{fingerprint}.json"
    if cache_path.exists():
        return Extraction.from_dict(json.loads(cache_path.read_text(encoding="utf-8"))), True
    extraction = client.extract(
        reduced.text,
        provider=row["provider"],
        city=row["city"],
        url=row["url"],
    )
    temp_path = cache_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(extraction.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, cache_path)
    return extraction, False


def _write_city_result(output_dir: Path, row: dict[str, str], extraction: Extraction, cards: list[dict], reduced: ReducedHtml) -> None:
    folder = output_dir / _safe_name(row["provider"]) / _safe_name(row["city"])
    folder.mkdir(parents=True, exist_ok=True)
    message = "\n\n".join(pprint.pformat(card, width=100, sort_dicts=False) for card in cards)
    if not cards:
        message = pprint.pformat({
            "provider": row["provider"],
            "city": row["city"],
            "availability": extraction.availability,
            "page_notes": extraction.page_notes,
        }, width=100, sort_dicts=False)
    (folder / "message.txt").write_text(message + "\n", encoding="utf-8")
    audit = {
        "provider": row["provider"], "city": row["city"], "url": row["url"],
        "timestamp": row["timestamp"], "reduction": {
            "originalChars": reduced.original_chars,
            "reducedChars": reduced.reduced_chars,
            "candidateBlocks": reduced.candidate_blocks,
        },
        "extraction": extraction.to_dict(),
    }
    (folder / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def _completed_keys(cards_path: Path) -> set[str]:
    if not cards_path.exists():
        return set()
    result: set[str] = set()
    with cards_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                key = json.loads(line).get("_rowKey")
            except json.JSONDecodeError:
                continue
            if key:
                result.add(key)
    return result


def _row_key(row: dict[str, str]) -> str:
    return hashlib.sha1("\0".join(row.get(key, "") for key in ("provider", "city", "url", "timestamp")).encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:100] or "unknown"


def load_locality_map(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("locality map must be a JSON object: city -> integer id")
    return {str(city).casefold(): int(identifier) for city, identifier in value.items()}
