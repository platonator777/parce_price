from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit


SIGNALS = re.compile(
    r"(?:\b(?:тариф|интернет|скорост|мбит|гбит|руб|₽|цена|скидк|акци|месяц|мес\.?|"
    r"подключ|роутер|оборудован|телевид|канал|минут|гигабайт|gb|price|tariff|speed|"
    r"broadband|fiber|fttx|xpon|gpon|adsl|unavailable)\w*\b)", re.IGNORECASE
)
STRONG_SIGNALS = re.compile(
    r"(?:\d[\d\s]*(?:₽|руб(?:\.|лей)?|р\.?\s*/)|\d+(?:[.,]\d+)?\s*(?:мбит|гбит|mbps|gbps)|"
    r"(?:₽|руб(?:\.|лей)?)\s*\d)", re.IGNORECASE
)
RECURRING_PRICE = re.compile(
    r"(?:\d[\d\s]*(?:₽|руб(?:\.|лей)?|р\.?)\s*(?:/|в\s+)?\s*(?:мес|месяц)|"
    r"(?:₽|руб(?:\.|лей)?)\s*\d[\d\s]*(?:/\s*)?(?:мес|месяц))", re.IGNORECASE
)
SPEED_SIGNAL = re.compile(r"\d+(?:[.,]\d+)?\s*(?:мбит|гбит|mbps|gbps)", re.IGNORECASE)
UNAVAILABLE_SIGNAL = re.compile(r"(?:недоступ\w*|доступен\s+только\s+мобильн|нет\s+технической\s+возможности)", re.IGNORECASE)
FAQ_MARKER = re.compile(r"^(?:частые\s+вопросы|вопросы\s+и\s+ответы|faq|как\s+подключить\b)", re.IGNORECASE)
CARD_START = re.compile(
    r"^(?:(?:тарифы?|подписка|план|offer|plan)\b.{0,100}|"
    r"(?:домашн\w*\s+интернет|home\s+internet|broadband)(?:\s+и\s+(?:тв|телевидение))?)$",
    re.IGNORECASE,
)
COMMERCIAL_START = re.compile(r"^(?:тарифы?|подписка|план|offer|plan)\b", re.IGNORECASE)
HOME_START = re.compile(r"^(?:домашн\w*\s+интернет|home\s+internet|broadband)(?:\s+и\s+(?:тв|телевидение))?$", re.IGNORECASE)
HOME_OFFER_SIGNAL = re.compile(
    r"(?:домашн\w*\s+интернет|интернет\s+для\s+дома|для\s+дома|home\s+internet|broadband)",
    re.IGNORECASE,
)
BLOCK_NOISE = re.compile(r"^(?:аватар|avatar|logo|icon|image|подключить)$", re.IGNORECASE)
NOISE = re.compile(
    r"(?:cookie|куки|персональн\w+ данн|политик\w+ конфиденц|яндекс метрик|google analytics|"
    r"copyright|все права защищены)", re.IGNORECASE
)
USEFUL_META = re.compile(r"(?:^|:|_)(?:description|title|region|location|product|tariff|offer|price|city)(?:$|:|_)", re.IGNORECASE)
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul"
}
IGNORED_TAGS = {"style", "noscript", "svg", "canvas", "template"}


@dataclass(slots=True)
class ReducedHtml:
    text: str
    original_chars: int
    reduced_chars: int
    candidate_blocks: int


class _ContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.meta: list[str] = []
        self.scripts: list[tuple[str, str]] = []
        self._ignored_depth = 0
        self._script_attrs = ""
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "script":
            self._script_attrs = " ".join(f"{k}={v}" for k, v in attrs)
            self._script_parts = []
            return
        if tag in IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self.fragments.append("\n")
        if tag == "meta":
            label = attr_map.get("name") or attr_map.get("property")
            content = attr_map.get("content", "")
            if label and content and USEFUL_META.search(label) and not NOISE.search(label):
                self.meta.append(f"META {label}: {content}")
        if tag == "img" and attr_map.get("alt"):
            self.fragments.extend((" ", attr_map["alt"], " "))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            if self._script_parts is not None:
                self.scripts.append((self._script_attrs, "".join(self._script_parts)))
            self._script_parts = None
            self._script_attrs = ""
            return
        if tag in IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if not self._ignored_depth and tag in BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)
        elif not self._ignored_depth:
            self.fragments.append(data)


def reduce_html(raw_html: str, *, city: str = "", max_chars: int = 18_000) -> ReducedHtml:
    """Reduce arbitrary HTML to evidence-rich text that fits a small local model context."""
    if not raw_html.strip():
        return ReducedHtml("EMPTY HTML SNAPSHOT", len(raw_html), 19, 0)

    parser = _ContentParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        # HTMLParser is tolerant, and content accumulated before malformed markup is still useful.
        pass

    visible = _clean_lines("".join(parser.fragments))
    metadata = _deduplicate(_clean_lines("\n".join(parser.meta)))
    selected = _extract_offer_blocks(visible, max_chars=max_chars * 3 // 4)
    if not selected or not any(RECURRING_PRICE.search(line) for line in selected):
        selected = _select_candidate_windows(visible, max_chars=max_chars * 3 // 4)
    recurring_visible = sum(bool(RECURRING_PRICE.search(line)) for line in visible)
    script_budget = max_chars // 5 if recurring_visible < 2 else 0
    script_data = _extract_script_evidence(parser.scripts, max_chars=script_budget) if script_budget else []

    sections = []
    if metadata:
        sections.append("[PAGE METADATA]\n" + "\n".join(metadata[:40]))
    if selected:
        sections.append("[VISIBLE OFFER CANDIDATES]\n" + "\n".join(selected))
    if script_data:
        sections.append("[EMBEDDED DATA CANDIDATES]\n" + "\n".join(script_data))
    if not sections:
        sections.append("[VISIBLE PAGE TEXT]\n" + "\n".join(visible[:200]))

    result = "\n\n".join(sections)
    result = _normalize_page_identity(result, city)
    result = result[:max_chars]
    return ReducedHtml(result, len(raw_html), len(result), len(selected))


def _clean_lines(text: str) -> list[str]:
    text = html.unescape(text).replace("\xa0", " ")
    result: list[str] = []
    for raw_line in text.splitlines():
        line = _repair_mojibake(re.sub(r"\s+", " ", raw_line).strip(" |\t"))
        if not line or len(line) == 1 and not line.isdigit():
            continue
        if len(line) > 800:
            line = line[:800]
        result.append(line)
    joined: list[str] = []
    unit = re.compile(r"^(?:₽|руб(?:\.|лей)?|р\.?)\s*/?\s*(?:мес|месяц)$", re.I)
    period_only = re.compile(r"^/?\s*(?:мес|месяц)$", re.I)
    speed_unit_only = re.compile(r"^(?:мбит|гбит|mbps|gbps)(?:/с)?$", re.I)
    numeric = re.compile(r"^\d[\d\s]{0,8}(?:[.,]\d+)?$")
    for line in result:
        if period_only.fullmatch(line) and joined and re.search(r"(?:₽|руб(?:\.|лей)?|р\.?)$", joined[-1], re.I):
            joined[-1] = f"{joined[-1]} {line}"
            continue
        if speed_unit_only.fullmatch(line) and joined and numeric.fullmatch(joined[-1]):
            joined[-1] = f"{joined[-1]} {line}"
            continue
        if unit.fullmatch(line):
            indexes = []
            cursor = len(joined) - 1
            while cursor >= 0 and len(indexes) < 2 and numeric.fullmatch(joined[cursor]):
                indexes.append(cursor)
                cursor -= 1
            for index in indexes:
                joined[index] = f"{joined[index]} {line}"
            if indexes:
                continue
        joined.append(line)
    return joined


def _repair_mojibake(value: str) -> str:
    """Repair common UTF-8 decoded as Latin-1 once or twice in saved snapshots."""
    result = value
    for _ in range(2):
        if not any(marker in result for marker in ("Ð", "Ñ", "Ã", "Â")):
            break
        try:
            candidate = result.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate.count("Ð") + candidate.count("Ñ") + candidate.count("Ã") >= result.count("Ð") + result.count("Ñ") + result.count("Ã"):
            break
        result = candidate
    return result


def _select_candidate_windows(lines: list[str], *, max_chars: int) -> list[str]:
    if not lines:
        return []
    chosen: set[int] = set()
    used_estimate = 0
    centers: list[tuple[int, int]] = []
    recurring_indexes = [index for index, line in enumerate(lines) if RECURRING_PRICE.search(line)]
    first_recurring = recurring_indexes[0] if recurring_indexes else None
    cutoff = len(lines)
    if first_recurring is not None:
        for index in range(first_recurring + 1, len(lines)):
            if FAQ_MARKER.search(lines[index]):
                cutoff = index
                break
    for index, line in enumerate(lines):
        if index >= cutoff:
            continue
        if RECURRING_PRICE.search(line):
            centers.append((12, index))
        elif UNAVAILABLE_SIGNAL.search(line):
            centers.append((10, index))
        elif SPEED_SIGNAL.search(line):
            centers.append((7, index))
    # Fallback for unusual sites without recognizable recurring price formatting.
    if not centers:
        for index, line in enumerate(lines):
            score = len(SIGNALS.findall(line)) + 3 * len(STRONG_SIGNALS.findall(line))
            if score and not (NOISE.search(line) and score < 4):
                centers.append((score, index))
    # Strong numeric evidence wins the character budget. Source order is restored below.
    for score, index in sorted(centers, key=lambda item: (-item[0], item[1])):
        radius = 9 if score >= 12 else 5 if score >= 7 else 3
        window = set(range(max(0, index - radius), min(cutoff, index + radius + 1))) - chosen
        cost = sum(len(lines[item]) + 1 for item in window)
        if used_estimate + cost > max_chars and score < 3:
            continue
        if used_estimate + cost > max_chars:
            window = {item for item in window if STRONG_SIGNALS.search(lines[item]) or SIGNALS.search(lines[item])}
            cost = sum(len(lines[item]) + 1 for item in window)
        if used_estimate + cost <= max_chars:
            chosen.update(window)
            used_estimate += cost
    output: list[str] = []
    used = 0
    previous = -2
    for index in sorted(chosen):
        line = lines[index]
        if not re.sub(r"\W+", "", line):
            continue
        prefix = "… " if index > previous + 1 else ""
        candidate = prefix + line
        if used + len(candidate) + 1 > max_chars:
            continue
        output.append(candidate)
        used += len(candidate) + 1
        previous = index
    return output


def _extract_offer_blocks(lines: list[str], *, max_chars: int) -> list[str]:
    """Mark repeated offer-like regions so a small LLM cannot merge adjacent cards."""
    if not lines:
        return []
    raw_starts = [index for index, line in enumerate(lines) if len(line) <= 180 and CARD_START.search(line)]
    inferred_starts: set[int] = set()
    generic = re.compile(
        r"^(?:мобильная\s+связь|выбрать\s+тариф|тарифы?|для\s+дома|подробнее|"
        r"отправить\s+заявку|подключить)$", re.I,
    )
    for index, line in enumerate(lines):
        following = lines[index + 1:index + 14]
        mobile_position = next((
            offset for offset, item in enumerate(following[:4])
            if re.fullmatch(r"мобильн\w*\s+связь|mobile", item, re.I)
        ), None)
        home_position = next((
            offset for offset, item in enumerate(following[:10]) if HOME_START.search(item)
        ), None)
        if (
            2 <= len(line) <= 80 and not re.search(r"\d", line) and not generic.fullmatch(line)
            and not HOME_OFFER_SIGNAL.search(line)
            and not re.search(r"мобильн\w*\s+связь", line, re.I)
            and mobile_position is not None and home_position is not None
            and mobile_position < home_position
        ):
            raw_starts.append(index)
            inferred_starts.add(index)
    raw_starts = sorted(set(raw_starts))
    starts: list[int] = []
    for index in raw_starts:
        if (
            starts and index in inferred_starts and starts[-1] in inferred_starts
            and index - starts[-1] <= 4
            and not any(RECURRING_PRICE.search(line) for line in lines[starts[-1]:index])
        ):
            continue
        if (
            starts and index in inferred_starts and COMMERCIAL_START.search(lines[starts[-1]])
            and index - starts[-1] <= 2
            and not any(RECURRING_PRICE.search(line) for line in lines[starts[-1]:index])
        ):
            starts[-1] = index
            continue
        # A generic service/speed line immediately followed by a branded name is one card header.
        if (
            starts and COMMERCIAL_START.search(lines[index]) and HOME_START.search(lines[starts[-1]])
            and index - starts[-1] <= 16
            and not any(RECURRING_PRICE.search(line) for line in lines[starts[-1]:index])
        ):
            continue
        # The inverse ordering ("Tariff Name" then "Home internet") is also one
        # header and must retain the commercial name at the start of the block.
        if (
            starts and HOME_START.search(lines[index])
            and (COMMERCIAL_START.search(lines[starts[-1]]) or starts[-1] in inferred_starts)
            and index - starts[-1] <= 16
            and not any(RECURRING_PRICE.search(line) for line in lines[starts[-1]:index])
        ):
            continue
        starts.append(index)
    blocks: list[list[str]] = []
    seen: set[str] = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else min(len(lines), start + 40)
        block = [line for line in lines[start:end] if not BLOCK_NOISE.fullmatch(line)]
        if not any(RECURRING_PRICE.search(line) for line in block):
            continue
        first_speed = next((i for i, line in enumerate(block) if SPEED_SIGNAL.search(line)), None)
        detail_markers = [
            i for i, line in enumerate(block)
            if re.fullmatch(r"(?:подробнее|details?|learn\s+more)", line, re.I)
            and first_speed is not None and i < first_speed
        ]
        if detail_markers:
            trim = detail_markers[-1] + 1
            while trim < len(block) and re.search(r"(?:стрелоч|подробнее|arrow)", block[trim], re.I):
                trim += 1
            block = block[trim:]
        # Offer cards are compact; a large region usually means a navigation/SEO heading.
        if len(block) > 35:
            first_price = next((i for i, line in enumerate(block) if RECURRING_PRICE.search(line)), None)
            if first_price is None or first_price > 28:
                continue
            block = block[: min(len(block), first_price + 8)]
        last_price = max(i for i, line in enumerate(block) if RECURRING_PRICE.search(line))
        tail = last_price + 1
        if tail < len(block) and re.search(r"(?:скидк|акци|подар)", block[tail], re.IGNORECASE):
            tail += 1
        block = block[:tail]
        key = "\n".join(re.sub(r"\s+", " ", line).casefold() for line in block)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(block)
    if len(blocks) < 2:
        # A tariff detail page legitimately has one hero card. Keep it only when
        # the same compact region independently contains home-internet semantics,
        # speed and a recurring price; this excludes ordinary service/navigation
        # regions while avoiding the previous "repeated cards only" recall hole.
        if not blocks:
            return []
        single_text = "\n".join(blocks[0])
        if not (
            HOME_OFFER_SIGNAL.search(single_text)
            and SPEED_SIGNAL.search(single_text)
            and RECURRING_PRICE.search(single_text)
        ):
            return []
    output: list[str] = []
    used = 0
    for index, block in enumerate(blocks, start=1):
        rendered = [f"[OFFER BLOCK {index}]", *block]
        cost = sum(len(line) + 1 for line in rendered)
        if used + cost > max_chars:
            break
        output.extend(rendered)
        used += cost
    return output


def _extract_script_evidence(scripts: list[tuple[str, str]], *, max_chars: int) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for attrs, body in scripts:
        if not body or len(body) > 2_000_000:
            continue
        lower_attrs = attrs.lower()
        body_unescaped = _repair_mojibake(html.unescape(body))
        signal_count = len(SIGNALS.findall(body_unescaped))
        is_json = "json" in lower_attrs or body_unescaped.lstrip().startswith(("{", "["))
        if not is_json and signal_count < 3:
            continue
        for fragment in _script_fragments(body_unescaped):
            score = len(SIGNALS.findall(fragment)) + 3 * len(STRONG_SIGNALS.findall(fragment))
            if score:
                candidates.append((score, fragment))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    output: list[str] = []
    used = 0
    for _, fragment in candidates:
        normalized = re.sub(r"\s+", " ", fragment).strip()
        if len(normalized) > 1200:
            normalized = normalized[:1200]
        if not normalized or normalized in output:
            continue
        if used + len(normalized) + 1 > max_chars:
            break
        output.append(normalized)
        used += len(normalized) + 1
    return output


def _script_fragments(body: str) -> list[str]:
    # JSON-LD and hydration payloads often contain useful strings even when wrapped in JavaScript.
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        value = None
    if value is not None:
        strings: list[str] = []
        _walk_json(value, strings)
        return strings
    decoded = body.replace(r"\u0026", "&").replace(r"\n", " ")
    return [match.group(0) for match in re.finditer(r"[^{}\[\],;]{0,500}(?:₽|руб|мбит|гбит|тариф|скидк|price|speed)[^{}\[\],;]{0,700}", decoded, re.I)]


def _walk_json(value: object, output: list[str], path: str = "", depth: int = 0) -> None:
    if depth > 12 or len(output) > 2000:
        return
    if isinstance(value, dict):
        compact_parts = []
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(child, (str, int, float, bool)) and child not in ("", None):
                compact_parts.append(f"{key}={child}")
            else:
                _walk_json(child, output, child_path, depth + 1)
        compact = f"{path}: " + "; ".join(compact_parts)
        if compact_parts and SIGNALS.search(compact):
            output.append(compact)
    elif isinstance(value, list):
        for index, child in enumerate(value[:500]):
            _walk_json(child, output, f"{path}[{index}]", depth + 1)
    elif isinstance(value, str) and SIGNALS.search(value):
        output.append(f"{path}: {value}")


def _deduplicate(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for line in lines:
        key = line.casefold()
        if key not in seen:
            result.append(line)
            seen.add(key)
    return result


def _normalize_page_identity(text: str, city: str) -> str:
    if city:
        text = re.sub(re.escape(city), "<CITY>", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://[^/\s\"']+", "https://<HOST>", text)
    text = re.sub(r"(?im)^META\s+(?:REGION|LOCATION|CITY)[^\n]*\n?", "", text)
    text = re.sub(r"(?im)^META\s+PRODUCT_ID[^\n]*\n?", "", text)
    return text


def host_from_url(url: str) -> str:
    return urlsplit(url).hostname or ""
