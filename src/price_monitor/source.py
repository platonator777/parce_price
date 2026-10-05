from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class SourceRecord:
    provider: str
    city: str
    region: str | None
    url: str
    timestamp: str
    payload_type: Literal["html", "json"]
    payload: str
    status: str | None = None

    @property
    def record_id(self) -> str:
        identity = "\0".join((self.provider, self.city, self.region or "", self.url, self.timestamp))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def metadata(self) -> dict[str, str | None]:
        value = asdict(self)
        value.pop("payload")
        value["source_record_id"] = self.record_id
        return value


def infer_provider(path: Path, url: str = "") -> str:
    stem = path.stem.casefold()
    host = (urlsplit(url).hostname or "").casefold()
    aliases = {"mts": "mts", "beeline": "beeline", "rtk": "rtk", "rostelecom": "rtk"}
    for marker, provider in aliases.items():
        if marker in stem or marker in host:
            return provider
    return (host.split(".")[-2] if host.count(".") else host) or stem.split("_")[0]


def iter_source_records(path: Path, *, provider: str | None = None, max_records: int | None = None) -> Iterator[SourceRecord]:
    """Stream heterogeneous CSV/Parquet snapshots without retaining CSV payloads."""
    csv.field_size_limit(2_147_483_647)
    suffix = path.suffix.casefold()
    if suffix in {".parquet", ".parqeut"}:
        yield from _iter_parquet_records(path, provider=provider, max_records=max_records)
        return
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        yield from _records_from_rows(
            path, reader, set(reader.fieldnames or ()), provider=provider, max_records=max_records,
        )


def _iter_parquet_records(path: Path, *, provider: str | None, max_records: int | None) -> Iterator[SourceRecord]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("Parquet input requires pyarrow; install local-price-monitor[parquet]") from exc
    file = parquet.ParquetFile(path)
    seen = 0
    for batch in file.iter_batches(batch_size=32):
        rows = batch.to_pylist()
        remaining = None if max_records is None else max_records - seen
        if remaining is not None and remaining <= 0:
            break
        selected = rows if remaining is None else rows[:remaining]
        yield from _records_from_rows(path, selected, set(batch.schema.names), provider=provider, row_offset=seen)
        seen += len(selected)


def _records_from_rows(
    path: Path,
    rows: Iterable[dict[str, Any]],
    fields: set[str],
    *,
    provider: str | None,
    max_records: int | None = None,
    row_offset: int = 0,
) -> Iterator[SourceRecord]:
    payload_fields = [name for name in ("response_json", "html", "html_content", "content") if name in fields]
    if not payload_fields:
        raise ValueError("input must contain html, html_content, response_json, or content")
    fallback_timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    for index, row in enumerate(rows):
        if max_records is not None and index >= max_records:
            break
        raw_url = row.get("url") or row.get("target_url") or row.get("base_url") or ""
        url = str(raw_url).strip()
        selected_provider = str(provider or row.get("provider") or infer_provider(path, url)).strip()
        if not url:
            url = f"{path.resolve().as_uri()}#row={row_offset + index + 1}"
        timestamp = row.get("timestamp")
        payload_field = next((name for name in payload_fields if row.get(name) not in (None, "")), payload_fields[0])
        value = row.get(payload_field)
        payload = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
        payload_type: Literal["html", "json"] = "json" if payload_field == "response_json" or (payload_field == "content" and payload.lstrip().startswith(("{", "["))) else "html"
        if payload_type == 'html' and 'initialTariffs' in payload:
            from .embedded_tariffs import embedded_tariff_items
            embedded = embedded_tariff_items(payload)
            if embedded is not None:
                payload = json.dumps(embedded, ensure_ascii=False)
                payload_type = 'json'
        yield SourceRecord(
            provider=selected_provider,
            city=str(row.get("city") or row.get("city_name") or "").strip(),
            region=str(row.get("region") or row.get("region_name") or "").strip() or None,
            url=url,
            timestamp=str(timestamp).strip() if timestamp not in (None, "") else fallback_timestamp,
            payload_type=payload_type,
            payload=payload,
            status=str(row.get("status") or "").strip() or None,
        )
