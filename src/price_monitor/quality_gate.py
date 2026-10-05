from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .adapter_helpers import html_text
from .adapter_runtime import run_adapter
from .sampling import fingerprint, select_samples
from .source import iter_source_records
from .validation import validate_offers


_MONTHLY_PRICE = re.compile(
    r"(?:\d[\d \u00a0]{0,8}\s*(?:₽|руб(?:\.|лей)?|р\.?)\s*(?:/|в\s+)?\s*(?:мес|месяц)"
    r"|\d{2,8}\s*\n\s*(?:руб\.?|р\.?)?\s*/?\s*(?:мес|месяц))", re.I
)
_SPEED = re.compile(r"\d+(?:[.,]\d+)?\s*(?:мбит|гбит|mbps|gbps)", re.I)
_HOME = re.compile(r"(?:домашн\w*\s+интернет|интернет\s+для\s+дома|тариф)", re.I)


@dataclass(slots=True)
class QualityGateResult:
    status: str
    passed: bool
    records_seen: int = 0
    records_passed: int = 0
    records_failed: int = 0
    positive_records: int = 0
    empty_records: int = 0
    offers: int = 0
    fingerprint_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    gold: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_adapter_quality(
    input_csv: Path,
    adapter_path: Path,
    *,
    provider: str | None = None,
    gold_path: Path | None = None,
    report_path: Path | None = None,
    max_records: int | None = None,
    timeout_seconds: float = 8.0,
    min_positive_records: int = 0,
    min_offers: int = 0,
    require_gold: bool = False,
) -> QualityGateResult:
    """Run a saved adapter over the source and independently gate its quality.

    Deterministic validation proves source grounding. Approved gold cases provide the
    only strict recall check; signal coverage is deliberately advisory because pages
    may repeat tariff words in navigation and cross-sell sections.
    """
    gold_document = _load_gold(gold_path) if gold_path else {"cases": []}
    approved = [case for case in gold_document.get("cases", []) if case.get("approved") is True]
    pending = len(gold_document.get("cases", [])) - len(approved)
    cases_by_id = {str(case.get("source_record_id")): case for case in approved if case.get("source_record_id")}
    cases_by_location: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in approved:
        if not case.get("source_record_id") and case.get("source_url"):
            cases_by_location[(str(case["source_url"]), str(case.get("city") or "").casefold())].append(case)

    counters = Counter()
    group_stats: dict[str, Counter] = defaultdict(Counter)
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    gold_checked: set[int] = set()
    gold_failed = 0

    for record in iter_source_records(input_csv, provider=provider, max_records=max_records):
        counters["records_seen"] += 1
        key = fingerprint(record).key
        group = group_stats[key]
        group["records"] += 1
        execution = run_adapter(adapter_path, record, timeout_seconds=timeout_seconds)
        if not execution.ok:
            counters["records_failed"] += 1
            group["failed"] += 1
            _append_limited(errors, {
                "source_record_id": record.record_id, "url": record.url,
                "error": execution.error, "stderr": execution.stderr[-2000:],
            })
            continue
        validation = validate_offers(record, execution.offers)
        if not validation.ok:
            counters["records_failed"] += 1
            group["failed"] += 1
            _append_limited(errors, {
                "source_record_id": record.record_id, "url": record.url,
                "error": "; ".join(validation.errors),
            })
            continue

        counters["records_passed"] += 1
        group["passed"] += 1
        count = len(execution.offers)
        counters["offers"] += count
        group["offers"] += count
        if count:
            counters["positive_records"] += 1
            group["positive_records"] += 1
        else:
            counters["empty_records"] += 1
            group["empty_records"] += 1
        if record.payload_type == "html" and _has_independent_offer_signal(record.payload):
            group["independent_signal_records"] += 1
            counters["independent_signal_records"] += 1
            if not count:
                group["signal_without_output"] += 1
                counters["signal_without_output"] += 1

        cases = []
        by_id = cases_by_id.get(record.record_id)
        if by_id:
            cases.append(by_id)
        cases.extend(cases_by_location.get((record.url, record.city.casefold()), []))
        for case in cases:
            marker = id(case)
            if marker in gold_checked:
                continue
            gold_checked.add(marker)
            differences = _compare_gold_case(case, execution.offers)
            if differences:
                gold_failed += 1
                _append_limited(errors, {
                    "source_record_id": record.record_id, "url": record.url,
                    "error": "gold mismatch: " + "; ".join(differences),
                })

    missing_gold = len(approved) - len(gold_checked)
    hard_failures: list[str] = []
    if counters["records_failed"]:
        hard_failures.append(f"{counters['records_failed']} records failed execution or validation")
    if counters["positive_records"] < min_positive_records:
        hard_failures.append(
            f"positive records {counters['positive_records']} below required {min_positive_records}"
        )
    if counters["offers"] < min_offers:
        hard_failures.append(f"offers {counters['offers']} below required {min_offers}")
    if gold_failed:
        hard_failures.append(f"{gold_failed} approved gold cases differ")
    if missing_gold:
        hard_failures.append(f"{missing_gold} approved gold cases were not found in the input")
    if require_gold and not approved:
        hard_failures.append("no approved gold cases; strict certification is impossible")

    for key, group in group_stats.items():
        if group["independent_signal_records"] and not group["positive_records"]:
            warnings.append(
                f"fingerprint {key}: {group['independent_signal_records']} records have independent "
                "price/speed/home signals but the adapter returned no offers"
            )
    if counters["independent_signal_records"] and not counters["positive_records"]:
        hard_failures.append("all records with independent offer signals produced zero offers")
    if pending:
        warnings.append(f"{pending} gold template cases are pending manual approval")

    certified = bool(approved) and not hard_failures
    status = "failed" if hard_failures else "passed" if certified else "provisional"
    for message in hard_failures:
        _append_limited(errors, {"error": message})
    result = QualityGateResult(
        status=status,
        passed=not hard_failures,
        records_seen=counters["records_seen"],
        records_passed=counters["records_passed"],
        records_failed=counters["records_failed"],
        positive_records=counters["positive_records"],
        empty_records=counters["empty_records"],
        offers=counters["offers"],
        fingerprint_groups={key: dict(value) for key, value in sorted(group_stats.items())},
        gold={
            "path": str(gold_path) if gold_path else None,
            "approved": len(approved), "checked": len(gold_checked),
            "failed": gold_failed, "missing": missing_gold, "pending": pending,
        },
        errors=errors,
        warnings=warnings,
    )
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def write_gold_template(
    input_csv: Path,
    adapter_path: Path,
    output_path: Path,
    *,
    provider: str | None = None,
    limit: int = 20,
    timeout_seconds: float = 8.0,
) -> int:
    """Create diverse, explicitly unapproved cases for human source review."""
    plan = select_samples(
        iter_source_records(input_csv, provider=provider),
        train_count=limit, holdout_count=0, smoke_count=0, max_groups=max(24, limit),
    )
    selected: list[tuple[Any, list[dict[str, Any]]]] = []
    for record in plan.train:
        execution = run_adapter(adapter_path, record, timeout_seconds=timeout_seconds)
        if not execution.ok or not validate_offers(record, execution.offers).ok:
            continue
        selected.append((record, execution.offers))
    cases = []
    for record, offers in selected:
        cases.append({
            "approved": False,
            "source_record_id": record.record_id,
            "source_url": record.url,
            "city": record.city,
            "expected_offer_count": len(offers),
            "offers": [
                {key: offer.get(key) for key in (
                    "name", "price", "price_old", "internet_speed_mbps", "tv_channels",
                    "mobile_minutes", "mobile_data_gb", "services",
                )}
                for offer in offers
            ],
            "review_note": "Compare with the saved source payload, then set approved=true.",
        })
    document = {"version": 1, "provider": provider, "cases": cases}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(cases)


def _load_gold(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("gold file must be a JSON object with a cases array")
    return value


def _compare_gold_case(case: dict[str, Any], actual: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    expected_count = case.get("expected_offer_count")
    if expected_count is not None and len(actual) != int(expected_count):
        errors.append(f"expected {expected_count} offers, got {len(actual)}")
    remaining = list(range(len(actual)))
    for index, expected in enumerate(case.get("offers") or []):
        match = next((position for position in remaining if _offer_matches(expected, actual[position])), None)
        if match is None:
            errors.append(f"expected offer {index} not found: {expected!r}")
        else:
            remaining.remove(match)
    return errors


def _offer_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, wanted in expected.items():
        got = actual.get(key)
        if isinstance(wanted, str):
            if re.sub(r"\s+", " ", str(got or "")).strip().casefold() != re.sub(r"\s+", " ", wanted).strip().casefold():
                return False
        elif isinstance(wanted, list):
            if got != wanted:
                return False
        elif wanted is None:
            if got is not None:
                return False
        elif not isinstance(got, (int, float)) or float(got) != float(wanted):
            return False
    return True


def has_independent_offer_signal(payload: str) -> bool:
    visible = html_text(payload)
    for price in _MONTHLY_PRICE.finditer(visible):
        # Keep this independent from DOM-card extraction, but require close textual
        # proximity so global navigation/cross-sell items do not masquerade as a card.
        window = visible[max(0, price.start() - 800):price.end() + 800]
        if _SPEED.search(window) and _HOME.search(window):
            return True
    return False


_has_independent_offer_signal = has_independent_offer_signal


def _append_limited(items: list[dict[str, Any]], value: dict[str, Any], limit: int = 100) -> None:
    if len(items) < limit:
        items.append(value)
