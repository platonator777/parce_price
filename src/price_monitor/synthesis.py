from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapter_runtime import check_adapter_source, extract_python_code, run_adapter
from .context_pack import build_context_pack, relevant_fragment
from .sampling import SamplePlan, fingerprint, select_samples
from .source import iter_source_records
from .validation import validate_offers
from .quality_gate import has_independent_offer_signal
from .prompt_budget import fit_prompt, prompt_byte_budget, INPUT_MARKER
from .adapter_helpers import number, parse_json, json_items
from .atomic_files import replace_with_retry


SCAFFOLD_VERSION = "adapter-v2"
JSON_CONTRACT = '''Write only executable Python code defining def parse(record) -> list[RawOffer].
Allowed imports: json, re, math, html, typing, collections,
price_monitor.adapter_api and price_monitor.adapter_helpers.
Helpers: parse_json(payload), json_items(parsed), number(value), integer(value).
Import RawOffer from price_monitor.adapter_api.
record.payload is saved JSON. Return [] for invalid/empty input.
Iterate json_items(parse_json(record.payload)); ONE tariff per source item.
The root is a real list or supported catalog mapping, NOT the illustrative schema.
products is a real list of dictionaries, not a mapping with an item key.
Do not filter tariffs by technology or presence of products/speed: retain each named tariff.
Initialize speed = None before the products loop for EACH tariff.
Internet product uses productCode == 'SHPD', not codeTechnology == 'Internet'.
Never assign item['services'] or any other key: RawOffer fields are separate from raw_offer.
Always preserve the ORIGINAL WHOLE item with raw_offer=item. Never copy a subset,
mutate item, or emit nested products as tariffs. Compact examples are not runtime input.
RawOffer: name, price, price_old, price_period, connection_cost, technology,
internet_speed_mbps, tv_channels, mobile_minutes, mobile_data_gb,
services (list), evidence=[], raw_offer. Missing values are None.
Map source fields semantically: name, price, price_old, pricePeriod, costConnection, tech.
Use number() for numeric values. price_old must not be lower than price.
For products inspect speedVal only for the Internet service (e.g. SHPD).
Do not use iconText, v, codeTechnology as TV counts or mobile allowances.
Leave TV/mobile numeric fields None unless an explicit matching field exists.
services only: internet, tv, mobile, phone, cinema, gaming, other.
Populate services from productCode: SHPD -> internet, IPTV/ITV -> tv,
SOTOVAYA_SVYAZ_MVNO/VIRTUAL_MVNO -> mobile, ONLINE_CINEMA -> cinema,
GAMING_ADVANTAGES -> gaming. Include EVERY matching service, not just Internet/mobile.
Also use packagesTypes and pts for services.
codeTechnology is a network technology such as XPON, not a service label.
Do not append capitalized service names. Always number(product.get('speedVal')).
In the SHPD branch BOTH assign speed AND append 'internet' to services.
SIM cards are mobile, never phone. Deduplicate services with sorted(set(services)).
products describe included services, not separate offers. Do not change IDs/hashes.
No files/network/process/environment/dynamic imports. Keep code under 90 lines.
Return Python code only, never summarize the JSON.'''
CONTRACT = '''
Write adapter.py. Allowed imports: json, re, math, html, typing, collections and:
from price_monitor.adapter_api import RawOffer
from price_monitor.adapter_helpers import parse_json, json_items, number, integer, text, first, evidence_fragment, html_text, html_offer_blocks, html_block_facts, iter_json_dicts

Helper signatures are exact:
parse_json(payload); json_items(parsed_value); number(value); integer(value); text(value);
first(mapping, *keys); evidence_fragment(full_payload, needle); html_text(payload);
html_offer_blocks(payload, city) returns clean repeated offer-card text blocks;
html_block_facts(block) returns conservative name, current/old monthly prices, speed,
TV/mobile facts and canonical services for that exact block;
iter_json_dicts(parsed_value). Do not pass extra arguments.
Keep the entire file under 90 non-empty lines. Never repeat a field extraction or block.
An empty evidence list is valid; if used, each evidence string must be an exact substring
of record.payload. raw_offer must be the original top-level item object, not a copy or subset.

Required interface: def parse(record) -> list[RawOffer]
record.payload_type is "html" or "json"; record.payload contains saved data.
RawOffer fields: name (required), availability="available", price, price_old, price_period,
connection_cost, technology, internet_speed_mbps, tv_channels, mobile_minutes,
mobile_data_gb, services (list[str]), evidence (exact substrings), raw_offer (dict).
Every numeric field must be int/float/None: always apply number() or integer() to a
matched text line and never pass a numeric string. Skip duplicate card signatures.
services values are only: internet, tv, mobile, phone, cinema, gaming, other.
Do not access files/network/processes/environment, mutate globals, or use dynamic imports.
For JSON, preserve each entire source item in raw_offer. Do not change source IDs/hashes.
If JSON SCHEMA says root type=array, call json_items(data) directly and create one RawOffer
per top-level source offer; nested products describe services, they are not separate tariffs.
Never call number() on a list/dict. For internet speed, inspect product dictionaries and
use a numeric speedVal. Leave TV/mobile numeric fields None unless a matching semantic
field exists; do not reuse speedVal/iconText for unrelated facts. When validation says
a field "is not grounded in a matching JSON field", remove that assignment and keep it
None. iconText and v are not mobile minutes/data; codeTechnology is not a TV count.
Use ordinary dict access for nested arrays, e.g. products = item.get("products") or [].
Use first(item, "price", "cost") only with string field names; never pass default values.
For line lists use next((line for line in lines if "marker" in line.casefold()), None);
never call first() on a list and never pass a lambda to first().
For HTML, parse repeated catalog items; exclude mobile-only and ancillary-service pages.
For HTML catalogs, iterate html_offer_blocks(record.payload, record.city); never split raw HTML
on newlines. Each block is one card with clean lines and exact source text.
Use facts = html_block_facts(block), then construct exactly one RawOffer per block with
RawOffer(**facts, evidence=[], raw_offer={"block": block}). Do not reimplement facts.
The HTML branch must follow this immutable scaffold exactly:
    blocks = html_offer_blocks(record.payload, record.city)
    return [RawOffer(**html_block_facts(block), evidence=[], raw_offer={"block": block})
            for block in blocks]
Do not read nonexistent facts fields such as lines and do not parse price again.
For price choose a line containing both a currency marker and мес/месяц; do not use
equipment/connection/static-IP prices that lack the monthly-period marker.
Return [] on empty/unavailable/non-catalog input. Do not invent missing values.
Return only code, no Markdown.
'''.strip()


@dataclass(slots=True)
class Evaluation:
    ok: bool
    passed: int
    failed: int
    offers: int
    failures: list[dict[str, Any]]


@dataclass(slots=True)
class SynthesisResult:
    adapter_path: Path
    accepted: bool
    attempts: int
    train: Evaluation
    holdout: Evaluation
    smoke: Evaluation


def evaluate_adapter(path: Path, records: list, *, timeout_seconds: float = 8.0) -> Evaluation:
    passed = failed = offers = 0
    failures: list[dict[str, Any]] = []
    for record in records:
        execution = run_adapter(path, record, timeout_seconds=timeout_seconds)
        if not execution.ok:
            failed += 1
            failures.append({"source_record_id": record.record_id, "url": record.url, "error": execution.error, "stderr": execution.stderr[-2000:]})
            continue
        report = validate_offers(record, execution.offers)
        omissions = _json_omissions(execution.offers) if record.payload_type == 'json' else []
        offers += len(execution.offers)
        missing_signalled_offers = (
            record.payload_type == "html" and not execution.offers
            and has_independent_offer_signal(record.payload)
        )
        if report.ok and not missing_signalled_offers and not omissions:
            passed += 1
        else:
            failed += 1
            error = "; ".join(report.errors + omissions) or "independent offer signals but adapter returned no offers"
            failures.append({"source_record_id": record.record_id, "url": record.url, "error": error, "warnings": report.warnings})
    return Evaluation(failed == 0 and bool(records), passed, failed, offers, failures)


def _json_example(record, budget: int) -> str:
    parsed = parse_json(record.payload)
    lines = [f'ACTUAL ROOT TYPE: {type(parsed).__name__}. Iterate json_items(parsed).',
             'ITEM examples are projections only; preserve each WHOLE runtime item in raw_offer.']
    for item in json_items(parsed)[:2]:
        compact = {key: value for key, value in item.items()
                   if isinstance(value, (str, int, float, bool)) or value is None}
        compact = {key: value[:100] if isinstance(value, str) else value for key, value in compact.items()}
        products, seen = [], set()
        for product in item.get('products', []) if isinstance(item.get('products'), list) else []:
            if not isinstance(product, dict):
                continue
            code = str(product.get('productCode', product.get('code', '')))
            if code in seen:
                continue
            seen.add(code)
            products.append({key: product[key] for key in ('productCode', 'code', 'codeTechnology', 'speedVal', 'name', 'title') if key in product})
        if products:
            compact['products'] = products
        line = 'ITEM: ' + json.dumps(compact, ensure_ascii=False, separators=(',', ':'))
        if len(('\n'.join(lines + [line])).encode('utf-8')) > budget:
            break
        lines.append(line)
    if len(lines) == 2 and json_items(parsed):
        raise ValueError('JSON example does not fit the safe prompt budget; increase context size')
    return '\n'.join(lines)


def _json_omissions(offers: list[dict]) -> list[str]:
    """Known explicit JSON facts must not silently disappear during synthesis."""
    errors = []
    service_codes = {'SHPD': 'internet', 'IPTV': 'tv', 'ITV': 'tv',
                     'SOTOVAYA_SVYAZ_MVNO': 'mobile', 'VIRTUAL_MVNO': 'mobile',
                     'ONLINE_CINEMA': 'cinema', 'GAMING_ADVANTAGES': 'gaming'}
    for index, offer in enumerate(offers):
        raw = offer.get('raw_offer')
        if not isinstance(raw, dict):
            continue
        for field in ('price', 'price_old'):
            expected = number(raw.get(field))
            current = number(raw.get('price'))
            if field == 'price_old' and current is not None and expected is not None and expected < current:
                continue
            if expected is not None and offer.get(field) != expected:
                errors.append(f'offer {index}: preserve explicit source {field}={expected}; use item.get("{field}")')
        products = raw.get('products')
        if not isinstance(products, list):
            continue
        for product in products:
            if not isinstance(product, dict):
                continue
            code = product.get('productCode')
            service = service_codes.get(code)
            if service and service not in (offer.get('services') or []):
                errors.append(f'offer {index}: include service {service} from productCode {code}')
            speed = number(product.get('speedVal')) if code == 'SHPD' else None
            if speed is not None and offer.get('internet_speed_mbps') != speed:
                errors.append(f'offer {index}: preserve SHPD speedVal={speed} as internet_speed_mbps')
    return errors


def synthesize_adapter(input_csv: Path, output_root: Path, client, *, provider: str | None = None, max_records: int | None = None, max_attempts: int = 5, train_count: int = 4, holdout_count: int = 3, smoke_count: int = 2, context_chars: int = 24_000, seed: int = 42, resume: bool = False, timeout_seconds: float = 8.0) -> SynthesisResult:
    plan = select_samples(iter_source_records(input_csv, provider=provider, max_records=max_records), train_count=train_count, holdout_count=holdout_count, smoke_count=smoke_count)
    if not plan.train or not plan.holdout:
        raise ValueError("not enough structurally sampled records for train and holdout")
    selected_provider = provider or plan.train[0].provider
    artifact_dir = output_root / _safe_provider(selected_provider)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    adapter_path = artifact_dir / "adapter.py"
    contract = JSON_CONTRACT if all(record.payload_type == 'json' for record in plan.train) else CONTRACT
    safe_bytes = min(context_chars, prompt_byte_budget(client.context_size, 2048))
    data_bytes = safe_bytes - len((contract + INPUT_MARKER).encode('utf-8'))
    if data_bytes < 256:
        raise ValueError('context budget is too small for instructions and examples')
    # The validators still evaluate every train/holdout record. A compact JSON
    # example avoids repeating the same large catalog schema across all cities.
    context_records = plan.train[:1] if contract == JSON_CONTRACT else plan.train
    context = build_context_pack(context_records, max_chars=data_bytes)
    context_text = context.text
    if contract == JSON_CONTRACT:
        example_record = next((record for record in plan.train if json_items(parse_json(record.payload))), plan.train[0])
        context_text = _json_example(example_record, data_bytes)
    prompt = fit_prompt(contract, context_text, max_bytes=safe_bytes)
    (artifact_dir / "generation_prompt.txt").write_text(prompt, encoding="utf-8")
    (artifact_dir / "context_pack.txt").write_text(context_text, encoding="utf-8")
    attempts_path = artifact_dir / "attempts.jsonl"
    start_attempt = _existing_attempts(attempts_path) if resume else 0
    if not resume:
        attempts_path.write_text("", encoding="utf-8")
    current_code = adapter_path.read_text(encoding="utf-8") if resume and adapter_path.exists() else ""
    best_code, best_score = current_code, -10**9
    best_train = Evaluation(False, 0, len(plan.train), 0, [])
    best_holdout = Evaluation(False, 0, len(plan.holdout), 0, [])
    if current_code:
        best_train = evaluate_adapter(adapter_path, plan.train, timeout_seconds=timeout_seconds)
        best_holdout = evaluate_adapter(adapter_path, plan.holdout, timeout_seconds=timeout_seconds)
        best_score = _evaluation_score(best_train, best_holdout)
    accepted = bool(current_code and best_train.ok and best_holdout.ok)
    current_train, current_holdout = best_train, best_holdout
    attempts_run = start_attempt
    for attempt in range(start_attempt + 1, max_attempts + 1):
        if accepted:
            break
        attempts_run = attempt
        attempt_prompt = prompt if not current_code else _repair_prompt(current_code, current_train, current_holdout, plan, safe_bytes, attempt, contract=contract)
        attempt_prompt = fit_prompt(contract, attempt_prompt.partition(INPUT_MARKER)[2], max_bytes=safe_bytes)
        # A deterministic per-attempt seed prevents a failed repair from replaying
        # byte-for-byte forever while remaining fully reproducible from the base seed.
        response = client.generate_code(attempt_prompt, seed=seed + attempt - 1, num_predict=2048)
        code = extract_python_code(response)
        check = check_adapter_source(code)
        temp_path = artifact_dir / f"adapter.attempt-{attempt}.py"
        temp_path.write_text(code, encoding="utf-8")
        if check.ok:
            train_eval = evaluate_adapter(temp_path, plan.train, timeout_seconds=timeout_seconds)
            holdout_eval = evaluate_adapter(temp_path, plan.holdout, timeout_seconds=timeout_seconds)
        else:
            failure = [{"error": item} for item in check.errors]
            train_eval = Evaluation(False, 0, len(plan.train), 0, failure)
            holdout_eval = Evaluation(False, 0, len(plan.holdout), 0, [])
        score = _evaluation_score(train_eval, holdout_eval)
        attempt_entry = {
            "attempt": attempt, "seed": seed + attempt - 1, "prompt_sha256": _sha(attempt_prompt), "response": response,
            "prompt_bytes": len(attempt_prompt.encode('utf-8')), "safe_prompt_bytes": safe_bytes,
            "code_sha256": _sha(code), "static_errors": check.errors,
            "train": asdict(train_eval), "holdout": asdict(holdout_eval), "score": score,
        }
        with attempts_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(attempt_entry, ensure_ascii=False) + "\n")
        if score > best_score:
            best_code, best_score = code, score
            best_train, best_holdout = train_eval, holdout_eval
            _atomic_write(adapter_path, code)
        # A summary or syntactically invalid response is not a parser to repair.
        current_code = code if check.ok else ''
        current_train, current_holdout = train_eval, holdout_eval
        if train_eval.ok and holdout_eval.ok:
            accepted = True
            best_code, best_train, best_holdout = code, train_eval, holdout_eval
            _atomic_write(adapter_path, code)
            break
        # A failed holdout becomes visible to repair; replace it with untouched smoke where possible.
        if train_eval.ok and holdout_eval.failed and plan.smoke:
            promoted = plan.holdout.pop(0)
            plan.train.append(promoted)
            plan.holdout.append(plan.smoke.pop(0))
    if not adapter_path.exists():
        _atomic_write(adapter_path, best_code)
    smoke_eval = evaluate_adapter(adapter_path, plan.smoke, timeout_seconds=timeout_seconds) if plan.smoke else Evaluation(False, 0, 0, 0, [])
    validation = {
        "accepted": accepted, "attempts": attempts_run, "records_seen": plan.records_seen,
        "train_count": len(plan.train), "holdout_count": len(plan.holdout), "smoke_count": len(plan.smoke),
        "train": asdict(best_train), "holdout": asdict(best_holdout), "smoke": asdict(smoke_eval),
        "fingerprint_groups": len(plan.fingerprints),
        "covered_fingerprint_groups": {
            "train": sorted({fingerprint(record).key for record in plan.train}),
            "holdout": sorted({fingerprint(record).key for record in plan.holdout}),
            "smoke": sorted({fingerprint(record).key for record in plan.smoke}),
        },
        "train_success_rate": best_train.passed / max(1, best_train.passed + best_train.failed),
        "holdout_success_rate": best_holdout.passed / max(1, best_holdout.passed + best_holdout.failed),
        "smoke_success_rate": smoke_eval.passed / max(1, smoke_eval.passed + smoke_eval.failed),
        "best_score": best_score,
    }
    (artifact_dir / "validation_report.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "provider": selected_provider, "model": client.model, "seed": seed,
        "context_size": client.context_size, "scaffold_version": SCAFFOLD_VERSION,
        "schema_version": "raw-offer-v1", "code_sha256": _sha(adapter_path.read_text(encoding="utf-8")),
        "created_at": datetime.now(timezone.utc).isoformat(), "input": str(input_csv),
        "runtime_sha256": _runtime_hash(),
        "fingerprints": plan.fingerprints, "metrics": validation,
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return SynthesisResult(adapter_path, accepted, attempts_run, best_train, best_holdout, smoke_eval)


def _repair_prompt(code: str, train: Evaluation, holdout: Evaluation, plan: SamplePlan, budget: int, attempt: int, *, contract: str = CONTRACT) -> str:
    failures = (train.failures + holdout.failures)[:3]
    snippets = []
    by_id = {record.record_id: record for record in plan.train + plan.holdout}
    for failure in failures:
        record = by_id.get(failure.get("source_record_id"))
        if record:
            snippets.append({'error': failure.get('error'), 'fragment': relevant_fragment(record, max_chars=600)})
    errors = '\n'.join((str(failure.get('error', ''))[:300] + '\n' + str(failure.get('stderr', ''))[-700:]) for failure in failures[:1])
    if contract == JSON_CONTRACT:
        snippets = []
    data = f'REPAIR ATTEMPT {attempt}: return a corrected complete parser.\nERRORS:\n{errors}\nCURRENT CODE:\n{code}\nINPUT EXAMPLES:\n' + json.dumps(snippets, ensure_ascii=False)
    if len((contract + INPUT_MARKER + data).encode('utf-8')) > budget:
        prefix = f'Regenerate a complete parser from scratch. Fix these errors:\n{errors}\n'
        if contract == JSON_CONTRACT:
            example_record = next((record for record in plan.train if json_items(parse_json(record.payload))), plan.train[0])
            remaining = budget - len((contract + INPUT_MARKER + prefix).encode('utf-8'))
            fragment = _json_example(example_record, remaining)
        else:
            fragment = relevant_fragment(plan.train[0], max_chars=1800)
        data = prefix + fragment
    return fit_prompt(contract, data, max_bytes=budget)


def _existing_attempts(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    replace_with_retry(temporary, path)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _runtime_hash() -> str:
    package = Path(__file__).parent
    content = "".join(
        (package / name).read_text(encoding="utf-8")
        for name in ("adapter_api.py", "adapter_helpers.py", "adapter_runtime.py", "validation.py")
    )
    return _sha(content)


def _safe_provider(value: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._") or "unknown"


def _evaluation_score(train: Evaluation, holdout: Evaluation) -> int:
    evaluations = (train, holdout)
    violations = sum(
        str(failure.get("error", "")).count(";") + 1
        for evaluation in evaluations for failure in evaluation.failures
    )
    return (
        1000 * sum(evaluation.passed for evaluation in evaluations)
        + sum(evaluation.offers for evaluation in evaluations)
        - 2000 * sum(evaluation.failed for evaluation in evaluations)
        - 50 * violations
    )
